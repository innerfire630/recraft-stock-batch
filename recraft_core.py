#!/usr/bin/env python3
"""
recraft_core.py — shared library for recraft.ai session capture + replay.

Used by: setup_session.py (CLI capture), generate.py (CLI), account_manager.py,
batch_processor.py and app.py (Gradio web UI).

Verified live flow (2026-09-15):
  refresh  : GET  https://www.recraft.ai/api/auth/session      -> accessToken
             (API JWT expires ~5 min; re-mint from long-lived cookies)
  credits  : GET  https://api.recraft.ai/users/me              -> credits, plan
  create   : POST /queue_recraft/prompt_to_image?project_id=   -> {operationId}
  poll     : POST /recrafts/{operationId}?project_id=          -> result_image_ids
  upscale  : POST /project/{pid}/super_resolution (sync)       -> 4x image_id
  bgremove : POST /project/{pid}/remove_background (sync)      -> RGBA image_id
  download : GET  /image/{image_id}                            -> image bytes

NOTE: uses your own logged-in sessions and spends account credits.
Automating recraft.ai's private web endpoints may violate their ToS.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

try:
    import httpx
except ImportError:
    sys.exit("httpx missing. Run:  pip install -r requirements.txt")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MARKER = "ZQXV-CAPTURE-2026-PASTE-ME"
START_URL = "https://www.recraft.ai/generate"
SESSIONS_DIR = Path("sessions")
DEFAULT_SESSION_FILE = Path("recraft_session.json")

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
STATIC_RE = re.compile(
    r"\.(js|css|png|jpe?g|webp|gif|svg|woff2?|ttf|otf|ico|map)(\?|$)", re.I)
IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".webp": "image/webp"}

# --- Cloudflare stealth ----------------------------------------------------
# Injected into every page before any site script runs. Removes the
# properties Cloudflare Turnstile fingerprints to spot automated browsers.
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
if (!window.chrome) window.chrome = {};
if (!window.chrome.runtime) window.chrome.runtime = {};
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5].map(i => ({ name: 'Chrome PDF Plugin ' + i })),
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
const origQuery = window.navigator.permissions.query.bind(
  window.navigator.permissions);
window.navigator.permissions.query = (params) =>
  params.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : origQuery(params);
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (p) {
  if (p === 37445) return 'Intel Inc.';
  if (p === 37446) return 'Intel Iris OpenGL Engine';
  return getParameter.call(this, p);
};
"""

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


