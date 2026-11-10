"""Tests for build_site.py (run: python3 -m pytest scripts/test_build_site.py, or python3 scripts/test_build_site.py)."""
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_site  # noqa: E402
from make_fixture_inputs import make_fixture  # noqa: E402


def run(root, *extra):
    """Run build_site.py as a subprocess against a fixture root; returns CompletedProcess.
    The fixture's one scanned letter is a 380-line page, so a build with the default MAX_IMAGE_EXPOSURE (0.20) fails
    by design; tests that are not about that cap pass --max-image-exposure 0.6 (never done for the real build)."""
    if "--max-image-exposure" not in extra:
        extra = (*extra, "--max-image-exposure", "0.6")
    return subprocess.run([sys.executable, str(HERE / "build_site.py"), "--root", str(root), "--allow-fixture", *extra],
                          capture_output=True, text=True)


def fresh():
    return make_fixture(tempfile.mkdtemp(prefix="pl_test_"))


def raises(fn, *a):
    try:
        fn(*a)
    except build_site.PipelineError:
        return True
    return False


def test_happy_path():
    root = fresh()
    r = run(root, "--local")
    assert r.returncode == 0, r.stdout + r.stderr
    info = json.loads((root / "site/data/build_info.json").read_text())
    assert info["fixture"] is True and info["counts"]["entities_person_dropped"] == 20
    assert info["exposure"]["fraction"] <= 0.15
    ents = json.loads((root / "site/data/entities.json").read_text())["entities"]
    assert ents and all(e["type"] != "person" for e in ents)
    assert (root / "site/data/vectors.i8").stat().st_size == info["counts"]["chunks"] * 384


def test_crops_built_and_counted():
    root = fresh()
    r = run(root)
    assert r.returncode == 0, r.stdout + r.stderr
    info = json.loads((root / "site/data/build_info.json").read_text())
    doc = json.loads((root / "site/data/crops.json").read_text())
    assert doc["fixture"] is True and len(doc["crops"]) == info["counts"]["crops"] == 22
    assert info["counts"]["crops_unaligned"] == 418 == len(info["crop_alignment"]["unaligned_list"])
    assert "UNALIGNED" in r.stdout
    for c in doc["crops"]:
        assert (root / "site/data" / c["file"]).is_file() and c["similarity"] >= info["crop_alignment"]["threshold"]
    ie = info["image_exposure"]
    assert 0 < ie["max"] <= ie["cap"] and ie["per_letter"][0]["post_id"] == 9001


def test_image_exposure_over_cap_fails():
    r = run(fresh(), "--max-image-exposure", "0.10")
    assert r.returncode != 0 and "FAILED" in r.stderr and "MAX_IMAGE_EXPOSURE" in r.stderr


def test_crop_unknown_field_fails():
    row = {"target": "chunk", "id": "9001-0", "post_id": 9001, "page": 1, "file": "crops/9001_p1_0.jpg",
           "box": [0, 0, 10, 10], "similarity": 0.9, "masked": [[1, 1, 5, 5]]}
    build_site.validate_public_crops([row], {"9001-0"}, {9001}, 0.6)
    assert raises(build_site.validate_public_crops, [dict(row, extra=1)], {"9001-0"}, {9001}, 0.6)
    assert raises(build_site.validate_public_crops, [dict(row, similarity=0.5)], {"9001-0"}, {9001}, 0.6)
    assert raises(build_site.validate_public_crops, [dict(row, masked=[[5, 5, 1, 1]])], {"9001-0"}, {9001}, 0.6)
    assert raises(build_site.validate_public_crops, [dict(row, masked=[[5, 5, 1, 1]])], {"9001-0"}, {9001}, 0.6)
    assert raises(build_site.validate_public_crops, [dict(row, id="nope")], {"9001-0"}, {9001}, 0.6)


def test_crop_missing_file_fails():
    root = fresh()
    assert run(root).returncode == 0
    crops = json.loads((root / "site/data/crops.json").read_text())["crops"]
    build_site.validate_crop_files(crops, root / "site/data")
    assert raises(build_site.validate_crop_files, [dict(crops[0], file="crops/9001_p1_99.jpg")], root / "site/data")


