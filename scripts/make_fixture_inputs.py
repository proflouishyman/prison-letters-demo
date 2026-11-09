#!/usr/bin/env python3
"""Generate FAKE inputs for every build_site.py input into a (temporary) root directory.

All text is invented placeholder text; every fixture file is marked "fixture": true (JSONL rows carry it per row,
the SQLite db carries meta(fixture='true')). Usage: make_fixture_inputs.py [dir]  -> prints the root path.
"""
import json
import pathlib
import sqlite3
import sys
import tempfile

import numpy as np

N_ESSAYS, SENT_PER_CHUNK, N_CHUNKS = 20, 19, 20
EMB_MODEL, DIM = "Xenova/bge-small-en-v1.5", 384


def transcript_for(i):
    """Deterministic fake transcript: 380 sentences, chunked every 19 sentences (~1,200 chars/chunk)."""
    return " ".join(f"Lorem-style placeholder letter {i:02d}, sentence {k:03d}, is fake text."
                    for k in range(SENT_PER_CHUNK * N_CHUNKS))


def make_fixture(root):
    """Write all fixture inputs under root; returns root as a Path."""
    root = pathlib.Path(root)
    for d in ("db", "enrich", "site/data/local"):
        (root / d).mkdir(parents=True, exist_ok=True)
    pids = [9000 + i for i in range(1, N_ESSAYS + 1)]
    transcripts = {p: transcript_for(i) for i, p in enumerate(pids, 1)}

    db = root / "db" / "apwa_mini.sqlite"
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE essays (post_id INTEGER PRIMARY KEY, url TEXT, slug TEXT, title TEXT, author TEXT,
        state TEXT, date_text TEXT, year INTEGER, genre TEXT, extent_pages INTEGER, subjects TEXT, language TEXT,
        pdf_url TEXT, transcript TEXT, transcript_chars INTEGER, fetched_at TEXT, source TEXT,
        harvester_version TEXT)""")
    con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    con.execute("INSERT INTO meta VALUES ('fixture','true')")
    for i, p in enumerate(pids, 1):
        t = transcripts[p]
        con.execute("INSERT INTO essays VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (p, f"https://prisonwitness.org/FIXTURE-not-a-real-essay-{i:02d}", f"fixture-{i:02d}",
                     f"Lorem-style placeholder letter {i}", f"Placeholder, Author {i:02d}", "Placeholder State",
                     "circa 2000", 2000, "Essay", 4, json.dumps(["Placeholder subject"]), "English",
                     f"https://prisonwitness.org/FIXTURE-not-a-real-essay-{i:02d}.pdf", t, len(t),
                     "2000-01-01T00:00:00Z", "prisonwitness.org/html", "FIXTURE"))
    con.commit()
    con.close()

    chunks, ents = [], []
    for p in pids:
        t = transcripts[p]
        sentence_len = len(t.split(", is fake text.")[0]) + len(", is fake text.")
        for n in range(N_CHUNKS):
            start = n * SENT_PER_CHUNK * (sentence_len + 1)
            end = start + SENT_PER_CHUNK * (sentence_len + 1) - 1
            chunks.append({"chunk_id": f"{p}-{n}", "post_id": p, "n": n, "start": start, "end": end,
                           "text": t[start:end], "fixture": True})
        q = "placeholder letter"
        s = t.index(q)
        base = {"post_id": p, "model": "fixture-model", "prompt_sha256": "0" * 64, "fixture": True}
        ents.append(dict(base, type="facility", text=q, quote=q, start=s, end=s + len(q), verified=True))
        ents.append(dict(base, type="person", text="Fake Person", quote=q, start=s, end=s + len(q), verified=True))
        ents.append(dict(base, type="place", text="not in text", quote="not in text", start=0, end=11,
                         verified=False))
    _jsonl(root / "enrich" / "chunks.jsonl", chunks)
    _jsonl(root / "enrich" / "entities.jsonl", ents)
    _jsonl(root / "enrich" / "themes.jsonl", [
        {"post_id": p, "subjects": ["Placeholder subject"], "affect": ["hope"], "sentiment": "neutral",
         "stance": "describing", "model": "fixture-model", "prompt_sha256": "0" * 64, "fixture": True}
        for p in pids])

    rng = np.random.default_rng(0)
    emb = rng.normal(size=(len(chunks), DIM)).astype("<f4")
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    emb.astype("<f4").tofile(root / "enrich" / "embeddings.f32")
    (root / "enrich" / "embeddings.meta.json").write_text(json.dumps(
        {"model": EMB_MODEL, "dim": DIM, "normalized": True, "n_rows": len(chunks), "fixture": True}))

    def demo(full):
        letters = []
        for i, p in enumerate(pids, 1):
            t = transcripts[p]
            letters.append({"post_id": p, "url": f"https://prisonwitness.org/FIXTURE-not-a-real-essay-{i:02d}",
                            "title": f"Lorem-style placeholder letter {i}", "state": "Placeholder State",
                            "year": 2000, "pages": 4, "handwriting": "handwritten" if i <= 10 else "typed",
                            "archive_excerpt": t if full else t[:300],
                            "models": [{"model": m, "cer": 0.1 * k, "wer": 0.2 * k,
                                        "cer_lower_nopunct": 0.09 * k, "wer_lower_nopunct": 0.19 * k,
                                        "seconds_total": 10.0, "pages_compared": [1, 2, 3, 4],
                                        "partial": False, "ref_word_span": None,
                                        "excerpt": "Fake model output placeholder."}
                                       for k, m in enumerate(("fixture-model-a", "fixture-model-b"), 1)]})
        return {"fixture": True, "generated_at": "2000-01-01T00:00:00Z",
                "normalization": "FIXTURE placeholder", "letters": letters}
    (root / "site" / "data" / "transcription_demo.json").write_text(json.dumps(demo(False)))
    (root / "site" / "data" / "local" / "transcription_demo.json").write_text(json.dumps(demo(True)))
    return root


def _jsonl(path, rows):
    """Write rows as JSON lines."""
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="pl_fixture_")
    print(make_fixture(target))
