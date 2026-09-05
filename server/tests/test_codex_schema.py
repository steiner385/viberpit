"""Pinned Codex app-server contract: DTO validation, redaction, and drift detection.

The fixtures under ``server/tests/fixtures/codex_app_server/<version>/`` are the
reviewable record of the real ``codex app-server generate-json-schema`` output for
the pinned CLI. These tests keep ``server/codex/schema.py``'s literal pin and the
committed fixtures from drifting apart in either direction.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server.codex import redaction, schema
from server.codex.schema import CodexSchemaError

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "codex_app_server" / "0.153.2"


def _fixture_contract() -> schema.Contract:
    return schema.load_pinned_fixture_contract(FIXTURE_ROOT)


# ---------------------------------------------------------------------------
# Pinned contract
# ---------------------------------------------------------------------------


def test_fixture_directory_is_named_for_the_pinned_cli_version() -> None:
    assert schema.PINNED_CLI_VERSION == "codex-cli 0.153.2"
    assert FIXTURE_ROOT.name == schema.PINNED_CLI_VERSION.split()[-1]


def test_committed_fixtures_hash_to_the_pinned_contract_sha256() -> None:
    assert _fixture_contract().sha256 == schema.PINNED_CONTRACT_SHA256


def test_committed_fixtures_contain_every_curated_document() -> None:
    documents = _fixture_contract().documents
    assert tuple(sorted(documents)) == tuple(sorted(schema.CURATED_SCHEMA_FILES))


def test_declared_server_methods_match_the_fixture_method_index() -> None:
    index = _fixture_contract().method_index
    assert frozenset(index["serverNotifications"]) == schema.DECLARED_SERVER_NOTIFICATIONS
    assert frozenset(index["serverRequests"]) == schema.DECLARED_SERVER_REQUESTS
    assert frozenset(index["clientNotifications"]) == schema.DECLARED_CLIENT_NOTIFICATIONS
    assert schema.SUPPORTED_CLIENT_REQUESTS <= frozenset(index["clientRequests"])


def test_supported_method_sets_are_subsets_of_the_declared_inventory() -> None:
    assert schema.SUPPORTED_SERVER_NOTIFICATIONS <= schema.DECLARED_SERVER_NOTIFICATIONS
    assert schema.SUPPORTED_SERVER_REQUESTS <= schema.DECLARED_SERVER_REQUESTS
    assert schema.SUPPORTED_CLIENT_NOTIFICATIONS <= schema.DECLARED_CLIENT_NOTIFICATIONS
    # `error` is the minimum supported notification for this phase.
    assert "error" in schema.SUPPORTED_SERVER_NOTIFICATIONS
    # The whole external-auth posture rests on exactly this one server request.
    assert schema.SUPPORTED_SERVER_REQUESTS == frozenset(
        {"account/chatgptAuthTokens/refresh"}
    )


def test_external_token_login_still_carries_the_unstable_markers() -> None:
    """The version pin exists because this surface is explicitly unstable."""
    document = json.dumps(_fixture_contract().documents["v2/LoginAccountParams.json"])
    for marker in schema.EXTERNAL_AUTH_UNSTABLE_MARKERS:
        assert marker in document


def test_fixtures_contain_no_secret_shaped_values() -> None:
    for path in sorted(FIXTURE_ROOT.glob("*.json")):
        assert not redaction.contains_secret_shaped_value(
            path.read_text(encoding="utf-8")
        ), path.name


# ---------------------------------------------------------------------------
# Drift detection — typed reason, never a bare raise
# ---------------------------------------------------------------------------


def test_check_contract_returns_none_when_the_observation_matches_the_pin() -> None:
    assert (
        schema.check_contract(
            cli_version=schema.PINNED_CLI_VERSION,
            contract_sha256=schema.PINNED_CONTRACT_SHA256,
        )
        is None
    )


def test_check_contract_reports_typed_cli_version_drift() -> None:
    drift = schema.check_contract(
        cli_version="codex-cli 0.154.0",
        contract_sha256=schema.PINNED_CONTRACT_SHA256,
    )
    assert drift is not None
    assert drift.reasons == (schema.ContractDriftReason.CLI_VERSION,)
    assert drift.observed_cli_version == "codex-cli 0.154.0"
    assert drift.expected_cli_version == schema.PINNED_CLI_VERSION


def test_check_contract_reports_typed_schema_hash_drift() -> None:
    drift = schema.check_contract(
        cli_version=schema.PINNED_CLI_VERSION,
        contract_sha256="0" * 64,
    )
    assert drift is not None
    assert drift.reasons == (schema.ContractDriftReason.CONTRACT_HASH,)


def test_check_contract_reports_both_reasons_together() -> None:
    drift = schema.check_contract(cli_version="codex-cli 9.9.9", contract_sha256="0" * 64)
    assert drift is not None
    assert drift.reasons == (
        schema.ContractDriftReason.CLI_VERSION,
        schema.ContractDriftReason.CONTRACT_HASH,
    )
    assert str(drift) == (
        "codex app-server contract drift "
        "(cli_version_mismatch, contract_hash_mismatch)"
    )


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


def test_initialize_params_advertise_experimental_api_only_when_asked() -> None:
    plain = schema.InitializeParams(
        client_info=schema.ClientInfo(name="ccswitch", version="1"),
    ).to_wire()
    assert plain["capabilities"]["experimentalApi"] is False

    experimental = schema.InitializeParams(
        client_info=schema.ClientInfo(name="ccswitch", version="1", title="ccswitch"),
        capabilities=schema.InitializeCapabilities(experimental_api=True),
    ).to_wire()
    assert experimental["capabilities"]["experimentalApi"] is True
    assert experimental["clientInfo"] == {
        "name": "ccswitch",
        "version": "1",
        "title": "ccswitch",
    }


def test_parse_initialize_response_accepts_the_real_shape() -> None:
    parsed = schema.parse_initialize_response(
        {
            "codexHome": "/var/lib/ccswitch/codex",
            "platformFamily": "unix",
            "platformOs": "linux",
            "userAgent": "codex_cli_rs/0.153.2",
        }
    )
    assert parsed.codex_home == "/var/lib/ccswitch/codex"
    assert parsed.user_agent == "codex_cli_rs/0.153.2"


def test_parse_initialize_response_tolerates_unknown_fields() -> None:
    parsed = schema.parse_initialize_response(
        {
            "codexHome": "/tmp/home",
            "platformFamily": "unix",
            "platformOs": "linux",
            "userAgent": "codex_cli_rs/0.153.2",
            "somethingAddedNextRelease": {"nested": [1, 2, 3]},
        }
    )
    assert parsed.platform_os == "linux"


def test_native_paths_and_model_content_never_leak_through_dto_reprs() -> None:
    initialized = schema.parse_initialize_response(
        {
            "codexHome": "/private/account/project",
            "platformFamily": "unix",
            "platformOs": "linux",
            "userAgent": "native-output-from-account",
        }
    )
    model = schema.parse_model(
        {
            "id": "account-owned-model-id",
            "model": "native-model-name",
            "displayName": "private display text",
            "description": "model output containing a prompt",
            "hidden": False,
            "isDefault": True,
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [],
        }
    )
    rendered = f"{initialized!r} {model!r}"
    for fragment in (
        "/private/account/project",
        "native-output-from-account",
        "account-owned-model-id",
        "private display text",
        "model output containing a prompt",
    ):
        assert fragment not in rendered


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"codexHome": "/tmp", "platformFamily": "unix", "platformOs": "linux"},
        {
            "codexHome": 7,
            "platformFamily": "unix",
            "platformOs": "linux",
            "userAgent": "x",
        },
        [],
    ],
)
def test_parse_initialize_response_rejects_invalid_payloads(payload: Any) -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_initialize_response(payload)


# ---------------------------------------------------------------------------
# account/login/start — external tokens
# ---------------------------------------------------------------------------


def test_external_auth_login_params_serialize_the_documented_variant() -> None:
    params = schema.ExternalAuthLoginParams(
        access_token=redaction.Redacted("header.payload.signature"),
        chatgpt_account_id=redaction.Redacted("acct-123456789"),
        chatgpt_plan_type="pro",
    )
    assert params.to_wire() == {
        "type": "chatgptAuthTokens",
        "accessToken": "header.payload.signature",
        "chatgptAccountId": "acct-123456789",
        "chatgptPlanType": "pro",
    }


def test_external_auth_login_params_never_expose_secrets_through_repr() -> None:
    params = schema.ExternalAuthLoginParams(
        access_token=redaction.Redacted("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"),
        chatgpt_account_id=redaction.Redacted("acct-123456789"),
    )
    for rendered in (repr(params), str(params)):
        assert "eyJhbGciOiJIUzI1NiJ9" not in rendered
        assert "acct-123456789" not in rendered
        assert "ExternalAuthLoginParams" in rendered


def test_external_auth_login_params_reject_empty_material() -> None:
    with pytest.raises(CodexSchemaError):
        schema.ExternalAuthLoginParams(
            access_token=redaction.Redacted(""),
            chatgpt_account_id=redaction.Redacted("acct-1"),
        )


def test_parse_login_account_response_requires_the_external_variant() -> None:
    assert schema.parse_login_account_response({"type": "chatgptAuthTokens"}).type == (
        "chatgptAuthTokens"
    )
    with pytest.raises(CodexSchemaError):
        schema.parse_login_account_response({"type": "apiKey"})
    with pytest.raises(CodexSchemaError):
        schema.parse_login_account_response({})


# ---------------------------------------------------------------------------
# account/read
# ---------------------------------------------------------------------------


def test_parse_get_account_response_reads_a_chatgpt_account() -> None:
    parsed = schema.parse_get_account_response(
        {
            "requiresOpenaiAuth": True,
            "account": {"type": "chatgpt", "email": "a@example.com", "planType": "pro"},
        }
    )
    assert parsed.requires_openai_auth is True
    account = parsed.account
    assert isinstance(account, schema.ChatgptAccount)
    assert account.plan_type == "pro"
    assert account.email is not None


def test_parse_get_account_response_allows_a_null_email_and_absent_account() -> None:
    parsed = schema.parse_get_account_response(
        {
            "requiresOpenaiAuth": False,
            "account": {"type": "chatgpt", "email": None, "planType": "unknown"},
        }
    )
    assert isinstance(parsed.account, schema.ChatgptAccount)
    assert parsed.account.email is None
    assert schema.parse_get_account_response({"requiresOpenaiAuth": True}).account is None


def test_parse_get_account_response_reads_non_chatgpt_variants() -> None:
    api_key = schema.parse_get_account_response(
        {"requiresOpenaiAuth": True, "account": {"type": "apiKey"}}
    )
    assert isinstance(api_key.account, schema.ApiKeyAccount)
    bedrock = schema.parse_get_account_response(
        {
            "requiresOpenaiAuth": True,
            "account": {"type": "amazonBedrock", "usesCodexManagedCredentials": True},
        }
    )
    assert isinstance(bedrock.account, schema.AmazonBedrockAccount)


def test_parse_get_account_response_never_leaks_the_email_through_repr() -> None:
    parsed = schema.parse_get_account_response(
        {
            "requiresOpenaiAuth": True,
            "account": {"type": "chatgpt", "email": "a@example.com", "planType": "pro"},
        }
    )
    assert "a@example.com" not in repr(parsed)


@pytest.mark.parametrize("plan_type", ["enterprise", "unknown", "pro"])
def test_account_plan_type_accepts_pinned_values(plan_type: str) -> None:
    parsed = schema.parse_get_account_response(
        {
            "requiresOpenaiAuth": False,
            "account": {
                "type": "chatgpt",
                "email": None,
                "planType": plan_type,
            },
        }
    )
    assert isinstance(parsed.account, schema.ChatgptAccount)
    assert parsed.account.plan_type == plan_type


def test_account_plan_type_rejects_unpinned_values() -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_get_account_response(
            {
                "requiresOpenaiAuth": False,
                "account": {
                    "type": "chatgpt",
                    "email": None,
                    "planType": "prompt=/private/project",
                },
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"requiresOpenaiAuth": "yes"},
        {"requiresOpenaiAuth": 1},
        {"account": {"type": "chatgpt", "email": None, "planType": "pro"}},
        {"requiresOpenaiAuth": True, "account": {"type": "unheardOf"}},
        {"requiresOpenaiAuth": True, "account": {"type": "chatgpt", "email": None}},
    ],
)
def test_parse_get_account_response_rejects_invalid_payloads(payload: Any) -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_get_account_response(payload)


def test_get_account_params_omit_an_unset_refresh_flag() -> None:
    assert schema.GetAccountParams().to_wire() == {}
    assert schema.GetAccountParams(refresh_token=False).to_wire() == {
        "refreshToken": False
    }


# ---------------------------------------------------------------------------
# account/rateLimits/read
# ---------------------------------------------------------------------------


def test_parse_rate_limits_response_reads_windows_and_buckets() -> None:
    parsed = schema.parse_rate_limits_response(
        {
            "accountId": "acct-1",
            "rateLimits": {
                "planType": "pro",
                "limitId": "codex",
                "limitName": "Codex",
                "primary": {
                    "usedPercent": 42,
                    "resetsAt": 1758000000,
                    "windowDurationMins": 300,
                },
                "secondary": {"usedPercent": 7, "resetsAt": None},
                "credits": {"hasCredits": True, "unlimited": False, "balance": "12.00"},
                "spendControlReached": False,
            },
            "rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 42}}},
        }
    )
    assert parsed.account_id == redaction.Redacted("acct-1")
    assert "acct-1" not in repr(parsed)
    assert parsed.rate_limits.primary is not None
    assert parsed.rate_limits.primary.used_percent == 42
    assert parsed.rate_limits.primary.window_duration_mins == 300
    assert parsed.rate_limits.secondary is not None
    assert parsed.rate_limits.secondary.resets_at is None
    assert parsed.rate_limits.credits is not None
    assert parsed.rate_limits.credits.has_credits is True
    assert set(parsed.rate_limits_by_limit_id) == {"codex"}


def test_parse_rate_limits_response_allows_an_empty_snapshot() -> None:
    parsed = schema.parse_rate_limits_response({"rateLimits": {}})
    assert parsed.rate_limits.primary is None
    assert parsed.rate_limits.secondary is None
    assert parsed.rate_limits_by_limit_id == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("planType", "not-a-plan"),
        ("rateLimitReachedType", "prompt=/private/project"),
    ],
)
def test_rate_limit_snapshot_rejects_unpinned_enum_values(
    field: str, value: str
) -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_rate_limits_response({"rateLimits": {field: value}})


def test_parse_rate_limits_response_rejects_a_bool_used_percent() -> None:
    """`bool` is an `int` subclass; a naive isinstance check would accept True."""
    with pytest.raises(CodexSchemaError):
        schema.parse_rate_limits_response({"rateLimits": {"primary": {"usedPercent": True}}})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"rateLimits": None},
        {"rateLimits": {"primary": {}}},
        {"rateLimits": {"primary": {"usedPercent": "42"}}},
        {"rateLimits": {}, "rateLimitsByLimitId": []},
    ],
)
def test_parse_rate_limits_response_rejects_invalid_payloads(payload: Any) -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_rate_limits_response(payload)


# ---------------------------------------------------------------------------
# model/list
# ---------------------------------------------------------------------------


def _model_wire(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "gpt-5.6-luna",
        "model": "gpt-5.6-luna",
        "displayName": "GPT-5.6 Luna",
        "description": "fast",
        "hidden": False,
        "isDefault": True,
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "quick"},
            {"reasoningEffort": "medium", "description": "balanced"},
        ],
    }
    payload.update(overrides)
    return payload


def test_parse_model_list_response_reads_the_catalog() -> None:
    parsed = schema.parse_model_list_response(
        {"data": [_model_wire()], "nextCursor": None}
    )
    assert parsed.next_cursor is None
    model = parsed.data[0]
    assert model.id == "gpt-5.6-luna"
    assert model.default_reasoning_effort == "medium"
    assert [option.reasoning_effort for option in model.supported_reasoning_efforts] == [
        "low",
        "medium",
    ]
    assert model.input_modalities == ("text", "image")


@pytest.mark.parametrize("modalities", [["text"], ["image", "audio"], []])
def test_model_input_modalities_accept_only_pinned_values(
    modalities: list[str],
) -> None:
    parsed = schema.parse_model(_model_wire(inputModalities=modalities))
    assert parsed.input_modalities == tuple(modalities)


def test_model_input_modalities_reject_an_unpinned_value() -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_model(_model_wire(inputModalities=["text", "video"]))


def test_parse_model_list_response_tolerates_unknown_model_fields() -> None:
    parsed = schema.parse_model_list_response(
        {"data": [_model_wire(brandNewField={"x": 1}, inputModalities=["text"])]}
    )
    assert parsed.data[0].input_modalities == ("text",)


def test_parse_model_list_response_accepts_an_empty_catalog() -> None:
    assert schema.parse_model_list_response({"data": []}).data == ()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {}},
        {"data": [_model_wire(isDefault="true")]},
        {"data": [_model_wire(defaultReasoningEffort="")]},
        {"data": [_model_wire(supportedReasoningEfforts=[{"reasoningEffort": "low"}])]},
        {"data": [_model_wire()], "nextCursor": 3},
    ],
)
def test_parse_model_list_response_rejects_invalid_payloads(payload: Any) -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_model_list_response(payload)


def test_model_list_params_only_serialize_supplied_fields() -> None:
    assert schema.ModelListParams().to_wire() == {}
    assert schema.ModelListParams(cursor="abc", include_hidden=True, limit=10).to_wire() == {
        "cursor": "abc",
        "includeHidden": True,
        "limit": 10,
    }


# ---------------------------------------------------------------------------
# account/chatgptAuthTokens/refresh (server request)
# ---------------------------------------------------------------------------


def test_parse_refresh_params_accepts_the_documented_reason() -> None:
    parsed = schema.parse_chatgpt_auth_tokens_refresh_params(
        {"reason": "unauthorized", "previousAccountId": "acct-1"}
    )
    assert parsed.reason == "unauthorized"
    assert parsed.previous_account_id is not None
    assert "acct-1" not in repr(parsed)


def test_parse_refresh_params_allows_a_null_previous_account_id() -> None:
    parsed = schema.parse_chatgpt_auth_tokens_refresh_params(
        {"reason": "unauthorized", "previousAccountId": None}
    )
    assert parsed.previous_account_id is None


@pytest.mark.parametrize(
    "payload", [{}, {"reason": "expired"}, {"reason": None}, {"reason": "unauthorized", "previousAccountId": 5}]
)
def test_parse_refresh_params_rejects_invalid_payloads(payload: Any) -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_chatgpt_auth_tokens_refresh_params(payload)


def test_refresh_response_serializes_and_hides_its_material() -> None:
    response = schema.ChatgptAuthTokensRefreshResponse(
        access_token=redaction.Redacted("new.access.token"),
        chatgpt_account_id=redaction.Redacted("acct-1"),
        chatgpt_plan_type=None,
    )
    assert response.to_wire() == {
        "accessToken": "new.access.token",
        "chatgptAccountId": "acct-1",
    }
    assert "new.access.token" not in repr(response)


# ---------------------------------------------------------------------------
# Server notifications
# ---------------------------------------------------------------------------


def test_parse_error_notification_keeps_its_message_out_of_repr() -> None:
    parsed = schema.parse_server_notification(
        "error",
        {
            "error": {"message": "secret-bearing failure detail", "codexErrorInfo": "unauthorized"},
            "threadId": "thread-1",
            "turnId": "turn-1",
            "willRetry": False,
        },
    )
    assert isinstance(parsed, schema.ErrorNotificationParams)
    assert parsed.message.reveal() == "secret-bearing failure detail"
    assert parsed.codex_error_info == "unauthorized"
    assert parsed.will_retry is False
    assert "secret-bearing failure detail" not in repr(parsed)


def test_error_notification_rejects_peer_controlled_error_categories() -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_server_notification(
            "error",
            {
                "error": {
                    "message": "ordinary native text",
                    "codexErrorInfo": "prompt=/private/project",
                },
                "threadId": "thread-1",
                "turnId": "turn-1",
                "willRetry": False,
            },
        )


def test_parse_account_notifications() -> None:
    updated = schema.parse_server_notification(
        "account/updated", {"authMode": "chatgptAuthTokens", "planType": "pro"}
    )
    assert isinstance(updated, schema.AccountUpdatedParams)
    assert updated.auth_mode == "chatgptAuthTokens"

    limits = schema.parse_server_notification(
        "account/rateLimits/updated", {"rateLimits": {"primary": {"usedPercent": 3}}}
    )
    assert isinstance(limits, schema.AccountRateLimitsUpdatedParams)
    assert limits.snapshot.primary is not None

    completed = schema.parse_server_notification(
        "account/login/completed", {"success": False, "error": "denied", "loginId": "l1"}
    )
    assert isinstance(completed, schema.AccountLoginCompletedParams)
    assert completed.success is False
    assert "l1" not in repr(completed)


@pytest.mark.parametrize(
    ("field", "value"),
    [("authMode", "arbitrary-mode"), ("planType", "arbitrary-plan")],
)
def test_account_updated_rejects_unpinned_enum_values(field: str, value: str) -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_server_notification("account/updated", {field: value})


def test_parse_server_notification_rejects_an_unsupported_method() -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_server_notification("thread/started", {})


def test_parse_server_notification_rejects_a_malformed_supported_payload() -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_server_notification("error", {"threadId": "t", "turnId": "u"})


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redacted_hides_its_value_from_str_repr_and_format() -> None:
    secret = redaction.Redacted("sk-proj-abcdefghijklmnop")
    assert str(secret) == redaction.REDACTED
    assert repr(secret) == redaction.REDACTED
    assert f"{secret}" == redaction.REDACTED
    assert "{}".format(secret) == redaction.REDACTED  # noqa: UP032 -- exercise __format__
    assert secret.reveal() == "sk-proj-abcdefghijklmnop"
    assert secret == redaction.Redacted("sk-proj-abcdefghijklmnop")


def test_scrub_text_redacts_jwt_and_sk_shaped_values() -> None:
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP"
    scrubbed_jwt = redaction.scrub_text(f"login failed for {jwt}")
    assert jwt not in scrubbed_jwt
    assert scrubbed_jwt == redaction.REDACTED

    scrubbed_key = redaction.scrub_text("key sk-proj-0123456789abcdef")
    assert "sk-proj-0123456789abcdef" not in scrubbed_key
    assert scrubbed_key == redaction.REDACTED

    # The shared server.audit patterns must still apply through this module.
    shared = redaction.scrub_text("sk-ant-api03-zzzz")
    assert "sk-ant-api03-zzzz" not in shared
    assert redaction.REDACTED in shared


def test_general_redaction_never_relays_arbitrary_native_text() -> None:
    native = "acct plain prompt=delete cwd=/private/project output=completed"
    assert redaction.scrub_text(native) == redaction.REDACTED
    assert redaction.scrub_value({"unexpected": native}) == redaction.REDACTED


def test_exception_redaction_returns_only_a_fixed_category() -> None:
    native = "acct plain prompt=delete cwd=/private/project output=completed"
    assert redaction.scrub_exception(RuntimeError(native)) == "runtime_error"


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (TimeoutError("native output"), "timeout"),
        (OSError("/private/project"), "transport_error"),
        (ValueError("account-id"), "validation_error"),
        (Exception("prompt text"), "internal_error"),
    ],
)
def test_exception_redaction_categories_are_fixed(
    exc: BaseException, category: str
) -> None:
    assert redaction.scrub_exception(exc) == category


def test_scrub_value_redacts_secret_and_content_bearing_fields() -> None:
    scrubbed = redaction.scrub_value(
        {
            "accessToken": "abc",
            "chatgptAccountId": "acct-1",
            "prompt": "do the thing",
            "command": ["rm", "-rf", "/"],
            "text": "model output",
            "method": "account/read",
            "nested": [{"refreshToken": "r"}],
        }
    )
    assert scrubbed == redaction.REDACTED


def test_scrub_value_unwraps_redacted_holders() -> None:
    assert redaction.scrub_value({"any": redaction.Redacted("x")}) == redaction.REDACTED


def test_contains_secret_shaped_value_detects_tokens_but_not_schema_text() -> None:
    assert redaction.contains_secret_shaped_value("sk-proj-0123456789abcdefghij")
    assert redaction.contains_secret_shaped_value(
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP"
    )
    assert not redaction.contains_secret_shaped_value(
        json.dumps({"title": "InitializeParams", "type": "object"})
    )


def test_scrub_exception_returns_a_safe_single_line_message() -> None:
    exc = RuntimeError("token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abcdefghijkl\nrejected")
    message = redaction.scrub_exception(exc)
    assert message == "runtime_error"


# ---------------------------------------------------------------------------
# Manifest bookkeeping and loader error branches (review round 1)
# ---------------------------------------------------------------------------


def _manifest() -> dict[str, Any]:
    loaded = json.loads((FIXTURE_ROOT / schema.MANIFEST_FILENAME).read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_manifest_metadata_is_pinned_to_the_module_constants() -> None:
    """These fields sit outside `Contract.sha256`, so nothing else pins them."""
    manifest = _manifest()
    assert manifest["cliVersion"] == schema.PINNED_CLI_VERSION
    assert manifest["contractSha256"] == schema.PINNED_CONTRACT_SHA256
    assert manifest["documents"] == list(schema.CURATED_SCHEMA_FILES)
    assert manifest["methodIndex"] == schema.METHOD_INDEX_FILENAME


def test_manifest_envelope_digests_and_counts_stay_consistent() -> None:
    manifest = _manifest()
    index = _fixture_contract().method_index
    assert set(manifest["envelopes"]) == set(schema.ENVELOPE_SCHEMA_FILES)
    for name, key in schema.ENVELOPE_SCHEMA_FILES.items():
        entry = manifest["envelopes"][name]
        digest = entry["sha256"]
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef"), name
        # The recorded count must match the inventory the hash actually covers.
        assert entry["methodCount"] == len(index[key]), name
    assert manifest["envelopes"]["ServerNotification.json"]["methodCount"] == len(
        schema.DECLARED_SERVER_NOTIFICATIONS
    )
    assert manifest["envelopes"]["ServerRequest.json"]["methodCount"] == len(
        schema.DECLARED_SERVER_REQUESTS
    )


def test_envelope_methods_rejects_malformed_envelopes() -> None:
    good = {"oneOf": [{"properties": {"method": {"enum": ["a/b"]}}}]}
    assert schema.envelope_methods(good) == ("a/b",)
    bad_envelopes: tuple[Any, ...] = (
        "not an object",
        {},
        {"oneOf": []},
        {"oneOf": ["not an object"]},
        {"oneOf": [{}]},
        {"oneOf": [{"properties": {}}]},
        {"oneOf": [{"properties": {"method": {"enum": []}}}]},
        {"oneOf": [{"properties": {"method": {"enum": ["a", "b"]}}}]},
        {"oneOf": [{"properties": {"method": {"enum": [7]}}}]},
    )
    for bad in bad_envelopes:
        with pytest.raises(CodexSchemaError):
            schema.envelope_methods(bad)


def test_extract_contract_reports_a_missing_generated_file(tmp_path: Path) -> None:
    with pytest.raises(CodexSchemaError) as caught:
        schema.extract_contract(tmp_path)
    assert schema.CURATED_SCHEMA_FILES[0] in str(caught.value)

    # Curated documents present, envelopes absent.
    for relative in schema.CURATED_SCHEMA_FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")
    with pytest.raises(CodexSchemaError) as caught:
        schema.extract_contract(tmp_path)
    assert "ClientRequest.json" in str(caught.value)


def test_extract_contract_reports_invalid_json(tmp_path: Path) -> None:
    for relative in schema.CURATED_SCHEMA_FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{ not json", encoding="utf-8")
    with pytest.raises(CodexSchemaError) as caught:
        schema.extract_contract(tmp_path)
    assert "not valid JSON" in str(caught.value)


def test_load_pinned_fixture_contract_rejects_a_broken_directory(tmp_path: Path) -> None:
    # No manifest at all.
    with pytest.raises(CodexSchemaError):
        schema.load_pinned_fixture_contract(tmp_path)

    # Manifest that is not an object.
    (tmp_path / schema.MANIFEST_FILENAME).write_text("[]", encoding="utf-8")
    with pytest.raises(CodexSchemaError) as caught:
        schema.load_pinned_fixture_contract(tmp_path)
    assert "not an object" in str(caught.value)

    # Manifest without a usable document list.
    (tmp_path / schema.MANIFEST_FILENAME).write_text(
        json.dumps({"documents": [1, 2]}), encoding="utf-8"
    )
    with pytest.raises(CodexSchemaError) as caught:
        schema.load_pinned_fixture_contract(tmp_path)
    assert "document list" in str(caught.value)

    # Valid document list, but the method index is missing a required key.
    (tmp_path / schema.MANIFEST_FILENAME).write_text(
        json.dumps({"documents": []}), encoding="utf-8"
    )
    (tmp_path / schema.METHOD_INDEX_FILENAME).write_text(
        json.dumps({"clientRequests": ["initialize"]}), encoding="utf-8"
    )
    with pytest.raises(CodexSchemaError) as caught:
        schema.load_pinned_fixture_contract(tmp_path)
    assert "method index is missing" in str(caught.value)


def test_parse_errors_never_echo_a_peer_controlled_value() -> None:
    """`CodexSchemaError` messages are documented as field/method names only."""
    poison = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln"
    for call in (
        lambda: schema.parse_login_account_response({"type": poison}),
        lambda: schema.parse_get_account_response(
            {"requiresOpenaiAuth": True, "account": {"type": poison}}
        ),
        lambda: schema.parse_chatgpt_auth_tokens_refresh_params({"reason": poison}),
        lambda: schema.parse_server_notification(poison, {}),
    ):
        with pytest.raises(CodexSchemaError) as caught:
            call()
        assert poison not in str(caught.value)


def test_parse_refresh_response_validates_an_outbound_payload() -> None:
    parsed = schema.parse_chatgpt_auth_tokens_refresh_response(
        {"accessToken": "a.b.c", "chatgptAccountId": "acct-1", "chatgptPlanType": "pro"}
    )
    assert parsed.chatgpt_plan_type == "pro"
    assert "acct-1" not in repr(parsed)
    invalid: tuple[Any, ...] = ({}, {"accessToken": "a"}, {"chatgptAccountId": "b"}, [])
    for payload in invalid:
        with pytest.raises(CodexSchemaError):
            schema.parse_chatgpt_auth_tokens_refresh_response(payload)
