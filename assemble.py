#!/usr/bin/env python3
"""
assemble.py — Automated Video Editing & Audio Ducking Engine for Bitrate Backtrack.

Automates the entire countdown compilation workflow:
  1. Pairs downloaded gameplay clips with corresponding voiceover (VO) files.
  2. Measures exact voiceover duration via ffprobe.
  3. Dynamic Audio Ducking: Game audio drops to ~18% during VO, then swells to 100%
     for raw gameplay solo sound effects (configurable solo buffer).
  4. Video Trimming & Scaling: Trims game clip to exact total duration and standardizes
     to 1080p60 (with optional clean title card overlay).
  5. Hardware Acceleration: Automatically detects and uses Nvidia NVENC (T4 GPU on Colab)
     or CPU (libx264).
  6. Master Concatenation: Stitches all countdown segments into the final master video.
  7. Dynamic Chapter Generation: Outputs YouTube-ready chapter timestamps.

Usage:
  python assemble.py
  python assemble.py videoplan1.md --vo-dir ./voiceovers --solo-sfx 10
  python assemble.py --gpu      # Force Nvidia NVENC hardware acceleration
"""

import os
import sys
import glob
import shutil
import argparse
import subprocess
from typing import List, Dict, Optional, Tuple

from core.parser import parse_markdown_plan, PlanItem
from core.downloader import sanitize_filename, format_time_str


