"""A tiny JSON-RPC client for talking to a running node over loopback HTTP.

Both the explorer and the wallet HUD are RPC clients and nothing more: they
hold no keys, no database and no chain state, and reach the node exactly the
way the CLI does. That shared conversation lives here, in one place, so the two
front-ends cannot drift apart in how they authenticate or parse a reply.

The node's RPC is loopback-only and token-authenticated (see obsidion/rpc.py);
the token is read from the data directory's cookie with `read_cookie`.
"""

from __future__ import annotations

import json
import urllib.request


class RPCClientError(LookupError):
    """The node accepted the request but returned an error result.

    Subclasses LookupError so existing callers that catch LookupError (the
    explorer's 404 handling) keep working unchanged.
    """


def rpc(port: int, method: str, *params, token: str = "", host: str = "127.0.0.1"):
    """Call one JSON-RPC method and return its result, or raise on error.

    Carries the node's auth token as a Bearer header and sends the
    application/json content type the node requires. Raises RPCClientError if
    the node reports an error, and lets urllib's own exceptions (a refused
    connection, a timeout) propagate so the caller can tell "node is down" from
    "node said no".
    """
    body = json.dumps({"method": method, "params": list(params), "id": 1}).encode()
    request = urllib.request.Request(
        f"http://{host}:{port}/",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        reply = json.loads(response.read())
    if reply.get("error"):
        raise RPCClientError(reply["error"]["message"])
    return reply["result"]
