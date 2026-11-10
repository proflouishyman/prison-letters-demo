"""Snippet -> page-scan alignment and crops (contract §5 page_boxes.jsonl, §6 crops.json).

Pure functions plus one orchestrator, `compute_crops`, which returns everything in memory (crop JPEG bytes included);
build_site.py validates it and only then writes files. Nothing here guesses: a snippet whose best fuzzy match scores
below the threshold gets NO crop and is returned in `unaligned`.

Alignment: the snippet and each page's OCR text are normalized with compare.norm_lower_nopunct (lowercase, no
punctuation, whitespace collapsed). Lines of a page are joined with single spaces; rapidfuzz's partial_ratio_alignment
finds the best-matching stretch of that page text (character-level Indel similarity, 0..1) and the lines that stretch
touches are the matched lines. A match never spans pages (a snippet running over a page break is judged on its
better page, and counted unaligned if that is below the threshold).
"""
import io
import json
import re
import statistics

from PIL import Image, ImageDraw
from rapidfuzz import fuzz

from compare import norm_lower_nopunct
from idpatterns import FACILITY_PATTERN, ID_PATTERNS, is_address_line
from pl_common import PipelineError

SIM_THRESHOLD = 0.60     # minimum similarity for a crop; tuned on the 20 demo letters (see docs §6, build output)
BOTTOM_PAD_PX = 3        # the only padding: a few px below the last matched line, NONE above the first (privacy: the
                         # line above a snippet is often a header the archive omitted)
OUTSIDE_TOL = 0.25       # a non-matched, non-masked line whose box overlaps the crop by more than this share drops the crop
MASK_PAD_PX = 4          # black boxes are the line/word box padded by this much
HEADER_TOP_SHARE = 0.30  # a "Facility" line in the top 30% of a page counts as a header line
HEADER_MAX_WORDS = 6     # header block chains only through lines this short
HEADER_CHAIN_SHARE = 0.35  # ... and only in the top share of the page
NEIGHBOUR_GAP = 1.0      # header block: neighbours closer than this many median line heights (no blank gap) join a hit
NAME_STOP = {"judge", "mister", "governor", "senator", "doctor", "jesus", "christ", "president", "king", "mark",
             "state", "court"}  # title/common words never treated as a person-name token
MAX_CROP_WIDTH = 900     # px; crops are downscaled to at most this width
JPEG_QUALITY = 70
POS_MARGIN = 0.15        # a snippet may sit this far (as a share of the letter) outside a page's text span
TARGETS = ("chunk", "essay", "demo")

PAGE_ROW = {"post_id": int, "page": int, "width": int, "height": int, "engine": str, "created_at": str, "lines": list}
LINE = {"text": str, "confidence": (int, float), "box": list, "words": list}


def load_page_boxes(path):
    """Read enrich/page_boxes.jsonl (§5), validating every row strictly. Returns {post_id: {page: row}}."""
    if not path.exists():
        raise PipelineError(f"page_boxes: input file not found: {path}")
    out = {}
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        where = f"page_boxes line {i}"
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PipelineError(f"{where}: invalid JSON ({e})")
        _check(row, PAGE_ROW, where)
        W, H = row["width"], row["height"]
        for j, ln in enumerate(row["lines"]):
            lw = f"{where} line[{j}]"
            _check(ln, LINE, lw)
            b = ln["box"]
            if len(b) != 4 or not all(isinstance(v, int) and not isinstance(v, bool) for v in b) \
                    or not (0 <= b[0] < b[2] <= W and 0 <= b[1] < b[3] <= H):
                raise PipelineError(f"{lw}: bad box {b} for a {W}x{H} page")
            if not 0 <= ln["confidence"] <= 1:
                raise PipelineError(f"{lw}: confidence {ln['confidence']} outside 0..1")
            for k, w in enumerate(ln["words"]):
                _check(w, {"text": str, "box": list}, f"{lw} word[{k}]")
        key = (row["post_id"], row["page"])
        if row["page"] < 1 or row["post_id"] in out and row["page"] in out[row["post_id"]]:
            raise PipelineError(f"{where}: bad or duplicate page {key}")
        out.setdefault(row["post_id"], {})[row["page"]] = row
    if not out:
        raise PipelineError("page_boxes: file is empty")
    return out


