# Checkmk MCP server (read-only)

A small [Model Context Protocol](https://modelcontextprotocol.io) server that gives an MCP client (Claude Code,
Claude Desktop) live read access to the homelab Checkmk site. It is a learning project: one Python file,
stdio transport, GET requests only.

> **Not deployed by CI.** `mcp/**` is outside the paths `ci.yaml` watches. It runs on the workstation, started by
> the MCP client, and talks to the Checkmk REST API over the LAN.

## Tools

| Tool | Arguments | Returns |
| :--- | :--- | :--- |
| `list_hosts` | none | Every host, its UP/DOWN state and WARN/CRIT/UNKNOWN service counts |
| `get_service_problems` | `min_state` (`WARN`/`CRIT`), `include_acknowledged` | Non-OK services across all hosts, worst first |
| `get_host_services` | `host`, `only_problems` | Services on one host with state and check output |

Output is capped at 200 rows and 300 characters of check output per row. Errors come back as tool errors with
a hint (bad credentials, unknown host, LAN unreachable), not stack traces.

## How it maps to the API

- Base URL: `http://192.168.1.249:30500/homelab/check_mk/api/1.0` (LAN only; the public URL's basic auth
  collides with the Bearer header).
- Auth header: `Authorization: Bearer <user> <secret>`.
- Live state comes from `GET /domain-types/host/collections/all`, `/domain-types/service/collections/all` and
  `/objects/host/<name>/collections/services`, with `columns=` and a Livestatus-style `query=` JSON filter.
- Service state: 0 OK, 1 WARN, 2 CRIT, 3 UNKNOWN. Host state: 0 UP, 1 DOWN, 2 UNREACHABLE.

## Credentials

Use a **read-only** automation user, not `automation`. The existing `grafana_ro` (guest role) user is enough.
Its secret is in Bitwarden. Pass it through environment variables; never write it into a file in this repo.

| Variable | Default |
| :--- | :--- |
| `CHECKMK_URL` | `http://192.168.1.249:30500/homelab/check_mk/api/1.0` |
| `CHECKMK_USER` | (required) |
| `CHECKMK_SECRET` | (required) |

## Setup (Windows)

```powershell
cd mcp\checkmk
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Smoke test (lists the tools and calls each one, including two invalid inputs):

```powershell
$env:CHECKMK_USER = 'grafana_ro'; $env:CHECKMK_SECRET = '<from Bitwarden>'
.\.venv\Scripts\python.exe smoke_test.py
```

Interactive testing with the MCP Inspector (needs Node):

```powershell
npx @modelcontextprotocol/inspector .\.venv\Scripts\python.exe server.py
```

## Register with Claude Code

User scope, so the secret stays in your own config and out of the repo:

```powershell
claude mcp add checkmk --scope user `
  -e CHECKMK_USER=grafana_ro -e CHECKMK_SECRET=<from Bitwarden> `
  -- C:\Users\whwar\GitHubRepos\homelab-ops\mcp\checkmk\.venv\Scripts\python.exe `
     C:\Users\whwar\GitHubRepos\homelab-ops\mcp\checkmk\server.py
```

Then start a new session (clients cache the tool list) and run `claude mcp list` to confirm it connects.

## Ideas for the next steps

- **Resources:** expose each host as `checkmk://host/<name>` so a client can attach it as context.
- **A prompt:** "morning health check" that calls `list_hosts` + `get_service_problems` and summarizes.
- **A write tool:** `acknowledge_problem` (POST `/domain-types/acknowledge/collections/service`) behind a separate
  user with more rights. Learn how MCP tool annotations (`readOnlyHint`, `destructiveHint`) mark it.
- **Remote transport:** switch `mcp.run()` to `transport="streamable-http"` and run it in k3s with auth in front.
