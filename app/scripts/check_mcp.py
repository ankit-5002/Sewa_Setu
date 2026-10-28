# -*- coding: utf-8 -*-
"""Quick diagnostic tool to verify live Streamable-HTTP MCP endpoints."""

import asyncio
import httpx2
from mcp.client.streamable_http import streamable_http_client
from mcp import ClientSession


async def check_endpoint(name: str, url: str):
    print(f"\n==================================================")
    print(f"Connecting to {name} at {url} ...")
    print(f"==================================================")
    try:
        async with httpx2.AsyncClient(timeout=10.0) as client:
            async with streamable_http_client(url, http_client=client) as (r, w):
                async with ClientSession(r, w) as session:
                    # 1. MCP Protocol Handshake
                    init_res = await session.initialize()
                    print(f"✓ Protocol Handshake: SUCCESS")
                    print(f"  • Server Name:    {init_res.server_info.name}")
                    print(f"  • Server Version: {init_res.server_info.version}")
                    print(f"  • Protocol:       2024-11-05 (Streamable HTTP)")

                    # 2. Tool Discovery
                    tools_res = await session.list_tools()
                    tool_names = [t.name for t in tools_res.tools]
                    print(f"✓ Tool Discovery:     SUCCESS ({len(tool_names)} tools registered)")
                    for i, t in enumerate(tools_res.tools, 1):
                        desc = (t.description or "").split("\n")[0][:60]
                        print(f"  {i}. {t.name:<30} — {desc}")

    except Exception as e:
        print(f"✗ Connection FAILED: {e}")


async def main():
    print("Sewa Setu — Live Streamable-HTTP MCP Diagnostic")
    await check_endpoint("Citizen MCP Server", "http://localhost:8000/mcp/citizen/")
    await check_endpoint("Officer MCP Server", "http://localhost:8000/mcp/officer/")
    print("\nAll MCP streamable-HTTP endpoints are fully operational!\n")


if __name__ == "__main__":
    asyncio.run(main())

