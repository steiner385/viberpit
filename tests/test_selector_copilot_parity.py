from datetime import datetime, timezone, timedelta
from ccswitch.selector import rank_accounts
from ccswitch.oauth import Usage
from ccswitch.config import Config

NOW = datetime(2026, 7, 6, tzinfo=timezone.utc)

def test_synthesized_copilot_usage_ranks_alongside_anthropic():
    soon = NOW + timedelta(hours=2)
    usages = {
        "anthropic1": Usage(50.0, soon, 50.0, soon, False),          # perishable
        "copilot1": Usage(10.0, NOW + timedelta(days=20), 10.0, NOW + timedelta(days=20), False),
    }
    ranked, _ = rank_accounts(usages, reserved=frozenset(), cfg=Config(), now=NOW)
    names = [r.name for r in ranked]
    assert set(names) == {"anthropic1", "copilot1"}  # both eligible, unified ranking