def _check(obj, spec, where):
    """Strict shape check: exact key set, exact types (bool is never an int)."""
    if not isinstance(obj, dict):
        raise PipelineError(f"{where}: expected an object")
    if set(obj) != set(spec):
        raise PipelineError(f"{where}: fields {sorted(obj)} != required {sorted(spec)}")
    for k, t in spec.items():
        if isinstance(obj[k], bool) or not isinstance(obj[k], t):
            raise PipelineError(f"{where}: field '{k}' has type {type(obj[k]).__name__}")


def page_text(row):
    """Normalized page text and, per non-empty line, (start, end, line) char spans within it."""
    parts, spans, pos = [], [], 0
    for ln in row["lines"]:
        t = norm_lower_nopunct(ln["text"])
        if not t:
            continue
        spans.append((pos, pos + len(t), ln))
        parts.append(t)
        pos += len(t) + 1
    return " ".join(parts), spans


def best_match(snippet, pages, frac):
    """Best (similarity, page, matched lines) for a snippet over the candidate pages, or None.
    `frac` = snippet start offset / transcript length; pages whose share of the letter's OCR text, widened by
    POS_MARGIN, does not contain it are not searched (the offset only says roughly where the snippet is)."""
    s = norm_lower_nopunct(snippet)
    if not s:
        raise PipelineError("alignment: snippet is empty after normalization")
    texts = {p: page_text(r) for p, r in pages.items()}
    total = sum(len(t) for t, _ in texts.values()) or 1
    best, lo = None, 0
    for p in sorted(texts):
        t, spans = texts[p]
        hi = lo + len(t)
        in_range = lo / total - POS_MARGIN <= frac <= hi / total + POS_MARGIN
        lo = hi
        if not in_range or len(t) < len(s):
            continue
        al = fuzz.partial_ratio_alignment(s, t)
        sim = al.score / 100.0
        lines = [ln for a, b, ln in spans if a < al.dest_end and b > al.dest_start]
        if lines and (best is None or sim > best[0]):
            best = (sim, p, lines)
    return best


def crop_box(lines, W, H):
    """Full-width box tight to the matched lines: no padding above the first, BOTTOM_PAD_PX below the last."""
    y0 = min(l["box"][1] for l in lines)
    y1 = max(l["box"][3] for l in lines)
    return [0, max(0, y0), W, min(H, y1 + BOTTOM_PAD_PX)]


# Deities and religious figures that the entity tagger sometimes files under `person`: never masked (counted as skips).
RELIGIOUS = {"yashua", "yahweh", "elohim", "ruah", "jesus", "god", "christ", "savior", "messiah", "hamashiak",
             "allah", "mohammed", "muhammad"}
PARTICLES = {"abu", "bin", "ibn", "de", "del", "la", "le", "van", "von", "al", "el", "bint", "der", "da", "di"}
NOT_NAME = {"by", "the", "dear", "to", "and", "of", "in", "on", "for", "from", "with", "but", "he", "she", "we", "my",
            "our", "his", "her", "it", "is", "as", "at", "an", "or"}  # capitalized sentence words that never extend a name


def name_tokens(texts, skipped=None):
    """Lowercase alphanumeric name tokens (>= 4 chars, not in NAME_STOP) from person entity texts / author fields.
    Entity texts containing a RELIGIOUS token contribute no such token; each such text is appended to `skipped`."""
    out = set()
    for t in texts:
        toks = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in re.split(r"[\s,.]+", t)]
        if skipped is not None and any(w in RELIGIOUS for w in toks):
            skipped.append(t)
        out |= {w for w in toks if len(w) >= 4 and w not in NAME_STOP and w not in RELIGIOUS}
    return out


def _is_capword(text):
    """A capitalized word or a dotted initial ('W.'); not a bare 'I'/'A' and not a common sentence word."""
    w = re.sub(r"^[^A-Za-z]+|[^A-Za-z.]+$", "", text)
    if not w or w.lower().strip(".") in NOT_NAME:
        return False
    return w[0].isupper() and (len(w.strip(".")) >= 2 or w.endswith("."))


def _name_span(words, i):
    """Indexes of the whole name around word i: adjacent capitalized words/initials either side, joined across
    particles (Abu, bin, de ...) that sit between two name words."""
    keep = {i}
    for step in (-1, 1):
        j = i + step
        while 0 <= j < len(words):
            if _is_capword(words[j]["text"]):
                keep.add(j)
            elif words[j]["text"].lower().strip(".") in PARTICLES and 0 <= j + step < len(words) \
                    and _is_capword(words[j + step]["text"]):
                keep.add(j)
            else:
                break
            j += step
    return keep


