#!/usr/bin/env python3
"""Enrichment stage (contract §5): chunks -> embeddings -> entities/themes, all written under enrich/.

Subcommands:
  chunks   transcript -> enrich/chunks.jsonl (~1,200-char chunks with offsets into essays.transcript)
  embed    chunks.jsonl -> enrich/embeddings.f32 + embeddings.meta.json (Xenova/bge-small-en-v1.5, mean pooling)
  extract  LLM entities + themes -> enrich/entities.jsonl + themes.jsonl (Ollama or an OpenAI-compatible/vLLM server)

Only the 20 demo letters are used: their post_ids come from data/selected_urls.txt matched to essays.url, so
survey test samples that may also sit in the db are ignored. Loud failure throughout: any problem prints a FAILED
banner and exits non-zero; nothing is silently skipped, coerced or defaulted.
"""
import argparse
import html
import json
import pathlib
import re
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pl_common import DB_PATH, ROOT, PipelineError, fail, now_utc, sha256_text  # noqa: E402

ENRICH_DIR = ROOT / "enrich"
SELECTED_URLS = ROOT / "data" / "selected_urls.txt"
SUBJECT_JSON = ROOT / "survey" / "rest_subject_per_page_100.json"

# Contract §5 enumerations (kept identical to build_site.py).
ENTITY_TYPES = ["facility", "place", "organization", "law_policy", "condition", "program", "event",
                "cultural_work", "person"]
AFFECTS = ["fear", "grief", "hope", "faith", "humour", "anger", "love", "resilience", "isolation", "dignity"]
SENTIMENTS = ["negative", "mixed", "neutral", "positive"]
STANCES = ["describing", "arguing", "testifying", "reflecting", "creative"]

EMB_MODEL, EMB_DIM = "Xenova/bge-small-en-v1.5", 384
CHUNK_TARGET = 1200   # chars: a chunk closes at the first boundary at or after this length
CHUNK_HARD_MAX = 1800  # a single unbroken run longer than this is split at whitespace
CHUNK_MIN_TAIL = 300  # a shorter final chunk is merged into its predecessor


# --------------------------------------------------------------------------------------------------
# Shared: the 20 letters
# --------------------------------------------------------------------------------------------------
def load_letters():
    """Return [(post_id, url, transcript)] for exactly the 20 URLs in selected_urls.txt, ordered by post_id.

    Fails if any selected URL is missing from the db or has a NULL transcript (contract §1: NULL = no transcript).
    """
    urls = [u.strip() for u in SELECTED_URLS.read_text(encoding="utf-8").splitlines() if u.strip()]
    if len(set(urls)) != len(urls):
        raise PipelineError(f"{SELECTED_URLS}: duplicate URLs")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        by_url = {r[1]: r for r in con.execute("SELECT post_id, url, transcript FROM essays")}
    finally:
        con.close()
    missing = [u for u in urls if u not in by_url]
    if missing:
        raise PipelineError(f"selected URLs not found in essays: {missing}")
    rows = [by_url[u] for u in urls]
    nulls = [r[0] for r in rows if r[2] is None or not r[2].strip()]
    if nulls:
        raise PipelineError(f"selected letters without a transcript: {nulls}")
    return sorted(rows)


