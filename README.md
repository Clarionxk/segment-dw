# Bitrate Backtrack — Cloud Video Pipeline & Auto-Editor

A high-performance pipeline that downloads YouTube clips directly from Markdown plan files, automatically ducks audio, trims gameplay to voiceovers, and stitches countdown compilations (Top 10 / Top 20) with zero manual video editing required.

---

## The Workflow

```text
[ AI Agent / videoplan1.md ]
            │
            ├─────────────────────────────────────────────┐
            ▼                                             ▼
┌──────────────────────────────────────┐     ┌──────────────────────────────────────┐
│ 1. Download Gameplay Clips           │     │ 2. Voiceover (VO) Files              │
│ - python download.py videoplan1.md   │     │ - Upload tiny MP3s (10-30 MB total)  │
│ - Fast Byte-Seeking (HTTP Range)     │     │ - Placed in ./voiceovers/ folder     │
└──────────────────────────────────────┘     └──────────────────────────────────────┘
            │                                             │
            └──────────────────────┬──────────────────────┘
                                   ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│ 3. Automated FFmpeg Assembly & Audio Ducking (python assemble.py)                  │
│ - While VO speaks: Game audio drops to 18% (Narration is crisp and clear)         │
│ - When VO finishes: Game audio smoothly swells to 100% for raw solo gameplay SFX │
│ - Automatically trims video to match exact VO length + solo buffer                │
│ - Overlays countdown title card (e.g. "#10: SWAT 4")                              │
│ - Hardware accelerated via Nvidia NVENC (Colab GPU) or fast CPU                   │
└───────────────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│ 4. Master Compilation & Dynamic Chapters                                          │
│ - Master video: output/master_compilation.mp4                                     │
│ - YouTube Chapters: output/youtube_chapters.txt                                   │
└───────────────────────────────────────────────────────────────────────────────────┘
```

---

## ⚡ Google Colab Free GPU (Recommended — Solves Slow Internet & VPS Freezes)

If your home internet upload is slow or your VPS freezes during editing:
1. Open **[Google Colab](https://colab.research.google.com/)** (free).
2. Click **Upload** and upload `BitrateBacktrack_AutoEditor.ipynb` from this repo.
3. Switch runtime to **T4 GPU** (`Runtime -> Change runtime type -> T4 GPU`).
4. Run the cells in order:
   - Downloads gameplay clips at **800+ Mbps** datacenter speed.
   - Upload your tiny VO MP3s (or read them from Google Drive).
   - Renders with the **Nvidia T4 GPU** in 3-5 minutes (zero CPU freezes).
   - Automatically saves the finished compilation directly into your **Google Drive** internally.

---

## Local / VPS Setup (Ubuntu / Debian)

### 1. Install System Requirements
```bash
sudo apt update && sudo apt install -y ffmpeg python3 python3-pip python3-venv git
```

### 2. Virtual Environment & Dependencies
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## CLI Usage

### Step 1: Download Gameplay Clips
```bash
# Preview targets without downloading
python download.py videoplan1.md --dry-run

# Download clips defined in videoplan1.md
python download.py videoplan1.md --res 1080p

# Use cookies.txt if YouTube rate-limits datacenter IP
python download.py videoplan1.md --cookies cookies.txt
```

### Step 2: Automated Assembly & Audio Ducking
Place your voiceover files in `./voiceovers/` (e.g. `01.mp3`, `02.mp3` or matching game titles) and run:
```bash
# Automatic assembly with 10s solo gameplay sound effects after VO:
python assemble.py videoplan1.md --solo-sfx 10

# Adjust ducking volume (e.g., 15% game audio during voiceover):
python assemble.py videoplan1.md --duck-vol 0.15

# Use Nvidia GPU acceleration (instant encoding on Google Colab or PC with Nvidia card):
python assemble.py videoplan1.md --gpu
```

Outputs:
- **`output/master_compilation.mp4`**: The complete stitched master video.
- **`output/youtube_chapters.txt`**: Timestamped chapters for the YouTube description.

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

---

## Assembly Flags Reference

| Flag | Default | Description |
|---|---|---|
| `plan` | `videoplan1.md` | Path to Markdown plan file |
| `--clips-dir` | `./downloads` | Folder containing downloaded clips |
| `--vo-dir` | `./voiceovers` | Folder containing voiceover audio files |
| `--output-dir` | `./output` | Folder to save assembled segments and master compilation |
| `--solo-sfx` | `10.0` | Solo gameplay audio duration in seconds after voiceover finishes |
| `--duck-vol` | `0.18` | Gameplay audio volume while voiceover is talking (0.18 = 18%) |
| `--gpu` | `auto` | Use Nvidia NVENC hardware acceleration (`h264_nvenc`) |
| `--no-title` | `False` | Disable countdown title card overlay on screen |
