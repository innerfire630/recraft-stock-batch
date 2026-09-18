#!/usr/bin/env python3
"""
batch_processor.py — Stock CSV -> compliant JPEG pipeline.

Input CSV columns (header required, case/space-insensitive):
    prompt        (required)  recraft text prompt
    title         (optional)  IPTC ObjectName / file slug base;
                              falls back to the prompt
    description   (optional)  IPTC Caption-Abstract
    tags          (required)  CSV/semicolon list -> IPTC Keywords (max 49)
    category      (optional)  kept in the run report (portals ask for it
                              in their own upload form)

Per row:
    account pool acquire (round-robin + pacing delay + failover)
      -> recraft create -> crisp upscale 4096x4096 (optional)
      -> NATIVE AI background removal (optional, best-effort)
      -> download webp
      -> DUAL export into a timestamped batch folder:
           ./output/Batch_YYYY-MM-DD_HH-MM-SS/
             jpg/NNN_<slug>.jpg   pure-white BG, sRGB q100 + IPTC/XMP/EXIF
             png/NNN_<slug>.png   transparent cutout
             png/metadata.csv     Adobe Stock companion metadata
      -> finished rows are removed from the live queue immediately and
         remaining_prompts.csv is rewritten (resume support)

Stop mid-run with BatchRunner.stop(); resume by re-uploading
remaining_prompts.csv.
"""

from __future__ import annotations

import csv
import json
import threading
import time
import traceback
from pathlib import Path

from account_manager import AccountPool
from metadata_helper import (clean_keywords, clean_text, cutout_to_dual_export,
                             slugify, webp_to_stock_jpeg)
from recraft_core import RecraftError

OUTPUT_DIR = Path("output")
TEMP_DIR = Path("output/_webp")

REQUIRED = ("prompt", "tags")
_ALIASES = {
    "prompt": ("prompt", "text prompt"),
    "title": ("title", "name", "objectname"),
    "description": ("description", "desc", "caption", "abstract"),
    "tags": ("tags", "tag", "keywords", "keyword"),
    "category": ("category", "cat"),
}


def _canon(header):
    out = {}
    for col in _ALIASES:
        for name in header:
            if name and name.strip().lower() in _ALIASES[col]:
                out[col] = name
                break
    return out


def read_batch_csv(path: str | Path) -> list[dict]:
    """Parse + validate the CSV. Returns list of row dicts with a `_row`
    number and cleaned fields. Raises ValueError listing all problems."""
    path = Path(path)
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError("CSV is empty")
    cols = _canon(rows[0].keys())
    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        raise ValueError("CSV is missing required column(s): "
                         + ", ".join(missing))
    parsed, problems = [], []
    for i, r in enumerate(rows, 1):
        prompt = clean_text(r[cols["prompt"]], 3000)
        tags = clean_keywords(r[cols["tags"]])
        if not prompt:
            problems.append(f"row {i}: empty prompt")
            continue
        if not tags:
            problems.append(f"row {i}: no valid tags/keywords")
            continue
        title = clean_text(r[cols["title"]] if "title" in cols else prompt,
                           120)
        desc = clean_text(r[cols["description"]]
                          if "description" in cols else prompt, 2000)
        parsed.append({
            "_row": i, "prompt": prompt, "title": title,
            "description": desc, "tags": tags,
            "category": clean_text(r[cols["category"]]
                                   if "category" in cols else "", 100),
        })
    if problems:
        raise ValueError("; ".join(problems[:10]))
    return parsed