def write_jsonl(path, rows):
    """Write rows as UTF-8 JSONL, creating the parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl_file(path):
    """Read a JSONL file into a list of dicts ([] if it does not exist)."""
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# --------------------------------------------------------------------------------------------------
# chunks
# --------------------------------------------------------------------------------------------------
# Boundary = newline run, or whitespace after sentence-final punctuation (+ optional closing quote/bracket).
# The punctuation stays in the preceding segment: the match starts at the whitespace via a capture-free lookbehind
# alternative per closer.
_BOUNDARY = re.compile(r"(?:\n[ \t]*)+|(?<=[.!?])[ \t]+|(?<=[.!?][\"')\]])[ \t]+")


def segments(text):
    """Split text into (start, end) segments at newlines and sentence ends; whitespace is trimmed off each ends.

    Segments longer than CHUNK_HARD_MAX (no punctuation or newlines) are split at the last space before the cap.
    """
    spans, pos = [], 0
    for m in list(_BOUNDARY.finditer(text)) + [None]:
        end = m.start() if m else len(text)
        s, e = pos, end
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e > s:
            spans.append((s, e))
        pos = m.end() if m else len(text)
    out = []
    for s, e in spans:
        while e - s > CHUNK_HARD_MAX:
            cut = text.rfind(" ", s, s + CHUNK_HARD_MAX)
            if cut <= s:
                raise PipelineError(f"unbreakable run of {CHUNK_HARD_MAX}+ chars at offset {s}")
            out.append((s, cut))
            s = cut + 1
        out.append((s, e))
    return out


def chunk_transcript(post_id, text):
    """Greedily pack segments into ~CHUNK_TARGET-char chunks; offsets index into `text`.

    Invariant (asserted): text[start:end] == chunk text for every chunk.
    """
    segs = segments(text)
    groups, cur = [], []
    for s, e in segs:
        cur.append((s, e))
        if e - cur[0][0] >= CHUNK_TARGET:
            groups.append(cur)
            cur = []
    if cur:
        if groups and cur[-1][1] - cur[0][0] < CHUNK_MIN_TAIL and cur[-1][1] - groups[-1][0][0] <= CHUNK_HARD_MAX:
            groups[-1].extend(cur)
        else:
            groups.append(cur)
    rows = []
    for n, g in enumerate(groups):
        start, end = g[0][0], g[-1][1]
        rows.append({"chunk_id": f"{post_id}-{n}", "post_id": post_id, "n": n, "start": start, "end": end,
                     "text": text[start:end]})
        assert text[start:end] == rows[-1]["text"], f"offset mismatch in {post_id}-{n}"
    return rows


def cmd_chunks(args):
    """chunks subcommand: chunk all 20 transcripts and write enrich/chunks.jsonl."""
    rows = []
    for post_id, _url, text in load_letters():
        rows.extend(chunk_transcript(post_id, text))
    letters = {pid: t for pid, _u, t in load_letters()}
    for r in rows:
        assert letters[r["post_id"]][r["start"]:r["end"]] == r["text"], r["chunk_id"]
    write_jsonl(ENRICH_DIR / "chunks.jsonl", rows)
    lens = [len(r["text"]) for r in rows]
    print(f"chunks: {len(rows)} chunks from {len(letters)} letters; chars min/median/max = "
          f"{min(lens)}/{sorted(lens)[len(lens) // 2]}/{max(lens)}")


# --------------------------------------------------------------------------------------------------
# embed
# --------------------------------------------------------------------------------------------------
def _hf_files():
    """Download (cached) the Xenova ONNX files; returns (model.onnx, model_quantized.onnx, tokenizer.json)."""
    from huggingface_hub import hf_hub_download
    return tuple(hf_hub_download(EMB_MODEL, f) for f in
                 ("onnx/model.onnx", "onnx/model_quantized.onnx", "tokenizer.json"))


def make_embedder(onnx_path, tokenizer_path):
    """Return embed(list[str]) -> float32 array (n, 384): mean pooling over the attention mask, L2-normalised.

    This mirrors transformers.js feature-extraction with {pooling: 'mean', normalize: true}. Texts are truncated
    at 512 tokens (the model's maximum).
    """
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer
    tok = Tokenizer.from_file(tokenizer_path)
    tok.enable_truncation(max_length=512)
    tok.enable_padding()
    sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    names = {i.name for i in sess.get_inputs()}

    def embed(texts):
        out = []
        for i in range(0, len(texts), 16):
            enc = tok.encode_batch(texts[i:i + 16])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hid = sess.run(None, feeds)[0]  # (b, seq, 384)
            m = mask[:, :, None].astype(np.float32)
            pooled = (hid * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
            pooled /= np.linalg.norm(pooled, axis=1, keepdims=True)
            out.append(pooled.astype(np.float32))
        return np.concatenate(out)
    return embed


def cmd_embed(args):
    """embed subcommand: embed every chunk with the fp32 ONNX model and cross-check against the quantized one."""
    import numpy as np
    chunks = read_jsonl_file(ENRICH_DIR / "chunks.jsonl")
    if not chunks:
        raise PipelineError("enrich/chunks.jsonl missing or empty: run `enrich.py chunks` first")
    fp32, quant, tok = _hf_files()
    emb = make_embedder(fp32, tok)
    mat = emb([c["text"] for c in chunks])
    if mat.shape != (len(chunks), EMB_DIM) or not np.isfinite(mat).all():
        raise PipelineError(f"bad embedding matrix: shape {mat.shape}")
    if not np.allclose(np.linalg.norm(mat, axis=1), 1.0, atol=1e-4):
        raise PipelineError("embeddings are not unit-normalised")
    mat.astype("<f4").tofile(ENRICH_DIR / "embeddings.f32")

    # Cross-check: transformers.js defaults to the quantized ONNX file, so compare fp32 against it.
    test = ["I was sentenced to life without parole when I was seventeen years old."]
    cos_quant = float(np.dot(emb(test)[0], make_embedder(quant, tok)(test)[0]))
    meta = {"model": EMB_MODEL, "dim": EMB_DIM, "normalized": True, "n_rows": len(chunks),
            "runtime": "onnxruntime-cpu + tokenizers (Xenova ONNX, fp32 onnx/model.onnx)",
            "pooling": "mean", "max_tokens": 512, "created_at": now_utc(),
            "crosscheck": {"test_sentence": test[0], "cosine_fp32_vs_quantized_onnx": cos_quant}}
    (ENRICH_DIR / "embeddings.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"embed: {len(chunks)} rows x {EMB_DIM}; cosine(fp32, quantized ONNX) on test sentence = {cos_quant:.6f}")


# --------------------------------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------------------------------
def load_subjects():
    """APWA's own subject list: the `name` of every term in the WordPress taxonomy dump (HTML-unescaped)."""
    terms = json.loads(SUBJECT_JSON.read_text(encoding="utf-8"))
    names = sorted({html.unescape(t["name"]).strip() for t in terms})
    if not names:
        raise PipelineError(f"no subject terms in {SUBJECT_JSON}")
    return names


def build_schema(subjects):
    """JSON schema for the structured output; enums enforce the contract (and are re-validated in code)."""
    return {
        "type": "object",
        "properties": {
            "entities": {"type": "array", "items": {
                "type": "object",
                "properties": {"type": {"type": "string", "enum": ENTITY_TYPES},
                               "text": {"type": "string"},
                               "quote": {"type": "string"}},
                "required": ["type", "text", "quote"]}},
            "subjects": {"type": "array", "items": {"type": "string", "enum": subjects}},
            "affect": {"type": "array", "items": {"type": "string", "enum": AFFECTS}},
            "sentiment": {"type": "string", "enum": SENTIMENTS},
            "stance": {"type": "string", "enum": STANCES},
        },
        "required": ["entities", "subjects", "affect", "sentiment", "stance"],
    }


def build_prompt(subjects):
    """The fixed instruction text (the letter is sent separately); its sha256 is recorded on every row."""
    return (
        "You annotate letters written by incarcerated people (American Prison Writing Archive). "
        "Return JSON only.\n\n"
        "ENTITIES: list every named thing in the letter of these types: "
        + ", ".join(ENTITY_TYPES) + ". facility = a named prison, jail or unit; place = a city, state, country "
        "or other named location; organization = an agency, company, court, church or group; law_policy = a named "
        "law, ruling, policy or sentence type; condition = a medical or mental-health condition; program = a "
        "named program or course; event = a named event; cultural_work = a book, song, film or other work; "
        "person = a named individual. For each, give `text` (the short name) and `quote`: a span copied "
        "CHARACTER FOR CHARACTER from the letter that contains the mention, at most 80 characters. Never "
        "correct, paraphrase or re-punctuate the quote. Do not invent entities that the letter does not name.\n\n"
        "SUBJECTS: choose 0-4 subjects, only from this list: " + "; ".join(subjects) + ".\n"
        "AFFECT: choose 0-4 of: " + ", ".join(AFFECTS) + ".\n"
        "SENTIMENT: one of " + ", ".join(SENTIMENTS) + ".\n"
        "STANCE: one of " + ", ".join(STANCES) + " (describing = reports conditions; arguing = makes a case; "
        "testifying = first-person account of events; reflecting = introspective; creative = poem or story).")


def refuse_cloud(model):
    """Standing rule: never use a model tag containing 'cloud'."""
    if "cloud" in model.lower():
        raise PipelineError(f"refusing model tag {model!r}: cloud models are forbidden")


def call_llm(args, system, schema, letter_text, seed_params):
    """One structured-output call; returns (parsed JSON dict, seconds, output_tokens|None)."""
    import requests
    t0 = time.time()
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": "LETTER:\n\n" + letter_text}]
    if args.backend == "ollama":
        body = {"model": args.model, "messages": messages, "stream": False, "format": schema,
                "options": {"temperature": 0, "num_ctx": args.num_ctx, "num_predict": args.num_predict}}
        if args.no_think:
            body["think"] = False
        r = requests.post(args.base_url.rstrip("/") + "/api/chat", json=body, timeout=args.timeout)
        r.raise_for_status()
        j = r.json()
        content, toks = j["message"]["content"], j.get("eval_count")
    else:
        body = {"model": args.model, "messages": messages, "temperature": 0, "max_tokens": args.num_predict,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "annotation", "schema": schema, "strict": True}}}
        r = requests.post(args.base_url.rstrip("/") + "/chat/completions", json=body, timeout=args.timeout)
        r.raise_for_status()
        j = r.json()
        content, toks = j["choices"][0]["message"]["content"], j.get("usage", {}).get("completion_tokens")
    try:
        return json.loads(content), time.time() - t0, toks
    except json.JSONDecodeError as e:
        raise PipelineError(f"model returned invalid JSON ({e}): {content[:200]!r}")


