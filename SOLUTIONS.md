# SOLUTIONS.md

Bug-fix log (format per AGENTS.md).

[2026-11-10] - Vision dropped lines on full pages, so human-transcription excerpts showed "no matching scan region"
Problem
The 136440 demo/essay excerpts ("THE COURT ROOM IS A BATTLE FIELD ... Until lions write books ... The recent media coverage")
were unaligned although the text is plainly on the scan.
Root Cause
Apple Vision VNRecognizeTextRequest run on the whole page silently drops lines (136440 p1: 9 lines of ~25; it missed the quote
and the first paragraph). Revision 3, minimumTextHeight 0 and language correction did not help. The alignment then had no OCR
text to match against.
Solution
scripts/ocr_boxes.swift now also runs Vision over overlapping full-width strips (band 500 px, step 350 px, regionOfInterest;
boxes mapped from ROI-relative to page pixels, checked by drawing them on 136440 p1) and merges the whole-page and strip lines
by IoU / containment + text similarity, preferring lines not cut by a strip edge. The whole page stays a candidate source
because strips alone lost a few lines the full-page pass had found. Contract section 5 updated; engine string records parameters.
Notes
Lines over the 36 pages 843 -> 1076. Vision still skips some lines even in strips (e.g. mid-paragraph lines on 136440 p1), so
alignment counts remain floors. Crops are regenerated with masking applied to all of them.

[2026-11-10] - Public model excerpts and scan crops exposed prison IDs, addresses and a victim's name the archive omitted
Problem
The archive's human transcripts deliberately omit authors' prison ID numbers and mailing addresses, but the live public
site data contained them: model excerpts in transcription_demo.json (e.g. H-45771, 1969037, "Tabor City") and scan crops
(137424_p1_0, 137559_p1_0, 137829_p1_0/_2, 138462_p1_0, 138720_p1_3, 153341_p1_0; 136995_p3_3 reached a line naming an
alleged sexual-assault victim).
Root Cause
compare.py published the first 200 chars of each model's raw output, and raw output starts with the page header the archive
chose not to publish. crops.py padded every crop by one line height above and below the matched lines, so the header (or
the neighbouring name line) fell inside the crop. Nothing checked public text for identifier patterns.
Solution
(1) compare.public_model_excerpt publishes only the model words aligned with the published archive_excerpt (best_window +
word-level Levenshtein; unmatched words withheld); unalignable output becomes "" with optional `excerpt_unaligned: true`,
counted and listed. (2) crops are tight (no padding above, 3 px below), dropped when a non-matched, unmasked line overlaps
them, and Vision lines matching ID/address patterns, person names (verified person entities + author, fuzzy) and their
header block are painted black; boxes recorded in crops.json `masked`. (3) build_site.guard_id_patterns fails the build on
any ID/address pattern in a public text field that is not verbatim in the archive's own text, and counts the allowed ones
in build_info.counts.id_pattern_hits_from_archive. Tests: test_compare.py, test_build_site.py (guard, mask, outside-line drop).
Notes
Pattern masking and the guard are backstops (Vision misreads handwriting); alignment and tight cropping are the defences.
Update (same day, Louis approved): everything matching ID/address patterns is also replaced by a redaction mark in public text even when the archive's own text contains it (build_site.redact_public; build_info.counts.text_redactions; the guard now requires 0 hits). Crop masks also cover whole names, address lines (ZIP, City, ST) and skip deities. Original finding: for 137559 and 137829 the archive's OWN transcript contains "Box E—22600", "H—4577l", "A2-206", "P.O. Box 4430" (dashes
and OCR typos differ from the model spelling, hence exact-string greps missed them); those stay public via archive_excerpt, essay
excerpts and chunk snippets, which this change does not alter. Contract changes: DATA_CONTRACTS.md section 4 (excerpt_unaligned),
section 6 (crops `masked`, tight box, outside-line drop, build_info count, guard). Already-pushed history needs a separate scrub.

