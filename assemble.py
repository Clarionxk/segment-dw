#!/usr/bin/env python3
"""
assemble.py — Automated 1440p Video Editing, Audio Ducking & Intro Montage Engine.

Features:
  1. Full Auto-Detect Mode: Automatically pairs voiceovers (e.g. BioShock.wav) with
     downloaded clips (e.g. BioShock_clip.mp4) without needing a manual markdown plan!
  2. Intro Trailer Montage: Auto-detects INTRO.wav, takes rapid cuts from clips in
     download/ (or trailers/), ducks trailer audio, and syncs your Intro VO.
  3. Pristine 1440p (2560x1440 @ 60fps) output — unlocks YouTube's premium VP9/AV1 codec.
  4. Zero Black Bars:
     - "crop" (default): Smart center-fill to 16:9 edge-to-edge (zero bars, zero stretch).
     - "blur": Ambient frosted-glass blur background for 4:3 retro clips (zero bars, full HUD).
  5. Fontconfig-Safe: Direct system font loading prevents Windows Fontconfig crashes.
  6. Bug-Free Master Concat: Strict CFR 60fps, SAR 1:1, BT.709, and 48kHz stereo standardization.

Usage:
  # Auto-pair voiceovers and download folder:
  python assemble.py --auto

  # Custom directories:
  python assemble.py --auto --clips-dir download --vo-dir voiceovers
"""

import os
import sys
import re
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


