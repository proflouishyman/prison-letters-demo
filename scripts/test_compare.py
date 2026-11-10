#!/usr/bin/env python3
"""Tests for compare.py: model-tag -> slug mapping, recorded failures, and missing-page errors.

Run: python3 scripts/test_compare.py   (builds a throwaway repo root under a mktemp dir; deletes nothing)
Regression for SOLUTIONS.md "[2026-11-09] compare.py --models rejected real model tags".
"""
import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from pl_common import model_slug  # noqa: E402

TAG = "richardyoung/olmocr2:7b-q8"   # contains both ':' and '/', the characters model_slug rewrites
TEXT = "The quick brown fox jumps over the lazy dog again and again."


def page_rec(pid, page, tag, text):
    return {"post_id": pid, "page": page, "model": tag, "model_digest": "d", "backend": "ollama",
            "prompt_sha256": "p", "params": {"temperature": 0, "num_ctx": 16384}, "image_dpi": 200,
            "text": text, "seconds": 2.0, "output_tokens": 5, "created_at": "2026-01-01T00:00:00Z"}


def build_root():
    """Letter 1 (2 pages) fully transcribed; letter 2 (2 pages) has only page 1 transcribed."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="pl_cmp_"))
    (root / "db").mkdir()
    con = sqlite3.connect(root / "db" / "apwa_mini.sqlite")
    con.execute("CREATE TABLE essays (post_id INTEGER, url TEXT, title TEXT, state TEXT, year INTEGER, transcript TEXT)")
    for pid in (1, 2):
        con.execute("INSERT INTO essays VALUES (?,?,?,?,?,?)",
                    (pid, f"https://example.org/e{pid}/", f"T{pid}", "X", 2000, TEXT + " " + TEXT))
    con.commit()
    con.close()
    (root / "data").mkdir()
    (root / "data" / "selection_candidates.csv").write_text(
        "url,handwriting\nhttps://example.org/e1/,typed\nhttps://example.org/e2/,handwritten\n")
    slug = model_slug(TAG)
    for pid in (1, 2):
        (root / "data" / "pages" / str(pid)).mkdir(parents=True)
        for pg in (1, 2):
            (root / "data" / "pages" / str(pid) / f"page_{pg:03d}.png").write_bytes(b"")
        d = root / "transcriptions" / str(pid) / slug
        d.mkdir(parents=True)
        (d / "page_001.json").write_text(json.dumps(page_rec(pid, 1, TAG, TEXT)))
        if pid == 1:
            (d / "page_002.json").write_text(json.dumps(page_rec(pid, 2, TAG, TEXT)))
    return root, slug


def run(root, *extra):
    return subprocess.run([sys.executable, str(HERE / "compare.py"), "--root", str(root), *extra],
                          capture_output=True, text=True)


def main():
    root, slug = build_root()
    d2 = root / "transcriptions" / "2" / slug

    # 1. Page 2 of letter 2 has neither a transcription nor a failure record: hard error.
    r = run(root, "--models", TAG)
    assert r.returncode != 0 and "neither a transcription nor a failure record" in r.stderr, r.stderr

    # 2. With a recorded failure: succeeds, counts it, excludes the letter from the mean, keeps going.
    (d2 / "fail_002.json").write_text(json.dumps(
        {"post_id": 2, "page": 2, "model": TAG, "reason": "looped", "created_at": "2026-01-01T00:00:00Z"}))
    r = run(root, "--models", TAG)   # a real tag (':' and '/'), not a slug: the old code failed here
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads((root / "site/data/local/transcription_demo.json").read_text())
    rows = {L["post_id"]: L["models"][0] for L in doc["letters"]}
    assert rows[1]["cer"] == 0.0 and rows[1]["failed_pages"] == 0 and rows[1]["failures"] == []
    assert rows[2]["cer"] is None and rows[2]["failed_pages"] == 1
    assert rows[2]["failures"] == [{"post_id": 2, "page": 2, "reason": "looped"}]
    assert "FAILED on 1 page(s)" in r.stdout
    hand = [ln for ln in r.stdout.splitlines() if ln.startswith("handwritten") and "n/a" in ln]
    assert hand and hand[0].split()[2:4] == ["0", "1"], r.stdout  # 0 letters scored, 1 failed page, mean n/a
    typed = [ln for ln in r.stdout.splitlines() if ln.startswith("typed")]
    assert typed and typed[0].split()[2:4] == ["1", "0"], typed

    # 2b. The public output with a failed row must pass both validators (frontend JS and build_site.py).
    pub = root / "site" / "data"
    v = subprocess.run(["node", str(HERE / "validate_built.mjs"), str(pub), "public", "--demo-only"],
                       capture_output=True, text=True)
    assert v.returncode == 0, v.stdout + v.stderr
    sys.path.insert(0, str(HERE))
    import build_site
    demo = json.loads((pub / "transcription_demo.json").read_text())
    build_site.validate_demo(demo, True, {1: {}, 2: {}}, 160)
    demo["letters"][1]["models"][0]["cer"] = 0.5   # non-null rate with failed_pages > 0 must be rejected
    try:
        build_site.validate_demo(demo, True, {1: {}, 2: {}}, 160)
        raise AssertionError("build_site accepted a non-null CER on a failed row")
    except build_site.PipelineError:
        pass

    # 2c. Public model excerpts: only the span aligned with the published archive excerpt; headers the archive omitted
    # (prison ID, P.O. box) never reach the public file, an unalignable output becomes "" + excerpt_unaligned.
    import compare
    ex, un = compare.public_model_excerpt("H-45771\nP.O. Box 730 Tabor City\n" + TEXT, TEXT)
    assert (ex, un) == (TEXT, False), (ex, un)
    assert compare.public_model_excerpt("zebras juggle xylophones beyond harbour wizards vex quartz", TEXT) == ("", True)
    assert compare.public_model_excerpt("", TEXT) == ("", False)           # genuinely empty output: not "unaligned"
    d1 = root / "transcriptions" / "1" / slug
    (d1 / "page_001.json").write_text(json.dumps(page_rec(1, 1, TAG, "H-45771 P.O. Box 730 Tabor City " + TEXT)))
    r = run(root, "--models", TAG)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "H-45771" in (root / "site/data/local/transcription_demo.json").read_text()   # local keeps raw output
    pubtxt = (pub / "transcription_demo.json").read_text()
    assert "H-45771" not in pubtxt and "Tabor City" not in pubtxt
    assert "PUBLIC EXCERPTS: 1 aligned" in r.stdout and "0 UNALIGNED" in r.stdout, r.stdout
    (d1 / "page_001.json").write_text(json.dumps(page_rec(1, 1, TAG, "zebras juggle xylophones H-45771 harbour")))
    (d1 / "page_002.json").write_text(json.dumps(page_rec(1, 2, TAG, "wizards vex quartz Box 730")))
    r = run(root, "--models", TAG)
    assert r.returncode == 0 and "1 UNALIGNED" in r.stdout and "excerpt_unaligned: post 1" in r.stdout, r.stdout
    m1 = json.loads((pub / "transcription_demo.json").read_text())["letters"][0]["models"][0]
    assert m1["excerpt"] == "" and m1["excerpt_unaligned"] is True and m1["cer"] is not None
    v = subprocess.run(["node", str(HERE / "validate_built.mjs"), str(pub), "public", "--demo-only"],
                       capture_output=True, text=True)
    assert v.returncode == 0, v.stdout + v.stderr
    (d1 / "page_001.json").write_text(json.dumps(page_rec(1, 1, TAG, TEXT)))   # restore for the steps below
    (d1 / "page_002.json").write_text(json.dumps(page_rec(1, 2, TAG, TEXT)))

    # 3. A page with both a transcription and a failure record is ambiguous: hard error.
    (d2 / "page_002.json").write_text(json.dumps(page_rec(2, 2, TAG, TEXT)))
    assert run(root, "--models", TAG).returncode != 0
    print("PASS: test_compare")


if __name__ == "__main__":
    main()
