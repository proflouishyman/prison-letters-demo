// Strict validators for every public data file (contract: docs/DATA_CONTRACTS.md section 6).
// Plain ES module with no DOM access, so the browser (app.js) and node (scripts/test_integration.sh) share it.
// No coercion, no defaults: a missing required field, a wrong type or an unexpected field throws DataError.

export const EMBED_MODEL = "Xenova/bge-small-en-v1.5";
export const EMBED_DIM = 384;
export const ENTITY_TYPES = ["facility", "place", "organization", "law_policy", "condition", "program", "event", "cultural_work"];
export const AFFECTS = ["fear", "grief", "hope", "faith", "humour", "anger", "love", "resilience", "isolation", "dignity"];
export const SENTIMENTS = ["negative", "mixed", "neutral", "positive"];
export const STANCES = ["describing", "arguing", "testifying", "reflecting", "creative"];
export const HANDWRITING = ["handwritten", "typed", "mixed"];
export const PUBLIC_EXCERPT_MAX = 300;
export const CROP_TARGETS = ["chunk", "essay", "demo"];
export const CROP_MAX_WIDTH = 900;

/** Thrown for any missing/malformed data; carries the file and field for the red error panel. */
export class DataError extends Error {
  constructor(file, field, msg) { super(`${file}: ${field}: ${msg}`); this.file = file; this.field = field; this.detail = msg; }
}

/** Primitive checks; each throws DataError(file, path, ...) on mismatch and returns the value. */
export const V = {
  obj: (f, p, v) => { if (v === null || typeof v !== "object" || Array.isArray(v)) throw new DataError(f, p, "expected object"); return v; },
  arr: (f, p, v, min = 0) => { if (!Array.isArray(v)) throw new DataError(f, p, "expected array"); if (v.length < min) throw new DataError(f, p, `expected at least ${min} item(s), got ${v.length}`); return v; },
  str: (f, p, v) => { if (typeof v !== "string") throw new DataError(f, p, `expected string, got ${v === undefined ? "undefined" : typeof v}`); return v; },
  strOrNull: (f, p, v) => { if (v !== null && typeof v !== "string") throw new DataError(f, p, `expected string or null, got ${v === undefined ? "undefined" : typeof v}`); return v; },
  num: (f, p, v) => { if (typeof v !== "number" || !Number.isFinite(v)) throw new DataError(f, p, `expected finite number, got ${v === undefined ? "undefined" : typeof v}`); return v; },
  int: (f, p, v) => { if (!Number.isInteger(v)) throw new DataError(f, p, `expected integer, got ${v === undefined ? "undefined" : typeof v}`); return v; },
  intOrNull: (f, p, v) => { if (v !== null && !Number.isInteger(v)) throw new DataError(f, p, `expected integer or null, got ${v === undefined ? "undefined" : typeof v}`); return v; },
  bool: (f, p, v) => { if (typeof v !== "boolean") throw new DataError(f, p, `expected boolean, got ${v === undefined ? "undefined" : typeof v}`); return v; },
  oneOf: (f, p, v, allowed) => { if (!allowed.includes(v)) throw new DataError(f, p, `"${v}" not in allowed values [${allowed.join(", ")}]`); return v; },
  strArr: (f, p, v, min = 0) => { V.arr(f, p, v, min).forEach((x, i) => V.str(f, `${p}[${i}]`, x)); return v; },
  intArr: (f, p, v) => { V.arr(f, p, v).forEach((x, i) => V.int(f, `${p}[${i}]`, x)); return v; },
  /** Object must contain every `required` key, may contain `optional` keys, and nothing else. */
  keys: (f, p, o, required, optional = []) => {
    V.obj(f, p, o);
    for (const k of required) if (!(k in o)) throw new DataError(f, `${p}.${k}`.replace(/^\./, ""), "required field is missing");
    const extra = Object.keys(o).filter((k) => !required.includes(k) && !optional.includes(k));
    if (extra.length) throw new DataError(f, p || "(top level)", `unexpected field(s): ${extra.join(", ")}`);
    return o;
  },
};

