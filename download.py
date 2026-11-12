#!/usr/bin/env python3
"""
download.py — Overhauled Video & Segment Downloader for Bitrate Backtrack.

Downloads videos and segmented clips defined in Markdown files.
Designed for local development and headless VPS deployment.

Usage:
  # Download from videoplan1.md (default)
  python download.py

  # Download from specific Markdown plan file
  python download.py my_plan.md

  # Preview targets without downloading (Dry Run)
  python download.py videoplan1.md --dry-run

  # Specify custom output folder and resolution
  python download.py videoplan1.md --output /media/clips --res 1080p

  # Use cookies to bypass YouTube bot detection on VPS
  python download.py videoplan1.md --cookies cookies.txt
"""

import os
import sys
import time
import argparse
from typing import List, Optional

from core.parser import parse_markdown_plan, PlanItem
from core.downloader import (
    download_media,
    parse_time_str,
    format_time_str,
    validate_url,
    find_ffmpeg,
)


def find_default_plan() -> Optional[str]:
    """Find default markdown plan file in the current directory."""
    if os.path.isfile("videoplan1.md"):
        return "videoplan1.md"
    md_files = [f for f in os.listdir(".") if f.endswith(".md") and f.lower() != "readme.md"]
    if md_files:
        return md_files[0]
    return None


def print_banner():
    banner = """
======================================================================
           BITRATE BACKTRACK — VIDEO & SEGMENT DOWNLOADER             
======================================================================
"""
    print(banner.strip())


def display_plan_summary(items: List[PlanItem], default_res: str):
    print(f"\n[*] Found {len(items)} download target(s):")
    print("-" * 75)
    print(f"{'#':<4} {'Title / Clip Name':<30} {'Range':<18} {'Res':<8} {'URL'}")
    print("-" * 75)
    for idx, item in enumerate(items, 1):
        name = (item.title or "Untitled")[:28]
        if item.start or item.end:
            time_range = f"{item.start or '00:00'} -> {item.end or 'End'}"
        else:
            time_range = "Full Video"
        res = item.resolution or default_res
        short_url = item.url[:35] + "…" if len(item.url) > 35 else item.url
        print(f"{idx:<4} {name:<30} {time_range:<18} {res:<8} {short_url}")
    print("-" * 75)


