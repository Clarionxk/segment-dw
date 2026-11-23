#!/usr/bin/env python3
"""
assemble.py — Automated 1440p Video Assembly Engine with Cinematic Transitions.

Key Features:
  1. Starts Directly With Games (Intro removed per user workflow):
     - No intro segment by default; starts immediately with the countdown.
  2. Multi-Clip Combination per Game:
     - Automatically combines ALL gameplay clips downloaded for each game
       (e.g. BioShock clip 1 + clip 2), extending the video to its full runtime!
  3. Cinematic Dip-to-Black Transitions:
     - Professional fade-in from black (0.5s) and fade-out to black (0.75s) on every segment.
     - Audio crossfades smoothly (afade in/out) — zero popping, clicking, or abrupt cuts.
  4. Perfect Voiceover & Audio Ducking:
     - Voiceover starts right as the video fades in from black.
     - Gameplay audio is ducked to 18% during narration, then smoothly swells to 100%.
     - Full gameplay audio and video play out to the very end of the clip!
  5. Acronym & Aliases Matching:
     - Automatically matches CoD4 <-> Call of Duty 4, TF2 <-> Team Fortress 2,
       UT3 <-> Unreal Tournament 3, MoH <-> Medal of Honor, ET <-> Enemy Territory, etc.
  6. 1440p Standard (2560x1440 @ 60fps):
     - Zero black bars via smart center-fill 16:9 crop.
     - Pure CPU (libx264 veryfast) execution on VPS with automatic GPU fallback.

Usage:
  # Assemble directly on VPS:
  python assemble.py --auto --cpu

  # Customize clips or vo folders:
  python assemble.py --auto --clips-dir download --vo-dir voiceovers --cpu
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

# Common gaming acronyms and aliases
ALIASES = {
    "cod": "call of duty",
    "cod4": "call of duty 4",
    "l4d": "left 4 dead",
    "tf2": "team fortress 2",
    "ut3": "unreal tournament 3",
    "ut2004": "unreal tournament 2004",
    "moh": "medal of honor",
    "airborne": "medal of honor",
    "et": "enemy territory",
    "hl2": "half life 2",
    "r6": "rainbow six",
    "vegas": "rainbow six vegas",
    "stalker": "s t a l k e r",
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


def check_nvenc_real_support() -> bool:
    """Test whether Nvidia NVENC hardware encoder actually works on current hardware."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=duration=0.1",
        "-c:v", "h264_nvenc",
        "-f", "null", "NUL" if os.name == "nt" else "/dev/null"
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
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


def normalize_name(text: str) -> str:
    """Normalize text expanding known aliases and removing punctuation."""
    t = re.sub(r"[^a-zA-Z0-9\s]", " ", text).lower()
    for short, full in ALIASES.items():
        t = re.sub(rf"\b{short}\b", full, t)
    return " ".join(t.split())


def match_score(vo_title: str, clip_filename: str) -> int:
    """Calculate match score between a voiceover title and a candidate clip filename."""
    vo_norm = normalize_name(vo_title)
    clip_norm = normalize_name(clip_filename)

    # Exact or prefix match
    if vo_norm in clip_norm or clip_norm.startswith(vo_norm):
        return 100

    vo_words = [w for w in vo_norm.split() if not w.isdigit() and len(w) > 2]
    vo_digits = [w for w in vo_norm.split() if w.isdigit()]

    clip_words = set([w for w in clip_norm.split() if not w.isdigit()])
    clip_digits = set([w for w in clip_norm.split() if w.isdigit()])

    # CRITICAL: Must match at least one non-digit keyword word!
    matched_words = [w for w in vo_words if w in clip_words]
    if not matched_words:
        return 0

    score = len(matched_words) * 15

    # Reward matching digits (e.g. Far Cry 2 vs Far Cry 1)
    for d in vo_digits:
        if d in clip_digits:
            score += 10
        else:
            score -= 10

    # Distinguish Warhead vs Crysis original
    if ("warhead" in vo_words) != ("warhead" in clip_words):
        score -= 30

    return score