def _norm_map(s):
    """Collapse whitespace runs to one space; return (normalized string, index map normalized->original)."""
    out, idx, prev_space = [], [], False
    for i, ch in enumerate(s):
        if ch.isspace():
            if not prev_space:
                out.append(" ")
                idx.append(i)
            prev_space = True
        else:
            out.append(ch)
            idx.append(i)
            prev_space = False
    return "".join(out), idx


def locate(transcript, quote, norm_cache):
    """Find quote in transcript: exact first, then whitespace-normalised. Never fuzzy.

    Returns (start, end, actual_text) or None. For a normalised match, actual_text is the transcript's own
    span, so the stored quote always equals transcript[start:end] (what build_site re-checks).
    """
    if not quote.strip():
        return None
    i = transcript.find(quote)
    if i >= 0:
        return i, i + len(quote), quote
    if "n" not in norm_cache:
        norm_cache["n"] = _norm_map(transcript)
    ntext, idx = norm_cache["n"]
    nq = " ".join(quote.split())
    j = ntext.find(nq)
    if j < 0:
        return None
    start, end = idx[j], idx[j + len(nq) - 1] + 1
    return start, end, transcript[start:end]


def validate_annotation(out, subjects, post_id):
    """Check an LLM result against the contract enumerations; raise PipelineError on any violation."""
    where = f"post {post_id}"
    if not isinstance(out, dict) or set(out) != {"entities", "subjects", "affect", "sentiment", "stance"}:
        raise PipelineError(f"{where}: unexpected top-level keys {sorted(out) if isinstance(out, dict) else out!r}")
    sset = set(subjects)
    bad = [s for s in out["subjects"] if s not in sset]
    if bad or not isinstance(out["subjects"], list):
        raise PipelineError(f"{where}: subjects outside APWA's list: {bad}")
    badaff = [a for a in out["affect"] if a not in AFFECTS]
    if badaff or not isinstance(out["affect"], list):
        raise PipelineError(f"{where}: affect values outside the contract: {badaff}")
    if out["sentiment"] not in SENTIMENTS or out["stance"] not in STANCES:
        raise PipelineError(f"{where}: bad sentiment/stance {out['sentiment']!r}/{out['stance']!r}")
    for e in out["entities"]:
        if not isinstance(e, dict) or set(e) != {"type", "text", "quote"} or \
                not all(isinstance(e[k], str) for k in e) or e["type"] not in ENTITY_TYPES:
            raise PipelineError(f"{where}: bad entity {e!r}")