def check_nvenc_available() -> bool:
    """Return True if ffmpeg has h264_nvenc encoder available."""
    try:
        res = subprocess.run(["ffmpeg", "-encoders"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        return "h264_nvenc" in res.stdout
    except Exception:
        return False


def get_media_duration(file_path: str) -> float:
    """Accurately measure media duration in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    return float(res.stdout.strip())


def has_audio_stream(file_path: str) -> bool:
    """Check if media file contains an audio stream."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return "audio" in res.stdout.lower()


def find_matching_video_file(clip_name: str, clips_dir: str) -> Optional[str]:
    """Find downloaded video file matching a clip title or keyword."""
    if not os.path.isdir(clips_dir):
        return None

    safe_name = sanitize_filename(clip_name).lower()
    candidates = glob.glob(os.path.join(clips_dir, "*.mp4"))
    
    # 1. Exact or prefix match
    for c in candidates:
        base = os.path.splitext(os.path.basename(c))[0].lower()
        if safe_name in base or base in safe_name:
            return c

    # 2. Word token match
    tokens = [t for t in safe_name.split() if len(t) > 2]
    best_match = None
    best_score = 0
    for c in candidates:
        base = os.path.splitext(os.path.basename(c))[0].lower()
        score = sum(1 for t in tokens if t in base)
        if score > best_score:
            best_score = score
            best_match = c

    if best_score >= max(1, len(tokens) // 2):
        return best_match

    return None


def find_matching_vo_file(clip_name: str, index: int, vo_dir: str) -> Optional[str]:
    """Find matching voiceover file by index or title."""
    if not os.path.isdir(vo_dir):
        return None

    extensions = ("*.mp3", "*.wav", "*.m4a", "*.aac", "*.ogg")
    candidates = []
    for ext in extensions:
        candidates.extend(glob.glob(os.path.join(vo_dir, ext)))

    if not candidates:
        return None

    safe_name = sanitize_filename(clip_name).lower()
    tokens = [t for t in safe_name.split() if len(t) > 2]

    # Check by index (e.g. "01_...", "20_...", "rank_20", "1.mp3")
    idx_patterns = [f"{index:02d}", f"{index}", f"rank_{index}", f"rank{index}"]
    for c in candidates:
        base = os.path.splitext(os.path.basename(c))[0].lower()
        for pat in idx_patterns:
            if base.startswith(pat) or f"_{pat}" in base or f"-{pat}" in base:
                return c

    # Check by title words
    for c in candidates:
        base = os.path.splitext(os.path.basename(c))[0].lower()
        if any(t in base for t in tokens):
            return c

    # Fallback to sorted index if counts match
    sorted_candidates = sorted(candidates)
    if 0 <= index - 1 < len(sorted_candidates):
        return sorted_candidates[index - 1]

    return None


def assemble_segment(
    video_path: str,
    vo_path: str,
    output_path: str,
    title_text: Optional[str] = None,
    solo_sfx_duration: float = 10.0,
    duck_volume: float = 0.18,
    use_gpu: bool = False,
    show_title_card: bool = True,
) -> float:
    """
    Assemble a single countdown segment with audio ducking and title card.
    Returns total segment duration in seconds.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    vo_duration = get_media_duration(vo_path)
    total_duration = vo_duration + max(0.0, solo_sfx_duration)
    game_has_audio = has_audio_stream(video_path)

    # Clean title text for drawtext
    clean_title = (title_text or "").replace("'", "").replace(":", " -") if show_title_card else ""

    # Build video filter (trim, standard scale to 1080p, 60fps, title card overlay)
    v_filters = [
        f"trim=duration={total_duration:.2f}",
        "setpts=PTS-STARTPTS",
        "scale=1920:1080:force_original_aspect_ratio=decrease",
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
        "setsar=1",
        "fps=60",
    ]

    if show_title_card and clean_title:
        # Title card banner for the first 6 seconds
        v_filters.append(
            f"drawtext=text='{clean_title}':x=60:y=h-120:fontsize=46:fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=10:enable='between(t,0,6)'"
        )

    v_filter_str = ",".join(v_filters)

    # Build audio filter (audio ducking + mix)
    if game_has_audio:
        # Duck gameplay during VO, then smooth 1s swell to 100% for solo sfx
        a_filter_str = (
            f"[0:a]volume='if(lt(t,{vo_duration:.2f}),{duck_volume:.2f},if(lt(t,{vo_duration + 1.0:.2f}),{duck_volume:.2f}+{(1.0 - duck_volume):.2f}*(t-{vo_duration:.2f}),1.0))':eval=frame,"
            f"atrim=duration={total_duration:.2f},asetpts=PTS-STARTPTS[aducked];"
            f"[1:a]volume=1.0,apad=pad_dur={solo_sfx_duration:.2f},atrim=duration={total_duration:.2f},asetpts=PTS-STARTPTS[avo];"
            f"[aducked][avo]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
    else:
        # If gameplay clip has no audio, just use VO padded with silence
        a_filter_str = (
            f"[1:a]volume=1.0,apad=pad_dur={solo_sfx_duration:.2f},atrim=duration={total_duration:.2f},asetpts=PTS-STARTPTS[aout]"
        )

    filter_complex = f"[0:v]{v_filter_str}[vout];{a_filter_str}"

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", vo_path,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "[aout]",
    ]

    # Hardware acceleration check
    if use_gpu:
        cmd.extend(["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"])
    else:
        cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "22"])

    cmd.extend(["-c:a", "aac", "-b:a", "192k", output_path])

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg assembly failed: {res.stderr[-400:]}")

    return total_duration


def concat_segments(segment_paths: List[str], output_master_path: str):
    """Concatenate assembled segments losslessly using FFmpeg concat demuxer."""
    os.makedirs(os.path.dirname(output_master_path), exist_ok=True)
    concat_list_path = os.path.join(os.path.dirname(output_master_path), "concat_list.txt")

    with open(concat_list_path, "w", encoding="utf-8") as f:
        for p in segment_paths:
            # Format path for FFmpeg concat file
            clean_p = os.path.abspath(p).replace("\\", "/")
            f.write(f"file '{clean_p}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_path,
        "-c", "copy",
        output_master_path,
    ]

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg concat failed: {res.stderr[-400:]}")

    if os.path.isfile(concat_list_path):
        os.remove(concat_list_path)


def main():
    parser = argparse.ArgumentParser(
        description="Automated Video Editing & Audio Ducking Assembly Engine for Countdown Compilations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("plan", nargs="?", default="videoplan1.md", help="Path to markdown plan file (default: videoplan1.md)")
    parser.add_argument("--clips-dir", default="./downloads", help="Directory containing downloaded gameplay video clips")
    parser.add_argument("--vo-dir", default="./voiceovers", help="Directory containing voiceover audio files")
    parser.add_argument("--output-dir", default="./output", help="Directory to save assembled segments and master compilation")
    parser.add_argument("--solo-sfx", type=float, default=10.0, help="Solo gameplay SFX duration in seconds after VO ends (default: 10.0s)")
    parser.add_argument("--duck-vol", type=float, default=0.18, help="Gameplay volume while VO is speaking (default: 0.18 = 18 percent)")
    parser.add_argument("--no-title", action="store_true", help="Disable on-screen title card overlay")
    parser.add_argument("--gpu", action="store_true", help="Force Nvidia NVENC hardware acceleration")
    parser.add_argument("--cpu", action="store_true", help="Force CPU encoding (libx264)")

    args = parser.parse_args()

    print("\n" + "=" * 75)
    print("      BITRATE BACKTRACK — AUTOMATED EDITING & AUDIO DUCKING ENGINE      ")
    print("=" * 75)

    # Detect GPU acceleration
    nvenc_avail = check_nvenc_available()
    use_gpu = False
    if args.gpu:
        if not nvenc_avail:
            print("[!] Warning: --gpu requested but h264_nvenc was not found. Falling back to CPU.")
        else:
            use_gpu = True
    elif not args.cpu and nvenc_avail:
        use_gpu = True

    print(f"[*] Encoder: {'Nvidia NVENC (GPU Accelerated)' if use_gpu else 'CPU (libx264 veryfast)'}")
    print(f"[*] Gameplay Duck Volume: {int(args.duck_vol * 100)}% during voiceover")
    print(f"[*] Solo Gameplay Swell:  {args.solo_sfx:.1f}s after voiceover ends")

    # Read plan
    if not os.path.isfile(args.plan):
        print(f"[!] Plan file not found: {args.plan}")
        sys.exit(1)

    with open(args.plan, "r", encoding="utf-8") as f:
        md_text = f.read()

    items = parse_markdown_plan(md_text)
    if not items:
        print("[!] No items found in plan file.")
        sys.exit(1)

    print(f"[*] Loaded {len(items)} items from {args.plan}")

    # Prepare directories
    assembled_dir = os.path.join(args.output_dir, "assembled_segments")
    os.makedirs(assembled_dir, exist_ok=True)
    os.makedirs(args.vo_dir, exist_ok=True)

    assembled_files = []
    chapters = []
    current_time = 0.0

    print("\n" + "-" * 75)
    print(f"{'#':<4} {'Title':<30} {'Video Clip':<20} {'Voiceover File'}")
    print("-" * 75)

    for idx, item in enumerate(items, 1):
        title = item.title or f"Segment_{idx}"
        video_path = find_matching_video_file(title, args.clips_dir)
        vo_path = find_matching_vo_file(title, idx, args.vo_dir)

        v_desc = os.path.basename(video_path)[:18] if video_path else "MISSING"
        vo_desc = os.path.basename(vo_path)[:18] if vo_path else "MISSING"

        print(f"{idx:<4} {title[:28]:<30} {v_desc:<20} {vo_desc}")

    print("-" * 75)

    # Assembly Loop
    for idx, item in enumerate(items, 1):
        title = item.title or f"Segment_{idx}"
        video_path = find_matching_video_file(title, args.clips_dir)
        vo_path = find_matching_vo_file(title, idx, args.vo_dir)

        if not video_path:
            print(f"\n[!] Skipping #{idx} {title}: Video clip not found in {args.clips_dir}")
            continue

        if not vo_path:
            print(f"\n[!] Skipping #{idx} {title}: Voiceover file not found in {args.vo_dir}")
            print(f"    Please place an audio file for #{idx} in {args.vo_dir}")
            continue

        out_name = f"segment_{idx:02d}_{sanitize_filename(title)}.mp4"
        out_path = os.path.join(assembled_dir, out_name)

        print(f"\n[{idx}/{len(items)}] Assembling: {title}")
        print(f"    Video:  {os.path.basename(video_path)}")
        print(f"    Audio:  {os.path.basename(vo_path)}")

        try:
            dur = assemble_segment(
                video_path=video_path,
                vo_path=vo_path,
                output_path=out_path,
                title_text=title,
                solo_sfx_duration=args.solo_sfx,
                duck_volume=args.duck_vol,
                use_gpu=use_gpu,
                show_title_card=not args.no_title,
            )

            # Record chapter marker
            chapters.append(f"{format_time_str(current_time)} - {title}")
            current_time += dur
            assembled_files.append(out_path)
            print(f"    [+] Segment ready ({dur:.1f}s): {out_name}")

        except Exception as e:
            print(f"    [!] Assembly error for {title}: {e}")

    if not assembled_files:
        print("\n[!] No segments were assembled. Please ensure both video clips and voiceovers exist.")
        return

    # Master Concatenation
    print("\n" + "=" * 75)
    print(f"[*] Stitched Concat: Merging {len(assembled_files)} segments into Master Video...")
    master_output = os.path.join(args.output_dir, "master_compilation.mp4")

    try:
        concat_segments(assembled_files, master_output)
        print(f"[+] SUCCESS! Master Compilation Saved: {os.path.abspath(master_output)}")
        size_mb = os.path.getsize(master_output) / (1024 * 1024)
        print(f"    Total Runtime: {format_time_str(current_time)} | File Size: {size_mb:.1f} MB")
    except Exception as e:
        print(f"[!] Concat failed: {e}")

    # Write YouTube Chapters
    chapters_path = os.path.join(args.output_dir, "youtube_chapters.txt")
    with open(chapters_path, "w", encoding="utf-8") as f:
        f.write("\n".join(chapters) + "\n")

    print(f"[+] YouTube Chapters generated: {os.path.abspath(chapters_path)}")
    print("\n--- YOUTUBE CHAPTERS PREVIEW ---")
    print("\n".join(chapters[:10]))
    if len(chapters) > 10:
        print(f"... and {len(chapters) - 10} more chapters")
    print("=" * 75 + "\n")


if __name__ == "__main__":
    main()
