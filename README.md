# 🎙️ SpytoRec – Spotify Track Recorder

**SpytoRec** is a powerful, CLI-based tool to record your currently playing Spotify tracks in real-time, automatically split them, embed metadata (title, artist, album, cover art), and organize them in your personal music library.

> ✅ Intended strictly for **personal archival use only**.

---

## ⚠️ Legal Disclaimer

> **This tool is for personal, non-commercial use.**
>  
> Recording copyrighted content from Spotify may violate their [Terms of Service](https://www.spotify.com/legal/end-user-agreement/) or local copyright laws.  
> You are solely responsible for your usage. The developers of this tool assume **no liability**.

---

## ✨ Features

- 🎵 Real-Time Recording (FLAC or OGG)
- 🎯 Track Splitting via Spotify API
- 🎼 Metadata Embedding: title, artist, album, and cover art
- ⚙️ Background Finalization for smooth capture
- 📁 File Organization: Automatically sort by Artist/Album
- 🛡️ Duplicate Detection & Auto-Skip via Blocklist
- 💬 Rich Terminal UI with Cross-Platform Keyboard Shortcuts
- 🔔 Webhook Notifications for session monitoring
- 🖥️ Cross-Platform: Windows, macOS, Linux
- 📦 **V8.1.0+**: Installable Python package architecture (`pip install .`)

---

## 💻 Installation Guides

SpytoRec v8.1.0+ is now an installable Python package.

### 🪟 Windows

1. Install **Python 3.8+** from [python.org](https://www.python.org/downloads/windows/)
2. Install **FFmpeg**:
   - Download from [gyan.dev FFmpeg builds](https://www.gyan.dev/ffmpeg/builds/)
   - Extract it and add the `/bin` folder to your `PATH`
3. Install **VB-Audio Cable** from [vb-audio.com](https://vb-audio.com/Cable/)
4. Clone the repo and install the package:
   ```bash
   git clone https://github.com/Danidukiyu/spytorec.git
   cd spytorec
   pip install -e .
   ```
5. Set Spotify output to **CABLE Input**, and run:
   ```bash
   spytorec
   ```

---

### 🍏 macOS

1. Install **Python 3.8+** (via [Homebrew](https://brew.sh/) or [python.org](https://www.python.org/downloads/macos/))
2. Install **FFmpeg**:
   ```bash
   brew install ffmpeg
   ```
3. Install **BlackHole (2ch)** via [BlackHole GitHub](https://github.com/ExistentialAudio/BlackHole)
4. Set Spotify output to BlackHole in System Preferences > Sound > Output
5. Clone and install the package:
   ```bash
   git clone https://github.com/Danidukiyu/spytorec.git
   cd spytorec
   pip install -e .
   ```
6. Run the tool:
   ```bash
   spytorec
   ```

---

### 🐧 Linux (PulseAudio)

1. Install **Python 3.8+**, `ffmpeg`, and `pavucontrol`:
   ```bash
   sudo apt update && sudo apt install python3 ffmpeg pavucontrol python3-pip
   ```
2. Load PulseAudio null sink:
   ```bash
   pactl load-module module-null-sink sink_name=spytorec_sink
   ```
3. Set Spotify output to **Monitor of spytorec_sink** using `pavucontrol`
4. Clone repo and install:
   ```bash
   git clone https://github.com/Danidukiyu/spytorec.git
   cd spytorec
   pip install -e .
   ```
5. Run the tool:
   ```bash
   spytorec
   ```

---

## 🎚️ Track Source

SpytoRec needs to know **what track is playing and when it changes** in order to split and tag recordings. It reads that from an **OS media source** — the local Spotify desktop app — with **no credentials or Premium** on macOS and Windows. There's nothing to configure: keep the Spotify app running and playing.

| Platform | Backend | Status |
|---|---|---|
| macOS | Spotify.app via AppleScript | ✅ Implemented — no credentials |
| Windows | SMTC (`GlobalSystemMediaTransportControls`) | ✅ Implemented — no credentials, validated on Windows 11 |
| Linux | MPRIS over D-Bus | 🚧 Planned (phase 3) — falls back to the Spotify Web API for now, which needs credentials + Premium |

> **macOS:** the first run shows a one-time system prompt — *"SpytoRec wants to control Spotify"*. Click **OK** (or approve it later under *System Settings → Privacy & Security → Automation*). The Spotify desktop app must be running. `release_date` isn't exposed by Spotify's AppleScript dictionary, so the year tag falls back to `0000`.

> **Windows:** requires the `winsdk` package (installed automatically on Windows via `pip install -e .`). The Spotify desktop app must be running **and must have played audio at least once** in the session before it appears in the system media controls. SMTC exposes no Spotify track id or release date, so track ids are synthesised from `artist + title` (blocklist `id:` entries only match within the same machine) and the year tag falls back to `0000`. Album art comes back as raw thumbnail bytes rather than a CDN url. Position updates in steps rather than ticking (SMTC reports it only as of the app's last push), so the dashboard progress figure is less smooth than on macOS. Validated end-to-end on Windows 11 (winsdk 1.0.0b10): connect, playing/paused/skip, thumbnail art, accented metadata.

### Spotify API credentials (Linux only)

Needed only on Linux, for the Web API fallback while the MPRIS source is unimplemented. macOS and Windows don't use these.

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and log in.
2. Click **Create app**. Give it any name and description.
3. In the app's **Settings**, add this exact **Redirect URI** and save:
   ```
   http://127.0.0.1:8888/callback
   ```
4. Copy the **Client ID** and **Client Secret**.
5. Edit `config.ini` (created automatically on first run — see note below) and fill in the `[SpotifyAPI]` section:
   ```ini
   [SpotifyAPI]
   spotipy_client_id = your_client_id_here
   spotipy_client_secret = your_client_secret_here
   ```
6. Run `spytorec` again. The first launch opens a browser to authorize the app (scopes: `user-read-playback-state`, `user-read-currently-playing`). A token cache is saved for subsequent runs.

> ⚠️ **`config.ini` lives in your current working directory**, not the install location. SpytoRec reads and writes `config.ini`, `spyto.lock`, `blocklist.txt`, `Recordings/`, and log files relative to wherever you launch `spytorec`. Pick one directory and always run from it, otherwise you'll end up with multiple blank configs.

---

## 🛠️ Usage & CLI

With the new package architecture, you can run SpytoRec from anywhere in your terminal.

```bash
spytorec
```

Or run via Python module syntax:

```bash
python -m spytorec
```

On first run, SpytoRec will launch an **Interactive Hardware Discovery Wizard** to help you select your virtual audio cable. It will save this selection to `config.ini`.

### CLI Options

```bash
spytorec --help
  --ffmpeg FFMPEG  Path to ffmpeg executable (default: ffmpeg in PATH)
```

*(Note: The legacy single-file script is still available as `SpytoRec_v8.0.0.py` in the repository root for backward compatibility.)*

---

## 📁 Output Features

- Tracks saved in chosen format and directory
- FLAC includes embedded album art
- Duplicate checking by track ID and filename
- Metadata includes artist, album, and title
- Rewrites headers using FFmpeg post-recording

---

## 💡 Troubleshooting

| Issue                        | Solution                                                            |
|-----------------------------|---------------------------------------------------------------------|
| `Could not initialise a track source` | Keep the Spotify desktop app running and playing. macOS: approve the "control Spotify" prompt. Linux: add `[SpotifyAPI]` credentials. Run `spytorec` from the directory holding that `config.ini` |
| `Not authorized to send Apple events` (macOS) | Approve SpytoRec under *System Settings → Privacy & Security → Automation → Spotify* |
| No sound recorded           | Verify Spotify is routed to virtual device                         |
| Device shown greyed out in the wizard | Cosmetic only — it means no audio has hit that device yet. It's still selectable. Play a track with Spotify routed to it and the level bar turns green |
| "Device not found"          | Re-run and use the Interactive Hardware Wizard to select the correct device |
| Beginning cut off           | Lower `--interval` (e.g. 0.3 or 0.2) in config.ini                 |
| Corrupted duration          | Ensure `ffmpeg` finalizer runs successfully                       |
| Ads in recording            | Use Spotify Premium  (eventhough Ads won't recorded in Free Subscriptions)                                              |

---

## 🗺️ Roadmap & Future Architecture

With the v8.1.0 modular package foundation in place, here are the major architectural upgrades planned for the future. Pull requests for any of these are highly encouraged!

### 1. Eliminate Virtual Cables (WASAPI Loopback)
* **Goal:** Make the app "plug-and-play" without requiring users to install VB-Cable or BlackHole.
* **How:** Leverage `sounddevice` WASAPI loopback support (Windows) to capture audio directly from the default speaker output (what the user actually hears).

### 2. Eliminate FFmpeg Dependency (Native Encoding)
* **Goal:** Remove the requirement for users to install and configure `ffmpeg.exe` in their system PATH.
* **How:** Since we already capture raw PCM float32 data in Python via `numpy`, we can use `soundfile` (which wraps `libsndfile`) to encode directly to FLAC natively within Python. Zero subprocesses, zero broken pipes.

### 3. Zero-Latency Track Changes (OS Media APIs) — *in progress*
* **Goal:** Stop depending on the Spotify Web API (quota limits, latency, and now blocked entirely for non-Premium accounts) and read playback from the OS / local Spotify app instead.
* **Status:** Pluggable track-source layer landed (`spytorec/sources/`), picked per platform. **macOS** (Spotify.app over AppleScript) and **Windows** (SMTC, validated on Windows 11) are implemented and need no credentials. **Linux** (MPRIS/D-Bus) is stubbed and pending — it falls back to the Web API for now. A later pass will convert the event-capable backends (MPRIS, SMTC) from polling to push events for true zero-latency splits — both support subscribing to an OS-level "now playing changed" notification instead of asking on a timer, which Spotify's macOS AppleScript interface has no equivalent of.
* **See:** [Track Source](#-track-source) section above.

### 4. Standalone Executable
* **Goal:** Allow non-developers to run SpytoRec without installing Python or `pip`.
* **How:** Use `PyInstaller` or `Nuitka` to compile the entire Python package into a single, portable `SpytoRec.exe` executable.

### 5. Interactive TUI
* **Goal:** Upgrade from a static logging UI to a fully interactive terminal application.
* **How:** Migrate from `rich` to `Textual` to add mouse support, clickable tabs, scrolling logs, and a built-in interactive settings menu.

---

## 🤝 Contributing

Pull requests and stars ⭐ are welcome!  
Fork the repo, give it a star, and help build more useful tools for personal music archiving!

---

## 🙏 Contributors

| Contributor | Contribution |
|---|---|
| [@electrodics-ship-it](https://github.com/electrodics-ship-it) | V8.1.0 Python Package Architecture — Zero-latency streaming pipes, background watchdog, rich UI, and module refactoring. |

---

## 📜 License

This project is licensed under **MIT License**  
See [LICENSE](LICENSE) for details.

---

### 👤 Author

**@Darkphoenix**   
GitHub: [github.com/Danidukiyu](https://github.com/Danidukiyu)
