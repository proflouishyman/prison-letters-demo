#!/usr/bin/env python3
"""Public build step (contract §6): local inputs -> site/data/ public files (+ optional local full-text files).

Loud failure: every input row and output file is schema-checked; any violation prints a FAILED banner and exits
non-zero, and nothing is written (all outputs are staged in memory, validated, then written).
"""
import argparse
import copy
import json
import os
import pathlib
import re
import sqlite3
import statistics
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import crops as cropmod  # noqa: E402
from compare import norm_lower_nopunct  # noqa: E402
from idpatterns import find_hits, redact  # noqa: E402
from pl_common import ROOT, PipelineError, fail, git_version, now_utc  # noqa: E402

try:
    import numpy as np
except ImportError:  # numpy is required for quantization; no pure-python fallback on purpose
    fail("numpy is required for the int8 quantization step (pip install numpy).")

ENTITY_TYPES = {"facility", "place", "organization", "law_policy", "condition", "program", "event",
                "cultural_work", "person"}
AFFECTS = {"fear", "grief", "hope", "faith", "humour", "anger", "love", "resilience", "isolation", "dignity"}
SENTIMENTS = {"negative", "mixed", "neutral", "positive"}
STANCES = {"describing", "arguing", "testifying", "reflecting", "creative"}
EMB_MODEL, EMB_DIM = "Xenova/bge-small-en-v1.5", 384
DEMO_EXCERPT_MAX = 300  # contract §4 (public)
ENTITY_TEXT_MAX = 120   # a public entity is a short name, never a passage

# Field specs: name -> (allowed types, required). bool is never accepted where int is wanted.
STR, INT, FLT, BOOL, LST = (str,), (int,), (int, float), (bool,), (list,)
OPT = type(None)


def check_row(row, spec, where, strict=False):
    """Validate one dict against spec {field: (types, required)}; raise PipelineError on any mismatch.
    strict=True (outputs) also rejects fields not in the spec; inputs may carry extra optional fields."""
    if not isinstance(row, dict):
        raise PipelineError(f"{where}: expected an object, got {type(row).__name__}")
    for f, (types, required) in spec.items():
        if f not in row:
            if required:
                raise PipelineError(f"{where}: missing required field '{f}'")
            continue
        v = row[f]
        if v is None:
            if required == "nullable" or not required:
                continue
            raise PipelineError(f"{where}: field '{f}' is null but required")
        if isinstance(v, bool) and bool not in types:
            raise PipelineError(f"{where}: field '{f}' is a bool, expected {[t.__name__ for t in types]}")
        if not isinstance(v, types):
            raise PipelineError(f"{where}: field '{f}'={v!r:.60} has type {type(v).__name__}, "
                                f"expected {[t.__name__ for t in types]}")
    if strict:
        extra = set(row) - set(spec)
        if extra:
            raise PipelineError(f"{where}: unexpected fields {sorted(extra)}")


# ---- input specs (contract §1, §5) ----
ESSAY_IN = {"post_id": (INT, True), "url": (STR, True), "slug": (STR, True), "title": (STR, True),
            "author": (STR, False), "state": (STR, False), "date_text": (STR, False), "year": (INT, False),
            "genre": (STR, False), "extent_pages": (INT, False), "subjects": (STR, True),
            "language": (STR, False), "pdf_url": (STR, False), "transcript": (STR, False),
            "transcript_chars": (INT, True), "fetched_at": (STR, True), "source": (STR, True),
            "harvester_version": (STR, True)}
CHUNK_IN = {"chunk_id": (STR, True), "post_id": (INT, True), "n": (INT, True), "start": (INT, True),
            "end": (INT, True), "text": (STR, True)}
ENT_IN = {"post_id": (INT, True), "type": (STR, True), "text": (STR, True), "quote": (STR, True),
          "start": (INT, True), "end": (INT, True), "verified": (BOOL, True), "model": (STR, True),
          "prompt_sha256": (STR, True)}
THEME_IN = {"post_id": (INT, True), "subjects": (LST, True), "affect": (LST, True), "sentiment": (STR, True),
            "stance": (STR, True), "model": (STR, True), "prompt_sha256": (STR, True)}
EMB_META_IN = {"model": (STR, True), "dim": (INT, True), "normalized": (BOOL, True), "n_rows": (INT, True)}
# Exactly what scripts/compare.py writes (contract §4); state/year/ref_word_span are present but may be null.
# cer/wer (+ lower_nopunct variants) are null exactly when failed_pages > 0 (checked in validate_demo);
# failed_pages/failures are optional (default 0 / []) so older files still validate.
DEMO_MODEL = {"model": (STR, True), "cer": (FLT, "nullable"), "wer": (FLT, "nullable"),
              "cer_lower_nopunct": (FLT, "nullable"), "wer_lower_nopunct": (FLT, "nullable"),
              "failed_pages": (INT, False), "failures": (LST, False),
              "seconds_total": (FLT, True), "pages_compared": (LST, True), "partial": (BOOL, True),
              "ref_word_span": (LST, "nullable"), "excerpt": (STR, True),
              "excerpt_unaligned": (BOOL, False)}
