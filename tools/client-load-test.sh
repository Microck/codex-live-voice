#!/usr/bin/env bash
# Load harness for client/gpt-live-client.js (ESM).
# LESSON from upstream: `node --check file.js` false-OKs ES modules — check in
# module mode AND import the file to simulate a real load.
set -euo pipefail
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
CLIENT="$(cd "$(dirname "$0")/.." && pwd)/client/gpt-live-client.js"

cp "$CLIENT" "$TMP/check.mjs"
node --check "$TMP/check.mjs"
echo "OK: ESM syntax (module mode)"

node -e "
import('$TMP/check.mjs').then(m => {
  if (!m.LiveVoice || !m.V3_VOICES) { console.error('FAIL: missing exports'); process.exit(1) }
  if (!m.V3_VOICES.includes('cove')) { console.error('FAIL: voice list wrong'); process.exit(1) }
  console.log('OK: module imports — LiveVoice:', typeof m.LiveVoice, '| voices:', m.V3_VOICES.join(','))
}).catch(e => { console.error('LOAD ERROR:', String(e && e.stack || e).slice(0, 900)); process.exit(1) })
"
