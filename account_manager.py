#!/usr/bin/env python3
"""
account_manager.py — multi-account pool with round-robin rotation,
credit tracking and auto-failover.

Sessions live in ./sessions/<account_name>.json (same format as
recraft_session.json produced by setup_session.py / recraft_core.capture).

Rotation policy:
  * round-robin across accounts;
  * a gentle delay (default 5-10 s, configurable) between generation
    requests per account to stay under rate limits;
  * accounts with 0 credits (or a quota error mid-generation) are marked
    exhausted and skipped until refreshed;
  * thread-safe: the Gradio UI and the batch worker share one pool.
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path

from recraft_core import (BASE_DIR, NoCredits, RecraftClient, RecraftError,
                          capture_session)

# Anchored to the project root so the pool works from any working directory
# (systemd units often start with a different CWD).
SESSIONS_DIR = BASE_DIR / "sessions"
STATE_FILE = SESSIONS_DIR / "_pool_state.json"


class Account:
    """One recraft.ai account: its session file + lazy client + status."""

    def __init__(self, name: str, path: Path):
        self.name = name
        self.path = path
        self._client: RecraftClient | None = None
        self.last_used = 0.0
        self.exhausted = False
        self.credits: int | None = None
        self.email: str = ""
        self.plan: str = "?"
        self.error: str = ""
        self.reload()

    # -- session file ---------------------------------------------------------
    def reload(self):
        try:
            self.session = json.loads(self.path.read_text(encoding="utf-8"))
            self.valid = True
            self.error = ""
        except Exception as e:
            self.session, self.valid = None, False
            self.error = f"bad session file: {e}"
        self._client = None

    @property
    def client(self) -> RecraftClient:
        if not self.valid:
            raise RecraftError(f"account '{self.name}' has no usable session")
        if self._client is None:
            self._client = RecraftClient(self.session, self.path)
        return self._client

    # -- live credit check ------------------------------------------------------
    def refresh_credits(self) -> dict:
        """GET /users/me through the account's session. Raises RecraftError
        when the login itself is dead (needs re-capture)."""
        info = self.client.credits()
        self.credits = info["total"]
        self.email = info["email"]
        self.plan = info["plan"]
        self.exhausted = self.credits <= 0
        self.error = ""
        return info

    def status(self) -> dict:
        return {
            "name": self.name,
            "email": self.email,
            "plan": self.plan,
            "credits": self.credits if self.credits is not None else "?",
            "state": ("EXHAUSTED" if self.exhausted else
                      "OK" if self.valid else "NEEDS LOGIN"),
            "error": self.error,
            "last_used": time.strftime("%Y-%m-%d %H:%M",
                                       time.localtime(self.last_used))
            if self.last_used else "-",
        }


class AccountPool:
    def __init__(self, sessions_dir: Path | str = SESSIONS_DIR,
                 min_delay: float = 5.0, max_delay: float = 10.0):
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._lock = threading.RLock()
        self._order: list[str] = []
        self._idx = 0
        self.accounts: dict[str, Account] = {}
        self.load()

    # -- persistence ----------------------------------------------------------
    def load(self):
        with self._lock:
            self.accounts = {}
            for f in sorted(self.sessions_dir.glob("*.json")):
                if f.name.startswith("_"):
                    continue
                acct = Account(f.stem, f)
                self.accounts[acct.name] = acct
            state = {}
            if STATE_FILE.exists() and STATE_FILE.parent == self.sessions_dir:
                try:
                    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                except Exception:
                    state = {}
            for name, s in state.items():
                if name in self.accounts:
                    a = self.accounts[name]
                    a.exhausted = s.get("exhausted", False)
                    a.credits = s.get("credits")
                    a.email = s.get("email", "")
                    a.plan = s.get("plan", "?")
            self._order = [n for n in self.accounts
                           if n not in state.get("_order_skipped", [])]
            self._idx = 0

    def save_state(self):
        with self._lock:
            STATE_FILE.write_text(json.dumps(
                {n: {"exhausted": a.exhausted, "credits": a.credits,
                     "email": a.email, "plan": a.plan}
                 for n, a in self.accounts.items()}, indent=1),
                encoding="utf-8")

    # -- management -----------------------------------------------------------
    def add_account(self, name: str, session: dict) -> Account:
        name = _sanitize(name)
        path = self.sessions_dir / f"{name}.json"
        if path.exists():
            raise FileExistsError(f"account '{name}' already exists — "
                                  f"delete it first to re-capture")
        path.write_text(json.dumps(session, indent=2), encoding="utf-8")
        acct = Account(name, path)
        with self._lock:
            self.accounts[name] = acct
            self._order.append(name)
        return acct

    def remove_account(self, name: str):
        with self._lock:
            acct = self.accounts.pop(name, None)
            if not acct:
                raise KeyError(name)
            acct.path.unlink(missing_ok=True)
            self._order = [n for n in self._order if n != name]

    def rename_session_file(self, src: Path, name: str) -> Account:
        return self.add_account(name, json.loads(src.read_text(encoding="utf-8")))

    def capture_new_account(self, name: str, log=print,
                            timeout=None) -> Account:
        """Interactive: opens the stealth browser; the user just logs in —
        no test image (endpoints are known, zero credits spent). Returns
        the new Account. timeout=secs auto-finishes once login is detected
        (web UI)."""
        profile = Path(".pw-profiles") / _sanitize(name)
        profile.mkdir(parents=True, exist_ok=True)
        session = capture_session(profile, log=log, timeout=timeout)
        return self.add_account(name, session)

    # -- rotation -------------------------------------------------------------
    def candidates(self) -> list[Account]:
        return [self.accounts[n] for n in self._order if n in self.accounts]

    def acquire(self, required_credits: int = 2,
                log=print) -> Account:
        """Next account (round-robin) that is valid and not exhausted.
        Sleeps the gentle per-account delay first. Raises RecraftError
        when the whole pool is spent."""
        with self._lock:
            pool = self.candidates()
            if not pool:
                raise RecraftError("no accounts in pool — add one first")
            n = len(pool)
            acct = None
            for _ in range(n):
                a = pool[self._idx % n]
                self._idx = (self._idx + 1) % n
                if a.valid and not a.exhausted and \
                        (a.credits is None or a.credits >= required_credits):
                    acct = a
                    break
            if acct is None:
                raise RecraftError(
                    "all accounts exhausted or need re-login — refresh "
                    "credits or add an account")
            wait = max(0.0, (acct.last_used +
                             random.uniform(self.min_delay, self.max_delay))
                       - time.time())
        if wait:
            log(f"[.] pacing delay {wait:.1f}s on account "
                f"'{acct.name}' (rate-limit protection)")
            time.sleep(wait)
        with self._lock:
            acct.last_used = time.time()
        return acct

    def mark_exhausted(self, name: str, log=print):
        with self._lock:
            a = self.accounts.get(name)
            if a:
                a.exhausted = True
                log(f"[!] account '{name}' marked EXHAUSTED — rotating")
        self.save_state()

    def refresh_all_credits(self, log=print) -> list[dict]:
        rows = []
        with self._lock:
            pool = self.candidates()
        for a in pool:
            try:
                info = a.refresh_credits()
                log(f"[+] {a.name}: {info['total']} credits "
                    f"({info['credits']}+{info['extra_credits']} extra, "
                    f"plan {info['plan']})")
            except (RecraftError, Exception) as e:  # noqa: BLE001
                a.error = str(e)
                log(f"[!] {a.name}: {e}")
            rows.append(a.status())
        self.save_state()
        return rows

    def status_table(self) -> list[dict]:
        with self._lock:
            return [a.status() for a in self.candidates()]

    # -- the failover workhorse -------------------------------------------------
    def generate_with_failover(self, prompt: str, dest: Path, *,
                               attempts: int | None = None,
                               upscale: bool = True, remove_bg: bool = False,
                               max_wait: int = 180,
                               width: int = 1024, height: int = 1024,
                               negative: str = "", seed: int | None = None,
                               log=print) -> dict:
        """Try accounts round-robin until one succeeds. NoCredits/quota ->
        mark exhausted, move on. Dead login -> flag and move on."""
        with self._lock:
            total = len([a for a in self.candidates() if a.valid])
        tries = attempts or total
        last_err = "no accounts"
        for i in range(tries):
            try:
                acct = self.acquire(required_credits=2 if upscale else 1,
                                    log=log)
            except RecraftError as e:
                last_err = str(e)
                break
            try:
                log(f"[.] using account '{acct.name}' "
                    f"({acct.credits if acct.credits is not None else '?'} "
                    f"credits, try {i + 1}/{tries})")
                result = acct.client.generate(
                    prompt, dest, upscale=upscale, remove_bg=remove_bg,
                    max_wait=max_wait, width=width, height=height,
                    negative=negative, seed=seed, log=log)
                result["account"] = acct.name
                with self._lock:
                    if acct.credits is not None:
                        acct.credits -= (2 if upscale else 1) + \
                            (1 if remove_bg else 0)
                self.save_state()
                return result
            except NoCredits as e:
                last_err = f"{acct.name}: {e}"
                log(f"[!] {last_err}")
                self.mark_exhausted(acct.name, log)
            except RecraftError as e:
                last_err = f"{acct.name}: {e}"
                log(f"[!] {last_err}")
                if "expired" in str(e) or "login" in str(e):
                    with self._lock:
                        acct.valid = False
                        acct.error = str(e)
                # transient generation failure: try next account
                continue
        raise RecraftError(f"generation failed after {tries} account "
                           f"attempt(s): {last_err}")


def _sanitize(name: str) -> str:
    import re
    return re.sub(r"[^\w\-]+", "_", str(name)).strip("_") or "account"
