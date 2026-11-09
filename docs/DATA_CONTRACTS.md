# Data contracts

Every stage reads and writes exactly these shapes. Each writer validates on write and each reader on read,
and **a mismatch fails loudly** (non-zero exit, FAILED banner, no coercion). New fields may be added as optional
with a documented default. Renaming or reshaping a field means updating this file and every consumer in the same commit.

Local-only paths are gitignored (full essay text and scans never enter the public repo; see "Public build").
All timestamps are ISO-8601 UTC. `post_id` is the WordPress post ID of the essay on prisonwitness.org.

---

## 1. `db/apwa_mini.sqlite` (local only), written by `scripts/harvest.py`

Table `essays`, one row per essay:
| field | type | required | notes |
|---|---|---|---|
| post_id | INTEGER PK | yes | WP post ID (from the REST API or the page's `postid-NNN` body class) |
| url | TEXT | yes | canonical essay URL |
| slug | TEXT | yes | |
| title | TEXT | yes | |
| author | TEXT | no (NULL = not shown) | as displayed, "Last, First" |
| state | TEXT | no | as displayed |
| date_text | TEXT | no | as displayed, e.g. "circa 2023" |
| year | INTEGER | no | first 4-digit year in date_text, else NULL |
| genre | TEXT | no | e.g. Essay, Poetry |
| extent_pages | INTEGER | no | from "Extent: N pages" |
| subjects | TEXT (JSON array of strings) | yes | `[]` if none shown |
| language | TEXT | no | |
| pdf_url | TEXT | no | absolute URL of the Original PDF |
| transcript | TEXT | no | **NULL = the page has no Transcript block** (not an empty string) |
| transcript_chars | INTEGER | yes | 0 when transcript is NULL |
| fetched_at | TEXT | yes | |
| source | TEXT | yes | constant `prisonwitness.org/html` |
| harvester_version | TEXT | yes | git short SHA or `dev` |

Table `harvest_runs`: run_id, started_at, finished_at, seed, n_requested, n_ok, n_failed, failures (JSON list of {url, error}).

## 2. Page images (local only): `data/pages/<post_id>/page_NNN.png`
Rendered from the PDF with `pdftoppm -r 200 -png`. NNN is 1-based and zero-padded to 3 digits.

## 3. Transcriptions (local only): `transcriptions/<post_id>/<model_slug>/page_NNN.json`
| field | type | notes |
|---|---|---|
| post_id | int | |
| page | int | 1-based |
| model | str | exact Ollama tag, e.g. `qwen3-vl:32b`; `pdf-text-layer` for the PDF's embedded OCR |
| model_digest | str | from `ollama show`/`/api/tags`; `n/a` for the text layer |
| backend | str | `ollama` \| `pdftotext` \| `vllm` |
| prompt_sha256 | str | sha256 of the exact prompt text |
| params | object | must include `temperature` and `num_ctx` (set explicitly, never the default) |
| image_dpi | int | |
| text | str | the model's transcription of that page |
| seconds | float | wall-clock |
| output_tokens | int \| null | from the backend response if reported |
| created_at | str | |

`model_slug` = model tag with `:` and `/` replaced by `_`. **Ollama `:cloud` models are forbidden.** The script must refuse any tag containing `cloud`.

## 4. Comparison: `site/data/local/transcription_demo.json` (full) and `site/data/transcription_demo.json` (public)
Written by `scripts/compare.py`. This is the contract for both the full and the public file; `build_site.py` and
`site/validate.js` reject any deviation (missing or extra fields).
```
{ "generated_at": str, "normalization": str,
  "letters": [ { "post_id": int, "url": str, "title": str, "state": str|null, "year": int|null, "pages": int,
                 "handwriting": "handwritten|typed|mixed",
                 "archive_excerpt": str,            # public: <= 300 chars; local: full text
                 "models": [ { "model": str, "cer": float, "wer": float,
                               "cer_lower_nopunct": float, "wer_lower_nopunct": float,
                               "seconds_total": float, "pages_compared": [int], "partial": bool,
                               "ref_word_span": [int, int] | null,   # only when partial
                               "excerpt": str      # public: <= 300 chars
                             } ] } ] }
```
Fixture builds additionally carry an optional top-level `"fixture": true` (absent in real output).
CER/WER are error rates **against the archive's human transcription, which is the ground truth** (per Louis).
Normalization: NFKC, unify quotes and dashes,
collapse whitespace, case kept. Also report a lowercase/no-punctuation variant.

## 5. Enrichment (local only): `enrich/`
- `chunks.jsonl`: `{chunk_id: "<post_id>-<n>", post_id, n, start, end, text}`. Offsets are into `essays.transcript`.
  Chunks are ~1,200 chars, split at paragraph or sentence boundaries.
- `embeddings.f32`: float32 row-major, one row per chunk in chunks.jsonl order. `embeddings.meta.json`:
  `{model: "Xenova/bge-small-en-v1.5", dim: 384, normalized: true, n_rows}`. The same model runs in the browser
  for queries (transformers.js), so the offline model must match it exactly.
- `entities.jsonl`: `{post_id, type, text, quote, start, end, verified: bool, model, prompt_sha256}`.
  `type` ∈ {facility, place, organization, law_policy, condition, program, event, cultural_work, person}.
  `verified` = the quote occurs in the transcript at [start, end). **Unverified rows are kept in the file but
  never used downstream.** Their count is reported at the top of the run log.
- `themes.jsonl`: `{post_id, subjects: [str from APWA's own subject list], affect: [str ⊂ {fear, grief, hope, faith,
  humour, anger, love, resilience, isolation, dignity}], sentiment: "negative|mixed|neutral|positive",
  stance: "describing|arguing|testifying|reflecting|creative", model, prompt_sha256}`.

## 6. Public build: `site/data/` (committed, published to GitHub Pages)
Built by `scripts/build_site.py --public`. Rules:
- **No full transcripts.** Each chunk carries at most a `SNIPPET_CHARS` (default 160) snippet from its start. Each
  essay carries at most a `SNIPPET_CHARS * 2` opening excerpt. The build prints the total share of transcript
  characters published and **fails if it exceeds `MAX_EXPOSURE` (default 15%)**.
- **No `person` entities** in any public file. The build fails if one appears.
- Every essay links back to its prisonwitness.org URL.
- Files and exact shapes (below). Every JSON file is validated on write by `build_site.py` and on read by
  `site/validate.js` (shared by `site/app.js` and `scripts/test_integration.sh`). Unknown fields, missing required
  fields and wrong types are errors; there are no defaults. "Optional" means the key is absent when the value is
  unknown (never `null`); "nullable" means the key is always present and may be `null`.
- `fixture` (bool) is always written by `build_site.py`. The page accepts `fixture: true` only under `?data=fixtures`
  and refuses it elsewhere.

**Envelope files** (`essays.json`, `chunks.json`, `entities.json`, `themes.json`):
`{fixture: bool, generated_at: str, <key>: [row, ...]}` where `<key>` is the file name without `.json`.
`essays.json` and `chunks.json` need at least 1 row.

### `essays.json` rows
| Field | Type | Required | Notes |
|---|---|---|---|
| post_id | int | yes | unique |
| url | str | yes | must start with `https://prisonwitness.org/` |
| slug | str | yes | |
| title | str | yes | |
| subjects | [str] | yes | archive's own subject list; may be empty |
| transcript_chars | int | yes | length of the full (unpublished) transcript |
| excerpt | str\|null | yes | opening <= 2 x SNIPPET_CHARS; null if no transcript |
| author, state, date_text, genre, language, pdf_url | str | optional | absent when unknown |
| year, extent_pages | int | optional | absent when unknown |

### `chunks.json` rows (row order = row order of `vectors.i8`)
| Field | Type | Required | Notes |
|---|---|---|---|
| chunk_id | str | yes | `<post_id>-<n>` |
| post_id | int | yes | must exist in essays.json |
| snippet | str | yes | <= SNIPPET_CHARS (also recorded as `build_info.snippet_chars`) |

### `vectors.i8` + `vectors.meta.json`
`vectors.i8`: raw int8, row-major, `n_rows * dim` bytes, row i = chunk i of `chunks.json`. Row i dequantizes as
`int8 * scales[i]` (per-row scale = max|x|/127). `vectors.meta.json` (no envelope, no `generated_at`):
| Field | Type | Required | Notes |
|---|---|---|---|
| fixture | bool | yes | |
| model | str | yes | must equal `Xenova/bge-small-en-v1.5` (the in-browser query model) |
| dim | int | yes | must be 384 |
| n_rows | int | yes | equals chunks.json length |
| dtype | str | yes | `"int8"` |
| normalized | bool | yes | must be true |
| scales | [float] | yes | length n_rows, each > 0; scales live here, not in the .i8 file |
| max_reconstruction_error | float | yes | max abs error of dequantized vs original float32 |
| row_order | str | yes | `"chunks.json"` |

### `entities.json` rows (verified only; never `person`)
| Field | Type | Required | Notes |
|---|---|---|---|
| post_id | int | yes | must exist in essays.json |
| type | str | yes | one of facility, place, organization, law_policy, condition, program, event, cultural_work |
| text | str | yes | <= 120 chars; unique per (post_id, type, text) |

### `themes.json` rows
| Field | Type | Required | Notes |
|---|---|---|---|
| post_id | int | yes | must exist in essays.json |
| subjects | [str] | yes | |
| affect | [str] | yes | each in the affect set of section 5 |
| sentiment | str | yes | negative, mixed, neutral, positive |
| stance | str | yes | describing, arguing, testifying, reflecting, creative |
| model | str | yes | |

### `transcription_demo.json`
Exactly the section 4 public shape (written by `compare.py`, copied unchanged by `build_site.py`, which validates it).
Optional top-level `fixture: bool`. Every `post_id` must exist in essays.json.

### `build_info.json` (no envelope; `fixture` and `generated_at` are top-level fields)
| Field | Type | Required | Notes |
|---|---|---|---|
| fixture | bool | yes | |
| generated_at | str | yes | |
| git_sha | str | yes | `dev` before the first commit |
| models | {embedding, entities, themes, transcription: [str]} | yes | all four keys required |
| counts | {essays, chunks, entities_public, entities_unverified_dropped, entities_person_dropped, themes, demo_letters: int} | yes | essays, chunks, entities_public, themes and demo_letters must equal the actual row counts |
| snippet_chars | int | yes | |
| max_exposure | float | yes | |
| exposure | {fraction: float, percent: float, chars_published: int, chars_total: int} | yes | fraction <= max_exposure |

`scripts/test_integration.sh` builds fixture inputs, runs `build_site.py` and validates every output file (and the
hand-made files in `site/data/fixtures/`, and compare.py's public output if present) with `site/validate.js`.