[2026-11-09] - compare.py --models rejected real model tags ("no page files")
Problem
`compare.py --models richardyoung/olmocr2:7b-q8 --post-ids ...` failed with "no page files" although
transcriptions/<id>/richardyoung_olmocr2_7b-q8/ existed. The same happened for `qwen3-vl:8b-instruct-q8_0`.
Root Cause
`--models` was documented and used as directory slugs, but callers (and the contract's `model` field) use Ollama tags.
The tag was used verbatim as a directory name, and tags containing `:` or `/` never match the slug directory
(`model_slug` replaces both with `_`). `pdf-text-layer` worked only because its tag equals its slug.
Solution
compare.py maps every `--models` value through `pl_common.model_slug`. Regression test: scripts/test_compare.py
(uses the real tag `richardyoung/olmocr2:7b-q8`; it fails with the old code).
Notes
Related design change, same commit: transcribe.py now records a page that failed after its one retry as
`fail_NNN.json`; compare.py treats that as a counted result (null rates, `failed_pages`, `failures`) instead of
aborting, while a page with no transcription and no failure record remains a hard error. Contract §3/§4,
build_site.py and site/validate.js updated together. The one existing failure (136842 p1, qwen3-vl:8b-instruct-q8_0,
num_predict loop) was backfilled from the run log q8.log.

[2026-11-09] - qwen3-vl:32b ran in thinking mode (slow, ~3000 reasoning tokens per page)
Problem
qwen3-vl:32b took ~25 min for 3 of 36 pages; one page used 2928 output tokens for 3708 chars (the 8b model uses ~350 tokens
for 1600 chars). Letters 136842 and 140883 produced no page file and no failure record.
Root Cause
`ollama show qwen3-vl:32b` reports renderer/parser `qwen3-vl-thinking` with thinking on by default. transcribe.py never
sent `think`, so every call generated a reasoning trace first (also eating the num_predict budget). The two missing
letters were skipped by the pre-fix process (started before failure records existed; its log was empty because stdout was
killed unflushed), so the reason is inferred (timeout or num_predict hit), not recorded.
Solution
transcribe.py sends `"think": false` on every Ollama call and records `think: false` in `params`. The 3 thinking-mode pages
were moved (not deleted) to data/archive_32b_thinking/<post_id>/ so they are out of scoring.
Notes
Other models were run before this change without `think`; their templates do not think (8b instruct, olmocr2), so their output is
unaffected, but their records lack `params.think`. Extra `params` keys are allowed by contract §3.
Follow-up (same fix): every print in transcribe.py is flushed (flush=True) and the restarted 32b run uses `python3 -u`,
so the log is live even if the process is killed. Any page that fails after its one retry, including timeouts and
connection errors, gets a fail_NNN.json (written in the retry loop's else-branch, covering every exception), so no page
can be skipped silently. Failures from before this change (136842, 140883 on 32b) have no record and cannot be
reconstructed; the 32b rerun will produce either a page or a record for each.

[2026-11-10] - Misread handwritten name escaped the crop mask
Problem
After the strip-OCR change, crop 144627_p1_0/_2 showed "My name is Frederick Mason" unmasked.
Root Cause
Vision re-read the name as "Freckerck Maxs"; the person-name match needed fuzz.ratio >= 80 and "freckerck" vs "frederick" scores 78.
Solution
crops._word_is_name also accepts a same-first-letter match at NAME_LOOSE_RATIO (70) for tokens of 5+ chars; the name span then extends over the adjacent capitalized word. Over-masking is the safe direction for names.
Notes
Regression test: scripts/test_crops_names.py. Fuzzy name matching on handwriting OCR stays a backstop; the manager's by-eye crop review before every push remains required.

[2026-11-10] - Most "no matching scan region" notes were found snippets rejected by an over-strict rule
Problem
27 of 35 crop-less snippets had been located on the page but were dropped because a neighbouring line overlapped the crop.
Root Cause
crops.py dropped any crop showing a line outside the snippet's matched window, including lines that are simply the letter's own adjacent text (slanted handwriting, newspaper columns).
Solution
An outside line no longer blocks a crop when it is the letter's own archive text (in_archive); it still passes through the name/ID/address masks. Unexplained outside text still blocks. Aligned snippets 56 -> 81 of 91.
Notes
The remaining 10 are genuine OCR misses (Vision did not read those lines), reported as low similarity.