def _word_is_name(word, tokens):
    """Fuzzy: Vision misreads handwriting, so near-spellings (ratio >= 80, or a name glued inside a word) count."""
    w = re.sub(r"[^a-z0-9]", "", word.lower())
    if len(w) < 3:
        return False
    for t in tokens:
        if w == t or (len(t) >= 5 and (t in w or (len(w) >= 4 and fuzz.ratio(w, t) >= 80))):
            return True
        # Handwriting misreads ("Freckerck" for "Frederick", ratio 78): a looser match when the first letter agrees.
        # Over-masking is the safe direction for names.
        if len(t) >= 5 and len(w) >= 5 and w[0] == t[0] and fuzz.ratio(w, t) >= NAME_LOOSE_RATIO:
            return True
    return False


NAME_LOOSE_RATIO = 70  # same-first-letter fuzzy name match for handwriting OCR misreads (144627 "Freckerck")
ARCHIVE_LINE_SIM = 0.60  # a line this similar to some window of the archive transcript is the letter's own text


def in_archive(text, archive):
    """True if the scan line (normalized like the alignment) fuzzily occurs in the archive transcript `archive`
    (already normalized with norm_lower_nopunct). Header lines (names, IDs, addresses) are not in the transcript;
    body text and titles are. Lines under 6 normalized chars cannot be judged and count as NOT in the archive."""
    t = norm_lower_nopunct(text)
    return bool(archive) and len(t) >= 6 and fuzz.partial_ratio(t, archive) / 100.0 >= ARCHIVE_LINE_SIM


def find_masks(row, tokens, archive=""):
    """All privacy masks on one page: list of {reason, box, line} with `line` = index into row['lines'].

    reason person: the whole name around a word fuzzily matching a person-name token (adjacent capitalized words and
    initials, particles like Abu/bin between them; everything after "By" in a byline; all of a short header-area line),
    or the whole line when the words don't localise the match;
    id_pattern: the whole line matches an ID/address pattern, or is a 'Facility' line near the page top;
    header_block: the line directly above/below an id_pattern line with no blank gap (< NEIGHBOUR_GAP median line
    heights) between them, i.e. the same name/ID/address block.
    """
    W, H, lines = row["width"], row["height"], row["lines"]
    pad = lambda b: [max(0, b[0] - MASK_PAD_PX), max(0, b[1] - MASK_PAD_PX),
                     min(W, b[2] + MASK_PAD_PX), min(H, b[3] + MASK_PAD_PX)]  # noqa: E731
    out = []
    hit = set()
    for i, ln in enumerate(lines):
        t = ln["text"]
        if any(p.search(t) for p in ID_PATTERNS) or is_address_line(t) or (FACILITY_PATTERN.search(t) and ln["box"][1] < HEADER_TOP_SHARE * H):
            hit.add(i)
            out.append({"reason": "id_pattern", "box": pad(ln["box"]), "line": i})
    if lines:
        med = statistics.median(l["box"][3] - l["box"][1] for l in lines)
        # BFS from the hits: a neighbour with no blank gap is part of the block; the block keeps growing only through
        # short lines (<= HEADER_MAX_WORDS words) in the top HEADER_CHAIN_SHARE of the page, so it never swallows a body.
        seen, frontier = set(hit), sorted(hit)
        while frontier:
            i = frontier.pop(0)
            for j in (i - 1, i + 1):
                if not (0 <= j < len(lines)) or j in seen:
                    continue
                a, b = (lines[j], lines[i]) if j < i else (lines[i], lines[j])
                if b["box"][1] - a["box"][3] >= NEIGHBOUR_GAP * med:
                    continue
                if in_archive(lines[j]["text"], archive):
                    continue  # the letter's own text is never absorbed into a header block
                short = len(lines[j]["words"]) <= HEADER_MAX_WORDS and lines[j]["box"][1] < HEADER_CHAIN_SHARE * H
                if i in hit or short:
                    seen.add(j)
                    out.append({"reason": "header_block", "box": pad(lines[j]["box"]), "line": j})
                    if short:
                        frontier.append(j)
    if tokens:
        done = {m["line"] for m in out}
        for i, ln in enumerate(lines):
            if i in done:
                continue
            words = ln["words"]
            idx = [k for k, w in enumerate(words) if _word_is_name(w["text"], tokens)]
            if idx:
                if words[0]["text"].lower().strip(".:,") == "by" and len(words) > 1:
                    keep = set(range(1, len(words)))          # byline: everything after "By"
                elif len(words) <= 6 and ln["box"][1] < HEADER_TOP_SHARE * H and not in_archive(ln["text"], archive):
                    keep = set(range(len(words)))             # short line in the header area
                else:
                    keep = set().union(*(_name_span(words, k) for k in idx))
                out.extend({"reason": "person", "box": pad(words[k]["box"]), "line": i} for k in sorted(keep))
            elif any(len(t) >= 5 and fuzz.partial_ratio(t, norm_lower_nopunct(ln["text"]).replace(" ", "")) >= 85
                     for t in tokens):
                out.append({"reason": "person", "box": pad(ln["box"]), "line": i})  # words did not localise it
    return out


