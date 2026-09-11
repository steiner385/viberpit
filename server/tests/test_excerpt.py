"""elide_middle: the one way a transcript excerpt is shortened for a helper model.

Live finding 2026-09-10 (session c07bbc41): the nudge composer saw assistant replies
cut at 1500 chars with no marker and told the session, four times, that its reply
"cut off mid-sentence". Every one of those replies was complete on disk (2.0–3.1k
chars). An excerpt cut must therefore keep the reply's ENDING (what the session hands
back is at the end) and say, in the excerpt itself, that the harness did the cutting."""
from server.sessions.excerpt import elide_middle


def test_short_text_is_returned_untouched():
    assert elide_middle("short reply", 100) == "short reply"
    assert elide_middle("x" * 100, 100) == "x" * 100


def test_long_text_keeps_head_and_tail_and_counts_the_cut():
    text = "H" * 1000 + "T" * 2000
    out = elide_middle(text, 600)
    assert out.startswith("H" * 200)
    assert out.endswith("T" * 400)
    assert "2400 chars omitted" in out
    assert len(out) <= 600 + 160


def test_marker_blames_the_harness_not_the_session():
    out = elide_middle("a" * 500, 90)
    assert "harness" in out
    assert "not the reply ending" in out


def test_empty_text_stays_empty():
    assert elide_middle("", 10) == ""
