from server.sessions import events as ev


def test_usermessage_wire_includes_images():
    imgs = [{"url": "/api/sessions/s1/images/abc", "mime": "image/png", "name": "x.png"}]
    w = ev.UserMessage(turn_id="t", text="hi", src_uuid="t", images=imgs).to_wire()
    assert w["type"] == "user" and w["text"] == "hi"
    assert w["images"] == imgs


def test_usermessage_wire_defaults_empty_images():
    w = ev.UserMessage(turn_id="t", text="hi", src_uuid="t").to_wire()
    assert w["images"] == []
