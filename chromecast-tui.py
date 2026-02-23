import os
import sys
import socket
import threading
import zeroconf
import queue
import random
import time
import logging
import argparse

from collections import deque # Pop and push last songs played

import curses  # standard‑library “curses” UI
from dataclasses import dataclass
from mutagen import File as MutagenFile # pip install mutagen
from mutagen import MutagenError
from pathlib import Path
import pychromecast
from pychromecast.controllers import media

browser = None
#logging.basicConfig(filename='myapp.log', level=logging.INFO)
#logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".mp3", ".aac", ".m4a", ".wav", ".flac", ".ogg", ".opus"
}


class ChromecastListener(pychromecast.discovery.AbstractCastListener):
    """Listener for discovering chromecasts."""
    def __init__(self):
        self.browser: CastBrowser = None
        self.services = None
        self._lock = threading.Lock()
        
    def add_cast(self, uuid: UUID, service: str) -> None:
        """Called when a new cast has beeen discovered."""
        with self._lock:
            self.services = list(self.browser.services.values())

    def remove_cast(self, uuid: UUID, service: str, cast_info: CastInfo) -> None:
        """Called when a cast has beeen lost (MDNS info expired or host down)."""
        with self._lock:
            self.services = list(self.browser.services.values())

    def update_cast(self, uuid: UUID, service: str) -> None:
        """Called when a cast has beeen updated (MDNS info renewed or changed)."""
        with self._lock:
            self.services = list(self.browser.services.values())

# Custom notification handler
class UINotificationHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        msg = self.format(record)
        self.q.put(msg)


def get_duration_from_file(path: str) -> float:
    """
    Return duration in seconds, or raise if the file is unreadable.
    """
    try:
        audio = MutagenFile(path)
        if audio is None or not hasattr(audio, "info"):
            raise ValueError(f"Cannot read duration from {path}")
        return float(audio.info.length)
    except MutagenError as e:
        raise ValueError(f"Failed to read duration for {path}: {e}") from e


def scan_music(root: Path) -> List[Path]:
    """Recursively collect all supported audio files."""
    valid_files = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                _ = get_duration_from_file(p)
                valid_files.append(p)
            except ValueError as e:
                logger.warning(f"Skipping unreadable file {p} : {e}")
    return valid_files

def guess_mime_type(p: Path) -> str:
    ext = p.suffix.lower()
    return {
        ".mp3":  "audio/mpeg",
        ".wav":  "audio/wav",
        ".flac": "audio/flac",
        ".aac":  "audio/aac",
        ".m4a":  "audio/mp4",
        ".ogg":  "audio/ogg",
        ".oga":  "audio/ogg",
        ".opus": "audio/opus",
        ".mka":  "audio/x-matroska",
    }.get(ext, "audio/mpeg")