def test_low_similarity_snippet_gets_no_crop():
    import crops as cropmod
    root = fresh()
    boxes = cropmod.load_page_boxes(root / "enrich/page_boxes.jsonl")
    essays = {9001: {"transcript_chars": 7000}}
    snips = [{"target": "chunk", "id": "9001-0", "post_id": 9001, "start": 0,
              "text": "Lorem-style placeholder letter 01, sentence 000, is fake text."},
             {"target": "chunk", "id": "9001-x", "post_id": 9001, "start": 0,
              "text": "zebras quietly juggle xylophones beyond the quantum harbour while wizards vex"}]
    res = cropmod.compute_crops(snips, boxes, essays, root / "data/pages", 0.6, 0.9)
    assert [c["id"] for c in res["crops"]] == ["9001-0"]
    assert [u["id"] for u in res["unaligned"]] == ["9001-x"]


def _page_row(lines, W=1000, H=1000):
    """A synthetic page_boxes row; lines = [(text, y0, y1)]. Each line is full-width-ish with one word per token."""
    out = []
    for text, y0, y1 in lines:
        words, x = [], 50
        for w in text.split():
            words.append({"text": w, "box": [x, y0, x + 12 * len(w), y1]})
            x += 12 * len(w) + 10
        out.append({"text": text, "confidence": 1.0, "box": [50, y0, x, y1], "words": words})
    return {"post_id": 7, "page": 1, "width": W, "height": H, "engine": "t", "created_at": "t", "lines": out}


def _crops_for(lines, snippet, names=None):
    """compute_crops on one synthetic white page; returns (result, jpeg-decoded image of the first crop or None)."""
    import io
    import crops as cropmod
    from PIL import Image
    d = pathlib.Path(tempfile.mkdtemp(prefix="pl_mask_"))
    (d / "7").mkdir()
    Image.new("RGB", (1000, 1000), "white").save(d / "7" / "page_001.png")
    res = cropmod.compute_crops([{"target": "demo", "id": "7", "post_id": 7, "text": snippet, "start": 0}],
                                {7: {1: _page_row(lines)}}, {7: {"transcript_chars": len(snippet)}}, d, 0.6, 0.9, names)
    img = Image.open(io.BytesIO(next(iter(res["images"].values())))).convert("L") if res["images"] else None
    return res, img


