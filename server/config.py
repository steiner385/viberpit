"""ccswitch web backend configuration.

Reads settings from environment variables. Copy .env.example → .env and fill in values.
"""
import os
from pathlib import Path


def env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    """Parse a float tuning knob from the environment, tolerating a bad value.

    THE single parser for float env knobs — every caller routes through here so the
    fallback/clamp rules cannot drift between modules (constitution VI).

    A typo'd override must not crash daemon startup, and a syntactically valid but
    nonsensical one must not be honoured: a non-numeric, NaN/inf, or below-``minimum``
    value falls back to *default*. That clamp is load-bearing for timeouts —
    ``CCSWITCH_TRANSCRIPT_WINDOW_CEILING_S=0`` would otherwise make the ceiling fire
    instantly and turn every transcript into an empty one.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value or value in (float("inf"), float("-inf")):  # NaN / ±inf
        return default
    if minimum is not None and value < minimum:
        return default
    return value


try:
    from pydantic_settings import BaseSettings
    from pydantic import Field

    class Settings(BaseSettings):
        """Application settings loaded from environment variables."""

        host: str = Field(default="127.0.0.1", alias="CCSWITCH_HOST")
        port: int = Field(default=8787, alias="CCSWITCH_PORT")
        db_url: str = Field(
            default_factory=lambda: (
                f"sqlite+aiosqlite:///{Path.home() / '.claude-accounts' / 'ccswitch.db'}"
            ),
            alias="CCSWITCH_DB_URL",
        )
        master_key: str | None = Field(default=None, alias="CCSWITCH_MASTER_KEY")
        session_secret: str | None = Field(default=None, alias="CCSWITCH_SESSION_SECRET")
        tailnet_allowlist: str = Field(default="", alias="CCSWITCH_TAILNET_ALLOWLIST")
        # Peers entitled to ASSERT an identity via the Tailscale-User-Login header.
        # Loopback is always trusted (the `tailscale serve` proxy reaches the edge
        # there); this adds addresses for any other front-end that stamps the header,
        # e.g. a container gateway. Empty = loopback only.
        trusted_proxy_ips: str = Field(default="", alias="CCSWITCH_TRUSTED_PROXY_IPS")
        # Opt-in LAN-open mode: admit clients that present NO Tailscale identity as a
        # synthetic guest instead of 401'ing them. Defaults OFF so the deployed,
        # Tailscale-fronted posture stays locked down — only flip it on a trusted,
        # isolated network where the LAN itself is the trust boundary (issue: dobby.local).
        allow_unauthenticated: bool = Field(
            default=False, alias="CCSWITCH_ALLOW_UNAUTHENTICATED")
        github_app_id: int | None = Field(default=None, alias="CCSWITCH_GITHUB_APP_ID")
        github_app_installation_id: int | None = Field(
            default=None, alias="CCSWITCH_GITHUB_APP_INSTALLATION_ID")
        github_app_private_key_path: str | None = Field(
            default=None, alias="CCSWITCH_GITHUB_APP_PRIVATE_KEY_PATH")
        repo_root: str = Field(
            default_factory=lambda: str(Path.home() / "GitHub"),
            alias="CCSWITCH_REPO_ROOT",
        )
        reauth_display: str = Field(default=":0", alias="CCSWITCH_REAUTH_DISPLAY")
        reauth_virtual_display: bool = Field(default=True, alias="CCSWITCH_REAUTH_VIRTUAL_DISPLAY")
        chrome_profile: str = Field(
            default_factory=lambda: str(Path.home() / ".config" / "google-chrome" / "Default"),
            alias="CCSWITCH_CHROME_PROFILE",
        )
        # Extension ids carried into the temp login profile (comma list). Unset → the
        # core default (1Password, ccswitch.auth_login.REAUTH_EXTENSION_IDS); set but
        # empty → carry none. Spec: 2026-09-04-reauth-profile-fidelity-design.md D2.
        reauth_extension_ids: str | None = Field(default=None, alias="CCSWITCH_REAUTH_EXTENSIONS")
        # A human doing a fresh Google login on a cold browser can take several
        # minutes; the timeout must outlast that or it tears down the localhost
        # callback listener mid-login (zombie consent page).
        reauth_timeout_s: float = Field(default=900.0, alias="CCSWITCH_REAUTH_TIMEOUT_S")
        enable_xvfb: bool = Field(default=False, alias="CCSWITCH_ENABLE_XVFB")
        enable_adoption: bool = Field(default=False, alias="CCSWITCH_ENABLE_ADOPTION")
        display_start: int = Field(default=100, alias="CCSWITCH_DISPLAY_START")
        display_count: int = Field(default=12, alias="CCSWITCH_DISPLAY_COUNT")
        max_browsers: int = Field(default=6, alias="CCSWITCH_MAX_BROWSERS")
        vnc_port_base: int = Field(default=6100, alias="CCSWITCH_VNC_PORT_BASE")
        xvfb_resolution: str = Field(default="1280x800x24", alias="CCSWITCH_XVFB_RESOLUTION")
        # Pass --no-sandbox to the managed Chromium. A conscious security downgrade for
        # hosts where kernel.apparmor_restrict_unprivileged_userns=1 would otherwise FATAL
        # it at startup (quickstart §8b); keep OFF unless the host requires it (issue #136).
        chromium_no_sandbox: bool = Field(default=False, alias="CCSWITCH_CHROMIUM_NO_SANDBOX")
        # Grace window (s) after spawning a managed browser before reporting success, so a
        # startup-FATAL launch isn't counted as live. 0 disables the check.
        browser_liveness_s: float = Field(default=0.3, alias="CCSWITCH_BROWSER_LIVENESS_S")
        vnc_idle_timeout_s: float = Field(default=1800.0, alias="CCSWITCH_VNC_IDLE_TIMEOUT_S")
        enable_copilot: bool = Field(default=False, alias="CCSWITCH_ENABLE_COPILOT")
        copilot_port_base: int = Field(default=4142, alias="CCSWITCH_COPILOT_PORT_BASE")
        copilot_port_count: int = Field(default=8, alias="CCSWITCH_COPILOT_PORT_COUNT")
        copilot_idle_grace_s: float = Field(default=300.0, alias="CCSWITCH_COPILOT_IDLE_GRACE_S")
        # Native OpenAI Codex (server/codex). Both flags default FALSE — this is a
        # launch flag, and absence or incompatibility must disable Codex ONLY. The
        # external-token API is the experimental app-server surface runners use to
        # authenticate without holding a credential, so it is gated separately.
        codex_enabled: bool = Field(default=False, alias="CCSWITCH_CODEX_ENABLED")
        codex_external_auth_enabled: bool = Field(
            default=False, alias="CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED")
        codex_bin: str = Field(default="codex", alias="CCSWITCH_CODEX_BIN")
        # Comma list of `codex --version` strings an operator has verified beyond
        # the pin in server/codex/schema.py. It WIDENS the pinned set; it can never
        # remove the version this code was written against.
        codex_supported_versions: str = Field(
            default="", alias="CCSWITCH_CODEX_SUPPORTED_VERSIONS")
        codex_idle_grace_s: float = Field(default=300.0, alias="CCSWITCH_CODEX_IDLE_GRACE_S")
        # How long the edge's authorization projection stays valid at the credential
        # authority, and how often the edge renews it. The authority stops issuing
        # when the lease lapses, so `interval < lease` is not a tuning preference —
        # a single missed renewal must not take Codex down.
        codex_auth_sync_lease_s: float = Field(
            default=900.0, alias="CCSWITCH_CODEX_AUTH_SYNC_LEASE_S")
        codex_auth_sync_interval_s: float = Field(
            default=300.0, alias="CCSWITCH_CODEX_AUTH_SYNC_INTERVAL_S")
        # Cache lifetimes for the per-account Codex `model/list` and
        # `account/rateLimits/read` answers. Both are cheap to re-fetch and both are
        # per (account, CLI version), so these are short.
        codex_model_cache_s: float = Field(
            default=300.0, alias="CCSWITCH_CODEX_MODEL_CACHE_S")
        codex_quota_cache_s: float = Field(
            default=60.0, alias="CCSWITCH_CODEX_QUOTA_CACHE_S")
        # Server-side dictation (server/stt): off by default; needs the `stt` extra.
        enable_stt: bool = Field(default=False, alias="CCSWITCH_ENABLE_STT")
        stt_model: str = Field(default="base.en", alias="CCSWITCH_STT_MODEL")
        stt_cpu_threads: int = Field(default=4, alias="CCSWITCH_STT_CPU_THREADS")
        stt_tick_s: float = Field(default=1.0, alias="CCSWITCH_STT_TICK_S")
        stt_max_concurrent: int = Field(default=3, alias="CCSWITCH_STT_MAX_CONCURRENT")
        stt_max_session_s: float = Field(default=600.0, alias="CCSWITCH_STT_MAX_SESSION_S")
        log_level: str = Field(default="INFO", alias="CCSWITCH_LOG_LEVEL")
        log_dir: str = Field(
            default_factory=lambda: os.environ.get("CCSWITCH_LOG_DIR")
            or ("/var/log/ccswitch" if os.access("/var/log", os.W_OK)
                else str(Path.home() / ".claude-accounts" / "logs")),
            alias="CCSWITCH_LOG_DIR",
        )
        client_log_level: str = Field(default="warn", alias="CCSWITCH_CLIENT_LOG_LEVEL")
        # Override the directory used for archived session working directories.
        # Defaults to $CCSWITCH_HOME/archive. Set to a path on a larger/slower
        # drive to keep live session data and archives on separate filesystems.
        archive_home: str | None = Field(default=None, alias="CCSWITCH_ARCHIVE_HOME")
        # Comma-separated name=endpoint pairs seeded into the host registry on startup
        # (insert-if-absent by name; existing rows are never overwritten, so operator
        # changes via the Fleet UI survive restarts).
        #
        # Deployed topology: rocky is the SOLE targeted session runner. dobby is the
        # edge/master (web UI, config, reauth, deploy) and its local ccswitch-daemon is
        # deliberately disabled after the rocky session-host cutover. A deploy now fans
        # out to every row here (server/deploy/runners.py), reaching each host over its
        # registered ``ssh_host``. Production therefore sets this to rocky ONLY, in
        # the out-of-band /etc/ccswitch/ccswitch.env:
        #   CCSWITCH_FLEET_HOSTS=rocky=https://rocky.tail0dd570.ts.net:9443
        # Adding dobby (…,dobby=https://dobby:9443) is a supported but currently-unenabled
        # two-host expansion (fleet plan Task 13); the ranking/routing code is already
        # N-host, so this is a config flip, not a code change.
        fleet_hosts: str = Field(default="", alias="CCSWITCH_FLEET_HOSTS")
        # Model-approval gate. Which model TIERS require human approval (comma list,
        # matched via model_catalog.model_tier), and the action a gated tier triggers.
        # Overridable per-install by AppSettings and per-account by the ledger; this is
        # the ops-level default (see server/sessions/model_policy.resolve_model_policy).
        model_approval_action: str = Field(
            default="require_approval", alias="CCSWITCH_MODEL_APPROVAL_ACTION")
        model_approval_denylist: str = Field(
            default="fable", alias="CCSWITCH_MODEL_APPROVAL_DENYLIST")
        # Self-directed model switching: whether sessions are told they may request a
        # model change (and whether a marker found in output is acted on at all).
        self_model_switch_enabled: bool = Field(
            default=True, alias="CCSWITCH_SELF_MODEL_SWITCH_ENABLED")
        # How many model switches one session may auto-apply before further requests
        # are parked for a human instead. Bounds a session driving itself in circles.
        self_model_switch_cap: int = Field(
            default=3, alias="CCSWITCH_SELF_MODEL_SWITCH_CAP")
        # Reconciler kill switch (spec 003 Phase B): whether the reconciler is
        # allowed to write back to GitHub (project moves, addBlockedBy, etc.).
        # Defaults off — Phase B lands the schema/read-side before any write path.
        gh_reconcile_enabled: bool = Field(
            default=False, alias="CCSWITCH_GH_RECONCILE_ENABLED")
        # AI assistant (docs/superpowers/specs/2026-09-04-ai-assistant-design.md §4.11).
        # Off by default until the launch PR flips it; AppSettings.assistant_enabled overrides.
        assistant_enabled: bool = Field(default=False, alias="CCSWITCH_ASSISTANT_ENABLED")
        assistant_model: str = Field(default="sonnet", alias="CCSWITCH_ASSISTANT_MODEL")
        assistant_idle_s: float = Field(default=900.0, alias="CCSWITCH_ASSISTANT_IDLE_S")
        assistant_max_turns: int = Field(default=30, alias="CCSWITCH_ASSISTANT_MAX_TURNS")
        assistant_max_prompts_per_hour: int = Field(
            default=120, alias="CCSWITCH_ASSISTANT_MAX_PROMPTS_PER_HOUR")

        model_config = {"populate_by_name": True, "env_file": ".env", "extra": "ignore"}

        @property
        def tailnet_allowlist_items(self) -> list[str]:
            """Return the tailnet allowlist as a list of non-empty strings."""
            return [item.strip() for item in self.tailnet_allowlist.split(",") if item.strip()]

        @property
        def trusted_proxy_ips_items(self) -> list[str]:
            """Extra peer addresses allowed to assert an identity header."""
            return [item.strip() for item in self.trusted_proxy_ips.split(",") if item.strip()]

        @property
        def reauth_extension_id_items(self) -> list[str] | None:
            """None = use the core default list; [] = disabled; else the configured ids."""
            if self.reauth_extension_ids is None:
                return None
            return [i.strip() for i in self.reauth_extension_ids.split(",") if i.strip()]

        @property
        def fleet_hosts_map(self) -> dict[str, str]:
            """Return {name: endpoint} parsed from CCSWITCH_FLEET_HOSTS.

            Splits on the first '=' only so endpoints containing '=' (query strings,
            base64) are preserved correctly.
            """
            result: dict[str, str] = {}
            for item in self.fleet_hosts.split(","):
                item = item.strip()
                if "=" not in item:
                    continue
                name, _, endpoint = item.partition("=")
                name = name.strip()
                endpoint = endpoint.strip()
                if name and endpoint:
                    result[name] = endpoint
            return result

    def get_settings() -> Settings:
        return Settings()

except ImportError:  # pragma: no cover — fallback if pydantic-settings unavailable
    import dataclasses

    @dataclasses.dataclass
    class Settings:  # type: ignore[no-redef]
        host: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_HOST", "127.0.0.1")
        )
        port: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_PORT", "8787"))
        )
        db_url: str = dataclasses.field(
            default_factory=lambda: os.environ.get(
                "CCSWITCH_DB_URL",
                f"sqlite+aiosqlite:///{Path.home() / '.claude-accounts' / 'ccswitch.db'}",
            )
        )
        master_key: str | None = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_MASTER_KEY")
        )
        session_secret: str | None = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_SESSION_SECRET")
        )
        tailnet_allowlist: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_TAILNET_ALLOWLIST", "")
        )
        trusted_proxy_ips: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_TRUSTED_PROXY_IPS", "")
        )
        allow_unauthenticated: bool = dataclasses.field(
            default_factory=lambda: os.environ.get(
                "CCSWITCH_ALLOW_UNAUTHENTICATED", "false").lower() in ("1", "true", "yes", "on")
        )
        github_app_id: int | None = dataclasses.field(
            default_factory=lambda: (
                int(os.environ["CCSWITCH_GITHUB_APP_ID"])
                if os.environ.get("CCSWITCH_GITHUB_APP_ID") else None))
        github_app_installation_id: int | None = dataclasses.field(
            default_factory=lambda: (
                int(os.environ["CCSWITCH_GITHUB_APP_INSTALLATION_ID"])
                if os.environ.get("CCSWITCH_GITHUB_APP_INSTALLATION_ID") else None))
        github_app_private_key_path: str | None = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_GITHUB_APP_PRIVATE_KEY_PATH"))
        repo_root: str = dataclasses.field(
            default_factory=lambda: os.environ.get(
                "CCSWITCH_REPO_ROOT",
                str(Path.home() / "GitHub"),
            )
        )
        reauth_display: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_REAUTH_DISPLAY", ":0")
        )
        reauth_virtual_display: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_REAUTH_VIRTUAL_DISPLAY", "true").lower() in ("1", "true", "yes", "on")
        )
        chrome_profile: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_CHROME_PROFILE") or str(Path.home() / ".config" / "google-chrome" / "Default")
        )
        reauth_extension_ids: str | None = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_REAUTH_EXTENSIONS")
        )
        reauth_timeout_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_REAUTH_TIMEOUT_S", "900"))
        )
        enable_xvfb: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_ENABLE_XVFB", "false").lower() in ("1","true","yes","on")
        )
        enable_adoption: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_ENABLE_ADOPTION", "false").lower() in ("1","true","yes","on")
        )
        display_start: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_DISPLAY_START", "100"))
        )
        display_count: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_DISPLAY_COUNT", "12"))
        )
        max_browsers: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_MAX_BROWSERS", "6"))
        )
        vnc_port_base: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_VNC_PORT_BASE", "6100"))
        )
        xvfb_resolution: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_XVFB_RESOLUTION", "1280x800x24")
        )
        chromium_no_sandbox: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_CHROMIUM_NO_SANDBOX", "false").lower() in ("1","true","yes","on")
        )
        browser_liveness_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_BROWSER_LIVENESS_S", "0.3"))
        )
        vnc_idle_timeout_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_VNC_IDLE_TIMEOUT_S", "1800"))
        )
        enable_copilot: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_ENABLE_COPILOT", "false").lower() in ("1","true","yes","on")
        )
        copilot_port_base: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_COPILOT_PORT_BASE", "4142"))
        )
        copilot_port_count: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_COPILOT_PORT_COUNT", "8"))
        )
        copilot_idle_grace_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_COPILOT_IDLE_GRACE_S", "300.0"))
        )
        codex_enabled: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_CODEX_ENABLED", "false").lower() in ("1","true","yes","on")
        )
        codex_external_auth_enabled: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED", "false").lower() in ("1","true","yes","on")
        )
        codex_bin: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_CODEX_BIN", "codex")
        )
        codex_supported_versions: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_CODEX_SUPPORTED_VERSIONS", "")
        )
        codex_idle_grace_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_CODEX_IDLE_GRACE_S", "300.0"))
        )
        codex_auth_sync_lease_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_CODEX_AUTH_SYNC_LEASE_S", "900.0"))
        )
        codex_auth_sync_interval_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_CODEX_AUTH_SYNC_INTERVAL_S", "300.0"))
        )
        codex_model_cache_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_CODEX_MODEL_CACHE_S", "300.0"))
        )
        codex_quota_cache_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_CODEX_QUOTA_CACHE_S", "60.0"))
        )
        enable_stt: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_ENABLE_STT", "false").lower() in ("1","true","yes","on")
        )
        stt_model: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_STT_MODEL", "base.en")
        )
        stt_cpu_threads: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_STT_CPU_THREADS", "4"))
        )
        stt_tick_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_STT_TICK_S", "1.0"))
        )
        stt_max_concurrent: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_STT_MAX_CONCURRENT", "3"))
        )
        stt_max_session_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_STT_MAX_SESSION_S", "600.0"))
        )
        log_level: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_LOG_LEVEL", "INFO")
        )
        log_dir: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_LOG_DIR")
            or ("/var/log/ccswitch" if os.access("/var/log", os.W_OK)
                else str(Path.home() / ".claude-accounts" / "logs"))
        )
        client_log_level: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_CLIENT_LOG_LEVEL", "warn")
        )
        archive_home: str | None = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_ARCHIVE_HOME")
        )
        fleet_hosts: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_FLEET_HOSTS", "")
        )
        model_approval_action: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_MODEL_APPROVAL_ACTION", "require_approval")
        )
        model_approval_denylist: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_MODEL_APPROVAL_DENYLIST", "fable")
        )
        self_model_switch_enabled: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_SELF_MODEL_SWITCH_ENABLED", "true").lower() in ("1","true","yes","on")
        )
        self_model_switch_cap: int = dataclasses.field(
            default_factory=lambda: int(v) if (v := os.environ.get("CCSWITCH_SELF_MODEL_SWITCH_CAP", "")) and v.isdigit() else 3
        )
        gh_reconcile_enabled: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_GH_RECONCILE_ENABLED", "false").lower() in ("1", "true", "yes", "on")
        )
        assistant_enabled: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_ASSISTANT_ENABLED", "false").lower() in ("1", "true", "yes", "on")
        )
        assistant_model: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_ASSISTANT_MODEL", "sonnet")
        )
        assistant_idle_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_ASSISTANT_IDLE_S", "900.0"))
        )
        assistant_max_turns: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_ASSISTANT_MAX_TURNS", "30"))
        )
        assistant_max_prompts_per_hour: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_ASSISTANT_MAX_PROMPTS_PER_HOUR", "120"))
        )

        @property
        def tailnet_allowlist_items(self) -> list[str]:
            return [item.strip() for item in self.tailnet_allowlist.split(",") if item.strip()]

        @property
        def trusted_proxy_ips_items(self) -> list[str]:
            """Extra peer addresses allowed to assert an identity header."""
            return [item.strip() for item in self.trusted_proxy_ips.split(",") if item.strip()]

        @property
        def reauth_extension_id_items(self) -> list[str] | None:
            if self.reauth_extension_ids is None:
                return None
            return [i.strip() for i in self.reauth_extension_ids.split(",") if i.strip()]

        @property
        def fleet_hosts_map(self) -> dict[str, str]:
            result: dict[str, str] = {}
            for item in self.fleet_hosts.split(","):
                item = item.strip()
                if "=" not in item:
                    continue
                name, _, endpoint = item.partition("=")
                name = name.strip()
                endpoint = endpoint.strip()
                if name and endpoint:
                    result[name] = endpoint
            return result

    def get_settings() -> Settings:  # type: ignore[misc]
        return Settings()


# File-only config: the webhook URL for outbound push notifications (ntfy / generic).
# Never web-writable; never logged in full.
PUSH_WEBHOOK_URL: str | None = (os.environ.get("CCSWITCH_PUSH_WEBHOOK_URL") or "").strip() or None

VAPID_PRIVATE_KEY: str | None = (os.environ.get("CCSWITCH_VAPID_PRIVATE_KEY") or "").strip() or None
VAPID_PUBLIC_KEY: str | None = (os.environ.get("CCSWITCH_VAPID_PUBLIC_KEY") or "").strip() or None
VAPID_SUBJECT: str = (os.environ.get("CCSWITCH_VAPID_SUBJECT") or "").strip() or "mailto:ccswitch@localhost"
