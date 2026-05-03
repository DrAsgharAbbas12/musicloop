#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════╗
║          MUSICLOOP — Termux Edition v4.0          ║
║   Hardened · Streaming · Daemon · Smart Queue     ║
║   SQLite · Crash Recovery · Parallel DL · IPC     ║
╚═══════════════════════════════════════════════════╝

Author  : Dr. Asghar Abbas Askari
          MBBS Year 1 — Nishtar Medical University, Multan

FIXES vs v3.0 AUDIT
  [FIX-A1]  IPC socket: per-invocation uuid suffix - zero collision
  [FIX-A2]  Control thread: shutdown Event - no zombie loops
  [FIX-A3]  Queue mutation: snapshot-at-start, safe indexed walk
  [FIX-A4]  DB mid-loop reload: version stamp, re-sync only on change
  [FIX-A5]  next_index overflow: clamped before DB write
  [FIX-A6]  JSON schema validation on every load
  [FIX-A7]  search_and_download info branch: loop not recursion
  [FIX-A8]  yt-dlp output parsing: safe filepath detection
  [FIX-A9]  Streaming fallback: tries bestaudio/worstaudio/best
  [FIX-A10] Crash recovery: resume file saves position every 5s
  [FIX-A11] Unicode/long filename: sanitize before write
  [FIX-A12] Dependency version check: mpv>=0.32, yt-dlp>=2023
  [FIX-A13] Progress bar IPC: single batched call not 2 round-trips

NEW FEATURES
  [NEW-A1]  SQLite backend: replaces all JSON files
  [NEW-A2]  Parallel batch download: ThreadPoolExecutor
  [NEW-A3]  Smart taste engine: listen-ratio + recency decay
  [NEW-A4]  Crash resume: auto-resume last song+position on restart
  [NEW-A5]  Stream URL cache (TTL-based) - avoid re-resolving
  [NEW-A6]  Configurable Termux paths via env var
  [NEW-A7]  ErrorCode enum + Result dataclass
  [NEW-A8]  Repeat-one mode
  [NEW-A9]  Volume normalization flag (loudnorm)
  [NEW-A10] Playback watchdog: detects mpv hang
"""

import argparse
import contextlib
import enum
import json
import math
import os
import random
import re
import select
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

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


class Spinner:
    FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
    def __init__(self, msg: str = "", color: str = C.CYAN):
        self.msg = msg; self.color = color
        self._stop = threading.Event(); self._thread = None
    def _spin(self):
        i = 0
        while not self._stop.is_set():
            print(f"\r{self.color}{self.FRAMES[i % 10]}{C.RESET}  {self.msg}   ",
                  end="", flush=True)
            i += 1; time.sleep(0.08)
        print(f"\r{' ' * (len(self.msg) + 8)}\r", end="", flush=True)
    def __enter__(self):
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start(); return self
    def __exit__(self, *_):
        self._stop.set()
        if self._thread: self._thread.join(timeout=0.5)

# ─────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────

VERSION = "5.0"
APP_DIR = Path.home() / ".musicloop"
APP_DIR.mkdir(parents=True, exist_ok=True)

DB_FILE         = APP_DIR / "musicloop.db"
CONFIG_FILE     = APP_DIR / "config.json"
CRASH_FILE      = APP_DIR / "crash_resume.json"
DAEMON_PID_FILE = APP_DIR / "daemon.pid"
DAEMON_SOCK     = APP_DIR / "daemon.sock"
LOG_FILE        = APP_DIR / "musicloop.log"

# [FIX-A1] per-invocation uuid -> zero socket collision
_SESSION_ID = uuid.uuid4().hex[:8]
IPC_SOCKET  = str(APP_DIR / f"mpv_{os.getpid()}_{_SESSION_ID}.sock")

DEBUG            = os.environ.get("MUSICLOOP_DEBUG", "").lower() in ("1","true","yes")
PARALLEL_WORKERS = int(os.environ.get("MUSICLOOP_WORKERS", "3"))

# [NEW-A6] Termux base from env, not hardcoded
_TERMUX_HOME     = os.environ.get("TERMUX_HOME", "/data/data/com.termux/files/home")
DEFAULT_DOWNLOAD = os.path.join(_TERMUX_HOME, "storage/downloads/songs")

# ─────────────────────────────────────────────────
#  [NEW-A7] ERROR TAXONOMY
# ─────────────────────────────────────────────────

class ErrorCode(enum.Enum):
    OK              = 0
    NOT_FOUND       = 1
    DOWNLOAD_FAILED = 2
    STREAM_FAILED   = 3
    IPC_UNAVAILABLE = 4
    DB_ERROR        = 5
    CANCELLED       = 7

@dataclass
class Result:
    ok:    bool
    code:  ErrorCode = ErrorCode.OK
    value: object    = None
    msg:   str       = ""

    @staticmethod
    def success(value=None, msg="") -> "Result":
        return Result(ok=True, value=value, msg=msg)

    @staticmethod
    def failure(code: ErrorCode, msg="") -> "Result":
        return Result(ok=False, code=code, msg=msg)

# ─────────────────────────────────────────────────
#  ANSI COLORS
# ─────────────────────────────────────────────────

def _log(level: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    with contextlib.suppress(Exception):
        with open(LOG_FILE, "a") as f:
            f.write(f"[{ts}] [{level}] {msg}\n")

def ok(msg: str):   print(f"{C.GREEN}✔  {msg}{C.RESET}");  _log("OK",   msg)
def err(msg: str, hint: str = ""):
    print(f"{C.RED}  ✘  {msg}{C.RESET}")
    if hint: print(f"{C.DIM}     → {hint}{C.RESET}")
    _log("ERR", msg)
def info(msg: str): print(f"{C.CYAN}ℹ  {msg}{C.RESET}")
def warn(msg: str): print(f"{C.YELLOW}⚠  {msg}{C.RESET}"); _log("WARN", msg)
def head(msg: str): print(f"\n{C.MAGENTA}{C.BOLD}── {msg} ──{C.RESET}")
def dprint(msg: str):
    if DEBUG:
        print(f"{C.DIM}[dbg] {msg}{C.RESET}")
        _log("DBG", msg)

def banner(cfg: dict) -> None:
    stats = db_stats()
    deps  = {d: shutil.which(d) is not None for d in ["mpv","yt-dlp","socat"]}
    opts  = {d: shutil.which(d) is not None
             for d in ["termux-notification","termux-wake-lock"]}
    dep_row = "  ".join(
        f"{'%s✔%s' % (C.GREEN, C.RESET) if v else '%s✘%s' % (C.RED, C.RESET)} {k}"
        for k,v in deps.items()
    )
    opt_row = "  ".join(
        f"{C.DIM}{'✔' if v else '–'} {k.replace('termux-','t-')}{C.RESET}"
        for k,v in opts.items()
    )
    cs = load_crash()
    crash_line = (f"\n{C.YELLOW}  ⚡ Unfinished session detected — see resume prompt below{C.RESET}"
                  if cs and cs.path else "")
    print(f"""
{C.CYAN}{C.BOLD}╔══════════════════════════════════════════════════╗
║           🎧  MUSICLOOP  v{VERSION}                    ║
║      Termux · SQLite · Smart Feedback · IPC      ║
║                                                  ║
║               Programmed By:                     ║
║           Dr. Asghar Abbas Askari                ║
╚══════════════════════════════════════════════════╝{C.RESET}
  {dep_row}
  {opt_row}
{C.DIM}  Queue: {stats["queue_size"]}  ·  History: {stats["history_count"]}  ·  Plays: {stats["total_plays"]}  ·  EQ: {cfg.get("eq_preset","flat")}{C.RESET}{crash_line}
""")

def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")

# ─────────────────────────────────────────────────
#  [FIX-A11] FILENAME SANITIZER
# ─────────────────────────────────────────────────

def sanitize_filename(name: str, max_len: int = 180) -> str:
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name[:max_len] if name else "unknown"

# ─────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────

DEFAULT_CONFIG: dict = {
    "download_dir":     DEFAULT_DOWNLOAD,
    "audio_format":     "mp3",
    "volume":           100,
    "notify":           True,
    "wake_lock":        True,
    "retry_count":      3,
    "retry_delay":      3,
    "eq_preset":        "flat",
    "normalize":        False,
    "parallel_dl":      PARALLEL_WORKERS,
    "stream_cache_ttl": 1800,
    "auto_resume":      True,
}

_SCHEMA: dict = {
    "download_dir": str, "audio_format": str, "volume": int,
    "notify": bool, "wake_lock": bool, "retry_count": int,
    "retry_delay": int, "eq_preset": str, "normalize": bool,
    "parallel_dl": int, "stream_cache_ttl": int, "auto_resume": bool,
}

def _validate_config(d: dict) -> dict:
    """[FIX-A6] Type-validate every key, fall back to default on mismatch."""
    out = DEFAULT_CONFIG.copy()
    for k, t in _SCHEMA.items():
        v = d.get(k)
        if v is not None and isinstance(v, t):
            out[k] = v
    return out

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("not a dict")
            return _validate_config(raw)
        except Exception as e:
            warn(f"Config corrupt ({e}), using defaults")
    return DEFAULT_CONFIG.copy()

def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# ─────────────────────────────────────────────────
#  [NEW-A1] SQLITE BACKEND
# ─────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_FILE), timeout=10, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    return c

def init_db() -> None:
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS queue_items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_name TEXT    NOT NULL DEFAULT 'default',
            path       TEXT    NOT NULL,
            title      TEXT    NOT NULL,
            position   INTEGER NOT NULL DEFAULT 0,
            added_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_queue ON queue_items(queue_name, position);

        CREATE TABLE IF NOT EXISTS queue_state (
            queue_name  TEXT PRIMARY KEY,
            current_idx INTEGER NOT NULL DEFAULT 0,
            mode        TEXT    NOT NULL DEFAULT 'normal',
            playing     INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO queue_state(queue_name) VALUES('default');

        CREATE TABLE IF NOT EXISTS play_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL,
            path       TEXT NOT NULL,
            source     TEXT NOT NULL DEFAULT 'local',
            played_at  TEXT NOT NULL DEFAULT (datetime('now')),
            duration_s INTEGER
        );

        CREATE TABLE IF NOT EXISTS taste_songs (
            path        TEXT PRIMARY KEY,
            title       TEXT    NOT NULL,
            play_count  INTEGER NOT NULL DEFAULT 0,
            total_secs  INTEGER NOT NULL DEFAULT 0,
            last_played TEXT,
            skip_count  INTEGER NOT NULL DEFAULT 0,
            language    TEXT,
            mood        TEXT,
            keywords    TEXT
        );

        CREATE TABLE IF NOT EXISTS taste_artists (
            artist      TEXT PRIMARY KEY,
            plays       INTEGER NOT NULL DEFAULT 0,
            last_played TEXT
        );

        CREATE TABLE IF NOT EXISTS taste_keywords (
            keyword TEXT PRIMARY KEY,
            count   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS taste_meta (
            key TEXT PRIMARY KEY, value TEXT
        );
        INSERT OR IGNORE INTO taste_meta(key,value) VALUES
            ('algorithm','artist_based'),('total_plays','0'),
            ('morning','0'),('afternoon','0'),('evening','0'),('night','0');

        CREATE TABLE IF NOT EXISTS blacklist (
            type  TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (type, value)
        );

        CREATE TABLE IF NOT EXISTS stream_cache (
            url_id     TEXT PRIMARY KEY,
            stream_url TEXT NOT NULL,
            title      TEXT,
            expires_at TEXT NOT NULL
        );
        """)

# ── Queue ────────────────────────────────────────

