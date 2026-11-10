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
| model | str | exact Ollama tag, e.g. `qwen3-vl:32b`; for `omlx` the Hugging Face repo ID, e.g. `mlx-community/Qwen3-VL-32B-Instruct-4bit`; `pdf-text-layer` for the PDF's embedded OCR |
| model_digest | str | from `ollama show`/`/api/tags`; for `omlx` the Hugging Face commit SHA of the downloaded weights (`--model-digest`); `n/a` for the text layer |
| backend | str | `ollama` \| `pdftotext` \| `vllm` \| `omlx` |
| prompt_sha256 | str | sha256 of the exact prompt text |
| params | object | must include `temperature` and `num_ctx` (set explicitly, never the default). Ollama runs also record `num_predict`, `think` (false) and `keep_alive` (-1) as sent; records written before these were added lack `think`/`keep_alive`. `omlx` runs record `max_tokens` (4096), `concurrency` (in-flight requests) and `num_ctx` = the model's `max_position_embeddings` (262144 for Qwen3-VL-32B): oMLX has no per-request context setting and applies no smaller cap, so this is the maximum context, not a value sent. An omlx page whose response carries reasoning (`reasoning_content` / `<think>`) or hits `max_tokens` is a failure record |
| image_dpi | int | |
| text | str | the model's transcription of that page |
| seconds | float | wall-clock for that page's request. Under `omlx` concurrency requests overlap, so per-page seconds are inflated and do not compare directly with sequential runs |
| output_tokens | int \| null | from the backend response if reported |
| created_at | str | |

**Failure records.** When a page fails after the one allowed retry, `transcribe.py` writes
`transcriptions/<post_id>/<model_slug>/fail_NNN.json` = `{post_id: int, page: int, model: str, reason: str, created_at: str}`
(named `fail_*` so `page_*.json` globs skip it). A recorded failure is never silently re-run; delete the file to retry.

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
                               "excerpt": str,     # public: only the model words aligned with archive_excerpt, <= 300 chars
                               "excerpt_unaligned": bool,            # optional; present (true) only on the public file
                               "failed_pages": int,                  # optional, default 0
                               "failures": [ {"post_id", "page", "reason"} ]   # optional, default []
                             } ] } ] }
```
Fixture builds additionally carry an optional top-level `"fixture": true` (absent in real output).
CER/WER are error rates **against the archive's human transcription, which is the ground truth** (per Louis).
Normalization: NFKC, unify quotes and dashes,
collapse whitespace, case kept. Also report a lowercase/no-punctuation variant.
**Headline metric:** the site's table and bars show `cer_lower_nopunct` / `wer_lower_nopunct` ("ignoring capitals
and punctuation"), because transcribers normalize case and punctuation by convention (e.g. an all-caps typed letter
transcribed in sentence case). `cer` / `wer` (case and punctuation kept) are shown as the secondary "exact" columns.
**Public model excerpts (privacy rule, 2026-11-10).** The archive's human transcripts deliberately omit authors' prison ID
numbers and mailing addresses, but raw model output reproduces them. A model's public `excerpt` is therefore NEVER a prefix
of its raw output: it is the span of the output that aligns with the published `archive_excerpt` (`compare.public_model_excerpt`,
which reuses the `best_window` word alignment behind `ref_word_span`). Inside that span only words that equal, or closely
substitute (similarity >= 0.7), an archive word are kept; words with no archive counterpart (headers, addresses) are
withheld, as is any word that would introduce an identifier pattern (`scripts/idpatterns.py`) absent from the archive's own
text. Metrics (`cer`, `wer`, ...) are computed on the full output and are unchanged; only the public excerpt text differs
(the local file keeps the full output). If the output cannot be aligned (window similarity < 0.6, or fewer than 60% of the
archive words matched) the public `excerpt` is `""` and the row carries `"excerpt_unaligned": true`; `compare.py` prints
the count and lists every such row (`excerpt_unaligned: post <id> <model>`). `excerpt_unaligned` is optional (absent = false),
only ever `true`, and requires `excerpt == ""` and `failed_pages == 0` (validators enforce this).
Group statistics on the site: mean and median over letters; a model's completely empty output (e.g. a PDF with no
text layer) is annotated from the data (`excerpt` empty, `failed_pages == 0` and no `excerpt_unaligned`), not hardcoded. An empty excerpt
WITH `excerpt_unaligned` means "excerpt withheld", not "no text layer"; the page shows a note instead of a diff.

**Failed pages.** If transcription of a page FAILED (a failure record exists, see §3), the (letter, model) row has
`cer`, `wer`, `cer_lower_nopunct`, `wer_lower_nopunct` = `null`, `failed_pages` = number of failed pages and `failures`
listing each. Rates are null **exactly when** `failed_pages > 0` (validators enforce this). Such letters are excluded
from that model's group means; the printed table and the site show "failed on N pages". A page with neither a
transcription nor a failure record is a hard error in `compare.py`.

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

### `page_boxes.jsonl` (local only, never published), written by `scripts/ocr_boxes.swift`
Line and word boxes of the page scans, from Apple's Vision framework (`VNRecognizeTextRequest`, `.accurate`, language
correction off, run locally: no cloud service, no LLM). Used only to locate text on the page, never as a transcription.
Vision silently drops lines when run on a whole page (136440 p1: 9 of ~25), so each page is read once whole and again as
overlapping full-width strips (`regionOfInterest`; band 500 px, step 350 px, constants in the script). Candidates from all
passes are merged: two lines are the same if their boxes have IoU >= 0.5, or the smaller box is >= 70% inside the other and
the texts are similar. Of a duplicate group, a line not cut by an interior strip edge wins (a box within 3 px of one is
"cut"), then the longer text, then higher confidence; a cut line survives only if nothing else matches it. Words travel
with their line. `engine` records the strip parameters.
Run: `swift scripts/ocr_boxes.swift data/pages enrich/page_boxes.jsonl <comma-separated post_ids>`. One row per page:
```
{ "post_id": int, "page": int (1-based), "width": int, "height": int,   # pixels of data/pages/<post_id>/page_NNN.png
  "engine": str, "created_at": str,
  "lines": [ { "text": str, "confidence": float 0..1,
               "box": [x0, y0, x1, y1],                                # source-page pixels, origin TOP-left, x0<x1, y0<y1,
                                                                       # clamped to the page; lines sorted top to bottom
               "words": [ {"text": str, "box": [x0, y0, x1, y1]} ] } ] }
