import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path

import pytest

from ccswitch import auth_login, paths

ONEP = "aeblfdkhhhdcdjpifhhbdiojplfjncoa"


def _make_source(tmp_path, *, with_extensions=False):
    """Create a mock Chrome profile directory with a Cookies database.

    with_extensions=True adds the files a real Linux profile has for an installed
    extension (1Password id) plus a second registered-but-absent id, Login Data,
    Secure Preferences and a NativeMessagingHosts manifest that must NOT be copied.
    """
    src = tmp_path / "google-chrome" / "Default"
    src.mkdir(parents=True)
    (tmp_path / "google-chrome" / "Local State").write_text('{"os_crypt":{"encrypted_key":"x"}}')
    db = src / "Cookies"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE cookies(host_key TEXT, name TEXT)")
    c.executemany(
        "INSERT INTO cookies VALUES(?,?)",
        [
            (".google.com", "SID"),
            ("accounts.google.com", "X"),
            (".claude.ai", "sess"),
            (".claude.com", "sess2"),
            ("platform.claude.com", "p"),
            ("console.anthropic.com", "tok"),
        ],
    )
    c.commit()
    c.close()
    if with_extensions:
        ext = src / "Extensions" / ONEP / "8.12.34.34_0"
        ext.mkdir(parents=True)
        (ext / "manifest.json").write_text('{"name":"1Password","version":"8.12.34.34"}')
        les = src / "Local Extension Settings" / ONEP
        les.mkdir(parents=True)
        (les / "000003.ldb").write_bytes(b"ldb")
        state = src / "Extension State"
        state.mkdir()
        (state / "CURRENT").write_text("MANIFEST-000001")
        (src / "Login Data").write_bytes(b"sqlite-login-data")
        (src / "Secure Preferences").write_text('{"protection":{"super_mac":"x"}}')
        (src / "Preferences").write_text(json.dumps({
            "extensions": {"settings": {
                ONEP: {"location": 1, "from_webstore": True, "path": f"{ONEP}/8.12.34.34_0"},
                "notinstalledxxxxxxxxxxxxxxxxxxxx": {"location": 1},
            }},
            "signin": {"allowed": True},
            "account_info": [{"email": "me@example.com"}],
        }))
        nmh = tmp_path / "google-chrome" / "NativeMessagingHosts"
        nmh.mkdir()
        (nmh / "com.1password.1password.json").write_text("{}")
    return str(src)


def test_prepare_strips_claude_keeps_google(tmp_path, monkeypatch):
    """Verify prepare_reauth_profile copies cookies, strips Claude/Anthropic rows, keeps Google."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path)
    temp = auth_login.prepare_reauth_profile(src)
    assert auth_login._under_reauth_tmp(temp)
    # 0700 dir
    assert stat.S_IMODE(os.stat(temp).st_mode) == 0o700
    ck = Path(temp) / "Default" / "Cookies"
    rows = sqlite3.connect(ck).execute("SELECT host_key FROM cookies").fetchall()
    hosts = {r[0] for r in rows}
    assert ".google.com" in hosts and "accounts.google.com" in hosts
    assert not any("claude" in h or "anthropic" in h for h in hosts)
    assert (Path(temp) / "Local State").exists()
    assert stat.S_IMODE(os.stat(ck).st_mode) == 0o600
    auth_login.cleanup_reauth_profile(temp)
    assert not os.path.exists(temp)


def test_prepare_missing_source_raises(tmp_path, monkeypatch):
    """Verify prepare_reauth_profile raises FileNotFoundError if source Cookies DB is missing."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    with pytest.raises(FileNotFoundError):
        auth_login.prepare_reauth_profile(str(tmp_path / "nope"))


def test_cleanup_refuses_outside_reauth_tmp(tmp_path, monkeypatch):
    """Verify cleanup_reauth_profile refuses to delete paths outside reauth-tmp."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    victim = tmp_path / "victim"
    victim.mkdir()
    auth_login.cleanup_reauth_profile(str(victim))  # must be a no-op, NOT delete
    assert victim.exists()


def test_cleanup_does_not_create_reauth_tmp(tmp_path, monkeypatch):
    """Verify cleanup_reauth_profile guard has no mkdir side effect."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    auth_login.cleanup_reauth_profile(str(tmp_path / "whatever"))  # outside path
    assert not paths.reauth_tmp_dir().exists()   # guard must not have created it


def test_prepare_copies_registered_extension_and_login_data(tmp_path, monkeypatch):
    """Spec D2: copy Extensions/<id>, Local Extension Settings/<id>, Extension State,
    Login Data, and write a Preferences that registers ONLY the copied extension."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path, with_extensions=True)
    temp = auth_login.prepare_reauth_profile(src)
    d = Path(temp) / "Default"
    assert (d / "Extensions" / ONEP / "8.12.34.34_0" / "manifest.json").exists()
    assert (d / "Local Extension Settings" / ONEP / "000003.ldb").exists()
    assert (d / "Extension State" / "CURRENT").exists()
    assert (d / "Login Data").read_bytes() == b"sqlite-login-data"
    assert stat.S_IMODE(os.stat(d / "Login Data").st_mode) == 0o600
    prefs = json.loads((d / "Preferences").read_text())
    assert prefs == {"extensions": {"settings": {
        ONEP: {"location": 1, "from_webstore": True, "path": f"{ONEP}/8.12.34.34_0"},
    }}}
    # Must NOT travel: they either break Google (full Preferences) or are unwanted (D2/D3).
    assert not (d / "Secure Preferences").exists()
    assert not (Path(temp) / "NativeMessagingHosts").exists()
    assert stat.S_IMODE(os.stat(d / "Preferences").st_mode) == 0o600
    auth_login.cleanup_reauth_profile(temp)


def test_prepare_without_extension_footprint_is_unchanged(tmp_path, monkeypatch):
    """Spec D4: a profile with no Extensions dir / Preferences still prepares fine and
    writes no Preferences at all (Chrome then starts as it does today)."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path)
    temp = auth_login.prepare_reauth_profile(src)
    d = Path(temp) / "Default"
    assert (d / "Cookies").exists()
    assert not (d / "Preferences").exists()
    assert not (d / "Extensions").exists()
    assert not (d / "Login Data").exists()
    auth_login.cleanup_reauth_profile(temp)


