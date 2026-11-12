"""
core/downloader.py — High-performance YouTube video & segment downloader using yt-dlp & FFmpeg.

Key Capabilities:
  - Fast Byte-Seeking Segment Extraction (downloads only specified time range in seconds via HTTP Range requests)
  - Stream-Copy (-c copy) keyframe-accurate cuts by default (zero quality loss, ultra fast)
  - Optional frame-accurate precise cuts via re-encoding (--precise)
  - Headless VPS optimization (Linux & Windows cross-platform FFmpeg detection)
  - Cookie file support (bypasses YouTube datacenter IP rate-limits on VPS)
  - Safe cross-platform filename sanitization
  - Automatic skip for already-downloaded files
"""

import os
import re
import glob
import shutil
from typing import Optional, Callable, Dict, Any, Tuple
import yt_dlp


# ---------------------------------------------------------------------------
# FFmpeg Detection
# ---------------------------------------------------------------------------

def find_ffmpeg() -> Optional[str]:
    """
    Locate ffmpeg on the current system (Linux VPS, macOS, or Windows).
    Returns the directory containing ffmpeg binary, or None if in PATH / not found.
    """
    # 1. Check PATH
    bin_path = shutil.which("ffmpeg")
    if bin_path:
        return os.path.dirname(os.path.abspath(bin_path))

    # 2. Linux VPS standard locations
    for linux_path in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/snap/bin/ffmpeg"]:
        if os.path.isfile(linux_path) and os.access(linux_path, os.X_OK):
            return os.path.dirname(linux_path)

    # 3. Windows WinGet install locations
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        winget_dir = os.path.join(local, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(winget_dir):
            for folder in os.listdir(winget_dir):
                if "FFmpeg" in folder:
                    bin_dir = os.path.join(winget_dir, folder)
                    for root, _, files in os.walk(bin_dir):
                        if "ffmpeg.exe" in files:
                            return root

    # 4. Common Windows manual install paths
    for candidate in [
        r"C:\ffmpeg\bin",
        r"C:\Program Files\ffmpeg\bin",
        os.path.expanduser(r"~\ffmpeg\bin"),
    ]:
        if os.path.isfile(os.path.join(candidate, "ffmpeg.exe")):
            return candidate

    return None


FFMPEG_DIR = find_ffmpeg()


# ---------------------------------------------------------------------------
# URL Validation & Sanitization
# ---------------------------------------------------------------------------

_YT_PATTERNS = [
    r"(https?://)?(www\.|m\.)?youtube\.com/watch\?[^\s\)\"\]]+",
    r"(https?://)?(www\.|m\.)?youtube\.com/shorts/[a-zA-Z0-9_-]+",
    r"(https?://)?(www\.|m\.)?youtube\.com/live/[a-zA-Z0-9_-]+",
    r"(https?://)?(www\.|m\.)?youtube\.com/embed/[a-zA-Z0-9_-]+",
    r"(https?://)?youtu\.be/[a-zA-Z0-9_-]+",
    r"(https?://)?music\.youtube\.com/watch\?[^\s\)\"\]]+",
]


def validate_url(url: str) -> bool:
    """Return True if url is a valid YouTube link."""
    if not url:
        return False
    url = url.strip()
    return any(re.match(p, url, re.IGNORECASE) for p in _YT_PATTERNS)


def sanitize_filename(name: str) -> str:
    """Sanitize title for safe cross-platform file naming (Windows + Linux)."""
    if not name:
        return "video"
    # Replace invalid Windows / Unix characters: < > : " / \ | ? *
    cleaned = re.sub(r'[<>:"/\\|?*]', "-", name)
    # Collapse multiple dashes or spaces
    cleaned = re.sub(r"[\s-]+", " ", cleaned).strip()
    # Strip leading/trailing dots or spaces
    cleaned = cleaned.strip(". ")
    return cleaned or "video"


# ---------------------------------------------------------------------------
# Time Formatting & Parsing Helpers
# ---------------------------------------------------------------------------

def parse_time_str(time_str: Optional[str | float | int]) -> Optional[float]:
    """
    Parse a timestamp string into seconds.
    Supported:
      - "80", 80, 45.5
      - "01:20" -> 80s
      - "01:04:30" -> 3870s
    """
    if time_str is None:
        return None
    if isinstance(time_str, (int, float)):
        return max(0.0, float(time_str))

    s = str(time_str).strip()
    if not s or s.lower() in ("none", "null", "-", "start", "end", "n/a"):
        return None

    # Strip trailing unit if any (e.g. 80s -> 80)
    s = re.sub(r"[sS]$", "", s).strip()

    # Numeric check
    try:
        val = float(s)
        return max(0.0, val)
    except ValueError:
        pass

    parts = s.split(":")
    try:
        if len(parts) == 2:
            mins = int(parts[0])
            secs = float(parts[1])
            if mins >= 0 and 0 <= secs < 60:
                return mins * 60 + secs
        elif len(parts) == 3:
            hours = int(parts[0])
            mins = int(parts[1])
            secs = float(parts[2])
            if hours >= 0 and 0 <= mins < 60 and 0 <= secs < 60:
                return hours * 3600 + mins * 60 + secs
    except (ValueError, TypeError):
        return None

    return None


def format_time_str(seconds: Optional[float]) -> str:
    """Format seconds into HH:MM:SS or MM:SS."""
    if seconds is None or seconds < 0:
        return "00:00"
    total_secs = int(seconds)
    mins, secs = divmod(total_secs, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_time_filename(seconds: Optional[float]) -> str:
    """Format seconds for clean filename embedding, e.g. 01m20s or 01h04m30s."""
    if seconds is None or seconds < 0:
        return "00m00s"
    total_secs = int(seconds)
    mins, secs = divmod(total_secs, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours:02d}h{mins:02d}m{secs:02d}s"
    return f"{mins:02d}m{secs:02d}s"


# ---------------------------------------------------------------------------
# Format Selector Generator
# ---------------------------------------------------------------------------

def _build_format_selector(resolution: str, is_segment: bool) -> str:
    """Build yt-dlp format selection string based on target resolution and clip mode."""
    res = (resolution or "best").lower()

    if is_segment:
        # Prioritize direct HTTPS streams to enable fast byte-seeking Range requests
        if res == "1080p":
            return (
                "bestvideo[protocol^=http][height<=1080][ext=mp4]+bestaudio[protocol^=http][ext=m4a]/"
                "bestvideo[protocol^=http][height<=1080]+bestaudio[protocol^=http]/"
                "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo[height<=1080]+bestaudio/"
                "best[protocol^=http][height<=1080][ext=mp4]/"
                "best[height<=1080]"
            )
        elif res == "720p":
            return (
                "bestvideo[protocol^=http][height<=720][ext=mp4]+bestaudio[protocol^=http][ext=m4a]/"
                "bestvideo[protocol^=http][height<=720]+bestaudio[protocol^=http]/"
                "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo[height<=720]+bestaudio/"
                "best[protocol^=http][height<=720][ext=mp4]/"
                "best[height<=720]"
            )
        elif res == "480p":
            return (
                "bestvideo[protocol^=http][height<=480][ext=mp4]+bestaudio[protocol^=http][ext=m4a]/"
                "bestvideo[protocol^=http][height<=480]+bestaudio[protocol^=http]/"
                "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo[height<=480]+bestaudio/"
                "best[protocol^=http][height<=480][ext=mp4]/"
                "best[height<=480]"
            )
        elif res == "360p":
            return (
                "bestvideo[protocol^=http][height<=360][ext=mp4]+bestaudio[protocol^=http][ext=m4a]/"
                "bestvideo[protocol^=http][height<=360]+bestaudio[protocol^=http]/"
                "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo[height<=360]+bestaudio/"
                "best[protocol^=http][height<=360][ext=mp4]/"
                "best[height<=360]"
            )
        else:
            # "best" / 1440p / 4k
            return (
                "bestvideo[protocol^=http][ext=mp4]+bestaudio[protocol^=http][ext=m4a]/"
                "bestvideo[protocol^=http]+bestaudio[protocol^=http]/"
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo+bestaudio/"
                "best[protocol^=http][ext=mp4]/"
                "best[ext=mp4]/best"
            )
    else:
        # Full video download
        if res == "1080p":
            return "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080][ext=mp4]/best[height<=1080]"
        elif res == "720p":
            return "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720][ext=mp4]/best[height<=720]"
        elif res == "480p":
            return "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480][ext=mp4]/best[height<=480]"
        elif res == "360p":
            return "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360][ext=mp4]/best[height<=360]"
        else:
            return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"


# ---------------------------------------------------------------------------
# Progress Callback Hook
# ---------------------------------------------------------------------------

class _DownloadProgressHook:
    def __init__(self, callback: Optional[Callable[[float, str], None]], is_segment: bool):
        self._cb = callback
        self._is_segment = is_segment

    def __call__(self, d: Dict[str, Any]):
        if not self._cb:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            pct = min(max(downloaded / total, 0.0), 0.95) if total > 0 else 0.5
            speed = d.get("_speed_str", "")
            eta = d.get("_eta_str", "")
            action = "Downloading segment (byte-seek)" if self._is_segment else "Downloading video"
            msg = f"{action}… ({speed} - ETA {eta})" if speed and eta else f"{action}…"
            self._cb(pct, msg)
        elif status == "finished":
            self._cb(0.98, "Finalizing container with FFmpeg…")


# ---------------------------------------------------------------------------
# Core Download Function
# ---------------------------------------------------------------------------

def download_media(
    url: str,
    output_dir: str = "./downloads",
    title: Optional[str] = None,
    start_sec: Optional[float] = None,
    end_sec: Optional[float] = None,
    resolution: str = "best",
    cookies_file: Optional[str] = None,
    precise_cut: bool = False,
    overwrite: bool = False,
    progress_callback: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, Any]:
    """
    Download a full video or a sliced segment from YouTube.

    Parameters:
      url: YouTube video URL.
      output_dir: Folder where file will be saved.
      title: Optional custom title/name.
      start_sec: Start timestamp in seconds (None = 0).
      end_sec: End timestamp in seconds (None = end of video).
      resolution: Target resolution (best, 1080p, 720p, 480p, 360p).
      cookies_file: Path to cookies.txt (useful for VPS datacenter IPs).
      precise_cut: If True, re-encodes cuts for frame accuracy (slower).
                   If False, uses fast stream-copy (-c copy) keyframe seeking.
      overwrite: Re-download even if file already exists.
      progress_callback: Callback(pct: float, message: str).

    Returns:
      Dict with status ("downloaded", "skipped", "error"), file_path, and metadata.
    """
    os.makedirs(output_dir, exist_ok=True)

    is_segment = (start_sec is not None and start_sec > 0) or (end_sec is not None)
    actual_start = max(0.0, float(start_sec or 0)) if is_segment else None
    actual_end = float(end_sec) if end_sec is not None else None

    # Construct clean output template
    s_tag = format_time_filename(actual_start) if is_segment else None
    e_tag = format_time_filename(actual_end) if actual_end is not None else ("end" if is_segment else None)

    if title:
        safe_title = sanitize_filename(title)
        if is_segment:
            target_filename = f"{safe_title}_clip_{s_tag}_to_{e_tag}.mp4"
        else:
            target_filename = f"{safe_title}.mp4"
        target_path = os.path.join(output_dir, target_filename)

        # Skip if already exists
        if not overwrite and os.path.isfile(target_path) and os.path.getsize(target_path) > 1024:
            return {
                "status": "skipped",
                "file_path": os.path.abspath(target_path),
                "title": title,
                "is_segment": is_segment,
                "message": "File already exists",
            }
        outtmpl = os.path.join(output_dir, f"{safe_title}_clip_{s_tag}_to_{e_tag}.%(ext)s") if is_segment else os.path.join(output_dir, f"{safe_title}.%(ext)s")
    else:
        if is_segment:
            outtmpl = os.path.join(output_dir, f"%(title)s_clip_{s_tag}_to_{e_tag}.%(ext)s")
        else:
            outtmpl = os.path.join(output_dir, "%(title)s.%(ext)s")

    # yt-dlp Configuration
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "keepvideo": False,
    }

    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR

    # Provide node runtime if installed (improves signature descrambling)
    if shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}

    # Cookies support
    if cookies_file and os.path.isfile(cookies_file):
        opts["cookiefile"] = os.path.abspath(cookies_file)
    elif os.path.isfile("cookies.txt"):
        opts["cookiefile"] = os.path.abspath("cookies.txt")

    # Format selector
    opts["format"] = _build_format_selector(resolution, is_segment)

    # Segment slicing configuration (Fast byte-seek protocol)
    if is_segment:
        opts["download_ranges"] = yt_dlp.utils.download_range_func([], [[actual_start, actual_end]])
        opts["force_keyframes_at_cuts"] = bool(precise_cut)
        opts["format_sort"] = ["proto:https"]

        ffmpeg_i_args = [
            "-seekable", "1",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
        ]
        opts["external_downloader_args"] = {
            "ffmpeg_i": ffmpeg_i_args,
        }
        if precise_cut:
            opts["external_downloader_args"]["ffmpeg_o"] = ["-preset", "veryfast"]

    if progress_callback:
        opts["progress_hooks"] = [_DownloadProgressHook(progress_callback, is_segment=is_segment)]

    # Record directory state before download to identify created file reliably
    pre_files = set(os.listdir(output_dir))

    # Execute download
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        video_title = info.get("title", title or "Unknown")

    # Identify output file
    post_files = set(os.listdir(output_dir))
    new_files = [f for f in post_files - pre_files if f.endswith(".mp4")]

    if new_files:
        final_filename = new_files[0]
        final_path = os.path.abspath(os.path.join(output_dir, final_filename))
    else:
        # Fallback: check expected filename or most recently modified mp4
        candidates = glob.glob(os.path.join(output_dir, "*.mp4"))
        if candidates:
            final_path = os.path.abspath(max(candidates, key=os.path.getmtime))
        else:
            raise FileNotFoundError("Downloaded MP4 file could not be found.")

    return {
        "status": "downloaded",
        "file_path": final_path,
        "title": video_title,
        "is_segment": is_segment,
        "start": actual_start,
        "end": actual_end,
        "file_size": os.path.getsize(final_path) if os.path.exists(final_path) else 0,
    }

