"""Add Tesseract lines to enrich/page_boxes.jsonl where Apple Vision found nothing (contract §5).

Why: Vision VNRecognizeTextRequest silently skips lines even with strip passes (e.g. 136440 p1, 138462 p1,
138495 p3), so snippets on those lines could not be located. Tesseract (local, `--psm 4`) reads typed pages reliably.

Rule: a Tesseract line is added only if no existing line overlaps it vertically by >= 50% of the shorter height
AND horizontally at all (so Vision lines always win; nothing is replaced or removed). Lines with no alphanumeric
text are skipped. Each added line has the same shape as a Vision line {text, confidence, box, words} and the page's
`engine` string records that Tesseract lines were merged and how many. Loud failure: a tesseract error stops the run.

Usage: python3 scripts/ocr_merge_tesseract.py [--root REPO]   (rewrites enrich/page_boxes.jsonl in place;
idempotent: pages whose engine already mentions tesseract are re-derived from their Vision lines only)
"""
import argparse, csv, io, json, pathlib, re, subprocess, sys

TAG = " + tesseract psm4 gap-fill"


def tesseract_lines(png):
    """Line records from `tesseract <png> - --psm 4 tsv`; confidence = mean word conf / 100."""
    p = subprocess.run(["tesseract", str(png), "-", "--psm", "4", "tsv"], capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"FAILED: tesseract on {png}: {p.stderr.strip()}")
    lines = {}
    for r in csv.DictReader(io.StringIO(p.stdout), delimiter="\t", quoting=csv.QUOTE_NONE):
        if r["level"] != "5" or not r["text"].strip():
            continue
        x, y, w, h = (int(r[k]) for k in ("left", "top", "width", "height"))
        lines.setdefault((r["block_num"], r["par_num"], r["line_num"]), []).append(
            {"text": r["text"], "box": [x, y, x + w, y + h], "conf": max(0.0, float(r["conf"]))})
    out = []
    for words in lines.values():
        text = " ".join(w["text"] for w in words)
        if not re.search(r"[A-Za-z0-9]", text):
            continue
        box = [min(w["box"][0] for w in words), min(w["box"][1] for w in words),
               max(w["box"][2] for w in words), max(w["box"][3] for w in words)]
        out.append({"text": text, "confidence": round(sum(w["conf"] for w in words) / len(words) / 100, 4),
                    "box": box, "words": [{"text": w["text"], "box": w["box"]} for w in words]})
    return out


def overlaps(a, b):
    """True when boxes share >= 50% of the shorter height vertically and overlap horizontally."""
    v = min(a[3], b[3]) - max(a[1], b[1])
    h = min(a[2], b[2]) - max(a[0], b[0])
    return h > 0 and v >= 0.5 * min(a[3] - a[1], b[3] - b[1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parent.parent))
    a = ap.parse_args()
    root = pathlib.Path(a.root)
    path = root / "enrich" / "page_boxes.jsonl"
    rows = [json.loads(l) for l in path.open()]
    total = 0
    for r in rows:
        if TAG in r["engine"]:  # re-derive: drop previously merged lines
            r["lines"] = [l for l in r["lines"] if l.get("source") != "tesseract"]
            r["engine"] = re.sub(re.escape(TAG) + r".*$", "", r["engine"])
        png = root / "data" / "pages" / str(r["post_id"]) / f"page_{r['page']:03d}.png"
        if not png.exists():
            sys.exit(f"FAILED: missing page image {png}")
        added = [t for t in tesseract_lines(png) if not any(overlaps(t["box"], l["box"]) for l in r["lines"])]
        for t in added:
            t["source"] = "tesseract"
        r["lines"] = sorted(r["lines"] + added, key=lambda l: (l["box"][1], l["box"][0]))
        r["engine"] += f"{TAG} ({len(added)} lines added)"
        total += len(added)
        print(f"{r['post_id']} p{r['page']}: +{len(added)} tesseract lines", flush=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    tmp.replace(path)
    print(f"OK: {total} tesseract lines added across {len(rows)} pages")


if __name__ == "__main__":
    main()
