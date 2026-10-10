#!/usr/bin/env bash
# Fetch HumanEval + MBPP-sanitized corpora into orchestrator/datasets/.
# Called automatically by build-all.sh before `docker buildx build`, so
# the dataset files are baked into the orchestrator image (no network
# access needed at run time on c7i / c8g).
#
# Idempotent: skips downloads if files already present and non-empty.

set -euo pipefail
cd "$(dirname "$0")/.."

DEST="orchestrator/datasets"
mkdir -p "${DEST}"

HUMANEVAL_URL="https://raw.githubusercontent.com/openai/human-eval/master/data/HumanEval.jsonl.gz"
MBPP_SANITIZED_URL="https://raw.githubusercontent.com/google-research/google-research/master/mbpp/sanitized-mbpp.json"

fetch() {
  local url="$1" out="$2"
  if [[ -s "${out}" ]]; then
    echo "    [ok]      ${out}"
    return 0
  fi
  echo "    [fetch]   ${out}"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "${url}" -o "${out}"
  else
    wget -qO "${out}" "${url}"
  fi
}

echo "==> fetching evaluation datasets into ${DEST}/"
fetch "${HUMANEVAL_URL}"      "${DEST}/HumanEval.jsonl.gz"
fetch "${MBPP_SANITIZED_URL}" "${DEST}/sanitized-mbpp.json"

# Decompress HumanEval (jsonl.gz -> jsonl) so the runner can read it
# without depending on gzip in the image.
if [[ ! -s "${DEST}/HumanEval.jsonl" ]]; then
  echo "    [decode]  ${DEST}/HumanEval.jsonl"
  gunzip -kc "${DEST}/HumanEval.jsonl.gz" > "${DEST}/HumanEval.jsonl"
fi

# Quick sanity counts
he_lines=$(wc -l < "${DEST}/HumanEval.jsonl" | tr -d ' ')
mbpp_count=$(python3 -c "import json,sys; print(len(json.load(open('${DEST}/sanitized-mbpp.json'))))" 2>/dev/null || echo "?")
echo "==> HumanEval problems: ${he_lines} (expected 164)"
echo "==> MBPP-sanitized problems: ${mbpp_count} (expected 427)"
