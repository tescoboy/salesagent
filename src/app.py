"""Central FastAPI application.

Mounts MCP and Admin into a single process. A2A is served by the core/
stack via ``adcp.server.serve(transport="a2a")`` — this app is the legacy
src/ stack, kept reachable for fallback only (RUN_STACK=src).
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastmcp.utilities.lifespan import combine_lifespans
from starlette.routing import Route

from src.core.main import mcp

logger = logging.getLogger(__name__)


def _install_admin_mounts() -> None:
    """Ensure Flask admin mounts are the final routes in the FastAPI app.

    The root fallback mount must stay last so dynamically-added FastAPI test
    routes (and any later app routes) are matched before Flask catches all
    remaining paths.
    """

    from a2wsgi import WSGIMiddleware
    from starlette.routing import Mount

    filtered_routes = []
    for route in app.router.routes:
        # Remove any prior compatibility mounts so we can re-add them at the end.
        if isinstance(route, Mount) and isinstance(route.app, WSGIMiddleware) and route.path in {"/admin", ""}:
            continue
        filtered_routes.append(route)

    app.router.routes = filtered_routes
    app.mount("/admin", admin_wsgi)  # type: ignore[arg-type]
    app.mount("/", admin_wsgi)  # type: ignore[arg-type]


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """FastAPI application lifespan — startup and shutdown hooks."""
    _install_admin_mounts()
    logger.info("FastAPI application starting up")
    yield
    logger.info("FastAPI application shutting down")


# Build the MCP sub-application.
# path="/" because we mount it at /mcp — routes inside are relative.
mcp_app = mcp.http_app(path="/")

# Create the root FastAPI app with combined lifespans so that both
# the MCP schedulers (delivery webhooks, media-buy status) and any
# future app-level startup/shutdown hooks fire correctly.
app = FastAPI(
    title="AdCP Sales Agent",
    description="Unified REST API for the AdCP Sales Agent. Also serves MCP at /mcp.",
    version="1.0.0",
    lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan),
)

# Mount MCP at /mcp
app.mount("/mcp", mcp_app)


# ---------------------------------------------------------------------------
# AdCP exception handlers — translate typed exceptions to HTTP responses.
# ---------------------------------------------------------------------------

from src.core.exceptions import AdCPError  # noqa: E402


@app.exception_handler(AdCPError)
async def adcp_error_handler(request: Request, exc: AdCPError) -> JSONResponse:
    """Convert AdCP exceptions to structured JSON error responses."""
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict(),
    )


# ---------------------------------------------------------------------------
# Health and debug routes
# ---------------------------------------------------------------------------

from src.routes.api_v1 import router as api_v1_router  # noqa: E402
from src.routes.health import debug_router as health_debug_router  # noqa: E402
from src.routes.health import router as health_router  # noqa: E402

app.include_router(api_v1_router)
app.include_router(health_router)
app.include_router(health_debug_router)

# ---------------------------------------------------------------------------
# Middleware stack (via add_middleware — outermost = last registered):
#   1. CORSMiddleware (outermost — adds CORS headers to all responses)
#   2. UnifiedAuthMiddleware (extracts auth token, sets scope["state"]["auth_context"])
# ---------------------------------------------------------------------------

from src.core.auth_middleware import UnifiedAuthMiddleware  # noqa: E402
from src.routes.rest_compat_middleware import RestCompatMiddleware  # noqa: E402

app.add_middleware(UnifiedAuthMiddleware)
app.add_middleware(RestCompatMiddleware)

_cors_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:8000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Admin UI — mount Flask admin via WSGIMiddleware
# ---------------------------------------------------------------------------

from a2wsgi import WSGIMiddleware  # noqa: E402

from src.admin.app import create_app  # noqa: E402

flask_admin_app = create_app()
admin_wsgi = WSGIMiddleware(flask_admin_app)


# ---------------------------------------------------------------------------
# Landing page routes
# ---------------------------------------------------------------------------

from fastapi.responses import HTMLResponse  # noqa: E402

from src.core.domain_routing import route_landing_page  # noqa: E402
from src.landing import generate_tenant_landing_page  # noqa: E402
from src.landing.landing_page import generate_fallback_landing_page  # noqa: E402


async def _handle_landing_page(request: Request):
    """Common landing page logic for root and /landing routes."""
    result = await asyncio.to_thread(route_landing_page, dict(request.headers))
    logger.info(
        f"[LANDING] Routing decision: type={result.type}, host={result.effective_host}, "
        f"tenant={'yes' if result.tenant else 'no'}"
    )

    if result.type == "admin":
        return RedirectResponse(url="/admin/login", status_code=302)

    if result.type in ("custom_domain", "subdomain") and result.tenant:
        try:
            html_content = await asyncio.to_thread(generate_tenant_landing_page, result.tenant, result.effective_host)
            return HTMLResponse(content=html_content)
        except Exception as e:
            logger.error(f"Error generating landing page: {e}", exc_info=True)
            return HTMLResponse(
                content=generate_fallback_landing_page(
                    f"Error generating landing page for {result.tenant.get('name', 'tenant')}"
                )
            )

    # Custom domain not configured for any tenant
    if result.type == "custom_domain":
        return HTMLResponse(content=generate_fallback_landing_page(f"Domain {result.effective_host} is not configured"))

    return HTMLResponse(content=generate_fallback_landing_page("No tenant found"))


# NOTE: These landing routes must be added BEFORE the /admin mount catch-all
# so FastAPI matches them first. We insert at position 0 (before mounts).

app.router.routes.insert(0, Route("/", _handle_landing_page, methods=["GET"]))
app.router.routes.insert(1, Route("/landing", _handle_landing_page, methods=["GET"]))

logger.info("FastAPI app created: MCP at /mcp, Admin at /admin")