/** Top-level "fixture" flag: must be true in fixtures mode and must not be true otherwise. */
function checkFixtureFlag(file, d, mode) {
  if (mode === "fixtures" && d.fixture !== true) throw new DataError(file, "fixture", "fixtures mode requires top-level fixture: true");
  if (mode !== "fixtures" && d.fixture === true) throw new DataError(file, "fixture", "fixture data found outside ?data=fixtures; refusing to show it");
}

/** Optional string/int field: absent is fine, but if present it must have the right type (null is not allowed). */
const optStr = (f, p, o, k) => { if (k in o) V.str(f, `${p}.${k}`, o[k]); };
const optInt = (f, p, o, k) => { if (k in o) V.int(f, `${p}.${k}`, o[k]); };

/** Envelope shared by essays/chunks/entities/themes: {fixture, generated_at, <key>: [...]}. */
function envelope(f, d, mode, key, min, extraTop = []) {
  V.keys(f, "", d, ["fixture", "generated_at", key], extraTop);
  V.bool(f, "fixture", d.fixture); V.str(f, "generated_at", d.generated_at); checkFixtureFlag(f, d, mode);
  return V.arr(f, key, d[key], min);
}

/** Per-file validators. Each takes (parsed JSON, mode) and returns the data unchanged or throws. */
export const SCHEMAS = {
  "essays.json": (d, mode) => {
    const f = "essays.json";
    envelope(f, d, mode, "essays", 1).forEach((e, i) => {
      const p = `essays[${i}]`;
      V.keys(f, p, e, ["post_id", "url", "slug", "title", "subjects", "transcript_chars", "excerpt"],
        ["author", "state", "date_text", "year", "genre", "extent_pages", "language", "pdf_url"]);
      V.int(f, p + ".post_id", e.post_id); V.str(f, p + ".url", e.url); V.str(f, p + ".slug", e.slug); V.str(f, p + ".title", e.title);
      V.strArr(f, p + ".subjects", e.subjects); V.int(f, p + ".transcript_chars", e.transcript_chars); V.strOrNull(f, p + ".excerpt", e.excerpt);
      for (const k of ["author", "state", "date_text", "genre", "language", "pdf_url"]) optStr(f, p, e, k);
      for (const k of ["year", "extent_pages"]) optInt(f, p, e, k);
      if (!e.url.startsWith("https://prisonwitness.org/")) throw new DataError(f, p + ".url", "must link back to https://prisonwitness.org/");
    });
    return d;
  },
  "chunks.json": (d, mode) => {
    const f = "chunks.json";
    envelope(f, d, mode, "chunks", 1).forEach((c, i) => {
      const p = `chunks[${i}]`; V.keys(f, p, c, ["chunk_id", "post_id", "snippet"]);
      V.str(f, p + ".chunk_id", c.chunk_id); V.int(f, p + ".post_id", c.post_id); V.str(f, p + ".snippet", c.snippet);
    });
    return d;
  },
  // Crops of the original page scans (contract section 6). No crop row for a snippet = "no matching scan region".
  "crops.json": (d, mode) => {
    const f = "crops.json";
    envelope(f, d, mode, "crops", 0).forEach((c, i) => {
      const p = `crops[${i}]`; V.keys(f, p, c, ["target", "id", "post_id", "page", "file", "box", "similarity", "masked"]);
      V.oneOf(f, p + ".target", c.target, CROP_TARGETS); V.str(f, p + ".id", c.id);
      V.int(f, p + ".post_id", c.post_id); V.int(f, p + ".page", c.page); if (c.page < 1) throw new DataError(f, p + ".page", "must be >= 1");
      V.str(f, p + ".file", c.file);
      if (!/^crops\/[0-9]+_p[0-9]+_[0-9]+\.jpg$/.test(c.file)) throw new DataError(f, p + ".file", "must look like crops/<post_id>_p<page>_<n>.jpg");
      V.intArr(f, p + ".box", c.box);
      if (c.box.length !== 4 || c.box.some((v) => v < 0) || !(c.box[0] < c.box[2]) || !(c.box[1] < c.box[3])) throw new DataError(f, p + ".box", "expected [x0,y0,x1,y1] with x0<x1 and y0<y1");
      V.arr(f, p + ".masked", c.masked).forEach((mb, k) => {
        V.intArr(f, `${p}.masked[${k}]`, mb);
        if (mb.length !== 4 || mb.some((v) => v < 0) || !(mb[0] < mb[2]) || !(mb[1] < mb[3])) throw new DataError(f, `${p}.masked[${k}]`, "expected [x0,y0,x1,y1] with x0<x1 and y0<y1");
      });
      V.num(f, p + ".similarity", c.similarity); if (c.similarity < 0 || c.similarity > 1) throw new DataError(f, p + ".similarity", "must be within 0..1");
    });
    return d;
  },
  "vectors.meta.json": (d, mode) => {
    const f = "vectors.meta.json";
    V.keys(f, "", d, ["fixture", "model", "dim", "n_rows", "dtype", "normalized", "scales", "max_reconstruction_error", "row_order"]);
    V.bool(f, "fixture", d.fixture); checkFixtureFlag(f, d, mode);
    V.str(f, "model", d.model);
    if (d.model !== EMBED_MODEL) throw new DataError(f, "model", `must be ${EMBED_MODEL} to match the in-browser query model, got ${d.model}`);
    V.int(f, "dim", d.dim);
    if (d.dim !== EMBED_DIM) throw new DataError(f, "dim", `must be ${EMBED_DIM}, got ${d.dim}`);
    V.int(f, "n_rows", d.n_rows); V.oneOf(f, "dtype", d.dtype, ["int8"]);
    if (d.normalized !== true) throw new DataError(f, "normalized", "must be true");
    V.arr(f, "scales", d.scales).forEach((s, i) => { V.num(f, `scales[${i}]`, s); if (!(s > 0)) throw new DataError(f, `scales[${i}]`, "must be > 0"); });
    if (d.scales.length !== d.n_rows) throw new DataError(f, "scales", `length ${d.scales.length} != n_rows ${d.n_rows}`);
    V.num(f, "max_reconstruction_error", d.max_reconstruction_error); V.oneOf(f, "row_order", d.row_order, ["chunks.json"]);
    return d;
  },
  "entities.json": (d, mode) => {
    const f = "entities.json";
    envelope(f, d, mode, "entities", 0).forEach((e, i) => {
      const p = `entities[${i}]`; V.keys(f, p, e, ["post_id", "type", "text"]);
      V.int(f, p + ".post_id", e.post_id); V.str(f, p + ".text", e.text);
      if (e.type === "person") throw new DataError(f, p + ".type", "person entities must never appear in public data");
      V.oneOf(f, p + ".type", e.type, ENTITY_TYPES);
    });
    return d;
  },
  "themes.json": (d, mode) => {
    const f = "themes.json";
    envelope(f, d, mode, "themes", 0).forEach((t, i) => {
      const p = `themes[${i}]`; V.keys(f, p, t, ["post_id", "subjects", "affect", "sentiment", "stance", "model"]);
      V.int(f, p + ".post_id", t.post_id); V.strArr(f, p + ".subjects", t.subjects);
      V.strArr(f, p + ".affect", t.affect); t.affect.forEach((a, j) => V.oneOf(f, `${p}.affect[${j}]`, a, AFFECTS));
      V.oneOf(f, p + ".sentiment", t.sentiment, SENTIMENTS); V.oneOf(f, p + ".stance", t.stance, STANCES); V.str(f, p + ".model", t.model);
    });
    return d;
  },
  // Shape is exactly what scripts/compare.py writes (it has no top-level "fixture" outside fixture builds).
  "transcription_demo.json": (d, mode) => {
    const f = "transcription_demo.json";
    V.keys(f, "", d, ["generated_at", "normalization", "letters"], ["fixture"]);
    if ("fixture" in d) V.bool(f, "fixture", d.fixture);
    checkFixtureFlag(f, d, mode);
    V.str(f, "generated_at", d.generated_at); V.str(f, "normalization", d.normalization);
    V.arr(f, "letters", d.letters, 1).forEach((l, i) => {
      const p = `letters[${i}]`;
      V.keys(f, p, l, ["post_id", "url", "title", "state", "year", "pages", "handwriting", "archive_excerpt", "models"]);
      V.int(f, p + ".post_id", l.post_id); V.str(f, p + ".url", l.url); V.str(f, p + ".title", l.title);
      V.strOrNull(f, p + ".state", l.state); V.intOrNull(f, p + ".year", l.year); V.int(f, p + ".pages", l.pages);
      V.oneOf(f, p + ".handwriting", l.handwriting, HANDWRITING); V.str(f, p + ".archive_excerpt", l.archive_excerpt);
      if (mode !== "local" && l.archive_excerpt.length > PUBLIC_EXCERPT_MAX)
        throw new DataError(f, p + ".archive_excerpt", `${l.archive_excerpt.length} chars exceeds the public limit of ${PUBLIC_EXCERPT_MAX}`);
      V.arr(f, p + ".models", l.models, 1).forEach((m, j) => {
        const q = `${p}.models[${j}]`;
        V.keys(f, q, m, ["model", "cer", "wer", "cer_lower_nopunct", "wer_lower_nopunct", "seconds_total", "pages_compared", "partial", "ref_word_span", "excerpt"], ["failed_pages", "failures", "excerpt_unaligned"]);
        V.str(f, q + ".model", m.model);
        const nfail = "failed_pages" in m ? V.int(f, q + ".failed_pages", m.failed_pages) : 0;
        const fl = "failures" in m ? V.arr(f, q + ".failures", m.failures) : [];
        if (fl.length !== nfail) throw new DataError(f, q + ".failures", `failed_pages=${nfail} but ${fl.length} failures listed`);
        fl.forEach((x, k) => { V.keys(f, `${q}.failures[${k}]`, x, ["post_id", "page", "reason"]); V.int(f, `${q}.failures[${k}].page`, x.page); V.int(f, `${q}.failures[${k}].post_id`, x.post_id); V.str(f, `${q}.failures[${k}].reason`, x.reason); });
        // CER/WER are null exactly when the model failed on a page of this letter.
        for (const k of ["cer", "wer", "cer_lower_nopunct", "wer_lower_nopunct"]) {
          if (nfail > 0) { if (m[k] !== null) throw new DataError(f, `${q}.${k}`, "must be null when failed_pages > 0"); }
          else V.num(f, `${q}.${k}`, m[k]);
        }
        V.num(f, `${q}.seconds_total`, m.seconds_total);
        V.intArr(f, q + ".pages_compared", m.pages_compared); V.bool(f, q + ".partial", m.partial);
        if (m.ref_word_span !== null) { V.intArr(f, q + ".ref_word_span", m.ref_word_span); if (m.ref_word_span.length !== 2) throw new DataError(f, q + ".ref_word_span", "expected [start, end] or null"); }
        V.str(f, q + ".excerpt", m.excerpt);
        if ("excerpt_unaligned" in m) {
          V.bool(f, q + ".excerpt_unaligned", m.excerpt_unaligned);
          if (m.excerpt_unaligned && (m.excerpt !== "" || nfail > 0)) throw new DataError(f, q + ".excerpt_unaligned", "true requires an empty excerpt and failed_pages == 0");
        }
        if (mode !== "local" && m.excerpt.length > PUBLIC_EXCERPT_MAX)
          throw new DataError(f, q + ".excerpt", `${m.excerpt.length} chars exceeds the public limit of ${PUBLIC_EXCERPT_MAX}`);
      });
    });
    return d;
  },
  "build_info.json": (d, mode) => {
    const f = "build_info.json";
    V.keys(f, "", d, ["fixture", "generated_at", "git_sha", "models", "counts", "snippet_chars", "max_exposure", "exposure", "crop_alignment", "image_exposure"]);
    V.bool(f, "fixture", d.fixture); checkFixtureFlag(f, d, mode);
    V.str(f, "generated_at", d.generated_at); V.str(f, "git_sha", d.git_sha);
    V.keys(f, "models", d.models, ["embedding", "entities", "themes", "transcription"]);
    for (const k of Object.keys(d.models)) V.strArr(f, "models." + k, d.models[k]);
    V.keys(f, "counts", d.counts, ["essays", "chunks", "entities_public", "entities_unverified_dropped", "entities_person_dropped", "themes", "demo_letters", "crops", "crops_unaligned", "text_redactions"]);
    for (const k of Object.keys(d.counts)) V.int(f, "counts." + k, d.counts[k]);
    V.int(f, "snippet_chars", d.snippet_chars); V.num(f, "max_exposure", d.max_exposure);
    V.keys(f, "exposure", d.exposure, ["fraction", "percent", "chars_published", "chars_total"]);
    V.num(f, "exposure.fraction", d.exposure.fraction); V.num(f, "exposure.percent", d.exposure.percent);
    V.int(f, "exposure.chars_published", d.exposure.chars_published); V.int(f, "exposure.chars_total", d.exposure.chars_total);
    const ca = d.crop_alignment;
    V.keys(f, "crop_alignment", ca, ["threshold", "aligned", "unaligned", "similarity_min", "similarity_median", "unaligned_list"]);
    V.num(f, "crop_alignment.threshold", ca.threshold); V.num(f, "crop_alignment.similarity_min", ca.similarity_min); V.num(f, "crop_alignment.similarity_median", ca.similarity_median);
    for (const k of ["aligned", "unaligned"]) { V.keys(f, "crop_alignment." + k, ca[k], CROP_TARGETS); for (const t of CROP_TARGETS) V.int(f, `crop_alignment.${k}.${t}`, ca[k][t]); }
    V.arr(f, "crop_alignment.unaligned_list", ca.unaligned_list).forEach((u, i) => {
      const p = `crop_alignment.unaligned_list[${i}]`; V.keys(f, p, u, ["target", "id", "reason"]); V.oneOf(f, p + ".target", u.target, CROP_TARGETS); V.str(f, p + ".id", u.id); V.str(f, p + ".reason", u.reason); });
    const ie = d.image_exposure;
    V.keys(f, "image_exposure", ie, ["cap", "overall", "max", "mean", "per_letter"]);
    for (const k of ["cap", "overall", "max", "mean"]) V.num(f, "image_exposure." + k, ie[k]);
    V.arr(f, "image_exposure.per_letter", ie.per_letter).forEach((x, i) => {
      const p = `image_exposure.per_letter[${i}]`; V.keys(f, p, x, ["post_id", "fraction", "crops"]); V.int(f, p + ".post_id", x.post_id); V.num(f, p + ".fraction", x.fraction); V.int(f, p + ".crops", x.crops); });
    return d;
  },
};