def main():
    parser = argparse.ArgumentParser(
        description="Download YouTube videos and time-stamped segments from a Markdown plan file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "plan",
        nargs="?",
        default=None,
        help="Path to Markdown plan file (defaults to videoplan1.md or first .md file found).",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="./downloads",
        help="Directory to save downloaded videos and segments (default: ./downloads).",
    )
    parser.add_argument(
        "-r",
        "--res",
        "--resolution",
        default="1080p",
        choices=["best", "1080p", "720p", "480p", "360p"],
        help="Default video resolution if not specified in markdown (default: 1080p).",
    )
    parser.add_argument(
        "-c",
        "--cookies",
        default=None,
        help="Path to cookies.txt file for authenticating on VPS datacenter IPs.",
    )
    parser.add_argument(
        "--precise",
        action="store_true",
        help="Re-encode frames at cuts for exact precision (slower). Default is fast keyframe stream-copy.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Force re-download even if the output file already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse the markdown file and display all detected targets without downloading.",
    )

    args = parser.parse_args()
    print_banner()

    # Determine plan file
    plan_path = args.plan or find_default_plan()
    if not plan_path or not os.path.isfile(plan_path):
        print(f"\n[!] Error: Plan file not found: {plan_path or 'No .md file specified'}")
        print("    Usage: python download.py <videoplan.md>")
        print("    Please paste your video list into videoplan1.md or provide a file path.")
        sys.exit(1)

    print(f"\n[*] Reading plan file: {os.path.abspath(plan_path)}")
    with open(plan_path, "r", encoding="utf-8") as f:
        md_content = f.read()

    items = parse_markdown_plan(md_content)
    if not items:
        print("[!] No video URLs found in markdown plan!")
        print("    Ensure your plan includes YouTube URLs in tables, key-value lists, or markdown links.")
        sys.exit(1)

    display_plan_summary(items, args.res)

    if args.dry_run:
        print("\n[i] Dry run complete. No files were downloaded.")
        return

    # Check FFmpeg
    ffmpeg_dir = find_ffmpeg()
    if ffmpeg_dir:
        print(f"[*] FFmpeg detected: {ffmpeg_dir}")
    else:
        print("[!] Warning: FFmpeg not detected in PATH. Audio/video merging or segment cutting may fail.")
        print("    On VPS (Ubuntu/Debian), run: sudo apt install -y ffmpeg")

    # Check cookies
    cookies_file = args.cookies
    if not cookies_file and os.path.isfile("cookies.txt"):
        cookies_file = "cookies.txt"
        print(f"[*] Auto-detected cookies file: {cookies_file}")
    elif cookies_file:
        if os.path.isfile(cookies_file):
            print(f"[*] Using cookies file: {cookies_file}")
        else:
            print(f"[!] Warning: Specified cookies file not found: {cookies_file}")

    print(f"[*] Output directory: {os.path.abspath(args.output)}")
    print(f"[*] Cut mode: {'Precise re-encoding' if args.precise else 'Fast stream-copy (byte-seeking)'}")
    print("\n" + "=" * 75)

    success_count = 0
    skipped_count = 0
    failed_count = 0
    start_time = time.time()

    for idx, item in enumerate(items, 1):
        display_name = item.title or f"Item_{idx}"
        target_res = item.resolution or args.res

        start_sec = parse_time_str(item.start)
        end_sec = parse_time_str(item.end)

        range_desc = f"{item.start or '00:00'} to {item.end or 'End'}" if (start_sec or end_sec) else "Full Video"
        print(f"\n[{idx}/{len(items)}] Processing: {display_name}")
        print(f"    URL:        {item.url}")
        print(f"    Range:      {range_desc}")
        print(f"    Resolution: {target_res}")

        if not validate_url(item.url):
            print(f"    [!] Error: Invalid YouTube URL: {item.url}")
            failed_count += 1
            continue

        def progress_cb(pct: float, msg: str):
            bar_len = 25
            filled = int(round(bar_len * pct))
            bar = "=" * filled + "-" * (bar_len - filled)
            print(f"\r    [{bar}] {pct*100:5.1f}% | {msg[:35]:<35}", end="", flush=True)

        try:
            res = download_media(
                url=item.url,
                output_dir=args.output,
                title=item.title,
                start_sec=start_sec,
                end_sec=end_sec,
                resolution=target_res,
                cookies_file=cookies_file,
                precise_cut=args.precise,
                overwrite=args.overwrite,
                progress_callback=progress_cb,
            )

            status = res.get("status")
            if status == "skipped":
                print(f"\n    [SKIP] Already exists: {os.path.basename(res['file_path'])}")
                skipped_count += 1
            else:
                size_mb = res.get("file_size", 0) / (1024 * 1024)
                print(f"\n    [+] Saved: {os.path.basename(res['file_path'])} ({size_mb:.1f} MB)")
                success_count += 1

        except Exception as e:
            print(f"\n    [!] Failed to download {display_name}: {e}")
            failed_count += 1

    elapsed = time.time() - start_time
    print("\n" + "=" * 75)
    print(f"DOWNLOAD BATCH COMPLETED in {elapsed:.1f}s")
    print(f"  • Succeeded: {success_count}")
    print(f"  • Skipped:   {skipped_count}")
    print(f"  • Failed:    {failed_count}")
    print(f"  • Total:     {len(items)}")
    print(f"  • Output:    {os.path.abspath(args.output)}")
    print("=" * 75 + "\n")


if __name__ == "__main__":
    main()

