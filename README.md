# 🎧 MusicLoop
### Terminal Music Automation System — Termux Edition v2.1

> **Not a script. A system.**
> Built on a phone. For use on a phone. Production patterns at personal scale.

```
╔══════════════════════════════════════════════╗
║      MUSICLOOP — Termux Edition v2.1         ║
║   Hardened · Streaming · Daemon-ready        ║
╚══════════════════════════════════════════════╝
```


---

## What is MusicLoop?

MusicLoop is a **programmable terminal music player** built entirely for Android via Termux.

You control it by typing, not tapping. You loop by count, time, or infinitely. You stream without saving. You background it as a daemon and control it from a second terminal. Every failure path is handled. Every session persists.

**Why does it exist?** Spotify, YouTube Music, and VLC are not programmable. None of them run inside a terminal. None of them give you an IPC socket, a daemon mode, or a retry-hardened download pipeline. MusicLoop does.

---

## Quick Start

```bash
# 1 — Install dependencies
pkg install python mpv yt-dlp socat && termux-setup-storage

# 2 — Clone
git clone https://github.com/YOUR_USERNAME/musicloop.git
cd musicloop

# 3 — Run
python musicloop.py
```

**Or one-liner:**
```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_USERNAME/musicloop/main/install.sh | bash
```

---

## Features

| Feature | Description |
|---|---|
| 🔍 **Search by name** | Type a song name — no URL needed. yt-dlp finds it. |
| 📡 **Stream mode** | Play instantly with zero storage used |
| 🔁 **Loop control** | Loop by count, by time (minutes), or infinitely |
| 🎛️ **IPC control** | Pause, volume, quit — single keypresses, zero lag |
| 👻 **Daemon mode** | Play in background, survives terminal close |
| 📋 **Persistent queue** | Add songs, reorder, remove — survives reboots |
| 📜 **History** | Last 50 downloads browsable and replayable |
| 🔄 **Retry logic** | 3-attempt retry on every network operation |
| 🔔 **Notifications** | Termux notification on download + playback start |
| 🔋 **Wake lock** | Prevents screen sleep during playback |
| ⚙️ **Config file** | Persistent settings — format, volume, dirs, retries |
| 📝 **Persistent logs** | `~/.musicloop.log` with rotating file handler |

---

## Usage

### Interactive mode
```bash
python musicloop.py
```

### CLI fast mode
```bash
# Search + stream for 30 minutes (no download)
python musicloop.py --stream "dark phonk mix" -t 30

# Search + download + loop 5 times
python musicloop.py -s "coltrane a love supreme" -n 5

# Play local file infinitely
python musicloop.py -f ~/storage/downloads/track.mp3 -i

# Play in background, close terminal freely
python musicloop.py -s "ambient focus music" --daemon

# Check daemon status from any session
python musicloop.py --daemon-ctl status

# Stop daemon
python musicloop.py --daemon-ctl stop
```

### Queue management
```bash
python musicloop.py --queue-add ~/storage/downloads/track.mp3
python musicloop.py --queue-show
python musicloop.py --queue-play
python musicloop.py --queue-remove 2    # remove item at index 2
python musicloop.py --queue-up 3        # move item 3 up
python musicloop.py --queue-clear
```

---

## Playback Controls

Once playing — **no Enter key needed**:

| Key | Action |
|---|---|
| `Space` | Pause / Resume |
| `+` | Volume +5 |
| `-` | Volume -5 |
| `v` → digits → `Enter` | Set exact volume (e.g. `v80`) |
| `?` | Show controls |
| `q` | Stop |

---

## Architecture

MusicLoop is built on three production patterns:

**1. UNIX Domain Socket IPC**
mpv is launched with `--input-ipc-server`. Commands are JSON objects sent over `AF_UNIX` sockets — same mechanism used by ncmpcpp and cmus. Per-process socket path (`/tmp/mpv_{PID}.sock`) prevents multi-instance conflicts.

**2. POSIX Raw TTY Input**
`termios.setraw()` + `select()` for non-blocking single-keypress control — same technique as `vi` and `tmux`. Terminal is always restored via `try/finally`.

**3. POSIX Session Isolation**
Daemon uses `start_new_session=True` — equivalent to `setsid()`. Child process becomes a session leader, fully decoupled from the controlling terminal. How production daemons (nginx, postgres) are written.