DEMO_LETTER = {"post_id": (INT, True), "url": (STR, True), "title": (STR, True), "state": (STR, "nullable"),
               "year": (INT, "nullable"), "pages": (INT, True), "handwriting": (STR, True),
               "archive_excerpt": (STR, True), "models": (LST, True)}
DEMO_TOP = {"generated_at": (STR, True), "normalization": (STR, True), "letters": (LST, True),
            "fixture": (BOOL, False)}

# ---- output specs (contract §6): strict, wrapped in an object so each file can carry "fixture" ----
ESSAY_OUT = {"post_id": (INT, True), "url": (STR, True), "slug": (STR, True), "title": (STR, True),
             "author": (STR, False), "state": (STR, False), "date_text": (STR, False), "year": (INT, False),
             "genre": (STR, False), "extent_pages": (INT, False), "subjects": (LST, True),
             "language": (STR, False), "pdf_url": (STR, False), "transcript_chars": (INT, True),
             "excerpt": (STR, "nullable")}
CHUNK_OUT = {"chunk_id": (STR, True), "post_id": (INT, True), "snippet": (STR, True)}
ENT_OUT = {"post_id": (INT, True), "type": (STR, True), "text": (STR, True)}
THEME_OUT = {"post_id": (INT, True), "subjects": (LST, True), "affect": (LST, True), "sentiment": (STR, True),
             "stance": (STR, True), "model": (STR, True)}
CROP_OUT = {"target": (STR, True), "id": (STR, True), "post_id": (INT, True), "page": (INT, True),
            "file": (STR, True), "box": (LST, True), "similarity": (FLT, True),
            "masked": (LST, True)}
CROP_ALIGN_OUT = {"threshold": (FLT, True), "aligned": (dict, True), "unaligned": (dict, True),
                  "similarity_min": (FLT, True), "similarity_median": (FLT, True), "unaligned_list": (LST, True)}
IMG_EXPOSURE_OUT = {"cap": (FLT, True), "overall": (FLT, True), "max": (FLT, True), "mean": (FLT, True),
                    "per_letter": (LST, True)}
CROP_FILE_RE = re.compile(r"^crops/[0-9]+_p[0-9]+_[0-9]+\.jpg$")
VEC_META_OUT = {"fixture": (BOOL, True), "model": (STR, True), "dim": (INT, True), "n_rows": (INT, True),
                "dtype": (STR, True), "normalized": (BOOL, True), "scales": (LST, True),
                "max_reconstruction_error": (FLT, True), "row_order": (STR, True)}


def read_jsonl(path, spec, what):
    """Read a JSONL input, validating each row against spec; returns the list of rows."""
    if not path.exists():
        raise PipelineError(f"{what}: input file not found: {path}")
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise PipelineError(f"{what} line {i}: invalid JSON ({e})")
        check_row(row, spec, f"{what} line {i}")
        rows.append(row)
    return rows


def read_json(path, what):
    """Read a JSON input file; missing or malformed files raise."""
    if not path.exists():
        raise PipelineError(f"{what}: input file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PipelineError(f"{what}: invalid JSON in {path} ({e})")


def load_essays(root):
    """Read table essays from db/apwa_mini.sqlite (§1); returns (essays by post_id, fixture flag)."""
    db = root / "db" / "apwa_mini.sqlite"
    if not db.exists():
        raise PipelineError(f"essays: database not found: {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM essays ORDER BY post_id")]
        fixture = False
        has_meta = con.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
        if has_meta:
            fixture = any(r[0] == "fixture" and r[1] == "true"
                          for r in con.execute("SELECT key, value FROM meta"))
    except sqlite3.Error as e:
        raise PipelineError(f"essays: cannot read table 'essays' ({e})")
    finally:
        con.close()
    if not rows:
        raise PipelineError("essays: table is empty")
    essays = {}
    for r in rows:
        where = f"essays post_id={r.get('post_id')}"
        try:
            subj = json.loads(r["subjects"]) if isinstance(r.get("subjects"), str) else None
        except json.JSONDecodeError:
            subj = None
        if not isinstance(subj, list) or not all(isinstance(s, str) for s in subj):
            raise PipelineError(f"{where}: 'subjects' is not a JSON array of strings")
        r["subjects"] = subj
        spec = dict(ESSAY_IN, subjects=(LST, True))
        check_row(r, spec, where)
        want = len(r["transcript"]) if r["transcript"] is not None else 0
        if r["transcript_chars"] != want:
            raise PipelineError(f"{where}: transcript_chars={r['transcript_chars']} but transcript has {want}")
        if not r["url"].startswith("https://prisonwitness.org/"):
            raise PipelineError(f"{where}: url must link back to prisonwitness.org, got {r['url']!r}")
        essays[r["post_id"]] = r
    return essays, fixture


