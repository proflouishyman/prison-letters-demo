#!/usr/bin/env python3
"""Transcribe rendered letter pages with a local Ollama vision model (or the PDF text layer baseline).

Usage:
  transcribe.py --model qwen3-vl:8b-instruct-q4_K_M [--post-ids 157294 ...] [--pages 1 2]
  transcribe.py --model pdf-text-layer ...
  transcribe.py --backend omlx --model mlx-community/Qwen3-VL-32B-Instruct-4bit --model-digest <HF commit SHA> \
                --concurrency 4 [--base-url http://localhost:8000]   # oMLX OpenAI-compatible server, batched requests

Writes transcriptions/<post_id>/<model_slug>/page_NNN.json (contract §3). Resumable: pages with a valid
JSON already present are skipped. Per-page timeout 600 s, at most one retry, then a FAILED banner and
non-zero exit. Any model tag containing "cloud" is refused: letters must never leave this machine.
"""
import argparse
import base64
import json
import os
import pathlib
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

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
OMLX_URL = "http://localhost:8000"
OMLX_MAX_TOKENS = 4096  # same output cap as NUM_PREDICT
OMLX_MODEL_ID = "Qwen3-VL-32B-Instruct-4bit"  # oMLX serves each ~/.omlx/models/<dir> under its directory name
OMLX_NUM_CTX = 262144  # Qwen3-VL-32B config max_position_embeddings; oMLX applies no smaller cap (see contract §3)

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
    if rec["backend"] not in ("ollama", "pdftotext", "vllm", "omlx"):
        raise PipelineError(f"bad backend {rec['backend']}")
    if "temperature" not in rec["params"] or "num_ctx" not in rec["params"]:
        raise PipelineError("params must include temperature and num_ctx")
    if rec["backend"] == "ollama" and (rec["params"]["num_ctx"] is None or rec["params"]["temperature"] is None):
        raise PipelineError("ollama params must set temperature and num_ctx explicitly")


FAIL_SPEC = {"post_id": int, "page": int, "model": str, "reason": str, "created_at": str}


def failure_path(pid, tag, page):
    """Failure record sits next to the page files but is named fail_NNN.json so `page_*.json` globs skip it."""
    return TRANSCRIPTIONS_DIR / str(pid) / model_slug(tag) / f"fail_{page:03d}.json"


def validate_failure(rec):
    """Raise PipelineError unless rec matches the failure-record shape (contract §3, 'Failure records')."""
    if set(rec) != set(FAIL_SPEC) or any(not isinstance(rec[k], t) for k, t in FAIL_SPEC.items()):
        raise PipelineError(f"bad failure record {rec}")


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
        "model": tag, "stream": False, "think": False, "keep_alive": -1,  # qwen3-vl:32b thinks by default; reasoning tokens are wasted here
        "messages": [{"role": "user", "content": PROMPT, "images": [img]}],
        "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
    }, timeout=TIMEOUT_S)
    text = resp["message"]["content"]
    if resp.get("done_reason") == "length":
        raise PipelineError(f"output hit num_predict={NUM_PREDICT} (runaway/looping output)")
    if not text.strip():
        raise PipelineError("model returned empty text")
    return text, resp.get("eval_count")


def omlx_headers():
    """Auth header for oMLX: OMLX_API_KEY env var, else the key file oMLX keeps at ~/.omlx/api_key."""
    key = os.environ.get("OMLX_API_KEY")
    kf = pathlib.Path.home() / ".omlx" / "api_key"
    if not key and kf.exists():
        key = kf.read_text().strip()
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = "Bearer " + key
    return h


