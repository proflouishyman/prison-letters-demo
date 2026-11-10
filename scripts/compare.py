#!/usr/bin/env python3
"""Compute CER/WER of each model's transcription against the archive's human transcript (ground truth).

Usage: compare.py [--post-ids ...] [--models ...] [--allow-partial] [--handwriting-csv data/selection_candidates.csv]

Writes site/data/local/transcription_demo.json (full text, local only) and site/data/transcription_demo.json
(public: excerpts <= 300 chars, aligned to the archive excerpt) per contract §4, and prints a plain-text table.

Normalization (primary): NFKC, unify quotes and dashes, collapse whitespace, case kept.
Variant ("lower_nopunct"): primary, then lowercase and drop every punctuation character.
CER = char-level Levenshtein / len(reference chars); WER = whitespace-token Levenshtein / len(reference tokens).

A letter is compared in full only when every rendered page has a transcription for the model. The human
transcript is not split by page, so with --allow-partial a subset of pages is scored against the
best-matching word window of the human text (semi-global word alignment); such rows carry partial=true.
Without the flag, incomplete coverage is an error.

A page whose transcription FAILED (transcribe.py leaves transcriptions/<id>/<slug>/fail_NNN.json) is a counted
result, not an error: the (letter, model) row gets cer/wer = null, failed_pages and failures, and is left out of
that model's group means. A page with neither a transcription nor a failure record is still a hard error.
--models takes Ollama tags (e.g. richardyoung/olmocr2:7b-q8) or `pdf-text-layer`; they are mapped to slugs.
"""
import argparse
import csv
import json
import pathlib
import sqlite3
import unicodedata

from rapidfuzz.distance import Levenshtein

from idpatterns import find_hits
from pl_common import ROOT, PipelineError, fail, model_slug, now_utc

HAND_VALUES = {"handwritten", "typed", "mixed"}
EXCERPT_MAX = 200  # contract allows <= 300; 200 keeps the 20-letter public build under MAX_EXPOSURE
EXCERPT_ALIGN_MIN = 0.60  # min similarity of the model window to the archive excerpt, and share of archive words matched
TOKEN_SIM_MIN = 0.70      # a substituted model word is kept only if this similar to the archive word it replaces
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


def public_model_excerpt(model_text, archive_excerpt, archive_full=None):
    """The public excerpt of a model's output: ONLY the model words that align with the PUBLISHED archive_excerpt
    (never the first N chars of raw output, which carries headers the archive omitted: prison IDs, addresses).

    1. best_window (the same semi-global word alignment as window_of_reference) finds the stretch of the model output
       matching the archive excerpt;
    2. inside it, a word-level Levenshtein alignment keeps model words that equal an archive word or are a close
       substitution (similarity >= TOKEN_SIM_MIN); words with no archive counterpart (insertions: a header or address
       the archive left out) are withheld;
    3. a kept word that would introduce an identifier pattern (idpatterns) not present in the archive's own text
       (`archive_full`, default the excerpt) is withheld too.
    Returns (excerpt, unaligned). Truly empty output (e.g. a PDF with no text layer) gives ("", False). Output whose
    window is too dissimilar (< EXCERPT_ALIGN_MIN) or of which fewer than EXCERPT_ALIGN_MIN of the archive words are
    matched gives ("", True): counted and listed by main().
    """
    toks = [(w, norm_lower_nopunct(w)) for w in norm(model_text).split()]
    toks = [(w, k) for w, k in toks if k]
    if not toks:
        return "", False
    arch = [k for k in norm_lower_nopunct(archive_excerpt).split() if k]
    if not arch:
        raise PipelineError("public_model_excerpt: archive excerpt is empty after normalization")
    s, e = best_window([k for _, k in toks], arch)
    win = toks[s:e]
    if not win:
        return "", True
    if 1 - Levenshtein.normalized_distance(" ".join(k for _, k in win), " ".join(arch)) < EXCERPT_ALIGN_MIN:
        return "", True
    keep = []
    for tag, i1, i2, j1, j2 in Levenshtein.opcodes([k for _, k in win], arch):
        if tag == "equal":
            keep += win[i1:i2]
        elif tag == "replace":
            for x, y in zip(win[i1:i2], arch[j1:j2]):
                if 1 - Levenshtein.normalized_distance(x[1], y) >= TOKEN_SIM_MIN:
                    keep.append(x)
    if len(keep) < EXCERPT_ALIGN_MIN * len(arch):
        return "", True
    allowed = norm(archive_full if archive_full is not None else archive_excerpt)
    while True:  # withhold words that would introduce an identifier pattern the archive text does not contain
        text, spans, pos = " ".join(w for w, _ in keep), [], 0
        for w, _ in keep:
            spans.append((pos, pos + len(w)))
            pos += len(w) + 1
        bad = [h for h in find_hits(text) if norm(h) not in allowed]
        if not bad:
            break
        drop = set()
        for h in bad:
            at = text.find(h)
            drop |= {i for i, (a, b) in enumerate(spans) if a < at + len(h) and b > at}
        keep = [t for i, t in enumerate(keep) if i not in drop]
    return " ".join(w for w, _ in keep)[:EXCERPT_MAX], False


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


