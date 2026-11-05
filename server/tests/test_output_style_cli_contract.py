"""D-16a contract test against the INSTALLED ``claude`` binary (constitution I/III).

ViberPit reads the session's style list from ``initialize.available_output_styles``,
passes the effective style at spawn in the ``--settings`` JSON, and changes it live with
``apply_flag_settings {outputStyle}`` (``server/sessions/flag_settings.py``). This pins,
against the binary:

- ``initialize.available_output_styles`` lists the five built-ins
  (``flag_settings.BUILTIN_OUTPUT_STYLES``) and ``output_style`` is ``default``;
- ``--settings {"outputStyle": "Learning"}`` starts the CLI on that style;
- a live ``{"outputStyle": <name>}`` write is accepted and read back by ``get_settings``.

No model call is ever made: the D-14 harness (``test_settings_cli_contract._Cli``) runs
the CLI with a throwaway ``CLAUDE_CONFIG_DIR``/``HOME``, no credential, a closed-port
``ANTHROPIC_BASE_URL``, and writes only control requests. Skipped when no ``claude``
resolves.
"""
from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from server.sessions import flag_settings as fs
from server.tests.test_settings_cli_contract import _Cli, binary  # noqa: F401 (fixture)

pytestmark = pytest.mark.slow


@pytest.fixture
def cli(binary: str) -> Iterator[_Cli]:  # noqa: F811
    c = _Cli(binary)
    try:
        yield c
    finally:
        c.close()


def test_initialize_lists_the_builtin_styles(cli: _Cli) -> None:
    init = cli.ok({"subtype": "initialize", "hooks": None})
    styles = fs.output_styles(init)
    assert styles is not None, init.keys()
    assert list(fs.BUILTIN_OUTPUT_STYLES) == [s for s in styles
                                              if s in fs.BUILTIN_OUTPUT_STYLES]
    assert fs.live_output_style(init) == fs.DEFAULT_OUTPUT_STYLE


def test_a_spawn_with_the_style_in_settings_starts_on_it(binary: str) -> None:  # noqa: F811
    c = _Cli(binary, ["--settings", json.dumps({fs.OUTPUT_STYLE_KEY: "Learning"})])
    try:
        init = c.ok({"subtype": "initialize", "hooks": None})
        assert fs.live_output_style(init) == "Learning"
    finally:
        c.close()


def test_a_live_style_write_is_read_back(cli: _Cli) -> None:
    cli.ok({"subtype": "initialize", "hooks": None})
    cli.ok({"subtype": "apply_flag_settings", "settings": {fs.OUTPUT_STYLE_KEY: "Explanatory"}})
    effective = cli.ok({"subtype": "get_settings"})["effective"]
    assert effective[fs.OUTPUT_STYLE_KEY] == "Explanatory"
    cli.ok({"subtype": "apply_flag_settings",
            "settings": {fs.OUTPUT_STYLE_KEY: fs.DEFAULT_OUTPUT_STYLE}})
    effective = cli.ok({"subtype": "get_settings"})["effective"]
    assert effective[fs.OUTPUT_STYLE_KEY] == fs.DEFAULT_OUTPUT_STYLE
