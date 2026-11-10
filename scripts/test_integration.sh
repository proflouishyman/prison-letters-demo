#!/usr/bin/env bash
# Integration test: build_site.py output must pass the frontend validators (site/validate.js).
# Builds fixture inputs into a fresh mktemp dir (nothing is deleted), builds, validates, and also checks that a
# corrupted file is rejected. If site/data/transcription_demo.json exists (compare.py output), validates it too.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/pl_integration.XXXXXX")"
echo "temp dir: $TMP"

python3 "$HERE/make_fixture_inputs.py" "$TMP" >/dev/null
mkdir -p "$TMP/site"
cp "$REPO/site/index.html" "$REPO/site/app.js" "$REPO/site/validate.js" "$REPO/site/style.css" "$TMP/site/"
# The fixture's single scanned letter is one 380-line page, above the default 0.20 image cap; only this test relaxes it.
python3 "$HERE/build_site.py" --root "$TMP" --allow-fixture --max-image-exposure 0.6
node "$HERE/validate_built.mjs" "$TMP/site/data" fixtures

# Negative control: a built file with a required field removed must be rejected.
cp "$TMP/site/data/build_info.json" "$TMP/build_info.good"
python3 - "$TMP/site/data/build_info.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); del d["generated_at"]; json.dump(d, open(p, "w"))
PY
if node "$HERE/validate_built.mjs" "$TMP/site/data" fixtures >/dev/null 2>&1; then
  echo "FAILED: validator accepted build_info.json with generated_at removed" >&2; exit 1
fi
cp "$TMP/build_info.good" "$TMP/site/data/build_info.json"
echo "OK: negative control rejected as expected"

# Negative controls for crops.json: an unknown field and a crop pointing at a missing file must both be rejected.
cp "$TMP/site/data/crops.json" "$TMP/crops.good"
python3 - "$TMP/site/data/crops.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d["crops"][0]["extra"] = 1; json.dump(d, open(p, "w"))
PY
if node "$HERE/validate_built.mjs" "$TMP/site/data" fixtures >/dev/null 2>&1; then
  echo "FAILED: validator accepted a crop with an unknown field" >&2; exit 1
fi
python3 - "$TMP/site/data/crops.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); del d["crops"][0]["extra"]; d["crops"][0]["file"] = "crops/9001_p1_99.jpg"; json.dump(d, open(p, "w"))
PY
if node "$HERE/validate_built.mjs" "$TMP/site/data" fixtures >/dev/null 2>&1; then
  echo "FAILED: validator accepted a crop whose file is missing" >&2; exit 1
fi
cp "$TMP/crops.good" "$TMP/site/data/crops.json"
echo "OK: crop negative controls rejected as expected"

# The hand-made fixtures the page loads with ?data=fixtures must satisfy the same contract.
node "$HERE/validate_built.mjs" "$REPO/site/data/fixtures" fixtures

# compare.py's public output, if present (it has no fixture flag unless built from fixtures).
DEMO="$REPO/site/data/transcription_demo.json"
if [ -f "$DEMO" ]; then
  if grep -q '"fixture": *true' "$DEMO"; then M=fixtures; else M=public; fi
  mkdir -p "$TMP/demo" && cp "$DEMO" "$TMP/demo/"
  node "$HERE/validate_built.mjs" "$TMP/demo" "$M" --demo-only
else
  echo "SKIP: $DEMO not present"
fi
echo "PASS: integration test"
