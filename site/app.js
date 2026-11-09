// Prison letters demo frontend. Plain ES module, no build step.
// Layout: (1) data loading (shapes are validated by validate.js), (2) word diff, (3) the three sections, (4) footer, (5) boot.

import { DataError, SCHEMAS, EMBED_MODEL, ENTITY_TYPES, AFFECTS, SENTIMENTS, STANCES, HANDWRITING, PUBLIC_EXCERPT_MAX, checkVectors, crossCheck } from "./validate.js";

const MINISEARCH_URL = "https://cdn.jsdelivr.net/npm/minisearch@6.3.0/dist/es/index.js";
const TRANSFORMERS_URL = "https://cdn.jsdelivr.net/npm/@xenova/transformers@2.17.2/dist/transformers.min.js";
const ARCHIVE_URL = "https://prisonwitness.org/";

/* ============ small DOM helper ============ */

/** Build an element. children may be strings (inserted as text, never HTML), nodes, or arrays. */
function h(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) if (c != null) e.append(c);
  return e;
}

/* ============ 1. DATA LOADING (validation lives in validate.js) ============ */

/** Resolve ?data= into {mode, base}. An unknown value is an error, not a default. */
function resolveMode() {
  const m = new URLSearchParams(location.search).get("data");
  if (m === null) return { mode: "public", base: "data/" };
  if (m === "fixtures") return { mode: "fixtures", base: "data/fixtures/" };
  if (m === "local") return { mode: "local", base: "data/local/" };
  throw new DataError("URL", "?data", `unknown value "${m}" (allowed: fixtures, local, or omit)`);
}

/** Fetch one JSON file and validate it. Throws DataError on HTTP, parse, or shape problems. */
async function loadJson(base, file, mode) {
  let r;
  try { r = await fetch(base + file); } catch (e) { throw new DataError(base + file, "(fetch)", e.message); }
  if (!r.ok) throw new DataError(base + file, "(fetch)", `HTTP ${r.status}`);
  let d;
  try { d = await r.json(); } catch (e) { throw new DataError(base + file, "(parse)", "not valid JSON: " + e.message); }
  try { return SCHEMAS[file](d, mode); } catch (e) { if (e instanceof DataError) e.file = base + file; throw e; }
}

/** Fetch vectors.i8 and check its byte length against the meta. Returns an Int8Array. */
async function loadVectors(base, meta) {
  const file = base + "vectors.i8";
  let r;
  try { r = await fetch(file); } catch (e) { throw new DataError(file, "(fetch)", e.message); }
  if (!r.ok) throw new DataError(file, "(fetch)", `HTTP ${r.status}`);
  const buf = new Int8Array(await r.arrayBuffer());
  return checkVectors(file, buf, meta);
}

/** Load and cross-check everything. Returns {data} or {errors} (every failing file is reported). */
async function loadAll(mode, base) {
  const names = Object.keys(SCHEMAS);
  const settled = await Promise.allSettled(names.map((n) => loadJson(base, n, mode)));
  const errors = settled.filter((s) => s.status === "rejected").map((s) => s.reason);
  if (errors.length) return { errors };
  const data = Object.fromEntries(names.map((n, i) => [n.replace(".json", ""), settled[i].value]));
  try {
    data.vectors = await loadVectors(base, data["vectors.meta"]);
    crossCheck(Object.fromEntries(names.map((n, i) => [n, settled[i].value])));
  } catch (e) { if (e instanceof DataError) return { errors: [e] }; throw e; }
  return { data };
}

/** Render a red error panel naming file and field. */
function errorPanel(title, e) {
  return h("div", { class: "errpanel" }, h("h3", {}, title),
    h("div", {}, "File: ", h("b", {}, e.file || "(unknown)")), h("div", {}, "Field: ", h("b", {}, e.field || "(unknown)")), h("div", {}, e.detail || e.message));
}

/* ============ 2. WORD-LEVEL DIFF ============ */

