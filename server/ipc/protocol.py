"""Wire envelopes for the edge↔daemon Unix-socket transport. Pure dict builders, no I/O."""

# Bumped only when the edge↔daemon RPC or event-wire contract changes incompatibly.
PROTOCOL_VERSION = 1

EVENT = "event"
SUBSCRIBE = "subscribe"
UNSUBSCRIBE = "unsubscribe"
GAP = "gap"

#: The ``/info`` key listing what this daemon can do that an older one cannot — additive
#: behaviour the edge must not assume during a rolling upgrade. Absent on a daemon that
#: predates the key: the edge reads that as "none".
CAPABILITIES_INFO_KEY = "capabilities"
#: D-16a: the daemon's ``send_prompt`` intercepts ``APP_ONLY`` commands (``/fast``,
#: ``/effort``, ``/output-style``, ``/plan``) and runs them only for a human sender. A
#: daemon without it hands the text to the CLI, so the edge refuses those itself.
CAP_APP_COMMAND_GATE = "app_command_gate"
#: Everything THIS build's daemon advertises.
DAEMON_CAPABILITIES: tuple[str, ...] = (CAP_APP_COMMAND_GATE,)


def rpc_response_ok(req_id: str, result: dict) -> dict:
    return {"id": req_id, "result": result}


def rpc_response_err(req_id: str, err_type: str, message: str) -> dict:
    return {"id": req_id, "error": {"type": err_type, "message": message}}


def event_frame(stream: str, event: dict, seq: int) -> dict:
    return {"type": EVENT, "stream": stream, "event": event, "seq": seq}


def subscribe_frame(stream: str, since: int | None = None) -> dict:
    return {"type": SUBSCRIBE, "stream": stream, "since": since}


def gap_frame(stream: str, *, lost: int, oldest: int) -> dict:
    """Control frame: the subscriber's ``since`` anchor predates the oldest event
    still in the daemon's replay buffer, so ``lost`` events (seq since+1 .. oldest-1)
    were evicted and can't be replayed from memory. The edge should resync that
    stream from durable storage (transcript) rather than trust the in-memory replay.
    """
    return {"type": GAP, "stream": stream, "lost": lost, "oldest": oldest}


def unsubscribe_frame(stream: str) -> dict:
    return {"type": UNSUBSCRIBE, "stream": stream}
