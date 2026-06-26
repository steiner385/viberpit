import os
from unittest import mock

from server.ipc.tls import peer_identities, client_allowlist, allowed

# getpeercert() shape: subject is a tuple of RDNs, each RDN a tuple of (key, value) pairs;
# subjectAltName is a tuple of (type, value) pairs.
_CERT = {
    "subject": ((("commonName", "edge"),),),
    "subjectAltName": (("DNS", "edge.internal"), ("IP Address", "10.0.0.5")),
}


def test_peer_identities_unions_cn_and_sans():
    assert peer_identities(_CERT) == {"edge", "edge.internal", "10.0.0.5"}


def test_peer_identities_cn_only():
    assert peer_identities({"subject": ((("commonName", "edge"),),)}) == {"edge"}


def test_peer_identities_empty_cert():
    assert peer_identities({}) == set()


def test_client_allowlist_unset_is_none():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert client_allowlist() is None


def test_client_allowlist_blank_is_none():
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_CLIENT_ALLOWLIST": "  ,, "}, clear=True):
        assert client_allowlist() is None


def test_client_allowlist_parses_trims_and_drops_empties():
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_CLIENT_ALLOWLIST": " edge, edge.internal ,"}, clear=True):
        assert client_allowlist() == frozenset({"edge", "edge.internal"})


def test_allowed_cn_hit():
    assert allowed(_CERT, frozenset({"edge"})) is True


def test_allowed_san_hit():
    assert allowed(_CERT, frozenset({"edge.internal"})) is True


def test_allowed_miss():
    assert allowed(_CERT, frozenset({"someone-else"})) is False
