"""FastAPI application factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from .db import init_db
from .routers import agents, artifacts, audit, configs, duts, lineage


@asynccontextmanager
async def lifespan(app: FastAPI):
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


app = FastAPI(
    title="aits",
    lifespan=lifespan,
    generate_unique_id_function=_operation_id,
)

app.include_router(agents.router)
app.include_router(artifacts.router)
app.include_router(audit.router)
app.include_router(configs.router)
app.include_router(duts.router)
app.include_router(lineage.router)


class HealthStatus(BaseModel):
    status: str


@app.get("/health", tags=["health"], response_model=HealthStatus)
def health() -> HealthStatus:
    return HealthStatus(status="ok")