def load_chunks(root, essays):
    """Read enrich/chunks.jsonl and check ids, ordering and that text == transcript[start:end] (§5)."""
    chunks = read_jsonl(root / "enrich" / "chunks.jsonl", CHUNK_IN, "chunks")
    if not chunks:
        raise PipelineError("chunks: file is empty")
    seen = set()
    for c in chunks:
        where = f"chunk {c['chunk_id']}"
        e = essays.get(c["post_id"])
        if e is None or e["transcript"] is None:
            raise PipelineError(f"{where}: post_id {c['post_id']} has no transcript in the database")
        if c["chunk_id"] != f"{c['post_id']}-{c['n']}":
            raise PipelineError(f"{where}: chunk_id does not equal '<post_id>-<n>'")
        if c["chunk_id"] in seen:
            raise PipelineError(f"{where}: duplicate chunk_id")
        seen.add(c["chunk_id"])
        if not (0 <= c["start"] < c["end"] <= len(e["transcript"])) or c["text"] == "":
            raise PipelineError(f"{where}: bad offsets [{c['start']},{c['end']}) or empty text")
        if e["transcript"][c["start"]:c["end"]] != c["text"]:
            raise PipelineError(f"{where}: text differs from transcript[start:end]")
    return chunks


def load_embeddings(root, n_chunks):
    """Read embeddings.f32 + meta; returns (float32 matrix, meta). Must match the browser model exactly."""
    meta = read_json(root / "enrich" / "embeddings.meta.json", "embeddings.meta")
    check_row(meta, dict(EMB_META_IN, fixture=(BOOL, False)), "embeddings.meta")
    if meta["model"] != EMB_MODEL or meta["dim"] != EMB_DIM or meta["normalized"] is not True:
        raise PipelineError(f"embeddings.meta: must be model={EMB_MODEL} dim={EMB_DIM} normalized=true, got {meta}")
    if meta["n_rows"] != n_chunks:
        raise PipelineError(f"embeddings.meta: n_rows={meta['n_rows']} but chunks.jsonl has {n_chunks} rows")
    path = root / "enrich" / "embeddings.f32"
    if not path.exists():
        raise PipelineError(f"embeddings: file not found: {path}")
    want = n_chunks * EMB_DIM * 4
    if path.stat().st_size != want:
        raise PipelineError(f"embeddings.f32: {path.stat().st_size} bytes, expected {want}")
    mat = np.fromfile(path, dtype="<f4").reshape(n_chunks, EMB_DIM)
    if not np.isfinite(mat).all():
        raise PipelineError("embeddings.f32: contains NaN or inf")
    if (np.abs(mat).max(axis=1) == 0).any():
        raise PipelineError("embeddings.f32: contains an all-zero row")
    return mat, meta


def load_entities(root, essays):
    """Read entities.jsonl, validate, and re-check verified quotes against the transcript."""
    ents = read_jsonl(root / "enrich" / "entities.jsonl", ENT_IN, "entities")
    for i, e in enumerate(ents, 1):
        where = f"entities row {i}"
        if e["type"] not in ENTITY_TYPES:
            raise PipelineError(f"{where}: unknown type {e['type']!r}")
        if e["post_id"] not in essays:
            raise PipelineError(f"{where}: unknown post_id {e['post_id']}")
        if e["verified"]:
            t = essays[e["post_id"]]["transcript"]
            if t is None or t[e["start"]:e["end"]] != e["quote"]:
                raise PipelineError(f"{where}: marked verified but quote is not at transcript[start:end]")
    return ents


def load_themes(root, essays):
    """Read themes.jsonl, validate enumerations, and require one row per post_id at most."""
    themes = read_jsonl(root / "enrich" / "themes.jsonl", THEME_IN, "themes")
    seen = set()
    for t in themes:
        where = f"themes post_id={t['post_id']}"
        if t["post_id"] not in essays:
            raise PipelineError(f"{where}: unknown post_id")
        if t["post_id"] in seen:
            raise PipelineError(f"{where}: duplicate row")
        seen.add(t["post_id"])
        if not all(isinstance(s, str) for s in t["subjects"]) or not set(t["affect"]) <= AFFECTS:
            raise PipelineError(f"{where}: bad subjects or affect values {t['affect']}")
        if t["sentiment"] not in SENTIMENTS or t["stance"] not in STANCES:
            raise PipelineError(f"{where}: bad sentiment/stance")
    return themes


