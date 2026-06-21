"""Tool configuration with sane defaults, overridable via config.json."""
import json
from dataclasses import dataclass, fields

from . import paths


@dataclass
class Config:
    soft_threshold: float = 0.90
    hard_ceiling: float = 1.00
    usage_cache_ttl_sec: int = 60
    poll_interval_sec: int = 60
    reservation_ttl_sec: int = 120
    expiry_skew_sec: int = 300
    timezone: str = "America/New_York"
    claude_bin: str = "claude"
    prefer_soon_reset: bool = True


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
