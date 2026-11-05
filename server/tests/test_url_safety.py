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


def test_caps_length_only_when_asked():
    head = "https://e.x/"
    at_cap = head + "a" * (MAX_URL_LEN - len(head))
    assert is_absolute_http_url(at_cap, max_len=MAX_URL_LEN)
    assert not is_absolute_http_url(at_cap + "a", max_len=MAX_URL_LEN)


def test_long_url_passes_with_no_max_len():
    # A long presigned URL (share_artifact) is fine: no cap unless the caller asks.
    assert is_absolute_http_url("https://bucket.example/obj?X-Amz-Signature=" + "a" * 5000)