/**
 * Word diff between the human transcript (a) and a model's text (b), via LCS.
 * Returns ops: {t:"eq",a,b} | {t:"sub",a,b} | {t:"del",a} (human word the model missed) | {t:"ins",b} (extra model word).
 * Adjacent deletions and insertions are paired up as substitutions.
 */
function wordDiff(aText, bText) {
  const a = aText.split(/\s+/).filter(Boolean), b = bText.split(/\s+/).filter(Boolean);
  const dp = Array.from({ length: a.length + 1 }, () => new Int32Array(b.length + 1));
  for (let i = a.length - 1; i >= 0; i--)
    for (let j = b.length - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const ops = []; let i = 0, j = 0, dels = [], inss = [];
  const flush = () => {
    const n = Math.min(dels.length, inss.length);
    for (let k = 0; k < n; k++) ops.push({ t: "sub", a: dels[k], b: inss[k] });
    dels.slice(n).forEach((w) => ops.push({ t: "del", a: w })); inss.slice(n).forEach((w) => ops.push({ t: "ins", b: w }));
    dels = []; inss = [];
  };
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) { flush(); ops.push({ t: "eq", a: a[i], b: b[j] }); i++; j++; }
    else if (j >= b.length || (i < a.length && dp[i + 1][j] >= dp[i][j + 1])) dels.push(a[i++]);
    else inss.push(b[j++]);
  }
  flush();
  return ops;
}

/** Render one side of a diff ("a" = human, "b" = model) as highlighted text. */
function renderDiffSide(ops, side) {
  const out = [];
  for (const o of ops) {
    const w = side === "a" ? o.a : o.b;
    if (w === undefined) continue;
    out.push(o.t === "eq" ? w : h("mark", { class: o.t, title: o.t === "sub" ? "different word" : o.t === "del" ? "missing from the model's version" : "extra in the model's version" }, w), " ");
  }
  return out;
}

/* ============ 3a. SECTION 1: TRANSCRIPTION ACCURACY ============ */

const pct = (x) => (x * 100).toFixed(1) + "%";
const mean = (xs) => xs.reduce((s, x) => s + x, 0) / xs.length;

/** Mean CER/WER for each model within each handwriting group: {model:{group:{cer,wer,n}}}. */
function summarise(letters) {
  const acc = {};
  for (const l of letters) for (const m of l.models) {
    ((acc[m.model] ??= {})[l.handwriting] ??= []).push(m);
  }
  const out = {};
  for (const [model, groups] of Object.entries(acc)) {
    out[model] = {};
    for (const [g, ms] of Object.entries(groups)) out[model][g] = { cer: mean(ms.map((m) => m.cer)), wer: mean(ms.map((m) => m.wer)), n: ms.length };
  }
  return out;
}

