# Bitrate Backtrack — Markdown Video & Segment Downloader

A lightweight, high-performance CLI tool that downloads YouTube videos and precise video segments directly from `.md` (Markdown) plan files. Perfect for AI agents, content researchers, and automated video workflows.

---

## Features

- **Markdown Driven:** Paste tables, lists, or links from your AI agent into `videoplan1.md` and run.
- **Fast Byte-Seeking Segments:** Slices clips in seconds using HTTP Range requests without downloading full long videos.
- **Lossless Stream-Copy:** Uses FFmpeg `-c copy` keyframe seeking by default for near-instant downloads with zero re-encoding loss.
- **VPS Ready:** Lightweight footprint, runs headless on Linux VPS or Windows.
- **Bot-Detection Bypassing:** Built-in `--cookies` support to handle YouTube datacenter IP challenges on VPS.
- **Smart Resume / Deduplication:** Skips already-downloaded clips automatically.

---

## VPS Quick Start (Ubuntu / Debian)

### 1. Install System Requirements
```bash
sudo apt update && sudo apt install -y ffmpeg python3 python3-pip python3-venv git
```

### 2. Clone or Copy Project
```bash
git clone <your-repo-url> bitrate-backtrack
cd bitrate-backtrack
```

### 3. Create Virtual Environment & Install Dependencies
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Usage

### Basic Usage
Place your targets in `videoplan1.md` (or any `.md` file) and run:
```bash
python download.py
```

### Preview Plan Without Downloading (Dry Run)
```bash
python download.py --dry-run
```

### Download a Specific Plan File
```bash
python download.py path/to/custom_plan.md
```

### Custom Output Directory & Resolution
```bash
python download.py videoplan1.md --output /media/clips --res 1080p
```

### VPS Authentication with Cookies (Recommended for Datacenter IPs)
YouTube often blocks requests from cloud VPS providers (AWS, Hetzner, DigitalOcean, etc.). Export your YouTube cookies using a browser extension (such as *Get cookies.txt LOCALLY*) into a `cookies.txt` file in this directory:
```bash
python download.py videoplan1.md --cookies cookies.txt
```
*(The script also auto-detects `cookies.txt` if present in the project root).*

### Frame-Accurate Precise Cutting
For frame accuracy around cuts (re-encodes cut points with FFmpeg):
```bash
python download.py videoplan1.md --precise
```

---

## Markdown Plan Formats

The parser automatically detects any of the following structures:

### Format A: Markdown Table (Recommended for AI Agents)
```markdown
| Title | URL | Start | End | Resolution | Notes |
|---|---|---|---|---|---|
| Half-Life 2 Ravenholm | https://www.youtube.com/watch?v=dQw4w9WgXcQ | 01:20 | 02:45 | 1080p | Dark atmosphere |
| Doom 3 Alpha Labs | https://youtu.be/dQw4w9WgXcQ | 00:30 | 01:15 | 720p | Action sequence |
| Full Video Example | https://www.youtube.com/watch?v=dQw4w9WgXcQ | | | best | Full download |
```

### Format B: Key-Value Bullet List
```markdown
### Half-Life 2 Ravenholm
- URL: https://www.youtube.com/watch?v=dQw4w9WgXcQ
- Start: 01:20
- End: 02:45
- Resolution: 1080p
```

### Format C: Inline Links
```markdown
- [Half-Life 2 Ravenholm](https://www.youtube.com/watch?v=dQw4w9WgXcQ) (01:20 - 02:45) [1080p]
- https://youtu.be/dQw4w9WgXcQ | 00:30 - 01:15 | Doom 3
```

---

## CLI Options Reference

| Flag | Default | Description |
|---|---|---|
| `plan` | `videoplan1.md` | Path to Markdown plan file |
| `-o`, `--output` | `./downloads` | Folder to save videos and segments |
| `-r`, `--res` | `1080p` | Default video resolution (`best`, `1080p`, `720p`, `480p`, `360p`) |
| `-c`, `--cookies` | `cookies.txt` | Path to Netscape format cookie file |
| `--precise` | `False` | Re-encode frames at cut points for exact accuracy |
| `--overwrite` | `False` | Force re-download if file already exists |
| `--dry-run` | `False` | Parse and display plan summary without downloading |

