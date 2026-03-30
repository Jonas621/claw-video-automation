#!/usr/bin/env python3
"""TikTok upload helper — called by mac_api.py via subprocess.

Runs inside .venv-tiktok (Python 3.12 + tiktokautouploader + phantomwright).

Uses tiktokautouploader which includes:
  - Automatic captcha solving (built-in ML model)
  - Bot detection evasion via Phantomwright (patched Playwright)
  - Per-account cookie/session persistence

Usage:
    .venv-tiktok/bin/python bin/tiktok_upload.py \
        --video /path/to/video.mp4 \
        --description "caption #hashtag" \
        --account mytiktokaccount \
        [--headless] [--schedule HH:MM] [--day N]

First run per account: browser opens for manual TikTok login (stored for reuse).
Subsequent runs: fully automatic, headless.

Exits 0 on success, 1 on failure.  Prints JSON result to stdout.
"""

import argparse
import json
import sys
import traceback


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload a video to TikTok")
    parser.add_argument("--video", required=True, help="Path to video file")
    parser.add_argument("--description", required=True, help="Caption text including hashtags")
    parser.add_argument("--account", required=True, help="TikTok account name (for session persistence)")
    parser.add_argument("--headless", action="store_true", default=True, help="Run headless (default)")
    parser.add_argument("--no-headless", action="store_true", help="Show browser window")
    parser.add_argument("--schedule", default=None, help="Schedule time HH:MM (optional)")
    parser.add_argument("--day", type=int, default=None, help="Schedule day 1-10 (optional)")
    parser.add_argument("--hashtags", default=None, help="Comma-separated hashtags (optional)")
    parser.add_argument("--copyrightcheck", action="store_true", help="Run copyright check before upload")
    args = parser.parse_args()

    headless = not args.no_headless

    # Suppress all print output from the library (we only want JSON on stdout)
    suppress = True

    try:
        from tiktokautouploader import upload_tiktok

        hashtags = None
        if args.hashtags:
            hashtags = [h.strip() for h in args.hashtags.split(",") if h.strip()]

        kwargs = dict(
            video=args.video,
            description=args.description,
            accountname=args.account,
            headless=headless,
            stealth=True,
            suppressprint=suppress,
        )

        if hashtags:
            kwargs["hashtags"] = hashtags
        if args.schedule:
            kwargs["schedule"] = args.schedule
        if args.day is not None:
            kwargs["day"] = args.day
        if args.copyrightcheck:
            kwargs["copyrightcheck"] = True

        upload_tiktok(**kwargs)

        result = {
            "ok": True,
            "video": args.video,
            "account": args.account,
            "description": args.description,
        }
        print(json.dumps(result))
        sys.exit(0)

    except Exception as e:
        result = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
        print(json.dumps(result))
        sys.exit(1)


if __name__ == "__main__":
    main()
