#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════╗
║      MUSICLOOP — Termux Edition v2.2         ║
║   Hardened · Streaming · Daemon-ready        ║
╚══════════════════════════════════════════════╝

Author: Dr. Asghar Abbas Askari
MBBS Year 1 — Nishtar Medical University Multan


Fixes in v2.1:
  [1] Non-blocking tty input (raw mode) for controls
  [2] IPC errors now logged, not swallowed
  [3] yt-dlp retry logic (3 attempts + timeout)
  [4] Queue: remove by index, move up/down
  [5] settings_menu properly returns cfg
  [6] Per-process IPC socket (no multi-instance conflicts)
  [7] Streaming mode (instant play, no download)
  [8] Background daemon mode (play + detach)
"""

import random
import subprocess
import time
import os
import sys
import glob
import json
import argparse
import threading
import socket
import tty
import termios
import select
from datetime import datetime
from pathlib import Path
import tempfile
# ─────────────────────────────────────────────
#  PATHS & CONFIG
# ─────────────────────────────────────────────

CONFIG_PATH  = Path.home() / ".musicloop.json"
HISTORY_PATH = Path.home() / ".musicloop_history.json"
DB_PATH   = Path.home() / ".musicloop_queue.json"
DAEMON_PID   = Path.home() / ".musicloop_daemon.pid"
DAEMON_SOCK  = Path.home() / ".musicloop_daemon.sock"

# Per-process socket — no multi-instance conflicts [Fix #6]

IPC_SOCKET = os.path.join(tempfile.gettempdir(), f"mpv_{os.getpid()}.sock")

DEFAULT_DOWNLOAD_DIR = "/data/data/com.termux/files/home/storage/downloads"

DEFAULT_CONFIG = {
    "download_dir": DEFAULT_DOWNLOAD_DIR,
    "audio_format": "mp3",
    "volume": 100,
    "notify": True,
    "wake_lock": True,
    "retry_count": 3,
    "retry_delay": 3,
}


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            base = DEFAULT_CONFIG.copy()
            base.update(json.load(f))
            return base
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

def clear_screen():
    os.system("cls" if os.name == 'nt' else "clear")

# ─────────────────────────────────────────────
#  ANSI COLORS
# ─────────────────────────────────────────────

class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    CYAN    = "\033[96m"
    RED     = "\033[91m"
    MAGENTA = "\033[95m"
    WHITE   = "\033[97m"

def banner():
    print(f"""
{C.CYAN}{C.BOLD}╔══════════════════════════════════════════╗
║           🎧 MUSICLOOP v2.2              ║
║         Termux Edition · Hardened        ║
║          Streaming · Daemon Mode         ║
║                                          ║
║              Programmed By:              ║
║          Dr. Asghar Abbas Askari         ║
╚══════════════════════════════════════════╝{C.RESET}
""")

def ok(msg):   print(f"{C.GREEN}✔  {msg}{C.RESET}")
def err(msg):  print(f"{C.RED}✘  {msg}{C.RESET}")
def info(msg): print(f"{C.CYAN}ℹ  {msg}{C.RESET}")
def warn(msg): print(f"{C.YELLOW}⚠  {msg}{C.RESET}")
def head(msg): print(f"\n{C.MAGENTA}{C.BOLD}── {msg} ──{C.RESET}")


# ─────────────────────────────────────────────
#  TERMUX INTEGRATIONS
# ─────────────────────────────────────────────

def termux_available(cmd):
    return subprocess.run(["which", cmd], capture_output=True).returncode == 0

def acquire_wake_lock():
    if termux_available("termux-wake-lock"):
        subprocess.Popen(["termux-wake-lock"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        info("Wake lock acquired")

def release_wake_lock():
    if termux_available("termux-wake-unlock"):
        subprocess.run(["termux-wake-unlock"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        info("Wake lock released")

def send_notification(title, content):
    if termux_available("termux-notification"):
        subprocess.Popen(
            ["termux-notification", "--title", title, "--content", content, "--id", "musicloop"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

def check_storage_permission():
    test = Path("/data/data/com.termux/files/home/storage/downloads")
    if not test.exists():
        warn("Storage not set up. Run: termux-setup-storage")
        return False
    return True

def check_dependencies():
    missing = []
    for dep in ["mpv", "yt-dlp", "socat"]:
        if not termux_available(dep):
            missing.append(dep)
    if missing:
        err(f"Missing: {', '.join(missing)}")
        print(f"{C.DIM}Install via: pkg install {' '.join(missing)}{C.RESET}")
        sys.exit(1)


# ─────────────────────────────────────────────
#  NON-BLOCKING TTY INPUT  [Fix #1]
# ─────────────────────────────────────────────

class RawInput:
    """
    Context manager: puts terminal in raw mode.
    Reads single keypresses using select() — never blocks.
    Falls back gracefully if tty unavailable (piped input).
    """
    def __init__(self):
        self.is_raw = False
        self._old_settings = None

    def __enter__(self):
        try:
            self._old_settings = termios.tcgetattr(sys.stdin)
            tty.setraw(sys.stdin.fileno())
            self.is_raw = True
        except Exception:
            self.is_raw = False
        return self

    def __exit__(self, *_):
        if self.is_raw and self._old_settings:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old_settings)

    def read_key(self, timeout=0.2):
        """Non-blocking read. Returns char or None."""
        if not self.is_raw:
            return None
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if ready:
            return sys.stdin.read(1)
        return None


# ─────────────────────────────────────────────
#  HISTORY
# ─────────────────────────────────────────────

def load_history():
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH) as f:
            return json.load(f)
    return []

def save_to_history(title, path, source):
    history = []
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH) as f:
            history = json.load(f)
    history.insert(0, {
        "title": title,
        "path": str(path),
        "source": source,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M")
    })
    history = history[:50]
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)
    db = load_db()
    db["history"].insert(0, {
        "title": title,
        "path": str(path),
        "source": source,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M")
    })
    db["history"] = db["history"][:50]
    save_db(db)

def show_history():
    history = load_history()
    if not history:
        info("No history yet.")
        return None

    head("Recent Songs")
    for i, entry in enumerate(history[:15], 1):
        exists = "✔" if os.path.exists(entry.get("path", "")) else "✘"
        color = C.GREEN if exists == "✔" else C.RED
        title = entry.get("title", "Unknown Title")[:50]
        date = entry.get("date", "Unknown Date")
        print(f"  {C.DIM}{i:2}.{C.RESET} {color}{exists}{C.RESET} "
              f"{title:<50} {C.DIM}{date}{C.RESET}")

    print()
    print(f"{C.CYAN}Commands: {C.RESET}number (play), {C.CYAN}q 1 2 3{C.RESET} (add to queue), "
          f"{C.CYAN}qp 1-5{C.RESET} (add & play), {C.CYAN}Enter{C.RESET} (skip)")
    choice = input(f"{C.CYAN}> {C.RESET}").strip()
    if not choice:
        return None
    parts = choice.split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    arg_str = parts[1] if len(parts) > 1 else ""
    if cmd in ["q", "queue", "a", "add"]:
        action = "queue"
        indices_str = arg_str
    elif cmd in ["qp", "queueplay"]:
        action = "queueplay"
        indices_str = arg_str
    else:
        action = "play"
        indices_str = cmd if cmd.isdigit() or '-' in cmd or ',' in cmd else choice

    selected_indices = set()
    if indices_str:
        for part in indices_str.replace(',', ' ').split():
            if '-' in part:
                try:
                    start, end = map(int, part.split('-'))
                    selected_indices.update(range(start, end + 1))
                except ValueError:
                    err(f"Invalid range: {part}")
                    return None
            elif part.isdigit():
                selected_indices.add(int(part))
            else:
                err(f"Invalid input: {part}")
                return None
    if not selected_indices:
        err("No valid indices provided")
        return None
    valid_paths = []
    valid_titles = []
    missing_count = 0
    for idx in sorted(selected_indices):
        if idx < 1 or idx > len(history):
            warn(f"Index {idx} out of range (1-{len(history)})")
            continue
        entry = history[idx - 1]
        path = entry.get("path", "")
        title = entry.get("title", "Unknown")
        if os.path.exists(path):
            valid_paths.append({"path": path, "title": title})
            valid_titles.append(f"#{idx}: {title[:40]}")
        else:
            missing_count += 1
            warn(f"#{idx}: {title} - file missing")
    if not valid_paths:
        err("No valid files found")
        return None
    if action == "play":
        if len(valid_paths) > 1:
            info(f"Multiple selections in play mode. Using first: {valid_paths[0]['title']}")
        return valid_paths[0]["path"]
    elif action == "queue":
        for item in valid_paths:
            add_to_queue(item["path"], item["title"])
        msg = f"Added {len(valid_paths)} songs to queue"
        if missing_count:
            msg += f" ({missing_count} missing, skipped)"
        ok(msg)
        if len(valid_paths) == 1:
            return valid_paths[0]["path"]
        return {"action": "queue_added", "count": len(valid_paths)}
    elif action == "queueplay":
        for item in valid_paths:
            add_to_queue(item["path"], item["title"])
        msg = f"Added {len(valid_paths)} songs to queue"
        if missing_count:
            msg += f" ({missing_count} missing, skipped)"
        ok(msg)

        if valid_paths:
            info("Starting queue playback...")
            return valid_paths[0]["path"]

    return None

# ─────────────────────────────────────────────
#  QUEUE  [Fix #4: remove by index, move up/down]
# ─────────────────────────────────────────────

def load_db():
    default_db = {
        "queues": {"default": []},
        "state": {
            "current_queue": "default",
            "index": 0,
            "mode": "normal",
            "playing": False
        },
        "history": []
    }

    if not DB_PATH.exists():
        return default_db

    try:
        with open(DB_PATH, "r") as f:
            db = json.load(f)

        # ---------------------------
        # FIX: OLD FORMAT HANDLING
        # ---------------------------

        if isinstance(db, list):
            warn("Old queue format detected. Migrating...")
            db = {
                "queues": {"default": db},
                "state": {
                    "current_queue": "default",
                    "index": 0,
                    "mode": "normal",
                    "playing": False
                },
                "history": []
            }
            save_db(db)
            return db

        if not isinstance(db, dict):
            warn("Invalid DB format. Resetting.")
            return default_db

        # ensure structure exists
        db.setdefault("queues", {"default": []})
        db.setdefault("state", {})
        db.setdefault("history", [])

        db["state"].setdefault("current_queue", "default")
        db["state"].setdefault("index", 0)
        db["state"].setdefault("mode", "normal")
        db["state"].setdefault("playing", False)

        return db

    except json.JSONDecodeError:
        warn("Corrupted DB file. Resetting.")
        return default_db



def save_db(db):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=str(DB_PATH.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(db, f, indent=2)
        os.replace(tmp, DB_PATH)
    except Exception as e:
        err(f"DB save failed: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)

def _q(db, name):
    if "queues" not in db:
        db["queues"] = {}
    db["queues"].setdefault(name, [])
    return db["queues"][name]


def current(db):
    return db["state"]["current_queue"]


def set_queue(db, name):
    db["queues"].setdefault(name, [])
    db["state"]["current_queue"] = name


def state(db):
    return db["state"]

def add_to_queue(path, title="Unknown", queue_name=None):
    db = load_db()
    qname = queue_name or current(db)
    q = _q(db, qname)

    if not Path(path).exists():
        err("File not found")
        return

    if any(x["path"] == str(path) for x in q):
        warn("Already in queue")
        return

    q.append({"path": str(path), "title": title})
    save_db(db)
    ok(f"Added → [{qname}] {title}")

def remove_from_queue(index, queue_name=None):
    db = load_db()
    qname = queue_name or current(db)
    q = _q(db, qname)

    if 1 <= index <= len(q):
        removed = q.pop(index - 1)
        save_db(db)
        ok(f"Removed: {removed['title']}")
        return removed

    err("Invalid index")

def move_queue_item(index, direction, queue_name=None):
    db = load_db()
    qname = queue_name or current(db)
    q = _q(db, qname)

    i = index - 1
    j = i + direction

    if 0 <= i < len(q) and 0 <= j < len(q):
        q[i], q[j] = q[j], q[i]
        save_db(db)
        ok("Moved item")
        return True

    err("Cannot move")
    return False

def show_queue(queue_name=None):
    db = load_db()
    qname = queue_name or current(db)
    q = _q(db, qname)

    if not q:
        info(f"[{qname}] empty")
        return

    head(f"Queue: {qname} ({len(q)})")

    for i, item in enumerate(q, 1):
        exists = Path(item["path"]).exists()
        symbol = "✔" if exists else "✘"
        color = C.GREEN if exists else C.RED

        print(f"{i:2}. {color}{symbol}{C.RESET} {item['title']}")

def set_mode(mode):
    db = load_db()
    if mode not in ["normal", "shuffle", "repeat_all"]:
        err("Invalid mode")
        return

    db["state"]["mode"] = mode
    save_db(db)
    ok(f"Mode → {mode}")


def next_index(db, q):
    """
    Calculate next index based on current mode.
    Returns new index without modifying db state.
    """
    if not q:
        return 0

    st = db["state"]
    mode = st.get("mode", "normal")
    current_idx = st.get("index", 0)

    if mode == "shuffle":
        new_idx = current_idx
        while new_idx == current_idx and len(q) > 1:
            new_idx = random.randint(0, len(q) - 1)
        return new_idx

    elif mode == "repeat_all":
        return (current_idx + 1) % len(q)

    else:
        return min(current_idx + 1, len(q) - 1)

# ─────────────────────────────────────────────
#  DOWNLOAD WITH RETRY  [Fix #3]
# ─────────────────────────────────────────────

def run_ytdlp_with_retry(cmd, cfg, label="Download"):
    """
    Run yt-dlp command with retry logic.
    Streams stdout live. Returns (returncode, file_path, stderr).
    """
    retries = cfg.get("retry_count", 3)
    delay   = cfg.get("retry_delay", 3)

    for attempt in range(1, retries + 1):
        if attempt > 1:
            warn(f"Retry {attempt}/{retries} in {delay}s...")
            time.sleep(delay)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            file_path = None
            print(f"{C.DIM}", end="", flush=True)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("/"):
                    file_path = line
                elif line:
                    print(f"  {line}", flush=True)
            print(C.RESET, end="")

            stderr_out = proc.stderr.read()
            proc.wait()

            if proc.returncode == 0:
                return proc.returncode, file_path, stderr_out

            # [Fix #2] log errors instead of swallowing
            err(f"{label} failed (attempt {attempt})")
            if stderr_out:
                print(f"{C.DIM}  yt-dlp: {stderr_out[:200].strip()}{C.RESET}")

        except Exception as e:
            err(f"{label} error (attempt {attempt}): {e}")  # [Fix #2]

    return -1, None, "All retries exhausted"

def search_results(query, limit=6):
    """Return list of (title, url_or_id) from yt-dlp search"""
    cmd = [
        "yt-dlp", "--flat-playlist", "--print", "%(title)s|%(url)s",
        "--no-playlist", "--limit", str(limit),
        f"ytsearch{limit}:{query}"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return []

        entries = []
        for line in result.stdout.strip().split('\n'):
            if '|' in line:
                title, url = line.rsplit('|', 1)
                entries.append((title.strip(), url.strip()))
        return entries
    except Exception:
        return []


def search_and_download(query, cfg, selected_idx=0):
    info(f"Searching: {C.BOLD}{query}{C.RESET}")
    entries = search_results(query)
    if not entries:
        err("No results found")
        return None, None
    if selected_idx == 0 and len(entries) > 1:
        head(f"Search results for: {query}")
        for i, (title, _) in enumerate(entries[:6], 1):
            print(f"  {C.CYAN}{i}.{C.RESET} {title[:70]}")
        print(f"  {C.CYAN}0.{C.RESET} Cancel")
        choice = input(f"\n{C.WHITE}Pick number (1-{len(entries)}): {C.RESET}").strip()
        if not choice.isdigit() or int(choice) == 0:
            return None, None
        selected_idx = int(choice) - 1
    if selected_idx >= len(entries):
        err("Invalid selection")
        return None, None
    title, url_or_id = entries[selected_idx]
    info(f"Downloading: {title[:50]}")
    os.makedirs(cfg["download_dir"], exist_ok=True)
    cmd = [
        "yt-dlp", "-x",
        "--audio-format", cfg["audio_format"],
        "-o", f"{cfg['download_dir']}/%(title)s.%(ext)s",
        "--print", "after_move:filepath",
        "--newline", url_or_id
    ]
    rc, file_path, _ = run_ytdlp_with_retry(cmd, cfg, "Download")
    if rc != 0 or not file_path or not os.path.exists(file_path):
        err("Could not download song.")
        return None, None
    title = Path(file_path).stem
    ok(f"Downloaded: {title}")
    if cfg.get("notify"):
        send_notification("🎵 Download Complete", title)
    save_to_history(title, file_path, "yt-dlp")
    return file_path, title

def download_from_url(url, cfg):
    info("Downloading from URL...")
    os.makedirs(cfg["download_dir"], exist_ok=True)

    cmd = [
        "yt-dlp", "-x",
        "--audio-format", cfg["audio_format"],
        "-o", f"{cfg['download_dir']}/%(title)s.%(ext)s",
        "--print", "after_move:filepath",
        "--newline", url
    ]

    rc, file_path, _ = run_ytdlp_with_retry(cmd, cfg, "Download")

    if rc != 0 or not file_path or not os.path.exists(file_path):
        err("Could not download.")
        return None, None

    title = Path(file_path).stem
    ok(f"Downloaded: {title}")
    if cfg.get("notify"):
        send_notification("🎵 Download Complete", title)
    save_to_history(title, file_path, url)
    return file_path, title


# ─────────────────────────────────────────────
#  STREAMING MODE  [Feature #7]
# ─────────────────────────────────────────────

def get_stream_url(query, cfg, selected_idx=0):
    """
    DEPRECATED: Use direct resolution in interactive menu instead.
    Kept for backward compatibility with CLI --stream flag.
    Get direct audio stream URL — optional selection from results
    """
    entries = search_results(query)
    if not entries:
        err("No results found")
        return None, None

    # Only show selection if no index provided or index is 0
    if selected_idx <= 0:
        head(f"Search results for: {query}")
        for i, (title, _) in enumerate(entries[:6], 1):
            print(f"  {C.CYAN}{i}.{C.RESET} {title[:70]}")
        print(f"  {C.CYAN}0.{C.RESET} Cancel")
        choice = input(f"\n{C.WHITE}Pick number (1-{len(entries)}): {C.RESET}").strip()
        if not choice.isdigit() or int(choice) == 0:
            return None, None
        selected_idx = int(choice) - 1
    else:
        selected_idx = selected_idx  # Use provided index (0-based)

    if selected_idx >= len(entries):
        err("Invalid selection")
        return None, None

    title, url_or_id = entries[selected_idx]
    info(f"Resolving stream for: {title[:50]}")

    # If it's a full URL, return directly
    if url_or_id.startswith(('http://', 'https://')):
        return url_or_id, title

    # Otherwise resolve to stream URL
    resolve_cmd = ["yt-dlp", "-f", "bestaudio", "-g", url_or_id]
    try:
        result = subprocess.run(resolve_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            stream_url = result.stdout.strip().split('\n')[0]
            return stream_url, title
    except Exception as e:
        err(f"Resolution error: {e}")
    return None, None


def play_stream(query, loop_mode, loop_value, cfg):
    """Stream audio directly — zero storage used."""
    result = get_stream_url(query, cfg)
    if not result:
        err("Could not get stream URL.")
        return
    stream_url, title = result
    play_file(stream_url, loop_mode, loop_value, cfg, f"[STREAM] {title}")


# ─────────────────────────────────────────────
#  MPV IPC CONTROLLER  [Fix #2: errors logged]
# ─────────────────────────────────────────────

class MPVController:
    def __init__(self, socket_path=None):
        self.socket_path = socket_path or IPC_SOCKET

    def _send(self, command_dict):
        try:
            payload = json.dumps(command_dict) + "\n"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect(self.socket_path)
                s.sendall(payload.encode())
                return s.recv(4096).decode().strip()
        except socket.timeout:
            err("IPC timeout — mpv may have closed")
            return None
        except FileNotFoundError:
            err(f"IPC socket not found: {self.socket_path}")
            return None
        except ConnectionRefusedError:
            err("IPC connection refused — mpv not running?")
            return None
        except Exception as e:
            err(f"IPC error: {e}")   # [Fix #2] no silent swallowing
            return None

    def pause(self):           self._send({"command": ["set_property", "pause", True]})
    def resume(self):          self._send({"command": ["set_property", "pause", False]})
    def toggle_pause(self):    self._send({"command": ["cycle", "pause"]})
    def set_volume(self, v):   self._send({"command": ["set_property", "volume", v]})
    def volume_up(self, s=5):  self._send({"command": ["add", "volume", s]})
    def volume_down(self, s=5):self._send({"command": ["add", "volume", -s]})
    def quit(self):            self._send({"command": ["quit"]})

    def get_title(self):
        resp = self._send({"command": ["get_property", "media-title"]})
        if resp:
            try:
                return json.loads(resp).get("data", "")
            except Exception as e:
                err(f"IPC parse error: {e}")
        return ""

    def is_alive(self):
        return os.path.exists(self.socket_path)


# ─────────────────────────────────────────────
#  NON-BLOCKING CONTROL LOOP  [Fix #1]
# ─────────────────────────────────────────────

CONTROLS_HELP = (
    "  [space]=pause/resume  [+/-]=volume  "
    "[v N+Enter]=set vol  [q]=quit  [?]=help"
)

DEBUG = True

def dprint(msg):
    if DEBUG:
        print(msg)


def control_loop_raw(mpv: MPVController):
    """
    Raw tty single-keypress control.
    select() makes reads non-blocking — zero lag.
    """
    print(f"{C.DIM}{CONTROLS_HELP}{C.RESET}")
    print(f"{C.DIM}  (no Enter needed for most keys){C.RESET}\n")

    paused = False

    with RawInput() as raw:
        if not raw.is_raw:
            control_loop_line(mpv)
            return

        buf = ""
        while mpv.is_alive():
            ch = raw.read_key(timeout=0.15)
            if ch is None:
                continue

            # Volume-set mode: "v" → digits → Enter
            if buf.startswith("v"):
                if ch in ("\r", "\n"):
                    try:
                        vol = int(buf[1:].strip())
                        mpv.set_volume(vol)
                        if DEBUG:
                            print(f"\r{C.GREEN}✔  Volume → {vol}   {C.RESET}")
                    except ValueError:
                        if DEBUG:
                            print(f"\r{C.RED}✘  Invalid volume   {C.RESET}")
                    buf = ""
                elif ch.isdigit() or ch == " ":
                    buf += ch
                    if DEBUG:
                        print(f"\r{C.CYAN}vol: {buf[1:]}  {C.RESET}", end="", flush=True)
                elif ch == "\x7f":
                    buf = buf[:-1]
                    if DEBUG:
                        print(f"\r{C.CYAN}vol: {buf[1:]}   {C.RESET}", end="", flush=True)
                else:
                    buf = ""
                continue

            if ch == " ":
                paused = not paused
                if paused:
                    mpv.pause()
                    if DEBUG:
                        print(f"\r{C.YELLOW}⏸  Paused          {C.RESET}")
                else:
                    mpv.resume()
                    if DEBUG:
                        print(f"\r{C.GREEN}▶  Playing         {C.RESET}")

            elif ch in ("+", "="):
                mpv.volume_up()
                if DEBUG:
                    print(f"\r{C.GREEN}🔊 Volume +5       {C.RESET}")

            elif ch in ("-", "_"):
                mpv.volume_down()
                if DEBUG:
                    print(f"\r{C.GREEN}🔉 Volume -5       {C.RESET}")

            elif ch == "v":
                buf = "v"
                if DEBUG:
                    print(f"\r{C.CYAN}vol: {C.RESET}", end="", flush=True)

            elif ch in ("q", "Q", "\x03"):
                mpv.quit()
                if DEBUG:
                    print(f"\r{C.RED}⏹  Stopped         {C.RESET}")
                break

            elif ch == "?":
                print(f"\n{C.DIM}{CONTROLS_HELP}{C.RESET}")


def control_loop_line(mpv: MPVController):
    """Fallback line-mode controls when raw tty is unavailable."""
    info("Controls (line mode — press Enter after each): "
         "p=pause  r=resume  +=vol+  -=vol-  v N=set volume  q=quit")
    while mpv.is_alive():
        try:
            cmd = input().strip().lower()
            if cmd == "p":   mpv.pause();   info("Paused")
            elif cmd == "r": mpv.resume();  info("Resumed")
            elif cmd == "+": mpv.volume_up();   info("Volume +5")
            elif cmd == "-": mpv.volume_down(); info("Volume -5")
            elif cmd.startswith("v "):
                try:
                    mpv.set_volume(int(cmd.split()[1]))
                    info("Volume set")
                except (ValueError, IndexError):
                    warn("Usage: v <0-130>")
            elif cmd == "q":
                mpv.quit(); info("Stopped"); break
        except (EOFError, KeyboardInterrupt):
            mpv.quit()
            break


# ─────────────────────────────────────────────
#  PLAYBACK CORE (SAFE FIXED VERSION)
# ─────────────────────────────────────────────

def now_playing_display(title, mode_str):
    bar = "─" * 50
    print(f"\n{C.MAGENTA}{bar}{C.RESET}")
    print(f"{C.BOLD}{C.WHITE}  🎵  {title[:45]}{C.RESET}")
    print(f"{C.DIM}  Mode: {mode_str}{C.RESET}")
    print(f"{C.MAGENTA}{bar}{C.RESET}")


def play_file(file_path, loop_mode, loop_value, cfg, title=""):
    # Fix: Handle URLs gracefully (don't use Path() on them)
    is_url = str(file_path).startswith(('http://', 'https://'))
    if not title:
        if is_url:
            title = "Streaming Audio"
        else:
            title = Path(str(file_path)).stem

    mpv_cmd = [
        "mpv",
        "--no-video",

        # FIX #1: clean output (prevents ugly terminal spam)
        "--no-terminal",
        "--quiet",
        "--msg-level=all=no",

        # IPC (unchanged logic, safer path usage)
        f"--input-ipc-server={IPC_SOCKET}",

        f"--volume={cfg.get('volume', 100)}",
    ]

    if loop_mode == "count":
        mpv_cmd.append(f"--loop-file={loop_value}")
        mode_str = f"Loop × {loop_value}"
    elif loop_mode == "time":
        mpv_cmd.append("--loop-file=inf")
        mode_str = f"Loop for {loop_value} min"
    elif loop_mode == "inf":
        mpv_cmd.append("--loop-file=inf")
        mode_str = "Loop ∞"
    else:
        mode_str = "Play once"

    mpv_cmd.append(str(file_path))

    if cfg.get("wake_lock"):
        acquire_wake_lock()

    now_playing_display(title, mode_str)
    send_notification("▶ Now Playing", title)

    try:
        # FIX #2: safer socket removal (avoid race crash)
        try:
            os.remove(IPC_SOCKET)
        except FileNotFoundError:
            pass

        proc = subprocess.Popen(mpv_cmd)

        # FIX #3: safer IPC wait (also detects mpv crash)
        for _ in range(50):  # up to ~5 seconds
            if proc.poll() is not None:
                break
            if os.path.exists(IPC_SOCKET):
                break
            time.sleep(0.1)

        if not os.path.exists(IPC_SOCKET):
            warn("IPC socket did not appear — controls unavailable")

        mpv = MPVController()

        ctrl_thread = threading.Thread(
            target=control_loop_raw, args=(mpv,), daemon=True
        )
        ctrl_thread.start()

        if loop_mode == "time":
            timeout = loop_value * 60
            start = time.time()
            while proc.poll() is None:
                if time.time() - start >= timeout:
                    mpv.quit()
                    ok(f"Time limit reached ({loop_value} min)")
                    break
                time.sleep(0.5)
        else:
            proc.wait()

    except KeyboardInterrupt:
        try:
            mpv.quit()
        except Exception:
            pass
        proc.terminate()
        print()
        warn("Interrupted")

    finally:
        if cfg.get("wake_lock"):
            release_wake_lock()

        # FIX #4: safe cleanup
        try:
            if os.path.exists(IPC_SOCKET):
                os.remove(IPC_SOCKET)
        except Exception:
            pass

#fixed play queue function
def play_queue(cfg, queue_name=None, auto_clear=False):
    db = load_db()
    qname = queue_name or current(db)
    q = _q(db, qname)

    if not q:
        warn("Queue empty")
        return

    st = db["state"]
    st["playing"] = True
    save_db(db)

    head(f"Playing [{qname}] mode={st['mode']}")

    current_idx = st.get("index", 0)
    if current_idx >= len(q):
        current_idx = 0
        st["index"] = 0
        save_db(db)
    while current_idx < len(q) and st["playing"]:
        item = q[current_idx]
        path = item["path"]
        title = item["title"]
        if not path.startswith(('http://', 'https://')) and not Path(path).exists():
            warn(f"Missing file: {title}")
            if input(f"{C.CYAN}Remove from queue? [y/N]: {C.RESET}").lower() == 'y':
                q.pop(current_idx)
                save_db(db)
                continue
            else:
                current_idx += 1
                continue
        info(f"[{current_idx+1}/{len(q)}] {title}")
        st["index"] = current_idx
        save_db(db)
        play_file(path, "once", None, cfg, title)
        db = load_db()
        st = db["state"]
        q = _q(db, qname)
        if not st["playing"]:
            warn("Playback stopped by user")
            break
        db["history"].insert(0, {
            "title": title,
            "path": str(path),
            "source": "queue",
            "date": datetime.now().strftime("%Y-%m-%d %H:%M")
        })
        db["history"] = db["history"][:50]
        save_db(db)
        mode = st["mode"]

        if mode == "shuffle":
            remaining_indices = [i for i in range(len(q)) if i != current_idx]
            if remaining_indices:
                current_idx = random.choice(remaining_indices)
            else:
                current_idx = len(q)
        elif mode == "repeat_all":
            if current_idx + 1 >= len(q):
                current_idx = 0
                info("Repeating queue from start")
            else:
                current_idx += 1

        else:
            current_idx += 1
        st["index"] = current_idx
        save_db(db)

        if current_idx < len(q):
            time.sleep(0.5)
    st["playing"] = False

    if current_idx >= len(q):
        st["index"] = 0
        ok("Queue playback completed")
    else:
        warn("Playback stopped before completing queue")

    save_db(db)

    if auto_clear and current_idx >= len(q):
        db["queues"][qname] = []
        save_db(db)
        ok(f"Queue '{qname}' cleared")

def stop():
    db = load_db()
    db["state"]["playing"] = False
    db["state"]["index"] = 0
    save_db(db)
    ok("Stopped")

    try:
        if os.path.exists(IPC_SOCKET):
            mpv = MPVController()
            mpv.quit()
    except Exception:
        pass

def skip():
    db = load_db()
    db["state"]["index"] += 1
    save_db(db)
    ok("Skipped")


def pause():
    db = load_db()
    db["state"]["playing"] = False
    save_db(db)
    ok("Paused")

def set_queue(db, name):
    db["queues"].setdefault(name, [])
    db["state"]["current_queue"] = name

def clear_queue(queue_name=None):
    db = load_db()
    qname = queue_name or current(db)
    if qname not in db["queues"]:
        err(f"Queue '{qname}' does not exist")
        return
    db["queues"][qname] = []
    save_db(db)
    ok(f"Queue '{qname}' cleared")

# ─────────────────────────────────────────────
#  PLAYLIST SUPPORT  [New Feature]
# ─────────────────────────────────────────────

def parse_playlist(playlist_path):
    """
    Parse .m3u, .m3u8, .pls playlist files.
    Returns list of (path, title) tuples.
    """
    playlist_path = Path(playlist_path)
    if not playlist_path.exists():
        err(f"Playlist not found: {playlist_path}")
        return []
    entries = []
    ext = playlist_path.suffix.lower()
    try:
        with open(playlist_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        if ext in ['.m3u', '.m3u8']:
            # Standard m3u - each line is a file path or URL
            for line in lines:
                if line and not line.startswith('#'):
                    path = os.path.expanduser(line)
                    title = Path(path).stem if not path.startswith(('http://', 'https://')) else "Stream"
                    entries.append((path, title))
        elif ext == '.pls':
            # PLS format: File1=/path/to/song.mp3, Title1=Song Name
            file_dict = {}
            title_dict = {}
            for line in lines:
                if '=' in line:
                    key, value = line.split('=', 1)
                    if key.startswith('File'):
                        idx = key[4:] if key[4:].isdigit() else '1'
                        file_dict[idx] = value
                    elif key.startswith('Title'):
                        idx = key[5:] if key[5:].isdigit() else '1'
                        title_dict[idx] = value
            for idx in sorted(file_dict.keys()):
                path = file_dict[idx]
                title = title_dict.get(idx, Path(path).stem)
                entries.append((path, title))
        else:
            err(f"Unsupported playlist format: {ext}")
            return []
    except Exception as e:
        err(f"Error parsing playlist: {e}")
        return []
    ok(f"Loaded {len(entries)} tracks from playlist")
    return entries

def load_playlist_to_queue(playlist_path, queue_name=None):
    """Load playlist entries into specified queue."""
    entries = parse_playlist(playlist_path)
    if not entries:
        return False
    db = load_db()
    qname = queue_name or current(db)
    q = _q(db, qname)

    added = 0
    for path, title in entries:
        if os.path.exists(path) or path.startswith(('http://', 'https://')):
            if not any(x["path"] == path for x in q):
                q.append({"path": path, "title": title})
                added += 1

    save_db(db)
    ok(f"Added {added}/{len(entries)} tracks to queue '{qname}'")
    return added > 0

# ─────────────────────────────────────────────
#  DAEMON MODE  [Feature #8]
# ─────────────────────────────────────────────

def start_daemon(file_path, cfg, title=""):
    """Launch mpv fully detached — survives terminal close."""
    title = title or Path(str(file_path)).stem
    daemon_sock = str(DAEMON_SOCK)

    if os.path.exists(daemon_sock):
        os.remove(daemon_sock)

    mpv_cmd = [
        "mpv", "--no-video", "--quiet",
        "--loop-file=inf",
        f"--input-ipc-server={daemon_sock}",
        f"--volume={cfg.get('volume', 100)}",
        str(file_path)
    ]

    proc = subprocess.Popen(
        mpv_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True      # detach from terminal
    )

    with open(DAEMON_PID, "w") as f:
        json.dump({"pid": proc.pid, "title": title, "socket": daemon_sock}, f)

    ok(f"Daemon started: {title} (PID {proc.pid})")
    send_notification("▶ Daemon Playing", title)
    info("Control with: musicloop.py --daemon-ctl [pause|resume|stop|status|vol]")


def daemon_ctl(action, value=None):
    """Control the running daemon instance."""
    if not DAEMON_PID.exists():
        err("No daemon running.")
        return

    with open(DAEMON_PID) as f:
        data = json.load(f)

    sock_path = data.get("socket", str(DAEMON_SOCK))
    mpv = MPVController(socket_path=sock_path)

    if not mpv.is_alive():
        err("Daemon socket not found — daemon may have stopped.")
        DAEMON_PID.unlink(missing_ok=True)
        return

    if action == "pause":
        mpv.pause(); ok("Daemon paused")
    elif action == "resume":
        mpv.resume(); ok("Daemon resumed")
    elif action == "stop":
        mpv.quit(); ok("Daemon stopped")
        DAEMON_PID.unlink(missing_ok=True)
        if os.path.exists(sock_path):
            os.remove(sock_path)
    elif action == "vol" and value is not None:
        try:
            mpv.set_volume(int(value)); ok(f"Daemon volume → {value}")
        except ValueError:
            err("Volume must be an integer.")
    elif action == "status":
        head("Daemon Status")
        print(f"  PID   : {data.get('pid')}")
        print(f"  Title : {data.get('title')}")
        print(f"  Alive : {mpv.is_alive()}")
    else:
        err(f"Unknown daemon action: {action}")


# ─────────────────────────────────────────────
#  INPUT HELPERS
# ─────────────────────────────────────────────

def choose(prompt, options):
    print()
    for key, label in options:
        print(f"  {C.CYAN}{key}{C.RESET}. {label}")
    while True:
        val = input(f"\n{C.WHITE}{prompt}: {C.RESET}").strip()
        for key, _ in options:
            if val == key:
                return val
        err("Invalid choice.")

def get_int(prompt):
    while True:
        try:
            v = int(input(f"{C.WHITE}{prompt}: {C.RESET}"))
            if v > 0: return v
            err("Must be > 0")
        except ValueError:
            err("Enter a number.")

def get_float(prompt):
    while True:
        try:
            v = float(input(f"{C.WHITE}{prompt}: {C.RESET}"))
            if v > 0: return v
            err("Must be > 0")
        except ValueError:
            err("Enter a number.")

def get_file_path(prompt):
    while True:
        p = input(f"{C.WHITE}{prompt}: {C.RESET}").strip()
        path_of_file = f'/data/data/com.termux/files/home/storage/downloads/songs/{p}'
        p = os.path.expanduser(p)
        if os.path.exists(path_of_file): return path_of_file
        err("File not found.")

def get_loop_mode():
    return choose("Loop mode", [
        ("1", "Loop by count"),
        ("2", "Loop by time (minutes)"),
        ("3", "Loop infinitely"),
        ("4", "Play once"),
    ])

def get_loop_params(mode):
    if mode == "1":   return "count", get_int("Repeat how many times")
    elif mode == "2": return "time",  get_float("Play for how many minutes")
    elif mode == "3": return "inf",   None
    else:             return "once",  None


# ─────────────────────────────────────────────
#  SETTINGS MENU  [Fix #5: always returns cfg]
# ─────────────────────────────────────────────

def settings_menu(cfg):
    head("Settings")
    print(f"  {C.DIM}Download dir : {cfg['download_dir']}{C.RESET}")
    print(f"  {C.DIM}Audio format : {cfg['audio_format']}{C.RESET}")
    print(f"  {C.DIM}Volume       : {cfg['volume']}{C.RESET}")
    print(f"  {C.DIM}Notify       : {cfg['notify']}{C.RESET}")
    print(f"  {C.DIM}Wake lock    : {cfg['wake_lock']}{C.RESET}")
    print(f"  {C.DIM}Retries      : {cfg['retry_count']}{C.RESET}")

    what = choose("Edit", [
        ("1", "Download directory"),
        ("2", "Audio format"),
        ("3", "Default volume"),
        ("4", "Toggle notifications"),
        ("5", "Toggle wake lock"),
        ("6", "Retry count"),
        ("7", "Back"),
    ])

    if what == "1":
        d = input(f"{C.WHITE}New dir: {C.RESET}").strip()
        cfg["download_dir"] = os.path.expanduser(d)
    elif what == "2":
        fmt = input(f"{C.WHITE}Format (mp3/opus/m4a/flac): {C.RESET}").strip()
        if fmt in ("mp3", "opus", "m4a", "flac"):
            cfg["audio_format"] = fmt
        else:
            warn("Unsupported format, keeping current.")
    elif what == "3":
        cfg["volume"] = get_int("Volume (0-130)")
    elif what == "4":
        cfg["notify"] = not cfg["notify"]
        ok(f"Notifications {'on' if cfg['notify'] else 'off'}")
    elif what == "5":
        cfg["wake_lock"] = not cfg["wake_lock"]
        ok(f"Wake lock {'on' if cfg['wake_lock'] else 'off'}")
    elif what == "6":
        cfg["retry_count"] = get_int("Retries (1-5)")

    save_config(cfg)
    ok("Settings saved.")
    return cfg   # [Fix #5]


# ─────────────────────────────────────────────
#  ARGPARSE
# ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(prog="musicloop", description="🎧 MusicLoop v2.1")

    src = p.add_mutually_exclusive_group()
    src.add_argument("-s", "--search", metavar="QUERY", help="Search & download by name")
    src.add_argument("-u", "--url",    metavar="URL",   help="Download from URL")
    src.add_argument("-f", "--file",   metavar="PATH",  help="Play local file")
    src.add_argument("--stream",       metavar="QUERY", help="Stream (no download)")

    lp = p.add_mutually_exclusive_group()
    lp.add_argument("-n", "--count", metavar="N",   type=int,   help="Loop N times")
    lp.add_argument("-t", "--time",  metavar="MIN", type=float, help="Loop N minutes")
    lp.add_argument("-i", "--inf",   action="store_true",       help="Loop infinitely")

    p.add_argument("--queue-add",    metavar="PATH")
    p.add_argument("--queue-play",   action="store_true")
    p.add_argument("--queue-show",   action="store_true")
    p.add_argument("--queue-clear",  action="store_true")
    p.add_argument("--queue-remove", metavar="N", type=int)
    p.add_argument("--queue-up",     metavar="N", type=int)
    p.add_argument("--queue-down",   metavar="N", type=int)

    p.add_argument("--daemon",      action="store_true", help="Play in background")
    p.add_argument("--daemon-ctl",  metavar="ACTION",    help="pause|resume|stop|status|vol")
    p.add_argument("--daemon-val",  metavar="VALUE",     help="Value for vol")
    p.add_argument("--load-playlist", metavar="FILE", help="Load .m3u/.pls playlist to queue")

    p.add_argument("--history", action="store_true")
    return p.parse_args()


def resolve_loop(args):
    if args.count: return "count", args.count
    if args.time:  return "time",  args.time
    if args.inf:   return "inf",   None
    return None, None


def handle_args(args, cfg):
    if args.queue_show:    show_queue();                        return True
    if args.queue_clear:   clear_queue();                       return True
    if args.queue_play:    play_queue(cfg);                     return True
    if args.queue_remove:  remove_from_queue(args.queue_remove);return True
    if args.queue_up:      move_queue_item(args.queue_up, -1);  return True
    if args.queue_down:    move_queue_item(args.queue_down, 1); return True
    if args.queue_add:
        p = os.path.expanduser(args.queue_add)
        if os.path.exists(p): add_to_queue(p, Path(p).stem)
        else: err("File not found.")
        return True

    if args.daemon_ctl:
        daemon_ctl(args.daemon_ctl, args.daemon_val)
        return True

    if args.history:
        result = show_history()
        if result:
            if isinstance(result, dict) and result.get("action") == "queue_added":
            # Queue was populated, optionally start playing
                if input(f"{C.CYAN}Start playing queue now? [y/N]: {C.RESET}").lower() == 'y':
                    play_queue(cfg)
            elif isinstance(result, str):
                lm, lv = resolve_loop(args)
                if lm is None:
                    lm, lv = get_loop_params(get_loop_mode())
                play_file(result, lm, lv, cfg)
        return True

    if args.stream:
        lm, lv = resolve_loop(args)
        if lm is None: lm, lv = get_loop_params(get_loop_mode())
        play_stream(args.stream, lm, lv, cfg)
        return True

    file_path, title = None, ""

    if args.search:
        file_path, title = search_and_download(args.search, cfg)
    elif args.url:
        file_path, title = download_from_url(args.url, cfg)
    elif args.file:
        p = os.path.expanduser(args.file)
        if os.path.exists(p): file_path, title = p, Path(p).stem
        else: err("File not found."); return True
    else:
        return False

    if not file_path:
        return True

    if args.daemon:
        start_daemon(file_path, cfg, title)
    else:
        lm, lv = resolve_loop(args)
        if lm is None:
            lm, lv = get_loop_params(get_loop_mode())
        play_file(file_path, lm, lv, cfg, title)
    if args.load_playlist:
        if load_playlist_to_queue(args.load_playlist):
            if args.queue_play:
                play_queue(cfg)
        return True
    return True


# ─────────────────────────────────────────────
#  INTERACTIVE MENUS
# ─────────────────────────────────────────────
def queue_menu(cfg):
    head("Queue System")

    db = load_db()
    current_q = db["state"]["current_queue"]

    action = choose(
        f"Queue action (current: {current_q})",
        [
            ("1", "Show queue"),
            ("2", "Play queue"),
            ("3", "Add file to queue"),
            ("4", "Remove item by index"),
            ("5", "Move item up"),
            ("6", "Move item down"),
            ("7", "Clear current queue"),
            ("8", "Switch queue"),
            ("a", "Load playlist( .m3u/.pls)"),
            ("9", "List all queues"),
            ("0", "Back"),
        ]
    )

    # -------------------
    # BASIC OPERATIONS
    # -------------------

    if action == "1":
        show_queue()

    elif action == "2":
        play_queue(cfg)

    elif action == "3":
        p = get_file_path("File path")
        add_to_queue(p, Path(p).stem)

    elif action == "4":
        show_queue()
        remove_from_queue(get_int("Remove item #"))

    elif action == "5":
        show_queue()
        move_queue_item(get_int("Move item # up"), -1)

    elif action == "6":
        show_queue()
        move_queue_item(get_int("Move item # down"), 1)

    elif action == "7":
        clear_queue()

    # -------------------
    # MULTI-QUEUE FEATURES
    # -------------------

    elif action == "8":
        db = load_db()
        queues = list(db.get("queues", {}).keys())

        if not queues:
            warn("No queues exist")
            return

    # build indexed menu (stable input system)
        options = [(str(i), q) for i, q in enumerate(queues)]

        selected_index = choose(
            "Select queue",
            options
         )

        queue_name = queues[int(selected_index)]

        if selected_index is None or selected_index == "":
            warn("No selection made")
            return

        idx = int(selected_index)
        if idx < 0 or idx >= len(queues):
            warn("Out of range")
            return

        set_queue(db, queue_name)
        save_db(db)

        ok(f"Switched to queue: {queue_name}")


    elif action == "9":
        db = load_db()
        queues = db.get("queues", {})

        head("All Queues")
        for name, items in queues.items():
            print(f" - {name} ({len(items)})")
    elif action == "a":
        playlist_path = input(f"{C.WHITE}Playlist file path: {C.RESET}").strip()
        if not playlist_path:
            return
        playlist_path = os.path.expanduser(playlist_path)
        if load_playlist_to_queue(playlist_path):
            if input(f"{C.CYAN}Start playing queue now? [y/N]: {C.RESET}").lower() == 'y':
                play_queue(cfg)

    elif action == "0":
        return


def interactive_menu(cfg):
    while True:
        head("Main Menu")
        action = choose("Choose action", [
            ("1", "Search & download by name"),
            ("2", "Stream by name (instant, no download)"),
            ("3", "Download from URL"),
            ("4", "Play local file"),
            ("5", "Play from history"),
            ("6", "Queue management"),
            ("7", "Daemon mode (background play)"),
            ("8", "Settings"),
            ("9", "Clear screen"),
            ("10", "Exit"),
          ])

        if action == "1":
            query = input(f"\n{C.WHITE}Search query: {C.RESET}").strip()
            if not query: continue
            file_path, title = search_and_download(query, cfg)
            if not file_path: continue
            if input(f"{C.CYAN}Add to queue instead? [y/N]: {C.RESET}").strip().lower() == "y":
                add_to_queue(file_path, title)
            else:
                lm, lv = get_loop_params(get_loop_mode())
                play_file(file_path, lm, lv, cfg, title)

        elif action == "2":
            query = input(f"\n{C.WHITE}Search query: {C.RESET}").strip()
            lm, lv = get_loop_params(get_loop_mode())
            if not query:
                err('no input given')
                continue

            # Get search results once
            entries = search_results(query)
            if not entries:
                err("No results found")
                continue

            # Display results
            head(f"Results for: {query}")
            for i, (title, _) in enumerate(entries[:6], 1):
                print(f"  {C.CYAN}{i}.{C.RESET} {title[:70]}")
            print(f"  {C.CYAN}0.{C.RESET} Cancel")

            # Get user choice
            choice = input(f"\n{C.WHITE}Pick number (1-{len(entries)}): {C.RESET}").strip()
            if not choice.isdigit() or int(choice) == 0:
                continue

            selected_idx = int(choice) - 1
            if selected_idx >= len(entries):
                err("Invalid selection")
                continue

            # Get the selected entry
            title, url_or_id = entries[selected_idx]
            info(f"Selected: {title[:50]}")

            # Get stream URL without re-searching (use the URL/ID we already have)
            if url_or_id.startswith(('http://', 'https://')):
                stream_url = url_or_id
            else:
                # Resolve to actual stream URL
                resolve_cmd = ["yt-dlp", "-f", "bestaudio", "-g", url_or_id]
                try:
                    result = subprocess.run(resolve_cmd, capture_output=True, text=True, timeout=30)
                    if result.returncode == 0:
                        stream_url = result.stdout.strip().split('\n')[0]
                    else:
                        err("Could not resolve stream URL")
                        continue
                except Exception as e:
                        err(f"Stream resolution failed: {e}")
                        continue
            play_file(stream_url, lm, lv, cfg, f"[STREAM] {title}")

        elif action == "3":
            url = input(f"\n{C.WHITE}URL: {C.RESET}").strip()
            file_path, title = download_from_url(url, cfg)
            if not file_path: continue
            lm, lv = get_loop_params(get_loop_mode())
            play_file(file_path, lm, lv, cfg, title)

        elif action == "4":
            MUSIC_DIR = Path("/data/data/com.termux/files/home/storage/downloads/songs")
            MUSIC_DIR.mkdir(parents=True, exist_ok=True)
            audio_extensions = ("*.mp3", "*.m4a", "*.opus", "*.flac", "*.wav", "*.ogg")
            music_files = []
            for ext in audio_extensions:
                music_files.extend(MUSIC_DIR.glob(ext))

                music_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            if not music_files:
                warn("No music files found in songs directory")
                print(f"{C.DIM}Download some songs first using search option{C.RESET}")
                continue
            print(f"\n{C.BOLD}{C.CYAN}——— Available Files ({len(music_files)}) ———{C.RESET}\n")
            display_limit = 30
            for idx, file in enumerate(music_files[:display_limit], 1):
                size_mb = file.stat().st_size / (1024 * 1024)
                name = file.stem[:55] + "..." if len(file.stem) > 55 else file.stem
                print(f"  {C.CYAN}{idx:3}.{C.RESET} {name:<58} {C.DIM}({size_mb:.1f} MB){C.RESET}")
            if len(music_files) > display_limit:
                print(f"\n  {C.DIM}... and {len(music_files) - display_limit} more files{C.RESET}")

            while True:
                try:
                    choice = input(f"\n{C.WHITE}Enter number (or 0 to cancel): {C.RESET}").strip()
                    if choice == "0":
                        continue
                    if choice.isdigit():
                        idx = int(choice) - 1
                        if 0 <= idx < len(music_files):
                            fp = str(music_files[idx])
                            title = music_files[idx].stem
                            ok(f"Selected: {title}")
                            break
                        else:
                            err(f"Invalid number. Choose 1-{len(music_files)}")
                    else:
                        err("Please enter a valid number")
                except (ValueError, IndexError):
                    err("Invalid selection")
                    continue
            lm, lv = get_loop_params(get_loop_mode())
            play_file(fp, lm, lv, cfg, title)

        elif action == "5":
            result = show_history()
            if result:
                if isinstance(result, dict) and result.get("action") == "queue_added":
                    if input(f"{C.CYAN}Start playing queue now? [y/N]: {C.RESET}").lower() == 'y':
                        play_queue(cfg)
                elif isinstance(result, str):
                    lm, lv = get_loop_params(get_loop_mode())
                    play_file(result, lm, lv, cfg, Path(result).stem)

        elif action == "6":
            queue_menu(cfg)

        elif action == "7":
            head("Daemon Mode")
            src = choose("Source", [("1", "Local file"), ("2", "Search & download")])
            if src == "1":
                fp = get_file_path("File path")
                start_daemon(fp, cfg, Path(fp).stem)
            else:
                q = input(f"{C.WHITE}Search query: {C.RESET}").strip()
                fp, t = search_and_download(q, cfg)
                if fp: start_daemon(fp, cfg, t)

        elif action == "8":
            cfg = settings_menu(cfg)   # [Fix #5] capture returned cfg

        elif action == "9":
            clear_screen()
            banner()

        elif action == "10":
            print(f"\n{C.CYAN}Ya Ali a.s madad 🎵{C.RESET}\n")
            sys.exit(0)


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

def main():
    banner()
    check_dependencies()
    check_storage_permission()

    cfg  = load_config()
    args = parse_args()

    if any(vars(args).values()):
        handled = handle_args(args, cfg)
        if handled:
            return

    try:
        interactive_menu(cfg)
    except KeyboardInterrupt:
        print(f"\n{C.CYAN}Ya Ali a.s madad 🎵{C.RESET}\n")
        sys.exit(0)


if __name__ == "__main__":
    main()





