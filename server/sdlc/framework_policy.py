"""Load the portable policy from the trusted installed checkout only."""
from pathlib import Path
import importlib.util

# Always load installed server checkout code, never a queried repository module.
_spec = importlib.util.spec_from_file_location("portable_framework_policy", Path(__file__).resolve().parents[2] / ".agents/framework_policy.py")
assert _spec is not None and _spec.loader is not None
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)