```
All keys are required; unknown keys, wrong types or a box outside the page fail the build (`crops.load_page_boxes`).
A page with zero recognized lines is valid (and simply yields no crops). Vision cannot read most handwriting, so such
pages have few or garbled lines; their snippets then fall below the alignment threshold and are counted unaligned.

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
- **The public build is run with `--snippet-chars 100`** (decision of 2026-11-10; the 160 default applies only to
  tests and fixtures): `python3 scripts/build_site.py --local --snippet-chars 100`, then
  `node scripts/validate_built.mjs site/data public`.
- **Text redaction and ID-pattern guard** (`build_site.redact_public`, `build_site.guard_id_patterns`; before anything is
  written). Louis approved blacking out names, ID numbers and addresses (2026-11-10), even where the archive's own text
  contains them. Every public text field (essay excerpts, chunk snippets, demo `archive_excerpt` and each model `excerpt`)
  has each span matching `idpatterns.ID_PATTERNS` (prison/cell IDs incl. `H-45771` and OCR variants such as `H\u20144577l`,
  `#` numbers, 6+ digit numbers, `P.O. Box`, `Box <n|letter>`, `City, ST ZIP`, with dash variants tolerated) replaced by
  `\u2588\u2588\u2588`; the number of replaced spans is `build_info.counts.text_redactions` (replaces
  `id_pattern_hits_from_archive`). Exposure and crop alignment are computed on the unredacted text, as before. The guard then
  scans every public text field (titles and entity texts too) and FAILS the build, naming field and match, on any hit left:
  after redaction there must be 0. The `author` metadata field is APWA's own catalogue field and is left as is.
  `compare.py`'s public file is an intermediate: only the build output is redacted.
- **Scan crops** (`crops.json`, `site/data/crops/*.jpg`; rules below): each public snippet (chunk snippet, essay opening
  excerpt, archive excerpt in the transcription demo) may carry a crop of the original page scan.
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

