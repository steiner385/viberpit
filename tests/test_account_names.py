from ccswitch.account_names import validate_account_name, assert_valid_account_name, InvalidAccountName
import pytest


@pytest.mark.parametrize("name", ["kindash", "primary", "a", "A1._-x", "x"*64])
def test_valid_names(name):
    assert validate_account_name(name) is True


@pytest.mark.parametrize("name", ["", ".", "..", "...", "../x", "a/b", "a\\b", ".hidden",
                                   "name with space", "x"*65, "-leading-dash", "a;b", "a$b"])
def test_invalid_names(name):
    assert validate_account_name(name) is False


def test_assert_raises():
    with pytest.raises(InvalidAccountName):
        assert_valid_account_name("../evil")
    assert_valid_account_name("kindash")  # no raise
