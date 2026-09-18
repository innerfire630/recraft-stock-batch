# Setup Guide — Recraft Stock Batch Generator

A step-by-step guide to get the tool running on your machine with **your
own recraft.ai account**. Total time: ~10 minutes.

---

## 1. What you need

| Requirement | Details |
|---|---|
| **Python 3.10+** | [python.org/downloads](https://www.python.org/downloads/) — on Windows, tick **"Add python.exe to PATH"** during install |
| **Google Chrome or Edge** | The login capture uses your installed browser so Cloudflare passes silently |
| **A recraft.ai account** | Sign up free at [recraft.ai](https://www.recraft.ai) — the tool spends **your** credits (free plan works) |
| **Git** (optional) | Only if cloning the repo; a ZIP download works too |

> ⚠️ **Read first:** this tool talks to recraft.ai's private web endpoints,
> which is likely against their Terms of Service (that's what the paid API
> is for). Use at your own risk, with your own account. It spends credits
> exactly like the web UI: 1 per generation, +1 per 4K upscale, +1 per
> background removal.

---

## 2. Get the code

**Option A — clone (recommended):**
```bash
git clone <REPO_URL> recraft
cd recraft
```

**Option B — download:** click **Code → Download ZIP** on the repo page,
extract it, and open a terminal in that folder.

---

## 3. Install

### Windows (Command Prompt / PowerShell)
```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### Windows (Git Bash)
```bash
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
playwright install chromium
```

### macOS / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

> `playwright install chromium` is only needed for the one-time account
> login capture — but install it now so you're not blocked later.

---

## 4. Connect your recraft.ai account

**No test image is generated and no credits are spent during login.**

### Easiest way — the web app
```bash
python app.py
```
Your browser opens at `http://127.0.0.1:7860`.

1. Go to the **Account Manager** tab.
2. Type a short name for your account (e.g. `main`) in **Account name**.
3. Click **Add New Account (browser capture)**.
4. A browser window opens at recraft.ai → **log in** with your account.
   - If a Cloudflare checkbox appears, click it yourself.
5. That's it — the capture **finishes automatically the moment login is
   detected**. The account appears in the pool table with its live credits.

### CLI alternative
```bash
python setup_session.py                # -> recraft_session.json
# or with a name:  python setup_session.py --name alt1   # -> sessions/alt1.json
```
Log in in the opened browser, then press ENTER in the terminal.

> 🔐 Your login cookies are saved locally to `sessions/<name>.json` —
> **treat that file like a password**. It is git-ignored and never leaves
> your machine.

---

## 5. Generate something

### Single image (quick test)
**Single Playground** tab → type a prompt → **Generate**.
- Tick **Crisp 4K Upscale** for a 4096×4096 result.
- Tick **Remove Background** to also get a transparent PNG cutout.

Result lands in `output/playground/` — a stock-compliant JPEG (sRGB,
quality 100, IPTC/XMP/EXIF metadata embedded) plus the transparent PNG.

### Batch run
1. Prepare a CSV (see `sample_batch.csv`):
   ```csv
   prompt,title,description,tags,category
   "a red fox sitting in fresh snow at dusk",Red Fox In Snow,"A red fox in snow.","animal, fox, snow, winter",Animals
   ```
   - `prompt` and `tags` are required; `title`/`description` fall back to
     the prompt; max **49** keywords.
2. **Batch Generator** tab → upload the CSV → review the queue (each row
   has its own **Remove** button) → **Start Batch**.
3. Watch progress live; finished rows vanish from the queue in real time.

### Output structure
```
output/Batch_2026-09-19_14-30-00/
├── jpg/
│   ├── 001_red_fox_in_snow.jpg   ← pure-white BG, IPTC metadata embedded
│   └── ...
└── png/
    ├── 001_red_fox_in_snow.png   ← transparent cutout (if Remove BG on)
    └── metadata.csv              ← Adobe Stock companion metadata
```
Stopped mid-run? Re-upload `remaining_prompts.csv` from the batch folder
to resume where you left off.

---

## 6. Credits & multi-account

| Operation | Cost |
|---|---|
| Generate 1024×1024 | 1 credit |
| Crisp 4K Upscale | +1 credit |
| Remove Background | +1 credit |

Free plan = 50 credits/month (roughly 16 fully-processed images).

Add more accounts in **Account Manager → Add New Account** — the pool
rotates between them automatically and fails over when one runs dry.
Check balances anytime with **Refresh Credits**.

---

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| `playwright: command not found` | Run `python -m playwright install chromium` |
| Capture times out | Make sure you actually **log in** in the opened browser window; the capture auto-finishes on login |
| Cloudflare checkbox keeps appearing | Click it manually once — the browser profile remembers |
| `session cookies expired` | Delete `sessions/<name>.json` and re-add the account (Section 4) |
| `all accounts exhausted` | Add credits/accounts, or untick upscale/background-removal to spend less |
| Port 7860 busy | Another instance is running — close it, or edit the port at the bottom of `app.py` |
| PNG has no transparency | Check the batch log — it warns explicitly if background removal didn't run (usually out of credits) |

---

## 8. Daily usage

```bash
# activate the venv, then:
python app.py          # web UI at http://127.0.0.1:7860
```

CLI one-off generation:
```bash
python generate.py "a red fox in snow" -v     # -> ./images
python generate.py --check                    # session live? credits?
```

---

*The repo contains no credentials — every user logs in with their own
recraft.ai account and spends only their own credits.*
