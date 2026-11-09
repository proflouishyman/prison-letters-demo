# Prison letters: a small AI demo

A small, local-first demo of what AI tools could do for an archive of writing by incarcerated people.
It uses 20 letters (10 handwritten, 10 typewritten) from the public
[American Prison Writing Archive](https://prisonwitness.org/). The archive is the place to read them.

- **Transcription test:** local vision models (run through Ollama, with no cloud services) transcribe each letter. The
  results are scored by character and word error rate against the archive's human transcriptions.
- **Search:** keyword and semantic (embedding) search, running entirely in the browser.
- **What's in the letters:** places, institutions, policies and conditions mentioned, with themes and tone. Every
  item is checked against the letter's text. People's names are deliberately excluded.

This is an independent demo, not an official APWA product.

## What is and isn't published
Full essays are **not** republished. The public site carries metadata, short snippets (capped, with total exposure
checked at build time), search vectors and extracted labels, and every item links back to the original. Full text,
scans and raw model output stay on the local machine (see `.gitignore`). `scripts/build_site.py` refuses to build
if the snippet cap, the exposure cap or the no-person-names rule is broken.

## Layout
- `scripts/`: harvest → render pages → transcribe → compare → build_site (see `docs/DATA_CONTRACTS.md`)
- `site/`: the static page published to GitHub Pages. `?data=fixtures` shows fake test data.
- Tests: `python3 scripts/test_build_site.py`, `bash scripts/test_integration.sh`
