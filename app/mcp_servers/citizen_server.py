# -*- coding: utf-8 -*-
"""Sewa Setu Citizen MCP Server.

Dedicated streamable-HTTP MCP server providing tools exclusively for applicants
and family helpers. Discovers and executes ONLY citizen-scope tools.
"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from mcp_servers.auth import (
    MCPBearerTokenMiddleware,
    require_citizen_auth,
    AuthError
)
from services import portal_service

IST = ZoneInfo("Asia/Kolkata")


def create_citizen_server() -> MCPServer:
    """Create and configure the Citizen MCP server instance with citizen tools."""
    server = MCPServer(
        name="sewasetu-citizen",
        instructions=(
            "Official Sewa Setu Citizen Assistant MCP Server. "
            "Exposes tools strictly for pension applicants: checking requirements, "
            "checking own application status, submitting applications, and withdrawing pending ones."
        ),
        version="1.0.0"
    )

    @server.tool(name="citizen_health")
    def citizen_health() -> dict:
        """Health and readiness diagnostic for the Citizen MCP server.

        Returns server identity, active scheme details, and current IST time.
        Does not return applicant PII or sensitive data.
        """
        now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        return {
            "status": "healthy",
            "server": "sewasetu-citizen",
            "role": "citizen",
            "scheme": "Purvanchal Old Age Pension Scheme",
            "statutory_min_age": 60,
            "statutory_sla_days": 15,
            "timestamp": now,
        }

    @server.tool(name="get_scheme_requirements")
    def get_scheme_requirements() -> dict:
        """Explain authoritative scheme guidelines, eligibility criteria, and required information.

        Returns minimum age requirement (60 years or older), approved blocks, required personal,
        address, and bank details, required age-proof document specs, and statutory 15-day SLA policy.
        Does not require or disclose personal applicant information.
        """
        return portal_service.get_scheme_information()

    @server.tool(name="check_my_application_status")
    def check_my_application_status(application_reference: str = "", auth_token: str = "") -> dict:
        """Check status of old-age pension applications strictly owned by the authenticated citizen.

        The citizen identity is securely derived from the verified OAuth token. The caller cannot
        query or view another citizen's application. Returns application reference, status
        (PENDING, APPROVED, REJECTED, DEEMED_APPROVED, WITHDRAWN), submission timestamp, and
        official next steps. If rejected, returns the officer's recorded decision note.
        Redacts sensitive bank account numbers and document bytes.

        Args:
            application_reference: Optional application number (e.g. SSP26123456) or ID to filter.
            auth_token: Optional Bearer token string if not supplied in HTTP Authorization header.
        """
        try:
            auth = require_citizen_auth(custom_token=auth_token if auth_token else None)
        except AuthError as e:
            return {
                "status": "error",
                "error": e.error_code,
                "message": e.message
            }

        return portal_service.get_citizen_application_status(
            mobile=auth.mobile,
            app_reference=application_reference
        )

    @server.tool(name="check_application_requirements")
    def check_application_requirements(
        applicant_name: str = "",
        dob: str = "",
        gender: str = "",
        marital_status: str = "",
        husband_name: str = "",
        village: str = "",
        block: str = "",
        bank_account: str = "",
        ifsc: str = "",
        has_age_proof: bool = False,
        auth_token: str = ""
    ) -> dict:
        """Validate draft pension application details against official scheme rules.

        Checks age eligibility (applicant must be >= 60 in IST), approved block names,
        bank account digit length (9-18 digits), IFSC format, and age-proof presence.
        Read-only pre-submission validation; does not create a database application.

        Args:
            applicant_name: Full legal name of the applicant.
            dob: Date of birth formatted as DD/MM/YYYY.
            gender: 'Male', 'Female', or 'Other'.
            marital_status: Marital status (e.g., 'Married', 'Widowed', 'Single').
            husband_name: Husband's name if applicable.
            village: Name of residential village.
            block: Approved block (Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, Khelua).
            bank_account: Bank account number (9 to 18 digits).
            ifsc: Bank IFSC code (11 characters).
            has_age_proof: True if age-proof document is attached.
            auth_token: Optional Bearer token string.
        """
        try:
            require_citizen_auth(custom_token=auth_token if auth_token else None)
        except AuthError as e:
            return {
                "status": "error",
                "error": e.error_code,
                "message": e.message
            }

        draft = {
            "applicant_name": applicant_name,
            "dob": dob,
            "gender": gender,
            "marital_status": marital_status,
            "husband_name": husband_name,
            "village": village,
            "block": block,
            "bank_account": bank_account,
            "ifsc": ifsc,
            "doc_name": "age_proof_file.pdf" if has_age_proof else None
        }
        return portal_service.check_application_draft(draft)

    @server.tool(name="submit_pension_application")
    def submit_pension_application(
        applicant_name: str,
        dob: str,
        gender: str,
        marital_status: str,
        village: str,
        block: str,
        bank_account: str,
        ifsc: str,
        document_base64: str,
        document_filename: str = "age_proof.pdf",
        husband_name: str = "",
        auth_token: str = ""
    ) -> dict:
        """Submit a new official old age pension application.

        Enforces citizen ownership strictly from authenticated token claim (citizen:<mobile>).
        Validates eligibility (age >= 60 in IST), approved blocks, bank details, and
        verifies the uploaded age-proof document via magic bytes.
        Prevents duplicate applications if an active application already exists.

        Args:
            applicant_name: Legal full name of the applicant.
            dob: Date of birth formatted as DD/MM/YYYY. Applicant must be 60 years or older.
            gender: 'Male', 'Female', or 'Other'.
            marital_status: Marital status (e.g. 'Married', 'Widowed', 'Single').
            village: Name of residential village.
            block: Approved block (Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, Khelua).
            bank_account: Bank account number (9 to 18 digits).
            ifsc: Bank IFSC code (11 characters).
            document_base64: Base64-encoded file contents of age-proof document (PDF, JPG, or PNG, max 5MB).
            document_filename: Optional filename of the document (e.g. 'aadhaar.pdf', 'voter_id.jpg').
            husband_name: Husband's name if applicable (required if married/widowed woman).
            auth_token: Optional Bearer token string if not supplied in HTTP Authorization header.
        """
        try:
            auth = require_citizen_auth(custom_token=auth_token if auth_token else None)
        except AuthError as e:
            return {
                "status": "error",
                "error": e.error_code,
                "message": e.message
            }

        app_data = {
            "applicant_name": applicant_name,
            "dob": dob,
            "gender": gender,
            "marital_status": marital_status,
            "husband_name": husband_name,
            "village": village,
            "block": block,
            "bank_account": bank_account,
            "ifsc": ifsc
        }

        return portal_service.submit_citizen_application(
            mobile=auth.mobile,
            data=app_data,
            document_base64=document_base64,
            document_filename=document_filename
        )

    @server.tool(name="withdraw_my_pending_application")
    def withdraw_my_pending_application(
        application_reference: str = "",
        auth_token: str = ""
    ) -> dict:
        """Withdraw an existing PENDING pension application owned by the authenticated citizen.

        Under scheme rules, only an application with PENDING status can be withdrawn.
        Applications that are already Approved, Deemed Approved, or Rejected cannot be withdrawn.
        Once withdrawn, the citizen is immediately eligible to submit a fresh application.

        Args:
            application_reference: Optional application number (e.g. 'SSP26123456') or ID. If omitted, targets the citizen's current pending application.
            auth_token: Optional Bearer token string if not supplied in HTTP Authorization header.
        """
        try:
            auth = require_citizen_auth(custom_token=auth_token if auth_token else None)
        except AuthError as e:
            return {
                "status": "error",
                "error": e.error_code,
                "message": e.message
            }

        return portal_service.withdraw_citizen_application(
            mobile=auth.mobile,
            app_reference=application_reference if application_reference.strip() else None
        )

    @server.tool(name="citizen_auth_test")
    def citizen_auth_test(auth_token: str = "") -> dict:
        """Verify citizen authorization. Returns authenticated citizen identity or structured error."""
        try:
            auth = require_citizen_auth(custom_token=auth_token if auth_token else None)
            return {
                "status": "authenticated",
                "role": auth.role,
                "scope": auth.scope,
                "subject": auth.subject,
                "mobile": auth.mobile,
                "audience": auth.audience,
            }
        except AuthError as e:
            return {
                "status": "error",
                "error": e.error_code,
                "message": e.message
            }

    return server


citizen_server = create_citizen_server()


def create_citizen_asgi_app(streamable_http_path: str = "/"):
    """Create the Starlette ASGI streamable HTTP application for Citizen MCP."""
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = citizen_server.streamable_http_app(
        streamable_http_path=streamable_http_path,
        transport_security=security,
        max_request_body_size=10 * 1024 * 1024
    )
    app.add_middleware(MCPBearerTokenMiddleware)
    return app


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("CITIZEN_MCP_PORT", 8001))
    print(f"Starting standalone Sewa Setu Citizen MCP Server on port {port}...")
    app = create_citizen_asgi_app(streamable_http_path="/mcp")
    uvicorn.run(app, host="0.0.0.0", port=port)

