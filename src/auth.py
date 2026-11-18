"""Role-Based Access Control (RBAC) and OAuth token refresh for ops-bridge-mcp."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from typing import Any, Iterator, Mapping

import httpx
import jwt

from src.telemetry import log_audit_event

JWT_ISSUER = "ops-bridge-idp"


class JWTConfigurationError(RuntimeError):
    """Raised when JWT signing/verification is attempted without OPS_JWT_SECRET configured."""


def _get_jwt_secret(secret: str | None = None) -> str:
    """Return the HS256 signing key.

    There is intentionally no hard-coded fallback: a default secret committed to a
    public repository would let anyone mint an ``incident_commander`` token.
    """
    key = secret if secret is not None else os.environ.get("OPS_JWT_SECRET")
    if not key or not key.strip():
        raise JWTConfigurationError(
            "OPS_JWT_SECRET is not configured; cannot sign or verify operator JWTs."
        )
    return key


class OperatorRole(str, Enum):
    """Supported RBAC operator roles in increasing order of privilege."""

    FIELD_VIEWER = "field_viewer"
    OPS_ENGINEER = "ops_engineer"
    INCIDENT_COMMANDER = "incident_commander"


ROLE_PRIVILEGE_RANK: dict[str, int] = {
    OperatorRole.FIELD_VIEWER.value: 0,
    OperatorRole.OPS_ENGINEER.value: 1,
    OperatorRole.INCIDENT_COMMANDER.value: 2,
}


ROLE_PERMISSION_MATRIX: dict[str, frozenset[str]] = {
    OperatorRole.FIELD_VIEWER.value: frozenset(
        {
            "query_telemetry",
            "search_runbooks",
            "diagnose_and_triage_incident",
        }
    ),
    OperatorRole.OPS_ENGINEER.value: frozenset(
        {
            "query_telemetry",
            "search_runbooks",
            "diagnose_and_triage_incident",
            "create_work_order",
            "replay_outbox_actions",
        }
    ),
    OperatorRole.INCIDENT_COMMANDER.value: frozenset(
        {
            "query_telemetry",
            "search_runbooks",
            "diagnose_and_triage_incident",
            "create_work_order",
            "escalate_line_stoppage",
            "replay_outbox_actions",
        }
    ),
}

_operator_role_ctx: ContextVar[str | None] = ContextVar("ops_operator_role", default=None)
_operator_id_ctx: ContextVar[str] = ContextVar("ops_operator_id", default="fde-operator")
_operator_jwt_ctx: ContextVar[str | None] = ContextVar("ops_operator_jwt", default=None)
_request_headers_ctx: ContextVar[Mapping[str, str] | None] = ContextVar(
    "ops_request_headers", default=None
)


def issue_operator_jwt(
    role: str,
    operator_id: str = "fde-operator",
    secret: str | None = None,
    expires_in_seconds: int = 3600,
) -> str:
    """Issue a signed HS256 JWT asserting operator identity and RBAC role."""
    now = int(time.time())
    payload = {
        "sub": operator_id,
        "role": role.strip().lower(),
        "iat": now,
        "exp": now + int(expires_in_seconds),
        "iss": JWT_ISSUER,
    }
    return jwt.encode(payload, _get_jwt_secret(secret), algorithm="HS256")


def verify_operator_jwt(token: str, secret: str | None = None) -> dict[str, Any]:
    """Verify HS256 operator JWT signature, expiry, issuer, and required claims."""
    claims = jwt.decode(
        token,
        _get_jwt_secret(secret),
        algorithms=["HS256"],
        issuer=JWT_ISSUER,
        options={"require": ["exp", "sub", "role", "iss"]},
    )
    return dict(claims)


def _extract_header_ci(headers: Mapping[str, str] | None, target: str) -> str | None:
    if not headers:
        return None
    target_lower = target.lower()
    for key, value in headers.items():
        if key.lower() == target_lower and value is not None and str(value).strip():
            return str(value).strip()
    return None


def _extract_role_from_headers(headers: Mapping[str, str] | None) -> str | None:
    val = _extract_header_ci(headers, "x-operator-role")
    return val.lower() if val else None


def _resolve_active_jwt_claims(
    headers: Mapping[str, str] | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    token = (
        _extract_header_ci(headers, "x-operator-token")
        or _operator_jwt_ctx.get()
        or _extract_header_ci(_request_headers_ctx.get(), "x-operator-token")
        or os.environ.get("OPS_OPERATOR_JWT")
    )
    if not token or not token.strip():
        return False, None
    try:
        claims = verify_operator_jwt(token.strip())
        return True, claims
    except (jwt.InvalidTokenError, JWTConfigurationError, ValueError):
        return True, None


def resolve_operator_role(
    explicit_role: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> str:
    """Resolve the effective operator role.

    The *trusted* role comes from a signed JWT, request headers, the operator context,
    or server environment. ``explicit_role`` is a tool argument supplied by the MCP
    client/LLM and is therefore untrusted: it may only *narrow* privileges (e.g. an
    incident commander acting as a field viewer), never widen them. Unknown explicit
    roles are returned as-is so RBAC fails closed.
    """
    trusted_role = _resolve_trusted_role(headers=headers)

    if explicit_role is None or not str(explicit_role).strip():
        return trusted_role

    requested = str(explicit_role).strip().lower()
    if requested not in ROLE_PRIVILEGE_RANK:
        return requested  # fail closed in check_rbac_permission
    trusted_rank = ROLE_PRIVILEGE_RANK.get(trusted_role)
    if trusted_rank is None:
        return trusted_role  # e.g. invalid_jwt_signature stays denied
    if ROLE_PRIVILEGE_RANK[requested] <= trusted_rank:
        return requested
    return trusted_role


def _resolve_trusted_role(headers: Mapping[str, str] | None = None) -> str:
    """Resolve role from server-trusted sources only (JWT, headers, context, env)."""
    has_jwt, claims = _resolve_active_jwt_claims(headers=headers)
    if has_jwt:
        if claims and claims.get("role"):
            return str(claims["role"]).strip().lower()
        return "invalid_jwt_signature"

    header_role = _extract_role_from_headers(headers)
    if header_role:
        return header_role

    ctx_role = _operator_role_ctx.get()
    if ctx_role:
        return ctx_role.strip().lower()

    ctx_header_role = _extract_role_from_headers(_request_headers_ctx.get())
    if ctx_header_role:
        return ctx_header_role

    for env_key in ("X_OPERATOR_ROLE", "X-Operator-Role", "OPS_OPERATOR_ROLE"):
        env_val = os.environ.get(env_key)
        if env_val and env_val.strip():
            return env_val.strip().lower()

    return OperatorRole.FIELD_VIEWER.value


def resolve_operator_id(headers: Mapping[str, str] | None = None) -> str:
    """Resolve operator identity for audit logging."""
    has_jwt, claims = _resolve_active_jwt_claims(headers=headers)
    if has_jwt and claims and claims.get("sub"):
        return str(claims["sub"])

    hdr_id = _extract_header_ci(headers, "x-operator-id")
    if hdr_id:
        return hdr_id
    ctx_hdr_id = _extract_header_ci(_request_headers_ctx.get(), "x-operator-id")
    if ctx_hdr_id:
        return ctx_hdr_id
    return _operator_id_ctx.get()


@contextmanager
def operator_context(
    role: str,
    operator_id: str = "fde-operator",
    headers: Mapping[str, str] | None = None,
    jwt_token: str | None = None,
) -> Iterator[None]:
    """Context manager scoping operator role/JWT/headers for async or sync execution."""
    role_token = _operator_role_ctx.set(role)
    id_token = _operator_id_ctx.set(operator_id)
    jwt_ctx_token = _operator_jwt_ctx.set(jwt_token)
    hdr_token = _request_headers_ctx.set(dict(headers) if headers is not None else None)
    try:
        yield
    finally:
        _operator_role_ctx.reset(role_token)
        _operator_id_ctx.reset(id_token)
        _operator_jwt_ctx.reset(jwt_ctx_token)
        _request_headers_ctx.reset(hdr_token)


def check_rbac_permission(
    tool_name: str,
    explicit_role: str | None = None,
    headers: Mapping[str, str] | None = None,
    trace_id: str | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Enforce RBAC permission matrix and return structured MCP error payload on deny."""
    started = time.perf_counter()
    role = resolve_operator_role(explicit_role=explicit_role, headers=headers)
    operator_id = resolve_operator_id(headers=headers)

    allowed_tools = ROLE_PERMISSION_MATRIX.get(role)
    if allowed_tools is None:
        latency_ms = (time.perf_counter() - started) * 1000.0
        error_payload = {
            "status": "ERROR",
            "error_code": "RBAC_FORBIDDEN",
            "tool_name": tool_name,
            "operator_role": role,
            "message": (
                f"Access denied: unrecognized operator role '{role}'. "
                f"Valid roles are: {', '.join(r.value for r in OperatorRole)}."
            ),
            "required_roles": [
                r for r, tools in ROLE_PERMISSION_MATRIX.items() if tool_name in tools
            ],
        }
        log_audit_event(
            tool_name=tool_name,
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="DENY",
            latency_ms=latency_ms,
            status="FORBIDDEN",
            trace_id=trace_id,
            metadata={"reason": "unknown_role"},
        )
        return False, role, error_payload

    if tool_name not in allowed_tools:
        latency_ms = (time.perf_counter() - started) * 1000.0
        required_roles = [
            r for r, tools in ROLE_PERMISSION_MATRIX.items() if tool_name in tools
        ]
        error_payload = {
            "status": "ERROR",
            "error_code": "RBAC_FORBIDDEN",
            "tool_name": tool_name,
            "operator_role": role,
            "allowed_tools_for_role": sorted(allowed_tools),
            "required_roles": required_roles,
            "message": (
                f"Access denied: role '{role}' is not authorized to execute '{tool_name}'. "
                f"Required role privilege: {', '.join(required_roles)}."
            ),
        }
        log_audit_event(
            tool_name=tool_name,
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="DENY",
            latency_ms=latency_ms,
            status="FORBIDDEN",
            trace_id=trace_id,
            metadata={"required_roles": required_roles},
        )
        return False, role, error_payload

    return True, role, None


