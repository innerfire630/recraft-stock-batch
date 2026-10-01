#!/usr/bin/env python3
"""
server.py — production entry point for hosting this app (aaPanel, VPS, LAN).

Why this exists instead of `python app.py`:

* `app.py` hardcodes 127.0.0.1 (fine at a desk, unreachable from a server)
  and opens a browser.
* `server.py` exposes a plain ASGI app so you can put a real reverse proxy
  (nginx via aaPanel) and a process supervisor (systemd / aaPanel's Supervisor)
  in front of it, which is how a server deployment is supposed to look.

Configure entirely through environment variables (see .env.example):

    RECRAFT_HOST        bind address              default 127.0.0.1
    RECRAFT_PORT        bind port                 default 7860
    RECRAFT_ROOT_PATH   public URL prefix          default ""   e.g. /recraft
    RECRAFT_AUTH        1/true to require login    default 1 on non-Windows
    RECRAFT_USER        username                   default recraft
    RECRAFT_PASSWORD    password  (REQUIRED if auth on — startup aborts)
    RECRAFT_MAX_UPLOAD  max upload size in MB      default 200
    RECRAFT_SSR         0 to disable SSR           default 1

Run it:
    uvicorn server:app --host 127.0.0.1 --port 7860 --workers 1

Use ONE worker. The batch runner, the account pool and the capture status
all live in module-level memory, so two workers would each keep their own
copy and the UI would show stale/duplicated state.

NOTE: this app replays your logged-in recraft.ai session over private API
endpoints, which is likely against their Terms of Service. You run it at your
own risk, with your own accounts, at human-like rates.
"""

from __future__ import annotations

import os
import secrets
import sys

# Gradio phones home for usage analytics unless told not to. This is a
# self-hosted tool with real credentials in play — opt out before Gradio is
# imported anywhere.
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

# Never auto-open a browser on a server.
os.environ.setdefault("GRADIO_LAUNCH_IN_BROWSER", "False")

# ---------------------------------------------------------------------------
# Read config BEFORE importing app, so Gradio picks up the same environment.
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _load_dotenv() -> None:
    """Minimal .env reader (no python-dotenv dependency on the server)."""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Real environment variables always win over the file.
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

HOST = os.environ.get("RECRAFT_HOST", "127.0.0.1").strip()
PORT = int(os.environ.get("RECRAFT_PORT", "7860"))
ROOT_PATH = (os.environ.get("RECRAFT_ROOT_PATH", "") or "").strip().rstrip("/")
USER = os.environ.get("RECRAFT_USER", "recraft").strip() or "recraft"
PASSWORD = os.environ.get("RECRAFT_PASSWORD", "").strip()
MAX_UPLOAD_MB = int(os.environ.get("RECRAFT_MAX_UPLOAD", "200"))
# Default to ON everywhere except Windows, so a local double-click of app.py
# never starts demanding a password.
AUTH = _env_flag("RECRAFT_AUTH", os.name != "nt")
SSR = _env_flag("RECRAFT_SSR", True)

if AUTH and not PASSWORD:
    sys.exit(
        "FATAL: RECRAFT_AUTH is on but RECRAFT_PASSWORD is empty.\n"
        "       Set RECRAFT_PASSWORD in .env (or the environment) — this app "
        "holds live\n"
        "       recraft.ai session cookies and spends your credits, so it "
        "must not be\n"
        "       reachable without a password. To run without a password on a "
        "trusted\n"
        "       network only, set RECRAFT_AUTH=0."
    )

# ---------------------------------------------------------------------------
# Now import the Gradio UI.
# ---------------------------------------------------------------------------
import gradio as gr  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

from app import demo  # noqa: E402

# Files the UI is allowed to serve back to the browser (downloads, previews).
# Gradio 5+ blocks everything outside the CWD by default, and these live
# outside it when the app is started from a systemd unit.
ALLOWED_PATHS = [
    os.path.join(BASE_DIR, "output"),
    os.path.join(BASE_DIR, "images"),
]

AUTH_ARG: object = (USER, PASSWORD) if AUTH else None

# Gradio 6 mounts into an existing FastAPI app, so we create a parent and
# mount the UI onto it. That also gives us a health endpoint for monitoring.
parent = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@parent.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    """Liveness probe. Reports which recraft.ai session files are on disk —
    NOT whether they are still valid (use the UI's Refresh Credits for that)."""
    sessions_dir = os.path.join(BASE_DIR, "sessions")
    try:
        found = sorted(f[:-5] for f in os.listdir(sessions_dir)
                       if f.endswith(".json") and not f.startswith("_"))
    except OSError:
        found = []
    return {"status": "ok", "accounts": found}


app = gr.routes.mount_gradio_app(
    parent,
    demo,
    # Starlette requires "" or a leading "/"; it strips the trailing slash
    # itself. ROOT_PATH is stored without one, so re-add the leading slash.
    path="/" + ROOT_PATH.lstrip("/") if ROOT_PATH else "/",
    auth=AUTH_ARG,  # type: ignore[arg-type]
    auth_message="Recraft Stock Generator — sign in to continue.",
    root_path=ROOT_PATH or None,
    allowed_paths=ALLOWED_PATHS,
    max_file_size=f"{MAX_UPLOAD_MB}MB",
    ssr_mode=SSR,
    # This app spends real credits and holds session cookies: never let the
    # UI be indexed, and keep the dev "settings"/debug affordances quiet.
    show_error=False,
)


# ---------------------------------------------------------------------------
# Hardening: Gradio/FastAPI serve some introspection routes by default.
# The UI has no need for them, so remove them.
# ---------------------------------------------------------------------------
for path in ("/docs", "/redoc", "/openapi.json"):
    try:
        app.router.routes = [r for r in app.router.routes
                             if getattr(r, "path", None) != path]
    except Exception:  # pragma: no cover - never block startup on this
        pass

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[h.strip() for h in
                   os.environ.get("RECRAFT_ALLOWED_HOSTS", "*").split(",")
                   if h.strip()] or ["*"],
)


def _log_banner() -> None:
    print("=" * 66, file=sys.stderr)
    print("  Recraft Stock Generator — server mode", file=sys.stderr)
    print(f"  bind        : {HOST}:{PORT}", file=sys.stderr)
    print(f"  public path : {ROOT_PATH or '/'}", file=sys.stderr)
    print(f"  auth        : {'ON' if AUTH else 'OFF  <-- no password!'}"
          + (f" (user '{USER}')" if AUTH else ""), file=sys.stderr)
    print(f"  upload limit: {MAX_UPLOAD_MB} MB", file=sys.stderr)
    print(f"  workers     : 1 (required — in-process batch/pool state)",
          file=sys.stderr)
    print("=" * 66, file=sys.stderr)


if __name__ == "__main__":
    # Uvicorn is only needed here; the systemd unit imports `server:app`.
    import uvicorn

    _log_banner()
    if AUTH and PASSWORD in ("change-me", "changeme"):
        print("WARNING: you are using the example password. Change it in "
              ".env now.", file=sys.stderr)
    uvicorn.run(
        "server:app" if ROOT_PATH else app,
        host=HOST,
        port=PORT,
        workers=1,
        log_level="info",
        # Long image generations can hold a request open; be generous.
        timeout_keep_alive=75,
    )

# Keep a random secret importable so nothing accidentally falls back to a
# fixed token; also handy for CSRF-style tweaks later.
SERVER_SECRET = os.environ.get("RECRAFT_SECRET") or secrets.token_urlsafe(32)
