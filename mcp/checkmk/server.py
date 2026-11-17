"""Read-only MCP server for the homelab Checkmk site (stdio transport).

Exposes live host/service state from the Checkmk REST API as MCP tools. It only
ever issues GET requests, so it cannot change rules, acknowledge problems or
activate changes.

Configuration (environment variables, never files in the repo):
  CHECKMK_URL     API base, default http://192.168.1.249:30500/homelab/check_mk/api/1.0
                  (LAN only: the public cmk.ferrinhouse.org basic auth collides with Bearer)
  CHECKMK_USER    automation user, ideally a read-only one such as grafana_ro
  CHECKMK_SECRET  that user's automation secret

stdout carries the MCP protocol, so all logging goes to stderr.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("checkmk-mcp")

DEFAULT_URL = "http://192.168.1.249:30500/homelab/check_mk/api/1.0"
MAX_ROWS = 200          # cap on rows returned to the model
MAX_OUTPUT_CHARS = 300  # cap on each plugin_output string
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

SERVICE_STATES = {0: "OK", 1: "WARN", 2: "CRIT", 3: "UNKNOWN"}
HOST_STATES = {0: "UP", 1: "DOWN", 2: "UNREACHABLE"}

mcp = FastMCP("checkmk")


# --------------------------------------------------------------------------- helpers

def _client() -> httpx.Client:
    user = os.environ.get("CHECKMK_USER")
    secret = os.environ.get("CHECKMK_SECRET")
    if not user or not secret:
        raise ToolError(
            "CHECKMK_USER and CHECKMK_SECRET are not set. Set them in the environment "
            "the MCP client starts this server with (see mcp/checkmk/README.md)."
        )
    return httpx.Client(
        base_url=os.environ.get("CHECKMK_URL", DEFAULT_URL).rstrip("/"),
        headers={"Authorization": f"Bearer {user} {secret}", "Accept": "application/json"},
        timeout=15.0,
    )


def _get(path: str, params: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """GET a Checkmk collection and return the `extensions` of each member."""
    try:
        with _client() as client:
            resp = client.get(path, params=params)
    except httpx.HTTPError as exc:
        raise ToolError(
            f"Could not reach Checkmk ({type(exc).__name__}). Is the LAN reachable and "
            "CHECKMK_URL correct? Default is the yoga-node NodePort 30500."
        ) from exc

    if resp.status_code == 401:
        raise ToolError("Checkmk rejected the credentials (401). Check CHECKMK_USER / CHECKMK_SECRET.")
    if resp.status_code == 404:
        raise ToolError(f"Not found (404) for {path}. Check the host name with list_hosts.")
    if resp.status_code >= 400:
        detail = resp.json().get("detail", resp.text[:200]) if resp.content else ""
        raise ToolError(f"Checkmk returned HTTP {resp.status_code}: {detail}")

    return [m.get("extensions", {}) for m in resp.json().get("value", [])]


def _trim(text: Any) -> str:
    text = str(text or "")
    return text if len(text) <= MAX_OUTPUT_CHARS else text[:MAX_OUTPUT_CHARS] + "…"


def _service_row(ext: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": ext.get("host_name"),
        "service": ext.get("description"),
        "state": SERVICE_STATES.get(ext.get("state"), str(ext.get("state"))),
        "acknowledged": bool(ext.get("acknowledged")),
        "output": _trim(ext.get("plugin_output")),
    }


def _capped(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"count": len(rows), "truncated": len(rows) > MAX_ROWS, "rows": rows[:MAX_ROWS]}


def _check_host(host: str) -> str:
    host = host.strip()
    if not HOST_RE.match(host):
        raise ToolError(f"'{host}' is not a valid Checkmk host name. Use list_hosts to see valid names.")
    return host


SERVICE_COLUMNS = [("columns", c) for c in ("host_name", "description", "state", "acknowledged", "plugin_output")]


# --------------------------------------------------------------------------- tools

@mcp.tool()
def list_hosts() -> dict[str, Any]:
    """List every host Checkmk monitors with its current state (UP/DOWN/UNREACHABLE) and service problem counts."""
    params = [("columns", c) for c in (
        "name", "state", "plugin_output", "num_services_warn", "num_services_crit", "num_services_unknown",
    )]
    rows = [
        {
            "host": e.get("name"),
            "state": HOST_STATES.get(e.get("state"), str(e.get("state"))),
            "warn": e.get("num_services_warn", 0),
            "crit": e.get("num_services_crit", 0),
            "unknown": e.get("num_services_unknown", 0),
            "output": _trim(e.get("plugin_output")),
        }
        for e in _get("/domain-types/host/collections/all", params)
    ]
    return _capped(sorted(rows, key=lambda r: r["host"] or ""))


@mcp.tool()
def get_service_problems(
    min_state: Literal["WARN", "CRIT"] = "WARN",
    include_acknowledged: bool = False,
) -> dict[str, Any]:
    """List services across all hosts that are not OK, worst first.

    min_state: WARN returns WARN, CRIT and UNKNOWN; CRIT returns only CRIT.
    include_acknowledged: also return problems someone has already acknowledged.
    """
    if min_state == "CRIT":
        state_expr: dict[str, Any] = {"op": "=", "left": "state", "right": "2"}
    else:
        state_expr = {"op": "!=", "left": "state", "right": "0"}
    exprs = [state_expr]
    if not include_acknowledged:
        exprs.append({"op": "=", "left": "acknowledged", "right": "0"})
    query = {"op": "and", "expr": exprs}

    rows = [_service_row(e) for e in _get(
        "/domain-types/service/collections/all",
        [("query", json.dumps(query)), *SERVICE_COLUMNS],
    )]
    # CRIT (2) first, then UNKNOWN (3), then WARN (1)
    order = {"CRIT": 0, "UNKNOWN": 1, "WARN": 2}
    rows.sort(key=lambda r: (order.get(r["state"], 9), r["host"] or "", r["service"] or ""))
    return _capped(rows)


@mcp.tool()
def get_host_services(
    host: str,
    only_problems: bool = False,
) -> dict[str, Any]:
    """List the services on one Checkmk host with their state and check output.

    host: the Checkmk host name exactly as list_hosts shows it (e.g. yoga-node, omv-nas).
    only_problems: return only services that are not OK.
    """
    host = _check_host(host)
    params = list(SERVICE_COLUMNS)
    if only_problems:
        params.append(("query", json.dumps({"op": "!=", "left": "state", "right": "0"})))
    rows = [_service_row(e) for e in _get(f"/objects/host/{host}/collections/services", params)]
    rows.sort(key=lambda r: r["service"] or "")
    return _capped(rows)


if __name__ == "__main__":
    mcp.run()  # stdio