/** Check vectors.i8's byte length against the meta. `buf` is an Int8Array/Uint8Array. */
export function checkVectors(file, buf, meta) {
  if (buf.length !== meta.n_rows * meta.dim) throw new DataError(file, "(length)", `${buf.length} bytes != n_rows*dim = ${meta.n_rows * meta.dim}`);
  return buf;
}

/**
 * Cross-file checks on the parsed documents, keyed by file name ("essays.json" -> parsed doc).
 * Checks ids, row counts against build_info.counts, and snippet/excerpt limits against build_info.snippet_chars.
 */
export function crossCheck(docs) {
  const E = docs["essays.json"].essays, C = docs["chunks.json"].chunks, N = docs["entities.json"].entities;
  const K = docs["crops.json"].crops;
  const T = docs["themes.json"].themes, M = docs["vectors.meta.json"], B = docs["build_info.json"], D = docs["transcription_demo.json"];
  const ids = new Set(E.map((e) => e.post_id));
  if (ids.size !== E.length) throw new DataError("essays.json", "post_id", "duplicate post_id");
  const unknown = (file, list) => { const bad = list.find((x) => !ids.has(x.post_id)); if (bad) throw new DataError(file, "post_id", `${bad.post_id} is not in essays.json`); };
  unknown("chunks.json", C); unknown("entities.json", N); unknown("themes.json", T); unknown("transcription_demo.json", D.letters);
  if (C.length !== M.n_rows) throw new DataError("vectors.meta.json", "n_rows", `${M.n_rows} != chunks.json length ${C.length}`);
  unknown("crops.json", K);
  const chunkIds = new Set(C.map((c) => c.chunk_id)), seenCrop = new Set();
  K.forEach((k, i) => {
    const w = `crops[${i}]`;
    if (k.target === "chunk" ? !chunkIds.has(k.id) : k.id !== String(k.post_id) || !ids.has(k.post_id)) throw new DataError("crops.json", w + ".id", `${k.target} ${k.id} does not exist`);
    if (k.target === "chunk" && C.find((c) => c.chunk_id === k.id).post_id !== k.post_id) throw new DataError("crops.json", w + ".post_id", "differs from the chunk's post_id");
    const key = k.target + ":" + k.id; if (seenCrop.has(key)) throw new DataError("crops.json", w, "duplicate crop for the same snippet"); seenCrop.add(key);
    if (k.similarity < B.crop_alignment.threshold) throw new DataError("crops.json", w + ".similarity", `${k.similarity} is below the alignment threshold ${B.crop_alignment.threshold}`);
  });
  if (B.image_exposure.max > B.image_exposure.cap) throw new DataError("build_info.json", "image_exposure.max", "exceeds image_exposure.cap");
  const wantA = Object.values(B.crop_alignment.aligned).reduce((a, b) => a + b, 0), wantU = Object.values(B.crop_alignment.unaligned).reduce((a, b) => a + b, 0);
  if (wantA !== K.length || wantU !== B.counts.crops_unaligned || B.crop_alignment.unaligned_list.length !== wantU) throw new DataError("build_info.json", "crop_alignment", "aligned/unaligned counts disagree with crops.json or counts.crops_unaligned");
  const want = { crops: K.length, essays: E.length, chunks: C.length, entities_public: N.length, themes: T.length, demo_letters: D.letters.length };
  for (const [k, v] of Object.entries(want)) if (B.counts[k] !== v) throw new DataError("build_info.json", `counts.${k}`, `${B.counts[k]} != actual ${v}`);
  C.forEach((c, i) => { if (c.snippet.length > B.snippet_chars) throw new DataError("chunks.json", `chunks[${i}].snippet`, `${c.snippet.length} chars exceeds snippet_chars ${B.snippet_chars}`); });
  E.forEach((e, i) => { if (e.excerpt !== null && e.excerpt.length > 2 * B.snippet_chars) throw new DataError("essays.json", `essays[${i}].excerpt`, `${e.excerpt.length} chars exceeds 2*snippet_chars`); });
  if (B.exposure.fraction > B.max_exposure) throw new DataError("build_info.json", "exposure.fraction", "exceeds max_exposure");
  if (B.fixture !== docs["essays.json"].fixture) throw new DataError("build_info.json", "fixture", "differs from essays.json fixture flag");
}