/** Build section 1: explanation, summary table, bar chart, per-letter diff view. */
function renderAccuracy(root, demo, mode) {
  const letters = demo.letters, sum = summarise(letters);
  const models = Object.keys(sum);
  const groups = HANDWRITING.filter((g) => letters.some((l) => l.handwriting === g));
  root.append(h("h2", {}, "How well do local AI models transcribe?"),
    h("p", {}, `We gave ${letters.length} letters to AI models running on a local computer (no cloud services) and compared each result with the archive's own human transcription, which we treat as the correct answer. `,
      "The scores are error rates, so lower is better."),
    h("div", { class: "note" }, h("b", {}, "What CER and WER mean. "),
      "CER (character error rate) counts wrong, missing or extra letters. A CER of 5% means about 5 wrong characters in every 100. ",
      "WER (word error rate) does the same for whole words: a WER of 10% means about 10 wrong words in every 100. ",
      "Handwriting is usually harder than typewriting. Rates are averaged over the letters in each group; \"n\" is how many letters that is."));

  const head = h("tr", {}, h("th", {}, "Model"), groups.map((g) => [h("th", {}, `${g}: CER`), h("th", {}, `${g}: WER`)]));
  const rows = models.map((m) => h("tr", {}, h("td", {}, m), groups.map((g) => {
    const s = sum[m][g];
    return s ? [h("td", {}, `${pct(s.cer)} (n=${s.n})`), h("td", {}, pct(s.wer))] : [h("td", {}, "no data"), h("td", {}, "no data")];
  })));
  root.append(h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, head), h("tbody", {}, rows))));

  // Bar chart (CSS bars): average character error per model, one bar per handwriting group.
  const maxCer = Math.max(0.01, ...models.flatMap((m) => groups.map((g) => sum[m][g]?.cer ?? 0)));
  root.append(h("h3", {}, "Average character error rate (shorter bar is better)"),
    h("div", { class: "bars" }, models.map((m) => h("div", { class: "row" }, h("span", {}, m),
      h("div", { class: "bartrack" }, groups.map((g) => {
        const s = sum[m][g];
        return s ? h("div", { class: "bar " + (g === "typed" ? "typed" : ""), style: `width:${(s.cer / maxCer) * 100}%`, title: `${g}: ${pct(s.cer)}` }) : null;
      }))))),
    h("p", { class: "muted" }, "Blue bars: handwritten. Orange bars: typed. Where a group has a third colour or no bar, see the table."));

  root.append(h("h3", {}, "Look at one letter"));
  if (mode !== "local")
    root.append(h("div", { class: "note" }, `Only the first ${PUBLIC_EXCERPT_MAX} characters (an excerpt) of each letter are published here, so the word-by-word comparison below runs on those excerpts only, not on the whole letter. Scores in the table are as recorded in the data file.`));
  const sel = h("select", { "aria-label": "Choose a letter" }, letters.map((l, i) => h("option", { value: i }, `${l.title} (${l.handwriting})`)));
  const view = h("div", {});
  const draw = () => {
    const l = letters[+sel.value];
    view.replaceChildren(
      h("p", { class: "muted" }, `${l.state ?? "state not shown"}, ${l.year ?? "year not shown"}, ${l.pages} page(s). `, h("a", { href: l.url }, "Read the essay on prisonwitness.org")),
      h("p", { class: "muted" }, "Highlights: ", h("mark", { class: "del" }, "word the model missed"), " ", h("mark", { class: "sub" }, "different word"), " ", h("mark", { class: "ins" }, "extra word added by the model")),
      h("div", { class: "box" }, h("h4", {}, "Human transcription (the answer)"), l.archive_excerpt));
    const grid = h("div", { class: "cols two" });
    for (const m of l.models) {
      const ops = wordDiff(l.archive_excerpt, m.excerpt);
      grid.append(h("div", { class: "box" }, h("h4", {}, `${m.model}: CER ${pct(m.cer)}, WER ${pct(m.wer)}`), h("div", {}, renderDiffSide(ops, "b")),
        h("details", {}, h("summary", {}, "Human text with the differences marked"), renderDiffSide(ops, "a"))));
    }
    view.append(grid);
  };
  sel.addEventListener("change", draw); draw();
  root.append(sel, view);
}

/* ============ 3b. SECTION 2: HYBRID SEARCH ============ */

let embedderPromise = null;
/** Start (once) loading the in-browser embedding model; rejects loudly if it cannot load. */
function getEmbedder() {
  embedderPromise ??= (async () => {
    const t = await import(TRANSFORMERS_URL);
    t.env.allowLocalModels = false;
    return t.pipeline("feature-extraction", EMBED_MODEL);
  })();
  return embedderPromise;
}

/** Embed a query (mean pooling, normalised) and return a Float32Array of length dim. */
async function embedQuery(q, dim) {
  const pipe = await getEmbedder();
  const out = await pipe(q, { pooling: "mean", normalize: true });
  if (out.data.length !== dim) throw new Error(`query embedding has ${out.data.length} dims, expected ${dim}`);
  return out.data;
}

