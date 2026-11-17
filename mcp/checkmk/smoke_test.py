"""Smoke test: start server.py over stdio, list its tools, and call them.

    python smoke_test.py

Without CHECKMK_USER/CHECKMK_SECRET set, the live calls are expected to come back
as tool errors with a hint; with them set they should return real data.
"""

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).with_name("server.py")


def show(label, result):
    text = " ".join(getattr(c, "text", "") for c in result.content)
    flag = "ERROR" if result.isError else "ok"
    print(f"[{flag}] {label}: {text[:400]}")


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))

            show("list_hosts", await session.call_tool("list_hosts", {}))
            show("get_service_problems", await session.call_tool("get_service_problems", {"min_state": "WARN"}))
            show("get_host_services(yoga-node)", await session.call_tool(
                "get_host_services", {"host": "yoga-node", "only_problems": True}))

            # Invalid input: should be rejected with a hint, not a stack trace
            show("invalid host name", await session.call_tool("get_host_services", {"host": "../etc/passwd"}))
            show("invalid enum", await session.call_tool("get_service_problems", {"min_state": "PANIC"}))


if __name__ == "__main__":
    asyncio.run(main())