def validate_demo(demo, public, essays, snippet_chars):
    """Validate a transcription_demo document (§4). public=True enforces the <=300-char excerpt limits."""
    tag = "public" if public else "local"
    check_row(demo, DEMO_TOP, f"transcription_demo ({tag})", strict=True)
    for L in demo["letters"]:
        where = f"transcription_demo ({tag}) letter {L.get('post_id')}"
        check_row(L, DEMO_LETTER, where, strict=True)
        if L["post_id"] not in essays:
            raise PipelineError(f"{where}: post_id not in database")
        if L["handwriting"] not in ("handwritten", "typed", "mixed"):
            raise PipelineError(f"{where}: bad handwriting value {L['handwriting']!r}")
        if public and len(L["archive_excerpt"]) > DEMO_EXCERPT_MAX:
            raise PipelineError(f"{where}: archive_excerpt is {len(L['archive_excerpt'])} chars (max {DEMO_EXCERPT_MAX})")
        for m in L["models"]:
            check_row(m, DEMO_MODEL, f"{where} model", strict=True)
            nfail = m.get("failed_pages", 0)
            if len(m.get("failures", [])) != nfail:
                raise PipelineError(f"{where} {m['model']}: failed_pages={nfail} but {len(m.get('failures', []))} failures listed")
            for f in m.get("failures", []):
                check_row(f, {"post_id": (INT, True), "page": (INT, True), "reason": (STR, True)},
                          f"{where} {m['model']} failure", strict=True)
            rates = [m[k] for k in ("cer", "wer", "cer_lower_nopunct", "wer_lower_nopunct")]
            if (nfail > 0) != all(x is None for x in rates) or (nfail == 0 and any(x is None for x in rates)):
                raise PipelineError(f"{where} {m['model']}: CER/WER must be null exactly when failed_pages > 0")
            if "cloud" in m["model"]:
                raise PipelineError(f"{where}: ':cloud' model forbidden: {m['model']}")
            if m.get("excerpt_unaligned", False) and (m["excerpt"] != "" or nfail > 0):
                raise PipelineError(f"{where} {m['model']}: excerpt_unaligned requires an empty excerpt and failed_pages == 0")
            if public and len(m["excerpt"]) > DEMO_EXCERPT_MAX:
                raise PipelineError(f"{where} {m['model']}: excerpt is {len(m['excerpt'])} chars (max {DEMO_EXCERPT_MAX})")


def _public_text_fields(pub_essays, pub_chunks, pub_ents, demo):
    """Every public text field as (description, container, key): the guard reads them, redact_public rewrites them."""
    f = []
    for e in pub_essays:
        f += [(f"essays.json {e['post_id']}.title", e, "title")]
        if e.get("excerpt"):
            f.append((f"essays.json {e['post_id']}.excerpt", e, "excerpt"))
    f += [(f"chunks.json {c['chunk_id']}.snippet", c, "snippet") for c in pub_chunks]
    f += [(f"entities.json {n['post_id']} {n['type']}", n, "text") for n in pub_ents]
    for L in demo["letters"]:
        f += [(f"transcription_demo.json {L['post_id']}.title", L, "title"),
              (f"transcription_demo.json {L['post_id']}.archive_excerpt", L, "archive_excerpt")]
        f += [(f"transcription_demo.json {L['post_id']} {m['model']}.excerpt", m, "excerpt") for m in L["models"]]
    return f


def redact_public(pub_essays, pub_chunks, pub_ents, demo):
    """Black out (in place) every identifier/address span in our public text with idpatterns.REDACTION, including spans
    the archive's own text contains (Louis, 2026-11-10). Titles and entity texts are scanned by the guard, not rewritten.
    Returns the number of redacted spans (build_info.counts.text_redactions)."""
    n = 0
    for _, box, key in _public_text_fields(pub_essays, pub_chunks, [], demo):
        if key != "title":
            box[key], k = redact(box[key])
            n += k
    return n


def guard_id_patterns(pub_essays, pub_chunks, pub_ents, demo):
    """Public-text guard (contract §6), run on the already-redacted text: ANY identifier/address pattern hit
    (idpatterns.ID_PATTERNS) in a public text field FAILS the build, naming the field and the match."""
    bad = [(w, h) for w, box, key in _public_text_fields(pub_essays, pub_chunks, pub_ents, demo) for h in find_hits(box[key])]
    if bad:
        raise PipelineError("public text contains identifier/address patterns after redaction:\n  "
                            + "\n  ".join(f"{w}: {h!r}" for w, h in bad))


def quantize(mat):
    """int8 with a per-row scale (max|x|/127). Returns (int8 matrix, float scales, max reconstruction error)."""
    scales = np.abs(mat).max(axis=1) / 127.0
    q = np.rint(mat / scales[:, None]).astype(np.int8)
    err = float(np.abs(q.astype(np.float32) * scales[:, None] - mat).max())
    return q, scales, err


def validate_public_chunks(rows, snippet_chars):
    """Output check: strict schema and snippet <= SNIPPET_CHARS."""
    for r in rows:
        check_row(r, CHUNK_OUT, f"public chunk {r.get('chunk_id')}", strict=True)
        if len(r["snippet"]) > snippet_chars:
            raise PipelineError(f"public chunk {r['chunk_id']}: snippet is {len(r['snippet'])} chars (max {snippet_chars})")


def validate_public_essays(rows, snippet_chars):
    """Output check: strict schema, excerpt <= 2*SNIPPET_CHARS, link back to prisonwitness.org."""
    for r in rows:
        where = f"public essay {r.get('post_id')}"
        check_row(r, ESSAY_OUT, where, strict=True)
        if r.get("excerpt") is not None and len(r["excerpt"]) > 2 * snippet_chars:
            raise PipelineError(f"{where}: excerpt is {len(r['excerpt'])} chars (max {2 * snippet_chars})")
        if not r["url"].startswith("https://prisonwitness.org/"):
            raise PipelineError(f"{where}: missing prisonwitness.org link")