def get_local_ip() -> str:
    """Return the IPv4 address of the NIC that can reach the Internet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        logger.warning("Failed to determine local IP; using 127.0.0.1")
        return "127.0.0.1"

# ----------------------------------------------------------------------
# Tiny HTTP server (daemon thread)
# ----------------------------------------------------------------------
def start_http_server(root: Path, port: int):
    """Serve *root* via SimpleHTTPRequestHandler on *port*."""
    import http.server
    import socketserver

    os.chdir(root)   # SimpleHTTPRequestHandler serves relative to cwd
    class QuietHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
        """Same as the stock handler but silently discards request logs."""
        def log_message(self, format, *args):
            # Do nothing – no log line is printed.
            pass
        
        def copyfile(self, source, outputfile):
            """Override the default implementation to catch
            ConnectionResetError caused by the Chromecast disconnecting."""
            try:
                super().copyfile(source, outputfile)
            except ConnectionResetError:
                logger.debug("Client disconnected during file transfer (ignored).")

    class ThreadingTCPServer(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = ThreadingTCPServer(("", port), QuietHTTPRequestHandler)
    thread = threading.Thread(target=httpd.serve_forever,
                              name="HTTP‑Server",
                              daemon=True)
    thread.start()
    logger.info(f"▶️  HTTP server listening at http://{get_local_ip()}:{port}/")
    return thread, httpd
# ----------------------------------------------------------------------

class _StatusListener:
    """Wraps CastPlayer to avoid circular references."""

    def __init__(self, player: CastPlayer):
        self._player = player

    def new_media_status(self, status: media.MediaStatus):
        self._player._handle_status_change(status)

# ----------------------------------------------------------------------
# CastPlayer – playback, status listener
# ----------------------------------------------------------------------
class CastPlayer:
    """
    Handles:
        • connection / disconnection,
        • random playback,
        • pause / stop / next,
        • a tiny status‑listener that updates public attributes
          (used by the UI).
    """
    def __init__(
        self,
        cast: pychromecast.ChromeCast,
        cast_info: CastInfo,
        music_files: List[Path],
        music_root: Path,
        local_ip: str,
        http_port: int,
        retry_delay: float = 2.0,
    ):
        self.cast = cast
        self.cast_info = cast_info
        self.music_files = music_files
        self.unplayed_files = []
        self.three_last_played_files = deque()
        self.music_root = music_root
        self.local_ip = local_ip
        self.http_port = http_port
        self.retry_delay = retry_delay
        self._mc: Optional[pychromecast.controllers.media.MediaController] = None
        self._status_listener = _StatusListener(self)
        self._lock = threading.Lock()

        # Public attributes that the UI reads
        self.current_path: Optional[Path] = None
        self.current_mime: Optional[str] = None
        self.state: str = media.MEDIA_PLAYER_STATE_UNKNOWN #"IDLE"
        self.idle_reason: Optional[str] = None
        self.elapsed: float = 0.0   # seconds
        self.duration: float = 0.0  # seconds (0 means unknown)


    def __enter__(self):
        self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        self.cast.disconnect()
        return False

    def _connect(self):
        logger.info(f"Connecting to '{self.cast_info.friendly_name}'...")
        try:
            self.cast.wait()
            self._mc = self.cast.media_controller
            self._mc.register_status_listener(self._status_listener)
            logger.info(f"✅ Connected to '{self.cast_info.friendly_name}'")
            self.play_random()
        except Exception as e:
            raise RuntimeError(f"Failed to connect to Chromecast: {e}") from e

    # ------------------------------------------------------------------
    #  Calls the media controller update_status function, which updates
    #  status data and triggers the callback function registered with
    #  register_status_listener. This poll function is not needed to be
    #  called for state changes, but for elapsed time changes.
    #  I.e. call poll every second to get a valid current_time value.
    # ------------------------------------------------------------------
    def poll(self):
        try:
            self._mc.update_status()
        except pychromecast.error.PyChromecastError as e:
            logger.debug(f"Status update failed: {e}")

    # ------------------------------------------------------------------
    #  Media status callback – invoked by pychromecast whenever the
    #  Cast reports a change (play, pause, finish, etc.)
    # ------------------------------------------------------------------
    def _handle_status_change(self, status):
        with self._lock:
            self.state = status.player_state or media.MEDIA_PLAYER_STATE_UNKNOWN
            self.idle_reason = status.idle_reason
            
            # Spooky, status.duration seems to work sometimes, if it fails
            # use the preinitialized self.duration value from when after
            # we loaded the file.
            #self.duration = float(status.duration or self.duration) 
            self.elapsed = float(status.current_time or -1)

    # ------------------------------------------------------------------
    #  Helper to build a URL reachable by the Chromecast
    # ------------------------------------------------------------------
    def build_url(self, path: Path, music_root) -> str:
        try:
            rel = path.relative_to(music_root).as_posix()
            return f"http://{self.local_ip}:{self.http_port}/{rel}"
        except ValueError as e:
            raise ValueError(f"{path} not under music root {self.music_root}") from e

    # ------------------------------------------------------------------
    #  Core playback commands
    # ------------------------------------------------------------------
    def is_idle_and_finished(self) -> bool:
        while self._lock:
            return self.state == media.MEDIA_PLAYER_STATE_IDLE and self.idle_reason == "FINISHED"

    def play_random(self):
        """Pick a random file and command the Cast to play it."""
        if len(self.music_files) == 0:
            return

        if len(self.unplayed_files) == 0:
            self.unplayed_files = self.music_files

        new_path = random.choice(self.unplayed_files)
        self.unplayed_files.remove(new_path)
        if len(self.three_last_played_files) > 2:
            self.three_last_played_files.popright();

        if self.current_path:
            self.three_last_played_files.appendleft(self.current_path)


        #candidates = [f for f in self.music_files if f!= self.current_path]
        #if not candidates:
        #    new_path = random.choice(self.music_files)
        #else:
        #    new_path = random.choice(candidates)

        try:
            url = self.build_url(new_path, self.music_root)
        except ValueError as e:
            logger.error(f"Failed to build URL for file: {e}")
            return

        mime = guess_mime_type(new_path)

        try:
            duration = get_duration_from_file(new_path)
        except ValueError as e:
            logger.error(f"Cannot get duration for {new_path}: {e}")
            return

        # Store the current track info *before* we ask the Cast to play it.
        with self._lock:
            self.current_path = new_path
            self.current_mime = mime
            self.duration = duration
            #self.state = media.MEDIA_PLAYER_STATE_BUFFERING
            self.elapsed = 0.0

        try:
            self._mc.play_media(url, mime, stream_type="BUFFERED")
            self._mc.block_until_active()   # blocks until the Cast acknowledges the stream
        except (pychromecast.error.PyChromecastError, TimeoutError) as e:
            logger.error(f"Playback failed for {url}: {e}")
            with self._lock:
                self.state = media.MEDIA_PLAYER_STATE_IDLE
        except Exception as e:
            logger.exception(f"Unexpected error playing {self.current_path}: {e}")

    def pause(self):
        """Toggle pause / resume."""
        try:
            self._mc.pause() if self.state == media.MEDIA_PLAYER_STATE_PLAYING else self._mc.play()
        except pychromecast.error.PyChromecastError as e:
            logger.error(f"Pause/Resume failed: {e}")

    def stop(self):
        """Stop playback – Cast goes to IDLE."""
        try:
            self._mc.stop()
        except pychromecast.error.PyChromecastError as e:
            logger.warning(f"Stop failed: {e}")

        with self._lock:
            self.state = media.MEDIA_PLAYER_STATE_IDLE
            self.elapsed = 0.0
            self.duration = 0.0
            self.current_path = None
            self.current_mime = None

    # ------------------------------------------------------------------
    #  Public entry point – wait for the handshake then start first track
    # ------------------------------------------------------------------
    def start(self):
        self.cast.wait()
        address = getattr(self.cast.cast_info, "host", "unknown address")
        print(f"✅ Connected to '{self.cast.name}' ({address})")
        self.play_random()
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# ChromecastPlayer UI
# ----------------------------------------------------------------------
class ChromecastPlayerUI:
    """
    Very small UI that refreshes the screen about 2 times per second
    and reacts to single‑key commands.

    Keys:
        n – next random track
        p – pause / resume
        s – stop (go idle)
        q – quit the program
    """

    def __init__(self, player: CastPlayer, total_tracks: int, music_root):
        self.player = player
        self.total_tracks = total_tracks
        self.music_root = music_root
        self._stop_requested = False
        self.play_next = True # If player is idle and finished, play next random track if true
        self.HELP_LINES = [
            "Controls: [n]ext  [p]ause/resume  [s]top  [q]uit",
            "Last message: ",
        ]

        # Catching pychromecast log output, which will be presented in the UI (see run function in ChromecastPlayerUI)
        self.pycc_log_queue = queue.Queue()

        pycc_logger = logging.getLogger("pychromecast")
        pycc_log_handler = UINotificationHandler(self.pycc_log_queue)
        pycc_log_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        pycc_logger.addHandler(pycc_log_handler)
        #pycc_logger.setLevel(logging.WARNING)



    # --------------------------------------------------------------
    #  Helper to draw a simple horizontal progress bar.
    # --------------------------------------------------------------
    @staticmethod
    def _progress_bar(width: int, fraction: float) -> str:
        """Return a string like '[####.....]' where fraction∈[0,1]."""
        filled = int(width * fraction)
        empty = width - filled
        return "[" + "#" * filled + "." * empty + "]"

    # --------------------------------------------------------------
    #  Main curses loop – runs until the user presses ‘q’.
    # --------------------------------------------------------------
    def run(self, stdscr):

        curses.curs_set(0)                # hide the cursor
        stdscr.nodelay(True)              # getch() is non‑blocking
        stdscr.timeout(1000)              # refresh every 1.0 s
       

        while not self._stop_requested:
            while not self.pycc_log_queue.empty():
                self.HELP_LINES[1] = f"Last message: {self.pycc_log_queue.get_nowait()}"

            max_y, max_x = stdscr.getmaxyx()
            self._draw(stdscr, max_y, max_x)
            self._handle_input(stdscr)
            self.player.poll()
            try:
                if self.player.is_idle_and_finished() and self.play_next:
                    self.player.play_random()
            except Exception as e:
                logger.error(f"Auto-play failed: {e}")

    # --------------------------------------------------------------
    #  Draw the whole screen.
    # --------------------------------------------------------------
    def _draw(self, stdscr, max_y, max_x):
        stdscr.erase()

        # ----- Header -------------------------------------------------
        stdscr.addstr(0, 0, f"📻  Chromecast Terminal Player - {self.player.cast_info.friendly_name}", curses.A_BOLD)

        # ----- Current track info ------------------------------------
        with self.player._lock:
            path = self.player.current_path
            mime = self.player.current_mime
            state = self.player.state
            elapsed = self.player.elapsed
            duration = self.player.duration
            last_played = self.player.three_last_played_files

        track_line = "No track playing" if path is None else f"{path.relative_to(self.music_root)}"
        stdscr.addstr(2, 0, f"Track : {track_line}")
        stdscr.addstr(3, 0, f"MIME  : {mime or '-'}")
        stdscr.addstr(4, 0, f"State : {state}")

        # ----- Progress bar -------------------------------------------
        bar_width = max_x - 24
        if duration > 0:
            frac = min(elapsed / duration, 1.0)
            time_str = f"{int(elapsed)}/{int(duration)} s"
        else:
            frac = 0.0
            time_str = f"{int(elapsed)} s / ?"

        filled = int(bar_width * frac)
        bar = "[" + "█" * filled + "░" * (bar_width - filled) + "]"
        stdscr.addstr(5, 0, f"{bar} {time_str}")

        # Library info
        stdscr.addstr(7, 0, f"📂 Library : {self.total_tracks} tracks")
        stdscr.addstr(8, 0, "Three last played songs:")
        for i, song in enumerate(last_played, 9):
            stdscr.addstr(i, 0, f"{song.relative_to(self.music_root)}")

        # Help line
        for i, line in enumerate(self.HELP_LINES, start=max_y - 2):
            stdscr.addstr(i, 0, line[:max_x - 2], curses.A_DIM)

        stdscr.refresh()
    # --------------------------------------------------------------
    #  Handle a single key press (non‑blocking).
    # --------------------------------------------------------------
    def _handle_input(self, stdscr):
        ch = stdscr.getch()
        if ch == -1:
            return  # no key pressed

        try:
            if ch in (ord('q'), ord('Q')):
                self._stop_requested = True
                self.player.stop()
            elif ch in (ord('n'), ord('N')):
                self.player.play_random()
            elif ch in (ord('p'), ord('P')):
                self.player.pause()
            elif ch in (ord('s'), ord('S')):
                self.player.stop()
        except Exception as e:
            logger.exception(f"Control error: {e}")
# ----------------------------------------------------------------------

class SelectCastDeviceUI:
    def __init__(self):
        self.current_page = 0
        self.browser: CastBrowser = None
        self.cc_listener: ChromecastListener = None
        self.selected_index = 0
        self._stop_requested = False
        self.cast_info = None

    def _try_select(self) -> bool:
        with self.cc_listener._lock:
            if self.cc_listener.services == None or self.selected_index >= len(self.cc_listener.services):
                return False
            self.cast_info = self.cc_listener.services[self.selected_index]
            return True

    def run(self, stdscr):
        zconf = zeroconf.Zeroconf()
        self.cc_listener = ChromecastListener()
        self.cc_browser = pychromecast.discovery.CastBrowser(self.cc_listener, zconf)
        self.cc_listener.browser = self.cc_browser
        self.cc_browser.start_discovery()

        curses.curs_set(0)                # hide the cursor
        stdscr.nodelay(True)              # getch() is non‑blocking
        stdscr.timeout(500)               # refresh every 0.5 s

        while not self._stop_requested:
            max_y, max_x = stdscr.getmaxyx()
            self._draw(stdscr, max_y, max_x)
            self._handle_input(stdscr)

        self.cc_browser.stop_discovery()

    def _draw(self, stdscr, max_y, max_x):
        stdscr.erase()
        
        # ----- Header -------------------------------------------------
        stdscr.addstr(0, 0, f"📻  Chromecast Terminal Player - Discovering devices", curses.A_BOLD)
        
        index = 0
        with self.cc_listener._lock:
            if self.cc_listener.services != None:
                if self.selected_index!=0 and self.selected_index >= len(self.cc_listener.services):
                    self.selected_index = len(self.cc_listener.services) - 1
                for service in self.cc_listener.services:
                    selected_marker = "✅" if index == self.selected_index else " "
                    stdscr.addstr(index + 2, 0, f"[{selected_marker}] '{service.friendly_name}' ({service.model_name}) @ {service.host}:{service.port}")
                    index+=1

        #stdscr.addstr(max_y - 2, 0, "Select in page: [0-9]")
        stdscr.addstr(max_y - 2, 0, " ↑ / ↓ to navigate • ⏎ to select • q[quit]")

        stdscr.refresh()

    def _handle_input(self, stdscr):
        ch = stdscr.getch()
        if ch == -1:
            return  # no key pressed
        try:
            if ch == curses.KEY_DOWN:
                self.selected_index+=1
            elif ch == curses.KEY_UP:
                if self.selected_index > 0:
                    self.selected_index-=1
            elif ch in (curses.KEY_ENTER, ord('\n'), ord('\r')):
                self._stop_requested = self._try_select()
            elif ch == (ord('q') or ord('Q')):
                self.cast_info = None
                self._stop_requested = True
        except Exception as e:
            logger.exception(f"Control error: {e}")


def main():
   
    parser = argparse.ArgumentParser(
        description="Music player for a Chromecast with a terminal UI."
    )
    parser.add_argument(
        "--music_dir",
        type=str,
        default = Path.home() / "Music",
        help="Root directory with music files (default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="HTTP port for serving music (default: %(default)s)",
    )
    #parser.add_argument(
    #    "--chromecast-name",
    #    dest="cast_name",
    #    help="Friendly name of the Chromecast (optional)",
    #)
    #parser.add_argument(
    #    "--chromecast-ip",
    #    dest="cast_ip",
    #    help="IP adress of the Chromecast (optional)",
    #)
    #parser.add_argument(
    #    "--port-filter",
    #    type=int,
    #    dest="port_filter",
    #    help="Port to filter (e.g., 8009) (optional)",
    #)
    args = parser.parse_args()

    
    music_root = Path(args.music_dir)
    if not music_root.exists():
        sys.exit(f"❌ Music directory does not exist: {music_root}")
    logger.info(f"📂 Scanning {music_root}...")    
    music_files = scan_music(music_root)
    if not music_files:
        sys.exit(f"❌ No supported audio files found in {music_root}")
    print(f"📂 Found {len(music_files)} playable tracks")


    cc_select_ui = SelectCastDeviceUI()
    curses.wrapper(lambda stdscr: cc_select_ui.run(stdscr))
    if cc_select_ui.cast_info == None:
        return
    cast_info = cc_select_ui.cast_info
    cast = pychromecast.get_chromecast_from_cast_info(cast_info, zeroconf.Zeroconf())
    print(f"✅ Found Chromecast: {cast_info.friendly_name} ({cast_info.host}:{cast_info.port})")

    
    server_thread, httpd = start_http_server(music_root, args.port)

    
    local_ip = get_local_ip()
    with CastPlayer(cast, cast_info, music_files, music_root, local_ip, args.port) as player:
        logger.info("▶️  Starting UI (press 'q' to quit)...")
        try:
            curses.wrapper(lambda stdscr: ChromecastPlayerUI(player, len(music_files), music_root).run(stdscr))
        except KeyboardInterrupt:
            logger.info("\n👋 KeyboardInterrupt detected — exiting cleanly.")
        finally:
            logger.info("\n👋  Shutting down …")
            httpd.shutdown()
            httpd.server_close()
            server_thread.join()
            logger.info("✅ Clean exit.")

if __name__ == "__main__":
    main()
