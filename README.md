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
| No sound recorded           | Verify Spotify is routed to virtual device                         |
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

### 3. Zero-Latency Track Changes (OS Media APIs)
* **Goal:** Stop polling the Spotify Web API (which eats quotas and causes delays) and achieve instant track splitting.
* **How:** Hook into the OS-level media events (Windows System Media Transport Controls or macOS Now Playing APIs) to get zero-latency event triggers the exact millisecond a track changes.

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
