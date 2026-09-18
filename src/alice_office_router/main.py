from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from alice_office_router.channels import enabled_adapters
from alice_office_router.config import get_settings
from alice_office_router.core import cancel_warmups
from alice_office_router.file_links import files_router
from alice_office_router.google_oauth import oauth_router
from alice_office_router.logging_setup import RequestContextMiddleware, configure_logging

# Before anything else logs: every logger in this process (uvicorn's too)
# renders through one structlog formatter from here on (logging_setup).
_settings = get_settings()
configure_logging(_settings)
logger = logging.getLogger(__name__)

# Deprecated single-channel path kept while the LINE OA console still posts
# here; remove once it points at /webhooks/line (see channel-interface-plan.md).
_LEGACY_LINE_WEBHOOK_PATH = "/webhook"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifespan events.

    Args:
        app: The FastAPI application instance.

    Yields:
        None during the application's running phase.
    """
    logger.info("Alice Office Router starting up.")
    get_settings()  # fail fast on misconfiguration (see Settings validators)
    yield
    cancel_warmups()
    logger.info("Alice Office Router shutting down.")


app = FastAPI(
    title="Alice Office Router",
    description="Central webhook router for LINE OA multi-tenant agent dispatch.",
    version="0.1.0",
    lifespan=lifespan,
)

# Outermost user middleware: binds request_id for every line logged while
# serving the request, and emits the one structured access line per request.
app.add_middleware(RequestContextMiddleware)

for adapter in enabled_adapters(_settings):
    app.include_router(adapter.api_router(), prefix=f"/webhooks/{adapter.name}")
    if adapter.name == "line":
        # Legacy alias: the LINE OA console still posts to /webhook. Same handler
        # logic; remove once the console points at /webhooks/line.
        app.include_router(adapter.api_router(), prefix=_LEGACY_LINE_WEBHOOK_PATH)

app.include_router(oauth_router)
# Unconditional, like oauth_router: with PUBLIC_BASE_URL unset the router
# never hands out a link, so the route simply finds nothing and 404s. Gating
# the mount would put a second "is this enabled" branch outside file_links.
app.include_router(files_router)
