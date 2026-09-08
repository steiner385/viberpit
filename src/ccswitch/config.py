"""Tool configuration with sane defaults, overridable via config.json."""
import json
from dataclasses import dataclass, fields

from . import paths


@dataclass
class Config:
    soft_threshold: float = 0.90
    hard_ceiling: float = 1.00
    usage_cache_ttl_sec: int = 60
    # Freshness window for PROCESS-BACKED quota (Copilot proxy, Codex app-server).
    # Those providers have no REST usage endpoint: reading them cold means starting
    # a process, so the server refreshes them on this cadence
    # (server/usage/process_quota.py) and the ledger keeps the reading fresh for
    # the same span. Anthropic stays on usage_cache_ttl_sec.
    process_quota_ttl_sec: int = 600
    poll_interval_sec: int = 60
    reservation_ttl_sec: int = 120
    expiry_skew_sec: int = 300
    timezone: str = "America/New_York"
    claude_bin: str = "claude"
    prefer_soon_reset: bool = True
    # Live-session load awareness: a heavily-used account's perishability score is
    # divided by (1 + load_sensitivity * weighted_live_sessions), spreading new
    # sessions across accounts to avoid 429s. idle_session_weight is the per-session
    # weight of an idle live session; an actively-working session always weighs 1.0.
    load_sensitivity: float = 0.5
    idle_session_weight: float = 0.3
    # Auto-retry budget for Anthropic API requests (see oauth.http_request).
    retry_max_retries: int = 3
    retry_max_total_sec: float = 15.0


def load_config() -> Config:
    cfg = Config()
    path = paths.config_path()
    if path.exists():
        data = json.loads(path.read_text())
        valid = {f.name for f in fields(Config)}
        for k, v in data.items():
            if k in valid:
                setattr(cfg, k, v)
    return cfg
