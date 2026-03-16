"""
Authentication module.

MIGRATION NOTE: JWT/Azure AD auth is commented out below for reference.
Replaced with optional API key auth for microservice use.

To enable: set ENABLE_API_KEY_AUTH=true and API_KEY=<strong-key> in .env.
When disabled (default), all requests pass through — suitable for an internal
service sitting behind a main app gateway.
"""

import logging
from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def verify_api_key(key: str | None) -> bool:
    """
    Returns True if the request should be allowed.
    - When auth is disabled: always True.
    - When auth is enabled: key must match settings.api_key.
    """
    if not settings.enable_api_key_auth:
        return True
    if not settings.api_key:
        logger.warning("ENABLE_API_KEY_AUTH=True but API_KEY is not set. Rejecting all requests.")
        return False
    return key == settings.api_key


class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Optional API key middleware for HTTP routes.
    Only active when ENABLE_API_KEY_AUTH=True.
    Checks the X-API-Key header.
    WebSocket auth is handled separately in the websocket endpoint.
    """

    PUBLIC_PATHS = {"/", "/docs", "/redoc", "/openapi.json", "/health", "/health/detailed"}

    async def dispatch(self, request: Request, call_next):
        if not settings.enable_api_key_auth:
            return await call_next(request)

        if request.method == "OPTIONS":
            return await call_next(request)

        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)

        key = request.headers.get("X-API-Key")
        if not verify_api_key(key):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"error": "Unauthorized", "detail": "Invalid or missing API key"},
                headers={"WWW-Authenticate": "X-API-Key"},
            )

        return await call_next(request)


# -- OLD: JWT auth (kept for reference) --
#
# from datetime import datetime
# from typing import Dict, Any
# from fastapi import WebSocket, WebSocketException
# from fastapi.security import HTTPBearer
# from starlette.middleware.base import BaseHTTPMiddleware
# from jose import ExpiredSignatureError, JWTError, jwt
#
# security = HTTPBearer()
#
# def _decode_token(token: str) -> Dict[str, Any]:
#     return jwt.decode(
#         token,
#         settings.JWT_SECRET_KEY,
#         algorithms=[settings.JWT_ALGORITHM],
#         issuer=settings.JWT_ISSUER,
#         options={"verify_exp": True, "verify_iss": False, "verify_signature": False},
#     )
#
# async def get_current_user_from_websocket(websocket: WebSocket, token: str | None = None):
#     if token is None:
#         raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Missing token")
#     try:
#         payload = _decode_token(token)
#         user_id = payload.get("unique_name")
#         if user_id is None:
#             raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token payload")
#         return {"user_id": user_id}
#     except ExpiredSignatureError:
#         raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Token has expired")
#     except JWTError as e:
#         raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token")
#
# class JWTMiddleware(BaseHTTPMiddleware):
#     PUBLIC_PATHS = {"/", "/docs", "/redoc", "/openapi.json"}
#     async def dispatch(self, request: Request, call_next):
#         if request.method == "OPTIONS":
#             return await call_next(request)
#         if request.url.path in self.PUBLIC_PATHS:
#             return await call_next(request)
#         auth_header = request.headers.get("Authorization")
#         if not auth_header or not auth_header.startswith("Bearer "):
#             return self._unauthorized_response("Missing or invalid authorization header")
#         token = auth_header.replace("Bearer ", "")
#         try:
#             payload = _decode_token(token)
#             request.state.user = {
#                 "user_id": payload.get("unique_name"),
#                 "email": payload.get("email"),
#                 "roles": payload.get("roles", []),
#             }
#             return await call_next(request)
#         except JWTError as e:
#             return self._unauthorized_response("Invalid token")
#     def _unauthorized_response(self, detail: str):
#         return JSONResponse(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             content={"error": "Unauthorized", "details": detail},
#             headers={"WWW-Authenticate": "Bearer"},
#         )
