# Datasets

This folder is populated at build time by `scripts/fetch-datasets.sh`,
which is invoked from `scripts/build-all.sh` before `docker buildx build`.

Files baked into the orchestrator image:

| File                                  | Source                                                                                                  | Purpose                                              |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| `HumanEval.jsonl` (~165 lines)        | https://github.com/openai/human-eval (`data/HumanEval.jsonl.gz`, decompressed at fetch time)             | B1 corpus - canonical Python solutions               |
| `sanitized-mbpp.json` (~427 entries)  | https://github.com/google-research/google-research (`mbpp/sanitized-mbpp.json`)                          | B1 corpus - MBPP-sanitized canonical solutions       |

## Why bake them in?

The c7i / c8g target instances may not have outbound internet access.
Fetching at build time means the orchestrator image is self-contained
and reproducible: same image digest -> same corpus.

## Licensing

- HumanEval: MIT (OpenAI)
- MBPP-sanitized: Apache-2.0 (Google Research)

Both permit redistribution as part of evaluation tooling.
