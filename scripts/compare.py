#!/usr/bin/env python3
"""Compute CER/WER of each model's transcription against the archive's human transcript (ground truth).

Usage: compare.py [--post-ids ...] [--models ...] [--allow-partial] [--handwriting-csv data/selection_candidates.csv]

Writes site/data/local/transcription_demo.json (full text, local only) and site/data/transcription_demo.json
(public: excerpts <= 300 chars) per contract §4, and prints a plain-text table.

Normalization (primary): NFKC, unify quotes and dashes, collapse whitespace, case kept.
Variant ("lower_nopunct"): primary, then lowercase and drop every punctuation character.
CER = char-level Levenshtein / len(reference chars); WER = whitespace-token Levenshtein / len(reference tokens).

A letter is compared in full only when every rendered page has a transcription for the model. The human
transcript is not split by page, so with --allow-partial a subset of pages is scored against the
best-matching word window of the human text (semi-global word alignment); such rows carry partial=true.
Without the flag, incomplete coverage is an error.
"""
import argparse
import csv
import json
import sqlite3
import unicodedata

from rapidfuzz.distance import Levenshtein

from pl_common import (DB_PATH, ROOT, PAGES_DIR, TRANSCRIPTIONS_DIR, PipelineError, fail, now_utc)

LOCAL_OUT = ROOT / "site" / "data" / "local" / "transcription_demo.json"
PUBLIC_OUT = ROOT / "site" / "data" / "transcription_demo.json"
HAND_CSV = ROOT / "data" / "selection_candidates.csv"
HAND_VALUES = {"handwritten", "typed", "mixed"}
EXCERPT_MAX = 300
NORMALIZATION = ("NFKC; curly quotes -> straight; en/em/minus dashes -> '-'; whitespace collapsed; case kept. "
                 "Variant (*_lower_nopunct): additionally lowercased with all punctuation removed. "
                 "CER over characters, WER over whitespace tokens, both against the archive's human transcript.")
_QUOTES = {"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "“": '"',
           "”": '"', "„": '"', "″": '"'}
_DASHES = {"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-"}
_MAP = str.maketrans({**_QUOTES, **_DASHES})


def norm(s):
    """Primary normalization (contract §4)."""
    return " ".join(unicodedata.normalize("NFKC", s).translate(_MAP).split())


def norm_lower_nopunct(s):
    """Variant: lowercase, no punctuation (Unicode category P*), whitespace collapsed."""
    t = "".join(c for c in norm(s).lower() if not unicodedata.category(c).startswith("P"))
    return " ".join(t.split())


def rates(ref, hyp, normalizer):
    """Return (cer, wer) of hyp against ref under the given normalizer. Reference must be non-empty."""
    r, h = normalizer(ref), normalizer(hyp)
    if not r.split():
        raise PipelineError("reference is empty after normalization")
    cer = Levenshtein.distance(r, h) / len(r)
    rw, hw = r.split(), h.split()
    wer = Levenshtein.distance(rw, hw) / len(rw)
    return cer, wer


def best_window(ref_tokens, hyp_tokens):
    """Semi-global word alignment: the ref window [s, e) minimizing edit distance to hyp (free ends in ref)."""
    n, m = len(ref_tokens), len(hyp_tokens)
    prev_d = [0] * (n + 1)
    prev_s = list(range(n + 1))
    for i in range(1, m + 1):
        d = [i] + [0] * n
        st = [0] + [0] * n
        for j in range(1, n + 1):
            sub = prev_d[j - 1] + (hyp_tokens[i - 1] != ref_tokens[j - 1])
            dele = prev_d[j] + 1
            ins = d[j - 1] + 1
            best = min(sub, dele, ins)
            d[j] = best
            st[j] = prev_s[j - 1] if best == sub else (prev_s[j] if best == dele else st[j - 1])
        prev_d, prev_s = d, st
    e = min(range(n + 1), key=lambda j: prev_d[j])
    return prev_s[e], e


def window_of_reference(ref, hyp):
    """Words (primary-normalized, case kept) of the ref window best matching hyp, plus its (start, end) span."""
    rt = norm(ref).split()
    keyed = [(i, norm_lower_nopunct(t)) for i, t in enumerate(rt)]
    keyed = [(i, k) for i, k in keyed if k]
    hk = [k for k in norm_lower_nopunct(hyp).split() if k]
    s, e = best_window([k for _, k in keyed], hk)
    if e <= s:
        raise PipelineError("partial alignment found an empty reference window")
    lo, hi = keyed[s][0], keyed[e - 1][0] + 1
    return " ".join(rt[lo:hi]), (lo, hi)


