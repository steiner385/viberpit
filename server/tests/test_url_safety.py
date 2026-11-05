"""THE absolute-http(s) URL rule (``server/url_safety.py``): the one gate for a
user-supplied link the web renders as an ``href``."""
import pytest

from server.url_safety import MAX_URL_LEN, is_absolute_http_url


@pytest.mark.parametrize("url", ["http://e.x", "https://e.x/a?b=1#c", "HTTPS://E.X/"])
def test_accepts_absolute_http(url):
    assert is_absolute_http_url(url)


@pytest.mark.parametrize("url", ["", "javascript:alert(1)", "data:text/html,x", "//e.x/a",
                                 "ftp://e.x", "https://", "e.x/a", " https://e.x"])
def test_rejects_everything_else(url):
    assert not is_absolute_http_url(url)


def test_caps_length():
    head = "https://e.x/"
    assert is_absolute_http_url(head + "a" * (MAX_URL_LEN - len(head)))
    assert not is_absolute_http_url(head + "a" * (MAX_URL_LEN - len(head) + 1))