def validate_public_entities(rows):
    """Output check: strict schema, known types, and ZERO 'person' entities."""
    for r in rows:
        check_row(r, ENT_OUT, f"public entity {r.get('text')!r}", strict=True)
        if r["type"] == "person":
            raise PipelineError(f"person entity leaked into public output: {r['text']!r} (post {r['post_id']})")
        if r["type"] not in ENTITY_TYPES:
            raise PipelineError(f"public entity: unknown type {r['type']!r}")
        if len(r["text"]) > ENTITY_TEXT_MAX:
            raise PipelineError(f"public entity text too long ({len(r['text'])} chars): {r['text'][:40]!r}")


def validate_public_themes(rows):
    """Output check: strict schema and enumerations."""
    for r in rows:
        check_row(r, THEME_OUT, f"public themes {r.get('post_id')}", strict=True)
        if not set(r["affect"]) <= AFFECTS or r["sentiment"] not in SENTIMENTS or r["stance"] not in STANCES:
            raise PipelineError(f"public themes {r['post_id']}: bad enumeration value")


def validate_public_crops(rows, chunk_ids, essay_ids, threshold):
    """Output check (§6 crops.json): strict schema, known targets/ids, one row per (target, id), a sane box,
    a file name of the documented form, and similarity >= the alignment threshold (never a best guess)."""
    seen = set()
    for r in rows:
        where = f"public crop {r.get('target')}:{r.get('id')}"
        check_row(r, CROP_OUT, where, strict=True)
        if r["target"] not in cropmod.TARGETS:
            raise PipelineError(f"{where}: unknown target {r['target']!r}")
        ok_ids = chunk_ids if r["target"] == "chunk" else {str(i) for i in essay_ids}
        if r["id"] not in ok_ids:
            raise PipelineError(f"{where}: id is not a known {'chunk_id' if r['target'] == 'chunk' else 'post_id'}")
        if (r["target"], r["id"]) in seen:
            raise PipelineError(f"{where}: duplicate crop for the same snippet")
        seen.add((r["target"], r["id"]))
        if r["post_id"] not in essay_ids or r["page"] < 1:
            raise PipelineError(f"{where}: bad post_id/page")
        b = r["box"]
        if len(b) != 4 or not all(isinstance(v, int) and not isinstance(v, bool) for v in b) \
                or not (0 <= b[0] < b[2] and 0 <= b[1] < b[3]):
            raise PipelineError(f"{where}: box must be [x0,y0,x1,y1] integers with x0<x1, y0<y1, got {b}")
        for mb in r["masked"]:
            if len(mb) != 4 or not all(isinstance(v, int) and not isinstance(v, bool) for v in mb) \
                    or not (0 <= mb[0] < mb[2] and 0 <= mb[1] < mb[3]):
                raise PipelineError(f"{where}: masked box must be [x0,y0,x1,y1] integers, got {mb}")
        if not CROP_FILE_RE.match(r["file"]):
            raise PipelineError(f"{where}: file {r['file']!r} is not of the form crops/<post_id>_p<page>_<n>.jpg")
        if not (threshold <= r["similarity"] <= 1):
            raise PipelineError(f"{where}: similarity {r['similarity']} is outside [{threshold}, 1]")


def validate_crop_files(rows, data_dir):
    """Every crop's `file` must exist under data_dir and be a JPEG no wider than 900 px."""
    from PIL import Image
    for r in rows:
        f = pathlib.Path(data_dir) / r["file"]
        if not f.is_file():
            raise PipelineError(f"public crop {r['target']}:{r['id']}: file missing: {f}")
        with Image.open(f) as im:
            if im.format != "JPEG" or im.width > cropmod.MAX_CROP_WIDTH:
                raise PipelineError(f"{f}: must be a JPEG <= {cropmod.MAX_CROP_WIDTH}px wide, is {im.format} {im.width}px")


def exposure(essays, chunks, public_chunks, public_essays, demo):
    """Share of transcript characters published: union of snippet, excerpt and demo-excerpt intervals
    (all start at known offsets, so overlaps are not double counted). Returns (fraction, chars, total)."""
    iv = {}  # post_id -> list of (start, end)
    by_id = {c["chunk_id"]: c for c in chunks}
    for p in public_chunks:
        c = by_id[p["chunk_id"]]
        iv.setdefault(c["post_id"], []).append((c["start"], c["start"] + len(p["snippet"])))
    for p in public_essays:
        if p.get("excerpt"):
            iv.setdefault(p["post_id"], []).append((0, len(p["excerpt"])))
    for L in demo["letters"]:
        iv.setdefault(L["post_id"], []).append((0, len(L["archive_excerpt"])))
    chars = 0
    for spans in iv.values():
        cur_end = -1
        for s, e in sorted(spans):
            s = max(s, cur_end)
            if e > s:
                chars += e - s
            cur_end = max(cur_end, e)
    total = sum(e["transcript_chars"] for e in essays.values())
    if total == 0:
        raise PipelineError("exposure: no transcript characters in the database")
    return chars / total, chars, total


