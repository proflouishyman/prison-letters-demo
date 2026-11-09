#!/usr/bin/env python3
"""Render every page of data/pdfs/<post_id>.pdf to data/pages/<post_id>/page_NNN.png (contract §2).

Usage: render_pages.py [--post-ids 1 2 ...]   (default: every essay in the db)
Uses `pdftoppm -r 200 -png`; pages already rendered are skipped. Output page count must equal the
PDF's page count (checked with pdfinfo) or the run fails.
"""
import argparse
import re
import shutil
import sqlite3
import subprocess

from pl_common import DB_PATH, IMAGE_DPI, PAGES_DIR, PDF_DIR, PipelineError, fail


def pdf_page_count(pdf):
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True)
    m = re.search(r"^Pages:\s+(\d+)", out.stdout, re.M)
    if out.returncode or not m:
        raise PipelineError(f"pdfinfo failed on {pdf}: {out.stderr.strip()}")
    return int(m.group(1))


def render(post_id):
    """Render one essay; returns the number of pages. Idempotent."""
    pdf = PDF_DIR / f"{post_id}.pdf"
    if not pdf.exists():
        raise PipelineError(f"missing PDF {pdf}")
    n = pdf_page_count(pdf)
    out = PAGES_DIR / str(post_id)
    expected = [out / f"page_{i:03d}.png" for i in range(1, n + 1)]
    if all(p.exists() for p in expected):
        return n
    tmp = PAGES_DIR / f".tmp_{post_id}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    r = subprocess.run(["pdftoppm", "-r", str(IMAGE_DPI), "-png", str(pdf), str(tmp / "p")],
                       capture_output=True, text=True)
    if r.returncode:
        raise PipelineError(f"pdftoppm failed for {post_id}: {r.stderr.strip()}")
    # pdftoppm names files p-<n>.png with zero padding that depends on the page count; renumber to page_NNN.
    files = sorted(tmp.glob("p-*.png"), key=lambda f: int(f.stem.split("-")[1]))
    if len(files) != n:
        raise PipelineError(f"{post_id}: rendered {len(files)} pages, PDF has {n}")
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    for i, f in enumerate(files, 1):
        f.rename(out / f"page_{i:03d}.png")
    shutil.rmtree(tmp)
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--post-ids", nargs="*", type=int)
    a = ap.parse_args()
    ids = a.post_ids
    if not ids:
        con = sqlite3.connect(DB_PATH)
        ids = [r[0] for r in con.execute("SELECT post_id FROM essays ORDER BY post_id")]
    if not ids:
        fail("no essays to render")
    failures = []
    for pid in ids:
        try:
            print(f"{pid}: {render(pid)} pages")
        except PipelineError as e:
            failures.append(f"{pid}: {e}")
    if failures:
        fail("\n".join(failures))


if __name__ == "__main__":
    main()
