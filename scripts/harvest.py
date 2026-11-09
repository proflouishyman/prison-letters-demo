#!/usr/bin/env python3
"""Harvest APWA essay pages into db/apwa_mini.sqlite (contract §1) and download their PDFs.

Usage:
  harvest.py --urls urls.txt [--db db/apwa_mini.sqlite] [--seed N]
  harvest.py --from-html DIR [--download-pdfs]     # offline: parse saved *.html pages

Loud failure: a page that cannot be fetched/parsed, or lacks a required field, is counted in
harvest_runs.failures, printed under a FAILED banner, and the exit code is non-zero. Optional fields
that are absent from the page are stored as NULL and counted in the run summary (never defaulted).
"""
import argparse
import glob
import html as htmllib
import json
import pathlib
import re
import sqlite3
import sys
import time
import urllib.request

from bs4 import BeautifulSoup, NavigableString

from pl_common import DB_PATH, PDF_DIR, PipelineError, fail, git_version, now_utc

UA = "Mozilla/5.0 (research survey)"
DELAY = 1.5  # seconds between requests
SOURCE = "prisonwitness.org/html"

SCHEMA = """
CREATE TABLE IF NOT EXISTS essays (
  post_id INTEGER PRIMARY KEY, url TEXT NOT NULL, slug TEXT NOT NULL, title TEXT NOT NULL,
  author TEXT, state TEXT, date_text TEXT, year INTEGER, genre TEXT, extent_pages INTEGER,
  subjects TEXT NOT NULL, language TEXT, pdf_url TEXT, transcript TEXT,
  transcript_chars INTEGER NOT NULL, fetched_at TEXT NOT NULL, source TEXT NOT NULL,
  harvester_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS harvest_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
  seed INTEGER, n_requested INTEGER NOT NULL, n_ok INTEGER, n_failed INTEGER, failures TEXT);
"""

# Contract §1 field -> (type, required). Used to validate every row before it is written.
ROW_SPEC = {
    "post_id": (int, True), "url": (str, True), "slug": (str, True), "title": (str, True),
    "author": (str, False), "state": (str, False), "date_text": (str, False), "year": (int, False),
    "genre": (str, False), "extent_pages": (int, False), "subjects": (str, True),
    "language": (str, False), "pdf_url": (str, False), "transcript": (str, False),
    "transcript_chars": (int, True), "fetched_at": (str, True), "source": (str, True),
    "harvester_version": (str, True),
}
OPTIONAL_FIELDS = [k for k, (_, req) in ROW_SPEC.items() if not req]


def validate_row(row):
    """Raise PipelineError unless `row` has exactly the contract §1 fields with the right types."""
    if set(row) != set(ROW_SPEC):
        raise PipelineError(f"row fields mismatch: extra={set(row) - set(ROW_SPEC)} missing={set(ROW_SPEC) - set(row)}")
    for k, (typ, req) in ROW_SPEC.items():
        v = row[k]
        if v is None:
            if req:
                raise PipelineError(f"required field {k} is NULL")
        elif not isinstance(v, typ) or isinstance(v, bool):
            raise PipelineError(f"field {k} has type {type(v).__name__}, expected {typ.__name__}")
    if row["transcript"] is None and row["transcript_chars"] != 0:
        raise PipelineError("transcript NULL but transcript_chars != 0")
    if row["transcript"] is not None and row["transcript_chars"] != len(row["transcript"]):
        raise PipelineError("transcript_chars != len(transcript)")
    if not isinstance(json.loads(row["subjects"]), list):
        raise PipelineError("subjects is not a JSON array")


def parse_meta(soup):
    """Return {heading: [values]} from the 'Details' grid (div.awpa-heading + div.apwa-values spans)."""
    meta = {}
    for field in soup.select("div.apwa-meta-field"):
        head = field.select_one("div.awpa-heading")
        vals = field.select_one("div.apwa-values")
        if head is None or vals is None:
            raise PipelineError("meta field without heading/values")
        spans = [s.get_text(strip=True) for s in vals.find_all("span", recursive=False)]
        meta[head.get_text(strip=True)] = [s for s in spans if s]
    return meta


def parse_transcript(soup):
    """Transcript = the text node right after the 'Transcript' heading's wrapper <div>.

    Returns None when the page has no Transcript heading or the block is empty (contract: NULL).
    The text keeps the page's own line breaks.
    """
    heads = [s for s in soup.find_all("strong") if s.get_text(strip=True) == "Transcript"]
    if len(heads) > 1:
        raise PipelineError(f"{len(heads)} 'Transcript' headings found")
    if not heads:
        return None
    wrapper = heads[0].find_parent("div")
    nxt = wrapper.next_sibling
    if not isinstance(nxt, NavigableString):
        return None
    text = htmllib.unescape(str(nxt)).strip()
    return text or None