def annotate_letter(args, system, schema, subjects, post_id, transcript, params):
    """Annotate one letter with at most one retry; returns (entity rows, theme row, seconds, tokens)."""
    last = None
    for attempt in (1, 2):
        try:
            out, secs, toks = call_llm(args, system, schema, transcript, params)
            validate_annotation(out, subjects, post_id)
            break
        except Exception as e:  # one retry, then the caller prints FAILED
            last = e
            print(f"  post {post_id}: attempt {attempt} failed: {e}", file=sys.stderr)
    else:
        raise PipelineError(f"post {post_id}: extraction failed after 1 retry: {last}")
    prov = {"model": args.model, "prompt_sha256": params["prompt_sha256"]}
    cache, ents, seen = {}, [], set()
    for e in out["entities"]:
        hit = locate(transcript, e["quote"], cache)
        key = (e["type"], e["text"], hit[0] if hit else e["quote"])
        if key in seen:
            continue
        seen.add(key)
        if hit:
            s, en, actual = hit
            assert transcript[s:en] == actual
            ents.append({"post_id": post_id, "type": e["type"], "text": e["text"], "quote": actual,
                         "start": s, "end": en, "verified": True, **prov})
        else:  # kept but flagged; offsets are -1 because there is no span
            ents.append({"post_id": post_id, "type": e["type"], "text": e["text"], "quote": e["quote"],
                         "start": -1, "end": -1, "verified": False, **prov})
    theme = {"post_id": post_id, "subjects": out["subjects"], "affect": out["affect"],
             "sentiment": out["sentiment"], "stance": out["stance"], **prov}
    return ents, theme, secs, toks