/** Dot product of query vector with every int8 row, dequantised by that row's scale. Returns Float32Array of scores. */
function semanticScores(qv, vectors, meta) {
  const { dim, n_rows, scales } = meta, scores = new Float32Array(n_rows);
  for (let r = 0; r < n_rows; r++) {
    let s = 0; const o = r * dim;
    for (let i = 0; i < dim; i++) s += qv[i] * vectors[o + i];
    scores[r] = s * scales[r];
  }
  return scores;
}

/** Reciprocal-rank fusion over ranked lists of post_ids. Returns [{id, score}] best first. */
function rrf(lists, k = 60) {
  const sc = new Map();
  for (const list of lists) list.forEach((id, rank) => sc.set(id, (sc.get(id) ?? 0) + 1 / (k + rank + 1)));
  return [...sc].map(([id, score]) => ({ id, score })).sort((a, b) => b.score - a.score);
}

/** Build section 2 and wire up keyword / semantic / both search. */
async function renderSearch(root, data) {
  const essays = new Map(data.essays.essays.map((e) => [e.post_id, e]));
  const chunks = data.chunks.chunks, byChunk = new Map(chunks.map((c, i) => [c.chunk_id, i]));
  root.append(h("h2", {}, "Search the letters"),
    h("p", {}, "Keyword search finds the exact words you type. Semantic search finds passages with a similar meaning even when the words differ. \"Both\" combines the two lists. Only short snippets of each letter are searched, not the full text."));
  const status = h("div", { class: "note" }, "Loading search tools...");
  const input = h("input", { type: "search", placeholder: "e.g. loneliness, medical care, visiting day", "aria-label": "Search query" });
  const radios = h("div", { class: "radio" }, [["both", "Both"], ["keyword", "Keyword"], ["semantic", "Semantic"]].map(([v, t]) =>
    h("label", {}, h("input", { type: "radio", name: "mode", value: v, ...(v === "both" ? { checked: "" } : {}) }), " " + t)));
  const results = h("div", {});
  root.append(status, input, radios, results);

  // Keyword index (MiniSearch over snippets + essay metadata). Failure is shown, and semantic search can still work.
  let mini = null, miniErr = null;
  try {
    const { default: MiniSearch } = await import(MINISEARCH_URL);
    mini = new MiniSearch({ fields: ["snippet", "title", "author", "state", "year", "subjects"], idField: "id", searchOptions: { prefix: true, fuzzy: 0.2, boost: { title: 2 } } });
    mini.addAll(chunks.map((c) => { const e = essays.get(c.post_id); const th = data.themes.themes.find((t) => t.post_id === c.post_id);
      return { id: c.chunk_id, snippet: c.snippet, title: e.title, author: e.author ?? "", state: e.state ?? "", year: String(e.year ?? ""), subjects: (th?.subjects ?? []).join(" ") }; }));
  } catch (e) { miniErr = e; }

  // Semantic model loads in the background; its state is always stated on the page.
  let semErr = null, semReady = false;
  const setStatus = () => {
    status.replaceChildren(
      miniErr ? h("div", { class: "errpanel" }, `Keyword search FAILED to load: ${miniErr.message}`) : "Keyword search: ready. ",
      semErr ? h("div", { class: "errpanel" }, `Semantic search FAILED to load (${semErr.message}). Keyword search still works; semantic and "both" modes will say so.`)
        : semReady ? " Semantic search: ready." : " Semantic search: downloading the language model (about 30 MB, first time only)...");
  };
  setStatus();
  getEmbedder().then(() => { semReady = true; setStatus(); run(); }, (e) => { semErr = e; setStatus(); run(); });

  /** Rank essays for a query by one method; returns {ids (post_ids best first), best (post_id->chunk index)}. */
  const keyword = (q) => { const ids = [], best = new Map();
    for (const r of mini.search(q)) { const ci = byChunk.get(r.id), pid = chunks[ci].post_id; if (!best.has(pid)) { best.set(pid, ci); ids.push(pid); } }
    return { ids, best }; };
  const semantic = async (q) => { const sc = semanticScores(await embedQuery(q, data["vectors.meta"].dim), data.vectors, data["vectors.meta"]);
    const order = [...sc.keys()].sort((a, b) => sc[b] - sc[a]), ids = [], best = new Map();
    for (const ci of order) { const pid = chunks[ci].post_id; if (!best.has(pid)) { best.set(pid, ci); ids.push(pid); } }
    return { ids, best }; };

  let token = 0;
  /** Run the current query in the selected mode and draw results; stale runs are dropped. */
  async function run() {
    const my = ++token, q = input.value.trim(), mode = root.querySelector("input[name=mode]:checked").value;
    if (!q) { results.replaceChildren(h("p", { class: "muted" }, "Type a word or phrase above.")); return; }
    const out = [];
    let kw = null, sem = null;
    if (mode !== "semantic") { if (miniErr) out.push(h("div", { class: "errpanel" }, "Keyword search is unavailable (see above).")); else kw = keyword(q); }
    if (mode !== "keyword") {
      if (semErr) out.push(h("div", { class: "errpanel" }, `Semantic search is unavailable: ${semErr.message}`));
      else if (!semReady) out.push(h("div", { class: "note" }, "Semantic search is still loading its language model; results will update automatically when it is ready."));
      else { try { sem = await semantic(q); } catch (e) { semErr = e; setStatus(); out.push(h("div", { class: "errpanel" }, `Semantic search FAILED: ${e.message}`)); } }
    }
    if (my !== token) return;
    const lists = [kw, sem].filter(Boolean);
    if (lists.length) {
      const ranked = lists.length === 2 ? rrf(lists.map((l) => l.ids)).map((r) => r.id) : lists[0].ids;
      if (!ranked.length) out.push(h("p", {}, "No matches."));
      for (const pid of ranked.slice(0, 10)) {
        const e = essays.get(pid), ci = (kw?.best.get(pid) ?? sem?.best.get(pid));
        const how = [kw?.ids.includes(pid) ? `keyword #${kw.ids.indexOf(pid) + 1}` : null, sem ? `semantic #${sem.ids.indexOf(pid) + 1}` : null].filter(Boolean).join(", ");
        out.push(h("div", { class: "result" }, h("b", {}, e.title), h("div", { class: "meta" }, `${e.author ?? "author not shown"} | ${e.state ?? "state not shown"} | ${e.year ?? "year not shown"} | ${how}`),
          h("div", {}, chunks[ci].snippet + " ..."), h("a", { href: e.url }, "Read the essay on prisonwitness.org")));
      }
    }
    results.replaceChildren(...out);
  }
  let timer; input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 250); });
  radios.addEventListener("change", run);
  run();
}

