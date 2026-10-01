# Recraft → Stock Batch Generator

> 📦 **Setting this up on a new machine? Read [SETUP_GUIDE.md](SETUP_GUIDE.md)** —
> step-by-step install, account login, first generation and troubleshooting.

Generate images on recraft.ai from the CLI **or a local web app**, replaying
your own logged-in browser session over plain HTTP — no paid API. Production
mode converts every image into an **Adobe Stock / Shutterstock compliant**
JPEG: 4096×4096 via Recraft Crisp Upscale, sRGB with embedded ICC, 100 %
quality, with IPTC + XMP + EXIF metadata that auto-populates the title,
description and keywords on the contributor portals.

> **Read first**
> * This talks to recraft.ai's **private web endpoints**, which is very
>   likely against their Terms of Service (that's what the paid API is
>   for). Your account could be rate-limited or flagged. Use at your own
>   risk, at human-like rates (the pool paces 5–10 s between requests).
> * It spends **your account's credits** exactly like the web UI: 1 credit
>   per generation + 1 per crisp upscale.
> * `sessions/*.json` and `recraft_session.json` contain live login
>   cookies — treat them like passwords. They are git-ignored.
> * AI-generated content has its own rules on stock portals: Adobe Stock
>   requires you to **submit it as generative AI** and not imply real
>   people/events; Shutterstock has an AI-only contributor program.
>   Review both portals' current AI policies before uploading.

## Install (new machine / new user)

```bash
git clone <your-repo-url> recraft && cd recraft   # or unzip the folder
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash (venv\Scripts\activate for cmd)
pip install -r requirements.txt
playwright install chromium        # only needed for session capture
```

> **Hosting it on your own server (aaPanel, VPS)?** See
> **[DEPLOY.md](DEPLOY.md)** — nginx reverse proxy, systemd service, and the
> one thing that does *not* work on a headless box (browser login capture).

Then capture YOUR OWN recraft.ai account (no test image, no credits spent):

```bash
python setup_session.py                # -> recraft_session.json (CLI use)
# or add more accounts in the web UI: Account Manager -> Add New Account
```

> The repo contains **no credentials** — `sessions/`, `recraft_session.json`
> and `sniffer_capture.json` are git-ignored. Each user logs in with their
> own account; the tool spends only their credits.

## Install

```bash
pip install -r requirements.txt
playwright install chromium     # only needed for session capture
```

## Quick start (CLI)

```bash
python setup_session.py                          # one-time browser capture
python generate.py "a red fox in snow" -v        # 4096x4096 webp -> ./images
python generate.py --check                       # session live? credits?
```

## Web app

```bash
python app.py        # opens http://127.0.0.1:7860
```

On a server, run the production entry point instead (it binds loopback,
requires a password, and sits behind nginx):

```bash
cp .env.example .env     # set RECRAFT_PASSWORD
uvicorn server:app --host 127.0.0.1 --port 7860 --workers 1
```

* **Tab 1 — Batch Generator**: upload a stock CSV, tick *Crisp 4K Upscale*
  (on by default), Start/Stop, live progress + log, preview of the last
  JPEG with its embedded IPTC.
* **Tab 2 — Account Manager**: pool table with live credits per account,
  *Add New Account* (stealth browser capture — just log in, it finishes
  automatically when login is detected; no test image, no credits spent),
  *Refresh Credits*, delete.
* **Tab 3 — Single Playground**: one prompt → compliant JPEG + download +
  compliance report.

### Batch CSV format

```csv
prompt,title,description,tags,category
"a red fox sitting in fresh snow at dusk, golden rim light",Red Fox In Snow At Dusk,"A red fox sits motionless in fresh snow under golden dusk light.","animal, fox, wildlife, snow, winter",Animals
```

* `prompt` and `tags` required; `title`/`description` fall back to the
  prompt; `category` is kept for the portal upload form.
* `tags` is a comma/semicolon list — cleaned, lowercased, deduped, capped
  at **49** (Adobe Stock limit).
* A row that fails (bad prompt, moderation, network) is logged and the
  batch continues. Results land in `output/001_<title_slug>.jpg` plus
  `output/batch_report.json`.

## Compliance pipeline (per image)

| Step | What | Endpoint / lib |
|---|---|---|
| 1 | Refresh 5-min JWT from long-lived cookies | `GET www.recraft.ai/api/auth/session` |
| 2 | Queue generation | `POST api.recraft.ai/queue_recraft/prompt_to_image` |
| 3 | Poll operation | `POST api.recraft.ai/recrafts/{operationId}` |
| 4 | **Crisp Upscale 4×** → 4096×4096 (≈16.8 MP ≥ 4 MP min) | `POST .../project/{pid}/super_resolution` |
| 5 | Download webp bytes | `GET .../image/{image_id}` |
| 6 | WebP → **JPEG q100, sRGB + embedded ICC**, alpha flattened | Pillow |
| 7 | **IPTC** 2:05 title / 2:120 caption / 2:25 keyword array | iptcinfo3 (APP13) |
| 8 | **EXIF** ImageDescription + XP* (Windows Explorer) | piexif |
| 9 | **XMP** dc:title/description/subject + Iptc4xmpCore | Pillow `xmp=` |

## Multi-account rotation

`AccountPool` (in `account_manager.py`) round-robins across
`sessions/*.json`, sleeps a configurable gentle delay (5–10 s) per
account, tracks live credits via `GET /users/me`, and **auto-failovers**:
on 0 credits / quota the account is marked exhausted and the next one
takes over mid-batch. Thread-safe, shared by the UI and the worker.

## Known recraft.ai quirks (reverse-engineered, verified 2026-09-15)

* The API JWT expires after ~5 min — every run re-mints it from the
  NextAuth cookies; only when *those* die do you need `setup_session.py`.
* The web UI learns finished images over a WebSocket; the REST poll above
  is the equivalent.
* The public CloudFront image URL is AccessDenied — always download via
  `/image/{id}`.
* **Jammed projects**: the Free plan allows one concurrent recraft. If a
  project's queue wedges (operations accept but poll 500 forever), the
  client detects it after 5 consecutive poll errors, **creates a fresh
  project and migrates** (persisted back to the session file).
* Cloudflare Turnstile: capture uses your installed Chrome with automation
  flags hidden; if a checkbox appears, click it yourself.

## Files

```
recraft_core.py       capture + replay library (verified endpoints)
setup_session.py      CLI: browser login capture (--from-capture recovery)
generate.py           CLI: one prompt -> 4096px webp        (--check = credits)
metadata_helper.py    WebP -> compliant JPEG (IPTC/XMP/EXIF/ICC)
account_manager.py    session pool, credits, rotation, failover
batch_processor.py    CSV -> threaded batch runner
app.py                Gradio web UI (3 tabs)
sample_batch.csv      example input
sessions/             <account>.json — LIVE CREDENTIALS, git-ignored
output/               compliant JPEGs + batch_report.json
```
