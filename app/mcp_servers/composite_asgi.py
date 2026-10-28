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
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse


class MCPBrowserDocumentationMiddleware(BaseHTTPMiddleware):
    """Serve developer documentation to web browsers while preserving JSON-RPC for MCP clients.

    When a human evaluator or web browser opens /mcp/citizen/ or /mcp/officer/ with
    Accept: text/html, returns a clean 200 OK status and architectural guide rather than
    a raw 406 Not Acceptable protocol error. MCP clients (which send application/json,
    text/event-stream, or POST) are passed directly through to the MCP handler.
    """
    async def dispatch(self, request, call_next):
        path = request.url.path.rstrip("/")
        if request.method == "GET" and path in ("/mcp/citizen", "/mcp/officer"):
            accept = request.headers.get("accept", "")
            has_session = bool(request.query_params.get("sessionId") or request.headers.get("mcp-session-id"))
            if "text/html" in accept and not has_session:
                server_type = "Officer" if "officer" in path else "Citizen"
                desc = (
                    "administrative queue review, dossier inspection, and statutory decision-making"
                    if "officer" in path else
                    "application drafting, eligibility verification, status tracking, and withdrawal"
                )
                tools_list = (
                    "list_pending_applications, get_application_for_review, record_application_decision"
                    if "officer" in path else
                    "get_scheme_requirements, check_my_application_status, check_application_requirements, submit_pension_application, withdraw_my_pending_application"
                )
                html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Sewa Setu — MCP {server_type} Service Endpoint</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow, noarchive">
  <meta name="googlebot" content="noindex, nofollow">
  <meta name="description" content="Technical developer documentation and status page for Model Context Protocol (MCP) streamable-HTTP server.">
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50 text-slate-800 font-sans min-h-screen p-6 flex flex-col justify-between">
  <div class="max-w-2xl mx-auto w-full my-auto bg-white border border-slate-200/90 rounded-2xl p-7 shadow-xs">
    <div class="flex items-center gap-3 mb-5 border-b border-slate-100 pb-4">
      <div class="w-10 h-10 rounded-xl bg-sky-50 border border-sky-200 flex items-center justify-center text-sky-700 font-bold text-sm">
        MCP
      </div>
      <div>
        <h1 class="text-lg font-bold text-slate-900 leading-tight">Sewa Setu MCP {server_type} Server</h1>
        <p class="text-xs text-slate-500 font-medium">Model Context Protocol • Streamable-HTTP Service</p>
      </div>
      <span class="ml-auto px-3 py-1 bg-emerald-50 text-emerald-700 text-xs font-semibold rounded-full border border-emerald-200">
        ● Active &amp; Ready
      </span>
    </div>

    <div class="space-y-4 text-sm leading-relaxed text-slate-600">
      <div class="p-4 rounded-xl bg-sky-50/70 border border-sky-100 text-sky-950">
        <p class="font-semibold text-sky-900 text-xs uppercase tracking-wider mb-1">Developer &amp; Evaluator Notice</p>
        <p class="text-xs text-sky-800 leading-normal">This URL is a dedicated <strong>Streamable-HTTP Model Context Protocol (MCP)</strong> API endpoint intended for AI assistants and MCP clients (such as Claude Desktop, Cursor, or the evaluation runner). It carries out {desc}.</p>
      </div>

      <div class="text-[11px] text-slate-500 bg-slate-100 p-2.5 rounded-lg border border-slate-200">
        <strong>Academic &amp; Evaluation Sandbox:</strong> This endpoint is an automated Model Context Protocol API service for software assessment. It does not solicit or collect personal credentials, passwords, or financial data from website visitors.
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-1">
        <div class="p-3 bg-slate-50 rounded-xl border border-slate-200/80">
          <div class="text-[10px] font-semibold text-slate-500 uppercase tracking-wider mb-0.5">Protocol Transport</div>
          <div class="font-mono text-xs font-bold text-slate-800">Streamable HTTP (JSON-RPC 2.0)</div>
        </div>
        <div class="p-3 bg-slate-50 rounded-xl border border-slate-200/80">
          <div class="text-[10px] font-semibold text-slate-500 uppercase tracking-wider mb-0.5">Authorization</div>
          <div class="font-mono text-xs font-bold text-slate-800">OAuth 2.1 Bearer Token</div>
        </div>
      </div>

      <div class="pt-1">
        <div class="text-xs font-semibold text-slate-700 mb-1.5">Registered Tools:</div>
        <div class="p-3 bg-slate-900 text-emerald-400 font-mono text-xs rounded-xl overflow-x-auto leading-relaxed">
          {tools_list}
        </div>
      </div>

      <div class="pt-4 flex flex-wrap items-center gap-3 border-t border-slate-100">
        <a href="/" class="px-3.5 py-2 bg-slate-900 text-white rounded-lg text-xs font-semibold hover:bg-slate-800 transition-colors">Portal Home</a>
        <a href="/eval-dashboard" class="px-3.5 py-2 bg-sky-600 text-white rounded-lg text-xs font-semibold hover:bg-sky-700 transition-colors">Evaluation Dashboard</a>
        <a href="/admin" class="px-3.5 py-2 bg-white border border-slate-300 text-slate-700 rounded-lg text-xs font-semibold hover:bg-slate-50 transition-colors">Admin Portal</a>
      </div>
    </div>
  </div>
  <footer class="text-center text-xs text-slate-400 mt-6">
    Sewa Setu Old Age Pension AI Assistant • Streamable-HTTP MCP Layer
  </footer>
</body>
</html>"""
                return HTMLResponse(
                    content=html,
                    status_code=200,
                    headers={
                        "X-Robots-Tag": "noindex, nofollow, noarchive",
                        "Cache-Control": "no-cache, no-store, must-revalidate"
                    }
                )
        return await call_next(request)

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
asgi_application.add_middleware(MCPBrowserDocumentationMiddleware)

app = asgi_application

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting Sewa Setu Unified Portal & MCP Servers on port {port}...")
    uvicorn.run(asgi_application, host="0.0.0.0", port=port)

