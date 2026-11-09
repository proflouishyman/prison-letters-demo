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
        V.keys(f, q, m, ["model", "cer", "wer", "cer_lower_nopunct", "wer_lower_nopunct", "seconds_total", "pages_compared", "partial", "ref_word_span", "excerpt"]);
        V.str(f, q + ".model", m.model);
        for (const k of ["cer", "wer", "cer_lower_nopunct", "wer_lower_nopunct", "seconds_total"]) V.num(f, `${q}.${k}`, m[k]);
        V.intArr(f, q + ".pages_compared", m.pages_compared); V.bool(f, q + ".partial", m.partial);
        if (m.ref_word_span !== null) { V.intArr(f, q + ".ref_word_span", m.ref_word_span); if (m.ref_word_span.length !== 2) throw new DataError(f, q + ".ref_word_span", "expected [start, end] or null"); }
        V.str(f, q + ".excerpt", m.excerpt);
        if (mode !== "local" && m.excerpt.length > PUBLIC_EXCERPT_MAX)
          throw new DataError(f, q + ".excerpt", `${m.excerpt.length} chars exceeds the public limit of ${PUBLIC_EXCERPT_MAX}`);
      });
    });
    return d;
  },
  "build_info.json": (d, mode) => {
    const f = "build_info.json";
    V.keys(f, "", d, ["fixture", "generated_at", "git_sha", "models", "counts", "snippet_chars", "max_exposure", "exposure"]);
    V.bool(f, "fixture", d.fixture); checkFixtureFlag(f, d, mode);
    V.str(f, "generated_at", d.generated_at); V.str(f, "git_sha", d.git_sha);
    V.keys(f, "models", d.models, ["embedding", "entities", "themes", "transcription"]);
    for (const k of Object.keys(d.models)) V.strArr(f, "models." + k, d.models[k]);
    V.keys(f, "counts", d.counts, ["essays", "chunks", "entities_public", "entities_unverified_dropped", "entities_person_dropped", "themes", "demo_letters"]);
    for (const k of Object.keys(d.counts)) V.int(f, "counts." + k, d.counts[k]);
    V.int(f, "snippet_chars", d.snippet_chars); V.num(f, "max_exposure", d.max_exposure);
    V.keys(f, "exposure", d.exposure, ["fraction", "percent", "chars_published", "chars_total"]);
    V.num(f, "exposure.fraction", d.exposure.fraction); V.num(f, "exposure.percent", d.exposure.percent);
    V.int(f, "exposure.chars_published", d.exposure.chars_published); V.int(f, "exposure.chars_total", d.exposure.chars_total);
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
  const T = docs["themes.json"].themes, M = docs["vectors.meta.json"], B = docs["build_info.json"], D = docs["transcription_demo.json"];
  const ids = new Set(E.map((e) => e.post_id));
  if (ids.size !== E.length) throw new DataError("essays.json", "post_id", "duplicate post_id");
  const unknown = (file, list) => { const bad = list.find((x) => !ids.has(x.post_id)); if (bad) throw new DataError(file, "post_id", `${bad.post_id} is not in essays.json`); };
  unknown("chunks.json", C); unknown("entities.json", N); unknown("themes.json", T); unknown("transcription_demo.json", D.letters);
  if (C.length !== M.n_rows) throw new DataError("vectors.meta.json", "n_rows", `${M.n_rows} != chunks.json length ${C.length}`);
  const want = { essays: E.length, chunks: C.length, entities_public: N.length, themes: T.length, demo_letters: D.letters.length };
  for (const [k, v] of Object.entries(want)) if (B.counts[k] !== v) throw new DataError("build_info.json", `counts.${k}`, `${B.counts[k]} != actual ${v}`);
  C.forEach((c, i) => { if (c.snippet.length > B.snippet_chars) throw new DataError("chunks.json", `chunks[${i}].snippet`, `${c.snippet.length} chars exceeds snippet_chars ${B.snippet_chars}`); });
  E.forEach((e, i) => { if (e.excerpt !== null && e.excerpt.length > 2 * B.snippet_chars) throw new DataError("essays.json", `essays[${i}].excerpt`, `${e.excerpt.length} chars exceeds 2*snippet_chars`); });
  if (B.exposure.fraction > B.max_exposure) throw new DataError("build_info.json", "exposure.fraction", "exceeds max_exposure");
  if (B.fixture !== docs["essays.json"].fixture) throw new DataError("build_info.json", "fixture", "differs from essays.json fixture flag");
}