def find_all_matching_clips(vo_title: str, clips_dir: str) -> List[str]:
    """Find all gameplay video clips matching a voiceover title (sorted to play in sequence)."""
    if not os.path.isdir(clips_dir):
        return []

    candidates = glob.glob(os.path.join(clips_dir, "*.mp4"))
    if not candidates:
        return []

    scored_clips = []
    for c in candidates:
        base = os.path.splitext(os.path.basename(c))[0]
        s = match_score(vo_title, base)
        if s >= 10:
            scored_clips.append((s, c))

    if not scored_clips:
        return []

    # Sort by match score descending
    scored_clips.sort(key=lambda x: -x[0])
    top_score = scored_clips[0][0]

    # Keep clips with high match relevance
    relevant = [c for s, c in scored_clips if s >= max(10, top_score - 15)]
    # Exclude trailers from countdown gameplay clips if regular clips exist
    gameplay_only = [c for c in relevant if not any(k in os.path.basename(c).lower() for k in ("trailer", "teaser", "cinematic"))]
    if gameplay_only:
        relevant = gameplay_only

    # Sort alphabetically so part1, part2, etc. play in natural sequence
    relevant.sort(key=lambda x: os.path.basename(x).lower())
    return relevant


def auto_detect_pairs(vo_dir: str, clips_dir: str, combine_clips: bool = True) -> List[Dict]:
    """Auto-detect voiceovers and pair with video clips (ignoring intro)."""
    pairs = []
    if not os.path.isdir(vo_dir):
        return []

    vo_files = []
    for ext in ("*.wav", "*.mp3", "*.m4a", "*.aac", "*.ogg"):
        vo_files.extend(glob.glob(os.path.join(vo_dir, ext)))

    vo_files = sorted(vo_files, key=lambda x: os.path.basename(x).lower())

    for vf in vo_files:
        base = os.path.splitext(os.path.basename(vf))[0]
        # Ignore intro files completely
        if "intro" in base.lower() or base.startswith("00") or base.startswith("0_"):
            continue

        matched_clips = find_all_matching_clips(base, clips_dir)
        if not combine_clips and len(matched_clips) > 1:
            # Pick longest single clip if combine_clips is disabled
            matched_clips = [max(matched_clips, key=lambda c: get_media_duration(c))]

        pairs.append({
            "title": base,
            "vo_path": vf,
            "video_paths": matched_clips,
        })

    return pairs


def build_video_filter(
    width: int,
    height: int,
    aspect_mode: str,
    total_duration: float,
    title_text: Optional[str],
    fade_in: float = 0.5,
    fade_out: float = 0.75,
) -> str:
    """Build FFmpeg video filter pipeline with cinematic dip-to-black transitions and zero black bars."""
    fps = 60
    fade_out_st = max(0.0, total_duration - fade_out)

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

    # Add smooth cinematic dip-to-black transitions
    if fade_in > 0:
        v_filter += f",fade=t=in:st=0:d={fade_in:.2f}"
    if fade_out > 0:
        v_filter += f",fade=t=out:st={fade_out_st:.2f}:d={fade_out:.2f}"

    clean_title = (title_text or "").replace("'", "").replace(":", " -")
    if clean_title and SYSTEM_FONT:
        # Title appears right after fade-in and holds for 5.5s
        v_filter += (
            f",drawtext=fontfile='{SYSTEM_FONT}':text='{clean_title}':x=80:y=h-140:fontsize=56:fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=12:enable='between(t,{fade_in:.2f},{fade_in + 5.5:.2f})'"
        )

    return v_filter


