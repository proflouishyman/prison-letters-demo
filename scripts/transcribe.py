#!/usr/bin/env python3
"""Transcribe rendered letter pages with a local Ollama vision model (or the PDF text layer baseline).

Usage:
  transcribe.py --model qwen3-vl:8b-instruct-q4_K_M [--post-ids 157294 ...] [--pages 1 2]
  transcribe.py --model pdf-text-layer ...

Writes transcriptions/<post_id>/<model_slug>/page_NNN.json (contract §3). Resumable: pages with a valid
JSON already present are skipped. Per-page timeout 600 s, at most one retry, then a FAILED banner and
non-zero exit. Any model tag containing "cloud" is refused: letters must never leave this machine.
"""
import argparse
import base64
import json
import sqlite3
import subprocess
import time
import urllib.request

from pl_common import (DB_PATH, IMAGE_DPI, PAGES_DIR, PDF_DIR, TRANSCRIPTIONS_DIR, PipelineError, fail,
                       model_slug, now_utc, sha256_text)

OLLAMA = "http://localhost:11434"
TEXT_LAYER = "pdf-text-layer"
TIMEOUT_S = 600
NUM_CTX = 16384    # set explicitly on every call; Ollama's own default is far larger and slower
NUM_PREDICT = 4096  # cap on output tokens; hitting it is treated as a failure, never as a result
PROMPT = ("Transcribe the handwritten or typed text on this page exactly as written. "
          "Keep the line breaks as they appear on the page. Keep the author's own spelling, grammar and "
          "punctuation, including any mistakes; do not correct or modernize anything. "
          "Output only the transcription, with no commentary, headings or explanations. "
          "Write [illegible] in place of any word you cannot read.")
PROMPT_SHA = sha256_text(PROMPT)

# Contract §3 fields and their types.
SPEC = {"post_id": int, "page": int, "model": str, "model_digest": str, "backend": str, "prompt_sha256": str,
        "params": dict, "image_dpi": int, "text": str, "seconds": float, "output_tokens": (int, type(None)),
        "created_at": str}


def validate_record(rec):
    """Raise PipelineError unless rec matches contract §3 exactly."""
    if set(rec) != set(SPEC):
        raise PipelineError(f"record fields mismatch: extra={set(rec) - set(SPEC)} missing={set(SPEC) - set(rec)}")
    for k, t in SPEC.items():
        if not isinstance(rec[k], t) or isinstance(rec[k], bool):
            raise PipelineError(f"field {k} has wrong type {type(rec[k]).__name__}")
    if rec["backend"] not in ("ollama", "pdftotext", "vllm"):
        raise PipelineError(f"bad backend {rec['backend']}")
    if "temperature" not in rec["params"] or "num_ctx" not in rec["params"]:
        raise PipelineError("params must include temperature and num_ctx")
    if rec["backend"] == "ollama" and (rec["params"]["num_ctx"] is None or rec["params"]["temperature"] is None):
        raise PipelineError("ollama params must set temperature and num_ctx explicitly")


def http_json(path, payload=None, timeout=30):
    req = urllib.request.Request(OLLAMA + path, data=None if payload is None else json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def model_digest(tag):
    """Digest of the installed model from /api/tags; fails if the model is not installed."""
    for m in http_json("/api/tags")["models"]:
        if m["name"] == tag or m["model"] == tag:
            return m["digest"]
    raise PipelineError(f"model {tag} is not installed in Ollama (see `ollama list`)")


def run_ollama(tag, image_path):
    """One chat call; returns (text, output_tokens). Raises on any error, empty output, or truncation."""
    img = base64.b64encode(image_path.read_bytes()).decode()
    resp = http_json("/api/chat", {
        "model": tag, "stream": False,
        "messages": [{"role": "user", "content": PROMPT, "images": [img]}],
        "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
    }, timeout=TIMEOUT_S)
    text = resp["message"]["content"]
    if resp.get("done_reason") == "length":
        raise PipelineError(f"output hit num_predict={NUM_PREDICT} (runaway/looping output)")
    if not text.strip():
        raise PipelineError("model returned empty text")
    return text, resp.get("eval_count")


def run_text_layer(post_id, page):
    """pdftotext -layout for one page. An empty text layer is a legitimate result (counted by the caller)."""
    r = subprocess.run(["pdftotext", "-layout", "-f", str(page), "-l", str(page),
                        str(PDF_DIR / f"{post_id}.pdf"), "-"], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise PipelineError(f"pdftotext failed: {r.stderr.strip()}")
    return r.stdout, None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--post-ids", nargs="*", type=int)
    ap.add_argument("--pages", nargs="*", type=int, help="restrict to these 1-based page numbers (testing)")
    a = ap.parse_args()

    if "cloud" in a.model.lower():
        fail(f"REFUSED model tag {a.model!r}: cloud models are forbidden (letters must stay on this machine).")
    is_text = a.model == TEXT_LAYER
    digest = "n/a" if is_text else model_digest(a.model)

    ids = a.post_ids
    if not ids:
        ids = [r[0] for r in sqlite3.connect(DB_PATH).execute("SELECT post_id FROM essays ORDER BY post_id")]
    t_start = time.time()
    done = skipped = empty = 0
    failures = []
    for pid in ids:
        pdir = PAGES_DIR / str(pid)
        imgs = sorted(pdir.glob("page_*.png"))
        if not imgs:
            failures.append(f"{pid}: no rendered pages in {pdir} (run render_pages.py)")
            continue
        for img in imgs:
            page = int(img.stem.split("_")[1])
            if a.pages and page not in a.pages:
                continue
            out = TRANSCRIPTIONS_DIR / str(pid) / model_slug(a.model) / f"page_{page:03d}.json"
            if out.exists():
                try:
                    validate_record(json.loads(out.read_text()))
                    skipped += 1
                    continue
                except (PipelineError, ValueError) as e:
                    failures.append(f"{pid} p{page}: existing {out} is invalid ({e}); delete it to redo")
                    continue
            last_err = None
            for attempt in range(2):  # at most one retry
                try:
                    t0 = time.time()
                    text, ntok = run_text_layer(pid, page) if is_text else run_ollama(a.model, img)
                    secs = round(time.time() - t0, 2)
                    break
                except Exception as e:  # noqa: BLE001 - reported below
                    last_err = f"{type(e).__name__}: {e}"
            else:
                failures.append(f"{pid} p{page}: {last_err}")
                continue
            rec = {"post_id": pid, "page": page, "model": a.model, "model_digest": digest,
                   "backend": "pdftotext" if is_text else "ollama",
                   "prompt_sha256": "n/a" if is_text else PROMPT_SHA,
                   "params": ({"temperature": None, "num_ctx": None, "layout": True} if is_text else
                              {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT}),
                   "image_dpi": IMAGE_DPI, "text": text, "seconds": float(secs), "output_tokens": ntok,
                   "created_at": now_utc()}
            validate_record(rec)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
            done += 1
            empty += not text.strip()
            print(f"{pid} p{page} {a.model}: {len(text)} chars in {secs}s")
    print(f"\nSUMMARY model={a.model} written={done} skipped_existing={skipped} empty_text={empty} "
          f"failed={len(failures)} wall={time.time() - t_start:.1f}s")
    if failures:
        fail("\n".join(failures))


if __name__ == "__main__":
    try:
        main()
    except PipelineError as e:
        fail(str(e))
