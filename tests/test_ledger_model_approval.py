from ccswitch import ledger


def _led():
    return {"accounts": {"acct": {}}}


def test_default_is_none():
    assert ledger.get_account_defaults(_led(), "acct")["modelApprovalAction"] is None


def test_set_and_get():
    led = _led()
    ledger.set_account_defaults(led, "acct", model_approval_action="allow")
    assert ledger.get_account_defaults(led, "acct")["modelApprovalAction"] == "allow"


def test_clear():
    led = _led()
    ledger.set_account_defaults(led, "acct", model_approval_action="block")
    ledger.set_account_defaults(led, "acct", clear_model_approval_action=True)
    assert ledger.get_account_defaults(led, "acct")["modelApprovalAction"] is None
