import os
import sys
import socket
import threading
import random
import time
import logging
import argparse
from mutagen import File as MutagenFile # pip install mutagen
from pathlib import Path
import curses  # standard‑library “curses” UI
import pychromecast
from pychromecast.controllers import media

#import ffmpeg

#def get_duration_from_file_ffprobe(path: str) -> float:
#    """Return duration (seconds) using ffprobe; requires ffprobe in PATH."""
#    probe = ffmpeg.probe(path)
#    format_info = probe.get("format", {})
#    return float(format_info.get("duration", 0))

SUPPORTED_EXTENSIONS = {
    ".mp3", ".aac", ".m4a", ".wav", ".flac", ".ogg", ".opus"
}

def get_duration_from_file(path: str) -> float:
    """
    Return duration in seconds, or raise if the file is unreadable.
    """
    audio = MutagenFile(path)
    if audio is None or not hasattr(audio, "info"):
        raise ValueError(f"Cannot read duration from {path}")
    return float(audio.info.length)


def scan_music(root: Path) -> List[Path]:
    """Recursively collect all supported audio files."""
    files = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(p)
    return files

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

def get_interface_ip() -> str:
    """Return the IPv4 address of the NIC that can reach the Internet."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"

def discover_chromecast(name_filter: Optional[str] = None, 
                        ip_filter: Optional[str] = None,
                        port_filter: Optional[int] = None) -> List[pychromecast.Chromecast, str, str, int]:
    chromecasts, _ = pychromecast.get_chromecasts()
    chosen_cast = None
    chosen_name = None
    chosen_ip = None
    chose_port = None
    for cast in chromecasts:
        cast_info = getattr(cast, "cast_info", None)
        if cast_info == None:
            continue

        chosen_name = getattr(cast_info, "friendly_name", None)
        if name_filter and chosen_name!=name_filter:
            print("❌ Failed to get chromecast name")
            quit()
        elif chosen_name == None:
            continue

        chosen_ip = getattr(cast_info, "host", None)
        if ip_filter and ip_filter!=chosen_ip:
            print("❌ Failed to get chromecast ip adress")
            quit()
        elif chosen_ip == None:
            continue
        
        chosen_port = getattr(cast_info, "port", None)
        if port_filter and port_filter!=chosen_port:
            print("❌ Failed to retrieve chromecast port number")
            quit()
        elif chosen_port == None:
            continue
        
        chosen_cast = cast
        break

    if chosen_cast == None:
        print("❌ Failed to find chromecast")
        quit()

    return [chosen_cast, chosen_name, chosen_ip, chosen_port]

# ----------------------------------------------------------------------
# 4️⃣  Tiny HTTP server (daemon thread)
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
                # Ignore it – the client closed the connection.
                pass

    class ThreadingTCPServer(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = ThreadingTCPServer(("", port), QuietHTTPRequestHandler)

    thread = threading.Thread(target=httpd.serve_forever,
                              name="HTTP‑Server",
                              daemon=True)
    thread.start()
    print(f"▶️  HTTP server listening at http://{get_interface_ip()}:{port}/")
    return thread, httpd
# ----------------------------------------------------------------------



# ----------------------------------------------------------------------
# 6️⃣  Chromecast wrapper – playback, status listener and UI‑friendly API
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
        cast: pychromecast.Chromecast,
        music_files: List[Path],
        music_root,
        host_ip: str,
        http_port: int,
    ):
        self.cast = cast
        self.mc = cast.media_controller
        self.music_files = music_files
        self.music_root = music_root
        self.host_ip = host_ip
        self.http_port = http_port

        # Public attributes that the UI reads
        self.current_path: Optional[Path] = None
        self.current_mime: Optional[str] = None
        self.state: str = media.MEDIA_PLAYER_STATE_UNKNOWN #"IDLE"
        self.idle_reason: str = None
        self.elapsed: float = 0.0   # seconds
        self.duration: float = 0.0  # seconds (0 means unknown)

        self.previous_played = None

        # Register for media‑status callbacks (called from a background thread)
        self.mc.register_status_listener(self)

        # A lock protects the above public attributes from race conditions
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    #  Calls the media controller update_status function, which updates
    #  status data and triggers the callback function registered with
    #  register_status_listener. This poll function is not needed to be
    #  called for state changes, but for elapsed time changes.
    #  I.e. call poll every second to get a valid current_time value.
    # ------------------------------------------------------------------
    def poll(self):
        self.mc.update_status()

    # ------------------------------------------------------------------
    #  Media status callback – invoked by pychromecast whenever the
    #  Cast reports a change (play, pause, finish, etc.)
    # ------------------------------------------------------------------
    def new_media_status(self, status):
        with self._lock:
            # Update generic state (PLAYING, PAUSED, IDLE, …)
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
        rel = path.relative_to(music_root).as_posix()
        return f"http://{self.host_ip}:{self.http_port}/{rel}"

    # ------------------------------------------------------------------
    #  Core playback commands
    # ------------------------------------------------------------------
    def is_idle_and_finished(self) -> bool:
        if self.idle_reason == "FINISHED":
            return True
        return False

    def play_random(self):
        """Pick a random file and command the Cast to play it."""
        candidates = [f for f in self.music_files if f!= self.previous_played]
        if not candidates:
            self.previous_played = random.choice(self.music_files)
        else:
            self.previous_played = random.choice(candidates)

        url = self.build_url(self.previous_played, self.music_root)
        mime = guess_mime_type(self.previous_played)

        # Store the current track info *before* we ask the Cast to play it.
        with self._lock:
            self.current_path = self.previous_played
            self.current_mime = mime
            self.state = media.MEDIA_PLAYER_STATE_BUFFERING
            self.elapsed = 0.0
            self.duration = get_duration_from_file(self.current_path)

        self.mc.play_media(url, mime, stream_type="BUFFERED")
        self.mc.block_until_active()   # blocks until the Cast acknowledges the stream

    def pause(self):
        """Toggle pause / resume."""
        with self._lock:
            if self.state == media.MEDIA_PLAYER_STATE_PLAYING:
                self.mc.pause()
                #self.state = media.MEDIA_PLAYER_STATE_PAUSED
            elif self.state == media.MEDIA_PLAYER_STATE_PAUSED:
                self.mc.play()
                #self.state = media.MEDIA_PLAYER_STATE_PLAYING

    def stop(self):
        """Stop playback – Cast goes to IDLE."""
        self.mc.stop()
        with self._lock:
            #self.state = media.MEDIA_PLAYER_STATE_IDLE
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
# 7️⃣  Curses UI
# ----------------------------------------------------------------------
class CursesUI:
    """
    Very small UI that refreshes the screen about 2 times per second
    and reacts to single‑key commands.

    Keys:
        n – next random track
        p – pause / resume
        s – stop (go idle)
        q – quit the program
    """
    HELP_LINE = "Controls: [n]ext  [p]ause/resume  [s]top  [q]uit"

    def __init__(self, player: CastPlayer, total_tracks: int, music_root):
        self.player = player
        self.total_tracks = total_tracks
        self.music_root = music_root
        self._stop_requested = False
        self.play_next = True # If player is idle and finished, play next random track if true

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
            self._draw(stdscr)
            self._handle_input(stdscr)
            self.player.poll()
            if self.player.is_idle_and_finished() and self.play_next:
                self.player.play_random()

    # --------------------------------------------------------------
    #  Draw the whole screen.
    # --------------------------------------------------------------
    def _draw(self, stdscr):
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()

        # ----- Header -------------------------------------------------
        stdscr.addstr(0, 0, "📻  Chromecast Terminal Player", curses.A_BOLD)

        # ----- Current track info ------------------------------------
        with self.player._lock:
            path = self.player.current_path
            mime = self.player.current_mime
            state = self.player.state
            elapsed = self.player.elapsed
            duration = self.player.duration

        track_line = "No track playing" if path is None else f"{path.relative_to(self.music_root)}"
        stdscr.addstr(2, 0, f"Track : {track_line}")

        mime_line = mime if mime else "–"
        stdscr.addstr(3, 0, f"MIME  : {mime_line}")

        stdscr.addstr(4, 0, f"State : {state}")

        # ----- Progress bar -------------------------------------------
        bar_width = max_x - 20
        if duration > 0:
            frac = min(elapsed / duration, 1.0)
            time_str = f"{int(elapsed)}/{int(duration)} s"
        else:
            frac = 0.0
            time_str = f"{int(elapsed)} s / ?"

        bar = self._progress_bar(bar_width, frac)
        stdscr.addstr(5, 0, f"{bar} {time_str}")

        # ----- Library size -------------------------------------------
        stdscr.addstr(7, 0, f"Library size : {self.total_tracks} tracks")

        # ----- Help line -----------------------------------------------
        stdscr.addstr(max_y - 2, 0, self.HELP_LINE, curses.A_DIM)

        stdscr.refresh()

    # --------------------------------------------------------------
    #  Handle a single key press (non‑blocking).
    # --------------------------------------------------------------
    def _handle_input(self, stdscr):
        ch = stdscr.getch()
        if ch == -1:
            return  # no key pressed

        if ch in (ord('q'), ord('Q')):
            self._stop_requested = True
            # Stop playback cleanly before we exit curses
            self.player.stop()
        elif ch in (ord('n'), ord('N')):
            #self.player.stop()
            self.player.play_random()
        elif ch in (ord('p'), ord('P')):
            self.player.pause()
        elif ch in (ord('s'), ord('S')):
            self.player.stop()
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Music player for a Chromecast with a terminal UI."
    )
    parser.add_argument(
        "--music_dir",
        type=str,
        default = Path.home() / "Music",
        help="Root directory that contains your music (default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="TCP port for the tiny HTTP server (default: %(default)s)",
    )
    parser.add_argument(
        "--chromecast-name",
        dest="cast_name",
        help="Friendly name of the Chromecast to use (optional)",
    )
    parser.add_argument(
        "--chromecast-ip",
        dest="cast_ip",
        help="If you know the IP of the Cast, set it here (optional)",
    )
    args = parser.parse_args()

    music_root = Path(args.music_dir)
    music_files = scan_music(music_root)
    if not music_files:
        sys.exit(f"❌ No supported audio files found in {music_root}")
    print(f"📂 {len(music_files)} playable files – starting UI…")

    cast, cast_name, cast_ip, cast_port = discover_chromecast(args.cast_name, args.cast_ip)
    print(f"✅ Found Chromecast: {cast_name} ({cast_ip}:{cast_port})")

    server_thread, httpd = start_http_server(music_root, args.port)
    local_ip = get_interface_ip()
    player = CastPlayer(cast, music_files, music_root, local_ip, args.port)
    player.start()

    ui = CursesUI(player, total_tracks=len(music_files), music_root=music_root)

    try:
        curses.wrapper(ui.run)   # <-- blocks until the user presses ‘q’
    finally:
        # ------------------------------------------------------------------
        # 7️⃣  Clean shutdown (stop playback, close HTTP server, disconnect)
        # ------------------------------------------------------------------
        print("\n👋  Shutting down …")
        player.stop()
        httpd.shutdown()
        server_thread.join()
        cast.disconnect()
        print("✅ Clean exit.")

if __name__ == "__main__":
    main()
