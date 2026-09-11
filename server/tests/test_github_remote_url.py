import pytest

from server.github.remote_url import slug_from_remote_url


@pytest.mark.parametrize("url", [
    "git@github.com:steiner385/viberpit.git",
    "https://github.com/steiner385/viberpit",
    "https://github.com/steiner385/viberpit.git",
    "https://github.com/steiner385/viberpit/",
    "ssh://git@github.com/steiner385/viberpit.git",
])
def test_slug_from_remote_url_accepts_ssh_and_https(url):
    assert slug_from_remote_url(url) == "steiner385/viberpit"


@pytest.mark.parametrize("url", ["", "   ", "not a url", "https://github.com/onlyowner",
                                 "git@github.com:a/b/c.git"])
def test_slug_from_remote_url_rejects_unrecognised(url):
    with pytest.raises(ValueError):
        slug_from_remote_url(url)
