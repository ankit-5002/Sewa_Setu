# -*- coding: utf-8 -*-
"""Sewa Setu Officer MCP Server.

Dedicated streamable-HTTP MCP server providing tools exclusively for departmental
officers. Discovers and executes ONLY officer-scope tools (queue, inspection, decisions).
"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from mcp_servers.auth import (
    MCPBearerTokenMiddleware,
    require_officer_auth,
    AuthError
)
from services import portal_service

IST = ZoneInfo("Asia/Kolkata")


def create_officer_server() -> MCPServer:
    """Create and configure the Officer MCP server instance with officer tools."""
    server = MCPServer(
        name="sewasetu-officer",
        instructions=(
            "Official Sewa Setu Officer Assistant MCP Server. "
            "Exposes tools strictly for authorized departmental officers: reviewing pending "
            "applications, inspecting applicant case dossiers and documents, and recording "
            "approval or rejection decisions with mandatory audit reasons."
        ),
        version="1.0.0"
    )

    @server.tool(name="officer_health")
    def officer_health() -> dict:
        """Health and readiness diagnostic for the Officer MCP server.

        Returns officer server identity, supported jurisdictional blocks, and current IST time.
        Does not return applicant PII or sensitive data.
        """
        now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        return {
            "status": "healthy",
            "server": "sewasetu-officer",
            "role": "officer",
            "jurisdiction": "Department of Social Welfare, Government of Purvanchal",
            "supported_blocks": [
                "Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"
            ],
            "statutory_sla_days": 15,
            "timestamp": now,
        }

    @server.tool(name="officer_auth_test")
    def officer_auth_test(auth_token: str = "") -> dict:
        """Verify officer authorization. Returns authenticated officer identity or structured error."""
        try:
            auth = require_officer_auth(custom_token=auth_token if auth_token else None)
            return {
                "status": "authenticated",
                "role": auth.role,
                "scope": auth.scope,
                "subject": auth.subject,
                "officer_username": auth.officer_username,
                "audience": auth.audience,
                "is_program_token": auth.is_program_token
            }
        except AuthError as e:
            return {
                "status": "error",
                "error": e.error_code,
                "message": e.message
            }

    @server.tool(name="list_pending_applications")
    def list_pending_applications(
        block: str = "",
        limit: int = 20,
        offset: int = 0,
        auth_token: str = ""
    ) -> dict:
        """Retrieve paginated list of pending pension applications for officer review.

        Restricted strictly to departmental officers. Returns FIFO-ordered pending applications
        to assist in complying with the 15-day statutory SLA under the Right to Public Services Act.
        Redacts sensitive bank accounts and document bytes for data minimization.

        Args:
            block: Optional filter by block (Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, Khelua).
            limit: Maximum number of applications to return (clamped between 1 and 50, default: 20).
            offset: Pagination offset (default: 0).
            auth_token: Optional Bearer token string if not supplied in HTTP Authorization header.
        """
        try:
            require_officer_auth(custom_token=auth_token if auth_token else None)
        except AuthError as e:
            return {
                "status": "error",
                "error": e.error_code,
                "message": e.message
            }

        return portal_service.get_pending_queue(
            block=block if block.strip() else None,
            limit=limit,
            offset=offset
        )

    @server.tool(name="get_application_for_review")
    def get_application_for_review(
        application_reference: str,
        auth_token: str = ""
    ) -> dict:
        """Retrieve complete case dossier, eligibility criteria, document metadata, and audit log.

        Restricted strictly to departmental officers. Evaluates statutory age eligibility in IST,
        calculates pending duration against SLA, inspects document verification metadata,
        and displays full historical audit trail.

        Args:
            application_reference: Application number (e.g. 'SSP26123456') or numeric application ID.
            auth_token: Optional Bearer token string if not supplied in HTTP Authorization header.
        """
        try:
            require_officer_auth(custom_token=auth_token if auth_token else None)
        except AuthError as e:
            return {
                "status": "error",
                "error": e.error_code,
                "message": e.message
            }

        return portal_service.get_application_dossier(
            app_reference=application_reference
        )

    @server.tool(name="record_application_decision")
    def record_application_decision(
        application_reference: str,
        decision: str,
        reason: str = "",
        auth_token: str = ""
    ) -> dict:
        """Record an official approval or rejection decision on a PENDING pension application.

        Restricted strictly to departmental officers. Under administrative rules:
        - Only applications in PENDING status can be decided.
        - Rejection requires a mandatory, detailed justification note for the citizen.
        - Decisions are atomically guarded against stale state and concurrent officer actions.
        - Records officer username in decided_by and appends an immutable audit log row.

        Args:
            application_reference: Application number (e.g. 'SSP26123456') or numeric application ID.
            decision: 'APPROVE' or 'REJECT' (case-insensitive).
            reason: Justification note (MANDATORY for rejections, optional for approvals).
            auth_token: Optional Bearer token string if not supplied in HTTP Authorization header.
        """
        try:
            auth = require_officer_auth(custom_token=auth_token if auth_token else None)
        except AuthError as e:
            return {
                "status": "error",
                "error": e.error_code,
                "message": e.message
            }

        return portal_service.record_officer_decision(
            app_reference=application_reference,
            decision=decision,
            actor_username=auth.officer_username,
            reason=reason
        )

    return server


officer_server = create_officer_server()


def create_officer_asgi_app(streamable_http_path: str = "/"):
    """Create the Starlette ASGI streamable HTTP application for Officer MCP."""
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = officer_server.streamable_http_app(
        streamable_http_path=streamable_http_path,
        transport_security=security,
        max_request_body_size=10 * 1024 * 1024
    )
    app.add_middleware(MCPBearerTokenMiddleware)
    return app


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("OFFICER_MCP_PORT", 8002))
    print(f"Starting standalone Sewa Setu Officer MCP Server on port {port}...")
    app = create_officer_asgi_app(streamable_http_path="/mcp")
    uvicorn.run(app, host="0.0.0.0", port=port)

