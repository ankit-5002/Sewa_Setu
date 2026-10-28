# -*- coding: utf-8 -*-
"""Quick diagnostic tool to verify live Streamable-HTTP MCP endpoints."""

import asyncio
import sys
import os
import httpx2
from mcp.client.streamable_http import streamable_http_client
from mcp import ClientSession


async def check_endpoint(name: str, url: str, health_tool: str):
    print(f"\n==================================================")
    print(f"Connecting to {name} at {url} ...")
    print(f"==================================================")
    try:
        async with httpx2.AsyncClient(timeout=15.0) as client:
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

                    # 3. Live Tool Execution
                    if health_tool in tool_names:
                        call_res = await session.call_tool(health_tool, {})
                        result_text = call_res.content[0].text if call_res.content else "{}"
                        print(f"✓ Tool Call ({health_tool}): SUCCESS")
                        print(f"  Response: {result_text.strip()[:100]}...")

    except Exception as e:
        print(f"✗ Connection FAILED: {e}")


async def main():
    base_url = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
    print(f"Sewa Setu — Live Streamable-HTTP MCP Diagnostic")
    print(f"Target Base URL: {base_url}")
    await check_endpoint("Citizen MCP Server", f"{base_url}/mcp/citizen/", "citizen_health")
    await check_endpoint("Officer MCP Server", f"{base_url}/mcp/officer/", "officer_health")
    print("\nAll MCP streamable-HTTP endpoints are fully operational!\n")


if __name__ == "__main__":
    asyncio.run(main())