def test_prepare_extension_ids_empty_disables_copy(tmp_path, monkeypatch):
    """CCSWITCH_REAUTH_EXTENSIONS="" → extension_ids=() → nothing extension-related is
    copied, but Login Data still is (it is independent of the id list)."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path, with_extensions=True)
    temp = auth_login.prepare_reauth_profile(src, extension_ids=())
    d = Path(temp) / "Default"
    assert not (d / "Extensions").exists()
    assert not (d / "Preferences").exists()
    assert not (d / "Extension State").exists()
    assert (d / "Login Data").exists()
    auth_login.cleanup_reauth_profile(temp)


def test_prepare_skips_id_registered_but_not_installed(tmp_path, monkeypatch):
    """An id in Preferences with no Extensions/<id> dir is skipped, never registered."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path, with_extensions=True)
    temp = auth_login.prepare_reauth_profile(src, extension_ids=("notinstalledxxxxxxxxxxxxxxxxxxxx",))
    d = Path(temp) / "Default"
    assert not (d / "Extensions").exists()
    assert not (d / "Preferences").exists()
    auth_login.cleanup_reauth_profile(temp)


def test_prepare_unreadable_preferences_skips_extensions(tmp_path, monkeypatch):
    """Spec D4: a torn/invalid source Preferences must not fail the login."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path, with_extensions=True)
    (Path(src) / "Preferences").write_text("{not json")
    temp = auth_login.prepare_reauth_profile(src)
    d = Path(temp) / "Default"
    assert (d / "Cookies").exists()
    assert not (d / "Preferences").exists()
    assert not (d / "Extensions").exists()
    auth_login.cleanup_reauth_profile(temp)


def test_prepare_malformed_preferences_structure_skips_extensions(tmp_path, monkeypatch):
    """Valid JSON but a non-dict extensions/settings value must not raise (AttributeError)."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path, with_extensions=True)
    (Path(src) / "Preferences").write_text(json.dumps({"extensions": None}))
    temp = auth_login.prepare_reauth_profile(src)
    d = Path(temp) / "Default"
    assert (d / "Cookies").exists()
    assert not (d / "Preferences").exists()
    assert not (d / "Extensions").exists()
    auth_login.cleanup_reauth_profile(temp)


def test_prepare_extensions_dir_copy_failure_excludes_id_from_preferences(tmp_path, monkeypatch):
    """Finding 1 (D4): a racing Chrome can make Extensions/<id> fail mid-copytree
    (e.g. a crx file vanishing between scandir and copy2). This must not raise —
    and since a half-copied Extensions/<id> is worse than no extension at all, the
    id must NOT be registered in the written Preferences."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path, with_extensions=True)
    real_copytree = shutil.copytree
    target = str(Path(src) / "Extensions" / ONEP)

    def fake_copytree(s, d, *a, **kw):
        if str(s) == target:
            raise shutil.Error([(str(s), str(d), "boom")])
        return real_copytree(s, d, *a, **kw)

    monkeypatch.setattr(auth_login.shutil, "copytree", fake_copytree)
    temp = auth_login.prepare_reauth_profile(src)
    d = Path(temp) / "Default"
    assert (d / "Cookies").exists()
    assert not (d / "Extensions" / ONEP).exists()
    assert not (d / "Preferences").exists(), "half-copied id must not be registered"
    auth_login.cleanup_reauth_profile(temp)


def test_prepare_local_extension_settings_copy_failure_keeps_id_registered(tmp_path, monkeypatch):
    """Finding 1 (D4): Local Extension Settings/<id> is a live LevelDB store the user's
    running Chrome compacts continuously; a failing copy here is not fatal to the
    extension (it still loads, just without saved state) so the id must stay
    registered and Extensions/<id> must still be present."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path, with_extensions=True)
    real_copytree = shutil.copytree
    target = str(Path(src) / "Local Extension Settings" / ONEP)

    def fake_copytree(s, d, *a, **kw):
        if str(s) == target:
            raise shutil.Error([(str(s), str(d), "boom")])
        return real_copytree(s, d, *a, **kw)

    monkeypatch.setattr(auth_login.shutil, "copytree", fake_copytree)
    temp = auth_login.prepare_reauth_profile(src)
    d = Path(temp) / "Default"
    assert (d / "Extensions" / ONEP / "8.12.34.34_0" / "manifest.json").exists()
    assert not (d / "Local Extension Settings" / ONEP).exists()
    prefs = json.loads((d / "Preferences").read_text())
    assert ONEP in prefs["extensions"]["settings"], "id must stay registered"
    auth_login.cleanup_reauth_profile(temp)