def find_real_browser() -> str | None:
    for p in CHROME_PATHS:
        if Path(p).exists():
            return p
    return None


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------
def walk_scalars(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_scalars(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_scalars(v, path + (i,))
    else:
        yield path, obj


def walk_strings(obj, path=()):
    for p, v in walk_scalars(obj, path):
        if isinstance(v, str):
            yield p, v


def get_path(obj, path):
    cur = obj
    for p in path or []:
        if isinstance(cur, list):
            try:
                cur = cur[int(p)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
        if cur is None:
            return None
    return cur


def parse_json_maybe(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return {"__raw__": text[:2000]}


# ---------------------------------------------------------------------------
# Template substitution
# ---------------------------------------------------------------------------
def substitute(obj, values: dict):
    if isinstance(obj, dict):
        return {k: substitute(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, values) for v in obj]
    if isinstance(obj, str):
        m = re.fullmatch(r"\{\{(\w+)\}\}", obj)
        if m:
            return values.get(m.group(1), obj)
        for key in ("PROMPT", "NEGATIVE", "PROJECT_ID", "IMAGE_ID"):
            token = "{{%s}}" % key
            if token in obj and key in values:
                obj = obj.replace(token, str(values[key]))
        return obj
    return obj


def coerce_scalars(obj, values: dict):
    """Replace '{{KEY}}' strings whose value is non-str (ints/bools)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                m = re.fullmatch(r"\{\{(\w+)\}\}", v)
                if m and m.group(1) in values and not isinstance(
                        values[m.group(1)], str):
                    obj[k] = values[m.group(1)]
            else:
                coerce_scalars(v, values)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                m = re.fullmatch(r"\{\{(\w+)\}\}", v)
                if m and m.group(1) in values and not isinstance(
                        values[m.group(1)], str):
                    obj[i] = values[m.group(1)]
            else:
                coerce_scalars(v, values)
    return obj


# ===========================================================================
# PART 1 — session capture (Playwright sniffer)
# ===========================================================================
class Capture:
    """Records JSON traffic and builds a replayable session dict."""

    def __init__(self):
        self.records: list[dict] = []

    def handle_response(self, response):
        try:
            req = response.request
            if "json" not in (response.headers.get("content-type") or ""):
                return
            if STATIC_RE.search(response.url):
                return
            resp_body = response.json()
        except Exception:
            return
        self.records.append({
            "n": len(self.records),
            "method": req.method,
            "url": response.url,
            "status": response.status,
            "ts": time.time(),
            "headers": dict(req.headers),
            "req_body": parse_json_maybe(req.post_data),
            "resp_body": resp_body,
        })

    # -- detection ----------------------------------------------------------
    def find_create(self) -> dict | None:
        cands = [r for r in self.records
                 if MARKER in json.dumps(r["req_body"] or {})
                 and r["method"] in ("POST", "PUT", "PATCH")]
        if not cands:
            return None

        def score(r):
            s = 0
            u = r["url"].lower()
            if re.search(r"queue|generat|creation|render|image|prompt|task", u):
                s += 2
            if re.search(r"setting|preference|history", u):
                s -= 4
            rb = r["resp_body"]
            if isinstance(rb, dict):
                if any(k.lower() in ("operationid", "operation_id", "id",
                                     "jobid", "job_id", "taskid", "requestid")
                       for k in rb):
                    s += 3
                if len(rb) <= 4:
                    s += 1
            return s

        return max(cands, key=score)

    def marker_image_ready(self) -> bool:
        """True once the UI has fetched the finished marker image metadata
        (GET /recraft_images/<id> whose recraft.prompt == MARKER)."""
        for r in self.records:
            if "/recraft_images/" in r["url"] and r["method"] == "GET":
                rb = r["resp_body"]
                if isinstance(rb, dict) and rb.get("prompt") == MARKER:
                    return True
        return False


def templatize(obj, project_id=None):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = k.lower()
            if isinstance(v, str) and MARKER in v:
                out[k] = "{{PROMPT}}"
            elif isinstance(v, str) and "negative" in kl:
                out[k] = "{{NEGATIVE}}"
            elif isinstance(v, (int, float)) and "seed" in kl:
                out[k] = "{{SEED}}"
            elif isinstance(v, int) and kl in ("width", "imagewidth", "w"):
                out[k] = "{{WIDTH}}"
            elif isinstance(v, int) and kl in ("height", "imageheight", "h"):
                out[k] = "{{HEIGHT}}"
            elif isinstance(v, bool) and kl in ("enhance", "upscale"):
                out[k] = "{{%s}}" % kl.upper()
            elif isinstance(v, str) and project_id and v == project_id:
                out[k] = "{{PROJECT_ID}}"
            else:
                out[k] = templatize(v, project_id)
        return out
    if isinstance(obj, list):
        return [templatize(v, project_id) for v in obj]
    if isinstance(obj, str) and MARKER in obj:
        return "{{PROMPT}}"
    return obj


def build_session(records, storage, user_agent) -> dict:
    """Detect endpoints in captured traffic; return the session dict."""
    cap = Capture()
    cap.records = records
    create = cap.find_create()
    if not create:
        raise RuntimeError(
            f"No request containing the marker prompt was captured — "
            f"generation never happened.")

    m = re.search(r"project_id=([0-9a-fA-F-]{36})", create["url"]) \
        or re.search(r"[0-9a-fA-F-]{36}", create["url"])
    project_id = m.group(1) if m else None

    parts = urlsplit(create["url"])
    api_base = parts.scheme + "://" + parts.netloc

    def keep_headers(h):
        drop = {"cookie", "content-length", "connection", "host",
                "accept-encoding"}
        return {k: v for k, v in h.items() if k.lower() not in drop}

    def t_url(url):
        return url.replace(project_id, "{{PROJECT_ID}}") if project_id else url

    session = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "origin": api_base,
        "user_agent": user_agent,
        "project_id": project_id,
        "create": {
            "method": create["method"],
            "url": t_url(create["url"]),
            "headers": keep_headers(create["headers"]),
            "body": templatize(create["req_body"] or {}, project_id),
        },
        "cookies": storage.get("cookies", []),
        "local_storage": [
            {"origin": o["origin"], "localStorage": o["localStorage"]}
            for o in storage.get("origins", [])
        ],
        "image_paths": [],
    }

    if "recraft.ai" in api_base and project_id:
        session["poll"] = {
            "method": "POST",
            "url": api_base + "/recrafts/{{ID}}?project_id={{PROJECT_ID}}",
            "body": {},
            "id_path": ["operationId"],
            "image_ids_path": ["result_image_ids"],
            "image_id_field": "image_id",
        }
        session["download"] = {"url": api_base + "/image/{{IMAGE_ID}}"}
        session["refresh_url"] = "https://www.recraft.ai/api/auth/session"
        session["credits_url"] = api_base + "/users/me"
        session["upscale"] = {
            "method": "POST",
            "url": api_base + "/project/{{PROJECT_ID}}/super_resolution",
            "body": {"image_id": {"image_id": "{{IMAGE_ID}}"}},
        }
        session["remove_background"] = {
            "method": "POST",
            "url": api_base + "/project/{{PROJECT_ID}}/remove_background",
            "body": {"image_id": {"image_id": "{{IMAGE_ID}}"}},
        }
    return session


def build_known_session(project_id: str | None, storage: dict,
                        user_agent: str) -> dict:
    """Build a session dict straight from the verified recraft.ai
    endpoints — no marker generation needed, zero credits spent."""
    api_base = "https://api.recraft.ai"
    return {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "origin": api_base,
        "user_agent": user_agent,
        "project_id": project_id,
        "create": {
            "method": "POST",
            "url": api_base +
                   "/queue_recraft/prompt_to_image?project_id={{PROJECT_ID}}",
            "headers": {
                "content-type": "application/json",
                "x-client-type": "web-app.recraft.ai",
                "referer": "https://www.recraft.ai/",
            },
            "body": {
                "prompt": "{{PROMPT}}",
                "image_type": "any",
                "negative_prompt": "{{NEGATIVE}}",
                "user_controls": {},
                "layer_size": {"height": "{{HEIGHT}}",
                               "width": "{{WIDTH}}"},
                "random_seed": "{{SEED}}",
                "style_id": "b9af225b-6b98-43d0-a4b3-16343641e852",
                "num_images_per_prompt": 1,
                "transform_model": "recraftv3_raster",
            },
        },
        "poll": {
            "method": "POST",
            "url": api_base + "/recrafts/{{ID}}?project_id={{PROJECT_ID}}",
            "body": {},
            "id_path": ["operationId"],
            "image_ids_path": ["result_image_ids"],
            "image_id_field": "image_id",
        },
        "download": {"url": api_base + "/image/{{IMAGE_ID}}"},
        "refresh_url": "https://www.recraft.ai/api/auth/session",
        "credits_url": api_base + "/users/me",
        "upscale": {
            "method": "POST",
            "url": api_base + "/project/{{PROJECT_ID}}/super_resolution",
            "body": {"image_id": {"image_id": "{{IMAGE_ID}}"}},
        },
        "remove_background": {
            "method": "POST",
            "url": api_base + "/project/{{PROJECT_ID}}/remove_background",
            "body": {"image_id": {"image_id": "{{IMAGE_ID}}"}},
        },
        "cookies": storage.get("cookies", []),
        "local_storage": [
            {"origin": o["origin"], "localStorage": o["localStorage"]}
            for o in storage.get("origins", [])
        ],
        "image_paths": [],
    }


def _mint_token(ctx) -> str | None:
    """GET /api/auth/session inside the browser context -> accessToken
    (None while the user is not logged in yet)."""
    try:
        r = ctx.request.get("https://www.recraft.ai/api/auth/session",
                            timeout=10000)
        if r.ok:
            return (r.json() or {}).get("accessToken")
    except Exception:
        pass
    return None


def _sniff_project_id(records) -> str | None:
    """Fallback: pull a project_id out of any captured traffic."""
    for r in reversed(records):
        m = re.search(r"project_id=([0-9a-fA-F-]{36})", r["url"])
        if m:
            return m.group(1)
    return None


def capture_session(profile_dir: Path, log=print, timeout=None,
                    headless=False) -> dict:
    """Open a stealth browser for interactive login. NO test image is
    generated — the create/poll/download/upscale endpoints are already
    verified, so the session is built as soon as the login is detected.
    Zero credits spent, and the new Chat UI can't cause a timeout.

    timeout=None  -> CLI mode: user presses ENTER after logging in.
    timeout=secs  -> auto mode (web UI): finishes as soon as login is
                     detected, or times out.
    Returns the session dict (caller saves it).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright missing: pip install playwright "
                           "&& playwright install chromium")

    cap = Capture()
    browser_exe = find_real_browser()
    log(f"[*] launching {'real browser: ' + browser_exe if browser_exe else 'Playwright Chromium (no Chrome found)'}")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(Path(profile_dir).resolve()),
            headless=headless,
            viewport={"width": 1440, "height": 900},
            executable_path=browser_exe,
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check"],
        )
        ctx.add_init_script(STEALTH_JS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", cap.handle_response)
        try:
            page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"(navigation hiccup, keep going: {e})")

        if timeout is None:
            stop = threading.Event()

            def waiter():
                input(">>> Log in to recraft.ai, then press ENTER... ")
                stop.set()

            threading.Thread(target=waiter, daemon=True).start()
            while not stop.is_set():
                page.wait_for_timeout(300)
        else:
            deadline = time.time() + timeout
            log(f"[*] waiting up to {timeout}s for login "
                f"(no test image needed) ...")
            while time.time() < deadline and not _mint_token(ctx):
                page.wait_for_timeout(1000)

        token = _mint_token(ctx)
        if not token:
            ctx.close()
            raise RuntimeError(
                "Login not detected (no accessToken from "
                "/api/auth/session) — log in and try again.")
        log("[*] login detected")

        # Fresh project -> deterministic project_id and a clean generation
        # queue (Free plan queues wedge easily; see ProjectJammed).
        project_id = None
        try:
            r = ctx.request.post(
                "https://api.recraft.ai/project",
                headers={"authorization": "Bearer " + token,
                         "content-type": "application/json"},
                data="{}", timeout=15000)
            if r.ok:
                project_id = (r.json() or {}).get("id")
        except Exception:
            pass
        if project_id:
            log(f"[+] fresh project {project_id[:8]}... created")
        else:
            project_id = _sniff_project_id(cap.records)
            if project_id:
                log(f"[+] using project {project_id[:8]}... from traffic")
        if not project_id:
            ctx.close()
            raise RuntimeError(
                "Could not determine a project_id (project creation failed "
                "and none seen in traffic).")

        storage = ctx.storage_state()
        user_agent = page.evaluate("navigator.userAgent")
        ctx.close()

    log(f"[*] captured {len(cap.records)} JSON exchanges")
    try:  # keep a raw dump for --from-capture recovery / manual repair
        Path("sniffer_capture.json").write_text(
            json.dumps(cap.records, indent=1, default=str), encoding="utf-8")
    except Exception:
        pass
    return build_known_session(project_id, storage, user_agent)


# ===========================================================================
# PART 2 — replay client (pure HTTP)
# ===========================================================================
class RecraftError(RuntimeError):
    pass


class NoCredits(RecraftError):
    """Account is out of credits / hit a quota — trigger failover."""


class ProjectJammed(RecraftError):
    """The project's generation queue is wedged (operations accept but poll
    500 forever — e.g. Free plan max_concurrent_recrafts=1 held by a dead
    job). Fix: migrate to a freshly created project."""


class RecraftClient:
    """One instance per account session dict."""

    def __init__(self, session: dict, session_path: Path | str | None = None):
        self.session = session
        self.session_path = Path(session_path) if session_path else None
        self.client = self._build_client()

    # -- plumbing -----------------------------------------------------------
    def _build_client(self) -> httpx.Client:
        s = self.session
        headers = {
            "User-Agent": s.get("user_agent", "Mozilla/5.0"),
            "Accept": "application/json",
            "Referer": "https://www.recraft.ai/generate",
            "Origin": "https://www.recraft.ai",
        }
        for k, v in (s["create"].get("headers") or {}).items():
            headers[k] = v
        cookies = {c["name"]: c["value"] for c in s.get("cookies", [])}
        return httpx.Client(headers=headers, cookies=cookies, timeout=300.0,
                            follow_redirects=True)

    @staticmethod
    def _looks_authed(r: httpx.Response) -> bool:
        if r.status_code in (401, 403):
            return False
        if r.status_code in (301, 302, 307, 308):
            loc = r.headers.get("location", "").lower()
            if any(t in loc for t in ("login", "auth", "sign-in", "sso")):
                return False
        return True

    def refresh_token(self, log=None) -> bool:
        url = self.session.get("refresh_url")
        if not url:
            return False
        try:
            tok = (self.client.get(url).json() or {}).get("accessToken")
        except Exception:
            tok = None
        if tok:
            self.client.headers["Authorization"] = "Bearer " + tok
            return True
        if log:
            log("[!] token refresh failed (cookies expired?)")
        return False

    # -- project maintenance ----------------------------------------------------
    @staticmethod
    def save_session(session: dict, path):
        Path(path).write_text(json.dumps(session, indent=2), encoding="utf-8")

    def new_project(self) -> str:
        """Create a fresh recraft project and switch this session to it.
        (A project's queue can wedge on the Free plan; a new project clears
        it. The session dict is updated + persisted when a path is known.)"""
        origin = self.session.get("origin", "https://api.recraft.ai")
        r = self.client.post(origin + "/project", json={})
        if r.status_code >= 400:
            raise RecraftError(f"could not create project: HTTP "
                               f"{r.status_code} {r.text[:120]}")
        pid = r.json()["id"]
        self.session["project_id"] = pid
        if self.session_path:      # persist so the next run isn't re-jammed
            try:
                self.save_session(self.session, self.session_path)
            except Exception:
                pass
        return pid

    # -- account info ---------------------------------------------------------
    def credits(self) -> dict:
        """Live credit snapshot. Raises RecraftError if the session is dead."""
        url = self.session.get("credits_url") or \
            self.session.get("origin", "https://api.recraft.ai") + "/users/me"
        if not self.refresh_token():
            raise RecraftError("session cookies expired — re-capture login")
        r = self.client.get(url)
        if not self._looks_authed(r) or r.status_code >= 400:
            raise RecraftError(f"credits lookup failed (HTTP {r.status_code})")
        j = r.json()
        plan = j.get("plan") or {}
        return {
            "email": j.get("email") or j.get("name") or "?",
            "credits": int(j.get("credits") or 0),
            "extra_credits": int(j.get("extra_credits") or 0),
            "api_credits": int(j.get("api_credits") or 0),
            "plan": plan.get("name") or plan.get("id") or "plan",
            "total": int(j.get("credits") or 0) + int(j.get("extra_credits") or 0),
        }

    # -- generation -----------------------------------------------------------
    def _sub_url(self, url: str) -> str:
        return url.replace("{{PROJECT_ID}}", self.session.get("project_id") or "")

    def create(self, prompt, width=1024, height=1024, negative="",
               seed=None) -> str:
        """Queue a generation; returns operationId."""
        s = self.session
        values = {
            "PROMPT": prompt, "NEGATIVE": negative,
            "WIDTH": int(width), "HEIGHT": int(height),
            "SEED": seed if seed is not None else
            int.from_bytes(hashlib.md5(prompt.encode()).digest()[:4], "big"),
            "ENHANCE": False, "UPSCALE": False,
            "PROJECT_ID": s.get("project_id"),
        }
        body = substitute(json.loads(json.dumps(s["create"]["body"])), values)
        coerce_scalars(body, values)
        r = self.client.request(s["create"]["method"],
                                self._sub_url(s["create"]["url"]), json=body)
        if not self._looks_authed(r):
            self.refresh_token()
            r = self.client.request(s["create"]["method"],
                                    self._sub_url(s["create"]["url"]),
                                    json=body)
            if not self._looks_authed(r):
                raise RecraftError("session expired — re-capture login")
        if r.status_code in (402, 429) or "credit" in r.text.lower() \
                or "quota" in r.text.lower():
            if r.status_code >= 400:
                raise NoCredits(f"HTTP {r.status_code}: {r.text[:160]}")
        if r.status_code >= 400:
            raise RecraftError(f"create failed HTTP {r.status_code}: "
                               f"{r.text[:300]}")
        op = get_path(r.json(),
                      (s.get("poll") or {}).get("id_path", ["operationId"]))
        if not op:
            raise RecraftError(f"no operationId in create response: {r.text[:200]}")
        return op

    def wait_operation(self, op_id, want=1, max_wait=180, log=None) -> list[str]:
        """Poll POST /recrafts/{op} until result_image_ids fills in.
        Raises ProjectJammed after 5 consecutive HTTP>=400 polls (one
        transient 500 right after create is normal)."""
        poll = self.session["poll"]
        url = self._sub_url(poll["url"].replace("{{ID}}", op_id))
        deadline = time.time() + max_wait
        delay = 2.0
        ids: list[str] = []
        errors = 0
        while time.time() < deadline:
            time.sleep(delay)
            r = self.client.request(poll.get("method", "POST"), url,
                                    json=poll.get("body") or {})
            if r.status_code >= 400:
                errors += 1
                if errors >= 5:
                    raise ProjectJammed(
                        f"operation {op_id[:8]}…: {errors} consecutive poll "
                        f"errors (HTTP {r.status_code}) — project queue is "
                        f"wedged")
                delay = min(delay * 1.25, 6)
                continue
            errors = 0
            try:
                j = r.json()
            except Exception:
                continue
            field = poll.get("image_id_field", "image_id")
            items = get_path(j, poll.get("image_ids_path", ["result_image_ids"]))
            ids = [it[field] for it in (items or [])
                   if isinstance(it, dict) and it.get(field)]
            if len(ids) >= want:
                return ids
            if log:
                log(f"[.] generating ... ({len(ids)}/{want}, "
                    f"{int(deadline - time.time())}s left)")
            delay = min(delay * 1.25, 6)
        raise RecraftError(f"operation {op_id} timed out after {max_wait}s")

    def upscale_crisp(self, image_id: str, log=None) -> str | None:
        """Recraft Crisp Upscale (4x, synchronous, ~1 credit).
        Returns new image_id, or None if unavailable (caller keeps original).
        Raises NoCredits when the account is out."""
        up = self.session.get("upscale")
        if not up:
            return None
        url = self._sub_url(up["url"])
        body = substitute(up["body"], {"IMAGE_ID": image_id})
        r = self.client.request(up.get("method", "POST"), url, json=body)
        if r.status_code in (402, 429):
            raise NoCredits(f"upscale HTTP {r.status_code}: {r.text[:160]}")
        if r.status_code >= 400:
            if "credit" in r.text.lower() or "quota" in r.text.lower():
                raise NoCredits(f"upscale: {r.text[:160]}")
            if log:
                log(f"[!] upscale HTTP {r.status_code}: {r.text[:140]} "
                    f"— keeping original resolution")
            return None
        try:
            j = r.json()
        except Exception:
            return None
        new_id = (j.get("result") or {}).get("image_id")
        if new_id and log:
            log(f"[+] crisp upscale -> {j.get('width')}x{j.get('height')}")
        return new_id

    def remove_background(self, image_id: str, log=None) -> str | None:
        """Recraft NATIVE AI background removal (synchronous, 1 credit).
        Returns the new RGBA image_id, or None on any failure — the caller
        keeps the original image and the batch continues gracefully.
        Raises NoCredits when the account is out (failover handles it)."""
        rb = self.session.get("remove_background")
        if not rb:
            # legacy session file (captured before this endpoint was added)
            # — fall back to the verified native endpoint instead of failing
            rb = {
                "method": "POST",
                "url": self.session.get("origin",
                                        "https://api.recraft.ai")
                       + "/project/{{PROJECT_ID}}/remove_background",
                "body": {"image_id": {"image_id": "{{IMAGE_ID}}"}},
            }
            self.session["remove_background"] = rb
            if log:
                log("[i] legacy session: using built-in remove_background "
                    "endpoint")
        url = self._sub_url(rb["url"])
        body = substitute(rb["body"], {"IMAGE_ID": image_id})
        r = self.client.request(rb.get("method", "POST"), url, json=body)
        if r.status_code in (402, 429):
            raise NoCredits(f"remove_background HTTP {r.status_code}: "
                            f"{r.text[:160]}")
        if r.status_code >= 400:
            if "credit" in r.text.lower() or "quota" in r.text.lower():
                raise NoCredits(f"remove_background: {r.text[:160]}")
            if log:
                log(f"[!] remove_background HTTP {r.status_code}: "
                    f"{r.text[:140]} — keeping original background")
            return None
        try:
            j = r.json()
        except Exception:
            if log:
                log("[!] remove_background: unreadable response — keeping "
                    "original background")
            return None
        new_id = (j.get("result") or {}).get("image_id")
        if new_id and log:
            log(f"[+] background removed -> RGBA {j.get('width')}x"
                f"{j.get('height')} ({j.get('credits', '?')} credit)")
        return new_id

    def download(self, image_id: str, dest: Path) -> Path:
        url = self.session["download"]["url"].replace("{{IMAGE_ID}}", image_id)
        r = self.client.get(url)
        if r.status_code >= 400:
            raise RecraftError(f"download failed HTTP {r.status_code}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest

    # -- high level ------------------------------------------------------------
    def generate(self, prompt, dest: Path, *, width=1024, height=1024,
                 negative="", seed=None, upscale=True, remove_bg=False,
                 max_wait=180, log=None) -> dict:
        """Full pipeline: create -> poll -> [remove_bg path]:
        bg-removal at BASE resolution (clean segmentation — soft table
        reflections / contact shadows are cut away) -> crisp upscale of the
        OPAQUE base to 4K -> download both -> re-apply the 1024px alpha mask
        onto the 4K image locally (LANCZOS) -> transparent 4K webp at `dest`.
        [no remove_bg]: create -> poll -> (crisp upscale) -> download.

        WHY THIS ORDER: Recraft's super_resolution FLATTENS alpha (verified
        2026-09-19: RGBA in -> RGB out), so upscaling the cutout destroys
        transparency; and running bg-removal at 4096px keeps soft
        reflections/shadows. Cutting at 1024px + local mask compositing
        gives a clean transparent 4K cutout with no extra credits.
        Background removal is best-effort: on failure the original image is
        downloaded instead and 'bg_removed' is False."""
        log = log or (lambda *_: None)
        self.refresh_token(log)
        num = 1
        try:
            num = int(self.session["create"]["body"]
                      .get("num_images_per_prompt", 1))
        except Exception:
            pass
        for attempt in (1, 2):
            try:
                op = self.create(prompt, width, height, negative, seed)
                log(f"[.] queued operation {op[:8]}...")
                ids = self.wait_operation(op, want=num, max_wait=max_wait,
                                          log=log)
                break
            except ProjectJammed as e:
                if attempt == 2:
                    raise RecraftError(str(e))
                log(f"[!] {e} — migrating to a fresh project")
                pid = self.new_project()
                log(f"[.] new project {pid[:8]}...")
        img_id = ids[0]
        cutout_id = None
        if remove_bg:
            # FIRST: cut the subject out at base resolution (before any
            # upscaling) so reflections/shadows are segmented away cleanly.
            try:
                cutout_id = self.remove_background(img_id, log)
            except NoCredits:
                raise          # failover to the next account
            except RecraftError as e:
                log(f"[!] background removal failed ({e}) — continuing "
                    f"with the original image")
        bg_removed = cutout_id is not None
        upscaled = False
        if upscale:
            # THEN: 4K upscale of the OPAQUE base (super_resolution
            # flattens alpha, so we upscale the base, not the cutout).
            new_id = self.upscale_crisp(img_id, log)
            if new_id:
                img_id, upscaled = new_id, True
        if bg_removed and upscaled:
            # 4K transparent cutout = 4K upscaled base + 1024px alpha mask
            # (resized LANCZOS) applied locally. No extra credits.
            log("[.] compositing 4K cutout (mask resize, local) ...")
            base_tmp = dest.with_suffix(".base.webp")
            cut_tmp = dest.with_suffix(".cut.webp")
            try:
                self.download(img_id, base_tmp)
                self.download(cutout_id, cut_tmp)
                from PIL import Image
                with Image.open(cut_tmp) as cut_im:
                    with Image.open(base_tmp) as base_im:
                        mask = cut_im.convert("RGBA").getchannel("A") \
                            .resize(base_im.size, Image.LANCZOS)
                        out = base_im.convert("RGBA")
                out.putalpha(mask)
                out.save(dest, "WEBP", quality=95)
                log("[+] 4K transparent cutout composited")
            except Exception as e:
                log(f"[!] mask compositing failed ({e}) — falling back to "
                    f"the 1024px cutout")
                self.download(cutout_id, dest)
            finally:
                base_tmp.unlink(missing_ok=True)
                cut_tmp.unlink(missing_ok=True)
        elif bg_removed:
            self.download(cutout_id, dest)
        else:
            self.download(img_id, dest)
        if remove_bg and not bg_removed:
            log("[!] PNG will NOT be transparent — background removal "
                "did not run (see warnings above)")
        return {"operation_id": op, "image_id": img_id,
                "cutout_id": cutout_id,
                "upscaled": upscaled, "bg_removed": bg_removed,
                "path": str(dest)}