/* ============ 3c. SECTION 3: WHAT'S IN THE LETTERS ============ */

/** Count in how many distinct letters each entity text (case-insensitive) of a type is mentioned. */
function topMentions(entities, type, limit = 8) {
  const m = new Map();
  for (const e of entities.filter((x) => x.type === type)) { const k = e.text.toLowerCase(); (m.get(k) ?? m.set(k, new Set()).get(k)).add(e.post_id); }
  return [...m].map(([k, s]) => [k, s.size]).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, limit);
}

/** Build section 3: per-letter chips and themes, then counts across all letters. */
function renderContents(root, data) {
  const essays = data.essays.essays, N = essays.length, ents = data.entities.entities, themes = data.themes.themes;
  root.append(h("h2", {}, "What's in the letters"),
    h("p", {}, "An AI model read each letter and listed the places, institutions, policies and conditions it mentions, plus its overall tone. Every listed item was checked against the letter's text. People's names are deliberately left out. These are machine-made labels, so treat them as a guide, not a finding."));
  const sel = h("select", { "aria-label": "Choose a letter" }, essays.map((e, i) => h("option", { value: i }, e.title)));
  const view = h("div", {});
  const draw = () => {
    const e = essays[+sel.value], mine = ents.filter((x) => x.post_id === e.post_id), th = themes.find((t) => t.post_id === e.post_id);
    view.replaceChildren(h("h3", {}, "Mentioned in this letter"),
      ...ENTITY_TYPES.map((t) => { const xs = [...new Set(mine.filter((x) => x.type === t).map((x) => x.text))];
        return h("div", {}, h("b", {}, t.replace("_", " ") + ": "), xs.length ? xs.map((x) => h("span", { class: "chip" }, x)) : h("span", { class: "muted" }, "none found")); }),
      h("h3", {}, "Themes and tone"),
      th ? h("div", {}, h("div", {}, h("b", {}, "Archive subjects: "), th.subjects.map((s) => h("span", { class: "chip" }, s))),
        h("div", {}, h("b", {}, "Feelings: "), th.affect.map((s) => h("span", { class: "chip" }, s))),
        h("div", {}, h("b", {}, "Overall sentiment: "), th.sentiment), h("div", {}, h("b", {}, "Way of writing: "), th.stance))
        : h("p", { class: "muted" }, "No theme labels for this letter."));
  };
  sel.addEventListener("change", draw); draw();
  root.append(sel, view, h("h3", {}, "Across all letters"));
  const list = (title, rows) => h("div", { class: "box" }, h("h4", {}, title),
    rows.length ? h("ul", {}, rows.map(([k, n]) => h("li", {}, `${k}: mentioned in ${n} of ${N} letters`))) : h("span", { class: "muted" }, "none found"));
  const tally = (key, allowed) => allowed.map((v) => [v, themes.filter((t) => (key === "affect" ? t.affect.includes(v) : t[key] === v)).length]).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  root.append(h("div", { class: "cols two" }, list("Conditions most often mentioned", topMentions(ents, "condition")), list("Places most often mentioned", topMentions(ents, "place")),
    list("Feelings", tally("affect", AFFECTS)), list("Overall sentiment", tally("sentiment", SENTIMENTS))));
}

