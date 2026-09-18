#!/usr/bin/env python3
"""
generate.py — fast CLI image generator for recraft.ai (session replay).

Thin CLI over recraft_core (shared with the Gradio web app). Reads
`recraft_session.json` (created by setup_session.py) — or any session
file you point it at — and replays the verified flow with plain HTTP:

    refresh JWT -> create -> poll operation -> crisp upscale 4096x4096
    -> download webp

Examples
--------
    python generate.py "a red fox in snow, cinematic"
    python generate.py "logo for a coffee shop" -n 4 --width 1024 --height 1024
    python generate.py "portrait" --negative "blurry, text" --out my_images
    python generate.py "quick draft" --no-upscale     # save credits
    python generate.py --check                        # session still live?

NOTE: uses your own logged-in session and spends your account's credits.
Automating private endpoints may violate recraft.ai's Terms of Service.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from recraft_core import (DEFAULT_SESSION_FILE, NoCredits, RecraftClient,
                          RecraftError)


def main():
    ap = argparse.ArgumentParser(
        description="Fast recraft.ai image generation via captured session.")
    ap.add_argument("prompt", nargs="?", help="text prompt")
    ap.add_argument("-n", "--count", type=int, default=1,
                    help="number of separate generations (default 1)")
    ap.add_argument("--negative", default="", help="negative prompt")
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--no-upscale", action="store_true",
                    help="skip the automatic Recraft Crisp 4096x4096 upscale")
    ap.add_argument("--out", default="images", help="output directory")
    ap.add_argument("--session", default=str(DEFAULT_SESSION_FILE),
                    help="session json file (default recraft_session.json)")
    ap.add_argument("--max-wait", type=int, default=180,
                    help="seconds to wait per image (default 180)")
    ap.add_argument("--check", action="store_true",
                    help="verify the session and show credits, then exit")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    sess_path = Path(args.session)
    if not sess_path.exists():
        sys.exit(f"[X] {sess_path} not found. Run setup_session.py first.")
    session = json.loads(sess_path.read_text(encoding="utf-8"))
    client = RecraftClient(session, sess_path)
    vlog = print if args.verbose else (lambda *_: None)

    if args.check:
        client.refresh_token(vlog)
        try:
            info = client.credits()
            print(f"[OK] session live — {info['email']} | plan {info['plan']} "
                  f"| {info['credits']} credits (+{info['extra_credits']} "
                  f"extra)")
        except RecraftError as e:
            sys.exit(f"[X] {e} — re-run: python setup_session.py")
        return

    if not args.prompt:
        ap.error("a prompt is required (or use --check)")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    upscale = not args.no_upscale
    total = 0
    for i in range(1, args.count + 1):
        if args.count > 1:
            print(f"\n=== generation {i}/{args.count} ===")
        stamp = int(time.time())
        dest = out_dir / f"{i:02d}_{stamp}.webp"
        try:
            res = client.generate(args.prompt, dest,
                                  width=args.width, height=args.height,
                                  negative=args.negative, seed=args.seed,
                                  upscale=upscale, max_wait=args.max_wait,
                                  log=vlog)
        except NoCredits as e:
            sys.exit(f"[X] out of credits: {e}")
        except RecraftError as e:
            sys.exit(f"[X] {e}")
        total += 1
        print(f"[saved] {res['path']}"
              + (" (4096x4096 crisp-upscaled)" if res["upscaled"] else
                 " (native resolution)"))
        if i < args.count:
            time.sleep(1)

    print(f"\nDone — {total} image(s) in {out_dir.resolve()}")


if __name__ == "__main__":
    main()
