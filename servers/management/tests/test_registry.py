"""Tests for devoks_mcp_management.tools.registry (TASK-008).

Traces: AC-001-2, DSN-005.
"""

from __future__ import annotations

from typing import cast

import pytest
from mcp.server.mcpserver import MCPServer

from devoks_mcp_management.adapters.knowledge.github.tools import register as register_github_tools
from devoks_mcp_management.tools import registry
from devoks_mcp_management.tools.registry import Guard


def test_github_adapter_is_registered_by_default_after_task_022() -> None:
    # DSN-005: `adapters/knowledge/github/tools.py` is TASK-022's job — the
    # collection point now carries exactly that one registrar (PR2's only
    # adapter so far). Was `test_zero_adapters_are_registered_in_stage_1`
    # (asserting `== ()`) before TASK-022 wired the first adapter in.
    assert (register_github_tools,) == registry._ADAPTER_REGISTRARS  # pyright: ignore[reportPrivateUsage]


def test_register_tools_is_a_noop_with_zero_registrars(monkeypatch: pytest.MonkeyPatch) -> None:
    # DSN-005: `register_tools` must complete without raising when no
    # registrar is configured. Exercised via monkeypatch (rather than relying
    # on the module's real default, which is no longer empty as of TASK-022)
    # so this keeps testing the empty-tuple behavior in isolation.
    monkeypatch.setattr(registry, "_ADAPTER_REGISTRARS", ())
    sentinel_mcp = cast(MCPServer, object())
    sentinel_guard = cast(Guard, object())

    registry.register_tools(sentinel_mcp, sentinel_guard)  # must not raise


def test_register_tools_forwards_mcp_and_guard_unchanged_to_each_registrar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DSN-005: this is the exact mechanism TASK-022 depends on — a
    # registrar must receive `(mcp, guard)` unchanged so it can decorate its
    # own tool functions with `guard(...)` and call `mcp.add_tool(...)`.
    calls: list[tuple[object, object]] = []

    def fake_registrar(mcp: MCPServer, guard: Guard) -> None:
        calls.append((mcp, guard))

    monkeypatch.setattr(registry, "_ADAPTER_REGISTRARS", (fake_registrar,))

    sentinel_mcp = cast(MCPServer, object())
    sentinel_guard = cast(Guard, object())
    registry.register_tools(sentinel_mcp, sentinel_guard)

    assert calls == [(sentinel_mcp, sentinel_guard)]


def test_register_tools_calls_every_registrar_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    # A second/third adapter must not be skipped once one is registered —
    # locks in "one line per adapter" actually accumulating, not replacing.
    order: list[str] = []

    def first(mcp: MCPServer, guard: Guard) -> None:
        order.append("first")

    def second(mcp: MCPServer, guard: Guard) -> None:
        order.append("second")

    monkeypatch.setattr(registry, "_ADAPTER_REGISTRARS", (first, second))

    registry.register_tools(cast(MCPServer, object()), cast(Guard, object()))

    assert order == ["first", "second"]
