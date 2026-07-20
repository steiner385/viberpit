"""ccswitch web backend configuration.

Reads settings from environment variables. Copy .env.example → .env and fill in values.
"""
import os
from pathlib import Path

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

        model_config = {"populate_by_name": True, "env_file": ".env", "extra": "ignore"}

        @property
        def tailnet_allowlist_items(self) -> list[str]:
            """Return the tailnet allowlist as a list of non-empty strings."""
            return [item.strip() for item in self.tailnet_allowlist.split(",") if item.strip()]

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

        @property
        def tailnet_allowlist_items(self) -> list[str]:
            return [item.strip() for item in self.tailnet_allowlist.split(",") if item.strip()]

    def get_settings() -> Settings:  # type: ignore[misc]
        return Settings()


# File-only config: the webhook URL for outbound push notifications (ntfy / generic).
# Never web-writable; never logged in full.
PUSH_WEBHOOK_URL: str | None = (os.environ.get("CCSWITCH_PUSH_WEBHOOK_URL") or "").strip() or None

VAPID_PRIVATE_KEY: str | None = (os.environ.get("CCSWITCH_VAPID_PRIVATE_KEY") or "").strip() or None
VAPID_PUBLIC_KEY: str | None = (os.environ.get("CCSWITCH_VAPID_PUBLIC_KEY") or "").strip() or None
VAPID_SUBJECT: str = (os.environ.get("CCSWITCH_VAPID_SUBJECT") or "").strip() or "mailto:ccswitch@localhost"