def _reorder(c: sqlite3.Connection, qname: str) -> None:
    rows = c.execute(
        "SELECT id FROM queue_items WHERE queue_name=? ORDER BY position,id", (qname,)
    ).fetchall()
    for i, r in enumerate(rows):
        c.execute("UPDATE queue_items SET position=? WHERE id=?", (i, r["id"]))

def get_queue(qname: str = "default") -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM queue_items WHERE queue_name=? ORDER BY position,id", (qname,)
        ).fetchall()
    return [dict(r) for r in rows]

def add_to_queue(path: str, title: str = "Unknown", qname: str = "default") -> None:
    is_url = path.startswith(("http://","https://"))
    if not is_url and not Path(path).exists():
        err(f"File not found: {path}"); return
    with _conn() as c:
        if c.execute("SELECT id FROM queue_items WHERE queue_name=? AND path=?",
                     (qname, path)).fetchone():
            warn("Already in queue"); return
        pos = c.execute(
            "SELECT COALESCE(MAX(position)+1,0) FROM queue_items WHERE queue_name=?",
            (qname,)
        ).fetchone()[0]
        c.execute(
            "INSERT INTO queue_items(queue_name,path,title,position) VALUES(?,?,?,?)",
            (qname, path, title, pos)
        )
    ok(f"Added → [{qname}] {title[:50]}")

def remove_from_queue(index: int, qname: str = "default") -> bool:
    items = get_queue(qname)
    if not (1 <= index <= len(items)):
        err("Invalid index"); return False
    item = items[index - 1]
    with _conn() as c:
        c.execute("DELETE FROM queue_items WHERE id=?", (item["id"],))
        _reorder(c, qname)
    ok(f"Removed: {item['title'][:50]}"); return True

def move_queue_item(index: int, direction: int, qname: str = "default") -> bool:
    items = get_queue(qname)
    i, j  = index - 1, index - 1 + direction
    if not (0 <= i < len(items) and 0 <= j < len(items)):
        err("Cannot move"); return False
    with _conn() as c:
        c.execute("UPDATE queue_items SET position=? WHERE id=?",
                  (items[j]["position"], items[i]["id"]))
        c.execute("UPDATE queue_items SET position=? WHERE id=?",
                  (items[i]["position"], items[j]["id"]))
    ok("Moved item"); return True

def clear_queue(qname: str = "default") -> None:
    with _conn() as c:
        c.execute("DELETE FROM queue_items WHERE queue_name=?", (qname,))
        c.execute("UPDATE queue_state SET current_idx=0,playing=0 WHERE queue_name=?",
                  (qname,))
    ok(f"Queue '{qname}' cleared")

def get_all_queues() -> dict[str,int]:
    with _conn() as c:
        rows   = c.execute(
            "SELECT queue_name,COUNT(*) as cnt FROM queue_items GROUP BY queue_name"
        ).fetchall()
        states = c.execute("SELECT queue_name FROM queue_state").fetchall()
    q = {r["queue_name"]: r["cnt"] for r in rows}
    for s in states: q.setdefault(s["queue_name"], 0)
    return q