class TokenRefreshError(RuntimeError):
    """Raised when OAuth bearer token refresh fails."""


class OAuthTokenManager:
    """Manages OAuth2 bearer tokens with transparent single-retry refresh on HTTP 401."""

    def __init__(
        self,
        token_url: str = "/auth/token",
        client_id: str = "ops-bridge-fde",
        client_secret: str = "fde-manufacturing-secret",
        initial_token: str | None = None,
    ) -> None:
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token: str = (
            initial_token
            if initial_token is not None
            else os.environ.get("ITSM_INITIAL_TOKEN", "expired-bootstrap-token")
        )
        self.refresh_count: int = 0
        self._lock = asyncio.Lock()

    async def refresh_token(self, client: httpx.AsyncClient) -> str:
        """Fetch a fresh OAuth bearer token from the ITSM auth endpoint."""
        started = time.perf_counter()
        response = await client.post(
            self.token_url,
            json={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        if response.status_code >= 400:
            log_audit_event(
                tool_name="oauth_token_refresh",
                operator_role=resolve_operator_role(),
                rbac_decision="SYSTEM",
                latency_ms=latency_ms,
                status="AUTH_REFRESH_FAILED",
                metadata={"status_code": response.status_code},
            )
            raise TokenRefreshError(
                f"Token refresh failed with HTTP {response.status_code}: {response.text}"
            )

        data = response.json()
        new_token = data.get("access_token")
        if not new_token:
            raise TokenRefreshError("Token refresh response missing 'access_token'.")

        self.access_token = str(new_token)
        self.refresh_count += 1
        log_audit_event(
            tool_name="oauth_token_refresh",
            operator_role=resolve_operator_role(),
            rbac_decision="SYSTEM",
            latency_ms=latency_ms,
            status="TOKEN_REFRESHED",
            metadata={"refresh_count": self.refresh_count},
        )
        return self.access_token

    async def request_with_token_refresh(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute an HTTP request and transparently refresh+retry once on HTTP 401."""
        base_headers = dict(kwargs.pop("headers", None) or {})

        token_used = self.access_token
        req_headers = dict(base_headers)
        req_headers["Authorization"] = f"Bearer {token_used}"

        response = await client.request(method, url, headers=req_headers, **kwargs)
        if response.status_code != 401:
            return response

        async with self._lock:
            # Refresh if no concurrent caller already rotated past token_used
            if self.access_token == token_used:
                await self.refresh_token(client)
            refreshed_token = self.access_token

        retry_headers = dict(base_headers)
        retry_headers["Authorization"] = f"Bearer {refreshed_token}"
        return await client.request(method, url, headers=retry_headers, **kwargs)