def get_system_font() -> Optional[str]:
    """Find a readable TTF font file on Windows or Linux to avoid Fontconfig errors."""
    candidates = [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c.replace("\\", "/").replace(":", r"\:")
    return None


SYSTEM_FONT = get_system_font()


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


def find_best_matching_clip(vo_title: str, clips_dir: str) -> Optional[str]:
    """
    Smart fuzzy matcher that pairs a voiceover title (e.g. 'BioShock' or 'Call of Duty 4')
    with the best video clip in clips_dir.
    """
    if not os.path.isdir(clips_dir):
        return None

    candidates = glob.glob(os.path.join(clips_dir, "*.mp4"))
    if not candidates:
        return None

    vo_clean = re.sub(r"[^a-zA-Z0-9\s]", "", vo_title).lower()
    vo_tokens = [t for t in vo_clean.split() if t not in ("game", "pc", "fps", "retro")]
    vo_set = set(vo_tokens)

    best_clip = None
    best_score = -999

    for c in candidates:
        base_name = os.path.splitext(os.path.basename(c))[0]
        c_clean = re.sub(r"[^a-zA-Z0-9\s]", "", base_name).lower()
        c_tokens = [t for t in c_clean.split() if t not in ("clip", "to", "end", "gameplay", "full", "hd", "1080p", "4k")]
        c_set = set(c_tokens)

        # 1. Exact or starts-with match
        if vo_clean == c_clean or c_clean.startswith(vo_clean) or vo_clean in c_clean:
            return c

        # 2. Token overlap score
        overlap = vo_set.intersection(c_set)
        score = len(overlap) * 3

        # Distinguish sub-titles like "Crysis Warhead" vs "Crysis"
        if ("warhead" in vo_set) != ("warhead" in c_set):
            score -= 15
        if ("two" in vo_set or "2" in vo_set) and ("two" in c_set or "2" in c_set):
            score += 4
        if ("vegas" in vo_set) and ("vegas" in c_set):
            score += 6
        if ("edge" in vo_set) and ("edge" in c_set):
            score += 6

        if score > best_score and score >= max(1, len(vo_tokens) // 2):
            best_score = score
            best_clip = c

    return best_clip


def auto_detect_pairs(vo_dir: str, clips_dir: str) -> Tuple[Optional[str], List[Dict[str, str]]]:
    """
    Auto-detect voiceover files in vo_dir and pair them with matching clips in clips_dir.
    Returns (intro_vo_path, list_of_pairs).
    """
    intro_vo = None
    pairs = []

    if not os.path.isdir(vo_dir):
        return None, []

    # Gather audio files (.wav, .mp3, .m4a, .aac)
    vo_files = []
    for ext in ("*.wav", "*.mp3", "*.m4a", "*.aac", "*.ogg"):
        vo_files.extend(glob.glob(os.path.join(vo_dir, ext)))

    # Sort files naturally
    vo_files = sorted(vo_files, key=lambda x: os.path.basename(x).lower())

    for vf in vo_files:
        base = os.path.splitext(os.path.basename(vf))[0]
        # Check if this is the intro voiceover
        if "intro" in base.lower() or base.startswith("00") or base.startswith("0_"):
            intro_vo = vf
            continue

        matched_clip = find_best_matching_clip(base, clips_dir)
        pairs.append({
            "title": base,
            "vo_path": vf,
            "video_path": matched_clip,
        })

    return intro_vo, pairs


def build_video_filter(width: int, height: int, aspect_mode: str, total_duration: float, title_text: Optional[str]) -> str:
    """Build FFmpeg video filter pipeline with Fontconfig safety and zero black bars."""
    fps = 60

    if aspect_mode == "blur":
        v_filter = (
            f"trim=duration={total_duration:.2f},setpts=PTS-STARTPTS,split=2[bg_in][fg_in];"
            f"[bg_in]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},boxblur=25:5[bg];"
            f"[fg_in]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps}"
        )
    elif aspect_mode == "pad":
        v_filter = (
            f"trim=duration={total_duration:.2f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
        )
    else:
        # "crop" (Default): Smart center-fill to 16:9 (zero black bars, zero stretch)
        v_filter = (
            f"trim=duration={total_duration:.2f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={fps}"
        )

    # Clean title text for drawtext
    clean_title = (title_text or "").replace("'", "").replace(":", " -")
    if clean_title and SYSTEM_FONT:
        # Use direct fontfile path to prevent Fontconfig errors
        v_filter += (
            f",drawtext=fontfile='{SYSTEM_FONT}':text='{clean_title}':x=80:y=h-140:fontsize=56:fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=12:enable='between(t,0,6)'"
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
    """Assemble a single countdown segment with 1440p standardization, audio ducking, and title card."""
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
        # Fallback if drawtext fails
        if "drawtext" in filter_complex:
            v_filter_fallback = build_video_filter(width=width, height=height, aspect_mode=aspect_mode, total_duration=total_duration, title_text=None)
            cmd[cmd.index("-filter_complex") + 1] = f"[0:v]{v_filter_fallback}[vout];{a_filter}"
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0:
                return total_duration

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
    """Assemble an intro sizzle-reel montage from trailers/clips matching Intro VO length."""
    width, height = RESOLUTIONS.get(target_res, (2560, 1440))
    vo_duration = get_media_duration(intro_vo_path)

    if not trailer_clips:
        raise ValueError("No trailer or gameplay clips available to build the intro montage.")

    num_cuts = max(1, math.ceil(vo_duration / cut_duration))
    slices: List[Tuple[str, float, float]] = []

    for i in range(num_cuts):
        clip = trailer_clips[i % len(trailer_clips)]
        repeat_idx = i // len(trailer_clips)
        start_t = repeat_idx * (cut_duration + 1.0)
        dur = min(cut_duration, vo_duration - (i * cut_duration))
        if dur > 0.2:
            slices.append((clip, start_t, start_t + dur))

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

    if title_text and SYSTEM_FONT:
        clean_title = title_text.replace("'", "")
        filter_parts.append(
            f"[vmontage_raw]drawtext=fontfile='{SYSTEM_FONT}':text='{clean_title}':x=(w-text_w)/2:y=h-160:fontsize=64:fontcolor=white:box=1:boxcolor=black@0.7:boxborderw=15[vout]"
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
        # Fallback if drawtext caused the error
        if "drawtext" in filter_complex:
            filter_parts[-3] = "[vmontage_raw]copy[vout]"
            cmd[cmd.index("-filter_complex") + 1] = ";".join(filter_parts)
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0:
                return vo_duration

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
    parser.add_argument("plan", nargs="?", default=None, help="Optional path to markdown plan file (leave empty for auto-detect)")
    parser.add_argument("--auto", action="store_true", help="Force auto-pairing of voiceovers and clips by filename without markdown plan")
    parser.add_argument("--clips-dir", default="./download", help="Directory containing downloaded gameplay video clips (default: ./download)")
    parser.add_argument("--vo-dir", default="./voiceovers", help="Directory containing voiceover audio files (default: ./voiceovers)")
    parser.add_argument("--output-dir", default="./output", help="Directory to save assembled segments and master compilation")
    parser.add_argument("--target-res", default="1440p", choices=["1440p", "1080p", "4k"], help="Target resolution (default: 1440p = 2560x1440)")
    parser.add_argument("--aspect", default="crop", choices=["crop", "blur", "pad"], help="Aspect ratio mode (crop = fill screen, no black bars; blur = frosted ambient background)")
    parser.add_argument("--solo-sfx", type=float, default=10.0, help="Solo gameplay SFX duration in seconds after VO ends (default: 10.0s)")
    parser.add_argument("--duck-vol", type=float, default=0.18, help="Gameplay volume while VO is speaking (default: 0.18 = 18 percent)")
    parser.add_argument("--intro-vo", default=None, help="Path to intro voiceover file (auto-detected if in vo-dir)")
    parser.add_argument("--intro-clips-dir", default=None, help="Directory containing trailer clips for intro montage (defaults to clips-dir)")
    parser.add_argument("--intro-cut", type=float, default=3.5, help="Duration of each trailer cut in intro montage (default: 3.5s)")
    parser.add_argument("--no-title", action="store_true", help="Disable on-screen title card overlay")
    parser.add_argument("--gpu", action="store_true", help="Force Nvidia NVENC hardware acceleration")
    parser.add_argument("--cpu", action="store_true", help="Force CPU encoding (libx264)")

    args = parser.parse_args()

    # Fallback to ./downloads if ./download doesn't exist
    if not os.path.isdir(args.clips_dir) and os.path.isdir("./downloads"):
        args.clips_dir = "./downloads"

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
    print(f"[*] Clips Folder:         {os.path.abspath(args.clips_dir)}")
    print(f"[*] Voiceovers Folder:    {os.path.abspath(args.vo_dir)}")

    # Check whether to use auto-detect or markdown plan
    use_auto_detect = args.auto or (not args.plan) or (args.plan == "videoplan1.md")

    intro_vo_file = None
    items_to_assemble = []

    if use_auto_detect:
        print("\n[*] SCANNING FOLDERS FOR AUTO-DETECT...")
        detected_intro, detected_pairs = auto_detect_pairs(args.vo_dir, args.clips_dir)
        intro_vo_file = args.intro_vo or detected_intro

        # Filter pairs that have matching video clips
        for p in detected_pairs:
            items_to_assemble.append({
                "title": p["title"],
                "video_path": p["video_path"],
                "vo_path": p["vo_path"],
            })
    else:
        # Load from markdown plan
        if not os.path.isfile(args.plan):
            print(f"[!] Plan file not found: {args.plan}")
            sys.exit(1)

        with open(args.plan, "r", encoding="utf-8") as f:
            md_text = f.read()

        plan_items = parse_markdown_plan(md_text)
        intro_vo_file = args.intro_vo
        for idx, item in enumerate(plan_items, 1):
            title = item.title or f"Segment_{idx}"
            v_match = find_best_matching_clip(title, args.clips_dir)
            vo_candidates = glob.glob(os.path.join(args.vo_dir, "*"))
            vo_match = None
            for vc in vo_candidates:
                if title.lower() in os.path.basename(vc).lower():
                    vo_match = vc
                    break

            items_to_assemble.append({
                "title": title,
                "video_path": v_match,
                "vo_path": vo_match,
            })

    if not items_to_assemble and not intro_vo_file:
        print(f"\n[!] Error: No voiceover files found in {args.vo_dir}!")
        print(f"    Please place your .wav or .mp3 files into: {os.path.abspath(args.vo_dir)}")
        sys.exit(1)

    assembled_dir = os.path.join(args.output_dir, "assembled_segments")
    os.makedirs(assembled_dir, exist_ok=True)

    assembled_files = []
    chapters = []
    current_time = 0.0

    # -----------------------------------------------------------------------
    # Step 1: Assemble Intro Montage (if intro VO exists)
    # -----------------------------------------------------------------------
    if intro_vo_file and os.path.isfile(intro_vo_file):
        print("\n" + "-" * 75)
        print(f"[*] INTRO DETECTED: {os.path.basename(intro_vo_file)}")

        trailers_dir = args.intro_clips_dir or ("./trailers" if os.path.isdir("./trailers") else args.clips_dir)
        trailer_clips = glob.glob(os.path.join(trailers_dir, "*.mp4"))

        if trailer_clips:
            print(f"    Building rapid-cut intro montage from {len(trailer_clips)} clips in {trailers_dir}...")
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
                print(f"    [+] Intro montage ready ({intro_dur:.1f}s)")
            except Exception as e:
                print(f"    [!] Failed to assemble intro montage: {e}")
        else:
            print(f"    [!] No trailer clips found in {trailers_dir}. Skipping intro.")

    # -----------------------------------------------------------------------
    # Step 2: Assemble Countdown Segments
    # -----------------------------------------------------------------------
    print("\n" + "-" * 75)
    print(f"{'#':<4} {'Title':<28} {'Video Clip':<28} {'Voiceover File'}")
    print("-" * 75)

    valid_count = 0
    for idx, item in enumerate(items_to_assemble, 1):
        t_desc = item["title"][:26]
        v_desc = os.path.basename(item["video_path"])[:26] if item["video_path"] else "MISSING"
        vo_desc = os.path.basename(item["vo_path"])[:26] if item["vo_path"] else "MISSING"
        if item["video_path"] and item["vo_path"]:
            valid_count += 1
        print(f"{idx:<4} {t_desc:<28} {v_desc:<28} {vo_desc}")

    print("-" * 75)
    print(f"[*] Found {valid_count} ready pair(s) out of {len(items_to_assemble)} item(s).")

    for idx, item in enumerate(items_to_assemble, 1):
        title = item["title"]
        video_path = item["video_path"]
        vo_path = item["vo_path"]

        if not video_path:
            print(f"\n[!] Skipping #{idx} {title}: No matching video clip found in {args.clips_dir}")
            continue

        if not vo_path:
            print(f"\n[!] Skipping #{idx} {title}: Voiceover file missing")
            continue

        out_name = f"segment_{idx:02d}_{sanitize_filename(title)}.mp4"
        out_path = os.path.join(assembled_dir, out_name)

        print(f"\n[{idx}/{len(items_to_assemble)}] Assembling: {title}")
        print(f"    Video: {os.path.basename(video_path)}")
        print(f"    Audio: {os.path.basename(vo_path)}")

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