def _png_has_alpha(path: Path) -> bool:
    """True when the PNG actually carries transparency (alpha < 255)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            if im.mode not in ("RGBA", "LA", "PA"):
                return False
            return im.getchannel("A").getextrema()[0] < 255
    except Exception:
        return False


class BatchRunner:
    """Threaded batch worker. UI polls status()/log_tail; stop() is async."""

    def __init__(self, pool: AccountPool, output_dir: Path | str = OUTPUT_DIR):
        self.pool = pool
        self.output_dir = Path(output_dir)
        self.temp_dir = self.output_dir / "_webp"
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.rows: list[dict] = []
        self.results: list[dict] = []
        self.current = -1
        self.log_lines: list[str] = []
        self.running = False
        self.finished = False
        self.last_file: Path | None = None
        self.batch_dir: Path | None = None
        self._queue: list[dict] = []      # live (not-yet-done) rows
        self._opts: dict = {}

    # -- logging --------------------------------------------------------------
    def _log(self, msg: str):
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.log_lines.append(f"[{stamp}] {msg}")
            if len(self.log_lines) > 2000:
                del self.log_lines[:-2000]

    def log_tail(self, n=40) -> str:
        with self._lock:
            return "\n".join(self.log_lines[-n:])

    # -- lifecycle ------------------------------------------------------------
    def start(self, rows: list[dict], upscale: bool = True,
              remove_bg: bool = False):
        if self.running:
            raise RuntimeError("a batch is already running")
        self.rows = rows
        self._opts = {"upscale": bool(upscale), "remove_bg": bool(remove_bg)}
        self.results = []
        self.current = -1
        self.finished = False
        self.last_file = None
        self.batch_dir = self.output_dir / (
            "Batch_" + time.strftime("%Y-%m-%d_%H-%M-%S"))
        (self.batch_dir / "jpg").mkdir(parents=True, exist_ok=True)
        (self.batch_dir / "png").mkdir(parents=True, exist_ok=True)
        self._queue = list(rows)
        self._write_remaining_csv()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.running = True
        self._thread.start()

    # -- dynamic queue management ---------------------------------------------
    def queue_table(self) -> list[list]:
        """Live remaining-queue rows for the UI table (finished rows vanish
        in real time)."""
        with self._lock:
            return [[r["_row"], r["prompt"][:70], r["title"][:40],
                     ", ".join(r["tags"][:8]), len(r["tags"]),
                     r["category"]]
                    for r in self._queue]

    def remove_rows(self, row_numbers: list[int]) -> int:
        """Manually delete rows (by their original CSV row number) from the
        queue before/during a run. Returns how many were removed."""
        removed = 0
        with self._lock:
            keep = []
            drop = set(row_numbers)
            for r in self._queue:
                if r["_row"] in drop:
                    removed += 1
                else:
                    keep.append(r)
            self._queue = keep
            self.rows = [r for r in self.rows if r["_row"] not in drop]
        if removed:
            self._log(f"removed {removed} row(s) from the queue")
            self._write_remaining_csv()
        return removed

    def _pop_current(self, row_num: int):
        """Remove a finished row from the live queue (auto-remove on
        completion)."""
        with self._lock:
            self._queue = [r for r in self._queue if r["_row"] != row_num]

    def _write_remaining_csv(self):
        """remaining_prompts.csv in the batch folder — re-upload it to
        resume an interrupted batch seamlessly."""
        if not self.batch_dir:
            return
        path = self.batch_dir / "remaining_prompts.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["prompt", "title", "description", "tags", "category"])
            for r in self._queue:
                w.writerow([r["prompt"], r["title"], r["description"],
                            ", ".join(r["tags"]), r["category"]])

    def stop(self):
        self._stop.set()
        self._log("stop requested — finishing current row then halting")

    def is_running(self) -> bool:
        return self.running

    def progress(self) -> float:
        total = max(len(self.rows), 1)
        done = sum(1 for r in self.results if r["status"] in ("ok", "failed"))
        return min(done / total, 1.0)

    def status(self) -> dict:
        ok = sum(1 for r in self.results if r["status"] == "ok")
        fail = sum(1 for r in self.results if r["status"] == "failed")
        return {
            "running": self.running,
            "finished": self.finished,
            "total": len(self.rows),
            "ok": ok, "failed": fail,
            "current": self.current,
            "progress": self.progress(),
            "last_file": str(self.last_file) if self.last_file else None,
        }

    # -- the pipeline -----------------------------------------------------------
    def _run(self):
        try:
            self._log(f"batch started: {len(self.rows)} row(s) -> "
                      f"{self.batch_dir}")
            self._log(f"options: upscale={self._opts.get('upscale')} "
                      f"remove_bg={self._opts.get('remove_bg')}, accounts: "
                      f"{', '.join(a.name for a in self.pool.candidates())}")
            for idx, row in enumerate(list(self.rows)):
                if self._stop.is_set():
                    self._log("stopped by user")
                    break
                self.current = idx
                t0 = time.time()
                try:
                    out = self._process_row(row)
                    self.results.append({"_row": row["_row"],
                                         "status": "ok", "file": str(out),
                                         "seconds": round(time.time() - t0, 1)})
                    self._log(f"row {row['_row']:>3}: OK -> {out.name} "
                              f"({time.time() - t0:.0f}s)")
                except Exception as e:  # noqa: BLE001 — keep the batch alive
                    self.results.append({"_row": row["_row"],
                                         "status": "failed",
                                         "error": str(e)[:300]})
                    self._log(f"row {row['_row']:>3}: FAILED — {e}")
                    self._log(traceback.format_exc(limit=2))
                finally:
                    # dynamic queue: finished rows vanish in real time
                    self._pop_current(row["_row"])
                    self._write_remaining_csv()
            self._write_report()
        finally:
            self.running = False
            self.finished = True
            self.current = -1
            self._log(f"batch finished: "
                      f"{sum(1 for r in self.results if r['status'] == 'ok')}"
                      f" ok / "
                      f"{sum(1 for r in self.results if r['status'] == 'failed')}"
                      f" failed")

    def _process_row(self, row: dict) -> Path:
        slug = slugify(row["title"] or row["prompt"])
        jpg = self.batch_dir / "jpg" / f"{row['_row']:03d}_{slug}.jpg"
        png = self.batch_dir / "png" / f"{row['_row']:03d}_{slug}.png"
        raw_webp = self.temp_dir / f"{row['_row']:03d}_{slug}.webp"

        def log(m):
            self._log(f"  r{row['_row']}: {m}")

        gen = self.pool.generate_with_failover(
            row["prompt"], raw_webp,
            upscale=self._opts.get("upscale", True),
            remove_bg=self._opts.get("remove_bg", False),
            width=1024, height=1024, log=log, max_wait=240)

        if self._opts.get("remove_bg", False):
            # dual export: transparent PNG + pure-white JPG with IPTC
            meta = cutout_to_dual_export(
                raw_webp, png, jpg,
                title=row["title"], description=row["description"],
                keywords=row["tags"])
            self._append_png_metadata_csv(row, slug)
            transparent = _png_has_alpha(png)
            if gen.get("bg_removed") and transparent:
                log(f"dual export: TRANSPARENT 4K PNG (cut at 1024, base "
                    f"upscaled to 4K, mask composited) + white JPG, "
                    f"{meta['n_keywords']} keywords, "
                    f"title '{meta['title'][:40]}'")
            elif gen.get("bg_removed"):
                log("[!] WARNING: bg-removal ran but the saved PNG has no "
                    "alpha — check the downloaded image")
            else:
                log("[!] WARNING: PNG saved WITHOUT transparency — "
                    "background removal did not run (quota/endpoint issue; "
                    "see warnings above)")
            final = jpg
        else:
            meta = webp_to_stock_jpeg(
                raw_webp, jpg,
                title=row["title"], description=row["description"],
                keywords=row["tags"])
            log(f"metadata: {meta['n_keywords']} keywords, title "
                f"'{meta['title'][:40]}'")
            final = jpg
        self.last_file = final
        raw_webp.unlink(missing_ok=True)  # keep only compliant exports
        return final

    def _append_png_metadata_csv(self, row: dict, slug: str):
        """Adobe Stock companion CSV for the PNG folder:
        Filename, Title, Keywords, Category (appended per finished row)."""
        path = self.batch_dir / "png" / "metadata.csv"
        new_file = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["Filename", "Title", "Keywords", "Category"])
            w.writerow([f"{row['_row']:03d}_{slug}.png",
                        row["title"], ", ".join(row["tags"]),
                        row["category"]])

    def _write_report(self):
        report = {
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "results": self.results,
        }
        (self.output_dir / "batch_report.json").write_text(
            json.dumps(report, indent=1), encoding="utf-8")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python batch_processor.py <batch.csv> [--dry-run]")
        sys.exit(1)
    rows = read_batch_csv(sys.argv[1])
    print(f"CSV OK: {len(rows)} row(s) parsed")
    for r in rows[:5]:
        print(f"  row {r['_row']}: {r['prompt'][:50]}... | "
              f"{len(r['tags'])} tags")
    if "--dry-run" not in sys.argv:
        pool = AccountPool()
        runner = BatchRunner(pool)
        runner.start(rows)
        while runner.is_running():
            time.sleep(2)
            print(runner.log_tail(3))
        print(runner.status())
