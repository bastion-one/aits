"""Service authentication for the resource routers.

User authentication belongs to the calling service. AITS authenticates the *services*
that call it: each presents ``Authorization: Bearer <key>`` with a key from
``AUTH_SERVICE_KEYS``, a comma-separated list of ``name:secret`` pairs. The
name identifies the calling service; the secret is everything after the
first colon.

The dependency attests each request's database session with the name of the
key the caller presented. Every commit-log entry records that name as its
``principal`` (see :func:`app.ledger.attest`): the identity AITS itself
authenticated, whether that is a gateway or any other holder of a key. AITS
records nothing it did not authenticate.

Auth fails closed. With no keys configured the service refuses to start
unless ``AUTH_DISABLED=true`` explicitly opts into open mode for local
development. ``/health`` and the OpenAPI documents stay open.
"""

import hmac
import logging
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings
from .db import SessionDep
from .ledger import attest

logger = logging.getLogger("aits.auth")

_bearer = HTTPBearer(auto_error=False, description="A service key from AUTH_SERVICE_KEYS.")

ANONYMOUS = "anonymous"
"""The principal recorded for writes in open mode."""


def ensure_auth_configured(settings: Settings) -> None:
    """Refuse to start without service keys, unless open mode is explicit."""
    if settings.auth_disabled:
        logger.warning("AUTH_DISABLED=true: the API accepts unauthenticated requests")
        return
    if not settings.service_keys:
        raise RuntimeError("set AUTH_SERVICE_KEYS, or AUTH_DISABLED=true for local development")


def require_service_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: SessionDep,
) -> str:
    """Authenticate the caller, attest the request's session with the key's name,
    and return that name."""
    settings = get_settings()
    if settings.auth_disabled:
        attest(session, ANONYMOUS)
        return ANONYMOUS
    presented = credentials.credentials.encode() if credentials is not None else b""
    service: str | None = None
    # Compare against every key so timing does not reveal which one matched.
    for secret, name in settings.service_keys.items():
        if hmac.compare_digest(presented, secret.encode()):
            service = name
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a valid service key is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    attest(session, service)
    return service
