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
        repo_root: str = Field(
            default_factory=lambda: str(Path.home() / "GitHub"),
            alias="CCSWITCH_REPO_ROOT",
        )

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
        repo_root: str = dataclasses.field(
            default_factory=lambda: os.environ.get(
                "CCSWITCH_REPO_ROOT",
                str(Path.home() / "GitHub"),
            )
        )

        @property
        def tailnet_allowlist_items(self) -> list[str]:
            return [item.strip() for item in self.tailnet_allowlist.split(",") if item.strip()]

    def get_settings() -> Settings:  # type: ignore[misc]
        return Settings()
