// Validate built public data files with the same validators the page uses (site/validate.js).
// Usage: node validate_built.mjs <data-dir> <mode: public|fixtures> [--demo-only]
// Exits non-zero on the first failure, with the file and field named.
import { readFileSync, existsSync } from "node:fs";
import { SCHEMAS, DataError, checkVectors, crossCheck } from "../site/validate.js";

const [dir, mode, flag] = process.argv.slice(2);
if (!dir || !["public", "fixtures"].includes(mode)) { console.error("usage: validate_built.mjs <data-dir> <public|fixtures> [--demo-only]"); process.exit(2); }
const names = flag === "--demo-only" ? ["transcription_demo.json"] : Object.keys(SCHEMAS);
try {
  const docs = {};
  for (const n of names) docs[n] = SCHEMAS[n](JSON.parse(readFileSync(`${dir}/${n}`, "utf8")), mode);
  if (flag !== "--demo-only") {
    const b = readFileSync(`${dir}/vectors.i8`);
    checkVectors("vectors.i8", new Int8Array(b.buffer, b.byteOffset, b.length), docs["vectors.meta.json"]);
    crossCheck(docs);
    // Every crop must point at a file that exists next to the data files (the browser would show a broken image).
    for (const c of docs["crops.json"].crops) if (!existsSync(`${dir}/${c.file}`)) throw new Error(`crops.json: file missing for ${c.target} ${c.id}: ${dir}/${c.file}`);
  }
  console.log(`OK: ${names.length} file(s) in ${dir} valid (mode=${mode}${flag ? ", " + flag : ""})`);
} catch (e) {
  console.error(`FAILED: ${e instanceof DataError ? `${e.file}: field ${e.field}: ${e.detail}` : e.stack}`);
  process.exit(1);
}