/* ============ 4. FOOTER ============ */

/** Provenance from build_info.json plus the standing notes. */
function renderFooter(root, info) {
  const models = Object.entries(info.models).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`).join("; ");
  const counts = Object.entries(info.counts).map(([k, v]) => `${v} ${k}`).join(", ");
  root.append(h("p", {}, h("b", {}, "Provenance. "), `Build ${info.git_sha}, ${info.generated_at}. Models (${models}). Counts: ${counts}. Share of letter text exposed on this site: ${info.exposure.percent}%.`),
    h("p", {}, "All text stays on JHU and local machines. No cloud models were used."),
    h("p", {}, "Full essays are not republished here; every result links back to the original. Source: ", h("a", { href: ARCHIVE_URL }, "American Prison Writing Archive (prisonwitness.org)"),
      ". See the archive's Takedown and Changes policy on that site."));
}

/* ============ 5. BOOT ============ */

/** Entry point: resolve mode, load and validate, then render; any failure shows red panels and stops. */
async function main() {
  const errs = document.getElementById("errors");
  let cfg;
  try { cfg = resolveMode(); } catch (e) { errs.append(errorPanel("Cannot start", e)); return; }
  if (cfg.mode === "fixtures") { const b = document.getElementById("banner"); b.hidden = false; b.textContent = "FIXTURE DATA — NOT REAL"; }
  const { data, errors } = await loadAll(cfg.mode, cfg.base);
  if (errors) {
    errs.append(h("p", {}, `Could not load data from ${cfg.base}. Nothing is shown rather than showing something wrong.`), ...errors.map((e) => errorPanel("Data problem", e)));
    return;
  }
  document.getElementById("app").hidden = false;
  try {
    renderAccuracy(document.getElementById("accuracy"), data["transcription_demo"], cfg.mode);
    renderContents(document.getElementById("contents"), data);
    renderFooter(document.getElementById("footer"), data["build_info"]);
    await renderSearch(document.getElementById("search"), data);
  } catch (e) { errs.append(errorPanel("Page error while rendering", { file: "app.js", field: "render", detail: e.stack || e.message })); }
}
main();
