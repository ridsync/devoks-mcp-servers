"""Handler dependency-isolation invariant tests (TASK-013).

Traces: EDGE-SB-007, DSN-SB-008.

``DSN-SB-008`` says ``devoks_slackbot.handler`` must never import the
``anthropic`` SDK -- measured at **1,384 ms**, against a **3,000 ms**
``CTR-SB-002`` ACK budget (Slack's own 2xx deadline; miss it and Slack
retries three times with exponential backoff). One careless import line
(e.g. ``from .ask import ask_claude``) burns 46% of that budget before
handler.py runs a single line, and per ``EDGE-SB-007`` the failure only
shows up in production, on a cold Lambda start -- nowhere a normal test
run would ever catch it. This file is the guard.

``test_handler.py`` (``TASK-012``) already carries one copy of this check
inline, deliberately kept there as a belt-and-suspenders assertion next to
the behavioral tests it sits beside. This file is the dedicated, wider-scope
version: it also forbids the ``devoks_slackbot.ask`` and
``devoks_slackbot.worker`` module names (not just the ``anthropic`` package
itself, so a failure here names the exact import to remove) and the
``httpx2`` HTTP client (handler only ACKs Slack, it never posts back --
that's worker's job).

**Why a subprocess, not a same-process ``sys.modules`` check:** pytest
collects every test module into one process, and ``test_ask.py`` in this
same suite imports ``devoks_slackbot.ask`` (hence ``anthropic``) for its own
purposes. Checking ``sys.modules`` in-process would see whatever the test
run order already loaded, not this module's own import graph -- always
failing, or flaking on collection order. A subprocess that imports nothing
but ``devoks_slackbot.handler`` is the only way to see its import graph in
isolation (same technique ``test_handler.py`` uses; see its own docstring).
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TypedDict, cast

import pytest

#: Measured in this workspace (see the workspace PLAN §1 handoff notes and
#: servers/slackbot/pyproject.toml's anthropic dependency comment) -- kept as
#: a named constant so failure messages and this docstring can't drift apart.
_ANTHROPIC_IMPORT_COST_MS: int = 1_384
_ACK_BUDGET_MS: int = 3_000

_SUBPROCESS_TIMEOUT_S: int = 30

#: Loose regression ceiling, deliberately far above the ~150 ms baseline this
#: module actually measures. CI machine performance varies too widely for a
#: tight threshold to be reliable (a tight one would make this test flaky,
#: not more correct) -- this is a regression *detector*, not a budget
#: *verdict*. The exact, authoritative budget verdict comes from TASK-033's
#: real Lambda ``Init Duration`` measurement, not from a unit test.
_IMPORT_DURATION_REGRESSION_CEILING_MS: int = 5_000

_IMPORT_PROBE_SCRIPT = (
    "import importlib.util\n"
    "import json\n"
    "import sys\n"
    "import time\n"
    "\n"
    "_t0 = time.perf_counter()\n"
    "import devoks_slackbot.handler\n"
    "_elapsed_ms = (time.perf_counter() - _t0) * 1000\n"
    "\n"
    "_worker_spec = importlib.util.find_spec('devoks_slackbot.worker')\n"
    "\n"
    "print(json.dumps({\n"
    "    'modules': sorted(sys.modules),\n"
    "    'elapsed_ms': _elapsed_ms,\n"
    "    'worker_module_exists': _worker_spec is not None,\n"
    "}))\n"
)


class _ImportProbeResult(TypedDict):
    modules: list[str]
    elapsed_ms: float
    worker_module_exists: bool


@pytest.fixture(scope="module")
def import_probe() -> _ImportProbeResult:
    """Import ``devoks_slackbot.handler`` in a throwaway subprocess; report its footprint.

    Every check in this file reads the *same* single import (one subprocess,
    module-scoped) -- they assert different things about one observation, so
    there is no reason to pay for a fresh subprocess per assertion.

    ``worker_module_exists`` is discovered via ``importlib.util.find_spec``
    rather than by attempting ``import devoks_slackbot.worker`` directly:
    ``worker.py`` is ``TASK-014``'s deliverable and may not exist yet on this
    checkout, and ``find_spec`` locates a module without executing it, so it
    can answer "does this exist" without ever raising ``ModuleNotFoundError``.
    """
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE_SCRIPT],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
    )
    assert result.returncode == 0, (
        "the isolation probe subprocess itself failed while importing "
        "devoks_slackbot.handler -- this is not one of the isolation checks "
        f"failing, the import itself is broken:\n{result.stderr}"
    )
    return cast("_ImportProbeResult", json.loads(result.stdout))


# --- DSN-SB-008 / EDGE-SB-007: the core invariant ---------------------------


def test_handler_import_excludes_anthropic_edge_sb_007_dsn_sb_008(
    import_probe: _ImportProbeResult,
) -> None:
    leaked = sorted(
        m for m in import_probe["modules"] if m == "anthropic" or m.startswith("anthropic.")
    )
    assert not leaked, (
        f"devoks_slackbot.handler's import graph now pulls in anthropic ({leaked}). "
        f"Importing anthropic costs {_ANTHROPIC_IMPORT_COST_MS} ms (measured) -- "
        f"{_ANTHROPIC_IMPORT_COST_MS / _ACK_BUDGET_MS:.0%} of the {_ACK_BUDGET_MS} ms "
        "Slack ACK budget (CTR-SB-002) gone before handler.py runs a single line "
        "(EDGE-SB-007). DSN-SB-008 requires handler.py to never import anthropic, "
        "directly or transitively. Remove whatever new import path pulled it in."
    )


def test_handler_import_excludes_ask_module_dsn_sb_008(import_probe: _ImportProbeResult) -> None:
    assert "devoks_slackbot.ask" not in import_probe["modules"], (
        "devoks_slackbot.handler's import graph now includes devoks_slackbot.ask, "
        "which itself imports anthropic -- the same DSN-SB-008 violation reached "
        "through a named module instead of the anthropic package directly. Checking "
        "the module name too means a failure here points straight at the import to "
        "remove (e.g. an accidental `from .ask import ask_claude` in handler.py)."
    )


def test_handler_import_does_not_pull_in_worker_module_dsn_sb_008(
    import_probe: _ImportProbeResult,
) -> None:
    if not import_probe["worker_module_exists"]:
        pytest.skip(
            "devoks_slackbot.worker does not exist yet (it is TASK-014's deliverable) "
            "-- nothing to verify until it lands; this check will start running once "
            "worker.py is created."
        )
    assert "devoks_slackbot.worker" not in import_probe["modules"], (
        "devoks_slackbot.handler's import graph now includes devoks_slackbot.worker, "
        "which imports devoks_slackbot.ask, which imports anthropic -- this "
        "reintroduces the DSN-SB-008 violation through a third path. handler.py must "
        "never import worker.py (they are separate Lambdas connected only by an "
        "async `boto3` Lambda invoke, never a Python import)."
    )


def test_handler_import_excludes_httpx2_dsn_sb_008(import_probe: _ImportProbeResult) -> None:
    leaked = sorted(m for m in import_probe["modules"] if m == "httpx2" or m.startswith("httpx2."))
    assert not leaked, (
        f"devoks_slackbot.handler's import graph now pulls in httpx2 ({leaked}). "
        "handler only ACKs Slack -- it never posts a reply back to Slack (that's "
        "worker.py's job) -- so it has no legitimate need for an HTTP client. If "
        "this changed on purpose, DSN-SB-008 and this allow-list need updating "
        "together, not just this assertion."
    )


# --- allow-list: these ARE expected, this file must not forbid them --------


def test_handler_import_still_includes_its_legitimate_dependencies(
    import_probe: _ImportProbeResult,
) -> None:
    """``boto3``/``botocore``/``starlette`` are handler's real, needed dependencies.

    Not a DSN-SB-008 check -- the opposite: a canary that this file's
    isolation checks above stay narrowly scoped to anthropic/ask/worker/
    httpx2 and never regress into forbidding something handler.py
    legitimately needs (DynamoDB idempotency + the async worker invoke via
    boto3/botocore, the ASGI app itself via starlette).
    """
    modules = set(import_probe["modules"])
    missing = sorted(name for name in ("boto3", "botocore", "starlette") if name not in modules)
    assert not missing, (
        f"devoks_slackbot.handler's import graph is missing {missing} -- these are "
        "legitimate, expected dependencies, not something this isolation test "
        "forbids. If they disappeared, something else in handler.py broke."
    )


# --- loose regression ceiling on import time --------------------------------


def test_handler_import_duration_stays_under_loose_regression_ceiling(
    import_probe: _ImportProbeResult,
) -> None:
    """Regression *detector*, not a budget *verdict* -- see module docstring.

    This exists to catch some *other* expensive import landing in handler's
    graph in the future (not necessarily named ``anthropic``) that the
    name-based checks above would miss. The ceiling is deliberately loose
    (CI machines vary too widely for a tight one to be reliable); the exact
    CTR-SB-002 budget verdict comes from TASK-033's real Lambda
    ``Init Duration`` measurement, not from this test.
    """
    elapsed_ms = import_probe["elapsed_ms"]
    assert elapsed_ms < _IMPORT_DURATION_REGRESSION_CEILING_MS, (
        f"devoks_slackbot.handler took {elapsed_ms:.0f} ms to import in a clean "
        f"subprocess -- over this test's loose {_IMPORT_DURATION_REGRESSION_CEILING_MS} ms "
        f"regression ceiling (the real budget is CTR-SB-002's {_ACK_BUDGET_MS} ms ACK "
        "window). Something expensive landed in handler's import graph; find it before "
        "assuming this ceiling is just too tight -- it has generous headroom on purpose."
    )