def load_handwriting(path):
    """URL -> handwriting label from the selection CSV; validates the allowed values."""
    with open(path, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        if not rd.fieldnames or not {"url", "handwriting"} <= set(rd.fieldnames):
            raise PipelineError(f"{path} must have columns url and handwriting; has {rd.fieldnames}")
        out = {}
        for row in rd:
            v = row["handwriting"].strip()
            if v not in HAND_VALUES:
                raise PipelineError(f"{path}: bad handwriting value {v!r} for {row['url']} (allowed {sorted(HAND_VALUES)})")
            out[row["url"].strip().rstrip("/")] = v
    return out


def load_model_pages(pid, slug):
    """Return {page: record} for one (letter, model) from transcriptions/, validating the contract."""
    from transcribe import validate_record
    recs = {}
    for f in sorted((TRANSCRIPTIONS_DIR / str(pid) / slug).glob("page_*.json")):
        rec = json.loads(f.read_text())
        validate_record(rec)
        recs[rec["page"]] = rec
    return recs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--post-ids", nargs="*", type=int)
    ap.add_argument("--models", nargs="*", help="model slugs (directory names); default: all present")
    ap.add_argument("--allow-partial", action="store_true")
    ap.add_argument("--handwriting-csv", default=str(HAND_CSV))
    a = ap.parse_args()

    hand = load_handwriting(a.handwriting_csv)
    con = sqlite3.connect(DB_PATH)
    ids = a.post_ids or sorted(int(p.name) for p in TRANSCRIPTIONS_DIR.iterdir() if p.is_dir() and p.name.isdigit())
    if not ids:
        raise PipelineError("no transcriptions found")
    letters, partial_rows = [], 0
    for pid in ids:
        row = con.execute("SELECT url, title, state, year, transcript FROM essays WHERE post_id=?", (pid,)).fetchone()
        if row is None:
            raise PipelineError(f"post {pid} not in {DB_PATH}")
        url, title, state, year, ref = row
        if ref is None:
            raise PipelineError(f"post {pid} has no human transcript (NULL); cannot score it")
        key = url.rstrip("/")
        if key not in hand:
            raise PipelineError(f"post {pid} ({url}) is missing from the handwriting CSV {a.handwriting_csv}")
        n_pages = len(list((PAGES_DIR / str(pid)).glob("page_*.png")))
        if n_pages == 0:
            raise PipelineError(f"post {pid}: no rendered pages")
        slugs = a.models or sorted(p.name for p in (TRANSCRIPTIONS_DIR / str(pid)).iterdir() if p.is_dir())
        models = []
        for slug in slugs:
            recs = load_model_pages(pid, slug)
            if not recs:
                raise PipelineError(f"post {pid} model {slug}: no page files")
            partial = set(recs) != set(range(1, n_pages + 1))
            if partial and not a.allow_partial:
                raise PipelineError(f"post {pid} model {slug}: pages {sorted(recs)} of {n_pages}; use --allow-partial")
            hyp = "\n".join(recs[p]["text"] for p in sorted(recs))
            span = None
            ref_used = ref
            if partial:
                ref_used, span = window_of_reference(ref, hyp)
                partial_rows += 1
            cer, wer = rates(ref_used, hyp, norm)
            cer2, wer2 = rates(ref_used, hyp, norm_lower_nopunct)
            models.append({
                "model": next(iter(recs.values()))["model"], "cer": cer, "wer": wer,
                "cer_lower_nopunct": cer2, "wer_lower_nopunct": wer2,
                "seconds_total": round(sum(r["seconds"] for r in recs.values()), 2),
                "pages_compared": sorted(recs), "partial": partial,
                "ref_word_span": list(span) if span else None, "excerpt": hyp.strip()})
        letters.append({"post_id": pid, "url": url, "title": title, "state": state, "year": year,
                        "pages": n_pages, "handwriting": hand[key], "archive_excerpt": ref, "models": models})

    stamp = now_utc()
    local = {"generated_at": stamp, "normalization": NORMALIZATION, "letters": letters}
    public = json.loads(json.dumps(local))
    for L in public["letters"]:
        L["archive_excerpt"] = L["archive_excerpt"].strip()[:EXCERPT_MAX]
        for m in L["models"]:
            m["excerpt"] = m["excerpt"][:EXCERPT_MAX]
            assert len(m["excerpt"]) <= EXCERPT_MAX
    for out, doc in ((LOCAL_OUT, local), (PUBLIC_OUT, public)):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1))

    print(f"{'post_id':>7} {'hand':<11} {'model':<34} {'CER':>6} {'WER':>6} {'CERlc':>6} {'WERlc':>6} {'secs':>7}  pages")
    for L in letters:
        for m in L["models"]:
            print(f"{L['post_id']:>7} {L['handwriting']:<11} {m['model']:<34} {m['cer']:6.3f} {m['wer']:6.3f} "
                  f"{m['cer_lower_nopunct']:6.3f} {m['wer_lower_nopunct']:6.3f} {m['seconds_total']:7.1f}  "
                  f"{m['pages_compared']}/{L['pages']}{' PARTIAL' if m['partial'] else ''}")
    print(f"\nwrote {LOCAL_OUT}\nwrote {PUBLIC_OUT}\npartial rows (scored against best-matching window): {partial_rows}")


if __name__ == "__main__":
    try:
        main()
    except PipelineError as e:
        fail(str(e))
