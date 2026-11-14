#!/usr/bin/env python3
"""
assemble.py — Automated 1440p Video Editing, Audio Ducking & Intro Montage Engine.

Features:
  1. Pristine 1440p (2560x1440 @ 60fps) output — enables YouTube's premium VP9/AV1 codec.
  2. Zero Black Bars:
     - "crop" (default): Smart center-fill crop to 16:9 edge-to-edge (zero bars, zero stretch).
     - "blur": Ambient frosted-glass blur background for 4:3 retro clips (zero bars, full HUD).
  3. Dynamic Audio Ducking: Game audio drops to ~18% during VO, swells to 100% for solo SFX.
  4. Intro Sizzle-Reel Montage: Automatically takes rapid cuts (3.5s) from trailers or clips,
     matches the exact duration of the Intro Voiceover, ducks trailer audio, and mixes Intro VO.
  5. Bug-Free Master Concat: Enforces identical framerate (60 CFR), SAR 1:1, BT.709 color,
     yuv420p, and 48kHz stereo across all segments for glitch-free lossless concatenation.
  6. Hardware Acceleration: Automatically detects and utilizes Nvidia NVENC (T4 GPU).

Usage:
  # Assemble at 1440p with default smart crop (no black bars):
  python assemble.py

  # With intro montage from a trailers/ folder:
  python assemble.py --intro-clips-dir ./trailers --solo-sfx 10

  # With ambient blur background for retro 4:3 clips:
  python assemble.py --aspect blur
"""

import os
import sys
import glob
import math
import shutil
import argparse
import subprocess
from typing import List, Dict, Optional, Tuple

from core.parser import parse_markdown_plan, PlanItem
from core.downloader import sanitize_filename, format_time_str


RESOLUTIONS = {
    "1440p": (2560, 1440),
    "1080p": (1920, 1080),
    "4k": (3840, 2160),
}


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

    for c in candidates:
        base = os.path.splitext(os.path.basename(c))[0].lower()
        if safe_name in base or base in safe_name:
            return c

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

    # Exclude intro voiceover files from countdown ranks
    candidates = [c for c in candidates if "intro" not in os.path.basename(c).lower()]
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

    for c in candidates:
        base = os.path.splitext(os.path.basename(c))[0].lower()
        if any(t in base for t in tokens):
            return c

    sorted_candidates = sorted(candidates)
    if 0 <= index - 1 < len(sorted_candidates):
        return sorted_candidates[index - 1]

    return None


def find_intro_vo_file(vo_dir: str, explicit_path: Optional[str] = None) -> Optional[str]:
    """Find intro voiceover audio file."""
    if explicit_path and os.path.isfile(explicit_path):
        return explicit_path

    if not os.path.isdir(vo_dir):
        return None

    for ext in ("*.mp3", "*.wav", "*.m4a", "*.aac"):
        for path in glob.glob(os.path.join(vo_dir, ext)):
            base = os.path.basename(path).lower()
            if "intro" in base or base.startswith("00") or base.startswith("0_"):
                return path
    return None


def build_video_filter(width: int, height: int, aspect_mode: str, total_duration: float, title_text: Optional[str]) -> str:
    """Build FFmpeg video filter pipeline to ensure 100% zero black bars and strict CFR standardization."""
    fps = 60

    if aspect_mode == "blur":
        # Ambient blurred frosted-glass background on sides for 4:3 footage (zero black bars, full HUD preserved)
        v_filter = (
            f"trim=duration={total_duration:.2f},setpts=PTS-STARTPTS,split=2[bg_in][fg_in];"
            f"[bg_in]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},boxblur=25:5[bg];"
            f"[fg_in]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps}"
        )
    elif aspect_mode == "pad":
        # Traditional letterbox/pillarbox padding
        v_filter = (
            f"trim=duration={total_duration:.2f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
        )
    else:
        # "crop" (Default): Smart center-fill 16:9 crop (zero black bars, edge-to-edge, zero distortion)
        v_filter = (
            f"trim=duration={total_duration:.2f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={fps}"
        )

    # Clean title text for drawtext
    clean_title = (title_text or "").replace("'", "").replace(":", " -")
    if clean_title:
        # Title card banner for the first 6 seconds
        v_filter += (
            f",drawtext=text='{clean_title}':x=80:y=h-140:fontsize=56:fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=12:enable='between(t,0,6)'"
        )

    return v_filter