def assemble_segment(
    video_paths: List[str],
    vo_path: str,
    output_path: str,
    target_res: str = "1440p",
    aspect_mode: str = "crop",
    title_text: Optional[str] = None,
    duck_volume: float = 0.18,
    fade_in: float = 0.5,
    fade_out: float = 0.75,
    use_gpu: bool = False,
    show_title_card: bool = True,
) -> float:
    """
    Assemble a countdown segment:
      - Automatically stitches multiple gameplay clips together if available!
      - Starts with a smooth cinematic fade-in from black (0.5s).
      - Voiceover begins at 0.5s while game audio is ducked to 18%.
      - Game audio swells to 100% and plays full raw gameplay until the end.
      - Smoothly fades out to black (0.75s) at the end, transitioning to the next game!
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    width, height = RESOLUTIONS.get(target_res, (2560, 1440))

    vo_duration = get_media_duration(vo_path)

    # Calculate total duration of all combined video clips
    total_video_duration = sum(get_media_duration(vp) for vp in video_paths)
    total_duration = max(vo_duration + fade_in + 2.0, total_video_duration)
    fade_out_st = max(0.0, total_duration - fade_out)

    num_clips = len(video_paths)
    vo_input_idx = num_clips

    filter_parts = []

    # 1. Video and audio joining if multiple clips exist
    if num_clips == 1:
        v_source = "[0:v]"
        a_source = "[0:a]"
    else:
        concat_v = "".join(f"[{i}:v]" for i in range(num_clips))
        concat_a = "".join(f"[{i}:a]" for i in range(num_clips))
        filter_parts.append(f"{concat_v}concat=n={num_clips}:v=1:a=0[vcat]")
        filter_parts.append(f"{concat_a}concat=n={num_clips}:v=0:a=1[acat]")
        v_source = "[vcat]"
        a_source = "[acat]"

    # 2. Video pipeline: scaling, cropping, transitions, title
    v_filter = build_video_filter(
        width=width,
        height=height,
        aspect_mode=aspect_mode,
        total_duration=total_duration,
        title_text=title_text if show_title_card else None,
        fade_in=fade_in,
        fade_out=fade_out,
    )
    filter_parts.append(f"{v_source}{v_filter}[vout]")

    # 3. Audio pipeline: ducking, delay, swell, smooth crossfades
    vo_start = fade_in
    vo_end = vo_start + vo_duration
    pad_duration = max(0.0, total_duration - vo_end)

    a_filter = (
        f"{a_source}volume='if(lt(t,{vo_end:.2f}),{duck_volume:.2f},if(lt(t,{vo_end + 1.0:.2f}),{duck_volume:.2f}+{(1.0 - duck_volume):.2f}*(t-{vo_end:.2f}),1.0))':eval=frame,"
        f"atrim=duration={total_duration:.2f},asetpts=PTS-STARTPTS[aducked];"
        f"[{vo_input_idx}:a]adelay={int(vo_start * 1000)}|{int(vo_start * 1000)},volume=1.0,apad=pad_dur={pad_duration:.2f},atrim=duration={total_duration:.2f},asetpts=PTS-STARTPTS[avo];"
        f"[aducked][avo]amix=inputs=2:duration=first:dropout_transition=2,"
        f"afade=t=in:st=0:d={fade_in:.2f},afade=t=out:st={fade_out_st:.2f}:d={fade_out:.2f},"
        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[aout]"
    )
    filter_parts.append(a_filter)

    filter_complex = ";".join(filter_parts)

    def build_cmd(gpu: bool) -> List[str]:
        cmd = ["ffmpeg", "-y"]
        for vp in video_paths:
            cmd.extend(["-i", vp])
        cmd.extend(["-i", vo_path])
        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "[aout]",
            "-pix_fmt", "yuv420p",
            "-colorspace", "bt709",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
        ])
        if gpu:
            cmd.extend(["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22"])
        else:
            cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])
        cmd.extend(["-c:a", "aac", "-b:a", "256k", "-ar", "48000", output_path])
        return cmd

    if use_gpu:
        res = subprocess.run(build_cmd(gpu=True), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0:
            return total_duration

    # CPU Execution
    res = subprocess.run(build_cmd(gpu=False), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode == 0:
        return total_duration

    # Fallback without drawtext if font error occurred
    if "drawtext" in filter_complex:
        v_filter_fallback = build_video_filter(width=width, height=height, aspect_mode=aspect_mode, total_duration=total_duration, title_text=None, fade_in=fade_in, fade_out=fade_out)
        filter_parts_fb = list(filter_parts)
        filter_parts_fb[-2] = f"{v_source}{v_filter_fallback}[vout]"
        cmd_fb = ["ffmpeg", "-y"]
        for vp in video_paths:
            cmd_fb.extend(["-i", vp])
        cmd_fb.extend(["-i", vo_path])
        cmd_fb.extend([
            "-filter_complex", ";".join(filter_parts_fb),
            "-map", "[vout]", "-map", "[aout]",
            "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "256k", "-ar", "48000", output_path
        ])
        res = subprocess.run(cmd_fb, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0:
            return total_duration

    raise RuntimeError(f"FFmpeg assembly failed: {res.stderr[-400:]}")


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
        description="Automated 1440p Video Assembly Engine with Cinematic Transitions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("plan", nargs="?", default=None, help="Optional path to markdown plan file (leave empty for auto-detect)")
    parser.add_argument("--auto", action="store_true", help="Force auto-pairing of voiceovers and clips by filename without markdown plan")
    parser.add_argument("--clips-dir", default="./download", help="Directory containing downloaded gameplay video clips (default: ./download)")
    parser.add_argument("--vo-dir", default="./voiceovers", help="Directory containing voiceover audio files (default: ./voiceovers)")
    parser.add_argument("--output-dir", default="./output", help="Directory to save assembled segments and master compilation")
    parser.add_argument("--target-res", default="1440p", choices=["1440p", "1080p", "4k"], help="Target resolution (default: 1440p = 2560x1440)")
    parser.add_argument("--aspect", default="crop", choices=["crop", "blur", "pad"], help="Aspect ratio mode (crop = fill screen, no black bars; blur = frosted ambient background)")
    parser.add_argument("--duck-vol", type=float, default=0.18, help="Gameplay volume while VO is speaking (default: 0.18 = 18 percent)")
    parser.add_argument("--single-clip", action="store_true", help="Only use the single longest clip per game instead of combining all clips")
    parser.add_argument("--fade-in", type=float, default=0.5, help="Fade-in duration in seconds from black at segment start (default: 0.5s)")
    parser.add_argument("--fade-out", type=float, default=0.75, help="Fade-out duration in seconds to black at segment end (default: 0.75s)")
    parser.add_argument("--no-title", action="store_true", help="Disable on-screen title card overlay")
    parser.add_argument("--gpu", action="store_true", help="Force Nvidia NVENC hardware acceleration")
    parser.add_argument("--cpu", action="store_true", help="Force CPU encoding (libx264) - recommended for VPS")

    args = parser.parse_args()

    if not os.path.isdir(args.clips_dir) and os.path.isdir("./downloads"):
        args.clips_dir = "./downloads"

    print("\n" + "=" * 75)
    print("   BITRATE BACKTRACK — 1440P COMPILATION ENGINE WITH CINEMATIC TRANSITIONS   ")
    print("=" * 75)

    use_gpu = False
    if args.gpu and not args.cpu:
        if check_nvenc_real_support():
            use_gpu = True
        else:
            print("[!] Warning: Nvidia GPU hardware not detected on this machine. Using CPU (libx264).")
    elif not args.cpu:
        use_gpu = check_nvenc_real_support()

    combine_clips = not args.single_clip

    print(f"[*] Target Resolution:    {args.target_res} ({RESOLUTIONS[args.target_res][0]}x{RESOLUTIONS[args.target_res][1]} @ 60fps)")
    print(f"[*] Aspect Ratio Mode:    {args.aspect.upper()} (Zero Black Bars)")
    print(f"[*] Transitions:          Cinematic Dip-to-Black ({args.fade_in}s Fade-In / {args.fade_out}s Fade-Out)")
    print(f"[*] Video Encoder:        {'Nvidia NVENC (GPU Accelerated)' if use_gpu else 'CPU (libx264 veryfast)'}")
    print(f"[*] Audio Duck Volume:    {int(args.duck_vol * 100)}% during voiceover (100% full volume afterwards)")
    print(f"[*] Multi-Clip Stitched:  {'YES (Combining all clips for each game)' if combine_clips else 'NO (Single longest clip)'}")
    print(f"[*] Intro Segment:        SKIPPED (Starting directly with games)")
    print(f"[*] Clips Folder:         {os.path.abspath(args.clips_dir)}")
    print(f"[*] Voiceovers Folder:    {os.path.abspath(args.vo_dir)}")

    use_auto_detect = args.auto or (not args.plan) or (args.plan == "videoplan1.md")
    items_to_assemble = []

    if use_auto_detect:
        print("\n[*] SCANNING FOLDERS FOR AUTO-DETECT...")
        items_to_assemble = auto_detect_pairs(args.vo_dir, args.clips_dir, combine_clips=combine_clips)
    else:
        if not os.path.isfile(args.plan):
            print(f"[!] Plan file not found: {args.plan}")
            sys.exit(1)

        with open(args.plan, "r", encoding="utf-8") as f:
            md_text = f.read()

        plan_items = parse_markdown_plan(md_text)
        for idx, item in enumerate(plan_items, 1):
            title = item.title or f"Segment_{idx}"
            v_matches = find_all_matching_clips(title, args.clips_dir)
            if not combine_clips and len(v_matches) > 1:
                v_matches = [max(v_matches, key=lambda c: get_media_duration(c))]

            vo_candidates = glob.glob(os.path.join(args.vo_dir, "*"))
            vo_match = None
            for vc in vo_candidates:
                if title.lower() in os.path.basename(vc).lower():
                    vo_match = vc
                    break

            items_to_assemble.append({
                "title": title,
                "video_paths": v_matches,
                "vo_path": vo_match,
            })

    if not items_to_assemble:
        print(f"\n[!] Error: No voiceover files found in {args.vo_dir}!")
        sys.exit(1)

    assembled_dir = os.path.join(args.output_dir, "assembled_segments")
    os.makedirs(assembled_dir, exist_ok=True)

    assembled_files = []
    chapters = []
    current_time = 0.0

    # -----------------------------------------------------------------------
    # Assemble Countdown Segments Directly (No Intro)
    # -----------------------------------------------------------------------
    print("\n" + "-" * 75)
    print(f"{'#':<4} {'Title':<26} {'Clips Found':<18} {'Voiceover File'}")
    print("-" * 75)

    valid_count = 0
    total_est_seconds = 0.0
    for idx, item in enumerate(items_to_assemble, 1):
        t_desc = item["title"][:24]
        clip_count = len(item.get("video_paths", []))
        if clip_count > 0:
            c_desc = f"{clip_count} clip(s)"
        else:
            c_desc = "MISSING"

        vo_desc = os.path.basename(item["vo_path"])[:24] if item.get("vo_path") else "MISSING"
        if clip_count > 0 and item.get("vo_path"):
            valid_count += 1
            for vp in item["video_paths"]:
                try:
                    total_est_seconds += get_media_duration(vp)
                except Exception:
                    pass

        print(f"{idx:<4} {t_desc:<26} {c_desc:<18} {vo_desc}")

    print("-" * 75)
    print(f"[*] Found {valid_count} ready game(s). Estimated Full Runtime: ~{format_time_str(total_est_seconds)}")

    for idx, item in enumerate(items_to_assemble, 1):
        title = item["title"]
        video_paths = item.get("video_paths", [])
        vo_path = item.get("vo_path")

        if not video_paths:
            print(f"\n[!] Skipping #{idx} {title}: No matching video clips found in {args.clips_dir}")
            continue

        if not vo_path:
            print(f"\n[!] Skipping #{idx} {title}: Voiceover file missing")
            continue

        out_name = f"segment_{idx:02d}_{sanitize_filename(title)}.mp4"
        out_path = os.path.join(assembled_dir, out_name)

        print(f"\n[{idx}/{len(items_to_assemble)}] Assembling: {title}")
        print(f"    Clips ({len(video_paths)}): {', '.join(os.path.basename(vp) for vp in video_paths)}")
        print(f"    Narration (VO):   {os.path.basename(vo_path)}")

        try:
            dur = assemble_segment(
                video_paths=video_paths,
                vo_path=vo_path,
                output_path=out_path,
                target_res=args.target_res,
                aspect_mode=args.aspect,
                title_text=title,
                duck_volume=args.duck_vol,
                fade_in=args.fade_in,
                fade_out=args.fade_out,
                use_gpu=use_gpu,
                show_title_card=not args.no_title,
            )

            chapters.append(f"{format_time_str(current_time)} - {title}")
            current_time += dur
            assembled_files.append(out_path)
            print(f"    [+] 1440p Segment ready ({dur:.1f}s): {out_name} (with dip-to-black transition)")

        except Exception as e:
            print(f"    [!] Assembly error for {title}: {e}")

    if not assembled_files:
        print("\n[!] No segments were assembled. Please ensure both video clips and voiceovers exist.")
        return

    # -----------------------------------------------------------------------
    # Step 2: Lossless Master Concatenation
    # -----------------------------------------------------------------------
    print("\n" + "=" * 75)
    print(f"[*] Merging {len(assembled_files)} full segments into Master Video...")
    master_output = os.path.join(args.output_dir, "master_compilation.mp4")

    try:
        concat_segments(assembled_files, master_output)
        print(f"[+] SUCCESS! Master Compilation Saved: {os.path.abspath(master_output)}")
        size_mb = os.path.getsize(master_output) / (1024 * 1024)
        print(f"    Total Video Runtime: {format_time_str(current_time)} | File Size: {size_mb:.1f} MB")
        print(f"    Format: 1440p (2560x1440 @ 60fps) | Aspect: {args.aspect.upper()}")
        print(f"    Transitions: Smooth Cinematic Dip-to-Black between all games")
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