def wrap(key, items, fixture, **extra):
    """Standard public file envelope {fixture, generated_at, <key>: [...]} (fixture is always present)."""
    return {"fixture": fixture, "generated_at": now_utc(), key: items, **extra}


def build(root, snippet_chars, max_exposure, local, allow_fixture, max_image_exposure=0.20,
          sim_threshold=cropmod.SIM_THRESHOLD):
    """Run the whole build; returns the build_info dict. Raises PipelineError on any violation."""
    root = pathlib.Path(root)
    essays, fix_db = load_essays(root)
    chunks = load_chunks(root, essays)
    mat, emb_meta = load_embeddings(root, len(chunks))
    ents = load_entities(root, essays)
    themes = load_themes(root, essays)
    demo_pub = read_json(root / "site" / "data" / "transcription_demo.json", "transcription_demo (public)")
    demo_loc = read_json(root / "site" / "data" / "local" / "transcription_demo.json", "transcription_demo (local)")
    validate_demo(demo_pub, True, essays, snippet_chars)
    validate_demo(demo_loc, False, essays, snippet_chars)

    flags = {"db": fix_db, "embeddings.meta": bool(emb_meta.get("fixture", False)),
             "entities": any(e.get("fixture") for e in ents), "themes": any(t.get("fixture") for t in themes),
             "chunks": any(c.get("fixture") for c in chunks), "demo": bool(demo_pub.get("fixture", False))}
    if len(set(flags.values())) != 1:
        raise PipelineError(f"mixed fixture and real inputs refused: {flags}")
    fixture = fix_db
    if fixture and not allow_fixture:
        raise PipelineError("inputs are FIXTURE (fake) data; refusing to build unless --allow-fixture is given")

    # Entities: unverified rows are never used; person rows are dropped on purpose.
    verified = [e for e in ents if e["verified"]]
    n_unverified = len(ents) - len(verified)
    n_person = sum(1 for e in verified if e["type"] == "person")
    pub_ents = [{"post_id": e["post_id"], "type": e["type"], "text": e["text"]}
                for e in verified if e["type"] != "person"]
    seen_e, uniq = set(), []
    for e in pub_ents:  # one public row per distinct (post, type, text)
        k = (e["post_id"], e["type"], e["text"])
        if k not in seen_e:
            seen_e.add(k)
            uniq.append(e)
    pub_ents = uniq

    pub_essays = []
    for e in essays.values():
        t = e["transcript"]
        row = {k: e[k] for k in ESSAY_OUT if k in e and k != "excerpt" and e[k] is not None}
        row["excerpt"] = t[:2 * snippet_chars] if t else None
        pub_essays.append(row)
    pub_chunks = [{"chunk_id": c["chunk_id"], "post_id": c["post_id"], "snippet": c["text"][:snippet_chars]}
                  for c in chunks]
    pub_themes = [{k: t[k] for k in THEME_OUT} for t in themes]

    q, scales, err = quantize(mat)
    vec_meta = {"fixture": fixture, "model": emb_meta["model"], "dim": EMB_DIM, "n_rows": len(chunks),
                "dtype": "int8", "normalized": True, "scales": [float(s) for s in scales],
                "max_reconstruction_error": err, "row_order": "chunks.json"}

    validate_public_essays(pub_essays, snippet_chars)
    validate_public_chunks(pub_chunks, snippet_chars)
    validate_public_entities(pub_ents)
    validate_public_themes(pub_themes)
    check_row(vec_meta, VEC_META_OUT, "vectors.meta", strict=True)
    frac, chars, total = exposure(essays, chunks, pub_chunks, pub_essays, demo_pub)
    by_chunk = {c["chunk_id"]: c for c in chunks}

    print(f"Entities: dropped {n_unverified} unverified row(s); dropped {n_person} person row(s); "
          f"{len(pub_ents)} public.")
    print(f"Max int8 reconstruction error: {err:.6g} (rows={len(chunks)}, dim={EMB_DIM})")
    print(f"Exposure: {chars}/{total} transcript chars published = {frac * 100:.2f}% (cap {max_exposure * 100:.2f}%)")
    # Crops: align every public snippet to the page scans (staged in memory; files are written below).
    snippets = [{"target": "chunk", "id": p["chunk_id"], "post_id": p["post_id"], "text": p["snippet"],
                 "start": by_chunk[p["chunk_id"]]["start"]} for p in pub_chunks]
    snippets += [{"target": "essay", "id": str(p["post_id"]), "post_id": p["post_id"], "text": p["excerpt"], "start": 0}
                 for p in pub_essays if p.get("excerpt")]
    snippets += [{"target": "demo", "id": str(L["post_id"]), "post_id": L["post_id"], "text": L["archive_excerpt"],
                  "start": 0} for L in demo_pub["letters"] if L["archive_excerpt"]]
    page_boxes = cropmod.load_page_boxes(root / "enrich" / "page_boxes.jsonl")
    extra = set(page_boxes) - set(essays)
    if extra:
        raise PipelineError(f"page_boxes: post_ids not in the database: {sorted(extra)}")
    # Person masking: verified person entities plus the letter's author field; deities/religious names are skipped.
    archives = {pid: norm_lower_nopunct(e["transcript"] or "") for pid, e in essays.items()}
    skipped_rel = []
    names = {}
    for pid, e in essays.items():
        names[pid] = cropmod.name_tokens([x["text"] for x in verified if x["post_id"] == pid and x["type"] == "person"]
                                         + ([e["author"]] if e.get("author") else []), skipped_rel)
    cres = cropmod.compute_crops(snippets, page_boxes, essays, root / "data" / "pages", sim_threshold,
                                 max_image_exposure, names, archives)
    # Text redaction (after exposure and crop alignment, which use the unredacted text), then the guard on the result.
    red_essays, red_chunks, red_demo = (copy.deepcopy(x) for x in (pub_essays, pub_chunks, demo_pub))
    n_red = redact_public(red_essays, red_chunks, pub_ents, red_demo)
    guard_id_patterns(red_essays, red_chunks, pub_ents, red_demo)
    pub_crops = cres["crops"]
    validate_public_crops(pub_crops, {c["chunk_id"] for c in chunks}, set(essays), sim_threshold)
    ie = cres["exposure"]
    ms = cres["mask_stats"]
    print(f"Crop masking: {ms['person']} person, {ms['id_pattern']} id_pattern, {ms['header_block']} header_block mask(s) "
          f"in {ms['files_with_masks']} of {len(cres['images'])} image file(s); {ms['dropped_outside_lines']} crop(s) "
          f"dropped for showing lines outside the matched window.")
    print(f"Text redaction: {n_red} identifier/address span(s) replaced by the redaction mark in public text; guard: 0 hits left.")
    print(f"Person masking: {len(skipped_rel)} religious/deity entity text(s) NOT masked (stoplist): "
          f"{sorted(set(skipped_rel))}")
    by_t = lambda rows: {t: sum(1 for r in rows if r["target"] == t) for t in cropmod.TARGETS}  # noqa: E731
    sims = [c["similarity"] for c in pub_crops] or [0.0]  # 0.0 only when nothing aligned (all counted unaligned)
    print(f"Crops: {len(pub_crops)} aligned {by_t(pub_crops)}, {len(cres['unaligned'])} UNALIGNED (no crop) "
          f"{by_t(cres['unaligned'])} at similarity threshold {sim_threshold}; {len(cres['images'])} image file(s), "
          f"{sum(len(b) for b in cres['images'].values())} bytes.")
    for u in cres["unaligned"]:
        print(f"  UNALIGNED {u['target']} {u['id']}: {u['reason']}")
    print(f"Image exposure: overall {ie['overall'] * 100:.2f}%, per letter max {ie['max'] * 100:.2f}% / "
          f"mean {ie['mean'] * 100:.2f}% (cap {max_image_exposure * 100:.2f}%).")

    missing_themes = len(essays) - len(themes)
    if missing_themes:
        print(f"GAP: {missing_themes} essay(s) have no themes row.")
    if frac > max_exposure:
        raise PipelineError(f"exposure {frac * 100:.2f}% exceeds MAX_EXPOSURE {max_exposure * 100:.2f}%")

    models = {"embedding": sorted({emb_meta["model"]}),
              "entities": sorted({e["model"] for e in ents}), "themes": sorted({t["model"] for t in themes}),
              "transcription": sorted({m["model"] for L in demo_pub["letters"] for m in L["models"]})}
    info = {"fixture": fixture, "generated_at": now_utc(), "git_sha": git_version(), "models": models,
            "counts": {"essays": len(essays), "chunks": len(chunks), "entities_public": len(pub_ents),
                       "entities_unverified_dropped": n_unverified, "entities_person_dropped": n_person,
                       "themes": len(themes), "demo_letters": len(demo_pub["letters"]),
                       "crops": len(pub_crops), "crops_unaligned": len(cres["unaligned"]),
                       "text_redactions": n_red},
            "snippet_chars": snippet_chars, "max_exposure": max_exposure,
            "exposure": {"fraction": round(frac, 6), "percent": round(frac * 100, 3),
                         "chars_published": chars, "chars_total": total},
            "crop_alignment": {"threshold": sim_threshold, "aligned": by_t(pub_crops),
                               "unaligned": by_t(cres["unaligned"]),
                               "similarity_min": round(min(sims), 4), "similarity_median": round(statistics.median(sims), 4),
                               "unaligned_list": cres["unaligned"]},
            "image_exposure": {"cap": ie["cap"], "overall": ie["overall"], "max": ie["max"], "mean": ie["mean"],
                               "per_letter": ie["per_letter"]}}
    check_row(info["crop_alignment"], CROP_ALIGN_OUT, "build_info.crop_alignment", strict=True)
    check_row(info["image_exposure"], IMG_EXPOSURE_OUT, "build_info.image_exposure", strict=True)
    for u in info["crop_alignment"]["unaligned_list"]:
        check_row(u, {"target": (STR, True), "id": (STR, True), "reason": (STR, True)}, "unaligned_list row", strict=True)
    for x in ie["per_letter"]:
        check_row(x, {"post_id": (INT, True), "fraction": (FLT, True), "crops": (INT, True)}, "per_letter row", strict=True)

    out = root / "site" / "data"
    files = {"essays.json": wrap("essays", red_essays, fixture), "chunks.json": wrap("chunks", red_chunks, fixture),
             "entities.json": wrap("entities", pub_ents, fixture), "themes.json": wrap("themes", pub_themes, fixture),
             "vectors.meta.json": vec_meta, "build_info.json": info}
    out.mkdir(parents=True, exist_ok=True)
    for name, doc in files.items():
        (out / name).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "vectors.i8").write_bytes(q.tobytes())
    (out / "crops.json").write_text(json.dumps(wrap("crops", pub_crops, fixture), ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    (out / "crops").mkdir(exist_ok=True)
    for stale in (out / "crops").glob("*.jpg"):  # the build owns this folder; never leave unlisted scan crops public
        if f"crops/{stale.name}" not in cres["images"]:
            stale.unlink()
    for name, data in cres["images"].items():
        (out / name).write_bytes(data)
    (out / "transcription_demo.json").write_text(json.dumps(red_demo, ensure_ascii=False, indent=1), encoding="utf-8")

    # Read back from disk and re-validate what was actually written (including the person-leak scan).
    for name in files:
        json.loads((out / name).read_text(encoding="utf-8"))
    validate_public_entities(json.loads((out / "entities.json").read_text(encoding="utf-8"))["entities"])
    validate_public_chunks(json.loads((out / "chunks.json").read_text(encoding="utf-8"))["chunks"], snippet_chars)
    written = json.loads((out / "crops.json").read_text(encoding="utf-8"))["crops"]
    validate_public_crops(written, {c["chunk_id"] for c in chunks}, set(essays), sim_threshold)
    validate_crop_files(written, out)
    if {f.name for f in (out / "crops").glob("*")} != {n.split("/", 1)[1] for n in cres["images"]}:
        raise PipelineError("site/data/crops contains files that are not referenced by crops.json")
    if (out / "vectors.i8").stat().st_size != len(chunks) * EMB_DIM:
        raise PipelineError("vectors.i8: written size does not equal n_rows*dim")

    if local:
        build_local(root, essays, chunks, ents, fixture)
    return info


def build_local(root, essays, chunks, ents, fixture):
    """Write full-text versions (essays with transcripts, chunks with text, all verified entities with quotes,
    persons included) to site/data/local/. Refuses unless that directory is gitignored in the real repo."""
    loc = root / "site" / "data" / "local"
    loc.mkdir(parents=True, exist_ok=True)
    if root.resolve() == ROOT.resolve():
        r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(loc / "essays.json")])
        if r.returncode != 0:
            raise PipelineError("site/data/local/ is not gitignored; refusing to write full text")
    docs = {"essays.json": wrap("essays", list(essays.values()), fixture, local=True),
            "chunks.json": wrap("chunks", chunks, fixture, local=True),
            "entities.json": wrap("entities", [e for e in ents if e["verified"]], fixture, local=True)}
    for name, doc in docs.items():
        (loc / name).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Local full-text files written to {loc}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(ROOT), help="repo root holding db/, enrich/, site/ (default: this repo)")
    ap.add_argument("--local", action="store_true", help="also write full-text files to site/data/local/")
    ap.add_argument("--allow-fixture", action="store_true", help="permit FAKE fixture inputs (tests/demos only)")
    ap.add_argument("--snippet-chars", type=int, default=int(os.environ.get("SNIPPET_CHARS", 160)))
    ap.add_argument("--max-exposure", type=float, default=float(os.environ.get("MAX_EXPOSURE", 0.15)))
    ap.add_argument("--max-image-exposure", type=float, default=float(os.environ.get("MAX_IMAGE_EXPOSURE", 0.20)),
                    help="cap on the share of a letter's page area published as crops (contract §6)")
    ap.add_argument("--sim-threshold", type=float, default=cropmod.SIM_THRESHOLD,
                    help="minimum snippet-to-scan similarity for a crop (contract §6)")
    a = ap.parse_args(argv)
    try:
        info = build(a.root, a.snippet_chars, a.max_exposure, a.local, a.allow_fixture, a.max_image_exposure,
                     a.sim_threshold)
    except PipelineError as e:
        fail(str(e))
    print(f"OK: built public site data ({info['counts']['essays']} essays, {info['counts']['chunks']} chunks, "
          f"git {info['git_sha']}{', FIXTURE' if info['fixture'] else ''})")


if __name__ == "__main__":
    main()
