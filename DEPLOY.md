# Deploying to your own server (aaPanel / VPS)

Host this Gradio app on a server you control. The local flow is
`python app.py`; a real deployment is **app behind nginx, run by systemd** —
and the whole app is *not* exposed to the internet, only to your reverse
proxy.

> ⚠️ **Read first.** This tool replays your logged-in recraft.ai session over
> **private web endpoints**, which is very likely against recraft.ai's Terms
> of Service (that is what the paid API is for). It also spends **your**
> credits and stores **live login cookies** on the server. Run it at your own
> risk, at human-like rates, and never expose it without a password.

---

## What the deployment looks like

```
Browser ──https──► nginx (aaPanel, port 443, TLS) ──http──► Gradio app
                                                                  │
                                                                  ├── output/   stock JPEGs
                                                                  └── sessions/ live recraft.ai cookies
                                    systemd keeps the app running (auto-restart)
```

Two servers, one port, two processes. You never open the app's port to the
world.

---

## 1. Requirements

On the server (aaPanel panel → **App Store** is the easiest route):

| Need | Version | Notes |
|---|---|---|
| Python | 3.10+ | 3.11/3.12 recommended |
| nginx | any | aaPanel installs it for you |
| OpenSSL/Git | any | to clone the repo |

aaPanel → **App Store** → install **Python项目管理器 (Python Project Manager)**
if you want aaPanel to manage the process instead of systemd. Both are
covered below — systemd is the more predictable option.

---

## 2. Get the code on the server

```bash
# From your machine, push the repo (sessions/ and .env stay git-ignored):
git add -A && git commit -m "server deploy support" && git push

# On the server:
cd /www/wwwroot
git clone <your-repo-url> recraft
cd recraft
```

Or copy the folder over SFTP/cPanel. The repo contains **no credentials** —
you add your own account on the server (step 7).

---

## 3. Install

The bundled script does everything and is safe to re-run:

```bash
sudo bash install.sh
```

It installs system packages, builds `.venv`, installs `requirements.txt` +
`uvicorn`, creates the writable dirs, writes a **random password** into
`.env`, and installs + starts the systemd unit. It prints your login once —
**save it**.

<details>
<summary>Prefer to do it by hand?</summary>

```bash
sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
cd /www/wwwroot/recraft
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install "uvicorn[standard]"

mkdir -p output images sessions .pw-profiles
sudo chown -R www:www /www/wwwroot/recraft
chmod 700 sessions .pw-profiles

cp .env.example .env
# edit .env: set RECRAFT_PASSWORD (and RECRAFT_ROOT_PATH if using a sub-path)
chmod 600 .env

sudo cp deploy/recraft.service /etc/systemd/system/recraft.service
sudo systemctl daemon-reload && sudo systemctl enable --now recraft
```
</details>

---

## 4. Configure (`.env`)

Everything is environment-driven — no code edits.

| Variable | Default | What it does |
|---|---|---|
| `RECRAFT_HOST` | `127.0.0.1` | Bind address. **Leave on loopback** — nginx proxies to it. |
| `RECRAFT_PORT` | `7860` | Port nginx proxies to. |
| `RECRAFT_ROOT_PATH` | *(empty)* | `/recraft` to serve under a sub-path; empty for a subdomain. |
| `RECRAFT_AUTH` | `1` on Linux | `0` disables login — **trusted LAN only**. |
| `RECRAFT_USER` / `RECRAFT_PASSWORD` | `recraft` / — | Login. Startup **aborts** if auth is on and the password is empty. |
| `RECRAFT_MAX_UPLOAD` | `200` | MB. Keep nginx's `client_max_body_size` ≥ this. |
| `RECRAFT_ALLOWED_HOSTS` | `*` | Tighten to your domain if you like. |

The systemd unit reads the same `.env`, so a password change is:

```bash
vi /www/wwwroot/recraft/.env && sudo systemctl restart recraft
```

---

## 5. Reverse proxy (nginx via aaPanel)

Pick **one**. The `deploy/nginx-recraft.conf` file contains both, annotated.

### Option A — sub-path (no extra DNS)

1. `.env` → `RECRAFT_ROOT_PATH=/recraft`
2. aaPanel → **Website → your existing site → Config File**, and paste the
   `location /recraft/ { ... }` block from `deploy/nginx-recraft.conf` into
   the `server{}` block.
3. Also raise the upload limit — add to that `server{}`:
   `client_max_body_size 250m;`
4. Save. aaPanel validates the config; if it complains about
   `$recraft_connection`, add the `map` block from the comments at the top of
   the nginx file to nginx's main `http{}` config, or substitute `upgrade`.
