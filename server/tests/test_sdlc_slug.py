import pytest

from server.sdlc.slug import normalize_epic_title, slugify


@pytest.mark.parametrize("raw,expected", [
    ("epic(ui-audit): 04 — Repair web widgets", "04 — Repair web widgets"),
    ("Epic: ViberPit UI E3 — Browser trust", "ViberPit UI E3 — Browser trust"),
    ("epic(tasks): use-case-first task views", "use-case-first task views"),
    ("program(web): 2026 benchmark", "2026 benchmark"),
    ("Plain title with   spaces", "Plain title with spaces"),
    ("epic:", "epic:"),          # nothing left → keep the original
    ("  padded  ", "padded"),
])
def test_normalize_epic_title(raw, expected):
    assert normalize_epic_title(raw) == expected


def test_slugify_of_a_normalized_title_has_no_epic_prefix():
    assert slugify(normalize_epic_title("epic(ui-audit): 04 — Repair web widgets")) == "04-repair-web-widgets"
