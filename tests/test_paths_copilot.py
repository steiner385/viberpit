from ccswitch import paths


def test_copilot_dirs_nest_under_store():
    assert paths.copilot_store_dir() == paths.store_dir() / "copilot"
    assert paths.copilot_token_dir("work") == paths.store_dir() / "copilot" / "work"
