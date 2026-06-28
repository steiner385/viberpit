"""Spike A: can a CDP-attached virtual WebAuthn authenticator complete a full
register + assert ceremony headlessly, with no human gesture?

Self-contained: launches headless Chrome-for-Testing with remote debugging, serves a
minimal page from http://localhost (valid secure-context RP), attaches a virtual
authenticator via the CDP WebAuthn domain, and runs navigator.credentials.create()+get()
in the page. Prints PASS/FAIL. No network beyond localhost.
"""
import asyncio
import json
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import websockets

CHROME = "/usr/local/bin/chromium"
CDP_PORT = 9333
PAGE_PORT = 9334


class _Quiet(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<!doctype html><title>spikeA</title><body>ok</body>")

    def log_message(self, *a):
        pass


def _serve():
    HTTPServer(("127.0.0.1", PAGE_PORT), _Quiet).serve_forever()


# JS run in the page: register then authenticate with the virtual authenticator.
# Returns {ok, credId, asserted} or {error}.
_JS = r"""
(async () => {
  const enc = new TextEncoder();
  const chal = enc.encode("spike-challenge-0123456789");
  try {
    const cred = await navigator.credentials.create({ publicKey: {
      challenge: chal,
      rp: { id: "localhost", name: "spikeA" },
      user: { id: enc.encode("user-1"), name: "u@localhost", displayName: "u" },
      pubKeyCredParams: [{ type: "public-key", alg: -7 }, { type: "public-key", alg: -257 }],
      authenticatorSelection: { userVerification: "required" },
      timeout: 10000,
    }});
    const rawId = new Uint8Array(cred.rawId);
    const assertion = await navigator.credentials.get({ publicKey: {
      challenge: chal,
      rpId: "localhost",
      allowCredentials: [{ type: "public-key", id: rawId.buffer }],
      userVerification: "required",
      timeout: 10000,
    }});
    return JSON.stringify({
      ok: true,
      credId: cred.id,
      asserted: !!assertion && !!assertion.response,
    });
  } catch (e) {
    return JSON.stringify({ ok: false, error: String(e) });
  }
})()
"""


async def _cdp(ws, method, params=None, _id=[0]):
    _id[0] += 1
    mid = _id[0]
    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == mid:
            if "error" in msg:
                raise RuntimeError(f"{method} -> {msg['error']}")
            return msg.get("result", {})


async def run():
    threading.Thread(target=_serve, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="spikeA-")
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
        f"--remote-debugging-port={CDP_PORT}", "--remote-allow-origins=*",
        f"--user-data-dir={tmp}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # Wait for the page target's debugger URL.
        ws_url = None
        for _ in range(50):
            try:
                tabs = requests.get(f"http://127.0.0.1:{CDP_PORT}/json").json()
                pages = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if not ws_url:
            print("FAIL: never got a CDP page target")
            return 1

        async with websockets.connect(ws_url, max_size=None) as ws:
            await _cdp(ws, "Page.enable")
            await _cdp(ws, "Runtime.enable")
            await _cdp(ws, "WebAuthn.enable")
            va = await _cdp(ws, "WebAuthn.addVirtualAuthenticator", {"options": {
                "protocol": "ctap2", "transport": "internal",
                "hasResidentKey": True, "hasUserVerification": True,
                "isUserVerified": True, "automaticPresenceSimulation": True,
            }})
            print("virtual authenticator id:", va.get("authenticatorId"))

            # Navigate to the localhost RP origin and wait for load.
            await _cdp(ws, "Page.navigate", {"url": f"http://localhost:{PAGE_PORT}/"})
            await asyncio.sleep(1.0)

            res = await _cdp(ws, "Runtime.evaluate", {
                "expression": _JS, "awaitPromise": True, "returnByValue": True,
            })
            payload = json.loads(res.get("result", {}).get("value", "{}"))
            print("ceremony result:", payload)
            if payload.get("ok") and payload.get("asserted"):
                print("PASS: virtual authenticator completed create()+get() headlessly, no gesture")
                return 0
            print("FAIL: ceremony did not complete:", payload.get("error"))
            return 1
    finally:
        proc.terminate()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