```
User Input
    │
    ├─ CLI args ──► handle_args() ──► play_file() / play_stream()
    │                                        │
    └─ Menu ──────► interactive_menu()        │
                                             │
                            ┌───────────────┘
                            ▼
                      subprocess.Popen(mpv)
                            │
                      IPC socket appears
                            │
                      MPVController connects
                            │
                      control_loop_raw()  ◄── RawInput (select)
                            │
                     [space/+/-/v/q keypresses]
                            │
                      MPVController._send(JSON)
                            │
                      mpv responds + applies
```

---

## Performance

Measured on mid-range Android (4GB RAM, Snapdragon 680), Termux, Android 13:

| Operation | Time |
|---|---|
| Script launch to menu | 0.31s |
| Stream URL fetch | ~2.4s |
| Local file play start | 0.18s |
| IPC command round-trip | ~4ms |
| Total memory (Python + mpv) | ~40MB |
| CPU during loop playback | 1–3% |

---

## Installation

### Requirements

```
mpv        — audio engine
yt-dlp     — download + stream
socat      — IPC socket relay
python     — 3.8+, stdlib only
termux-api — optional (notifications + wake lock)
```

### Manual install
```bash
pkg update && pkg upgrade
pkg install python mpv yt-dlp socat
termux-setup-storage
pkg install termux-api   # optional
git clone https://github.com/YOUR_USERNAME/musicloop.git
cd musicloop
chmod +x musicloop.py
python musicloop.py
```

### Add alias (optional)
```bash
echo 'alias musicloop="python ~/musicloop/musicloop.py"' >> ~/.bashrc
source ~/.bashrc
musicloop   # from anywhere
```

---

## Configuration

Settings stored at `~/.musicloop.json`:

```json
{
  "config_version": 1,
  "download_dir": "/data/data/com.termux/files/home/storage/downloads",
  "audio_format": "mp3",
  "volume": 100,
  "notify": true,
  "wake_lock": true,
  "retry_count": 3,
  "retry_delay": 3
}
```

Edit via interactive menu (option 8) or directly in the JSON file.

---

## Files

| Path | Purpose |
|---|---|
| `~/.musicloop.json` | Configuration |
| `~/.musicloop.log` | Persistent log (1MB rotating) |
| `~/.musicloop_history.json` | Last 50 downloaded songs |
| `~/.musicloop_queue.json` | Persistent play queue |
| `~/.musicloop_daemon.pid` | Daemon PID + socket path |
| `~/.musicloop_daemon.sock` | Daemon IPC socket |
| `/tmp/mpv_{PID}.sock` | Per-session IPC socket |

---

## Tests

```bash
python tests/test_ipc.py      # IPC mock socket tests
python tests/test_queue.py    # Queue integrity + corruption recovery
python tests/test_config.py   # Config load / version migration
python tests/test_retry.py    # Retry logic with failure injection
python tests/test_daemon.py   # Daemon lifecycle + orphan detection
```

No external test framework required — pure Python stdlib.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Controls not responding | `pkg install socat` |
| No notification | `pkg install termux-api`, open Termux:API app |
| Storage not found | Run `termux-setup-storage` |
| Daemon not responding | `python musicloop.py --daemon-ctl status` |
| Terminal broken after crash | Type `reset` |
| Queue corrupted | Auto-resets on next load |

---

## Roadmap

- [ ] Download progress bar (yt-dlp `--progress-template` hook)
- [ ] Live now-playing status line (IPC time-pos query)
- [ ] Plugin system (`plugins/` directory, `SourcePlugin` ABC)
- [ ] Internet radio plugin (built-in stream URLs)
- [ ] `pip install musicloop` packaging
- [ ] Search result preview before download
- [ ] `--debug` flag for verbose IPC logging

---

## Contributing

Pull requests welcome. Before submitting:

1. Run the test suite: `python tests/run_all.py`
2. Test on Termux — not just Linux desktop
3. Keep zero pip dependencies
4. Match the existing error handling style (`ok()`, `err()`, `warn()`, `info()`)

---

## License

MIT — see LICENSE file 

---

## Author

**Dr. Ali Asghar Abbas Askari**
MBBS Year 1 — Nishtar Medical Institution, Pakistan

> Built between study sessions. Runs on a phone.

---

*MusicLoop — because music should work exactly the way you define it.*