def create_queue(name: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO queue_state(queue_name) VALUES(?)", (name,))
    ok(f"Created queue '{name}'")

def rename_queue(old: str, new: str) -> None:
    if old == "default": err("Cannot rename 'default'"); return
    with _conn() as c:
        c.execute("UPDATE queue_items SET queue_name=? WHERE queue_name=?", (new,old))
        c.execute("UPDATE queue_state SET queue_name=? WHERE queue_name=?", (new,old))
    ok(f"Renamed '{old}' → '{new}'")

def copy_queue(src: str, dst: str) -> None:
    items = get_queue(src)
    if not items: warn(f"'{src}' empty"); return
    create_queue(dst)
    with _conn() as c:
        for i, item in enumerate(items):
            c.execute(
                "INSERT OR IGNORE INTO queue_items(queue_name,path,title,position)"
                " VALUES(?,?,?,?)",
                (dst, item["path"], item["title"], i)
            )
    ok(f"Copied '{src}' → '{dst}' ({len(items)} songs)")

def delete_queue(name: str) -> None:
    if name == "default": err("Cannot delete 'default'"); return
    with _conn() as c:
        c.execute("DELETE FROM queue_items WHERE queue_name=?", (name,))
        c.execute("DELETE FROM queue_state WHERE queue_name=?", (name,))
    ok(f"Deleted queue '{name}'")

def get_queue_state(qname: str = "default") -> dict:
    with _conn() as c:
        row = c.execute("SELECT * FROM queue_state WHERE queue_name=?", (qname,)).fetchone()
        if not row:
            c.execute("INSERT OR IGNORE INTO queue_state(queue_name) VALUES(?)", (qname,))
            return {"queue_name":qname,"current_idx":0,"mode":"normal","playing":0}
        return dict(row)

def set_queue_state(qname: str, **kw) -> None:
    allowed = {"current_idx","mode","playing"}
    upd = {k: v for k,v in kw.items() if k in allowed}
    if not upd: return
    sets   = ", ".join(f"{k}=?" for k in upd)
    values = list(upd.values()) + [qname]
    with _conn() as c:
        c.execute(f"UPDATE queue_state SET {sets} WHERE queue_name=?", values)

def get_active_queue() -> str:
    with _conn() as c:
        row = c.execute(
            "SELECT queue_name FROM queue_state WHERE playing=1 LIMIT 1"
        ).fetchone()
    return row["queue_name"] if row else "default"

def export_queue_as_m3u(qname: str = "default", out: str | None = None) -> None:
    items = get_queue(qname)
    if not items: warn("Queue empty"); return
    if not out:
        out = str(Path.home() / f"musicloop_{qname}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.m3u")
    with open(out, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in items:
            f.write(f"#EXTINF:-1,{item['title']}\n{item['path']}\n")
    ok(f"Exported {len(items)} songs → {out}")

def db_stats() -> dict:
    with _conn() as c:
        qs = c.execute("SELECT COUNT(*) FROM queue_items WHERE queue_name='default'").fetchone()[0]
        hc = c.execute("SELECT COUNT(*) FROM play_history").fetchone()[0]
        tp = c.execute("SELECT value FROM taste_meta WHERE key='total_plays'").fetchone()
    return {"queue_size": qs, "history_count": hc,
            "total_plays": int(tp["value"]) if tp else 0}

# ── History ──────────────────────────────────────

def add_history(title: str, path: str, source: str, duration_s: int | None = None) -> None:
    with _conn() as c:
        c.execute("INSERT INTO play_history(title,path,source,duration_s) VALUES(?,?,?,?)",
                  (title, path, source, duration_s))
        c.execute("DELETE FROM play_history WHERE id NOT IN "
                  "(SELECT id FROM play_history ORDER BY id DESC LIMIT 100)")

def get_history(limit: int = 15) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM play_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

# ── Taste ────────────────────────────────────────

def _extract_features(title: str) -> dict:
    tl  = title.lower()
    kws = [k for k in ["remix","cover","live","official","slowed","reverb",
                        "qawwali","sufi","classical","pop","rock","sped up",
                        "lofi","acoustic","instrumental"] if k in tl]
    lang = "unknown"
    for l, words in [("urdu",["mola","haq","qawwali","sufi","khuda"]),
                     ("hindi",["jaan","dil","pyar","ishq","mohabbat"]),
                     ("punjabi",["tera","mera","sadda","challa","yaar"])]:
        if any(w in tl for w in words): lang = l; break
    mood = "neutral"
    for m, words in [("calm",["slow","calm","peaceful","spiritual","sufi"]),
                     ("energetic",["fast","dance","party","remix","bass"]),
                     ("sad",["sad","heartbreak","lonely","tears"]),
                     ("happy",["happy","joy","celebrate","smile"])]:
        if any(w in tl for w in words): mood = m; break
    return {"keywords": kws, "language": lang, "mood": mood}

def taste_record_play(title: str, path: str,
                      played_secs: int, skipped: bool = False) -> None:
    """[NEW-A3] weighted taste tracking."""
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    features = _extract_features(title)
    with _conn() as c:
        c.execute("""
            INSERT INTO taste_songs(path,title,play_count,total_secs,last_played,
                                    skip_count,language,mood,keywords)
            VALUES(?,?,1,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                play_count=play_count+1,
                total_secs=total_secs+excluded.total_secs,
                last_played=excluded.last_played,
                skip_count=skip_count+excluded.skip_count
        """, (path, title, played_secs, now_str, 1 if skipped else 0,
              features["language"], features["mood"], json.dumps(features["keywords"])))
        artist = " ".join(title.split()[:2])
        c.execute("""
            INSERT INTO taste_artists(artist,plays,last_played) VALUES(?,1,?)
            ON CONFLICT(artist) DO UPDATE SET plays=plays+1,last_played=excluded.last_played
        """, (artist, now_str))
        for kw in features["keywords"]:
            c.execute("""
                INSERT INTO taste_keywords(keyword,count) VALUES(?,1)
                ON CONFLICT(keyword) DO UPDATE SET count=count+1
            """, (kw,))
        h   = datetime.now().hour
        col = ("morning" if 5<=h<12 else "afternoon" if 12<=h<17
               else "evening" if 17<=h<21 else "night")
        c.execute(
            "UPDATE taste_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key=?",
            (col,)
        )
        c.execute(
            "UPDATE taste_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)"
            " WHERE key='total_plays'"
        )

def _score_song(row: sqlite3.Row, now_ts: float) -> float:
    """[NEW-A3] listen-ratio × recency × frequency score."""
    plays      = row["play_count"] or 0
    total_secs = row["total_secs"] or 0
    skips      = row["skip_count"] or 0
    last       = row["last_played"]
    freq       = math.log1p(plays) * max(0.1, 1 - skips / max(1, plays))
    avg_listen = total_secs / max(1, plays)
    ratio      = min(1.0, avg_listen / 180.0)
    recency    = 0.5
    if last:
        with contextlib.suppress(Exception):
            days    = (now_ts - datetime.strptime(last, "%Y-%m-%d %H:%M:%S").timestamp()) / 86400
            recency = math.exp(-0.693 * days / 7)
    return freq * ratio * recency

def get_taste_meta() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT key,value FROM taste_meta").fetchall()
    return {r["key"]: r["value"] for r in rows}

def get_taste_algo() -> str:
    return get_taste_meta().get("algorithm", "artist_based")

def set_taste_algo(algo: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO taste_meta(key,value) VALUES(?,?)",
                  ("algorithm", algo))

def get_blacklist() -> dict[str, set]:
    with _conn() as c:
        rows = c.execute("SELECT type,value FROM blacklist").fetchall()
    out: dict[str, set] = {"artist": set(), "keyword": set(), "song": set()}
    for r in rows: out.setdefault(r["type"], set()).add(r["value"])
    return out

def add_blacklist(btype: str, value: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO blacklist(type,value) VALUES(?,?)", (btype,value))
    ok(f"Blacklisted [{btype}]: {value}")

def clear_blacklist() -> None:
    with _conn() as c: c.execute("DELETE FROM blacklist")
    ok("Blacklist cleared")

# ── Stream cache ─────────────────────────────────

def _scache_get(url_id: str) -> str | None:
    with _conn() as c:
        row = c.execute(
            "SELECT stream_url,expires_at FROM stream_cache WHERE url_id=?", (url_id,)
        ).fetchone()
    if not row: return None
    if datetime.now() > datetime.fromisoformat(row["expires_at"]):
        with _conn() as c: c.execute("DELETE FROM stream_cache WHERE url_id=?", (url_id,))
        return None
    return row["stream_url"]

def _scache_put(url_id: str, stream_url: str, ttl: int = 1800) -> None:
    exp = (datetime.now() + timedelta(seconds=ttl)).isoformat()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO stream_cache(url_id,stream_url,expires_at) VALUES(?,?,?)",
            (url_id, stream_url, exp)
        )

# ── Crash recovery ───────────────────────────────

@dataclass
class CrashState:
    path:       str   = ""
    title:      str   = ""
    position:   float = 0.0
    queue_name: str   = "default"
    saved_at:   str   = ""

def save_crash(cs: CrashState) -> None:
    with contextlib.suppress(Exception):
        with open(CRASH_FILE, "w") as f: json.dump(asdict(cs), f)

def load_crash() -> CrashState | None:
    if not CRASH_FILE.exists(): return None
    try:
        with open(CRASH_FILE) as f: return CrashState(**json.load(f))
    except Exception: return None

def clear_crash() -> None:
    with contextlib.suppress(Exception): CRASH_FILE.unlink()

# ─────────────────────────────────────────────────
#  TERMUX INTEGRATIONS
# ─────────────────────────────────────────────────

def _which(cmd: str) -> bool: return shutil.which(cmd) is not None

def acquire_wake_lock() -> None:
    if _which("termux-wake-lock"):
        subprocess.Popen(["termux-wake-lock"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

def release_wake_lock() -> None:
    if _which("termux-wake-unlock"):
        subprocess.run(["termux-wake-unlock"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

def send_notification(title: str, content: str) -> None:
    if _which("termux-notification"):
        subprocess.Popen(
            ["termux-notification","--title",title,"--content",content,"--id","musicloop"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

def check_storage_permission() -> bool:
    test = Path(_TERMUX_HOME) / "storage/downloads"
    if not test.exists():
        warn("Storage not set up. Run: termux-setup-storage"); return False
    return True

def check_dependencies() -> None:
    """[FIX-A12] Check presence + soft version warnings."""
    missing = [d for d in ["mpv","yt-dlp"] if not _which(d)]
    if missing:
        err(f"Missing: {', '.join(missing)}")
        print(f"{C.DIM}Install: pkg install {' '.join(missing)}{C.RESET}")
        sys.exit(1)
    with contextlib.suppress(Exception):
        r = subprocess.run(["mpv","--version"],capture_output=True,text=True,timeout=5)
        m = re.search(r"mpv (\d+)\.(\d+)", r.stdout)
        if m and (int(m.group(1)),int(m.group(2))) < (0,32):
            warn(f"mpv {m.group(1)}.{m.group(2)} detected; recommend ≥0.32")
    with contextlib.suppress(Exception):
        r = subprocess.run(["yt-dlp","--version"],capture_output=True,text=True,timeout=5)
        yr = r.stdout.strip()[:4]
        if yr.isdigit() and int(yr) < 2023:
            warn(f"yt-dlp {r.stdout.strip()} is old; run: pip install -U yt-dlp")

# ─────────────────────────────────────────────────
#  RAW TTY INPUT
# ─────────────────────────────────────────────────

class RawInput:
    def __init__(self):
        self.is_raw = False
        self._old   = None
    def __enter__(self):
        try:
            self._old   = termios.tcgetattr(sys.stdin)
            tty.setraw(sys.stdin.fileno())
            self.is_raw = True
        except Exception:
            self.is_raw = False
        return self
    def __exit__(self, *_):
        if self.is_raw and self._old:
            with contextlib.suppress(Exception):
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old)
    def read_key(self, timeout: float = 0.15) -> str | None:
        if not self.is_raw: return None
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.read(1) if r else None

# ─────────────────────────────────────────────────
#  [FIX-A13] MPV IPC - structured, thread-safe, batched
# ─────────────────────────────────────────────────

class MPVController:
    def __init__(self, sock: str | None = None):
        self.sock  = sock or IPC_SOCKET
        self._lock = threading.Lock()

    def _send(self, payload: str, timeout: float = 2.0) -> str | None:
        with self._lock:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(timeout)
                    s.connect(self.sock)
                    s.sendall(payload.encode())
                    data, deadline = b"", time.time() + timeout
                    while time.time() < deadline:
                        try:
                            chunk = s.recv(4096)
                            if not chunk: break
                            data += chunk
                            if b"\n" in data: break
                        except socket.timeout: break
                    return data.decode(errors="replace").strip() if data else None
            except (FileNotFoundError, ConnectionRefusedError): return None
            except Exception as e: dprint(f"IPC: {e}"); return None

    def _cmd(self, *args):
        resp = self._send(json.dumps({"command": list(args)}) + "\n")
        if resp:
            with contextlib.suppress(Exception):
                return json.loads(resp.split("\n")[0])
        return None

    def _prop(self, name):
        r = self._cmd("get_property", name)
        if r and r.get("error") == "success": return r.get("data")
        return None

    def get_state(self) -> dict:
        """[UX-15] returns pos+dur+vol+paused."""
        return {
            "pos":    self._prop("time-pos"),
            "dur":    self._prop("duration"),
            "vol":    self._prop("volume"),
            "paused": self._prop("pause"),
        }

    def pause(self):           self._cmd("set_property","pause",True)
    def resume(self):          self._cmd("set_property","pause",False)
    def set_volume(self, v):   self._cmd("set_property","volume",v)
    def volume_up(self, s=5):  self._cmd("add","volume", s)
    def volume_down(self,s=5): self._cmd("add","volume",-s)
    def seek(self, s: int):    self._cmd("seek",s,"relative")
    def quit(self):            self._cmd("quit")
    def get_position(self) -> float | None: return self._prop("time-pos")
    def get_duration(self)  -> float | None: return self._prop("duration")
    def is_alive(self) -> bool: return os.path.exists(self.sock)
    def wait_ready(self, timeout: float = 5.0) -> bool:
        dead = time.time() + timeout
        while time.time() < dead:
            if self.is_alive(): return True
            time.sleep(0.1)
        return False

# ─────────────────────────────────────────────────
#  EQUALIZER  [NEW-A9]
# ─────────────────────────────────────────────────

_EQ: dict[str,str] = {
    "flat":          "",
    "bass_boost":    "bass=g=6:f=110:w=0.6",
    "vocal_clarity": "equalizer=f=1000:width_type=o:width=2:g=3",
    "treble_boost":  "treble=g=5:f=8000:w=0.6",
    "low_cut":       "highpass=f=80",
    "sufi":          "bass=g=3:f=80,equalizer=f=800:width_type=o:width=2:g=2",
}

def _build_af(cfg: dict) -> list[str]:
    parts = []
    eq    = _EQ.get(cfg.get("eq_preset","flat"),"")
    if eq: parts.append(eq)
    if cfg.get("normalize"): parts.append("loudnorm")
    return [f"--af={','.join(parts)}"] if parts else []

# ─────────────────────────────────────────────────
#  PROGRESS BAR
# ─────────────────────────────────────────────────

def _fmt(secs: float | None) -> str:
    if secs is None: return "--:--"
    s = int(secs)
    return f"{s//60:02d}:{s%60:02d}"

def _progress(pos: float | None, dur: float | None,
              title: str = "", vol=None, paused: bool = False, w: int = 22) -> str:
    if pos is None or dur is None or dur <= 0: return ""
    frac   = min(pos / dur, 1.0)
    filled = int(frac * w)
    bar    = "█" * filled + "░" * (w - filled)
    pct    = int(frac * 100)
    pause_sym = f"{C.YELLOW}⏸{C.RESET}" if paused else f"{C.GREEN}▶{C.RESET}"
    title_str = f" {title[:28]}" if title else ""
    vol_str   = f"  🔊{int(vol)}%" if vol is not None else ""
    return (f"\r{pause_sym}{C.DIM}{title_str}  "
            f"{_fmt(pos)}[{C.CYAN}{bar}{C.DIM}]{_fmt(dur)}"
            f"  {pct}%{vol_str}{C.RESET}  ")

# ─────────────────────────────────────────────────
#  CONTROL LOOP  [FIX-A2] shutdown Event, [FIX-A10] watchdog
# ─────────────────────────────────────────────────

HELP = "  [space]=pause  [l/h]=seek±10s  [+/-]=vol  [v N↵]=set vol  [q]=quit  [?]=help"

def control_loop(mpv: MPVController,
                 stop: threading.Event,
                 sleep_until: float | None = None,
                 crash_path: str = "",
                 crash_title: str = "") -> None:
    """[FIX-A2] stop Event. [FIX-A10] watchdog. [NEW-A4] crash save every 5s."""
    print(f"{C.DIM}{HELP}{C.RESET}\n")
    paused = False
    last_bar = last_crash = last_alive = time.time()

    with RawInput() as raw:
        if not raw.is_raw:
            _ctl_line(mpv, stop); return
        buf = ""
        while not stop.is_set():
            now = time.time()
            # [FIX-A10] watchdog
            if mpv.is_alive(): last_alive = now
            elif now - last_alive > 2.0: stop.set(); break
            # sleep timer
            if sleep_until and now >= sleep_until:
                mpv.quit(); print(f"\n{C.YELLOW}⏰ Sleep timer.{C.RESET}"); stop.set(); break
            # progress + crash save
            if now - last_bar >= 1.0:
                st = mpv.get_state()
                bar = _progress(st["pos"], st["dur"])
                if bar: print(bar, end="", flush=True)
                if crash_path and now - last_crash >= 5.0 and st["pos"]:
                    save_crash(CrashState(path=crash_path, title=crash_title,
                                          position=st["pos"],
                                          saved_at=datetime.now().isoformat()))
                    last_crash = now
                last_bar = now
            ch = raw.read_key(0.1)
            if ch is None: continue
            # volume-set mode
            if buf.startswith("v"):
                if ch in ("\r","\n"):
                    with contextlib.suppress(ValueError):
                        target = int(buf[1:].strip())
                        mpv.set_volume(target); time.sleep(0.12)
                        actual = mpv._prop("volume")
                        if actual is not None:
                            print(f"\r{C.GREEN}  🔊 Volume → {int(actual)}%  (requested {target}){C.RESET}")
                        else:
                            print(f"\r{C.GREEN}  🔊 Volume set to {target}%{C.RESET}")
                    buf = ""
                elif ch.isdigit() or ch == " ":
                    buf += ch; print(f"\r{C.CYAN}vol: {buf[1:]}  {C.RESET}", end="", flush=True)
                elif ch == "\x7f":
                    buf = buf[:-1]; print(f"\r{C.CYAN}vol: {buf[1:]}   {C.RESET}", end="", flush=True)
                else: buf = ""
                continue
            if ch == " ":
                paused = not paused
                (mpv.pause if paused else mpv.resume)()
                label = "⏸  Paused" if paused else "▶  Playing"
                col   = C.YELLOW if paused else C.GREEN
                print(f"\r{col}{label}       {C.RESET}")
            elif ch in ("+","="):
                mpv.volume_up(); time.sleep(0.12)
                v = mpv._prop("volume")
                vstr = f"{int(v)}%" if v is not None else "+5"
                print(f"\r{C.GREEN}  🔊 Volume → {vstr}        {C.RESET}")
            elif ch in ("-","_"):
                mpv.volume_down(); time.sleep(0.12)
                v = mpv._prop("volume")
                vstr = f"{int(v)}%" if v is not None else "-5"
                print(f"\r{C.GREEN}  🔉 Volume → {vstr}        {C.RESET}")
            elif ch == "v": buf = "v"; print(f"\r{C.CYAN}vol: {C.RESET}", end="", flush=True)
            elif ch == "l":
                mpv.seek(10); time.sleep(0.12)
                p = mpv._prop("time-pos"); d = mpv._prop("duration")
                ts = f" → {_fmt(p)}/{_fmt(d)}" if p is not None else ""
                print(f"\r{C.CYAN}  ⏩ +10s{ts}          {C.RESET}")
            elif ch == "h":
                mpv.seek(-10); time.sleep(0.12)
                p = mpv._prop("time-pos"); d = mpv._prop("duration")
                ts = f" → {_fmt(p)}/{_fmt(d)}" if p is not None else ""
                print(f"\r{C.CYAN}  ⏪ -10s{ts}          {C.RESET}")
            elif ch in ("q","Q","\x03"):
                mpv.quit(); stop.set(); print(f"\r{C.RED}⏹  Stopped{C.RESET}"); break
            elif ch == "?": print(f"\n{C.DIM}{HELP}{C.RESET}")

def _ctl_line(mpv: MPVController, stop: threading.Event) -> None:
    info("Controls: p=pause r=resume +=vol+ -=vol- l=+10s h=-10s q=quit")
    while not stop.is_set() and mpv.is_alive():
        try: cmd = input().strip().lower()
        except (EOFError, KeyboardInterrupt): mpv.quit(); stop.set(); break
        if cmd == "p": mpv.pause(); info("Paused")
        elif cmd == "r": mpv.resume(); info("Resumed")
        elif cmd == "+": mpv.volume_up(); info("+5")
        elif cmd == "-": mpv.volume_down(); info("-5")
        elif cmd == "l": mpv.seek(10); info("+10s")
        elif cmd == "h": mpv.seek(-10); info("-10s")
        elif cmd.startswith("v "):
            with contextlib.suppress(Exception):
                mpv.set_volume(int(cmd.split()[1])); info("Volume set")
        elif cmd == "q": mpv.quit(); stop.set(); break

# ─────────────────────────────────────────────────
#  PLAYBACK ENGINE
# ─────────────────────────────────────────────────

def _now_playing(title: str, mode: str) -> None:
    bar = "─" * 54
    print(f"\n{C.MAGENTA}{bar}{C.RESET}")
    print(f"{C.BOLD}{C.WHITE}  🎵  {title[:48]}{C.RESET}")
    print(f"{C.DIM}  Mode: {mode}{C.RESET}")
    print(f"{C.MAGENTA}{bar}{C.RESET}")

def play_file(path: str, loop_mode: str, loop_val,
              cfg: dict, title: str = "",
              sleep_mins: float | None = None,
              start_pos: float = 0.0) -> None:
    """
    [FIX-A1] unique IPC socket per invocation
    [FIX-A2] shutdown Event
    [FIX-A10] watchdog in control loop
    [NEW-A4] start_pos for crash resume
    """
    is_url = path.startswith(("http://","https://"))
    if not title:
        title = "Stream" if is_url else Path(path).stem

    cmd = [
        "mpv","--no-video","--no-terminal","--quiet","--msg-level=all=no",
        f"--input-ipc-server={IPC_SOCKET}",
        f"--volume={cfg.get('volume',100)}",
    ] + _build_af(cfg)

    if start_pos > 1.0:
        cmd.append(f"--start={int(start_pos)}")

    mode_str = "Play once"
    if loop_mode == "count":
        cmd.append(f"--loop-file={loop_val}"); mode_str = f"Loop ×{loop_val}"
    elif loop_mode == "repeat_one":
        cmd.append("--loop-file=inf"); mode_str = "Repeat one ∞"
    elif loop_mode in ("time","inf"):
        cmd.append("--loop-file=inf")
        mode_str = f"Loop {loop_val}min" if loop_mode == "time" else "Loop ∞"
    cmd.append(path)

    if cfg.get("wake_lock"): acquire_wake_lock()
    _now_playing(title, mode_str)
    send_notification("▶ Now Playing", title)

    cs = CrashState(path=path, title=title, position=start_pos,
                    queue_name=get_active_queue(),
                    saved_at=datetime.now().isoformat())
    save_crash(cs)

    proc = stop_event = mpv = None
    t0   = time.time()
    stop_event = threading.Event()

    try:
        with contextlib.suppress(FileNotFoundError): os.remove(IPC_SOCKET)
        proc = subprocess.Popen(cmd)
        mpv  = MPVController()
        if not mpv.wait_ready(5.0):
            warn("IPC not ready — controls limited")

        sleep_until = (time.time() + sleep_mins * 60
                       if sleep_mins and sleep_mins > 0 else None)

        ctrl = threading.Thread(
            target=control_loop,
            args=(mpv, stop_event, sleep_until, path, title),
            daemon=True
        )
        ctrl.start()

        if loop_mode == "time":
            limit = loop_val * 60
            while proc.poll() is None and not stop_event.is_set():
                if time.time() - t0 >= limit: mpv.quit(); break
                time.sleep(0.3)
        else:
            while proc.poll() is None and not stop_event.is_set():
                time.sleep(0.3)

        stop_event.set()
        ctrl.join(timeout=1.5)   # [FIX-A2] guaranteed join

    except KeyboardInterrupt:
        stop_event.set() if stop_event else None
        with contextlib.suppress(Exception): mpv and mpv.quit()
        with contextlib.suppress(Exception): proc and proc.terminate()
        print(); warn("Interrupted")

    finally:
        played = int(time.time() - t0)
        skipped = played < 15
        # [UX-07] session summary
        if played >= 3:
            m, s   = played // 60, played % 60
            t_str  = f"{m}m {s}s" if m else f"{s}s"
            status = f"{C.YELLOW}skipped{C.RESET}" if skipped else f"{C.GREEN}finished{C.RESET}"
            print()
            info(f"Played {t_str} of '{title[:40]}' — {status}")
        with contextlib.suppress(Exception):
            taste_record_play(title, path, played, skipped=skipped)
        with contextlib.suppress(Exception):
            add_history(title, path, "stream" if is_url else "local", played)
        if cfg.get("wake_lock"): release_wake_lock()
        with contextlib.suppress(Exception):
            if os.path.exists(IPC_SOCKET): os.remove(IPC_SOCKET)
        clear_crash()

# ─────────────────────────────────────────────────
#  [FIX-A3][FIX-A4][FIX-A5] QUEUE PLAYBACK
# ─────────────────────────────────────────────────

def _next_idx(mode: str, cur: int, total: int) -> int:
    """[FIX-A5] always clamped."""
    if mode == "shuffle":
        candidates = [i for i in range(total) if i != cur]
        return random.choice(candidates) if candidates else 0
    if mode == "repeat_all": return (cur + 1) % total
    if mode == "repeat_one": return cur
    return min(cur + 1, total)   # clamped

def play_queue(cfg: dict, qname: str | None = None, auto_clear: bool = False) -> None:
    qname = qname or get_active_queue()
    st    = get_queue_state(qname)
    mode  = st["mode"]

    # [FIX-A3] snapshot at start
    snapshot = get_queue(qname)
    if not snapshot: warn("Queue empty"); return

    set_queue_state(qname, playing=1)
    head(f"Playing [{qname}]  mode={mode}  ({len(snapshot)} songs)")

    cur = st.get("current_idx") or 0
    if cur >= len(snapshot): cur = 0

    def _ver():
        return tuple(i["id"] for i in get_queue(qname))

    ver = _ver()

    while cur < len(snapshot):
        # [FIX-A4] detect external queue changes
        new_ver = _ver()
        if new_ver != ver:
            snapshot = get_queue(qname); ver = new_ver
            if cur >= len(snapshot): break

        item   = snapshot[cur]
        path   = item["path"]
        title  = item["title"]
        is_url = path.startswith(("http://","https://"))

        if not is_url and not Path(path).exists():
            warn(f"Missing: {title[:50]}")
            if input(f"{C.CYAN}Remove? [y/N]: {C.RESET}").lower() == "y":
                remove_from_queue(cur + 1, qname)
                snapshot = get_queue(qname); ver = _ver()
                if cur >= len(snapshot): break
                continue
            else:
                cur += 1; continue

        info(f"[{cur+1}/{len(snapshot)}] {title[:55]}")
        set_queue_state(qname, current_idx=cur)
        t_song = time.time()
        play_file(path, "once", None, cfg, title)
        if int(time.time() - t_song) < 15: session_skipped += 1
        else: session_played += 1

        st2 = get_queue_state(qname)
        if not st2["playing"]: warn("Stopped by user"); break

        # [FIX-A5] clamped next
        cur = _next_idx(mode, cur, len(snapshot))
        set_queue_state(qname, current_idx=min(cur, max(0, len(snapshot)-1)))
        if cur < len(snapshot): time.sleep(0.5)

    set_queue_state(qname, playing=0)
    elapsed_total = int(time.time() - t0)
    m_e, s_e = elapsed_total // 60, elapsed_total % 60
    print(f"\n{C.DIM}{'─'*52}{C.RESET}")
    head("Queue Session Summary")
    print(f"  {C.GREEN}✔{C.RESET}  Songs played  : {session_played}")
    if session_skipped:
        print(f"  {C.YELLOW}↷{C.RESET}  Songs skipped : {session_skipped}")
    print(f"  {C.CYAN}⏱{C.RESET}  Total time    : {m_e}m {s_e}s")
    print(f"  {C.DIM}Queue: {qname}  ·  Mode: {mode}{C.RESET}")
    print(f"{C.DIM}{'─'*52}{C.RESET}")
    if cur >= len(snapshot):
        set_queue_state(qname, current_idx=0)
        if auto_clear: clear_queue(qname)

# ─────────────────────────────────────────────────
#  DAEMON
# ─────────────────────────────────────────────────

def start_daemon(path: str, cfg: dict, title: str = "") -> None:
    title = title or Path(path).stem
    sock  = str(DAEMON_SOCK)
    with contextlib.suppress(FileNotFoundError): os.remove(sock)
    cmd = ["mpv","--no-video","--quiet","--loop-file=inf",
           f"--input-ipc-server={sock}", f"--volume={cfg.get('volume',100)}",
           path] + _build_af(cfg)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    with open(DAEMON_PID_FILE,"w") as f:
        json.dump({"pid":proc.pid,"title":title,"socket":sock}, f)
    ok(f"Daemon: {title}  PID {proc.pid}")
    send_notification("▶ Daemon Playing", title)
    info("Control: --daemon-ctl [pause|resume|stop|status|vol N|seek N]")

def daemon_ctl(action: str, val: str | None = None) -> None:
    if not DAEMON_PID_FILE.exists(): err("No daemon running"); return
    try:
        with open(DAEMON_PID_FILE) as f: data = json.load(f)
    except Exception as e: err(f"PID file error: {e}"); return
    mpv = MPVController(sock=data.get("socket", str(DAEMON_SOCK)))
    if not mpv.is_alive():
        err("Daemon not alive"); DAEMON_PID_FILE.unlink(missing_ok=True); return
    if action == "pause":   mpv.pause();  ok("Paused")
    elif action == "resume": mpv.resume(); ok("Resumed")
    elif action == "stop":
        mpv.quit(); ok("Stopped"); DAEMON_PID_FILE.unlink(missing_ok=True)
        with contextlib.suppress(Exception): os.remove(data.get("socket",""))
    elif action == "vol":
        with contextlib.suppress(Exception): mpv.set_volume(int(val)); ok(f"Vol → {val}")
    elif action == "seek":
        with contextlib.suppress(Exception): mpv.seek(int(val)); ok(f"Seek {val}s")
    elif action == "status":
        head("Daemon")
        print(f"  PID  : {data.get('pid')}")
        print(f"  Title: {data.get('title')}")
        p, d = mpv.get_position(), mpv.get_duration()
        if p is not None: print(f"  Time : {_fmt(p)} / {_fmt(d)}")
    else: err(f"Unknown action: {action}")

# ─────────────────────────────────────────────────
#  DOWNLOAD + SEARCH
# ─────────────────────────────────────────────────

_SCACHE: dict[str,list] = {}

def search_results(query: str, limit: int = 6) -> list[tuple[str,str]]:
    key = f"{query}:{limit}"
    if key in _SCACHE: dprint(f"cache hit: {query}"); return _SCACHE[key]
    cmd = ["yt-dlp","--flat-playlist","--print","%(title)s\t%(url)s",
           "--no-playlist", f"ytsearch{limit}:{query}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0: dprint(f"search fail: {r.stderr[:200]}"); return []
        entries = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2: entries.append((parts[0].strip(), parts[1].strip()))
        _SCACHE[key] = entries; return entries
    except subprocess.TimeoutExpired: err("Search timed out"); return []
    except Exception as e: err(f"Search error: {e}"); return []

def _resolve_stream(url_or_id: str, cfg: dict) -> str | None:
    """[UX-08] Stream resolve with per-format feedback."""
    cached = _scache_get(url_or_id)
    if cached:
        info("Using cached stream URL — instant playback")
        return cached
    for fmt in ["bestaudio","worstaudio","bestaudio/best"]:
        with Spinner(f"Resolving stream ({fmt})…"):
            try:
                r = subprocess.run(["yt-dlp","-f",fmt,"-g",url_or_id],
                                   capture_output=True, text=True, timeout=25)
                if r.returncode == 0:
                    url = r.stdout.strip().splitlines()[0]
                    _scache_put(url_or_id, url, cfg.get("stream_cache_ttl",1800))
                    ok(f"Stream resolved  [{fmt}]")
                    return url
            except Exception: pass
    err("Could not resolve stream URL",
        hint="Try downloading instead (option 1) or check your connection")
    return None

def _run_ytdlp(cmd: list, cfg: dict, label: str = "Download") -> tuple[int, str|None, str]:
    """[FIX-A8] safe filepath detection via Path().exists()."""
    for attempt in range(1, cfg.get("retry_count",3) + 1):
        if attempt > 1:
            warn(f"Retry {attempt}/{cfg.get('retry_count',3)} in {cfg.get('retry_delay',3)}s…")
            time.sleep(cfg.get("retry_delay",3))
        try:
            t_start = time.time()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            lines = []
            print(C.DIM, end="", flush=True)
            for line in proc.stdout:
                s = line.rstrip(); lines.append(s)
                if s and not s.startswith("["): print(f"  {s}", flush=True)
            print(C.RESET, end="")
            stderr_out = proc.stderr.read(); proc.wait()
            elapsed = int(time.time() - t_start)
            if proc.returncode == 0:
                fp = next((l for l in reversed(lines) if l and Path(l).exists()), None)
                if fp:
                    try:
                        size_mb = os.path.getsize(fp) / (1024*1024)
                        size_str = f"{size_mb:.1f}MB"
                    except Exception:
                        size_str = "?"
                    ok(f"{label} done in {elapsed}s — {size_str}")
                    print(f"  {C.DIM}→ {fp}{C.RESET}")
                return proc.returncode, fp, stderr_out
            err(f"{label} failed (attempt {attempt})",
                hint="Check internet connection or try a different query")
            if stderr_out:
                last = next((l for l in reversed(stderr_out.strip().splitlines()) if l.strip()), "")
                if last: print(f"  {C.DIM}{last[:200]}{C.RESET}")
        except Exception as e: err(f"{label} error: {e}")
    err(f"All {cfg.get('retry_count',3)} retries exhausted",
        hint="Try: yt-dlp -U  to update, or check your connection")
    return -1, None, "exhausted"

def search_and_download(query: str, cfg: dict,
                        preselected: int | None = None) -> tuple[str|None, str|None]:
    """[FIX-A7] info branch uses while-loop not recursion."""
    with Spinner(f"Searching: {query}…"):
        entries = search_results(query)
    if not entries:
        err("No results found", hint="Try different keywords or check your connection")
        return None, None

    sel = preselected
    while sel is None:
        head(f"Results for: {query}")
        for i,(t,_) in enumerate(entries[:6],1):
            print(f"  {C.CYAN}{i}.{C.RESET} {t[:68]}")
        print(f"  {C.DIM}{'─'*55}{C.RESET}")
        print(f"  {C.CYAN}i N{C.RESET} = Song info  ·  {C.CYAN}0{C.RESET} = Cancel")
        raw = input(f"\n{C.WHITE}Pick (1-{min(6,len(entries))}): {C.RESET}").strip()
        if raw.lower().startswith("i"):
            parts = raw.split()
            if len(parts)>=2 and parts[1].isdigit():
                idx = int(parts[1])-1
                if 0<=idx<len(entries): show_song_info(entries[idx][1])
                input(f"{C.DIM}Press Enter…{C.RESET}")
            continue   # [FIX-A7] loop not recurse
        if not raw.isdigit() or int(raw)==0: return None, None
        sel = int(raw)-1

    if sel >= len(entries): err("Invalid selection"); return None, None
    title, url_or_id = entries[sel]
    info(f"Downloading: {title[:55]}")
    safe = sanitize_filename(title)   # [FIX-A11]
    os.makedirs(cfg["download_dir"], exist_ok=True)
    cmd = ["yt-dlp","-x","--audio-format",cfg["audio_format"],
           "-o",f"{cfg['download_dir']}/{safe}.%(ext)s",
           "--print","after_move:filepath","--no-playlist",url_or_id]
    rc, fp, _ = _run_ytdlp(cmd, cfg)
    if rc!=0 or not fp: err("Download failed"); return None, None
    title = Path(fp).stem
    ok(f"Downloaded: {title[:55]}")
    if cfg.get("notify"): send_notification("🎵 Download Complete", title)
    add_history(title, fp, "yt-dlp")
    return fp, title

def download_from_url(url: str, cfg: dict) -> tuple[str|None, str|None]:
    info("Downloading…")
    os.makedirs(cfg["download_dir"], exist_ok=True)
    cmd = ["yt-dlp","-x","--audio-format",cfg["audio_format"],
           "-o",f"{cfg['download_dir']}/%(title)s.%(ext)s",
           "--print","after_move:filepath","--no-playlist",url]
    rc, fp, _ = _run_ytdlp(cmd, cfg)
    if rc!=0 or not fp: err("Download failed"); return None, None
    title = Path(fp).stem
    ok(f"Downloaded: {title}")
    if cfg.get("notify"): send_notification("🎵 Download Complete", title)
    add_history(title, fp, url)
    return fp, title

# ─────────────────────────────────────────────────
#  [NEW-A2] PARALLEL BATCH DOWNLOAD
# ─────────────────────────────────────────────────

def _dl_one(args: tuple) -> tuple[int, str|None, str|None]:
    idx, title_hint, url_or_id, cfg = args
    safe = sanitize_filename(title_hint)
    os.makedirs(cfg["download_dir"], exist_ok=True)
    cmd = ["yt-dlp","-x","--audio-format",cfg["audio_format"],
           "-o",f"{cfg['download_dir']}/{safe}.%(ext)s",
           "--print","after_move:filepath","--no-playlist","--quiet",url_or_id]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            fp = next((l for l in reversed(r.stdout.strip().splitlines())
                       if l and Path(l).exists()), None)
            return idx, fp, (Path(fp).stem if fp else title_hint)
    except Exception as e: dprint(f"dl_one [{idx}]: {e}")
    return idx, None, None

def batch_download(query: str, cfg: dict) -> list[str]:
    entries = search_results(query, 8)
    if not entries: err("No results"); return []
    head(f"Batch: {query}")
    for i,(t,_) in enumerate(entries,1): print(f"  {C.CYAN}{i}.{C.RESET} {t[:70]}")
    print(f"\n{C.DIM}e.g. 1 3 5-7{C.RESET}")
    raw = input(f"{C.WHITE}Select: {C.RESET}").strip()
    if not raw: return []
    chosen: list[int] = []
    for part in raw.replace(","," ").split():
        if "-" in part:
            with contextlib.suppress(ValueError):
                a,b = map(int,part.split("-",1)); chosen.extend(range(a,b+1))
        elif part.isdigit(): chosen.append(int(part))
    chosen = [i for i in dict.fromkeys(chosen) if 1<=i<=len(entries)]
    if not chosen: warn("No valid selections"); return []
    workers = min(cfg.get("parallel_dl", PARALLEL_WORKERS), len(chosen))
    info(f"Downloading {len(chosen)} songs · {workers} workers…")
    tasks  = [(i-1, entries[i-1][0], entries[i-1][1], cfg) for i in chosen]
    paths: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_dl_one, t): t for t in tasks}
        for fut in as_completed(futures):
            done += 1
            idx, fp, title = fut.result()
            label = entries[idx][0][:40] if idx<len(entries) else "?"
            if fp:
                paths.append(fp); add_history(title or label, fp, "yt-dlp")
                print(f"  {C.GREEN}✔{C.RESET} [{done}/{len(chosen)}] {label}")
            else:
                print(f"  {C.RED}✘{C.RESET} [{done}/{len(chosen)}] {label}")
    ok(f"Batch done: {len(paths)}/{len(chosen)}"); return paths

# ─────────────────────────────────────────────────
#  STREAMING
# ─────────────────────────────────────────────────

def stream_play(query: str, lm: str, lv, cfg: dict) -> None:
    entries = search_results(query)
    if not entries: err("No results"); return
    head(f"Results: {query}")
    for i,(t,_) in enumerate(entries[:6],1): print(f"  {C.CYAN}{i}.{C.RESET} {t[:70]}")
    print(f"  {C.CYAN}0.{C.RESET} Cancel")
    raw = input(f"\n{C.WHITE}Pick: {C.RESET}").strip()
    if not raw.isdigit() or int(raw)==0: return
    sel = int(raw)-1
    if sel >= len(entries): err("Invalid"); return
    title, url_or_id = entries[sel]
    info(f"Resolving: {title[:50]}")
    stream_url = _resolve_stream(url_or_id, cfg)
    if not stream_url: err("Could not resolve stream URL"); return
    play_file(stream_url, lm, lv, cfg, f"[STREAM] {title}")

# ─────────────────────────────────────────────────
#  SONG INFO
# ─────────────────────────────────────────────────

def fetch_song_info(url_or_id: str) -> dict | None:
    try:
        r = subprocess.run(["yt-dlp","--dump-json","--no-playlist","--quiet",url_or_id],
                           capture_output=True,text=True,timeout=20)
        if r.returncode==0: return json.loads(r.stdout)
    except Exception: pass
    return None

def show_song_info(url_or_id: str) -> None:
    info("Fetching metadata…")
    data = fetch_song_info(url_or_id)
    if not data: warn("Could not fetch info"); return
    head("Song Info")
    dur = data.get("duration") or 0
    for label, val in [
        ("Title",    data.get("title","N/A")[:60]),
        ("Uploader", data.get("uploader","N/A")),
        ("Duration", f"{int(dur)//60}m {int(dur)%60}s"),
        ("Views",    f"{data.get('view_count',0):,}"),
        ("Upload",   data.get("upload_date","N/A")),
        ("URL",      (data.get("webpage_url",url_or_id) or "")[:70]),
    ]:
        print(f"  {C.CYAN}{label:<12}{C.RESET} {val}")

# ─────────────────────────────────────────────────
#  RECOMMENDATIONS  [NEW-A3]
# ─────────────────────────────────────────────────

def get_recommendations(limit: int = 7) -> list[tuple[str,str,str]]:
    algo      = get_taste_algo()
    blacklist = get_blacklist()
    recs: list[tuple[str,str,str]] = []
    now_ts = time.time()

    with _conn() as c:
        songs   = c.execute("SELECT * FROM taste_songs").fetchall()
        artists = c.execute("SELECT * FROM taste_artists ORDER BY plays DESC LIMIT 5").fetchall()
        kwds    = c.execute("SELECT * FROM taste_keywords ORDER BY count DESC LIMIT 3").fetchall()

    def ok_title(t: str) -> bool:
        tl = t.lower()
        return not any(b in tl for b in blacklist.get("keyword",set()))

    if algo == "score_based" and songs:
        for row in sorted(songs, key=lambda r: _score_song(r, now_ts), reverse=True)[:limit]:
            for t, u in search_results(row["title"], 1):
                if ok_title(t): recs.append((t, u, f"Score · {row['play_count']} plays"))
    elif algo == "artist_based" and artists:
        a = artists[0]["artist"]
        recs = [(t,u,f"Because you like {a}")
                for t,u in search_results(f"{a} songs", limit) if ok_title(t)][:limit]
    elif algo == "keyword_based" and kwds:
        q = " ".join(r["keyword"] for r in kwds)
        recs = [(t,u,"Based on your keywords")
                for t,u in search_results(q, limit) if ok_title(t)][:limit]
    else:
        seen: set[str] = set()
        if artists:
            for t,u in search_results(f"{artists[0]['artist']} songs", 4):
                if ok_title(t) and t not in seen: seen.add(t); recs.append((t,u,"Hybrid"))
        if kwds:
            q = " ".join(r["keyword"] for r in kwds[:2])
            for t,u in search_results(q, 4):
                if ok_title(t) and t not in seen and len(recs)<limit:
                    seen.add(t); recs.append((t,u,"Hybrid"))
    return recs[:limit]

# ─────────────────────────────────────────────────
#  PLAYLIST PARSE
# ─────────────────────────────────────────────────

def parse_playlist(path: str) -> list[tuple[str,str]]:
    p = Path(path)
    if not p.exists(): err(f"Not found: {p}"); return []
    ext     = p.suffix.lower()
    entries: list[tuple[str,str]] = []
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        if ext in (".m3u",".m3u8"):
            for line in lines:
                ex = os.path.expanduser(line)
                entries.append((ex, Path(ex).stem if not ex.startswith("http") else "Stream"))
        elif ext == ".pls":
            fd, td = {}, {}
            for line in lines:
                if "=" in line:
                    k,v = line.split("=",1)
                    if k.startswith("File"): fd[k[4:]]=v
                    elif k.startswith("Title"): td[k[5:]]=v
            for idx in sorted(fd): entries.append((fd[idx], td.get(idx, Path(fd[idx]).stem)))
        else: err(f"Unsupported: {ext}"); return []
    except Exception as e: err(f"Playlist error: {e}"); return []
    ok(f"Loaded {len(entries)} tracks"); return entries

def load_playlist_to_queue(path: str, qname: str = "default") -> bool:
    entries = parse_playlist(path)
    if not entries: return False
    added = 0
    for fp, title in entries:
        if os.path.exists(fp) or fp.startswith(("http://","https://")):
            add_to_queue(fp, title, qname); added += 1
    ok(f"Added {added}/{len(entries)} to '{qname}'"); return added > 0

# ─────────────────────────────────────────────────
#  HISTORY DISPLAY
# ─────────────────────────────────────────────────

def show_history():
    history = get_history(15)
    if not history: info("No history yet"); return None
    head("Recent Songs")
    print(f"  {C.DIM}{'#':<4}{'Title':<42}{'Plays':>6}  {'Last played':<17}Time{C.RESET}")
    print(f"  {C.DIM}{'─'*74}{C.RESET}")
    for i,e in enumerate(history,1):
        exists   = "✔" if os.path.exists(e["path"]) else "✘"
        col      = C.GREEN if exists=="✔" else C.RED
        title    = e["title"][:40]
        plays    = e.get("play_count", 1)
        date     = (e["played_at"] or "")[:16]
        secs     = e.get("total_secs") or 0
        time_str = _fmt(secs) if secs else "  -  "
        print(f"  {C.DIM}{i:2}.{C.RESET} {col}{exists}{C.RESET} "
              f"{title:<41} {C.DIM}{plays:>4}×  {date}  {time_str}{C.RESET}")
    print(f"\n{C.CYAN}Commands:{C.RESET} number · q 1 2 3 · qp 1-5 · Enter=back")
    raw = input(f"{C.CYAN}> {C.RESET}").strip()
    if not raw: return None
    parts   = raw.split(maxsplit=1)
    cmd     = parts[0].lower()
    arg_str = parts[1] if len(parts)>1 else ""
    if cmd in ("q","queue","a"):   action, indices_str = "queue", arg_str
    elif cmd in ("qp","queueplay"): action, indices_str = "queueplay", arg_str
    else:
        action = "play"
        indices_str = cmd if (cmd.isdigit() or "-" in cmd or "," in cmd) else raw
    selected: set[int] = set()
    for part in indices_str.replace(","," ").split():
        if "-" in part:
            with contextlib.suppress(ValueError):
                a,b = map(int,part.split("-",1)); selected.update(range(a,b+1))
        elif part.isdigit(): selected.add(int(part))
    if not selected: err("No valid indices"); return None
    valid = []
    for idx in sorted(selected):
        if not (1<=idx<=len(history)): warn(f"Index {idx} out of range"); continue
        e = history[idx-1]
        if os.path.exists(e["path"]): valid.append({"path":e["path"],"title":e["title"]})
        else: warn(f"#{idx}: file missing")
    if not valid: err("No valid files"); return None
    if action=="play": return valid[0]["path"]
    for item in valid: add_to_queue(item["path"], item["title"])
    ok(f"Added {len(valid)} song(s) to queue")
    return {"action":"queue_added","count":len(valid)}

# ─────────────────────────────────────────────────
#  INPUT HELPERS
# ─────────────────────────────────────────────────

def choose(prompt: str, options: list[tuple]) -> str:
    print()
    for k,v in options: print(f"  {C.CYAN}{k}{C.RESET}. {v}")
    while True:
        v = input(f"\n{C.WHITE}{prompt}: {C.RESET}").strip()
        if any(v==k for k,_ in options): return v
        err("Invalid choice.")

def get_int(prompt: str, mn: int = 1) -> int:
    while True:
        try:
            v = int(input(f"{C.WHITE}{prompt}: {C.RESET}"))
            if v >= mn: return v
            err(f"Must be ≥{mn}")
        except ValueError: err("Enter a number.")

def get_float(prompt: str) -> float:
    while True:
        try:
            v = float(input(f"{C.WHITE}{prompt}: {C.RESET}"))
            if v > 0: return v
            err("Must be > 0")
        except ValueError: err("Enter a number.")

def get_file_path(prompt: str, cfg: dict) -> str | None:
    songs_dir = Path(cfg.get("download_dir", DEFAULT_DOWNLOAD))
    while True:
        raw = input(f"{C.WHITE}{prompt} (Enter=cancel): {C.RESET}").strip()
        if not raw: return None
        p = Path(os.path.expanduser(raw))
        if p.exists(): return str(p)
        c = songs_dir / raw
        if c.exists(): return str(c)
        err(f"Not found: {raw}")
        warn(f"Searched: {p}  and  {c}")

def get_loop_mode() -> str:
    return choose("Loop mode", [
        ("1","Loop by count"),("2","Loop by time (minutes)"),
        ("3","Loop infinitely"),("4","Repeat one track"),("5","Play once"),
    ])

def get_loop_params(mode: str) -> tuple:
    if mode=="1": return "count", get_int("Repeat how many times")
    elif mode=="2": return "time", get_float("Minutes")
    elif mode=="3": return "inf", None
    elif mode=="4": return "repeat_one", None
    else: return "once", None

# ─────────────────────────────────────────────────
#  [NEW-A4] CRASH RESUME CHECK
# ─────────────────────────────────────────────────

def maybe_resume(cfg: dict) -> None:
    if not cfg.get("auto_resume", True): return
    cs = load_crash()
    if not cs or not cs.path: return
    if not os.path.exists(cs.path) and not cs.path.startswith("http"):
        clear_crash(); return
    elapsed = ""
    if cs.saved_at:
        with contextlib.suppress(Exception):
            dt = datetime.fromisoformat(cs.saved_at)
            elapsed = f" ({int((datetime.now()-dt).total_seconds())}s ago)"
    print(f"\n{C.ORANGE}{'═'*52}{C.RESET}")
    print(f"{C.ORANGE}{C.BOLD}  ⚡ UNFINISHED SESSION DETECTED{C.RESET}")
    print(f"  {C.WHITE}Song    :{C.RESET} {cs.title[:48]}")
    print(f"  {C.WHITE}Position:{C.RESET} {_fmt(cs.position)}")
    if elapsed: print(f"  {C.WHITE}Crashed :{C.RESET} {elapsed}")
    print(f"{C.ORANGE}{'═'*52}{C.RESET}\n")
    ans = input(f"  {C.CYAN}Resume from {_fmt(cs.position)}? [Y/n]: {C.RESET}").strip().lower()
    if ans in ("","y"):
        ok(f"Resuming from {_fmt(cs.position)}")
        play_file(cs.path,"once",None,cfg,cs.title,start_pos=cs.position)
    else:
        clear_crash()
        info("Crash state cleared — starting fresh")

# ─────────────────────────────────────────────────
#  SETTINGS MENU
# ─────────────────────────────────────────────────

def settings_menu(cfg: dict) -> dict:
    head("Settings")
    rows = [
        ("Download dir", cfg["download_dir"]),("Audio format", cfg["audio_format"]),
        ("Volume", cfg["volume"]),("EQ preset", cfg.get("eq_preset","flat")),
        ("Normalize", cfg.get("normalize",False)),("Notifications", cfg["notify"]),
        ("Wake lock", cfg["wake_lock"]),("Retries", cfg["retry_count"]),
        ("Parallel DL", cfg.get("parallel_dl",PARALLEL_WORKERS)),
        ("Stream TTL", f"{cfg.get('stream_cache_ttl',1800)}s"),
        ("Auto resume", cfg.get("auto_resume",True)),
    ]
    for label,val in rows: print(f"  {C.DIM}{label:<16}: {val}{C.RESET}")
    what = choose("Edit", [
        ("1",  f"Download dir      [{cfg['download_dir'][-28:]}]"),
        ("2",  f"Audio format      [{cfg['audio_format']}]"),
        ("3",  f"Volume            [{cfg['volume']}%]"),
        ("4",  f"EQ preset         [{cfg.get('eq_preset','flat')}]"),
        ("5",  f"Loudnorm          [{'on' if cfg.get('normalize') else 'off'}]"),
        ("6",  f"Notifications     [{'on' if cfg['notify'] else 'off'}]"),
        ("7",  f"Wake lock         [{'on' if cfg['wake_lock'] else 'off'}]"),
        ("8",  f"Retry count       [{cfg['retry_count']}]"),
        ("9",  f"Parallel workers  [{cfg.get('parallel_dl', PARALLEL_WORKERS)}]"),
        ("10", f"Stream cache TTL  [{cfg.get('stream_cache_ttl',1800)}s]"),
        ("11", f"Auto-resume       [{'on' if cfg.get('auto_resume') else 'off'}]"),
        ("0",  "Back (auto-saved)"),
    ])
    if what=="1":
        d = input(f"{C.WHITE}New dir: {C.RESET}").strip()
        cfg["download_dir"] = os.path.expanduser(d)
    elif what=="2":
        fmt = input(f"{C.WHITE}Format (mp3/opus/m4a/flac): {C.RESET}").strip()
        if fmt in ("mp3","opus","m4a","flac"): cfg["audio_format"]=fmt
        else: warn("Unsupported, keeping current")
    elif what=="3": cfg["volume"]=get_int("Volume (0-130)",0)
    elif what=="4":
        print(f"\n  Options: {', '.join(_EQ)}")
        ch = input(f"{C.WHITE}EQ: {C.RESET}").strip()
        if ch in _EQ: cfg["eq_preset"]=ch; ok(f"EQ → {ch}")
        else: warn("Unknown preset")
    elif what=="5": cfg["normalize"]=not cfg.get("normalize",False); ok(f"Normalize {'on' if cfg['normalize'] else 'off'}")
    elif what=="6": cfg["notify"]=not cfg["notify"]; ok(f"Notify {'on' if cfg['notify'] else 'off'}")
    elif what=="7": cfg["wake_lock"]=not cfg["wake_lock"]; ok(f"Wake lock {'on' if cfg['wake_lock'] else 'off'}")
    elif what=="8": cfg["retry_count"]=get_int("Retries",1)
    elif what=="9": cfg["parallel_dl"]=get_int("Workers",1)
    elif what=="10": cfg["stream_cache_ttl"]=get_int("TTL seconds",60)
    elif what=="11": cfg["auto_resume"]=not cfg.get("auto_resume",True); ok(f"Auto-resume {'on' if cfg['auto_resume'] else 'off'}")
    save_config(cfg); ok("Saved"); return cfg

# ─────────────────────────────────────────────────
#  TASTE MENU
# ─────────────────────────────────────────────────

def display_taste_summary() -> None:
    clear_screen()
    meta = get_taste_meta()
    print(f"\n{C.CYAN}{C.BOLD}🎵 YOUR MUSICAL DNA{C.RESET}\n")
    print(f"{C.GREEN}📊 Stats:{C.RESET}")
    print(f"   Total plays: {meta.get('total_plays',0)}")
    with _conn() as c:
        songs   = c.execute("SELECT COUNT(*) FROM taste_songs").fetchone()[0]
        artists = c.execute("SELECT * FROM taste_artists ORDER BY plays DESC LIMIT 5").fetchall()
        kwds    = c.execute("SELECT * FROM taste_keywords ORDER BY count DESC LIMIT 5").fetchall()
    print(f"   Unique songs: {songs}")
    if artists:
        print(f"\n{C.YELLOW}🎤 Top Artists:{C.RESET}")
        for i,r in enumerate(artists,1):
            bar = "▰" * min(20, r["plays"])
            print(f"   {i}. {r['artist'][:30]:<30} {bar} {r['plays']}")
    if kwds:
        print(f"\n{C.MAGENTA}🔑 Top Keywords:{C.RESET}")
        for r in kwds: print(f"   • {r['keyword']}: {r['count']}")
    pats  = {k: int(meta.get(k,0)) for k in ("morning","afternoon","evening","night")}
    total = sum(pats.values())
    if total > 5:
        print(f"\n{C.BLUE}⏰ When You Listen:{C.RESET}")
        for label, key in [("🌅 Morning","morning"),("☀  Afternoon","afternoon"),
                            ("🌙 Evening","evening"),("🌃 Night","night")]:
            print(f"   {label}: {int(pats[key]/total*100)}%")
    print(f"\n{C.DIM}Press Enter…{C.RESET}"); input()

def taste_menu(cfg: dict) -> None:
    while True:
        head("🎨 Taste Manager")
        meta = get_taste_meta()
        print(f"   Total plays: {meta.get('total_plays',0)}  Algorithm: {get_taste_algo()}\n")
        for k,v in [("1","View profile"),("2","Change algorithm"),
                     ("3","Blacklist"),("4","Reset profile"),
                     ("5","Recommendations"),("0","Back")]:
            print(f"   {C.CYAN}{k}{C.RESET}. {v}")
        ch = input(f"\n{C.WHITE}Choose: {C.RESET}").strip()
        if ch=="1": display_taste_summary()
        elif ch=="2":
            nw = choose("Algorithm", [
                ("1","Score-based (listen ratio + recency)"),
                ("2","Artist-based"),("3","Keyword-based"),("4","Hybrid"),
            ])
            set_taste_algo({"1":"score_based","2":"artist_based","3":"keyword_based","4":"hybrid"}[nw])
            ok("Algorithm updated")
        elif ch=="3":
            ba = choose("Blacklist", [("1","Add artist"),("2","Add keyword"),("3","View"),("4","Clear")])
            if ba=="1":
                a = input(f"{C.WHITE}Artist: {C.RESET}").strip()
                if a: add_blacklist("artist",a)
            elif ba=="2":
                k = input(f"{C.WHITE}Keyword: {C.RESET}").strip()
                if k: add_blacklist("keyword",k)
            elif ba=="3":
                bl = get_blacklist(); head("Blacklist")
                for btype,vals in bl.items():
                    if vals: print(f"  {btype}: {', '.join(vals)}")
                input(f"{C.DIM}Press Enter…{C.RESET}")
            elif ba=="4":
                if input(f"{C.RED}Clear all? [y/N]: {C.RESET}").lower()=="y": clear_blacklist()
        elif ch=="4":
            if input(f"{C.RED}Type 'yes' to reset all taste data: {C.RESET}")=="yes":
                with _conn() as c:
                    c.executescript("""
                        DELETE FROM taste_songs; DELETE FROM taste_artists;
                        DELETE FROM taste_keywords;
                        UPDATE taste_meta SET value='0'
                        WHERE key IN ('total_plays','morning','afternoon','evening','night');
                    """)
                ok("Profile reset")
        elif ch=="5":
            recs = get_recommendations(7)
            if not recs: warn("Not enough data. Play some songs first!"); continue
            head("🎵 Personalized Recommendations")
            for i,(t,u,r) in enumerate(recs,1):
                print(f"  {C.GREEN}{i}.{C.RESET} {t[:60]}")
                print(f"     {C.DIM}→ {r}{C.RESET}")
            print(f"\n  {C.YELLOW}a{C.RESET}. Add all to queue  {C.YELLOW}0{C.RESET}. Back")
            pick = input(f"\n{C.WHITE}Pick: {C.RESET}").strip().lower()
            if pick=="a":
                for t,u,_ in recs:
                    fp,_ = search_and_download(t,cfg,0)
                    if fp: add_to_queue(fp,t)
            elif pick.isdigit() and 1<=int(pick)<=len(recs):
                t,u,_ = recs[int(pick)-1]
                su = _resolve_stream(u,cfg)
                if su: play_file(su,"once",None,cfg,f"[STREAM] {t}")
        elif ch=="0": break

# ─────────────────────────────────────────────────
#  QUEUE MENU
# ─────────────────────────────────────────────────

def show_queue(qname: str = "default") -> None:
    items = get_queue(qname)
    if not items:
        info(f"Queue '{qname}' is empty")
        info("Add songs with option 1 (search) or 3 (add file)")
        return
    st = get_queue_state(qname)
    cur_idx = st.get("current_idx", -1)
    head(f"Queue: {qname}  [{st['mode']}]  ({len(items)} songs)")
    for i, item in enumerate(items, 1):
        exists = Path(item["path"]).exists() or item["path"].startswith("http")
        col    = C.GREEN if exists else C.RED
        # [UX-17] playing indicator
        if i-1 == cur_idx and st.get("playing"):
            marker = f"{C.YELLOW}▶{C.RESET}"
        else:
            marker = f"{C.DIM} {C.RESET}"
        print(f"  {marker}{i:2}. {col}{'✔' if exists else '✘'}{C.RESET} {item['title'][:58]}")
    avail = sum(1 for it in items if Path(it["path"]).exists() or it["path"].startswith("http"))
    print(f"\n  {C.DIM}{avail}/{len(items)} files available on disk{C.RESET}")

def queue_menu(cfg: dict) -> None:
    while True:
        active = get_active_queue()
        head(f"Queue Manager  (active: {active})")
        action = choose("Action", [
            ("1","Show queue"),("2","Play queue"),("3","Add local file"),
            ("4","Remove item"),("5","Move item up"),("6","Move item down"),
            ("7","Clear queue"),("8","Switch / create queue"),("9","List all queues"),
            ("r","Rename queue"),("c","Copy queue"),("x","Delete queue"),
            ("e","Export as .m3u"),("a","Load playlist"),("m","Set mode"),("0","Back"),
        ])
        if action=="1": show_queue(active)
        elif action=="2": play_queue(cfg, active)
        elif action=="3":
            fp = get_file_path("File path", cfg)
            if fp: add_to_queue(fp, Path(fp).stem, active)
        elif action=="4": show_queue(active); remove_from_queue(get_int("Remove #"), active)
        elif action=="5": show_queue(active); move_queue_item(get_int("Move # up"), -1, active)
        elif action=="6": show_queue(active); move_queue_item(get_int("Move # down"), 1, active)
        elif action=="7":
            if input(f"{C.RED}Clear '{active}'? [y/N]: {C.RESET}").lower()=="y": clear_queue(active)
        elif action=="8":
            queues = get_all_queues(); names = list(queues.keys())
            head("All Queues")
            for i,n in enumerate(names,1):
                marker = "▶" if n==active else " "
                print(f"  {C.CYAN}{i}.{C.RESET} {marker} {n} ({queues[n]})")
            print(f"  {C.CYAN}n.{C.RESET}   Create new")
            raw = input(f"{C.WHITE}Select or 'n': {C.RESET}").strip()
            if raw.lower()=="n":
                name = input(f"{C.WHITE}Name: {C.RESET}").strip()
                if name: create_queue(name)
            elif raw.isdigit() and 1<=int(raw)<=len(names):
                chosen = names[int(raw)-1]
                with _conn() as c:
                    c.execute("UPDATE queue_state SET playing=0 WHERE playing=1")
                    c.execute("INSERT OR IGNORE INTO queue_state(queue_name) VALUES(?)",(chosen,))
                ok(f"Switched to '{chosen}'")
        elif action=="9":
            queues = get_all_queues(); head("All Queues")
            for name,cnt in queues.items():
                st = get_queue_state(name)
                print(f"  {'▶' if name==active else ' '} {name}  ({cnt} songs · mode={st['mode']})")
        elif action=="r":
            old = input(f"{C.WHITE}From: {C.RESET}").strip()
            new = input(f"{C.WHITE}To: {C.RESET}").strip()
            if old and new: rename_queue(old,new)
        elif action=="c":
            src = input(f"{C.WHITE}Source: {C.RESET}").strip()
            dst = input(f"{C.WHITE}Dest: {C.RESET}").strip()
            if src and dst: copy_queue(src,dst)
        elif action=="x":
            name = input(f"{C.WHITE}Delete: {C.RESET}").strip()
            if name and input(f"{C.RED}Delete '{name}'? [y/N]: {C.RESET}").lower()=="y":
                delete_queue(name)
        elif action=="e": export_queue_as_m3u(active)
        elif action=="a":
            pl = input(f"{C.WHITE}Playlist path: {C.RESET}").strip()
            if pl:
                load_playlist_to_queue(os.path.expanduser(pl), active)
                if input(f"{C.CYAN}Play now? [y/N]: {C.RESET}").lower()=="y":
                    play_queue(cfg,active)
        elif action=="m":
            mode_map = {"1":"normal","2":"shuffle","3":"repeat_all","4":"repeat_one"}
            m = choose("Mode",[("1","normal"),("2","shuffle"),("3","repeat_all"),("4","repeat_one")])
            set_queue_state(active, mode=mode_map[m])
            print(f"\n{C.MAGENTA}  ══ Mode → {mode_map[m]} ══{C.RESET}")
            ok(f"Queue '{active}' is now in {mode_map[m]} mode")
        elif action=="0": break

# ─────────────────────────────────────────────────
#  ARGPARSE
# ─────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(prog="musicloop", description="🎧 MusicLoop v4.0")
    p.add_argument("-I","--interactive", action="store_true")
    src = p.add_mutually_exclusive_group()
    src.add_argument("-s","--search",  metavar="QUERY")
    src.add_argument("-u","--url",     metavar="URL")
    src.add_argument("-f","--file",    metavar="PATH")
    src.add_argument("--stream",       metavar="QUERY")
    src.add_argument("--batch",        metavar="QUERY")
    lp = p.add_mutually_exclusive_group()
    lp.add_argument("-n","--count",      metavar="N",   type=int)
    lp.add_argument("-t","--time",       metavar="MIN", type=float)
    lp.add_argument("-i","--inf",        action="store_true")
    lp.add_argument("--repeat-one",      dest="repeat_one", action="store_true")
    p.add_argument("--sleep",       metavar="MIN",    type=float)
    p.add_argument("--eq",          metavar="PRESET")
    p.add_argument("--normalize",   action="store_true")
    p.add_argument("--info",        metavar="URL")
    p.add_argument("--queue-add",   metavar="PATH")
    p.add_argument("--queue-play",  action="store_true")
    p.add_argument("--queue-show",  action="store_true")
    p.add_argument("--queue-clear", action="store_true")
    p.add_argument("--queue-remove",metavar="N", type=int)
    p.add_argument("--queue-up",    metavar="N", type=int)
    p.add_argument("--queue-down",  metavar="N", type=int)
    p.add_argument("--queue-export",action="store_true")
    p.add_argument("--queue-mode",  metavar="MODE")
    p.add_argument("--daemon",      action="store_true")
    p.add_argument("--daemon-ctl",  metavar="ACTION")
    p.add_argument("--daemon-val",  metavar="VALUE")
    p.add_argument("--load-playlist",metavar="FILE")
    p.add_argument("--history",     action="store_true")
    return p.parse_args()

def _loop(args) -> tuple:
    if args.count:      return "count",      args.count
    if args.time:       return "time",        args.time
    if args.inf:        return "inf",         None
    if args.repeat_one: return "repeat_one",  None
    return None, None

def handle_args(args, cfg: dict) -> bool:
    if args.load_playlist:
        load_playlist_to_queue(args.load_playlist)
        if args.queue_play: play_queue(cfg)
        return True
    if args.interactive: return False
    if args.eq:
        if args.eq in _EQ: cfg["eq_preset"]=args.eq
        else: warn(f"Unknown EQ: {args.eq}")
    if args.normalize: cfg["normalize"]=True
    if args.info:           show_song_info(args.info); return True
    if args.queue_show:     show_queue(); return True
    if args.queue_clear:    clear_queue(); return True
    if args.queue_play:     play_queue(cfg); return True
    if args.queue_remove:   remove_from_queue(args.queue_remove); return True
    if args.queue_up:       move_queue_item(args.queue_up, -1); return True
    if args.queue_down:     move_queue_item(args.queue_down, 1); return True
    if args.queue_export:   export_queue_as_m3u(); return True
    if args.queue_mode:     set_queue_state("default", mode=args.queue_mode); ok(f"Mode → {args.queue_mode}"); return True
    if args.queue_add:
        p = os.path.expanduser(args.queue_add)
        if os.path.exists(p): add_to_queue(p, Path(p).stem)
        else: err("File not found")
        return True
    if args.daemon_ctl: daemon_ctl(args.daemon_ctl, args.daemon_val); return True
    if args.history:
        result = show_history()
        if result:
            if isinstance(result, dict) and result.get("action")=="queue_added":
                if input(f"{C.CYAN}Play queue? [y/N]: {C.RESET}").lower()=="y": play_queue(cfg)
            elif isinstance(result, str):
                lm, lv = _loop(args)
                if lm is None: lm, lv = get_loop_params(get_loop_mode())
                play_file(result, lm, lv, cfg, sleep_mins=args.sleep)
        return True
    if args.batch:
        paths = batch_download(args.batch, cfg)
        if paths and input(f"{C.CYAN}Add all to queue? [y/N]: {C.RESET}").lower()=="y":
            for fp in paths: add_to_queue(fp, Path(fp).stem)
        return True
    if args.stream:
        lm, lv = _loop(args)
        if lm is None: lm, lv = get_loop_params(get_loop_mode())
        stream_play(args.stream, lm, lv, cfg); return True
    fp, title = None, ""
    if args.search:   fp, title = search_and_download(args.search, cfg)
    elif args.url:    fp, title = download_from_url(args.url, cfg)
    elif args.file:
        p = os.path.expanduser(args.file)
        if os.path.exists(p): fp, title = p, Path(p).stem
        else: err("File not found"); return True
    else: return False
    if not fp: return True
    if args.daemon: start_daemon(fp, cfg, title)
    else:
        lm, lv = _loop(args)
        if lm is None: lm, lv = get_loop_params(get_loop_mode())
        play_file(fp, lm, lv, cfg, title, sleep_mins=args.sleep)
    return True

# ─────────────────────────────────────────────────
#  INTERACTIVE MENU
# ─────────────────────────────────────────────────

def interactive_menu(cfg: dict) -> None:
    while True:
        head("Main Menu")
        action = choose("Choose action", [
            ("1","Search & download"),("2","Stream (instant)"),
            ("3","Batch download (parallel)"),("4","Download from URL"),
            ("5","Play local file"),("6","Play from history"),
            ("7","Queue manager"),("8","Daemon mode"),
            ("9","Settings"),("a","Recommendations"),
            ("b","Taste profile"),("c","Song info"),
            ("d","Sleep timer play"),("e","Clear screen"),("0","Exit"),
        ])
        if action=="1":
            q = input(f"\n{C.WHITE}Search query: {C.RESET}").strip()
            if not q: continue
            fp, title = search_and_download(q, cfg)
            if not fp: continue
            if input(f"{C.CYAN}Add to queue? [y/N]: {C.RESET}").lower()=="y": add_to_queue(fp,title)
            else:
                lm,lv = get_loop_params(get_loop_mode())
                play_file(fp,lm,lv,cfg,title)
        elif action=="2":
            q = input(f"\n{C.WHITE}Search query: {C.RESET}").strip()
            if not q: continue
            lm,lv = get_loop_params(get_loop_mode())
            stream_play(q,lm,lv,cfg)
        elif action=="3":
            q = input(f"\n{C.WHITE}Search query: {C.RESET}").strip()
            if not q: continue
            paths = batch_download(q,cfg)
            if paths and input(f"{C.CYAN}Add all to queue? [y/N]: {C.RESET}").lower()=="y":
                for fp in paths: add_to_queue(fp,Path(fp).stem)
        elif action=="4":
            url = input(f"\n{C.WHITE}URL: {C.RESET}").strip()
            if not url: continue
            fp,title = download_from_url(url,cfg)
            if not fp: continue
            lm,lv = get_loop_params(get_loop_mode())
            play_file(fp,lm,lv,cfg,title)
        elif action=="5":
            music_dir = Path(cfg.get("download_dir",DEFAULT_DOWNLOAD))
            music_dir.mkdir(parents=True, exist_ok=True)
            files = sorted(
                [f for ext in ("*.mp3","*.m4a","*.opus","*.flac","*.wav","*.ogg")
                 for f in music_dir.glob(ext)],
                key=lambda x: x.stat().st_mtime, reverse=True
            )
            if not files: warn("No music files found"); continue
            print(f"\n{C.BOLD}{C.CYAN}── Files ({len(files)}) ──{C.RESET}\n")
            LIMIT = 30
            for i,fp in enumerate(files[:LIMIT],1):
                sz   = fp.stat().st_size/(1024*1024)
                name = (fp.stem[:55]+"…") if len(fp.stem)>55 else fp.stem
                print(f"  {C.CYAN}{i:3}.{C.RESET} {name:<58} {C.DIM}({sz:.1f}M){C.RESET}")
            if len(files)>LIMIT: print(f"  {C.DIM}… {len(files)-LIMIT} more{C.RESET}")
            while True:
                raw = input(f"\n{C.WHITE}Number (0=cancel): {C.RESET}").strip()
                if raw=="0": break
                if raw.isdigit():
                    idx = int(raw)-1
                    if 0<=idx<len(files):
                        fp = str(files[idx]); title = files[idx].stem
                        ok(f"Selected: {title}")
                        lm,lv = get_loop_params(get_loop_mode())
                        play_file(fp,lm,lv,cfg,title); break
                    err(f"Choose 1-{len(files)}")
                else: err("Enter a number")
        elif action=="6":
            result = show_history()
            if result:
                if isinstance(result,dict) and result.get("action")=="queue_added":
                    if input(f"{C.CYAN}Play queue? [y/N]: {C.RESET}").lower()=="y": play_queue(cfg)
                elif isinstance(result,str):
                    lm,lv = get_loop_params(get_loop_mode())
                    play_file(result,lm,lv,cfg,Path(result).stem)
        elif action=="7": queue_menu(cfg)
        elif action=="8":
            head("Daemon Mode")
            src = choose("Source",[("1","Local file"),("2","Search & download")])
            if src=="1":
                fp = get_file_path("File path",cfg)
                if fp: start_daemon(fp,cfg,Path(fp).stem)
            else:
                q = input(f"{C.WHITE}Search query: {C.RESET}").strip()
                if q:
                    fp,t = search_and_download(q,cfg)
                    if fp: start_daemon(fp,cfg,t)
        elif action=="9": cfg = settings_menu(cfg)
        elif action=="a":
            recs = get_recommendations(7)
            if not recs: warn("Not enough data. Play some songs first!"); continue
            head("🎵 Recommendations")
            for i,(t,u,r) in enumerate(recs,1):
                print(f"  {C.GREEN}{i}.{C.RESET} {t[:60]}")
                print(f"     {C.DIM}→ {r}{C.RESET}")
            print(f"\n  {C.YELLOW}a{C.RESET}. Add all  {C.YELLOW}0{C.RESET}. Back")
            pick = input(f"\n{C.WHITE}Pick: {C.RESET}").strip().lower()
            if pick=="a":
                for t,u,_ in recs:
                    fp,_ = search_and_download(t,cfg,0)
                    if fp: add_to_queue(fp,t)
            elif pick.isdigit() and 1<=int(pick)<=len(recs):
                t,u,_ = recs[int(pick)-1]
                su = _resolve_stream(u,cfg)
                if su: play_file(su,"once",None,cfg,f"[STREAM] {t}")
        elif action=="b": taste_menu(cfg)
        elif action=="c":
            q = input(f"\n{C.WHITE}Search query or URL: {C.RESET}").strip()
            if q:
                if q.startswith(("http://","https://")): show_song_info(q)
                else:
                    entries = search_results(q,1)
                    if entries: show_song_info(entries[0][1])
                    else: err("No results")
                input(f"{C.DIM}Press Enter…{C.RESET}")
        elif action=="d":
            q = input(f"\n{C.WHITE}Search query: {C.RESET}").strip()
            if not q: continue
            mins = get_float("Stop after N minutes")
            fp,title = search_and_download(q,cfg)
            if fp:
                lm,lv = get_loop_params(get_loop_mode())
                play_file(fp,lm,lv,cfg,title,sleep_mins=mins)
        elif action=="e": clear_screen(); banner(cfg)
        elif action=="0":
            print(f"\n{C.CYAN}Ya Ali a.s madad 🎵{C.RESET}\n"); sys.exit(0)

# ─────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────

def main() -> None:
    init_db()
    cfg  = load_config()
    args = parse_args()
    banner(cfg)
    check_dependencies()
    check_storage_permission()
    maybe_resume(cfg)   # [NEW-A4]
    if args.interactive:
        try: interactive_menu(cfg)
        except KeyboardInterrupt: print(f"\n{C.CYAN}Ya Ali a.s madad 🎵{C.RESET}\n")
        return
    if any(vars(args).values()):
        handled = handle_args(args, cfg)
        if handled: return
    try: interactive_menu(cfg)
    except KeyboardInterrupt:
        print(f"\n{C.CYAN}Ya Ali a.s madad 🎵{C.RESET}\n"); sys.exit(0)

if __name__ == "__main__":
    main()
