# -*- coding: utf-8 -*-
"""Composite ASGI Application for Sewa Setu.

Combines the Flask portal (via WsgiToAsgi) and the two independent streamable-HTTP
MCP servers (Citizen MCP and Officer MCP) under a unified ASGI router.
"""

import sys
import os
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.routing import Mount
from asgiref.wsgi import WsgiToAsgi

# Ensure app package is importable
app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

from app import app as flask_portal_app
from mcp_servers.citizen_server import create_citizen_asgi_app, citizen_server
from mcp_servers.officer_server import create_officer_asgi_app, officer_server
from mcp_servers.auth import MCPBearerTokenMiddleware

citizen_asgi = create_citizen_asgi_app(streamable_http_path="/")
officer_asgi = create_officer_asgi_app(streamable_http_path="/")
portal_asgi = WsgiToAsgi(flask_portal_app)


@asynccontextmanager
async def composite_lifespan(app):
    """Manage lifespans for both citizen and officer streamable HTTP servers."""
    async with citizen_asgi.router.lifespan_context(citizen_asgi), \
               officer_asgi.router.lifespan_context(officer_asgi):
        yield


# Unified Starlette application mounting independent MCP servers and Flask portal
asgi_application = Starlette(
    routes=[
        Mount("/mcp/citizen", citizen_asgi),
        Mount("/mcp/officer", officer_asgi),
        Mount("/", portal_asgi),
    ],
    lifespan=composite_lifespan
)
asgi_application.add_middleware(MCPBearerTokenMiddleware)

app = asgi_application

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting Sewa Setu Unified Portal & MCP Servers on port {port}...")
    uvicorn.run(asgi_application, host="0.0.0.0", port=port)