def load_model_pages(trans_dir, pid, slug):
    """Return ({page: record}, {page: failure record}) for one (letter, model), validating both contracts."""
    from transcribe import validate_failure, validate_record
    d = trans_dir / str(pid) / slug
    recs, fails = {}, {}
    for f in sorted(d.glob("page_*.json")):
        rec = json.loads(f.read_text())
        validate_record(rec)
        recs[rec["page"]] = rec
    for f in sorted(d.glob("fail_*.json")):
        rec = json.loads(f.read_text())
        validate_failure(rec)
        fails[rec["page"]] = rec
    both = set(recs) & set(fails)
    if both:
        raise PipelineError(f"post {pid} {slug}: pages {sorted(both)} have both a transcription and a failure record")
    return recs, fails


def group_summary(letters):
    """Per (handwriting group, model): letters scored (n), failed pages, mean CER/WER, mean seconds per page.

    Letters with a failed page for that model are excluded from the CER/WER means (they have null rates) but
    their failed pages are counted; seconds/page averages over every successfully transcribed page.
    """
    acc = {}
    for L in letters:
        for m in L["models"]:
            g = acc.setdefault((L["handwriting"], m["model"]), {"cer": [], "wer": [], "secs": 0.0, "pages": 0, "failed": 0})
            g["secs"] += m["seconds_total"]
            g["pages"] += len(m["pages_compared"])
            g["failed"] += m["failed_pages"]
            if m["cer"] is not None:
                g["cer"].append(m["cer"])
                g["wer"].append(m["wer"])
    return acc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--post-ids", nargs="*", type=int)
    ap.add_argument("--models", nargs="*", help="Ollama tags or pdf-text-layer (mapped to slugs); default: all present")
    ap.add_argument("--allow-partial", action="store_true")
    ap.add_argument("--root", default=str(ROOT), help="repo root holding db/, data/, transcriptions/, site/ (tests)")
    ap.add_argument("--handwriting-csv", default=None, help="default: <root>/data/selection_candidates.csv")
    a = ap.parse_args(argv)
    root = pathlib.Path(a.root)
    db_path, trans_dir, pages_dir = root / "db" / "apwa_mini.sqlite", root / "transcriptions", root / "data" / "pages"
    local_out = root / "site" / "data" / "local" / "transcription_demo.json"
    public_out = root / "site" / "data" / "transcription_demo.json"

    hand_csv = a.handwriting_csv or str(root / "data" / "selection_candidates.csv")
    hand = load_handwriting(hand_csv)
    con = sqlite3.connect(db_path)
    ids = a.post_ids or sorted(int(p.name) for p in trans_dir.iterdir() if p.is_dir() and p.name.isdigit())
    if not ids:
        raise PipelineError("no transcriptions found")
    letters, partial_rows = [], 0
    pub_excerpt = {}  # (post_id, model tag) -> (public excerpt, unaligned flag)
    for pid in ids:
        row = con.execute("SELECT url, title, state, year, transcript FROM essays WHERE post_id=?", (pid,)).fetchone()
        if row is None:
            raise PipelineError(f"post {pid} not in {db_path}")
        url, title, state, year, ref = row
        if ref is None:
            raise PipelineError(f"post {pid} has no human transcript (NULL); cannot score it")
        key = url.rstrip("/")
        if key not in hand:
            raise PipelineError(f"post {pid} ({url}) is missing from the handwriting CSV {hand_csv}")
        n_pages = len(list((pages_dir / str(pid)).glob("page_*.png")))
        if n_pages == 0:
            raise PipelineError(f"post {pid}: no rendered pages")
        slugs = [model_slug(t) for t in a.models] if a.models else sorted(
            p.name for p in (trans_dir / str(pid)).iterdir() if p.is_dir())
        models = []
        for slug in slugs:
            recs, fails = load_model_pages(trans_dir, pid, slug)
            if not recs and not fails:
                raise PipelineError(f"post {pid} model {slug}: no page files")
            missing = sorted(set(range(1, n_pages + 1)) - set(recs) - set(fails))
            if missing and not a.allow_partial:
                raise PipelineError(f"post {pid} model {slug}: pages {missing} of {n_pages} have neither a "
                                    f"transcription nor a failure record; use --allow-partial")
            tag = next(iter({**recs, **fails}.values()))["model"]
            if fails:
                # Counted, reported failure: no rates for this letter/model; never dropped silently.
                models.append({
                    "model": tag, "cer": None, "wer": None, "cer_lower_nopunct": None, "wer_lower_nopunct": None,
                    "seconds_total": round(sum(r["seconds"] for r in recs.values()), 2),
                    "pages_compared": sorted(recs), "partial": False, "ref_word_span": None, "excerpt": "",
                    "failed_pages": len(fails),
                    "failures": [{"post_id": pid, "page": pg, "reason": fails[pg]["reason"]} for pg in sorted(fails)]})
                continue
            partial = bool(missing)
            hyp = "\n".join(recs[p]["text"] for p in sorted(recs))
            span = None
            ref_used = ref
            if partial:
                ref_used, span = window_of_reference(ref, hyp)
                partial_rows += 1
            pub_excerpt[(pid, tag)] = public_model_excerpt(hyp, ref.strip()[:EXCERPT_MAX], ref)
            cer, wer = rates(ref_used, hyp, norm)
            cer2, wer2 = rates(ref_used, hyp, norm_lower_nopunct)
            models.append({
                "model": tag, "cer": cer, "wer": wer,
                "cer_lower_nopunct": cer2, "wer_lower_nopunct": wer2,
                "seconds_total": round(sum(r["seconds"] for r in recs.values()), 2),
                "pages_compared": sorted(recs), "partial": partial,
                "ref_word_span": list(span) if span else None, "excerpt": hyp.strip(),
                "failed_pages": 0, "failures": []})
        letters.append({"post_id": pid, "url": url, "title": title, "state": state, "year": year,
                        "pages": n_pages, "handwriting": hand[key], "archive_excerpt": ref, "models": models})

    stamp = now_utc()
    local = {"generated_at": stamp, "normalization": NORMALIZATION, "letters": letters}
    public = json.loads(json.dumps(local))
    for L in public["letters"]:
        L["archive_excerpt"] = L["archive_excerpt"].strip()[:EXCERPT_MAX]
        for m in L["models"]:
            if m["failed_pages"]:
                m["excerpt"] = ""  # no output to publish; counted as a failure, not as unaligned
                continue
            m["excerpt"], unaligned = pub_excerpt[(L["post_id"], m["model"])]
            if unaligned:
                m["excerpt_unaligned"] = True  # contract §4: withheld because it did not align, not "no text layer"
            assert len(m["excerpt"]) <= EXCERPT_MAX
    for out, doc in ((local_out, local), (public_out, public)):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1))

    f4 = lambda x: "  FAIL" if x is None else f"{x:6.3f}"
    print(f"{'post_id':>7} {'hand':<11} {'model':<34} {'CER':>6} {'WER':>6} {'CERlc':>6} {'WERlc':>6} {'secs':>7}  pages")
    for L in letters:
        for m in L["models"]:
            note = f" FAILED on {m['failed_pages']} page(s)" if m["failed_pages"] else ""
            print(f"{L['post_id']:>7} {L['handwriting']:<11} {m['model']:<34} {f4(m['cer'])} {f4(m['wer'])} "
                  f"{f4(m['cer_lower_nopunct'])} {f4(m['wer_lower_nopunct'])} {m['seconds_total']:7.1f}  "
                  f"{m['pages_compared']}/{L['pages']}{' PARTIAL' if m['partial'] else ''}{note}")
    print("\nGROUP MEANS (letters with a failed page are excluded from CER/WER; failed = failed pages)")
    print(f"{'group':<11} {'model':<34} {'n':>3} {'failed':>6} {'CER':>6} {'WER':>6} {'sec/page':>9}")
    for (grp, model), g in sorted(group_summary(letters).items()):
        mean = lambda xs: f"{sum(xs) / len(xs):6.3f}" if xs else "   n/a"
        spp = f"{g['secs'] / g['pages']:9.1f}" if g["pages"] else "      n/a"
        print(f"{grp:<11} {model:<34} {len(g['cer']):>3} {g['failed']:>6} {mean(g['cer'])} {mean(g['wer'])} {spp}")
    n_un = [(L["post_id"], m["model"]) for L in public["letters"] for m in L["models"] if m.get("excerpt_unaligned")]
    n_al = sum(1 for L in public["letters"] for m in L["models"] if m["excerpt"])
    print(f"\nPUBLIC EXCERPTS: {n_al} aligned to the archive excerpt, {len(n_un)} UNALIGNED (published as empty, flagged "
          f"excerpt_unaligned)" + "".join(f"\n  excerpt_unaligned: post {p} {mt}" for p, mt in n_un))
    print(f"\nwrote {local_out}\nwrote {public_out}\npartial rows (scored against best-matching window): {partial_rows}")


if __name__ == "__main__":
    try:
        main()
    except PipelineError as e:
        fail(str(e))
