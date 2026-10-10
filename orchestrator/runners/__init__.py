"""Per-benchmark runners.

Every runner maps one rollout to one CubeSandbox microVM; see
`orchestrator/rollout.py` for the lifecycle and the reasoning behind that
boundary. B8 is the exception - it measures the sandbox lifecycle itself
rather than a workload.

B2 (SWE-build) and B6 (DataSci) remain unimplemented placeholders and are
deliberately absent from ALL_RUNNERS rather than registered as stubs.
"""

from .base import BenchmarkResult, Runner
from .b1_codeexec import B1Runner
from .b3_toolcall import B3Runner
from .b4_browser import B4Runner
from .b5_sqlexec import B5Runner
from .b7_textgame import B7Runner
from .b8_sandboxlife import B8Runner
from .b9_concurrent import B9Runner

ALL_RUNNERS: dict[str, Runner] = {
    "B1": B1Runner(),
    "B3": B3Runner(),
    "B4": B4Runner(),
    "B5": B5Runner(),
    "B7": B7Runner(),
    "B8": B8Runner(),
    "B9": B9Runner(),
}

__all__ = ["BenchmarkResult", "Runner", "ALL_RUNNERS"]
