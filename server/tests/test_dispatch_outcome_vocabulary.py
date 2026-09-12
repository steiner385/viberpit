"""``DISPATCH_OUTCOMES`` is ONE vocabulary with a web mirror (constitution VI).

``server/db/models.py`` owns the tuple; ``web/src/api/pm.ts`` re-declares it as a
TS literal so the web can type its outcome unions. A mirror nothing pins drifts —
the same shape ``test_model_tier_order.py`` pins for the model tiers, so this test
reads the file the same way rather than growing a second mechanism.
"""
import re
from pathlib import Path

from server.db.models import DISPATCH_OUTCOMES


def test_web_dispatch_outcomes_match_the_server_tuple():
    ts = (Path(__file__).resolve().parents[2] / "web/src/api/pm.ts").read_text()
    m = re.search(r"export const DISPATCH_OUTCOMES\s*(?::[^=]*)?=\s*\[(.*?)\]", ts, re.S)
    assert m, "exported DISPATCH_OUTCOMES array literal not found in web/src/api/pm.ts"
    web = re.findall(r"'([^']*)'", m.group(1))
    # ORDER too: the array is the union's declaration order, and a reordered mirror
    # is the first symptom of a hand-edited copy.
    assert web == list(DISPATCH_OUTCOMES)
