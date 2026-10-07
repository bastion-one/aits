"""FastAPI application factory."""

import logging
import tomllib
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from .auth import ensure_auth_configured, require_service_key
from .body_limit import BodyLimitMiddleware
from .canonical_json import CanonicalEncodingError
from .config import get_settings
from .db import SessionDep, init_db
from .ledger import CidCollision, CommitLockTimeout
from .routers import agents, artifacts, audit, configs, duts, lineage

logger = logging.getLogger("aits.ledger")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_auth_configured(get_settings())
    init_db()
    yield


def _operation_id(route: APIRoute) -> str:
    """Derive a clean, predictable operationId from the route's tag and function name.

    Yields ``f"{tag}_{route.name}"`` (e.g. ``agents_create``,
    ``lineage_append_version``, ``configs_get_config``). The OpenAPI generator
    (``make client``) turns this into a Python method on a per-tag API class
    (e.g. ``AgentsApi.create``, ``LineageApi.append_version``).
    """

    tag = route.tags[0] if route.tags else "default"
    return f"{tag}_{route.name}"


def _package_version() -> str:
    try:
        return version("aits")
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        if pyproject.is_file():
            return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
        return "0.0.0"


app = FastAPI(
    title="aits",
    version=_package_version(),
    lifespan=lifespan,
    generate_unique_id_function=_operation_id,
)

app.add_middleware(BodyLimitMiddleware)

# Every resource router requires a service key; /health and the OpenAPI
# documents stay open.
_SERVICE_AUTH = [Depends(require_service_key)]
app.include_router(agents.router, dependencies=_SERVICE_AUTH)
app.include_router(artifacts.router, dependencies=_SERVICE_AUTH)
app.include_router(audit.router, dependencies=_SERVICE_AUTH)
app.include_router(configs.router, dependencies=_SERVICE_AUTH)
app.include_router(duts.router, dependencies=_SERVICE_AUTH)
app.include_router(lineage.router, dependencies=_SERVICE_AUTH)


@app.exception_handler(CommitLockTimeout)
async def commit_lock_timeout_handler(_request: Request, exc: CommitLockTimeout):
    """Write contention outlasted the lock timeout: tell the caller to retry."""
    return JSONResponse(status_code=503, content={"detail": str(exc)}, headers={"Retry-After": "1"})


@app.exception_handler(CanonicalEncodingError)
async def canonical_encoding_handler(_request: Request, exc: CanonicalEncodingError):
    """Content that cannot be hashed (too deep, non-finite floats) is a bad request."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    """Strip echoed ``input`` from every 422 so submitted text is not returned."""
    errors = [{k: v for k, v in err.items() if k != "input"} for err in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


@app.exception_handler(CidCollision)
async def cid_collision_handler(_request: Request, exc: CidCollision):
    """A CID collision is an integrity alarm: refuse the write, never dedup it."""
    logger.warning("CID collision on %s", exc.cid.hex())
    return JSONResponse(status_code=409, content={"detail": str(exc)})


class HealthStatus(BaseModel):
    status: str


@app.get("/health", tags=["health"], response_model=HealthStatus)
def health() -> HealthStatus:
    """Liveness: the process is up. Does not touch the database."""
    return HealthStatus(status="ok")


@app.get(
    "/ready",
    tags=["health"],
    response_model=HealthStatus,
    responses={503: {"description": "The database is unreachable."}},
)
def ready(session: SessionDep) -> HealthStatus:
    """Readiness: the service can reach its database."""
    try:
        session.connection().execute(text("SELECT 1"))
    except (DBAPIError, PoolTimeoutError) as exc:  # driver failure or pool exhausted
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return HealthStatus(status="ready")
