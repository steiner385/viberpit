from __future__ import annotations
from . import events as ev


def summarize(events: list) -> list[dict]:
    """Reduce a list of events to the latest sub-agent summary per agentId.

    Accepts either SubAgentUpdate objects or their to_wire() dicts; ignores
    everything else. Preserves first-seen order, last-status-wins.
    """
    order: list[str] = []
    by_id: dict[str, dict] = {}
    for e in events:
        d = e.to_wire() if hasattr(e, "to_wire") else e
        if not isinstance(d, dict) or d.get("type") != "subAgentUpdate":
            continue
        aid = d["agentId"]
        if aid not in by_id:
            order.append(aid)
        by_id[aid] = {"agentId": aid, "parentAgentId": d.get("parentAgentId"),
                      "agentType": d.get("agentType", "agent"),
                      "description": d.get("description", ""), "status": d.get("status", "running")}
    return [by_id[a] for a in order]