def overlap_rows(a, b):
    """Vertical overlap in px of two [x0,y0,x1,y1] boxes (crops are full width, so only y matters)."""
    return max(0, min(a[3], b[3]) - max(a[1], b[1]))


def render_crop(png_path, box, masked=()):
    """Cut `box` from the page PNG with every `masked` rectangle painted solid black (source-page pixels) BEFORE
    downscaling, downscale to <= MAX_CROP_WIDTH wide, return JPEG bytes."""
    with Image.open(png_path) as im:
        im = im.convert("RGB")
        c = im.crop(tuple(box))
    dr = ImageDraw.Draw(c)
    for m in masked:
        dr.rectangle([m[0] - box[0], m[1] - box[1], m[2] - box[0] - 1, m[3] - box[1] - 1], fill=(0, 0, 0))
    if c.width > MAX_CROP_WIDTH:
        c = c.resize((MAX_CROP_WIDTH, max(1, round(c.height * MAX_CROP_WIDTH / c.width))), Image.LANCZOS)
    buf = io.BytesIO()
    c.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def union_rows(boxes):
    """Total height covered by the union of [y0, y1) row intervals (crops are full-width)."""
    tot, cur = 0, None
    for y0, y1 in sorted((b[1], b[3]) for b in boxes):
        if cur is None or y0 > cur[1]:
            if cur:
                tot += cur[1] - cur[0]
            cur = [y0, y1]
        else:
            cur[1] = max(cur[1], y1)
    return tot + (cur[1] - cur[0] if cur else 0)


