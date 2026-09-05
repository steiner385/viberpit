"""Issue #1164: an anti-drift oracle for handlers that swallow an outer cancellation.

Two source shapes eat a ``CancelledError`` that may belong to the *surrounding* coroutine
rather than to the child being torn down::

    except (asyncio.CancelledError, Exception):   # pairs the cancel with a real exception
        pass
    with contextlib.suppress(asyncio.CancelledError, ...):
        await task

Both are reachable from the SIGTERM drain (``server/ipc/drain_on_signal.py``), where a
swallowed cancellation turns a graceful shutdown into a ``TimeoutStopSec`` SIGKILL that
takes every live session with it.

The check is AST-based rather than a grep so that the *legitimate* shape — catching
``CancelledError`` alongside others and re-raising it — stays allowed. Teardown paths
should route through ``server.aio.cancel_and_reap``, which makes the distinction
structural. Four instances were fixed when this oracle was written; it exists so the
fifth cannot land quietly.
"""
from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("server", "src")
_SKIP_PARTS = {"tests", "node_modules", ".venv", "__pycache__", "migrations"}


def _python_files() -> list[Path]:
    out: list[Path] = []
    for d in _SCAN_DIRS:
        for p in (_ROOT / d).rglob("*.py"):
            if _SKIP_PARTS & set(p.parts):
                continue
            out.append(p)
    return out


def _is_cancelled_error(node: ast.expr) -> bool:
    """True for ``asyncio.CancelledError`` / a bare ``CancelledError`` reference."""
    if isinstance(node, ast.Attribute):
        return node.attr == "CancelledError"
    return isinstance(node, ast.Name) and node.id == "CancelledError"


def _reraises(body: list[ast.stmt]) -> bool:
    """True if the handler body re-raises (bare ``raise`` or any ``raise``) at top level."""
    return any(isinstance(stmt, ast.Raise) for stmt in body)


def _offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    rel = path.relative_to(_ROOT)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Tuple):
            elts = node.type.elts
            if any(_is_cancelled_error(e) for e in elts) and len(elts) > 1:
                if not _reraises(node.body):
                    found.append(
                        f"{rel}:{node.lineno}: `except (…CancelledError, …)` without a "
                        f"re-raise — use server.aio.cancel_and_reap"
                    )
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "suppress" and any(_is_cancelled_error(a) for a in node.args):
                found.append(
                    f"{rel}:{node.lineno}: `suppress(…CancelledError…)` — use "
                    f"server.aio.cancel_and_reap"
                )
    return found


def test_no_handler_swallows_an_outer_cancellation():
    offenders: list[str] = []
    for path in _python_files():
        offenders.extend(_offenders(path))
    assert not offenders, (
        "These sites can swallow the CALLER's cancellation (issue #1164):\n  "
        + "\n  ".join(sorted(offenders))
    )