def cmd_extract(args):
    """extract subcommand: annotate the selected letters, merging into entities.jsonl and themes.jsonl.

    Rows for the post_ids processed in this run replace earlier rows for those ids; other rows are kept, so the
    run can be done in parts (--post-id) and resumed.
    """
    refuse_cloud(args.model)
    if args.backend == "openai" and not args.base_url:
        raise PipelineError("--backend openai requires --base-url")
    args.base_url = args.base_url or "http://localhost:11434"
    letters = load_letters()
    if args.post_id:
        want = set(args.post_id)
        unknown = want - {p for p, _, _ in letters}
        if unknown:
            raise PipelineError(f"post_ids not among the 20 selected letters: {sorted(unknown)}")
        letters = [l for l in letters if l[0] in want]
    elif not args.all:
        raise PipelineError("give --post-id N (repeatable) or --all")
    subjects = load_subjects()
    schema = build_schema(subjects)
    system = build_prompt(subjects)
    params = {"temperature": 0, "num_ctx": args.num_ctx, "num_predict": args.num_predict, "backend": args.backend,
              "think": not args.no_think,
              "prompt_sha256": sha256_text(system + json.dumps(schema, sort_keys=True))}
    new_ents, new_themes, total_secs = [], [], 0.0
    for post_id, _url, text in letters:
        print(f"extract: post {post_id} ({len(text)} chars) ...", flush=True)
        ents, theme, secs, toks = annotate_letter(args, system, schema, subjects, post_id, text, params)
        total_secs += secs
        print(f"  {secs:.1f}s, {toks} output tokens, {len(ents)} entities")
        new_ents += ents
        new_themes.append(theme)
    done = {t["post_id"] for t in new_themes}
    ents_all = [e for e in read_jsonl_file(ENRICH_DIR / "entities.jsonl") if e["post_id"] not in done] + new_ents
    themes_all = [t for t in read_jsonl_file(ENRICH_DIR / "themes.jsonl") if t["post_id"] not in done] + new_themes
    write_jsonl(ENRICH_DIR / "entities.jsonl", ents_all)
    write_jsonl(ENRICH_DIR / "themes.jsonl", themes_all)
    # Summary: counts first (contract §5: unverified count is reported at the top of the run log).
    ver = sum(e["verified"] for e in new_ents)
    by_type = {t: sum(e["type"] == t for e in new_ents) for t in ENTITY_TYPES}
    print("\n=== extract summary ===")
    print(f"entities this run: {len(new_ents)} (verified {ver}, UNVERIFIED {len(new_ents) - ver})")
    print("by type: " + ", ".join(f"{t}={n}" for t, n in by_type.items()))
    print(f"letters: {len(letters)}  model: {args.model} ({args.backend})  total {total_secs:.1f}s")
    print(f"files now hold {len({t['post_id'] for t in themes_all})}/20 letters")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chunks")
    sub.add_parser("embed")
    ex = sub.add_parser("extract")
    ex.add_argument("--backend", choices=["ollama", "openai"], default="ollama")
    ex.add_argument("--base-url", default=None, help="ollama: default http://localhost:11434; openai: e.g. http://host:8000/v1")
    ex.add_argument("--model", required=True)
    ex.add_argument("--post-id", type=int, action="append", help="restrict to this letter (repeatable)")
    ex.add_argument("--all", action="store_true", help="all 20 letters")
    ex.add_argument("--num-ctx", type=int, default=16384)
    ex.add_argument("--num-predict", type=int, default=4096)
    ex.add_argument("--no-think", action="store_true", help="ollama: send think=false")
    ex.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args(argv)
    try:
        {"chunks": cmd_chunks, "embed": cmd_embed, "extract": cmd_extract}[args.cmd](args)
    except PipelineError as e:
        fail(str(e))
    except AssertionError as e:
        fail(f"invariant violated: {e}")


if __name__ == "__main__":
    main()
