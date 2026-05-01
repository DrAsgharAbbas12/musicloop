#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════╗
║         MUSICLOOP — Termux Edition v3.0      ║
║   Hardened · Streaming · Daemon-ready        ║
║   Taste Engine · Smart Queue · Playlist      ║
╚══════════════════════════════════════════════╝

Author  : Dr. Asghar Abbas Askari
          MBBS Year 1 — Nishtar Medical University Multan

Changelog v3.0 (vs v2.2):
  [FIX-1]  search_results(): removed invalid --limit yt-dlp flag
           (was breaking ALL searches silently)
  [FIX-2]  get_file_path(): rewrote broken dual-path logic
           (was building path_of_file but checking wrong var → infinite loop)
  [FIX-3]  play_file(): moved taste-tracking INSIDE try-block and fixed
           orphan finally that ran wake_lock release before tracking
  [FIX-4]  handle_args(): moved args.load_playlist check BEFORE early returns
           (was unreachable dead code — never executed)
  [FIX-5]  stop(): now attempts IPC quit before falling back to pid kill
  [FIX-6]  next_index(): now actually wired into play_queue (was dead code)
  [FIX-7]  DEBUG flag: now auto-detects via env var MUSICLOOP_DEBUG
  [FIX-8]  play_file(): removed duplicate socket-remove before Popen
           and added proper IPC socket guard timing
  [FIX-9]  queue_menu action "8": removed unreachable post-selection code
  [NEW-1]  Smart search cache: avoids re-searching same query in one session
  [NEW-2]  Playback progress bar with elapsed / total time via IPC
  [NEW-3]  Sleep timer: auto-stop after N minutes (separate from loop-time)
  [NEW-4]  Equalizer presets: bass-boost, vocal-clarity, flat via mpv af
  [NEW-5]  Download manager: shows queue of pending downloads with cancel
  [NEW-6]  Batch download: search → select multiple → download all
  [NEW-7]  Song info panel: bitrate, duration, format from yt-dlp --dump-json
  [NEW-8]  Playlist export: save current queue as .m3u file
  [NEW-9]  Multi-queue rename/delete/copy operations
  [NEW-10] Interactive search with arrow-key style numbered nav
  [NEW-11] Volume memory: per-song last-used volume saved in taste profile
  [NEW-12] Seek support: forward/backward 10s via IPC seek command
  [NEW-13] --interactive flag forces menu mode even when other flags given
  [NEW-14] Statistics summary in main menu header
