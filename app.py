#!/usr/bin/env python3
"""
app.py — Stock Batch Generator web UI (Gradio).

Run:  python app.py     ->  http://127.0.0.1:7860

Tabs
----
1. Batch Generator   — upload the stock CSV, watch rows flow through
                       generate -> crisp 4K upscale -> sRGB JPEG + IPTC.
2. Account Manager   — pool table with live credits, add accounts
                       (stealth browser capture), refresh, remove.
3. Single Playground — one prompt, instant compliant JPEG + metadata view.

Uses your own recraft.ai sessions in ./sessions/*.json and spends their
credits. Automating recraft.ai may violate their ToS — contributor
uploading is fine, the generation method is your responsibility.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from pathlib import Path

import gradio as gr

from account_manager import AccountPool
from batch_processor import BatchRunner, read_batch_csv
from metadata_helper import (cutout_to_dual_export, read_back_metadata,
                             webp_to_stock_jpeg)
from recraft_core import PROFILE_DIR, RecraftError, capture_session

POOL = AccountPool(min_delay=5.0, max_delay=10.0)
RUNNER = BatchRunner(POOL)
CAPTURE = {"running": False, "log": "", "account": None, "error": None}
_LAST_ROWS: list[dict] = []


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------
def status_rows() -> list:
    rows = []
    for s in POOL.status_table():
        rows.append([s["name"], s["email"], s["plan"], s["credits"],
                     s["state"], s["last_used"], s["error"][:60]])
    return rows or [["(none)", "", "", "", "add an account", "", ""]]


# ---------------------------------------------------------------------------
# Tab 1 — Batch Generator
# ---------------------------------------------------------------------------
def _drop_row(rows, idx, row_num):
    """Remove one row from the queue state (per-row Remove button)."""
    global _LAST_ROWS
    if RUNNER.is_running():
        RUNNER.remove_rows([int(row_num)])
    else:
        _LAST_ROWS = [r for r in _LAST_ROWS if r["_row"] != int(row_num)]
        RUNNER.rows = list(_LAST_ROWS)
        RUNNER._queue = list(_LAST_ROWS)
    return rows[:idx] + rows[idx + 1:]


def on_upload_csv(file):
    global _LAST_ROWS
    if not file:
        return gr.update(), gr.update()
    try:
        _LAST_ROWS = read_batch_csv(file)
    except Exception as e:
        raise gr.Error(f"CSV rejected: {e}")
    RUNNER.rows = list(_LAST_ROWS)
    if not RUNNER.running:
        RUNNER._queue = list(_LAST_ROWS)
    return (queue_preview(), f"CSV OK — {len(_LAST_ROWS)} row(s) parsed "
                             f"and validated.")


def queue_rows() -> list[list]:
    """Live queue rows for the dynamic per-row UI: while a batch runs,
    finished rows are auto-removed; otherwise it mirrors the uploaded CSV
    (minus manually deleted rows). [row#, prompt, title]"""
    if RUNNER.is_running():
        return RUNNER.queue_table()
    return [[r["_row"], r["prompt"][:70], r["title"][:40]]
            for r in _LAST_ROWS]


def queue_preview():
    return queue_rows()


def on_start_batch(upscale, remove_bg, min_delay, max_delay):
    if RUNNER.is_running():
        raise gr.Error("A batch is already running.")
    if not _LAST_ROWS:
        raise gr.Error("Upload a CSV first.")
    POOL.min_delay, POOL.max_delay = float(min_delay), float(max_delay)
    RUNNER.start(_LAST_ROWS, upscale=bool(upscale),
                 remove_bg=bool(remove_bg))
    return (gr.update(interactive=False), gr.update(interactive=True),
            queue_preview())


def on_stop_batch():
    RUNNER.stop()
    return gr.update()


def poll_batch():
    """Refresh progress bar, log box and last-image preview."""
    st = RUNNER.status()
    prog = st["progress"]
    label = (f"{st['ok']} ok / {st['failed']} failed / {st['total']} total"
             + ("  — running" if st["running"] else "  — finished"
                if st["finished"] else ""))
    last = None
    meta_json = ""
    if st["last_file"] and Path(st["last_file"]).exists():
        last = st["last_file"]
        try:
            m = read_back_metadata(st["last_file"])
            meta_json = (f"title: {m['iptc']['ObjectName (2:05)']}\n"
                         f"caption: {str(m['iptc']['Caption-Abstract (2:120)'])[:120]}…\n"
                         f"keywords ({len(m['iptc']['Keywords (2:25)'])}): "
                         f"{', '.join(m['iptc']['Keywords (2:25)'][:12])}…\n"
                         f"colour: {m['icc']} | xmp: {m['xmp_present']} | "
                         f"size: {m['size']}")
        except Exception:
            meta_json = ""
    return (prog, label, RUNNER.log_tail(60), last, meta_json,
            queue_preview(),
            gr.update(interactive=not st["running"]),
            gr.update(interactive=st["running"]))


# ---------------------------------------------------------------------------
# Tab 2 — Account Manager
# ---------------------------------------------------------------------------
def on_refresh_credits():
    POOL.refresh_all_credits(log=lambda m: None)
    return status_rows()


def _capture_worker(name: str, timeout: int):
    CAPTURE.update(running=True, log="", account=None, error=None)
    profile = PROFILE_DIR / name
    profile.mkdir(parents=True, exist_ok=True)
    try:
        def log(msg):
            CAPTURE["log"] += str(msg) + "\n"
        session = capture_session(profile, log=log, timeout=timeout)
        POOL.add_account(name, session)
        CAPTURE["account"] = name
        log(f"[OK] account '{name}' captured and added to the pool.")
    except Exception as e:
        CAPTURE["error"] = str(e)
        CAPTURE["log"] += f"\n[X] {e}\n{traceback.format_exc(limit=3)}"
    finally:
        CAPTURE["running"] = False


def on_add_account(name, timeout):
    name = (name or "").strip()
    if not name:
        raise gr.Error("Give the account a short name (e.g. 'alt1').")
    if (POOL.sessions_dir / f"{name}.json").exists():
        raise gr.Error(f"Account '{name}' already exists — delete it first.")
    if CAPTURE["running"]:
        raise gr.Error("Another capture is already running.")
    threading.Thread(target=_capture_worker, args=(name, int(timeout)),
                     daemon=True).start()
    return (f"[*] browser opening for '{name}' — just log in to recraft.ai.\n"
            f"Take your time (Google 2FA / email login): you have up to "
            f"{int(timeout)}s. A green 'Confirm Login' button appears in "
            f"the browser — click it when you're done, or the capture "
            f"finishes automatically once the session is valid and the "
            f"page reaches /project/. Zero credits spent.")


def poll_capture2():
    if CAPTURE["running"]:
        return CAPTURE["log"] or "(waiting for browser…)", gr.update()
    done = CAPTURE["account"]
    if done:
        CAPTURE["account"] = None
    return CAPTURE["log"], (status_rows() if done else gr.update())


def on_delete_account(name):
    name = (name or "").strip()
    if not name or name == "(none)":
        raise gr.Error("Type the account name to delete.")
    try:
        POOL.remove_account(name)
    except KeyError:
        raise gr.Error(f"No account named '{name}'.")
    return status_rows()


# ---------------------------------------------------------------------------
# Tab 3 — Single Playground
# ---------------------------------------------------------------------------
def on_playground(prompt, title, description, tags, upscale, remove_bg,
                  seed):
    if not prompt.strip():
        raise gr.Error("Enter a prompt.")
    out_dir = Path("output/playground")
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / f"_draft_{int(time.time())}.webp"
    logs = []
    try:
        POOL.generate_with_failover(
            prompt, raw, upscale=bool(upscale), remove_bg=bool(remove_bg),
            log=logs.append, max_wait=240,
            seed=int(seed) if str(seed).strip() not in ("", "None") else None)
    except RecraftError as e:
        raise gr.Error(str(e))
    final = out_dir / f"playground_{int(time.time())}.jpg"
    png_out = None
    try:
        if remove_bg:
            png_out = out_dir / f"playground_{int(time.time())}.png"
            cutout_to_dual_export(raw, png_out, final,
                                  title=title or prompt,
                                  description=description or prompt,
                                  keywords=tags or prompt)
        else:
            webp_to_stock_jpeg(raw, final,
                               title=title or prompt,
                               description=description or prompt,
                               keywords=tags or prompt)
    except Exception as e:
        raise gr.Error(f"metadata step failed: {e}")
    raw.unlink(missing_ok=True)
    m = read_back_metadata(final)
    meta = (f"title: {m['iptc']['ObjectName (2:05)']}\n"
            f"caption: {m['iptc']['Caption-Abstract (2:120)']}\n"
            f"keywords ({len(m['iptc']['Keywords (2:25)'])}): "
            f"{', '.join(m['iptc']['Keywords (2:25)'])}\n"
            f"colour: {m['icc']}\nresolution: {m['size'][0]}x{m['size'][1]}")
    if png_out:
        from batch_processor import _png_has_alpha
        meta += (f"\nPNG: {png_out.name} — "
                 + ("TRANSPARENT (alpha OK)" if _png_has_alpha(png_out)
                    else "NO ALPHA (bg removal failed!)"))
    return final, meta, (str(png_out) if png_out else final), \
        "\n".join(logs[-15:])


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
with gr.Blocks(title="Recraft Stock Batch") as demo:
    gr.Markdown("# Recraft → Stock Batch Generator\n"
                "Adobe Stock / Shutterstock compliant pipeline: 4096×4096 "
                "crisp upscale, sRGB JPEG @100 %, IPTC + XMP + EXIF "
                "metadata auto-populate on upload.")

    # ---------------- Tab 1 ----------------
    with gr.Tab("Batch Generator"):
        csv_file = gr.File(label="Upload stock CSV "
                                 "(columns: prompt, title?, description?, "
                                 "tags, category?)", type="filepath")
        info = gr.Textbox(label="Status", interactive=False)
        with gr.Row():
            upscale_cb = gr.Checkbox(True, label="Crisp Upscale "
                                                 "(4096×4096, +1 credit/img)")
            remove_bg_cb = gr.Checkbox(False, label="Remove Background "
                                                    "(Isolated White JPG + "
                                                    "Transparent PNG, "
                                                    "+1 credit/img)")
            min_d = gr.Number(5, label="Min delay / account (s)")
            max_d = gr.Number(10, label="Max delay / account (s)")
        with gr.Row():
            start_b = gr.Button("Start Batch", variant="primary")
            stop_b = gr.Button("Stop", variant="stop", interactive=False)
        gr.Markdown("**Queue** — finished rows vanish in real time. Each "
                    "row has its own *Remove* button (before or during the "
                    "run).")
        queue_state = gr.State([])

        @gr.render(inputs=queue_state)
        def render_queue(rows):
            for i, r in enumerate(rows or []):
                with gr.Row(equal_height=True):
                    gr.Markdown(f"**#{r[0]}** — {r[1]}"
                                + (f"  ·  *{r[2]}*" if r[2] else ""))
                    rm = gr.Button("Remove", size="sm", variant="stop",
                                   scale=0, min_width=100)
                    rm.click(fn=lambda i=i, rows=rows, rn=r[0]: (
                        _drop_row(rows, i, rn)), outputs=queue_state)
        prog_bar = gr.Slider(0, 1, value=0, interactive=False,
                             label="Progress")
        prog_lbl = gr.Textbox(label="Counts", interactive=False)
        log_box = gr.Textbox(label="Live log", lines=14, interactive=False)
        with gr.Row():
            preview_img = gr.Image(label="Last finished JPEG", type="filepath")
            meta_box = gr.Textbox(label="Embedded IPTC (auto-populates on "
                                        "portal upload)", lines=10,
                                  interactive=False)

        csv_file.change(on_upload_csv, csv_file, [queue_state, info])
        start_b.click(on_start_batch, [upscale_cb, remove_bg_cb, min_d, max_d],
                      [start_b, stop_b, queue_state])
        stop_b.click(on_stop_batch, None, None)
        timer = gr.Timer(2.0)
        timer.tick(poll_batch, None,
                   [prog_bar, prog_lbl, log_box, preview_img, meta_box,
                    queue_state, start_b, stop_b])

    # ---------------- Tab 2 ----------------
    with gr.Tab("Account Manager"):
        acct_tbl = gr.Dataframe(
            headers=["account", "email", "plan", "credits", "state",
                     "last used", "error"],
            value=status_rows(), label="Account pool", interactive=False)
        with gr.Row():
            refresh_b = gr.Button("Refresh Credits")
        refresh_b.click(on_refresh_credits, None, acct_tbl)
        gr.Markdown("---\n### Add new account\n"
                    "Opens a stealth browser: just log in to recraft.ai "
                    "(Google 2FA / email login included). A green 'Confirm "
                    "Login' button appears in the browser — click it when "
                    "done, or the capture ends automatically once the "
                    "session is valid and the page reaches /project/. "
                    "No test image, no credits spent.")
        with gr.Row():
            acc_name = gr.Textbox(label="Account name", placeholder="alt1")
            acc_timeout = gr.Number(600, label="Capture timeout (s)")
            add_b = gr.Button("Add New Account (browser capture)",
                              variant="primary")
        cap_status = gr.Textbox(label="Capture status", interactive=False)
        cap_log = gr.Textbox(label="Capture log", lines=10,
                             interactive=False)
        del_name = gr.Textbox(label="Delete account by name")
        del_b = gr.Button("Delete Account", variant="stop")
        add_b.click(on_add_account, [acc_name, acc_timeout], cap_status)
        del_b.click(on_delete_account, del_name, acct_tbl)
        cap_timer = gr.Timer(3.0)
        cap_timer.tick(poll_capture2, None, [cap_log, acct_tbl])

    # ---------------- Tab 3 ----------------
    with gr.Tab("Single Playground"):
        pg_prompt = gr.Textbox(label="Prompt", lines=2,
                               placeholder="a red fox in snow, cinematic")
        with gr.Row():
            pg_title = gr.Textbox(label="Title (blank = prompt)")
            pg_tags = gr.Textbox(label="Tags CSV (blank = from prompt)",
                                 placeholder="fox, animal, snow, winter")
        pg_desc = gr.Textbox(label="Description (blank = prompt)", lines=2)
        with gr.Row():
            pg_up = gr.Checkbox(True, label="Crisp 4K Upscale")
            pg_rm = gr.Checkbox(False, label="Remove Background "
                                             "(white JPG + transparent PNG)")
            pg_seed = gr.Textbox(label="Seed (optional)", value="")
            pg_btn = gr.Button("Generate", variant="primary")
        with gr.Row():
            pg_img = gr.Image(label="Result", type="filepath")
            with gr.Column():
                pg_meta = gr.Textbox(label="Compliance report", lines=10,
                                     interactive=False)
                pg_file = gr.File(label="Download compliant JPEG")
                pg_png = gr.File(label="Download transparent PNG")
        pg_log = gr.Textbox(label="Log", lines=6, interactive=False)

        def pg_run(*a):
            img, meta, png, log = on_playground(*a)
            return img, meta, img, png, log
        pg_btn.click(pg_run, [pg_prompt, pg_title, pg_desc, pg_tags,
                              pg_up, pg_rm, pg_seed],
                     [pg_img, pg_meta, pg_file, pg_png, pg_log])

    gr.Markdown("*Sessions live in `./sessions/` — treat them like "
                "passwords. This tool spends your recraft.ai credits and "
                "automates private endpoints (ToS risk is yours).*")

if __name__ == "__main__":
    # Local/desktop use. For a real server run `server.py` (or the systemd
    # unit in deploy/) instead — it binds 0.0.0.0 behind a reverse proxy and
    # adds login protection.
    demo.launch(server_name=os.environ.get("RECRAFT_HOST", "127.0.0.1"),
                server_port=int(os.environ.get("RECRAFT_PORT", "7860")),
                inbrowser=True, theme=gr.themes.Soft())
