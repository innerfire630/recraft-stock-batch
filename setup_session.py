#!/usr/bin/env python3
"""
setup_session.py — one-time recraft.ai authentication + endpoint sniffer.

Thin CLI over recraft_core.capture_session(). Opens a stealth browser
(your installed Chrome/Edge when found, so Cloudflare Turnstile passes
silently — it never solves interactive challenges for you). You just log
in; the sniffer detects the login and saves a replayable session file
built from the verified create/poll/upscale endpoints — no test image,
no credits spent.

Usage
-----
    python setup_session.py                      # -> recraft_session.json
    python setup_session.py --name alt1          # -> sessions/alt1.json
    python setup_session.py --from-capture       # rebuild session from an
                                                 # existing sniffer_capture.json

NOTE: the session file contains your live login cookies. It is
git-ignored. Treat it like a password. Automating recraft.ai's private
web endpoints may violate their Terms of Service — your call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import recraft_core
from recraft_core import (DEFAULT_SESSION_FILE, START_URL,
                          SESSIONS_DIR, build_session, capture_session)

RAW_FILE = Path("sniffer_capture.json")
LEGACY_PROFILE = Path(".pw-profile")


def from_capture() -> None:
    """Recovery: rebuild a session file from an existing raw capture."""
    if not RAW_FILE.exists():
        sys.exit(f"[X] {RAW_FILE} not found — run the capture first.")
    records = json.loads(RAW_FILE.read_text(encoding="utf-8"))
    storage, user_agent = {"cookies": [], "origins": []}, "Mozilla/5.0"
    src = DEFAULT_SESSION_FILE
    if not src.exists() and (SESSIONS_DIR / "main.json").exists():
        src = SESSIONS_DIR / "main.json"
    if src.exists():
        old = json.loads(src.read_text(encoding="utf-8"))
        storage["cookies"] = old.get("cookies", [])
        user_agent = old.get("user_agent", user_agent)
    print(f"[*] rebuilding session from {RAW_FILE} ({len(records)} records)")
    session = build_session(records, storage, user_agent)
    src.write_text(json.dumps(session, indent=2), encoding="utf-8")
    print(f"[OK] Session saved to {src}")


def main():
    argv = sys.argv[1:]
    if "--from-capture" in argv:
        from_capture()
        return

    name = None
    if "--name" in argv:
        try:
            name = argv[argv.index("--name") + 1]
        except IndexError:
            sys.exit("[X] --name requires a value")
    out = (SESSIONS_DIR / f"{name}.json") if name else DEFAULT_SESSION_FILE
    if name:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        if out.exists():
            sys.exit(f"[X] {out} already exists — delete it to re-capture, "
                     f"or omit --name for the default session file.")
    profile = (SESSIONS_DIR / f".profile_{name}") if name else LEGACY_PROFILE

    print(f"""
======================================================================
 RECRAFT SESSION CAPTURE  ->  {out}
======================================================================
 1. A browser window (your installed Chrome/Edge if found) opens at
    {START_URL}
 2. Log in to recraft.ai (your own account). Cloudflare should pass
    silently; if a Turnstile CHECKBOX appears, click it yourself.
 3. That's it — the capture finishes automatically once login is
    detected (no test image, no credits spent). In CLI mode you can
    also just press ENTER after logging in.
======================================================================
""")
    try:
        session = capture_session(profile, log=print)
    except Exception as e:
        print(f"\n[X] capture failed: {e}\n"
              f"    (raw traffic may still be in {RAW_FILE}; try "
              f"'python setup_session.py --from-capture' after fixing)")
        sys.exit(1)

    out.write_text(json.dumps(session, indent=2), encoding="utf-8")
    print(f"""
[OK] Session saved to {out}  ({out.stat().st_size:,} bytes)
     create : {session['create']['method']} {session['create']['url']}
     poll   : {session.get('poll', {}).get('url', '(none)')}
     upscale: {session.get('upscale', {}).get('url', '(none)')}

Next:  python generate.py "a red fox in snow, cinematic" -n 2
   or:  python app.py    (web UI)
""")


if __name__ == "__main__":
    main()
