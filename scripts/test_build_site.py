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
    """Run build_site.py as a subprocess against a fixture root; returns CompletedProcess."""
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