def run_omlx(base_url, served_id, image_path):
    """One OpenAI-style chat completion against oMLX; returns (text, completion_tokens).

    Raises on any error, empty output, truncation (finish_reason length) or any sign of reasoning
    (a reasoning_content field or <think> text): reasoning is a failure, never stripped.
    """
    b64 = base64.b64encode(image_path.read_bytes()).decode()
    payload = {"model": served_id, "temperature": 0, "max_tokens": OMLX_MAX_TOKENS, "stream": False,
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": PROMPT},
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
    req = urllib.request.Request(base_url.rstrip("/") + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                 headers=omlx_headers())
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        resp = json.loads(r.read())
    choice = resp["choices"][0]
    msg = choice["message"]
    text = msg.get("content") or ""
    if msg.get("reasoning_content") or msg.get("reasoning") or "<think>" in text or "</think>" in text:
        raise PipelineError("response contains reasoning (reasoning_content / <think>)")
    if choice.get("finish_reason") == "length":
        raise PipelineError(f"output hit max_tokens={OMLX_MAX_TOKENS} (runaway/looping output)")
    if not text.strip():
        raise PipelineError("model returned empty text")
    return text, (resp.get("usage") or {}).get("completion_tokens")


def run_text_layer(post_id, page):
    """pdftotext -layout for one page. An empty text layer is a legitimate result (counted by the caller)."""
    r = subprocess.run(["pdftotext", "-layout", "-f", str(page), "-l", str(page),
                        str(PDF_DIR / f"{post_id}.pdf"), "-"], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise PipelineError(f"pdftotext failed: {r.stderr.strip()}")
    return r.stdout, None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="Ollama tag, `pdf-text-layer`, or (omlx) the HF repo ID")
    ap.add_argument("--backend", choices=["ollama", "omlx"], default="ollama")
    ap.add_argument("--base-url", default=OMLX_URL, help="oMLX server URL (omlx backend only)")
    ap.add_argument("--concurrency", type=int, default=4, help="in-flight requests (omlx backend only)")
    ap.add_argument("--model-digest", help="HF commit SHA of the downloaded weights (required for omlx)")
    ap.add_argument("--omlx-model-id", default=OMLX_MODEL_ID, help="model id as served by oMLX (its directory name)")
    ap.add_argument("--post-ids", nargs="*", type=int)
    ap.add_argument("--pages", nargs="*", type=int, help="restrict to these 1-based page numbers (testing)")
    a = ap.parse_args()

    if "cloud" in a.model.lower():
        fail(f"REFUSED model tag {a.model!r}: cloud models are forbidden (letters must stay on this machine).")
    if a.backend == "omlx" and urllib.parse.urlparse(a.base_url).hostname not in ("localhost", "127.0.0.1"):
        fail(f"REFUSED --base-url {a.base_url!r}: only a local oMLX server is allowed (letters must stay on this machine).")
    is_text = a.model == TEXT_LAYER
    is_omlx = a.backend == "omlx"
    if is_omlx:
        if not a.model_digest or a.concurrency < 1:
            fail("--backend omlx requires --model-digest (HF commit SHA) and --concurrency >= 1")
        digest = a.model_digest
    else:
        digest = "n/a" if is_text else model_digest(a.model)

    ids = a.post_ids
    if not ids:
        ids = [r[0] for r in sqlite3.connect(DB_PATH).execute("SELECT post_id FROM essays ORDER BY post_id")]
    t_start = time.time()
    done = skipped = empty = recorded = 0
    failures = []
    todo = []
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
            fpath = failure_path(pid, a.model, page)
            if fpath.exists() and not out.exists():
                # A recorded failure is a result (e.g. a looping model); never silently rerun it. Delete the
                # fail_NNN.json file to retry.
                validate_failure(json.loads(fpath.read_text()))
                recorded += 1
                continue
            if out.exists():
                try:
                    validate_record(json.loads(out.read_text()))
                    skipped += 1
                    continue
                except (PipelineError, ValueError) as e:
                    failures.append(f"{pid} p{page}: existing {out} is invalid ({e}); delete it to redo")
                    continue
            todo.append((pid, page, img, out, fpath))

    def process(job):
        """Transcribe one page with at most one retry; returns (job, text, ntok, secs, last_err)."""
        pid, page, img, _, _ = job
        last_err = None
        for attempt in range(2):  # at most one retry
            try:
                t0 = time.time()
                if is_text:
                    text, ntok = run_text_layer(pid, page)
                elif is_omlx:
                    text, ntok = run_omlx(a.base_url, a.omlx_model_id, img)
                else:
                    text, ntok = run_ollama(a.model, img)
                return job, text, ntok, round(time.time() - t0, 2), None
            except Exception as e:  # noqa: BLE001 - reported by the caller
                last_err = f"{type(e).__name__}: {e}"
        return job, None, None, None, last_err

    # Ollama and the text layer run sequentially; omlx keeps `concurrency` requests in flight (server batches them).
    workers = a.concurrency if is_omlx else 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for job, text, ntok, secs, last_err in ex.map(process, todo):
            pid, page, img, out, fpath = job
            if last_err is not None:
                failures.append(f"{pid} p{page}: {last_err}")
                frec = {"post_id": pid, "page": page, "model": a.model, "reason": last_err, "created_at": now_utc()}
                validate_failure(frec)
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(json.dumps(frec, ensure_ascii=False, indent=1))
                continue
            if is_omlx:
                params = {"temperature": 0, "num_ctx": OMLX_NUM_CTX, "max_tokens": OMLX_MAX_TOKENS,
                          "concurrency": a.concurrency}
            elif is_text:
                params = {"temperature": None, "num_ctx": None, "layout": True}
            else:
                params = {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT,
                          "think": False, "keep_alive": -1}
            rec = {"post_id": pid, "page": page, "model": a.model, "model_digest": digest,
                   "backend": "pdftotext" if is_text else a.backend,
                   "prompt_sha256": "n/a" if is_text else PROMPT_SHA, "params": params,
                   "image_dpi": IMAGE_DPI, "text": text, "seconds": float(secs), "output_tokens": ntok,
                   "created_at": now_utc()}
            validate_record(rec)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
            done += 1
            empty += not text.strip()
            print(f"{pid} p{page} {a.model}: {len(text)} chars, {ntok} tokens in {secs}s", flush=True)
    print(f"\nSUMMARY model={a.model} written={done} skipped_existing={skipped} previously_recorded_failures={recorded} empty_text={empty} "
          f"failed={len(failures)} wall={time.time() - t_start:.1f}s pages={done} concurrency={workers}", flush=True)
    if failures:
        fail("\n".join(failures))


if __name__ == "__main__":
    try:
        main()
    except PipelineError as e:
        fail(str(e))