def assemble_segment(
    video_path: str,
    vo_path: str,
    output_path: str,
    target_res: str = "1440p",
    aspect_mode: str = "crop",
    title_text: Optional[str] = None,
    solo_sfx_duration: float = 10.0,
    duck_volume: float = 0.18,
    use_gpu: bool = False,
    show_title_card: bool = True,
) -> float:
    """
    Assemble a single countdown segment with 1440p standardization, audio ducking, and title card.
    Returns total segment duration in seconds.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    width, height = RESOLUTIONS.get(target_res, (2560, 1440))

    vo_duration = get_media_duration(vo_path)
    total_duration = vo_duration + max(0.0, solo_sfx_duration)
    game_has_audio = has_audio_stream(video_path)

    v_filter = build_video_filter(
        width=width,
        height=height,
        aspect_mode=aspect_mode,
        total_duration=total_duration,
        title_text=title_text if show_title_card else None,
    )

    # Audio ducking filter
    if game_has_audio:
        a_filter = (
            f"[0:a]volume='if(lt(t,{vo_duration:.2f}),{duck_volume:.2f},if(lt(t,{vo_duration + 1.0:.2f}),{duck_volume:.2f}+{(1.0 - duck_volume):.2f}*(t-{vo_duration:.2f}),1.0))':eval=frame,"
            f"atrim=duration={total_duration:.2f},asetpts=PTS-STARTPTS[aducked];"
            f"[1:a]volume=1.0,apad=pad_dur={solo_sfx_duration:.2f},atrim=duration={total_duration:.2f},asetpts=PTS-STARTPTS[avo];"
            f"[aducked][avo]amix=inputs=2:duration=first:dropout_transition=2,"
            f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[aout]"
        )
    else:
        a_filter = (
            f"[1:a]volume=1.0,apad=pad_dur={solo_sfx_duration:.2f},atrim=duration={total_duration:.2f},asetpts=PTS-STARTPTS,"
            f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[aout]"
        )

    filter_complex = f"[0:v]{v_filter}[vout];{a_filter}"

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", vo_path,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "[aout]",
        "-pix_fmt", "yuv420p",
        "-colorspace", "bt709",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
    ]

    if use_gpu:
        cmd.extend(["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22"])
    else:
        cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])

    cmd.extend(["-c:a", "aac", "-b:a", "256k", "-ar", "48000", output_path])

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg assembly failed: {res.stderr[-400:]}")

    return total_duration


def assemble_intro_montage(
    intro_vo_path: str,
    trailer_clips: List[str],
    output_path: str,
    target_res: str = "1440p",
    aspect_mode: str = "crop",
    cut_duration: float = 3.5,
    duck_volume: float = 0.15,
    title_text: Optional[str] = "BITRATE BACKTRACK",
    use_gpu: bool = False,
) -> float:
    """
    Assemble an intro sizzle-reel montage by cutting multiple trailers and matching
    the exact duration of the intro voiceover.
    """
    width, height = RESOLUTIONS.get(target_res, (2560, 1440))
    vo_duration = get_media_duration(intro_vo_path)

    if not trailer_clips:
        raise ValueError("No trailer or gameplay clips available to build the intro montage.")

    # Calculate number of cuts needed
    num_cuts = max(1, math.ceil(vo_duration / cut_duration))
    slices: List[Tuple[str, float, float]] = []

    for i in range(num_cuts):
        clip = trailer_clips[i % len(trailer_clips)]
        # Cycle through clip start times to avoid repeating the exact same frame
        repeat_idx = i // len(trailer_clips)
        start_t = repeat_idx * (cut_duration + 1.0)
        dur = min(cut_duration, vo_duration - (i * cut_duration))
        if dur > 0.2:
            slices.append((clip, start_t, start_t + dur))

    # Build FFmpeg filter complex for montage
    filter_parts = []
    v_tags = []
    a_tags = []

    for i, (clip, start, end) in enumerate(slices):
        dur = end - start
        v_sub = build_video_filter(width=width, height=height, aspect_mode=aspect_mode, total_duration=dur, title_text=None)
        filter_parts.append(f"[{i}:v]trim=start={start:.2f}:duration={dur:.2f},setpts=PTS-STARTPTS,{v_sub}[v{i}]")
        filter_parts.append(f"[{i}:a]atrim=start={start:.2f}:duration={dur:.2f},asetpts=PTS-STARTPTS,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{i}]")
        v_tags.append(f"[v{i}]")
        a_tags.append(f"[a{i}]")

    filter_parts.append(f"{''.join(v_tags)}concat=n={len(slices)}:v=1:a=0[vmontage_raw]")
    filter_parts.append(f"{''.join(a_tags)}concat=n={len(slices)}:v=0:a=1[amontage_raw]")

    # Optional intro title banner
    if title_text:
        clean_title = title_text.replace("'", "")
        filter_parts.append(
            f"[vmontage_raw]drawtext=text='{clean_title}':x=(w-text_w)/2:y=h-160:fontsize=64:fontcolor=white:box=1:boxcolor=black@0.7:boxborderw=15[vout]"
        )
    else:
        filter_parts.append("[vmontage_raw]copy[vout]")

    vo_input_idx = len(slices)
    filter_parts.append(f"[amontage_raw]volume={duck_volume:.2f},atrim=duration={vo_duration:.2f},asetpts=PTS-STARTPTS[aducked]")
    filter_parts.append(f"[{vo_input_idx}:a]volume=1.0,atrim=duration={vo_duration:.2f},asetpts=PTS-STARTPTS[avo]")
    filter_parts.append(
        f"[aducked][avo]amix=inputs=2:duration=first:dropout_transition=2,"
        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[aout]"
    )

    filter_complex = ";".join(filter_parts)

    cmd = ["ffmpeg", "-y"]
    for s in slices:
        cmd.extend(["-i", s[0]])
    cmd.extend(["-i", intro_vo_path])
    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "[aout]",
        "-pix_fmt", "yuv420p",
        "-colorspace", "bt709",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
    ])

    if use_gpu:
        cmd.extend(["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22"])
    else:
        cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])

    cmd.extend(["-c:a", "aac", "-b:a", "256k", "-ar", "48000", output_path])

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg intro assembly failed: {res.stderr[-400:]}")

    return vo_duration


def concat_segments(segment_paths: List[str], output_master_path: str):
    """Concatenate standardized segments losslessly using FFmpeg concat demuxer."""
    os.makedirs(os.path.dirname(output_master_path), exist_ok=True)
    concat_list_path = os.path.join(os.path.dirname(output_master_path), "concat_list.txt")

    with open(concat_list_path, "w", encoding="utf-8") as f:
        for p in segment_paths:
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
        description="Automated 1440p Video Editing, Audio Ducking & Intro Montage Engine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("plan", nargs="?", default="videoplan1.md", help="Path to markdown plan file (default: videoplan1.md)")
    parser.add_argument("--clips-dir", default="./downloads", help="Directory containing downloaded gameplay video clips")
    parser.add_argument("--vo-dir", default="./voiceovers", help="Directory containing voiceover audio files")
    parser.add_argument("--output-dir", default="./output", help="Directory to save assembled segments and master compilation")
    parser.add_argument("--target-res", default="1440p", choices=["1440p", "1080p", "4k"], help="Target resolution (default: 1440p = 2560x1440)")
    parser.add_argument("--aspect", default="crop", choices=["crop", "blur", "pad"], help="Aspect ratio mode (crop = fill screen, no black bars; blur = frosted ambient background)")
    parser.add_argument("--solo-sfx", type=float, default=10.0, help="Solo gameplay SFX duration in seconds after VO ends (default: 10.0s)")
    parser.add_argument("--duck-vol", type=float, default=0.18, help="Gameplay volume while VO is speaking (default: 0.18 = 18 percent)")
    parser.add_argument("--intro-vo", default=None, help="Path to intro voiceover file (auto-detected if in vo-dir)")
    parser.add_argument("--intro-clips-dir", default=None, help="Directory containing trailer clips for intro montage (defaults to ./trailers or clips-dir)")
    parser.add_argument("--intro-cut", type=float, default=3.5, help="Duration of each trailer cut in intro montage (default: 3.5s)")
    parser.add_argument("--no-title", action="store_true", help="Disable on-screen title card overlay")
    parser.add_argument("--gpu", action="store_true", help="Force Nvidia NVENC hardware acceleration")
    parser.add_argument("--cpu", action="store_true", help="Force CPU encoding (libx264)")

    args = parser.parse_args()

    print("\n" + "=" * 75)
    print("      BITRATE BACKTRACK — 1440P AUTOMATED EDITING & INTRO ENGINE       ")
    print("=" * 75)

    nvenc_avail = check_nvenc_available()
    use_gpu = False
    if args.gpu:
        if not nvenc_avail:
            print("[!] Warning: --gpu requested but h264_nvenc was not found. Falling back to CPU.")
        else:
            use_gpu = True
    elif not args.cpu and nvenc_avail:
        use_gpu = True

    print(f"[*] Target Resolution:    {args.target_res} ({RESOLUTIONS[args.target_res][0]}x{RESOLUTIONS[args.target_res][1]} @ 60fps)")
    print(f"[*] Aspect Ratio Mode:    {args.aspect.upper()} ({'Smart Center Fill — Zero Black Bars' if args.aspect == 'crop' else 'Frosted Ambient Blur Background'})")
    print(f"[*] Video Encoder:        {'Nvidia NVENC (GPU Accelerated)' if use_gpu else 'CPU (libx264 veryfast)'}")
    print(f"[*] Audio Duck Volume:    {int(args.duck_vol * 100)}% during voiceover")
    print(f"[*] Solo Gameplay Swell:  {args.solo_sfx:.1f}s after voiceover ends")

    if not os.path.isfile(args.plan):
        print(f"[!] Plan file not found: {args.plan}")
        sys.exit(1)

    with open(args.plan, "r", encoding="utf-8") as f:
        md_text = f.read()

    items = parse_markdown_plan(md_text)
    if not items:
        print("[!] No items found in plan file.")
        sys.exit(1)

    print(f"[*] Loaded {len(items)} countdown items from {args.plan}")

    assembled_dir = os.path.join(args.output_dir, "assembled_segments")
    os.makedirs(assembled_dir, exist_ok=True)
    os.makedirs(args.vo_dir, exist_ok=True)

    assembled_files = []
    chapters = []
    current_time = 0.0

    # -----------------------------------------------------------------------
    # Step 1: Assemble Intro Montage (if intro VO exists)
    # -----------------------------------------------------------------------
    intro_vo_file = find_intro_vo_file(args.vo_dir, args.intro_vo)
    if intro_vo_file:
        print("\n" + "-" * 75)
        print(f"[*] INTRO SEGMENT DETECTED: {os.path.basename(intro_vo_file)}")

        # Find trailer clips
        trailers_dir = args.intro_clips_dir or ("./trailers" if os.path.isdir("./trailers") else args.clips_dir)
        trailer_clips = glob.glob(os.path.join(trailers_dir, "*.mp4"))

        if trailer_clips:
            print(f"    Building rapid-cut intro montage from {len(trailer_clips)} trailer clips in {trailers_dir}...")
            intro_output = os.path.join(assembled_dir, "segment_00_intro.mp4")
            try:
                intro_dur = assemble_intro_montage(
                    intro_vo_path=intro_vo_file,
                    trailer_clips=trailer_clips,
                    output_path=intro_output,
                    target_res=args.target_res,
                    aspect_mode=args.aspect,
                    cut_duration=args.intro_cut,
                    duck_volume=args.duck_vol,
                    title_text=None if args.no_title else "BITRATE BACKTRACK",
                    use_gpu=use_gpu,
                )
                chapters.append(f"{format_time_str(current_time)} - Intro")
                current_time += intro_dur
                assembled_files.append(intro_output)
                print(f"    [+] Intro montage assembled successfully ({intro_dur:.1f}s)")
            except Exception as e:
                print(f"    [!] Failed to assemble intro montage: {e}")
        else:
            print(f"    [!] No trailer clips found in {trailers_dir}. Skipping intro montage.")

    # -----------------------------------------------------------------------
    # Step 2: Assemble Countdown Segments (Rank Items)
    # -----------------------------------------------------------------------
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

    for idx, item in enumerate(items, 1):
        title = item.title or f"Segment_{idx}"
        video_path = find_matching_video_file(title, args.clips_dir)
        vo_path = find_matching_vo_file(title, idx, args.vo_dir)

        if not video_path:
            print(f"\n[!] Skipping #{idx} {title}: Video clip not found in {args.clips_dir}")
            continue

        if not vo_path:
            print(f"\n[!] Skipping #{idx} {title}: Voiceover file not found in {args.vo_dir}")
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
                target_res=args.target_res,
                aspect_mode=args.aspect,
                title_text=title,
                solo_sfx_duration=args.solo_sfx,
                duck_volume=args.duck_vol,
                use_gpu=use_gpu,
                show_title_card=not args.no_title,
            )

            chapters.append(f"{format_time_str(current_time)} - {title}")
            current_time += dur
            assembled_files.append(out_path)
            print(f"    [+] 1440p Segment ready ({dur:.1f}s): {out_name}")

        except Exception as e:
            print(f"    [!] Assembly error for {title}: {e}")

    if not assembled_files:
        print("\n[!] No segments were assembled. Please ensure both video clips and voiceovers exist.")
        return

    # -----------------------------------------------------------------------
    # Step 3: Lossless Master Concatenation
    # -----------------------------------------------------------------------
    print("\n" + "=" * 75)
    print(f"[*] Merging {len(assembled_files)} standardized segments into Master Video...")
    master_output = os.path.join(args.output_dir, "master_compilation.mp4")

    try:
        concat_segments(assembled_files, master_output)
        print(f"[+] SUCCESS! Master Compilation Saved: {os.path.abspath(master_output)}")
        size_mb = os.path.getsize(master_output) / (1024 * 1024)
        print(f"    Total Runtime: {format_time_str(current_time)} | File Size: {size_mb:.1f} MB")
        print(f"    Format: 1440p (2560x1440 @ 60fps) | Aspect: {args.aspect.upper()}")
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