### `crops.json` rows (crops of the original page scans)
| Field | Type | Required | Notes |
|---|---|---|---|
| target | str | yes | `chunk` (a `chunks.json` snippet), `essay` (an `essays.json` excerpt) or `demo` (a `transcription_demo.json` `archive_excerpt`) |
| id | str | yes | the `chunk_id` for `chunk`; the `post_id` as a string for `essay` and `demo`. Unique per (target, id) |
| post_id | int | yes | must exist in essays.json (equals the chunk's post_id for `chunk`) |
| page | int | yes | 1-based page of the letter; the PDF link is `pdf_url#page=<page>` |
| file | str | yes | `crops/<post_id>_p<page>_<n>.jpg`, relative to the data directory; the file must exist. Snippets whose region is identical share one file |
| box | [int x4] | yes | `[x0, y0, x1, y1]` of the crop in **source-page pixels** (200 dpi page, origin top-left); always full page width |
| similarity | float | yes | alignment score 0..1, always >= `build_info.crop_alignment.threshold` |
| masked | [[int x4]] | yes | black boxes `[x0, y0, x1, y1]` painted into the JPEG, in **source-page pixels**, clipped to `box` (x0<x1, y0<y1); `[]` when nothing was masked. Added 2026-11-10 |

Envelope `{fixture, generated_at, crops: [...]}`; zero rows is allowed. **A snippet with no row has no crop** ("no
matching scan region" on the page): there is no "null" crop and never a best guess.

Crop rules (implemented in `scripts/crops.py`, called by `build_site.py`):
- *Alignment.* The snippet and each page's OCR text (`page_boxes.jsonl`) are normalized with `compare.norm_lower_nopunct`
  (NFKC, quotes/dashes unified, lowercase, punctuation removed, whitespace collapsed). Lines are joined by spaces and
  rapidfuzz `partial_ratio_alignment` finds the best stretch of the page (character-level Indel similarity, 0..1);
  the lines that stretch touches are the matched lines. A match never spans pages. Only pages whose share of the letter's
  OCR text, widened by 0.15, contains the snippet's start offset / transcript length are searched.
- *Threshold* `SIM_THRESHOLD = 0.60` (`--sim-threshold`). Tuned on the 20 demo letters: unrelated letters' pages give
  at most 0.535 (153 samples), so 0.60 clears chance matches; the real score distribution is continuous, not bimodal, so
  the 0.6-0.7 matches are the ones to check by eye. Below the threshold: no crop, counted.
- *Box.* Union of the matched lines' boxes, **tight**: no padding above the first matched line and `BOTTOM_PAD_PX` (3 px)
  below the last (the line above a snippet is often a header the archive omitted), clamped to the page, **full page
  width**. Downscaled to <= 900 px wide, JPEG quality 70.
- *Outside lines.* A page line outside the snippet's matched window whose box overlaps the crop by more than
  `OUTSIDE_TOL` (25%) of its height, and that is not itself blacked out, DROPS the crop (no crop; counted in
  `crop_alignment.unaligned_list`, reason "crop would show N line(s) outside the snippet's matched window").
- *Masking* (`crops.find_masks`, painted solid black before downscaling, every box recorded in `masked`; the backstop,
  tight cropping is the main defence because Vision misreads handwriting). Applied to every Vision line overlapping the
  crop: `person` = a word (whole line if words do not localise it) fuzzily (ratio >= 80, or the name glued in a word)
  matching a name token of a verified `person` entity of that letter or the essay's `author` field, extended over the
  whole name (adjacent capitalized words/initials, particles such as Abu/bin between them; everything after "By" in a
  byline; all words of a short line in the top 30%); deities/religious names (`crops.RELIGIOUS`: Yashua, Yahweh, Elohim,
  Ruah, Jesus, God, Christ, Savior, Messiah, Ha'Mashiak, Allah, Mohammed/Muhammad) are never masked and the build counts
  the entity texts skipped; `id_pattern` = a whole line that has a standalone 5-digit/ZIP+4 token or "City, ST", or that
  matches `idpatterns.ID_PATTERNS` (`#\s*\w*\d{4,}`, `[A-Z]{1,2}-?\d{4,}`, cell numbers like `A2-206`, 6+ digit
  numbers, `P.O. Box`, `Box <n>`, `Box <letter>`, state abbreviation + ZIP) or a "Facility"/"Correctional Facility" line in
  the top 30% of the page; `header_block` = the line directly above and below an `id_pattern` line when no blank gap
  (< 1 median line height) separates them, and further neighbours chained through short lines (<= 6 words) in the top 35% of the page. Header-block growth and the short-line person rule never absorb a line that fuzzily occurs in the archive transcript (`partial_ratio` >= 0.6, same normalization; lines under 6 normalized characters cannot be judged): body text and titles are in the transcript, name/ID/address headers are not. `build_site.py` prints the mask counts by reason.
- *Image exposure cap.* For each letter, the union area of its crops / the total area of all its pages must be
  <= `MAX_IMAGE_EXPOSURE` (default 0.20, `--max-image-exposure`). Over the cap the build fails (no files written) and names the
  letters. The cap is not to be raised to make a build pass.
- *Stale files.* `build_site.py` removes `site/data/crops/*.jpg` not referenced by the new `crops.json` and fails if any other file is there.

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
| counts | {essays, chunks, entities_public, entities_unverified_dropped, entities_person_dropped, themes, demo_letters, crops, crops_unaligned, text_redactions: int} | yes | essays, chunks, entities_public, themes, demo_letters and crops must equal the actual row counts; `crops_unaligned` = public snippets (chunk + essay + demo) with no crop; `text_redactions` = identifier/address spans replaced by the redaction mark in public text (see guard) |
| snippet_chars | int | yes | |
| max_exposure | float | yes | |
| exposure | {fraction: float, percent: float, chars_published: int, chars_total: int} | yes | fraction <= max_exposure |
| crop_alignment | {threshold: float, aligned: {chunk, essay, demo: int}, unaligned: {chunk, essay, demo: int}, similarity_min: float, similarity_median: float, unaligned_list: [{target, id, reason: str}]} | yes | similarity stats are over aligned crops (0.0 if none); `unaligned_list` has one row per snippet without a crop (reason includes its best similarity, or "no page boxes for this letter"); its length equals `counts.crops_unaligned` |
| image_exposure | {cap: float, overall: float, max: float, mean: float, per_letter: [{post_id: int, fraction: float, crops: int}]} | yes | fractions = cropped page area / total page area; `overall` is over all letters with page boxes, `max`/`mean` over letters; `max` <= `cap` (= MAX_IMAGE_EXPOSURE) |

`scripts/test_integration.sh` builds fixture inputs, runs `build_site.py` and validates every output file (and the
hand-made files in `site/data/fixtures/`, and compare.py's public output if present) with `site/validate.js`.
