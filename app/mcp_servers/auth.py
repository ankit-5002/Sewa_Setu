# -*- coding: utf-8 -*-
"""Sewa Setu MCP OAuth Validation & Security Boundary.

Verifies RS256 JWT access tokens issued by the Sewa Setu portal.
Validates:
1. Cryptographic signature against portal RSA key / JWKS.
2. Canonical audience (RFC 8707 Resource Indicators):
   - Citizen MCP: ${PUBLIC_BASE_URL}/mcp/citizen
   - Officer MCP: ${PUBLIC_BASE_URL}/mcp/officer
3. Token issuer (iss == PUBLIC_BASE_URL).
4. Expiration (exp) and absolute session expiration (session_ends).
5. Role & scope match (citizen vs. officer).
6. Subject format (citizen:<mobile> vs. officer:<username>).
7. Program token revocation checking against database (oauth_program_tokens.revoked_at).
"""

import sys
import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any
from contextvars import ContextVar

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# Ensure app package is importable
app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

import app as portal_app
import oauth as oauth_mod

# ContextVar storing the current request's raw Bearer token string
current_token_var: ContextVar[Optional[str]] = ContextVar("current_mcp_bearer_token", default=None)


class MCPBearerTokenMiddleware(BaseHTTPMiddleware):
    """ASGI Middleware to extract HTTP Authorization: Bearer <token> into contextvar."""

    async def dispatch(self, request: Request, call_next):
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            current_token_var.set(auth_header[7:].strip())
        else:
            current_token_var.set(None)
        return await call_next(request)


@dataclass
class AuthContext:
    """Authenticated caller identity verified from RS256 JWT."""
    subject: str
    role: str
    scope: str
    audience: str
    client_id: str
    claims: Dict[str, Any]
    mobile: Optional[str] = None
    officer_username: Optional[str] = None
    is_program_token: bool = False


class AuthError(Exception):
    """Exception raised when token verification or authorization fails."""
    def __init__(self, error_code: str, message: str, status_code: int = 401):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code


def get_canonical_audience(role: str) -> str:
    """Return the RFC 8707 canonical resource audience for the requested role."""
    base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    if role == "citizen":
        return os.environ.get("CITIZEN_MCP_AUDIENCE", f"{base}/mcp/citizen")
    elif role == "officer":
        return os.environ.get("OFFICER_MCP_AUDIENCE", f"{base}/mcp/officer")
    return base


def verify_token_claims(
    token_str: str,
    expected_audience: str,
    expected_role: str
) -> Tuple[bool, Optional[AuthContext], str]:
    """Verify an RS256 token against the portal's JWKS and scheme authorization rules.

    Returns:
        (is_valid, auth_context, error_message)
    """
    if not token_str:
        return False, None, "Missing Bearer token in Authorization header."

    # 1. Cryptographic signature and standard claims validation
    try:
        kid, priv = oauth_mod.signing_key()
        issuer = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
        claims = jwt.decode(
            token_str,
            priv.public_key(),
            algorithms=["RS256"],
            audience=expected_audience,
            issuer=issuer,
            options={"require": ["exp", "iss", "aud", "sub"]}
        )
    except jwt.ExpiredSignatureError:
        return False, None, "Token has expired."
    except jwt.InvalidAudienceError:
        return False, None, f"Token audience does not match canonical resource: {expected_audience}."
    except jwt.InvalidIssuerError:
        return False, None, f"Token issuer is invalid."
    except jwt.PyJWTError as e:
        return False, None, f"JWT signature verification failed: {e}."
    except Exception as e:
        return False, None, f"Unexpected token verification error: {e}."

    # 2. Check absolute session ceiling (session_ends)
    now_ts = int(time.time())
    session_ends = claims.get("session_ends")
    if session_ends and now_ts > session_ends:
        return False, None, "User session has reached its absolute expiration time."

    # 3. Check role and scope alignment
    role = claims.get("role", "")
    if role != expected_role:
        return False, None, f"Insufficient role: token role '{role}' does not have access (expected '{expected_role}')."

    scope = claims.get("scope", "")
    if scope != expected_role:
        return False, None, f"Insufficient scope: '{scope}' (expected '{expected_role}')."

    # 4. Check subject format
    sub = claims.get("sub", "")
    mobile = None
    officer_user = None

    if expected_role == "citizen":
        if not sub.startswith("citizen:"):
            return False, None, f"Invalid citizen subject format: '{sub}'."
        mobile = sub.split(":", 1)[1].strip()
        if len(mobile) != 10 or not mobile.isdigit():
            return False, None, f"Invalid citizen mobile number in token subject: '{mobile}'."
    elif expected_role == "officer":
        if not sub.startswith("officer:"):
            return False, None, f"Invalid officer subject format: '{sub}'."
        officer_user = sub.split(":", 1)[1].strip()
        if not officer_user:
            return False, None, "Invalid empty officer username in token subject."

    # 5. Program token revocation enforcement
    is_program = claims.get("kind") == "program"
    if is_program:
        jti = claims.get("jti")
        try:
            conn = portal_app.get_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT revoked_at, expires_at FROM oauth_program_tokens WHERE jti = %s",
                (jti,)
            )
            row = cur.fetchone()
            cur.close()
            conn.close()
            if not row:
                return False, None, "Program token was not found in registered database records."
            revoked_at, db_expires_at = row
            if revoked_at is not None:
                return False, None, "Program token has been revoked by an administrator."
        except Exception as e:
            # If database check fails, fail closed for security
            return False, None, f"Database revocation check failed: {e}"

    context = AuthContext(
        subject=sub,
        role=role,
        scope=scope,
        audience=claims.get("aud", expected_audience),
        client_id=claims.get("client_id", ""),
        claims=claims,
        mobile=mobile,
        officer_username=officer_user,
        is_program_token=is_program
    )
    return True, context, ""


def require_auth(expected_role: str, custom_token: Optional[str] = None) -> AuthContext:
    """Enforce authorization for a tool call.

    Derives token from current HTTP contextvar or optional custom_token parameter.
    Raises AuthError if authentication or authorization fails.
    """
    token_str = custom_token or current_token_var.get()
    expected_aud = get_canonical_audience(expected_role)
    is_valid, ctx, err_msg = verify_token_claims(token_str, expected_aud, expected_role)
    if not is_valid:
        error_code = "FORBIDDEN" if "Insufficient" in err_msg or "audience" in err_msg else "UNAUTHENTICATED"
        raise AuthError(error_code=error_code, message=err_msg)
    return ctx


def require_citizen_auth(custom_token: Optional[str] = None) -> AuthContext:
    """Enforce citizen authorization. Returns verified AuthContext with citizen mobile."""
    return require_auth("citizen", custom_token)


def require_officer_auth(custom_token: Optional[str] = None) -> AuthContext:
    """Enforce officer authorization. Returns verified AuthContext with officer username."""
    return require_auth("officer", custom_token)