5. Visit `https://your-domain.com/recraft`.

### Option B — subdomain (cleaner)

1. DNS `A` record: `recraft.your-domain.com` → server IP.
2. aaPanel → **Website → Add Site** → that subdomain (PHP: *pure static*).
3. Open its **Config File** and paste the **whole** block from
   `deploy/nginx-recraft.conf`, updating `server_name` and the two
   `ssl_certificate` paths (apply SSL in aaPanel → Site → SSL → Let's Encrypt
   after the site exists).
4. `.env` → `RECRAFT_ROOT_PATH=` (empty).
5. Visit `https://recraft.your-domain.com`.

> **Why those settings:** `proxy_buffering off` + long `proxy_read_timeout`
> are required for Gradio's SSE progress stream — with buffering on, the live
> log/progress bar appears frozen. `client_max_body_size` must exceed
> `RECRAFT_MAX_UPLOAD` or large uploads fail silently at the 1 MB default.

---

## 6. Verify

```bash
curl -s http://127.0.0.1:7860/healthz     # {"status":"ok","accounts":[...]}
sudo systemctl status recraft
sudo journalctl -u recraft -f            # live logs
```

* Open the site → you should get the **login screen** (proof auth is on).
* Log in → all three tabs render.
* Accounts table shows your session(s).

---

## 7. Adding accounts on a headless server ⚠️

The "Add New Account" button opens a **real browser window** for you to log
in. A server has no display, so that button cannot work there. Two supported
routes:

**A) Capture on your own machine, upload the file (simplest).**
1. On your PC, in this repo run `python setup_session.py` (or add the account
   in the web UI) → creates `sessions/<name>.json`.
2. Upload that file to `/www/wwwroot/recraft/sessions/` (SFTP or
   `scp`). Permissions matter:
   ```bash
   sudo chown www:www sessions/<name>.json && sudo chmod 600 sessions/<name>.json
   ```
3. Restart (or just click **Refresh Credits**): `sudo systemctl restart recraft`.
4. **Delete the file from your PC** when done — it is a live credential.

**B) Real Chrome on the server + VNC/x11vnc.** Heavier: install Chrome, then
a VNC server, capture through the UI. Only worth it for many accounts. The app
already prefers a system Chrome over bundled Chromium, and warns clearly if
no display is available.

---

## 8. Day-to-day

| Task | Command |
|---|---|
| Logs | `sudo journalctl -u recraft -f` |
| Restart | `sudo systemctl restart recraft` |
| Update code | `cd /www/wwwroot/recraft && git pull && sudo systemctl restart recraft` |
| Reinstall deps | `sudo /www/wwwroot/recraft/.venv/bin/pip install -r requirements.txt && sudo systemctl restart recraft` |
| Generated images | `ls /www/wwwroot/recraft/output/Batch_*/jpg/` |
| Download a batch | open the folder in the UI, or `scp` it down |

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `FATAL: RECRAFT_AUTH is on but RECRAFT_PASSWORD is empty` | Set it in `.env` (or `RECRAFT_AUTH=0` on a trusted LAN only). |
| `502 Bad Gateway` from nginx | App not running/port mismatch: `systemctl status recraft`; confirm `RECRAFT_PORT` matches nginx's `upstream`. |
| Login page loops / 404 on the UI | `RECRAFT_ROOT_PATH` doesn't match the nginx location. Sub-path set? Use `/recraft/…`. |
| Page loads but progress bar never moves | nginx buffering the SSE stream — ensure `proxy_buffering off`. |
| Upload fails silently | nginx `client_max_body_size` (default 1 MB!) < your file. Raise to ≥ `RECRAFT_MAX_UPLOAD`. |
| Blank white page | Disable SSR: `RECRAFT_SSR=0`, restart. |
| WebSocket/`Upgrade` errors in nginx log | Add the `map` from the nginx file's header, or use `Connection "upgrade"`. |
| Account shows 0 credits / "session dead" | Session cookies expired. Re-capture (step 7) — the JWT refreshes every run, but the login cookies have a longer life. |
| Add-account button does nothing | Expected on a headless server — see step 7. |

---

## Security checklist

- [ ] `RECRAFT_AUTH=1` and a **strong** random password (not `change-me`)
- [ ] App bound to `127.0.0.1`; only nginx faces the internet
- [ ] TLS enabled (aaPanel → SSL)
- [ ] `chmod 700 sessions .pw-profiles` and `chmod 600 .env`
- [ ] `.env` and `sessions/` are git-ignored — never commit them
- [ ] Only your own IP needs access? add an IP allow/deny in the nginx config
- [ ] Remember: `sessions/*.json` **are** credentials — rotate by re-capturing
