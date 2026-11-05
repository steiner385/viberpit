"""actions_api.metrics_artifact — the product's ``viberpit-metrics`` CI artifact
(release milestones spec §6), read through injected fake runners."""
from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from server.github.actions_api import METRICS_ARTIFACT, METRICS_FILE, metrics_artifact


def _ok(payload) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def _zip(text: str, name: str = METRICS_FILE) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


GOOD = {"version": 1, "metrics": {"rubric_missing_live": 293}, "sha": "abc",
        "generated_at": "2026-11-05T06:00:00Z"}


class _Fake:
    def __init__(self, artifacts, blob: bytes = b""):
        self.listing = {"artifacts": artifacts}
        self.blob = blob
        self.run_calls: list[list[str]] = []
        self.bytes_calls: list[list[str]] = []

    def run(self, args, token=None):
        self.run_calls.append(args)
        return _ok(self.listing)

    def run_bytes(self, args):
        self.bytes_calls.append(args)
        return self.blob


def _read(fake: _Fake):
    return metrics_artifact("o/r", 7, run=fake.run, run_bytes=fake.run_bytes, sleep=lambda s: None)


def test_returns_parsed_metrics():
    fake = _Fake([{"id": 1, "name": "junit-python", "expired": False},
                  {"id": 9, "name": METRICS_ARTIFACT, "expired": False}],
                 _zip(json.dumps(GOOD)))
    assert _read(fake) == GOOD
    assert fake.run_calls == [["api", "repos/o/r/actions/runs/7/artifacts"]]
    assert fake.bytes_calls == [["api", "repos/o/r/actions/artifacts/9/zip"]]


def test_absent_artifact_returns_none():
    fake = _Fake([{"id": 1, "name": "junit-python", "expired": False}])
    assert _read(fake) is None
    assert fake.bytes_calls == []


def test_expired_artifact_returns_none():
    fake = _Fake([{"id": 9, "name": METRICS_ARTIFACT, "expired": True}])
    assert _read(fake) is None
    assert fake.bytes_calls == []


@pytest.mark.parametrize("blob", [
    _zip("{not json"),
    b"not a zip",
    _zip(json.dumps(GOOD), name="other.json"),
    _zip(json.dumps([1, 2])),
])
def test_bad_json_raises_value_error(blob):
    fake = _Fake([{"id": 9, "name": METRICS_ARTIFACT, "expired": False}], blob)
    with pytest.raises(ValueError, match=r"^metrics\.json: "):
        _read(fake)


@pytest.mark.parametrize("doc", [
    {**GOOD, "version": 2},
    {k: v for k, v in GOOD.items() if k != "version"},
    {**GOOD, "metrics": [1]},
])
def test_wrong_version_raises(doc):
    fake = _Fake([{"id": 9, "name": METRICS_ARTIFACT, "expired": False}], _zip(json.dumps(doc)))
    with pytest.raises(ValueError, match=r"^metrics\.json: "):
        _read(fake)
