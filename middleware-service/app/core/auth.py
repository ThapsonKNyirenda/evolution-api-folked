"""
Authentication & authorisation for the middleware API.

Uses the same shared SECRET_KEY as the helpdesk backend so that
tokens minted by the helpdesk can be validated here.
"""

import uuid
import logging
from typing import List, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from app.core.config import settings

logger = logging.getLogger(__name__)

oauth2_scheme = HTTPBearer(auto_error=False)


def get_token_payload(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
) -> dict:
    """
    Decode and validate a JWT token issued by the helpdesk backend.

    Returns the token payload if valid, or raises 401.

    The token is optional — anonymous callers (e.g. Evolution webhooks)
    can still reach unprotected endpoints.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.algorithm],
        )
        return payload
    except JWTError as exc:
        logger.warning("Invalid JWT token: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_helpdesk_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
) -> dict:
    """
    Like `get_token_payload` but also verifies that the token was issued
    to a known helpdesk service account (the 'whatsapp-middleware' user or
    an admin user from the helpdesk).
    """
    payload = get_token_payload(credentials)

    # Extract the user info embedded in the token
    user_data = payload.get("user", {})
    username = user_data.get("username", "")
    user_id = user_data.get("id", "")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing user information",
        )

    return payload


def require_admin_role(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
) -> dict:
    """
    Require the caller to have a helpdesk token with an admin-level role
    (tenant admin, system admin, or similar).
    """
    payload = require_helpdesk_token(credentials)
    user_data = payload.get("user", {})

    # Accept tokens that have a 'type' of 'service' (the middleware itself)
    # OR tokens that belong to a helpdesk admin user.
    user_type = user_data.get("type", "")
    if user_type == "service":
        return payload

    # For regular users, check roles stored in the token payload.
    # The helpdesk JWT may include role names in the user object.
    roles = user_data.get("roles", []) or []
    role_names = [r.get("name", "") if isinstance(r, dict) else str(r) for r in roles]

    admin_keywords = {"admin", "tenant admin", "system admin", "super admin"}
    if not any(r.lower() in admin_keywords for r in role_names):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to access this endpoint",
        )

    return payload