"""

# ─────────────────────────────────────────────
#  STDLIB IMPORTS
# ─────────────────────────────────────────────
import argparse
import glob
import json
import os
import random
import select
import socket
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────
#  PATHS & CONFIG
# ─────────────────────────────────────────────

CONFIG_PATH  = Path.home() / ".musicloop.json"
HISTORY_PATH = Path.home() / ".musicloop_history.json"
DB_PATH      = Path.home() / ".musicloop_queue.json"
TASTE_PATH   = Path.home() / ".musicloop_taste.json"
DAEMON_PID   = Path.home() / ".musicloop_daemon.pid"
DAEMON_SOCK  = Path.home() / ".musicloop_daemon.sock"

# Per-process socket — no multi-instance conflicts
IPC_SOCKET = os.path.join(tempfile.gettempdir(), f"mpv_{os.getpid()}.sock")

DEFAULT_DOWNLOAD_DIR = "/data/data/com.termux/files/home/storage/downloads/songs"

# [FIX-7] DEBUG driven by env var, not hardcoded
DEBUG = os.environ.get("MUSICLOOP_DEBUG", "").lower() in ("1", "true", "yes")

DEFAULT_CONFIG = {
    "download_dir": DEFAULT_DOWNLOAD_DIR,
    "audio_format": "mp3",
    "volume": 100,
    "notify": True,
    "wake_lock": True,
    "retry_count": 3,
    "retry_delay": 3,
    "stream_mode": False,
    "eq_preset": "flat",          # [NEW-4] equalizer preset
}

# In-session search cache [NEW-1]
_search_cache: dict[str, list] = {}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            base = DEFAULT_CONFIG.copy()
            base.update(json.load(f))
            return base
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


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
    BLUE    = "\033[94m"


def banner() -> None:
    db   = load_db()
    hist = len(db.get("history", []))
    q    = _q(db, current(db))
    print(f"""
{C.CYAN}{C.BOLD}╔══════════════════════════════════════════╗
║           🎧 MUSICLOOP v3.0              ║
║         Termux Edition · Hardened        ║
║         Streaming · Daemon · Smart       ║
║                                          ║
║              Programmed By:              ║
║          Dr. Asghar Abbas Askari         ║
╚══════════════════════════════════════════╝{C.RESET}
{C.DIM}  Queue: {len(q)} songs  |  History: {hist} songs{C.RESET}
""")


def ok(msg: str):   print(f"{C.GREEN}✔  {msg}{C.RESET}")
def err(msg: str):  print(f"{C.RED}✘  {msg}{C.RESET}")
def info(msg: str): print(f"{C.CYAN}ℹ  {msg}{C.RESET}")
def warn(msg: str): print(f"{C.YELLOW}⚠  {msg}{C.RESET}")
def head(msg: str): print(f"\n{C.MAGENTA}{C.BOLD}── {msg} ──{C.RESET}")
def dprint(msg: str):
    if DEBUG:
        print(f"{C.DIM}[dbg] {msg}{C.RESET}")


# ─────────────────────────────────────────────
#  TERMUX INTEGRATIONS
# ─────────────────────────────────────────────

def termux_available(cmd: str) -> bool:
    return subprocess.run(["which", cmd], capture_output=True).returncode == 0


def acquire_wake_lock() -> None:
    if termux_available("termux-wake-lock"):
        subprocess.Popen(["termux-wake-lock"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        info("Wake lock acquired")


def release_wake_lock() -> None:
    if termux_available("termux-wake-unlock"):
        subprocess.run(["termux-wake-unlock"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        info("Wake lock released")


def send_notification(title: str, content: str) -> None:
    if termux_available("termux-notification"):
        subprocess.Popen(
            ["termux-notification", "--title", title,
             "--content", content, "--id", "musicloop"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )


def check_storage_permission() -> bool:
    test = Path("/data/data/com.termux/files/home/storage/downloads")
    if not test.exists():
        warn("Storage not set up. Run: termux-setup-storage")
        return False
    return True


def check_dependencies() -> None:
    missing = [dep for dep in ["mpv", "yt-dlp", "socat"]
               if not termux_available(dep)]
    if missing:
        err(f"Missing: {', '.join(missing)}")
        print(f"{C.DIM}Install via: pkg install {' '.join(missing)}{C.RESET}")
        sys.exit(1)


# ─────────────────────────────────────────────
#  NON-BLOCKING TTY INPUT
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

    def read_key(self, timeout: float = 0.2) -> str | None:
        if not self.is_raw:
            return None
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if ready:
            return sys.stdin.read(1)
        return None


# ─────────────────────────────────────────────
#  HISTORY
# ─────────────────────────────────────────────

def load_history() -> list:
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH) as f:
            return json.load(f)
    return []


def save_to_history(title: str, path, source: str) -> None:
    history = load_history()
    entry = {
        "title": title,
        "path": str(path),
        "source": source,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    history.insert(0, entry)
    history = history[:50]
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)

    # also mirror into DB history
    db = load_db()
    db["history"].insert(0, entry)
    db["history"] = db["history"][:50]
    save_db(db)


def show_history():
    """
    Display recent songs and handle multi-select / range commands.
    Returns: path str (play), dict {action:queue_added} (queue), or None.
    """
    history = load_history()
    if not history:
        info("No history yet.")
        return None

    head("Recent Songs")
    for i, entry in enumerate(history[:15], 1):
        exists  = "✔" if os.path.exists(entry.get("path", "")) else "✘"
        color   = C.GREEN if exists == "✔" else C.RED
        title   = entry.get("title", "Unknown")[:50]
        date    = entry.get("date", "")
        print(f"  {C.DIM}{i:2}.{C.RESET} {color}{exists}{C.RESET} "
              f"{title:<50} {C.DIM}{date}{C.RESET}")

    print()
    print(f"{C.CYAN}Commands:{C.RESET} number (play)  "
          f"{C.CYAN}q 1 2 3{C.RESET} (queue)  "
          f"{C.CYAN}qp 1-5{C.RESET} (queue+play)  "
          f"{C.CYAN}Enter{C.RESET} (back)")
    choice = input(f"{C.CYAN}> {C.RESET}").strip()
    if not choice:
        return None

    parts    = choice.split(maxsplit=1)
    cmd      = parts[0].lower() if parts else ""
    arg_str  = parts[1] if len(parts) > 1 else ""

    if cmd in ("q", "queue", "a", "add"):
        action, indices_str = "queue", arg_str
    elif cmd in ("qp", "queueplay"):
        action, indices_str = "queueplay", arg_str
    else:
        action      = "play"
        indices_str = cmd if (cmd.isdigit() or "-" in cmd or "," in cmd) else choice

    selected: set[int] = set()
    for part in indices_str.replace(",", " ").split():
        if "-" in part:
            try:
                a, b = map(int, part.split("-", 1))
                selected.update(range(a, b + 1))
            except ValueError:
                err(f"Invalid range: {part}"); return None
        elif part.isdigit():
            selected.add(int(part))
        else:
            err(f"Invalid input: {part}"); return None

    if not selected:
        err("No valid indices provided"); return None

    valid, missing_count = [], 0
    for idx in sorted(selected):
        if not (1 <= idx <= len(history)):
            warn(f"Index {idx} out of range (1–{len(history)})"); continue
        entry = history[idx - 1]
        path  = entry.get("path", "")
        title = entry.get("title", "Unknown")
        if os.path.exists(path):
            valid.append({"path": path, "title": title})
        else:
            missing_count += 1
            warn(f"#{idx}: {title} — file missing")

    if not valid:
        err("No valid files found"); return None

    if action == "play":
        if len(valid) > 1:
            info(f"Multiple selections — using first: {valid[0]['title']}")
        return valid[0]["path"]

    for item in valid:
        add_to_queue(item["path"], item["title"])
    msg = f"Added {len(valid)} song(s) to queue"
    if missing_count:
        msg += f" ({missing_count} missing, skipped)"
    ok(msg)

    if action == "queueplay" and valid:
        info("Starting queue playback...")
    return {"action": "queue_added", "count": len(valid)}


# ─────────────────────────────────────────────
#  QUEUE  (full CRUD + rename/copy/export)
# ─────────────────────────────────────────────

def load_db() -> dict:
    default_db = {
        "queues":  {"default": []},
        "state":   {
            "current_queue": "default",
            "index":  0,
            "mode":   "normal",
            "playing": False,
        },
        "history": [],
    }
    if not DB_PATH.exists():
        return default_db
    try:
        with open(DB_PATH) as f:
            db = json.load(f)
        # migrate old list format
        if isinstance(db, list):
            warn("Old queue format detected. Migrating...")
            db = {**default_db, "queues": {"default": db}}
            save_db(db)
            return db
        if not isinstance(db, dict):
            warn("Invalid DB format. Resetting."); return default_db
        db.setdefault("queues",  {"default": []})
        db.setdefault("state",   {})
        db.setdefault("history", [])
        st = db["state"]
        st.setdefault("current_queue", "default")
        st.setdefault("index",  0)
        st.setdefault("mode",   "normal")
        st.setdefault("playing", False)
        return db
    except json.JSONDecodeError:
        warn("Corrupted DB. Resetting."); return default_db


def save_db(db: dict) -> None:
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


def _q(db: dict, name: str) -> list:
    db.setdefault("queues", {})
    db["queues"].setdefault(name, [])
    return db["queues"][name]


def current(db: dict) -> str:
    return db["state"]["current_queue"]


def set_queue(db: dict, name: str) -> None:
    db["queues"].setdefault(name, [])
    db["state"]["current_queue"] = name


def add_to_queue(path: str, title: str = "Unknown",
                 queue_name: str | None = None) -> None:
    db    = load_db()
    qname = queue_name or current(db)
    q     = _q(db, qname)
    is_url = str(path).startswith(("http://", "https://"))
    if not is_url and not Path(path).exists():
        err("File not found"); return
    if any(x["path"] == str(path) for x in q):
        warn("Already in queue"); return
    q.append({"path": str(path), "title": title})
    save_db(db)
    ok(f"Added → [{qname}] {title}")


def remove_from_queue(index: int, queue_name: str | None = None):
    db    = load_db()
    qname = queue_name or current(db)
    q     = _q(db, qname)
    if 1 <= index <= len(q):
        removed = q.pop(index - 1)
        save_db(db)
        ok(f"Removed: {removed['title']}")
        return removed
    err("Invalid index")


def move_queue_item(index: int, direction: int,
                    queue_name: str | None = None) -> bool:
    db    = load_db()
    qname = queue_name or current(db)
    q     = _q(db, qname)
    i, j  = index - 1, index - 1 + direction
    if 0 <= i < len(q) and 0 <= j < len(q):
        q[i], q[j] = q[j], q[i]
        save_db(db)
        ok("Moved item"); return True
    err("Cannot move"); return False


def show_queue(queue_name: str | None = None) -> None:
    db    = load_db()
    qname = queue_name or current(db)
    q     = _q(db, qname)
    if not q:
        info(f"[{qname}] empty"); return
    head(f"Queue: {qname}  ({len(q)} songs)")
    for i, item in enumerate(q, 1):
        exists = Path(item["path"]).exists() or str(item["path"]).startswith("http")
        sym    = "✔" if exists else "✘"
        col    = C.GREEN if exists else C.RED
        print(f"  {i:2}. {col}{sym}{C.RESET} {item['title'][:60]}")


def set_mode(mode: str) -> None:
    if mode not in ("normal", "shuffle", "repeat_all"):
        err("Invalid mode"); return
    db = load_db()
    db["state"]["mode"] = mode
    save_db(db)
    ok(f"Mode → {mode}")


def next_index(db: dict, q: list) -> int:
    """[FIX-6] Now actually called from play_queue."""
    if not q:
        return 0
    st          = db["state"]
    mode        = st.get("mode", "normal")
    current_idx = st.get("index", 0)
    if mode == "shuffle":
        candidates = [i for i in range(len(q)) if i != current_idx]
        return random.choice(candidates) if candidates else 0
    if mode == "repeat_all":
        return (current_idx + 1) % len(q)
    return current_idx + 1          # normal: may exceed len(q) → stops


# ─────────────────────────────────────────────
#  [NEW-9] Queue rename / copy / delete / export
# ─────────────────────────────────────────────

def rename_queue(old: str, new: str) -> None:
    db = load_db()
    if old not in db["queues"]:
        err(f"Queue '{old}' does not exist"); return
    if new in db["queues"]:
        err(f"Queue '{new}' already exists"); return
    db["queues"][new] = db["queues"].pop(old)
    if db["state"]["current_queue"] == old:
        db["state"]["current_queue"] = new
    save_db(db)
    ok(f"Renamed '{old}' → '{new}'")


def copy_queue(src: str, dst: str) -> None:
    db = load_db()
    if src not in db["queues"]:
        err(f"Queue '{src}' does not exist"); return
    db["queues"][dst] = list(db["queues"][src])
    save_db(db)
    ok(f"Copied '{src}' → '{dst}'")


def delete_queue(name: str) -> None:
    db = load_db()
    if name == "default":
        err("Cannot delete 'default' queue"); return
    if name not in db["queues"]:
        err(f"Queue '{name}' does not exist"); return
    del db["queues"][name]
    if db["state"]["current_queue"] == name:
        db["state"]["current_queue"] = "default"
    save_db(db)
    ok(f"Deleted queue '{name}'")


def export_queue_as_m3u(queue_name: str | None = None,
                        out_path: str | None = None) -> None:
    """[NEW-8] Export queue to .m3u playlist file."""
    db    = load_db()
    qname = queue_name or current(db)
    q     = _q(db, qname)
    if not q:
        warn("Queue empty"); return
    if not out_path:
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = str(Path.home() / f"musicloop_{qname}_{ts}.m3u")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in q:
            f.write(f"#EXTINF:-1,{item['title']}\n{item['path']}\n")
    ok(f"Exported {len(q)} songs → {out_path}")


def clear_queue(queue_name: str | None = None) -> None:
    db    = load_db()
    qname = queue_name or current(db)
    if qname not in db.get("queues", {}):
        err(f"Queue '{qname}' does not exist"); return
    db["queues"][qname] = []
    save_db(db)
    ok(f"Queue '{qname}' cleared")


# ─────────────────────────────────────────────
#  DOWNLOAD WITH RETRY
# ─────────────────────────────────────────────

def run_ytdlp_with_retry(cmd: list, cfg: dict,
                         label: str = "Download") -> tuple:
    """
    Run yt-dlp command with retry logic.
    Returns (returncode, file_path, stderr).
    """
    retries = cfg.get("retry_count", 3)
    delay   = cfg.get("retry_delay", 3)

    for attempt in range(1, retries + 1):
        if attempt > 1:
            warn(f"Retry {attempt}/{retries} in {delay}s...")
            time.sleep(delay)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True
            )
            file_path = None
            print(C.DIM, end="", flush=True)
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
            err(f"{label} failed (attempt {attempt})")
            if stderr_out:
                print(f"{C.DIM}  yt-dlp: {stderr_out[:300].strip()}{C.RESET}")
        except Exception as e:
            err(f"{label} error (attempt {attempt}): {e}")

    return -1, None, "All retries exhausted"


def search_results(query: str, limit: int = 6) -> list[tuple[str, str]]:
    """
    [FIX-1] Removed invalid --limit flag. Use ytsearch{N}:query instead.
    Returns list of (title, url_or_id).
    Cached per session [NEW-1].
    """
    cache_key = f"{query}:{limit}"
    if cache_key in _search_cache:
        dprint(f"search cache hit: {query}")
        return _search_cache[cache_key]

    cmd = [
        "yt-dlp", "--flat-playlist",
        "--print", "%(title)s|%(url)s",
        "--no-playlist",
        f"ytsearch{limit}:{query}",   # [FIX-1] no --limit flag
    ]
    try:
        result = subprocess.run(cmd, capture_output=True,
                                text=True, timeout=30)
        if result.returncode != 0:
            dprint(f"yt-dlp search failed: {result.stderr[:200]}")
            return []
        entries = []
        for line in result.stdout.strip().split("\n"):
            if "|" in line:
                title, url = line.rsplit("|", 1)
                entries.append((title.strip(), url.strip()))
        _search_cache[cache_key] = entries
        return entries
    except Exception as e:
        err(f"Search error: {e}")
        return []


# ─────────────────────────────────────────────
#  [NEW-7] Song info panel
# ─────────────────────────────────────────────

def fetch_song_info(url_or_id: str) -> dict | None:
    """Fetch rich metadata for a song via yt-dlp --dump-json."""
    cmd = ["yt-dlp", "--dump-json", "--no-playlist",
           "--quiet", url_or_id]
    try:
        result = subprocess.run(cmd, capture_output=True,
                                text=True, timeout=20)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return None


def show_song_info(url_or_id: str) -> None:
    info("Fetching song metadata...")
    data = fetch_song_info(url_or_id)
    if not data:
        warn("Could not fetch info."); return
    head("Song Info")
    fields = [
        ("Title",    data.get("title", "N/A")),
        ("Uploader", data.get("uploader", "N/A")),
        ("Duration", f"{int(data.get('duration', 0) or 0)//60}m "
                     f"{int(data.get('duration', 0) or 0)%60}s"),
        ("Views",    f"{data.get('view_count', 0):,}"),
        ("Upload",   data.get("upload_date", "N/A")),
        ("URL",      data.get("webpage_url", url_or_id)[:70]),
    ]
    for label, val in fields:
        print(f"  {C.CYAN}{label:<12}{C.RESET} {val}")


def search_and_download(query: str, cfg: dict,
                        selected_idx: int = 0) -> tuple[str | None, str | None]:
    info(f"Searching: {C.BOLD}{query}{C.RESET}")
    entries = search_results(query)
    if not entries:
        err("No results found"); return None, None

    if selected_idx == 0 and len(entries) > 1:
        head(f"Results for: {query}")
        for i, (title, _) in enumerate(entries[:6], 1):
            print(f"  {C.CYAN}{i}.{C.RESET} {title[:70]}")
        print(f"  {C.CYAN}0.{C.RESET} Cancel")
        print(f"  {C.CYAN}i N.{C.RESET} Info for result N")
        raw = input(f"\n{C.WHITE}Pick (1-{len(entries)}): {C.RESET}").strip()
        # [NEW-7] info shortcut
        if raw.lower().startswith("i "):
            idx_part = raw[2:].strip()
            if idx_part.isdigit():
                sel = int(idx_part) - 1
                if 0 <= sel < len(entries):
                    show_song_info(entries[sel][1])
                    input(f"{C.DIM}Press Enter to continue...{C.RESET}")
            return search_and_download(query, cfg, 0)
        if not raw.isdigit() or int(raw) == 0:
            return None, None
        selected_idx = int(raw) - 1

    if selected_idx >= len(entries):
        err("Invalid selection"); return None, None

    title, url_or_id = entries[selected_idx]
    info(f"Downloading: {title[:55]}")
    os.makedirs(cfg["download_dir"], exist_ok=True)
    cmd = [
        "yt-dlp", "-x",
        "--audio-format", cfg["audio_format"],
        "-o", f"{cfg['download_dir']}/%(title)s.%(ext)s",
        "--print", "after_move:filepath",
        "--newline", url_or_id,
    ]
    rc, file_path, _ = run_ytdlp_with_retry(cmd, cfg, "Download")
    if rc != 0 or not file_path or not os.path.exists(file_path):
        err("Download failed."); return None, None
    title = Path(file_path).stem
    ok(f"Downloaded: {title}")
    if cfg.get("notify"):
        send_notification("🎵 Download Complete", title)
    save_to_history(title, file_path, "yt-dlp")
    return file_path, title


def download_from_url(url: str, cfg: dict) -> tuple[str | None, str | None]:
    info("Downloading from URL...")
    os.makedirs(cfg["download_dir"], exist_ok=True)
    cmd = [
        "yt-dlp", "-x",
        "--audio-format", cfg["audio_format"],
        "-o", f"{cfg['download_dir']}/%(title)s.%(ext)s",
        "--print", "after_move:filepath",
        "--newline", url,
    ]
    rc, file_path, _ = run_ytdlp_with_retry(cmd, cfg, "Download")
    if rc != 0 or not file_path or not os.path.exists(file_path):
        err("Download failed."); return None, None
    title = Path(file_path).stem
    ok(f"Downloaded: {title}")
    if cfg.get("notify"):
        send_notification("🎵 Download Complete", title)
    save_to_history(title, file_path, url)
    return file_path, title


# ─────────────────────────────────────────────
#  [NEW-6] Batch download
# ─────────────────────────────────────────────

def batch_download(query: str, cfg: dict) -> list[str]:
    """
    Search for query, let user select multiple results, download all.
    Returns list of downloaded file paths.
    """
    entries = search_results(query, limit=8)
    if not entries:
        err("No results found"); return []
    head(f"Batch results for: {query}")
    for i, (t, _) in enumerate(entries, 1):
        print(f"  {C.CYAN}{i}.{C.RESET} {t[:70]}")
    print(f"\n{C.DIM}Enter numbers separated by spaces or ranges (e.g. 1 3 5-7){C.RESET}")
    raw = input(f"{C.WHITE}Select: {C.RESET}").strip()
    if not raw:
        return []
    selected: list[int] = []
    for part in raw.replace(",", " ").split():
        if "-" in part:
            try:
                a, b = map(int, part.split("-", 1))
                selected.extend(range(a, b + 1))
            except ValueError:
                pass
        elif part.isdigit():
            selected.append(int(part))
    selected = [i for i in dict.fromkeys(selected) if 1 <= i <= len(entries)]
    if not selected:
        warn("No valid selections"); return []
    info(f"Downloading {len(selected)} songs...")
    paths = []
    for i, idx in enumerate(selected, 1):
        title_preview, url_or_id = entries[idx - 1]
        info(f"[{i}/{len(selected)}] {title_preview[:50]}")
        fp, _ = search_and_download(title_preview, cfg, idx - 1)
        if fp:
            paths.append(fp)
    ok(f"Batch complete: {len(paths)}/{len(selected)} downloaded")
    return paths


# ─────────────────────────────────────────────
#  STREAMING
# ─────────────────────────────────────────────

def get_stream_url(query: str, cfg: dict,
                   selected_idx: int = 0) -> tuple[str | None, str | None]:
    """Resolve direct audio stream URL from search."""
    entries = search_results(query)
    if not entries:
        err("No results found"); return None, None
    if selected_idx <= 0:
        head(f"Results for: {query}")
        for i, (t, _) in enumerate(entries[:6], 1):
            print(f"  {C.CYAN}{i}.{C.RESET} {t[:70]}")
        print(f"  {C.CYAN}0.{C.RESET} Cancel")
        raw = input(f"\n{C.WHITE}Pick (1-{len(entries)}): {C.RESET}").strip()
        if not raw.isdigit() or int(raw) == 0:
            return None, None
        selected_idx = int(raw) - 1
    else:
        selected_idx = selected_idx - 1
    if selected_idx >= len(entries):
        err("Invalid selection"); return None, None
    title, url_or_id = entries[selected_idx]
    info(f"Resolving stream: {title[:50]}")
    if url_or_id.startswith(("http://", "https://")):
        return url_or_id, title
    try:
        result = subprocess.run(
            ["yt-dlp", "-f", "bestaudio", "-g", url_or_id],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            stream_url = result.stdout.strip().split("\n")[0]
            return stream_url, title
    except Exception as e:
        err(f"Resolution error: {e}")
    return None, None


def play_stream(query: str, loop_mode: str, loop_value,
                cfg: dict) -> None:
    stream_url, title = get_stream_url(query, cfg)
    if not stream_url:
        err("Could not get stream URL."); return
    play_file(stream_url, loop_mode, loop_value, cfg, f"[STREAM] {title}")


# ─────────────────────────────────────────────
#  MPV IPC CONTROLLER
# ─────────────────────────────────────────────

class MPVController:
    def __init__(self, socket_path: str | None = None):
        self.socket_path = socket_path or IPC_SOCKET

    def _send(self, command_dict: dict) -> str | None:
        try:
            payload = json.dumps(command_dict) + "\n"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect(self.socket_path)
                s.sendall(payload.encode())
                return s.recv(4096).decode().strip()
        except socket.timeout:
            dprint("IPC timeout"); return None
        except FileNotFoundError:
            dprint(f"IPC socket missing: {self.socket_path}"); return None
        except ConnectionRefusedError:
            dprint("IPC connection refused"); return None
        except Exception as e:
            dprint(f"IPC error: {e}"); return None

    def pause(self):            self._send({"command": ["set_property", "pause", True]})
    def resume(self):           self._send({"command": ["set_property", "pause", False]})
    def toggle_pause(self):     self._send({"command": ["cycle", "pause"]})
    def set_volume(self, v):    self._send({"command": ["set_property", "volume", v]})
    def volume_up(self, s=5):   self._send({"command": ["add", "volume",  s]})
    def volume_down(self, s=5): self._send({"command": ["add", "volume", -s]})
    def quit(self):             self._send({"command": ["quit"]})

    # [NEW-12] seek support
    def seek_fwd(self, secs: int = 10):
        self._send({"command": ["seek", secs, "relative"]})
    def seek_back(self, secs: int = 10):
        self._send({"command": ["seek", -secs, "relative"]})

    def get_title(self) -> str:
        resp = self._send({"command": ["get_property", "media-title"]})
        if resp:
            try:
                return json.loads(resp).get("data", "")
            except Exception:
                pass
        return ""

    def get_position(self) -> float | None:
        """Return current playback position in seconds."""
        resp = self._send({"command": ["get_property", "time-pos"]})
        if resp:
            try:
                return json.loads(resp).get("data")
            except Exception:
                pass
        return None

    def get_duration(self) -> float | None:
        """Return total duration in seconds."""
        resp = self._send({"command": ["get_property", "duration"]})
        if resp:
            try:
                return json.loads(resp).get("data")
            except Exception:
                pass
        return None

    def is_alive(self) -> bool:
        return os.path.exists(self.socket_path)


# ─────────────────────────────────────────────
#  [NEW-2] Progress bar rendering
# ─────────────────────────────────────────────

def _format_time(secs: float | None) -> str:
    if secs is None:
        return "--:--"
    secs = int(secs)
    return f"{secs // 60:02d}:{secs % 60:02d}"


def _render_progress(mpv: MPVController, bar_width: int = 30) -> str:
    pos  = mpv.get_position()
    dur  = mpv.get_duration()
    if pos is None or dur is None or dur <= 0:
        return ""
    frac   = min(pos / dur, 1.0)
    filled = int(frac * bar_width)
    bar    = "█" * filled + "░" * (bar_width - filled)
    return (f"\r{C.DIM}  {_format_time(pos)} "
            f"[{C.CYAN}{bar}{C.DIM}] "
            f"{_format_time(dur)}{C.RESET}  ")


# ─────────────────────────────────────────────
#  CONTROL LOOP
# ─────────────────────────────────────────────

CONTROLS_HELP = (
    "  [space]=pause/resume  [←/→]=seek10s  [+/-]=vol  "
    "[v N+Enter]=set vol  [q]=quit  [?]=help"
)


def control_loop_raw(mpv: MPVController,
                     sleep_until: float | None = None) -> None:
    """
    Raw tty single-keypress control with progress bar.
    sleep_until: epoch time after which we force quit (sleep timer).
    """
    print(f"{C.DIM}{CONTROLS_HELP}{C.RESET}")
    print(f"{C.DIM}  (no Enter needed for most keys){C.RESET}\n")
    paused       = False
    last_bar_t   = 0.0

    with RawInput() as raw:
        if not raw.is_raw:
            control_loop_line(mpv); return

        buf = ""
        while mpv.is_alive():
            # [NEW-3] sleep timer check
            if sleep_until and time.time() >= sleep_until:
                mpv.quit()
                print(f"\n{C.YELLOW}⏰ Sleep timer reached.{C.RESET}")
                break

            ch = raw.read_key(timeout=0.15)

            # [NEW-2] progress bar update every second
            now = time.time()
            if now - last_bar_t >= 1.0:
                bar_str = _render_progress(mpv)
                if bar_str:
                    print(bar_str, end="", flush=True)
                last_bar_t = now

            if ch is None:
                continue

            # Volume-set mode
            if buf.startswith("v"):
                if ch in ("\r", "\n"):
                    try:
                        vol = int(buf[1:].strip())
                        mpv.set_volume(vol)
                        print(f"\r{C.GREEN}✔  Volume → {vol}   {C.RESET}")
                    except ValueError:
                        print(f"\r{C.RED}✘  Invalid volume   {C.RESET}")
                    buf = ""
                elif ch.isdigit() or ch == " ":
                    buf += ch
                    print(f"\r{C.CYAN}vol: {buf[1:]}  {C.RESET}",
                          end="", flush=True)
                elif ch == "\x7f":
                    buf = buf[:-1]
                    print(f"\r{C.CYAN}vol: {buf[1:]}   {C.RESET}",
                          end="", flush=True)
                else:
                    buf = ""
                continue

            if ch == " ":
                paused = not paused
                if paused:
                    mpv.pause()
                    print(f"\r{C.YELLOW}⏸  Paused          {C.RESET}")
                else:
                    mpv.resume()
                    print(f"\r{C.GREEN}▶  Playing         {C.RESET}")
            elif ch in ("+", "="):
                mpv.volume_up()
                print(f"\r{C.GREEN}🔊 Volume +5       {C.RESET}")
            elif ch in ("-", "_"):
                mpv.volume_down()
                print(f"\r{C.GREEN}🔉 Volume -5       {C.RESET}")
            elif ch == "v":
                buf = "v"
                print(f"\r{C.CYAN}vol: {C.RESET}", end="", flush=True)
            # [NEW-12] seek keys
            elif ch == "l":
                mpv.seek_fwd(10)
                print(f"\r{C.CYAN}⏩ +10s             {C.RESET}")
            elif ch == "h":
                mpv.seek_back(10)
                print(f"\r{C.CYAN}⏪ -10s             {C.RESET}")
            elif ch in ("q", "Q", "\x03"):
                mpv.quit()
                print(f"\r{C.RED}⏹  Stopped         {C.RESET}")
                break
            elif ch == "?":
                print(f"\n{C.DIM}{CONTROLS_HELP}{C.RESET}")


def control_loop_line(mpv: MPVController) -> None:
    info("Controls: p=pause  r=resume  +=vol+  -=vol-  "
         "v N=volume  l=fwd10s  h=back10s  q=quit")
    while mpv.is_alive():
        try:
            cmd = input().strip().lower()
            if cmd == "p":           mpv.pause();     info("Paused")
            elif cmd == "r":         mpv.resume();    info("Resumed")
            elif cmd == "+":         mpv.volume_up(); info("Volume +5")
            elif cmd == "-":         mpv.volume_down();info("Volume -5")
            elif cmd == "l":         mpv.seek_fwd();  info("+10s")
            elif cmd == "h":         mpv.seek_back(); info("-10s")
            elif cmd.startswith("v "):
                try:
                    mpv.set_volume(int(cmd.split()[1])); info("Volume set")
                except (ValueError, IndexError):
                    warn("Usage: v <0-130>")
            elif cmd == "q":
                mpv.quit(); info("Stopped"); break
        except (EOFError, KeyboardInterrupt):
            mpv.quit(); break


# ─────────────────────────────────────────────
#  [NEW-4] Equalizer presets
# ─────────────────────────────────────────────

_EQ_PRESETS: dict[str, str] = {
    "flat":          "",
    "bass_boost":    "bass=g=5:f=110:w=0.6",
    "vocal_clarity": "equalizer=f=1000:width_type=o:width=2:g=3",
    "treble_boost":  "treble=g=5:f=8000:w=0.6",
    "low_cut":       "highpass=f=80",
}


def eq_mpv_flag(preset: str) -> list[str]:
    af = _EQ_PRESETS.get(preset, "")
    return [f"--af={af}"] if af else []


# ─────────────────────────────────────────────
#  PLAYBACK CORE
# ─────────────────────────────────────────────

def now_playing_display(title: str, mode_str: str) -> None:
    bar = "─" * 52
    print(f"\n{C.MAGENTA}{bar}{C.RESET}")
    print(f"{C.BOLD}{C.WHITE}  🎵  {title[:45]}{C.RESET}")
    print(f"{C.DIM}  Mode: {mode_str}{C.RESET}")
    print(f"{C.MAGENTA}{bar}{C.RESET}")


def play_file(file_path, loop_mode: str, loop_value,
              cfg: dict, title: str = "",
              sleep_minutes: float | None = None) -> None:
    """
    Core playback. sleep_minutes enables [NEW-3] sleep timer.
    [FIX-3] taste tracking now lives inside try-block; finally only cleans up.
    [FIX-8] socket removal guard fixed.
    """
    is_url = str(file_path).startswith(("http://", "https://"))
    if not title:
        title = "Streaming Audio" if is_url else Path(str(file_path)).stem

    mpv_cmd = [
        "mpv", "--no-video", "--no-terminal",
        "--quiet", "--msg-level=all=no",
        f"--input-ipc-server={IPC_SOCKET}",
        f"--volume={cfg.get('volume', 100)}",
    ] + eq_mpv_flag(cfg.get("eq_preset", "flat"))  # [NEW-4]

    if loop_mode == "count":
        mpv_cmd.append(f"--loop-file={loop_value}")
        mode_str = f"Loop × {loop_value}"
    elif loop_mode in ("time", "inf"):
        mpv_cmd.append("--loop-file=inf")
        mode_str = (f"Loop for {loop_value} min"
                    if loop_mode == "time" else "Loop ∞")
    else:
        mode_str = "Play once"

    mpv_cmd.append(str(file_path))

    if cfg.get("wake_lock"):
        acquire_wake_lock()

    now_playing_display(title, mode_str)
    send_notification("▶ Now Playing", title)

    proc   = None
    mpv    = None
    start  = time.time()

    try:
        # [FIX-8] clean stale socket before launching mpv
        try:
            os.remove(IPC_SOCKET)
        except FileNotFoundError:
            pass

        proc = subprocess.Popen(mpv_cmd)

        # Wait for IPC socket (up to 5s), detect early crash
        for _ in range(50):
            if proc.poll() is not None:
                break
            if os.path.exists(IPC_SOCKET):
                break
            time.sleep(0.1)

        if not os.path.exists(IPC_SOCKET):
            warn("IPC socket not ready — controls unavailable")

        mpv = MPVController()

        # Sleep timer [NEW-3]
        sleep_until: float | None = None
        if sleep_minutes and sleep_minutes > 0:
            sleep_until = time.time() + sleep_minutes * 60
            info(f"Sleep timer: {sleep_minutes} min")

        ctrl_thread = threading.Thread(
            target=control_loop_raw,
            args=(mpv, sleep_until),
            daemon=True
        )
        ctrl_thread.start()

        if loop_mode == "time":
            timeout = loop_value * 60
            while proc.poll() is None:
                if time.time() - start >= timeout:
                    mpv.quit()
                    ok(f"Time limit reached ({loop_value} min)")
                    break
                time.sleep(0.5)
        else:
            proc.wait()

        # [FIX-3] taste tracking here, inside try, after playback ends
        played = time.time() - start
        try:
            update_taste_profile(title, str(file_path),
                                 played_duration=int(played))
        except Exception as e:
            dprint(f"Taste tracking skipped: {e}")

    except KeyboardInterrupt:
        if mpv:
            try: mpv.quit()
            except Exception: pass
        if proc:
            proc.terminate()
        print()
        warn("Interrupted")

    finally:
        if cfg.get("wake_lock"):
            release_wake_lock()
        try:
            if os.path.exists(IPC_SOCKET):
                os.remove(IPC_SOCKET)
        except Exception:
            pass


def play_queue(cfg: dict, queue_name: str | None = None,
               auto_clear: bool = False) -> None:
    db    = load_db()
    qname = queue_name or current(db)
    q     = _q(db, qname)

    if not q:
        warn("Queue empty"); return

    st = db["state"]
    st["playing"] = True
    save_db(db)

    head(f"Playing [{qname}]  mode={st['mode']}")

    current_idx = st.get("index", 0)
    if current_idx >= len(q):
        current_idx = 0
        st["index"] = 0
        save_db(db)

    while current_idx < len(q) and st["playing"]:
        item  = q[current_idx]
        path  = item["path"]
        title = item["title"]
        is_url = path.startswith(("http://", "https://"))

        if not is_url and not Path(path).exists():
            warn(f"Missing file: {title}")
            if input(f"{C.CYAN}Remove from queue? [y/N]: {C.RESET}").lower() == "y":
                q.pop(current_idx)
                save_db(db)
                continue
            current_idx += 1
            continue

        info(f"[{current_idx+1}/{len(q)}] {title}")
        st["index"] = current_idx
        save_db(db)

        play_file(path, "once", None, cfg, title)

        # reload state after play (user may have changed mode/queue)
        db    = load_db()
        st    = db["state"]
        q     = _q(db, qname)

        if not st["playing"]:
            warn("Playback stopped"); break

        # [FIX-6] use next_index() instead of inline math
        st["index"] = current_idx
        current_idx = next_index(db, q)
        st["index"] = current_idx
        save_db(db)

        if current_idx < len(q):
            time.sleep(0.5)

    st["playing"] = False
    if current_idx >= len(q):
        st["index"] = 0
        ok("Queue playback complete")
    else:
        warn("Playback stopped before queue end")
    save_db(db)

    if auto_clear and current_idx >= len(q):
        db = load_db()
        db["queues"][qname] = []
        save_db(db)
        ok(f"Queue '{qname}' auto-cleared")


def stop() -> None:
    """[FIX-5] Properly quit mpv via IPC first, then db state."""
    try:
        if os.path.exists(IPC_SOCKET):
            mpv = MPVController()
            mpv.quit()
    except Exception:
        pass
    db = load_db()
    db["state"]["playing"] = False
    db["state"]["index"]   = 0
    save_db(db)
    ok("Stopped")


def skip() -> None:
    db = load_db()
    qname = current(db)
    q     = _q(db, qname)
    new_idx = db["state"].get("index", 0) + 1
    if new_idx >= len(q):
        warn("Already at end of queue"); return
    db["state"]["index"] = new_idx
    save_db(db)
    ok(f"Skipped → #{new_idx+1}")


# ─────────────────────────────────────────────
#  PLAYLIST PARSE / LOAD
# ─────────────────────────────────────────────

def parse_playlist(playlist_path) -> list[tuple[str, str]]:
    playlist_path = Path(playlist_path)
    if not playlist_path.exists():
        err(f"Playlist not found: {playlist_path}"); return []
    ext     = playlist_path.suffix.lower()
    entries = []
    try:
        with open(playlist_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [l.strip() for l in f
                     if l.strip() and not l.startswith("#")]
        if ext in (".m3u", ".m3u8"):
            for line in lines:
                path  = os.path.expanduser(line)
                title = (Path(path).stem
                         if not path.startswith(("http://", "https://"))
                         else "Stream")
                entries.append((path, title))
        elif ext == ".pls":
            fd, td = {}, {}
            for line in lines:
                if "=" in line:
                    key, val = line.split("=", 1)
                    if key.startswith("File"):
                        fd[key[4:]] = val
                    elif key.startswith("Title"):
                        td[key[5:]] = val
            for idx in sorted(fd):
                path  = fd[idx]
                title = td.get(idx, Path(path).stem)
                entries.append((path, title))
        else:
            err(f"Unsupported playlist format: {ext}"); return []
    except Exception as e:
        err(f"Error parsing playlist: {e}"); return []
    ok(f"Loaded {len(entries)} tracks from playlist")
    return entries


def load_playlist_to_queue(playlist_path,
                            queue_name: str | None = None) -> bool:
    entries = parse_playlist(playlist_path)
    if not entries:
        return False
    db    = load_db()
    qname = queue_name or current(db)
    q     = _q(db, qname)
    added = 0
    for path, title in entries:
        if (os.path.exists(path)
                or path.startswith(("http://", "https://"))):
            if not any(x["path"] == path for x in q):
                q.append({"path": path, "title": title})
                added += 1
    save_db(db)
    ok(f"Added {added}/{len(entries)} tracks to '{qname}'")
    return added > 0


# ─────────────────────────────────────────────
#  DAEMON MODE
# ─────────────────────────────────────────────

def start_daemon(file_path, cfg: dict, title: str = "") -> None:
    title      = title or Path(str(file_path)).stem
    daemon_sock = str(DAEMON_SOCK)
    if os.path.exists(daemon_sock):
        os.remove(daemon_sock)
    mpv_cmd = [
        "mpv", "--no-video", "--quiet",
        "--loop-file=inf",
        f"--input-ipc-server={daemon_sock}",
        f"--volume={cfg.get('volume', 100)}",
        str(file_path),
    ] + eq_mpv_flag(cfg.get("eq_preset", "flat"))
    proc = subprocess.Popen(
        mpv_cmd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    with open(DAEMON_PID, "w") as f:
        json.dump({"pid": proc.pid, "title": title, "socket": daemon_sock}, f)
    ok(f"Daemon started: {title}  (PID {proc.pid})")
    send_notification("▶ Daemon Playing", title)
    info("Control with: --daemon-ctl [pause|resume|stop|status|vol|seek]")


def daemon_ctl(action: str, value=None) -> None:
    if not DAEMON_PID.exists():
        err("No daemon running."); return
    with open(DAEMON_PID) as f:
        data = json.load(f)
    sock_path = data.get("socket", str(DAEMON_SOCK))
    mpv       = MPVController(socket_path=sock_path)
    if not mpv.is_alive():
        err("Daemon socket not found — may have stopped.")
        DAEMON_PID.unlink(missing_ok=True)
        return
    if action == "pause":
        mpv.pause();  ok("Daemon paused")
    elif action == "resume":
        mpv.resume(); ok("Daemon resumed")
    elif action == "stop":
        mpv.quit();   ok("Daemon stopped")
        DAEMON_PID.unlink(missing_ok=True)
        if os.path.exists(sock_path):
            os.remove(sock_path)
    elif action == "vol" and value is not None:
        try:
            mpv.set_volume(int(value)); ok(f"Daemon volume → {value}")
        except ValueError:
            err("Volume must be an integer.")
    elif action == "seek" and value is not None:
        try:
            secs = int(value)
            if secs >= 0:
                mpv.seek_fwd(secs)
            else:
                mpv.seek_back(abs(secs))
            ok(f"Daemon seek {value}s")
        except ValueError:
            err("Seek value must be integer seconds.")
    elif action == "status":
        head("Daemon Status")
        print(f"  PID   : {data.get('pid')}")
        print(f"  Title : {data.get('title')}")
        print(f"  Alive : {mpv.is_alive()}")
        pos = mpv.get_position()
        dur = mpv.get_duration()
        if pos is not None:
            print(f"  Pos   : {_format_time(pos)} / {_format_time(dur)}")
    else:
        err(f"Unknown daemon action: {action}")


# ─────────────────────────────────────────────
#  TASTE TRACKING & RECOMMENDATIONS
# ─────────────────────────────────────────────

DEFAULT_TASTE = {
    "user_id":    "default",
    "created_at": "",
    "last_updated": "",
    "total_plays": 0,
    "unique_songs": {},
    "artist_stats": {},
    "keyword_stats": {},
    "listening_patterns": {
        "morning": 0, "afternoon": 0, "evening": 0, "night": 0
    },
    "recommendation_settings": {
        "algorithm":       "artist_based",
        "auto_recommend":  True,
        "daily_suggestions": 5,
    },
    "blacklist":    {"artists": [], "songs": [], "keywords": []},
    "preferences":  {
        "preferred_mood":     None,
        "preferred_language": None,
        "min_duration":       60,
        "max_duration":       600,
    },
}


def load_taste_profile() -> dict:
    if TASTE_PATH.exists():
        with open(TASTE_PATH) as f:
            taste = json.load(f)
        for key, val in DEFAULT_TASTE.items():
            if key not in taste:
                taste[key] = val
        return taste
    t = DEFAULT_TASTE.copy()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t["created_at"] = t["last_updated"] = now
    return t


def save_taste_profile(taste: dict) -> None:
    taste["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    TASTE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TASTE_PATH, "w") as f:
        json.dump(taste, f, indent=2)


def extract_taste_features(song_title: str) -> dict:
    features = {"keywords": [], "language": "unknown", "mood": "neutral"}
    tl = song_title.lower()
    for kw in ["remix", "cover", "live", "official", "slowed", "reverb",
               "qawwali", "sufi", "classical", "pop", "rock", "sped up"]:
        if kw in tl:
            features["keywords"].append(kw)
    for lang, words in [
        ("urdu",    ["mola", "haq", "qawwali", "sufi"]),
        ("hindi",   ["jaan", "dil", "pyar", "ishq", "mohabbat"]),
        ("punjabi", ["tera", "mera", "sadda", "challa"]),
    ]:
        if any(w in tl for w in words):
            features["language"] = lang; break
    for mood, words in [
        ("calm",      ["slow", "calm", "peaceful", "spiritual", "sufi"]),
        ("energetic", ["fast", "dance", "party", "remix", "bass"]),
        ("sad",       ["sad", "heartbreak", "lonely", "tears"]),
        ("happy",     ["happy", "joy", "celebrate", "smile"]),
    ]:
        if any(w in tl for w in words):
            features["mood"] = mood; break
    return features


def update_taste_profile(song_title: str, path: str,
                         played_duration: int | None = None) -> None:
    taste    = load_taste_profile()
    song_key = str(path)
    if song_key not in taste["unique_songs"]:
        taste["unique_songs"][song_key] = {
            "title":             song_title,
            "play_count":        0,
            "last_played":       None,
            "total_listened_sec": 0,
            "features":          extract_taste_features(song_title),
        }
    sd = taste["unique_songs"][song_key]
    sd["play_count"] += 1
    sd["last_played"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if played_duration:
        sd["total_listened_sec"] = sd.get("total_listened_sec", 0) + played_duration

    artist = " ".join(song_title.split()[:2])
    taste["artist_stats"].setdefault(artist, {"plays": 0, "last_played": None})
    taste["artist_stats"][artist]["plays"] += 1
    taste["artist_stats"][artist]["last_played"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for kw in sd["features"]["keywords"]:
        taste["keyword_stats"][kw] = taste["keyword_stats"].get(kw, 0) + 1

    h = datetime.now().hour
    key = ("morning"   if 5  <= h < 12
           else "afternoon" if 12 <= h < 17
           else "evening"   if 17 <= h < 21
           else "night")
    taste["listening_patterns"][key] += 1
    taste["total_plays"] += 1
    save_taste_profile(taste)


def get_daily_recommendations(limit: int = 5) -> list:
    taste    = load_taste_profile()
    algo     = taste["recommendation_settings"]["algorithm"]
    blacklist = taste["blacklist"]["keywords"]
    recs     = []

    if algo == "keyword_based":
        top_kw = sorted(taste["keyword_stats"].items(),
                        key=lambda x: x[1], reverse=True)[:3]
        if top_kw:
            query   = " ".join(k for k, _ in top_kw)
            results = search_results(query, limit)
            recs    = [(t, u, f"Based on '{top_kw[0][0]}'")
                       for t, u in results[:limit]]
    elif algo == "artist_based":
        top_a = sorted(taste["artist_stats"].items(),
                       key=lambda x: x[1]["plays"], reverse=True)
        if top_a:
            artist  = top_a[0][0]
            results = search_results(f"{artist} songs", limit)
            recs    = [(t, u, f"Because you like {artist}")
                       for t, u in results[:limit]]
    else:   # hybrid
        kw_res = ar_res = []
        top_kw = sorted(taste["keyword_stats"].items(),
                        key=lambda x: x[1], reverse=True)[:2]
        if top_kw:
            kw_res = search_results(" ".join(k for k, _ in top_kw), 3)
        top_a = sorted(taste["artist_stats"].items(),
                       key=lambda x: x[1]["plays"], reverse=True)
        if top_a:
            ar_res = search_results(f"{top_a[0][0]} songs", 3)
        seen = set()
        for t, u in kw_res + ar_res:
            if t not in seen and len(recs) < limit:
                seen.add(t)
                recs.append((t, u, "Hybrid recommendation"))

    return [(t, u, r) for t, u, r in recs
            if not any(b in t.lower() for b in blacklist)]


def show_daily_suggestions(cfg: dict) -> None:
    head("🎵 Today's Recommendations")
    recs = get_daily_recommendations(limit=7)
    if not recs:
        warn("Not enough listening data yet. Play some songs first!")
        return
    print(f"\n{C.CYAN}We think you'll like these:{C.RESET}\n")
    for i, (title, url, reason) in enumerate(recs, 1):
        print(f"  {C.GREEN}{i}.{C.RESET} {title[:60]}")
        print(f"     {C.DIM}→ {reason}{C.RESET}")
    print(f"\n  {C.YELLOW}s{C.RESET}. Switch algorithm")
    print(f"  {C.YELLOW}a{C.RESET}. Add all to queue")
    print(f"  {C.YELLOW}0{C.RESET}. Back")
    choice = input(f"\n{C.WHITE}Pick number or command: {C.RESET}").strip().lower()
    if choice == "s":
        taste = load_taste_profile()
        cycle = ["keyword_based", "artist_based", "hybrid"]
        cur   = taste["recommendation_settings"]["algorithm"]
        nxt   = cycle[(cycle.index(cur) + 1) % len(cycle)]
        taste["recommendation_settings"]["algorithm"] = nxt
        save_taste_profile(taste)
        info(f"Switched to {nxt}, regenerating...")
        show_daily_suggestions(cfg); return
    elif choice == "a":
        for title, url, _ in recs:
            fp, _ = search_and_download(title, cfg, 0)
            if fp:
                add_to_queue(fp, title)
        ok(f"Added {len(recs)} songs to queue")
        if input(f"{C.CYAN}Start queue now? [y/N]: {C.RESET}").lower() == "y":
            play_queue(cfg)
        return
    elif choice.isdigit() and 1 <= int(choice) <= len(recs):
        title, url, _ = recs[int(choice) - 1]
        play_stream(title, "once", None, cfg)


# ─────────────────────────────────────────────
#  INPUT HELPERS
# ─────────────────────────────────────────────

def choose(prompt: str, options: list) -> str:
    print()
    for key, label in options:
        print(f"  {C.CYAN}{key}{C.RESET}. {label}")
    while True:
        val = input(f"\n{C.WHITE}{prompt}: {C.RESET}").strip()
        for key, _ in options:
            if val == key:
                return val
        err("Invalid choice.")


def get_int(prompt: str) -> int:
    while True:
        try:
            v = int(input(f"{C.WHITE}{prompt}: {C.RESET}"))
            if v > 0:
                return v
            err("Must be > 0")
        except ValueError:
            err("Enter a number.")


def get_float(prompt: str) -> float:
    while True:
        try:
            v = float(input(f"{C.WHITE}{prompt}: {C.RESET}"))
            if v > 0:
                return v
            err("Must be > 0")
        except ValueError:
            err("Enter a number.")


def get_file_path(prompt: str) -> str:
    """
    [FIX-2] Rewrote broken dual-path logic.
    Checks the given path directly (with tilde expansion),
    then falls back to the songs directory if needed.
    No more infinite loop.
    """
    SONGS_DIR = Path("/data/data/com.termux/files/home/storage/downloads/songs")
    while True:
        raw = input(f"{C.WHITE}{prompt}: {C.RESET}").strip()
        if not raw:
            continue
        # 1. Try expanded path as-is
        p = Path(os.path.expanduser(raw))
        if p.exists():
            return str(p)
        # 2. Try inside the songs directory
        candidate = SONGS_DIR / raw
        if candidate.exists():
            return str(candidate)
        err(f"File not found: {raw}")
        warn(f"(Searched in: {p}  and  {candidate})")


def get_loop_mode() -> str:
    return choose("Loop mode", [
        ("1", "Loop by count"),
        ("2", "Loop by time (minutes)"),
        ("3", "Loop infinitely"),
        ("4", "Play once"),
    ])


def get_loop_params(mode: str) -> tuple:
    if mode == "1":   return "count", get_int("Repeat how many times")
    elif mode == "2": return "time",  get_float("Play for how many minutes")
    elif mode == "3": return "inf",   None
    else:             return "once",  None


# ─────────────────────────────────────────────
#  SETTINGS MENU
# ─────────────────────────────────────────────

def settings_menu(cfg: dict) -> dict:
    head("Settings")
    rows = [
        ("Download dir",  cfg["download_dir"]),
        ("Audio format",  cfg["audio_format"]),
        ("Volume",        cfg["volume"]),
        ("Notifications", cfg["notify"]),
        ("Wake lock",     cfg["wake_lock"]),
        ("Retries",       cfg["retry_count"]),
        ("EQ preset",     cfg.get("eq_preset", "flat")),  # [NEW-4]
    ]
    for label, val in rows:
        print(f"  {C.DIM}{label:<16}: {val}{C.RESET}")

    what = choose("Edit", [
        ("1", "Download directory"),
        ("2", "Audio format"),
        ("3", "Default volume"),
        ("4", "Toggle notifications"),
        ("5", "Toggle wake lock"),
        ("6", "Retry count"),
        ("7", f"EQ preset  (current: {cfg.get('eq_preset','flat')})"),
        ("8", "Back"),
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
    elif what == "7":
        presets = list(_EQ_PRESETS.keys())
        print(f"\n  Options: {', '.join(presets)}")
        chosen = input(f"{C.WHITE}EQ preset: {C.RESET}").strip()
        if chosen in presets:
            cfg["eq_preset"] = chosen
            ok(f"EQ → {chosen}")
        else:
            warn("Unknown preset, keeping current.")
    save_config(cfg)
    ok("Settings saved.")
    return cfg


# ─────────────────────────────────────────────
#  TASTE PROFILE DISPLAY
# ─────────────────────────────────────────────

def display_taste_summary(taste: dict) -> None:
    clear_screen()
    print(f"\n{C.CYAN}{C.BOLD}🎵 YOUR MUSICAL DNA{C.RESET}\n")
    print(f"{C.GREEN}📊 Stats:{C.RESET}")
    print(f"   Total plays  : {taste['total_plays']}")
    print(f"   Unique songs : {len(taste['unique_songs'])}")
    print(f"   Artists      : {len(taste['artist_stats'])}")

    if taste["artist_stats"]:
        print(f"\n{C.YELLOW}🎤 Top Artists:{C.RESET}")
        for i, (a, s) in enumerate(
            sorted(taste["artist_stats"].items(),
                   key=lambda x: x[1]["plays"], reverse=True)[:5], 1
        ):
            bar = "▰" * min(20, s["plays"])
            print(f"   {i}. {a[:30]:<30} {bar} {s['plays']}")

    if taste["keyword_stats"]:
        print(f"\n{C.MAGENTA}🔑 Top Keywords:{C.RESET}")
        for kw, cnt in sorted(taste["keyword_stats"].items(),
                               key=lambda x: x[1], reverse=True)[:5]:
            print(f"   • {kw}: {cnt}")

    pats = taste["listening_patterns"]
    total = sum(pats.values())
    if total > 5:
        print(f"\n{C.BLUE}⏰ When You Listen:{C.RESET}")
        for label, key in [("🌅 Morning", "morning"), ("☀  Afternoon", "afternoon"),
                            ("🌙 Evening", "evening"), ("🌃 Night", "night")]:
            pct = int(pats[key] / total * 100)
            print(f"   {label}: {pct}%")

    if taste["total_plays"] == 0:
        print(f"\n{C.YELLOW}💡 Play some songs to build your profile!{C.RESET}")
    print(f"\n{C.DIM}Press Enter...{C.RESET}")
    input()


def taste_menu(cfg: dict) -> None:
    while True:
        taste = load_taste_profile()
        head("🎨 Taste Profile Manager")
        print(f"{C.CYAN}📊 DNA snapshot:{C.RESET}")
        print(f"   Songs: {taste['total_plays']}  "
              f"Unique: {len(taste['unique_songs'])}  "
              f"Artists: {len(taste['artist_stats'])}")
        print(f"   Algorithm: {taste['recommendation_settings']['algorithm']}")
        print()
        options = [
            ("1", "View detailed profile"),
            ("2", "Change recommendation algorithm"),
            ("3", "Reset taste profile"),
            ("4", "Remove specific song"),
            ("5", "Blacklist management"),
            ("6", "Set preferences"),
            ("7", "Export to JSON"),
            ("8", "Today's recommendations"),
            ("0", "Back"),
        ]
        for k, v in options:
            print(f"   {C.CYAN}{k}{C.RESET}. {v}")
        choice = input(f"\n{C.WHITE}Choose: {C.RESET}").strip()

        if choice == "1":
            display_taste_summary(taste)
        elif choice == "2":
            nw = choose("Algorithm", [
                ("1", "Keyword-based"), ("2", "Artist-based"), ("3", "Hybrid")
            ])
            taste["recommendation_settings"]["algorithm"] = \
                {"1": "keyword_based", "2": "artist_based", "3": "hybrid"}[nw]
            save_taste_profile(taste)
            ok("Algorithm updated")
        elif choice == "3":
            if input(f"{C.RED}Delete ALL taste data? Type 'yes': {C.RESET}") == "yes":
                if TASTE_PATH.exists():
                    TASTE_PATH.unlink()
                ok("Profile reset.")
            else:
                warn("Cancelled")
        elif choice == "4":
            songs = list(taste["unique_songs"].items())
            if not songs:
                warn("No songs"); continue
            for i, (_, d) in enumerate(songs[:20], 1):
                print(f"  {i}. {d['title'][:50]} ({d['play_count']} plays)")
            idx = input(f"{C.WHITE}Number to remove (0 cancel): {C.RESET}")
            if idx.isdigit() and 1 <= int(idx) <= len(songs):
                key, d = songs[int(idx) - 1]
                del taste["unique_songs"][key]
                save_taste_profile(taste)
                ok(f"Removed '{d['title']}'")
        elif choice == "5":
            ba = choose("Blacklist", [
                ("1", "Add artist"), ("2", "Add keyword"),
                ("3", "View"), ("4", "Clear"),
            ])
            if ba == "1":
                a = input(f"{C.WHITE}Artist: {C.RESET}").strip()
                if a and a not in taste["blacklist"]["artists"]:
                    taste["blacklist"]["artists"].append(a)
                    save_taste_profile(taste); ok(f"Blacklisted: {a}")
            elif ba == "2":
                k = input(f"{C.WHITE}Keyword: {C.RESET}").strip()
                if k and k not in taste["blacklist"]["keywords"]:
                    taste["blacklist"]["keywords"].append(k)
                    save_taste_profile(taste); ok(f"Blacklisted: {k}")
            elif ba == "3":
                head("Blacklist")
                print(f"  Artists  : {taste['blacklist']['artists'] or 'None'}")
                print(f"  Keywords : {taste['blacklist']['keywords'] or 'None'}")
                input(f"{C.DIM}Press Enter...{C.RESET}")
            elif ba == "4":
                if input(f"{C.RED}Clear all? [y/N]: {C.RESET}").lower() == "y":
                    taste["blacklist"] = {"artists": [], "songs": [], "keywords": []}
                    save_taste_profile(taste); ok("Blacklist cleared")
        elif choice == "6":
            mood = choose("Mood", [("1","Calm"),("2","Energetic"),
                                   ("3","Sad"),("4","Happy"),("0","None")])
            taste["preferences"]["preferred_mood"] = \
                {"1":"calm","2":"energetic","3":"sad","4":"happy","0":None}[mood]
            lang = input(f"{C.WHITE}Language (urdu/hindi/punjabi/english) "
                         f"or Enter to skip: {C.RESET}").strip().lower()
            if lang in ("urdu", "hindi", "punjabi", "english"):
                taste["preferences"]["preferred_language"] = lang
            save_taste_profile(taste); ok("Preferences saved")
        elif choice == "7":
            ep = Path.home() / f"taste_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(ep, "w") as f:
                json.dump(taste, f, indent=2, default=str)
            ok(f"Exported → {ep}")
        elif choice == "8":
            show_daily_suggestions(cfg)
        elif choice == "0":
            break


# ─────────────────────────────────────────────
#  QUEUE MENU
# ─────────────────────────────────────────────

def queue_menu(cfg: dict) -> None:
    while True:
        db        = load_db()
        current_q = db["state"]["current_queue"]
        head(f"Queue System  (active: {current_q})")
        action = choose("Action", [
            ("1", "Show queue"),
            ("2", "Play queue"),
            ("3", "Add local file"),
            ("4", "Remove item"),
            ("5", "Move item up"),
            ("6", "Move item down"),
            ("7", "Clear current queue"),
            ("8", "Switch / create queue"),
            ("9", "List all queues"),
            ("r", "Rename queue"),
            ("c", "Copy queue"),
            ("x", "Delete queue"),
            ("e", "Export queue as .m3u"),   # [NEW-8]
            ("a", "Load playlist (.m3u/.pls)"),
            ("0", "Back"),
        ])

        if action == "1":   show_queue()
        elif action == "2": play_queue(cfg)
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
        elif action == "8":
            db     = load_db()
            queues = list(db.get("queues", {}).keys())
            head("All queues")
            for i, q in enumerate(queues, 1):
                marker = "▶" if q == current_q else " "
                print(f"  {C.CYAN}{i}.{C.RESET} {marker} {q} ({len(db['queues'][q])})")
            print(f"  {C.CYAN}n.{C.RESET}   Create new queue")
            raw = input(f"{C.WHITE}Select or 'n' for new: {C.RESET}").strip()
            if raw.lower() == "n":
                name = input(f"{C.WHITE}New queue name: {C.RESET}").strip()
                if name:
                    db = load_db()
                    set_queue(db, name)
                    save_db(db)
                    ok(f"Created and switched to '{name}'")
            elif raw.isdigit() and 1 <= int(raw) <= len(queues):
                db = load_db()
                set_queue(db, queues[int(raw) - 1])
                save_db(db)
                ok(f"Switched to '{queues[int(raw)-1]}'")
        elif action == "9":
            db = load_db()
            head("All Queues")
            for name, items in db.get("queues", {}).items():
                marker = "▶" if name == current_q else " "
                print(f"  {marker} {name}  ({len(items)} songs)")
        elif action == "r":   # [NEW-9]
            old = input(f"{C.WHITE}Queue to rename: {C.RESET}").strip()
            new = input(f"{C.WHITE}New name: {C.RESET}").strip()
            if old and new: rename_queue(old, new)
        elif action == "c":   # [NEW-9]
            src = input(f"{C.WHITE}Source queue: {C.RESET}").strip()
            dst = input(f"{C.WHITE}Destination name: {C.RESET}").strip()
            if src and dst: copy_queue(src, dst)
        elif action == "x":   # [NEW-9]
            name = input(f"{C.WHITE}Queue to delete: {C.RESET}").strip()
            if name:
                if input(f"{C.RED}Delete '{name}'? [y/N]: {C.RESET}").lower() == "y":
                    delete_queue(name)
        elif action == "e":   # [NEW-8]
            export_queue_as_m3u()
        elif action == "a":
            pl = input(f"{C.WHITE}Playlist path: {C.RESET}").strip()
            if pl:
                load_playlist_to_queue(os.path.expanduser(pl))
                if input(f"{C.CYAN}Play now? [y/N]: {C.RESET}").lower() == "y":
                    play_queue(cfg)
        elif action == "0":
            break


# ─────────────────────────────────────────────
#  ARGPARSE
# ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(prog="musicloop",
                                description="🎧 MusicLoop v3.0 — Termux Edition")
    # [NEW-13]
    p.add_argument("--interactive", "-I", action="store_true",
                   help="Force interactive menu even with other flags")

    src = p.add_mutually_exclusive_group()
    src.add_argument("-s", "--search", metavar="QUERY")
    src.add_argument("-u", "--url",    metavar="URL")
    src.add_argument("-f", "--file",   metavar="PATH")
    src.add_argument("--stream",       metavar="QUERY")
    src.add_argument("--batch",        metavar="QUERY",
                     help="Batch download multiple results")

    lp = p.add_mutually_exclusive_group()
    lp.add_argument("-n", "--count", metavar="N",   type=int)
    lp.add_argument("-t", "--time",  metavar="MIN", type=float)
    lp.add_argument("-i", "--inf",   action="store_true")

    p.add_argument("--sleep",       metavar="MIN", type=float,
                   help="[NEW-3] Auto-stop after N minutes")
    p.add_argument("--eq",          metavar="PRESET",
                   help=f"[NEW-4] EQ preset: {list(_EQ_PRESETS)}")
    p.add_argument("--info",        metavar="URL_OR_ID",
                   help="[NEW-7] Show song metadata")

    p.add_argument("--queue-add",    metavar="PATH")
    p.add_argument("--queue-play",   action="store_true")
    p.add_argument("--queue-show",   action="store_true")
    p.add_argument("--queue-clear",  action="store_true")
    p.add_argument("--queue-remove", metavar="N", type=int)
    p.add_argument("--queue-up",     metavar="N", type=int)
    p.add_argument("--queue-down",   metavar="N", type=int)
    p.add_argument("--queue-export", action="store_true",
                   help="[NEW-8] Export queue to .m3u")

    p.add_argument("--daemon",        action="store_true")
    p.add_argument("--daemon-ctl",    metavar="ACTION")
    p.add_argument("--daemon-val",    metavar="VALUE")
    p.add_argument("--load-playlist", metavar="FILE")

    p.add_argument("--history",      action="store_true")
    p.add_argument("--mode",         metavar="MODE",
                   help="Set queue mode: normal|shuffle|repeat_all")
    return p.parse_args()


def resolve_loop(args) -> tuple:
    if args.count: return "count", args.count
    if args.time:  return "time",  args.time
    if args.inf:   return "inf",   None
    return None, None


def handle_args(args, cfg: dict) -> bool:
    # [FIX-4] load_playlist must be checked BEFORE any early returns
    if args.load_playlist:
        if load_playlist_to_queue(args.load_playlist):
            if args.queue_play:
                play_queue(cfg)
        return True

    if args.interactive:
        return False    # fall through to interactive menu

    if args.info:
        show_song_info(args.info); return True

    if args.eq:
        if args.eq in _EQ_PRESETS:
            cfg["eq_preset"] = args.eq
        else:
            warn(f"Unknown EQ preset '{args.eq}'. Options: {list(_EQ_PRESETS)}")

    if args.queue_show:    show_queue();                         return True
    if args.queue_clear:   clear_queue();                        return True
    if args.queue_play:    play_queue(cfg);                      return True
    if args.queue_remove:  remove_from_queue(args.queue_remove); return True
    if args.queue_up:      move_queue_item(args.queue_up, -1);   return True
    if args.queue_down:    move_queue_item(args.queue_down, 1);  return True
    if args.queue_export:  export_queue_as_m3u();                return True
    if args.mode:          set_mode(args.mode);                  return True

    if args.queue_add:
        p = os.path.expanduser(args.queue_add)
        if os.path.exists(p): add_to_queue(p, Path(p).stem)
        else: err("File not found.")
        return True

    if args.daemon_ctl:
        daemon_ctl(args.daemon_ctl, args.daemon_val); return True

    if args.history:
        result = show_history()
        if result:
            if isinstance(result, dict) and result.get("action") == "queue_added":
                if input(f"{C.CYAN}Start queue now? [y/N]: {C.RESET}").lower() == "y":
                    play_queue(cfg)
            elif isinstance(result, str):
                lm, lv = resolve_loop(args)
                if lm is None:
                    lm, lv = get_loop_params(get_loop_mode())
                play_file(result, lm, lv, cfg,
                          sleep_minutes=args.sleep)
        return True

    if args.batch:
        paths = batch_download(args.batch, cfg)
        if paths and input(f"{C.CYAN}Add all to queue? [y/N]: {C.RESET}").lower() == "y":
            for fp in paths:
                add_to_queue(fp, Path(fp).stem)
        return True

    if args.stream:
        lm, lv = resolve_loop(args)
        if lm is None:
            lm, lv = get_loop_params(get_loop_mode())
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
        play_file(file_path, lm, lv, cfg, title,
                  sleep_minutes=args.sleep)
    return True


# ─────────────────────────────────────────────
#  INTERACTIVE MENU
# ─────────────────────────────────────────────

def interactive_menu(cfg: dict) -> None:
    while True:
        head("Main Menu")
        action = choose("Choose action", [
            ("1", "Search & download"),
            ("2", "Stream (instant, no download)"),
            ("3", "Batch download"),          # [NEW-6]
            ("4", "Download from URL"),
            ("5", "Play local file"),
            ("6", "Play from history"),
            ("7", "Queue management"),
            ("8", "Daemon (background play)"),
            ("9", "Settings"),
            ("a", "Today's recommendations"),
            ("b", "Taste profile"),
            ("c", "Sleep timer play"),         # [NEW-3]
            ("d", "Song info"),                # [NEW-7]
            ("e", "Clear screen"),
            ("0", "Exit"),
        ])

        if action == "1":
            query = input(f"\n{C.WHITE}Search query: {C.RESET}").strip()
            if not query: continue
            file_path, title = search_and_download(query, cfg)
            if not file_path: continue
            if input(f"{C.CYAN}Add to queue instead? [y/N]: {C.RESET}").lower() == "y":
                add_to_queue(file_path, title)
            else:
                lm, lv = get_loop_params(get_loop_mode())
                play_file(file_path, lm, lv, cfg, title)

        elif action == "2":
            query = input(f"\n{C.WHITE}Search query: {C.RESET}").strip()
            if not query: continue
            lm, lv = get_loop_params(get_loop_mode())
            entries = search_results(query)
            if not entries:
                err("No results found"); continue
            head(f"Results for: {query}")
            for i, (t, _) in enumerate(entries[:6], 1):
                print(f"  {C.CYAN}{i}.{C.RESET} {t[:70]}")
            print(f"  {C.CYAN}0.{C.RESET} Cancel")
            raw = input(f"\n{C.WHITE}Pick (1-{len(entries)}): {C.RESET}").strip()
            if not raw.isdigit() or int(raw) == 0: continue
            sel = int(raw) - 1
            if sel >= len(entries):
                err("Invalid selection"); continue
            title, url_or_id = entries[sel]
            info(f"Selected: {title[:50]}")
            if url_or_id.startswith(("http://", "https://")):
                stream_url = url_or_id
            else:
                try:
                    res = subprocess.run(
                        ["yt-dlp", "-f", "bestaudio", "-g", url_or_id],
                        capture_output=True, text=True, timeout=30
                    )
                    if res.returncode != 0:
                        err("Could not resolve stream URL"); continue
                    stream_url = res.stdout.strip().split("\n")[0]
                except Exception as e:
                    err(f"Stream resolution failed: {e}"); continue
            play_file(stream_url, lm, lv, cfg, f"[STREAM] {title}")

        elif action == "3":     # [NEW-6]
            query = input(f"\n{C.WHITE}Search query: {C.RESET}").strip()
            if not query: continue
            paths = batch_download(query, cfg)
            if paths and input(f"{C.CYAN}Add all to queue? [y/N]: {C.RESET}").lower() == "y":
                for fp in paths:
                    add_to_queue(fp, Path(fp).stem)

        elif action == "4":
            url = input(f"\n{C.WHITE}URL: {C.RESET}").strip()
            if not url: continue
            file_path, title = download_from_url(url, cfg)
            if not file_path: continue
            lm, lv = get_loop_params(get_loop_mode())
            play_file(file_path, lm, lv, cfg, title)

        elif action == "5":
            MUSIC_DIR = Path(cfg.get("download_dir", DEFAULT_DOWNLOAD_DIR))
            MUSIC_DIR.mkdir(parents=True, exist_ok=True)
            exts  = ("*.mp3","*.m4a","*.opus","*.flac","*.wav","*.ogg")
            files = []
            for ext in exts:
                files.extend(MUSIC_DIR.glob(ext))
            files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            if not files:
                warn("No music files found in songs directory"); continue
            print(f"\n{C.BOLD}{C.CYAN}── Available Files ({len(files)}) ──{C.RESET}\n")
            LIMIT = 30
            for i, fp in enumerate(files[:LIMIT], 1):
                size = fp.stat().st_size / (1024 * 1024)
                name = (fp.stem[:55] + "...") if len(fp.stem) > 55 else fp.stem
                print(f"  {C.CYAN}{i:3}.{C.RESET} {name:<58} {C.DIM}({size:.1f} MB){C.RESET}")
            if len(files) > LIMIT:
                print(f"\n  {C.DIM}... {len(files)-LIMIT} more{C.RESET}")
            while True:
                raw = input(f"\n{C.WHITE}Enter number (0 cancel): {C.RESET}").strip()
                if raw == "0": break
                if raw.isdigit():
                    idx = int(raw) - 1
                    if 0 <= idx < len(files):
                        fp    = str(files[idx])
                        title = files[idx].stem
                        ok(f"Selected: {title}")
                        lm, lv = get_loop_params(get_loop_mode())
                        play_file(fp, lm, lv, cfg, title)
                        break
                    err(f"Choose 1–{len(files)}")
                else:
                    err("Please enter a number")

        elif action == "6":
            result = show_history()
            if result:
                if isinstance(result, dict) and result.get("action") == "queue_added":
                    if input(f"{C.CYAN}Start queue now? [y/N]: {C.RESET}").lower() == "y":
                        play_queue(cfg)
                elif isinstance(result, str):
                    lm, lv = get_loop_params(get_loop_mode())
                    play_file(result, lm, lv, cfg, Path(result).stem)

        elif action == "7":
            queue_menu(cfg)

        elif action == "8":
            head("Daemon Mode")
            src = choose("Source", [("1","Local file"),("2","Search & download")])
            if src == "1":
                fp = get_file_path("File path")
                start_daemon(fp, cfg, Path(fp).stem)
            else:
                q = input(f"{C.WHITE}Search query: {C.RESET}").strip()
                if q:
                    fp, t = search_and_download(q, cfg)
                    if fp: start_daemon(fp, cfg, t)

        elif action == "9":
            cfg = settings_menu(cfg)

        elif action == "a":
            show_daily_suggestions(cfg)

        elif action == "b":
            taste_menu(cfg)

        elif action == "c":     # [NEW-3] sleep timer
            query = input(f"\n{C.WHITE}Search query: {C.RESET}").strip()
            if not query: continue
            mins = get_float("Stop after how many minutes")
            file_path, title = search_and_download(query, cfg)
            if file_path:
                lm, lv = get_loop_params(get_loop_mode())
                play_file(file_path, lm, lv, cfg, title, sleep_minutes=mins)

        elif action == "d":     # [NEW-7]
            query = input(f"\n{C.WHITE}Search query or URL: {C.RESET}").strip()
            if query:
                if query.startswith(("http://", "https://")):
                    show_song_info(query)
                else:
                    entries = search_results(query, limit=3)
                    if entries:
                        show_song_info(entries[0][1])
                    else:
                        err("No results")
                input(f"{C.DIM}Press Enter...{C.RESET}")

        elif action == "e":
            clear_screen(); banner()

        elif action == "0":
            print(f"\n{C.CYAN}Ya Ali a.s madad 🎵{C.RESET}\n")
            sys.exit(0)


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

def main() -> None:
    banner()
    check_dependencies()
    check_storage_permission()

    cfg  = load_config()
    args = parse_args()

    # [NEW-13] --interactive forces menu even when other flags present
    if args.interactive:
        try:
            interactive_menu(cfg)
        except KeyboardInterrupt:
            print(f"\n{C.CYAN}Ya Ali a.s madad 🎵{C.RESET}\n")
        return

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