def test_crop_on_id_pattern_line_is_masked():
    lines = [("H-45771", 100, 130), ("Dear sir I write to you today", 140, 170), ("about the matter of my appeal", 180, 210)]
    res, img = _crops_for(lines, "H-45771 Dear sir I write to you today about the matter of my appeal")
    (c,) = res["crops"]
    assert c["box"][1] == 100 and c["box"][3] <= 213, c["box"]          # tight: nothing above, a few px below
    assert c["masked"] and res["mask_stats"]["id_pattern"] == 1
    mx0, my0, mx1, my1 = c["masked"][0]
    assert img.getpixel((60 * img.width // 1000, (my0 + 5 - c["box"][1]) * img.width // 1000)) < 30  # painted black
    # header_block: the neighbour of an ID line with no blank gap is masked too, even though it is plain text.
    res, _ = _crops_for(lines, "Dear sir I write to you today about the matter of my appeal")
    assert res["mask_stats"]["header_block"] == 1 and len(res["crops"][0]["masked"]) == 1


def test_crop_masks_person_name_fuzzily():
    import crops as cropmod
    lines = [("Dear sir I write about Angela Webber", 100, 130), ("and the long matter of my appeal", 140, 170)]
    names = {7: cropmod.name_tokens(["Angela Webber"])}
    res, _ = _crops_for(lines, "Dear sir I write about Angela Webber and the long matter of my appeal", names)
    assert res["mask_stats"]["person"] == 2
    lines[0] = ("Dear sir I write about Angelo Webbar", 100, 130)  # Vision misread
    res, _ = _crops_for(lines, "Dear sir I write about Angelo Webbar and the long matter of my appeal", names)
    assert res["mask_stats"]["person"] == 2


def test_crop_with_outside_line_is_dropped():
    lines = [("a line above the snippet here", 100, 150), ("Dear sir I write to you today", 140, 190),
             ("about the matter of my appeal", 200, 230)]  # line 1 overlaps line 2's box by 10 of 50 px (20%) -> kept
    res, _ = _crops_for(lines, "Dear sir I write to you today about the matter of my appeal")
    assert len(res["crops"]) == 1
    lines[0] = ("a line above the snippet here", 100, 175)  # now overlaps 35 of 75 px (47%) -> dropped, counted, listed
    res, _ = _crops_for(lines, "Dear sir I write to you today about the matter of my appeal")
    assert not res["crops"] and res["mask_stats"]["dropped_outside_lines"] == 1 and "outside" in res["unaligned"][0]["reason"]


def _guard_inputs(model_excerpt, archive_excerpt="Dear sir. My address is Box 4430"):
    pe = [{"post_id": 1, "title": "T", "excerpt": "Dear sir."}]
    demo = {"letters": [{"post_id": 1, "title": "T", "archive_excerpt": archive_excerpt,
                         "models": [{"model": "m", "excerpt": model_excerpt}]}]}
    return pe, [], [], demo


def test_guard_rejects_unredacted_id_and_redaction_clears_it():
    assert raises(build_site.guard_id_patterns, *_guard_inputs("H-45771 Dear sir."))          # raw model header
    assert raises(build_site.guard_id_patterns, *_guard_inputs("Dear sir."))                  # archive's own Box 4430
    pe, ch, ents, demo = _guard_inputs("H\u20144577l Dear sir, Tabor City, N.C.28463", "Box E\u201422600 San Quentin, CA 94974")
    n = build_site.redact_public(pe, ch, ents, demo)
    assert n == 4, (n, demo)                      # H--4577l, Tabor City N.C.28463, Box E--22600, San Quentin CA 94974
    build_site.guard_id_patterns(pe, ch, ents, demo)   # 0 hits left
    assert "\u2588\u2588\u2588" in demo["letters"][0]["archive_excerpt"] and "22600" not in str(demo)


def test_address_line_and_person_extension_and_religious_skip():
    import crops as cropmod
    from idpatterns import is_address_line
    assert is_address_line("Tabor City, N.C.28463") and is_address_line("New Castle, In. 47362")
    assert not is_address_line("Dear Sir, I write to you")
    lines = [("By Beverly Ann Smith", 100, 130), ("and Abu bin Hassan wrote this letter", 140, 170),
             ("Yashua and Yahweh Elohim", 180, 210), ("W. R. Carroll", 220, 250)]
    skipped = []
    names = {7: cropmod.name_tokens(["Beverly Smith", "Abu Hassan", "Yashua", "Yahweh Elohim", "W.R. Carroll"], skipped)}
    assert sorted(skipped) == ["Yahweh Elohim", "Yashua"] and "yashua" not in names[7]
    res, _ = _crops_for(lines, " ".join(t for t, _, _ in lines), names)
    got = {tuple(m) for m in res["crops"][0]["masked"]}
    row = _page_row(lines)["lines"]
    covered = lambda ln, k: any(m[0] <= row[ln]["words"][k]["box"][0] and m[2] >= row[ln]["words"][k]["box"][2]  # noqa: E731
                                and m[1] <= row[ln]["words"][k]["box"][1] and m[3] >= row[ln]["words"][k]["box"][3] for m in got)
    assert all(covered(0, k) for k in (1, 2, 3)) and not covered(0, 0)       # byline: everything after "By"
    assert covered(1, 1) and covered(1, 2) and covered(1, 3) and not covered(1, 0)  # Abu bin Hassan whole
    assert not any(covered(2, k) for k in range(4))                           # deities untouched
    assert all(covered(3, k) for k in range(3))                               # W. R. Carroll


def test_header_block_never_absorbs_archive_text():
    import crops as cropmod
    from compare import norm_lower_nopunct
    lines = [("#111204", 100, 130), ("How do we do it today", 140, 170)]
    row = _page_row(lines)
    arch = norm_lower_nopunct("How do we do it today and tomorrow")
    assert [m["reason"] for m in cropmod.find_masks(row, set(), "")] == ["id_pattern", "header_block"]
    assert [m["reason"] for m in cropmod.find_masks(row, set(), arch)] == ["id_pattern"]


def test_fixture_refused_without_flag():
    r = subprocess.run([sys.executable, str(HERE / "build_site.py"), "--root", str(fresh())],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "FAILED" in r.stderr


def test_person_entity_fails():
    assert raises(build_site.validate_public_entities, [{"post_id": 1, "type": "person", "text": "X"}])


def test_overlong_snippet_fails():
    assert raises(build_site.validate_public_chunks, [{"chunk_id": "1-0", "post_id": 1, "snippet": "x" * 161}], 160)


def test_exposure_over_cap_fails():
    r = run(fresh(), "--max-exposure", "0.01")
    assert r.returncode != 0 and "FAILED" in r.stderr and "exceeds MAX_EXPOSURE" in r.stderr


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
