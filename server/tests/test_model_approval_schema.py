from server.db.models import Session, AppSettings, CAPACITY_EXEMPT_STATUSES


def test_session_has_pending_columns():
    cols = Session.__table__.columns
    for name in ("pending_model", "pending_model_requested_by", "pending_model_requested_at"):
        assert name in cols, name
        assert cols[name].nullable is True


def test_appsettings_has_policy_columns():
    cols = AppSettings.__table__.columns
    for name in ("model_approval_action", "model_approval_denylist"):
        assert name in cols, name
        assert cols[name].nullable is True


def test_pending_approval_is_capacity_exempt():
    assert "pending_approval" in CAPACITY_EXEMPT_STATUSES
