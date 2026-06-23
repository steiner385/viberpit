"""Wire envelopes for the edge↔daemon Unix-socket transport. Pure dict builders, no I/O."""

# Bumped only when the edge↔daemon RPC or event-wire contract changes incompatibly.
PROTOCOL_VERSION = 1

EVENT = "event"
SUBSCRIBE = "subscribe"
UNSUBSCRIBE = "unsubscribe"


def rpc_response_ok(req_id: str, result: dict) -> dict:
    return {"id": req_id, "result": result}


def rpc_response_err(req_id: str, err_type: str, message: str) -> dict:
    return {"id": req_id, "error": {"type": err_type, "message": message}}


def event_frame(stream: str, event: dict, seq: int) -> dict:
    return {"type": EVENT, "stream": stream, "event": event, "seq": seq}


def subscribe_frame(stream: str, since: int | None = None) -> dict:
    return {"type": SUBSCRIBE, "stream": stream, "since": since}


def unsubscribe_frame(stream: str) -> dict:
    return {"type": UNSUBSCRIBE, "stream": stream}