def compute_crops(snippets, boxes, essays, pages_dir, threshold, max_image_exposure, names=None, archives=None):
    """Align every snippet and render its crop.

    snippets: list of dicts {target, id, post_id, text, start} (start = offset of the text in the transcript).
    boxes: load_page_boxes output. essays: {post_id: row with transcript_chars}. pages_dir: data/pages.
    Returns dict: crops (contract rows, order of `snippets`), images {file: jpeg bytes}, unaligned [{target,id,reason}],
    scores [(target, id, similarity|None)] for every snippet, exposure {...}. Raises PipelineError when the
    per-letter image exposure exceeds max_image_exposure or a page image is missing / has the wrong size.
    archives: {post_id: norm_lower_nopunct(transcript)}; lines found in it are never header-block/short-line masked.
    names: {post_id: set of lowercase name tokens} (see name_tokens) for person masking; absent = no person masks.
    Each crop row carries `masked` (black boxes, source-page pixels, clipped to the crop). A crop is DROPPED (counted in
    `unaligned`) when a page line outside the snippet's matched window overlaps it and is not itself masked.
    `mask_stats` counts the masks in the kept image files by reason, plus the number of crops dropped for that cause.
    """
    names = names or {}
    archives = archives or {}
    crops, images, unaligned, scores = [], {}, [], []
    file_of, counter = {}, {}
    stats = {"person": 0, "id_pattern": 0, "header_block": 0, "files_with_masks": 0, "dropped_outside_lines": 0}
    for sn in snippets:
        pid = sn["post_id"]
        pages = boxes.get(pid)
        if not pages:
            unaligned.append({"target": sn["target"], "id": sn["id"], "reason": "no page boxes for this letter"})
            scores.append((sn["target"], sn["id"], None))
            continue
        frac = sn["start"] / max(1, essays[pid]["transcript_chars"])
        m = best_match(sn["text"], pages, frac)
        if m is None:
            unaligned.append({"target": sn["target"], "id": sn["id"], "reason": "no candidate page"})
            scores.append((sn["target"], sn["id"], None))
            continue
        sim, page, lines = m
        scores.append((sn["target"], sn["id"], sim))
        if sim < threshold:
            unaligned.append({"target": sn["target"], "id": sn["id"], "reason": f"best similarity {sim:.3f} < {threshold}"})
            continue
        row = pages[page]
        box = crop_box(lines, row["width"], row["height"])
        allm = find_masks(row, names.get(pid, set()), archives.get(pid, ""))
        inbox = []
        for m in allm:
            b = m["box"]
            if overlap_rows(b, box) > 0:
                inbox.append({"reason": m["reason"], "line": m["line"],
                              "box": [b[0], max(b[1], box[1]), b[2], min(b[3], box[3])]})
        wholly = {m["line"] for m in allm if m["reason"] != "person"}  # lines blacked out entirely
        matched_ids = {id(l) for l in lines}
        outside = [ln for i, ln in enumerate(row["lines"]) if id(ln) not in matched_ids and i not in wholly
                   and overlap_rows(ln["box"], box) > OUTSIDE_TOL * (ln["box"][3] - ln["box"][1])]
        if outside:
            stats["dropped_outside_lines"] += 1
            unaligned.append({"target": sn["target"], "id": sn["id"], "reason":
                              f"crop would show {len(outside)} line(s) outside the snippet's matched window"})
            continue
        masked = sorted({tuple(m["box"]) for m in inbox})
        key = (pid, page, tuple(box))
        if key not in file_of:  # identical regions (e.g. chunk 0, essay opening, archive excerpt) share one file
            n = counter.get(pid, 0)
            counter[pid] = n + 1
            name = f"crops/{pid}_p{page}_{n}.jpg"
            png = pages_dir / str(pid) / f"page_{page:03d}.png"
            if not png.exists():
                raise PipelineError(f"crop for {sn['target']} {sn['id']}: page image missing: {png}")
            with Image.open(png) as im:
                if im.size != (row["width"], row["height"]):
                    raise PipelineError(f"{png}: image is {im.size}, page_boxes says {(row['width'], row['height'])}")
            images[name] = render_crop(png, box, masked)
            file_of[key] = name
            seen = set()
            for m in inbox:
                if (m["reason"], tuple(m["box"])) not in seen:
                    seen.add((m["reason"], tuple(m["box"])))
                    stats[m["reason"]] += 1
            stats["files_with_masks"] += bool(masked)
        crops.append({"target": sn["target"], "id": str(sn["id"]), "post_id": pid, "page": page,
                      "file": file_of[key], "box": box, "similarity": round(sim, 4),
                      "masked": [list(m) for m in masked]})
    exposure = image_exposure(crops, boxes, max_image_exposure)
    return {"crops": crops, "images": images, "unaligned": unaligned, "scores": scores, "exposure": exposure,
            "mask_stats": stats}


def image_exposure(crops, boxes, cap):
    """Per letter: union of cropped page area / total area of all that letter's pages. Fails over the cap.
    Returns {per_letter: [{post_id, fraction, crops}], overall, max, mean, cap}; letters without crops count as 0."""
    per, tot_c, tot_a = [], 0, 0
    for pid in sorted(boxes):
        pages = boxes[pid]
        area = sum(r["width"] * r["height"] for r in pages.values())
        mine = [c for c in crops if c["post_id"] == pid]
        cov = sum(union_rows([c["box"] for c in mine if c["page"] == p]) * pages[p]["width"] for p in pages)
        per.append({"post_id": pid, "fraction": round(cov / area, 6), "crops": len(mine)})
        tot_c += cov
        tot_a += area
    fr = [x["fraction"] for x in per]
    over = [x for x in per if x["fraction"] > cap]
    if over:
        raise PipelineError("image exposure over MAX_IMAGE_EXPOSURE "
                            f"{cap}: " + ", ".join(f"{x['post_id']}={x['fraction']:.3f}" for x in over))
    return {"per_letter": per, "overall": round(tot_c / tot_a, 6), "max": max(fr), "mean": round(statistics.mean(fr), 6),
            "cap": cap}