def parse_page(html_text, url_hint=None):
    """Parse one essay page into a contract §1 row (minus fetched_at, which the caller supplies)."""
    soup = BeautifulSoup(html_text, "html.parser")
    body = soup.find("body")
    ids = set(re.findall(r"\bpostid-(\d+)\b", " ".join(body.get("class", [])) if body else ""))
    short = soup.find("link", rel="shortlink")
    short_ids = set(re.findall(r"[?&]p=(\d+)", short["href"])) if short else set()
    if len(ids) != 1:
        raise PipelineError(f"expected exactly one postid-NNN body class, got {ids}")
    if short_ids and short_ids != ids:
        raise PipelineError(f"postid body class {ids} disagrees with shortlink {short_ids}")
    post_id = int(next(iter(ids)))

    canon = soup.find("link", rel="canonical")
    url = canon["href"] if canon else url_hint
    if not url:
        raise PipelineError("no canonical URL")
    slug = url.rstrip("/").rsplit("/", 1)[-1]

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else None
    if not title:
        raise PipelineError("no <h1> title")

    pdfs = set(re.findall(r"https://prisonwitness\.org/wp-content/uploads/sites/3/[^\s\"'<>&]*?\.pdf", html_text))
    if len(pdfs) > 1:
        raise PipelineError(f"multiple distinct PDF URLs: {sorted(pdfs)}")
    pdf_url = pdfs.pop() if pdfs else None

    meta = parse_meta(soup)
    def one(key):
        return "; ".join(meta[key]) if meta.get(key) else None
    date_text = one("Date")
    m = re.search(r"\b(\d{4})\b", date_text) if date_text else None
    ext = one("Extent")
    m2 = re.match(r"\s*(\d+)\s*page", ext) if ext else None
    if ext and not m2:
        raise PipelineError(f"unparseable Extent {ext!r}")
    transcript = parse_transcript(soup)
    return {
        "post_id": post_id, "url": url, "slug": slug, "title": title,
        "author": one("Author"), "state": one("State"), "date_text": date_text,
        "year": int(m.group(1)) if m else None, "genre": one("Genre"),
        "extent_pages": int(m2.group(1)) if m2 else None,
        "subjects": json.dumps(meta.get("Subject", [])), "language": one("Language"),
        "pdf_url": pdf_url, "transcript": transcript,
        "transcript_chars": len(transcript) if transcript else 0,
        "source": SOURCE, "harvester_version": git_version(),
    }


def http_get(url, binary=False):
    """GET with the fixed generic UA; one retry, then raises."""
    last = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
                data = r.read()
            return data if binary else data.decode("utf-8")
        except Exception as e:  # noqa: BLE001 - re-raised below as PipelineError
            last = e
            time.sleep(DELAY)
    raise PipelineError(f"fetch failed for {url}: {last!r}")


def download_pdf(post_id, pdf_url):
    """Save the PDF to data/pdfs/<post_id>.pdf (skipped if a valid one already exists)."""
    dest = PDF_DIR / f"{post_id}.pdf"
    if dest.exists() and dest.read_bytes()[:5] == b"%PDF-":
        return dest
    data = http_get(pdf_url, binary=True)
    if data[:5] != b"%PDF-":
        raise PipelineError(f"{pdf_url} did not return a PDF")
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    time.sleep(DELAY)
    return dest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--urls", help="file with one essay URL per line")
    src.add_argument("--from-html", help="directory of saved essay *.html pages (offline)")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--pattern", default="*.html", help="with --from-html: filename glob (default *.html)")
    ap.add_argument("--seed", type=int, default=None, help="sampling seed, recorded in harvest_runs (NULL if none)")
    ap.add_argument("--download-pdfs", action="store_true",
                    help="with --from-html: also download PDFs (always on with --urls)")
    a = ap.parse_args()

    # Each item: (label, loader returning html text)
    if a.urls:
        urls = [u.strip() for u in open(a.urls) if u.strip() and not u.startswith("#")]
        items = [(u, u) for u in urls]
    else:
        files = sorted(glob.glob(f"{a.from_html}/{a.pattern}"))
        items = [(f, None) for f in files]
    if not items:
        fail("no inputs found")
    online = a.urls is not None
    want_pdf = online or a.download_pdfs

    pathlib.Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(a.db)
    con.executescript(SCHEMA)
    started = now_utc()
    cur = con.execute("INSERT INTO harvest_runs (started_at, seed, n_requested) VALUES (?,?,?)",
                      (started, a.seed, len(items)))
    run_id = cur.lastrowid
    con.commit()

    ok, failures, null_counts = 0, [], {k: 0 for k in OPTIONAL_FIELDS}
    n_pdf = 0
    for i, (label, url) in enumerate(items):
        try:
            if online:
                if i:
                    time.sleep(DELAY)
                html_text = http_get(url)
                fetched = now_utc()
                row = parse_page(html_text, url_hint=url)
            else:
                html_text = open(label, encoding="utf-8").read()
                fetched = now_utc()
                row = parse_page(html_text)
            row["fetched_at"] = fetched
            validate_row(row)
            con.execute(f"INSERT OR REPLACE INTO essays ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                        list(row.values()))
            con.commit()
            for k in OPTIONAL_FIELDS:
                null_counts[k] += row[k] is None
            if want_pdf:
                if not row["pdf_url"]:
                    raise PipelineError("page has no PDF link")
                download_pdf(row["post_id"], row["pdf_url"])
                n_pdf += 1
            ok += 1
            print(f"ok {row['post_id']} {row['slug']} transcript_chars={row['transcript_chars']}")
        except Exception as e:  # noqa: BLE001 - recorded and reported loudly below
            failures.append({"url": url or label, "error": f"{type(e).__name__}: {e}"})
    con.execute("UPDATE harvest_runs SET finished_at=?, n_ok=?, n_failed=?, failures=? WHERE run_id=?",
                (now_utc(), ok, len(failures), json.dumps(failures), run_id))
    con.commit()
    con.close()

    print(f"\nSUMMARY run_id={run_id} requested={len(items)} ok={ok} failed={len(failures)} pdfs={n_pdf}")
    nulls = ", ".join(f"{k}={v}" for k, v in null_counts.items() if v) or "none"
    print(f"NULL counts (optional fields absent on page): {nulls}")
    if failures:
        fail("\n".join(f"{f['url']}: {f['error']}" for f in failures))


if __name__ == "__main__":
    try:
        main()
    except PipelineError as e:
        fail(str(e))
