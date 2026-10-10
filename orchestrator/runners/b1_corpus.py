"""Build the B1 code-execution corpus from HumanEval + MBPP-sanitized.

Datasets are baked into the orchestrator image by scripts/fetch-datasets.sh.
At runtime we read them once at process start and produce a list of
self-contained Python snippets that:

  - import everything they need
  - execute the canonical solution
  - run the bundled test cases
  - exit 0 on success, non-zero on assertion failure

This mirrors how an Agentic-RL code sandbox actually verifies a candidate
solution: run the function + run the test, check exit code.

If the dataset files are missing (e.g. running from a checkout without
running build-all.sh first), `load_corpus()` falls back to a small
hard-coded corpus so the smoke tests still work.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "datasets"

FALLBACK_CORPUS = [
    "print(sum(i*i for i in range(1000)))",
    "import math; print(math.factorial(15))",
    "s='abracadabra'; print(s[::-1])",
    "print([x for x in range(50) if x%7==0])",
    "import json; print(json.dumps({'a':1,'b':[1,2,3]}))",
    "def fib(n):\n a,b=0,1\n for _ in range(n): a,b=b,a+b\n return a\nprint(fib(25))",
    "import re; print(len(re.findall(r'\\w+', 'the quick brown fox')))",
    "print(sorted([3,1,4,1,5,9,2,6,5,3,5]))",
]


def _humaneval_snippet(item: dict) -> str | None:
    """Combine prompt + canonical_solution + test into one runnable script.

    HumanEval items look like:
      {
        "task_id": "HumanEval/0",
        "prompt": "from typing import List\n\ndef has_close_elements(...):\n    \"\"\"...\"\"\"\n    ",
        "canonical_solution": "    for idx, ...\n",
        "test": "def check(candidate):\n    assert candidate(...) == ...",
        "entry_point": "has_close_elements"
      }
    """
    try:
        prompt = item["prompt"]
        solution = item["canonical_solution"]
        test = item["test"]
        entry = item["entry_point"]
    except KeyError:
        return None
    return f"{prompt}{solution}\n{test}\ncheck({entry})\n"


def _mbpp_snippet(item: dict) -> str | None:
    """MBPP-sanitized items have `code` + `test_list` (list of asserts).

    Schema:
      {"task_id": 1, "code": "def remove_Occ(s,ch):...", "test_list": ["assert ..."]}
    """
    try:
        code = item["code"]
        tests = item["test_list"]
    except KeyError:
        return None
    body = "\n".join(tests)
    return f"{code}\n{body}\n"


def _load_humaneval() -> list[str]:
    path = DATA_DIR / "HumanEval.jsonl"
    if not path.exists():
        return []
    snippets: list[str] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            s = _humaneval_snippet(item)
            if s:
                snippets.append(s)
    return snippets


def _load_mbpp() -> list[str]:
    path = DATA_DIR / "sanitized-mbpp.json"
    if not path.exists():
        return []
    try:
        items = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    snippets: list[str] = []
    for item in items:
        s = _mbpp_snippet(item)
        if s:
            snippets.append(s)
    return snippets


def load_corpus() -> tuple[list[str], dict[str, int]]:
    """Return (snippets, breakdown).

    `breakdown` is logged + included in the result JSON for traceability.
    """
    he = _load_humaneval()
    mbpp = _load_mbpp()
    snippets = he + mbpp
    breakdown = {
        "humaneval": len(he),
        "mbpp_sanitized": len(mbpp),
        "fallback": 0,
    }
    if not snippets:
        log.warning(
            "B1 corpus datasets not found in %s; using built-in fallback (%d snippets)",
            DATA_DIR, len(FALLBACK_CORPUS),
        )
        snippets = list(FALLBACK_CORPUS)
        breakdown["fallback"] = len(snippets)
    else:
        log.info(
            "B1 corpus loaded: HumanEval=%d MBPP-sanitized=%d total=%d",
            breakdown["humaneval"], breakdown["mbpp_sanitized"], len(snippets),
        )
    return snippets, breakdown
