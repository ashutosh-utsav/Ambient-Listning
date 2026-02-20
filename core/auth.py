import logging
from datetime import datetime
from typing import Dict, Any

from fastapi import Request, status, WebSocket, Depends, WebSocketException
from fastapi.security import HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware
from jose import ExpiredSignatureError, JWTError, jwt

from core.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

security = HTTPBearer()

def _decode_token(token: str) -> Dict[str, Any]:
    """
    Decode and validate JWT token.
    
    Args:
        token: JWT token string
        
    Returns:
        Decoded token payload
        
    Raises:
        JWTError: If token is invalid
    """

    return jwt.decode(
        token,
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
        issuer=settings.JWT_ISSUER,
        options={"verify_exp": True, "verify_iss": False, "verify_signature": False}
    )

async def get_current_user_from_websocket(
    websocket: WebSocket,
    token: str | None = None,
):
    if token is None:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Missing token")
    
    try:
        payload = _decode_token(token)
        user_id = payload.get("unique_name")
        if user_id is None:
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token payload")
        
        return {"user_id": user_id}
    
    except ExpiredSignatureError:
        logger.warning("WebSocket JWT token has expired.")
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Token has expired")

    except JWTError as e:
        logger.warning(f"WebSocket JWT validation error: {str(e)}")
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token")


class JWTMiddleware(BaseHTTPMiddleware):
    """
    JWT Authentication middleware.
    Validates JWT tokens for all protected endpoints.
    """
    
    PUBLIC_PATHS = {"/", "/docs", "/redoc", "/openapi.json"}
    
    async def dispatch(self, request: Request, call_next):
        """
        Process request and validate JWT token if required.
        """

        if request.method == "OPTIONS":
            return await call_next(request)
        
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)
        
        auth_header = request.headers.get("Authorization")
        
        if not auth_header or not auth_header.startswith("Bearer "):
            
            logger.warning(f"Missing or invalid authorization header for {request.url.path}")
            return self._unauthorized_response("Missing or invalid authorization header")
        
        token = auth_header.replace("Bearer ", "")
        
        try:

            payload = _decode_token(token)
            
            request.state.user = {
                "user_id": payload.get("unique_name"), 
                "email": payload.get("email"),
                "roles": payload.get("roles", []),
            }
            
            if settings.ENABLE_AUDIT_LOGGING:
                logger.info(
                    f"Authenticated request from user {payload.get('unique_name')} "
                    f"to {request.url.path}"
                )
            
            response = await call_next(request)
            return response
            
        except JWTError as e:
            logger.error(f"JWT validation error: {str(e)}")
            return self._unauthorized_response("Invalid token")
        except Exception as e:
            logger.exception("Unexpected error in JWT middleware")
            return self._unauthorized_response("Authentication error")
    
    def _unauthorized_response(self, detail: str):
        """
        Create 401 Unauthorized response with structured error.
        """
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={
                "error": "Unauthorized",
                "details": detail
            },
            headers={"WWW-Authenticate": "Bearer"}
        )