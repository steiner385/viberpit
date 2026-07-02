from server.sessions import model_catalog as mc


def test_aliases_are_the_stable_binary_set():
    assert mc.ALIASES == ["default", "opus", "sonnet", "haiku", "fable", "opusplan"]


def test_catalog_returns_id_label_dicts():
    cat = mc.catalog()
    assert {"id": "opus", "label": "Opus"} in cat
    assert {"id": "fable", "label": "Fable"} in cat
    assert all(set(e) == {"id", "label"} for e in cat)


def test_is_known_alias():
    assert mc.is_known_alias("opus") is True
    assert mc.is_known_alias("fable") is True
    assert mc.is_known_alias("claude-haiku-4-5") is False  # pinned ids are free-text, not aliases
