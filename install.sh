#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install.sh — bootstrap the Recraft Stock Generator on a Debian/Ubuntu server
#              (tested layout: aaPanel, which is CentOS/Ubuntu based).
#
#   sudo bash install.sh
#
# What it does:
#   1. installs system packages (Python, ffmpeg-free, build tools)
#   2. creates a virtualenv and installs requirements.txt
#   3. creates writable data dirs owned by the service user
#   4. generates .env with a RANDOM password if you do not have one
#   5. installs + starts the systemd unit
#
# It is safe to re-run: it will not overwrite an existing .env password.
# ---------------------------------------------------------------------------
set -euo pipefail

APP_DIR="${APP_DIR:-/www/wwwroot/recraft}"
SERVICE_USER="${SERVICE_USER:-www}"
SERVICE_GROUP="${SERVICE_GROUP:-www}"
SERVICE_PORT="${SERVICE_PORT:-7860}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fatal]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash install.sh"
[[ -d "$APP_DIR" ]] || die "Project not found at $APP_DIR. Copy the repo there first, or set APP_DIR=/path/to/recraft."

cd "$APP_DIR"

# --- 1. system packages -----------------------------------------------------
say "Installing system packages"
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates
    PYBIN=python3
elif command -v yum >/dev/null 2>&1; then
    yum install -y -q python3 python3-pip curl ca-certificates
    PYBIN=python3
else
    die "Unsupported distro: need apt-get or yum."
fi
command -v "$PYBIN" >/dev/null || die "$PYBIN not available."

# --- 2. virtualenv + python deps ------------------------------------------
say "Creating virtualenv and installing Python dependencies"
[[ -d .venv ]] || "$PYBIN" -m venv .venv
./.venv/bin/python -m pip install --quiet --upgrade pip setuptools wheel
./.venv/bin/python -m pip install --quiet -r requirements.txt
# uvicorn is the ASGI server the systemd unit runs. FastAPI ships with Gradio,
# but uvicorn is a separate package.
./.venv/bin/python -m pip install --quiet "uvicorn[standard]"
say "Installed: $(./.venv/bin/python -m pip list 2>/dev/null | grep -ciE '^(gradio|uvicorn|pillow) ') core packages present"

# --- 3. writable directories + ownership -----------------------------------
say "Preparing data directories"
mkdir -p output images sessions .pw-profiles
# Only the service user needs write access; sessions/ holds live cookies.
chmod 700 sessions .pw-profiles
if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    chown -R "$SERVICE_USER":"$SERVICE_GROUP" "$APP_DIR"
    echo "Ownership set to ${SERVICE_USER}:${SERVICE_GROUP}"
else
    warn "User '$SERVICE_USER' does not exist (not aaPanel?). Leaving files as root — edit deploy/recraft.service yourself."
fi

# --- 4. .env with a random password ----------------------------------------
if [[ -f .env ]]; then
    say ".env already exists — leaving it untouched"
else
    say "Creating .env with a generated password"
    PW="$("$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))')"
    sed -e "s|^RECRAFT_PASSWORD=.*|RECRAFT_PASSWORD=${PW}|" \
        -e "s|^RECRAFT_PORT=.*|RECRAFT_PORT=${SERVICE_PORT}|" \
        -e "s|^# RECRAFT_SECRET=.*|RECRAFT_SECRET=$("$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')|" \
        .env.example > .env
    chmod 600 .env
    cat <<EOF

  ┌───────────────────────────────────────────────────────────┐
  │  Your login (save this now — it is not shown again):      │
  │  user     : recraft                                      │
  │  password : ${PW}
  └───────────────────────────────────────────────────────────┘
EOF
fi

# --- 5. systemd ------------------------------------------------------------
say "Installing systemd unit"
sed -e "s|/www/wwwroot/recraft|${APP_DIR}|g" \
    -e "s|^User=.*|User=${SERVICE_USER}|" \
    -e "s|^Group=.*|Group=${SERVICE_GROUP}|" \
    deploy/recraft.service > /etc/systemd/system/recraft.service

systemctl daemon-reload
systemctl enable recraft >/dev/null 2>&1 || warn "systemctl enable failed (container?)"
systemctl restart recraft
sleep 3

if systemctl is-active --quiet recraft; then
    say "Service is running"
else
    warn "Service did not start. Check: journalctl -u recraft -n 50 --no-pager"
fi

# --- 6. health check -------------------------------------------------------
say "Local health check"
if curl -fsS "http://127.0.0.1:${SERVICE_PORT}/healthz"; then
    echo
else
    warn "No response on 127.0.0.1:${SERVICE_PORT}/healthz"
    warn "If you set RECRAFT_ROOT_PATH=/recraft, the URL is /recraft/healthz"
fi

cat <<EOF

$(say "Done — next steps")
  1. Point a subdomain (e.g. recraft.YOURDOMAIN.com) at this server's IP.
  2. aaPanel -> Website -> your site -> Config File, and paste the
     reverse-proxy block from  deploy/nginx-recraft.conf .
     Match RECRAFT_ROOT_PATH in .env with the nginx location.
  3. Issue an SSL certificate (aaPanel -> Site -> SSL -> Let's Encrypt).
  4. Open the site and sign in with the credentials printed above.

  Useful commands:
    journalctl -u recraft -f          # live logs
    systemctl restart recraft         # restart after code updates
    ls /www/wwwroot/recraft/output    # generated stock images

  IMPORTANT: no account is captured yet. This server is headless, so the
  "Add New Account" browser capture cannot open a window. See the section
  "Adding accounts on a headless server" in DEPLOY.md.
EOF
