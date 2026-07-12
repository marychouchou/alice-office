from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from alice_office_router.channels.line import router as line_channel_router
from alice_office_router.channels.local import router as local_channel_router
from alice_office_router.config import get_settings
from alice_office_router.google_oauth import oauth_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
    logger.info("Alice Office Router shutting down.")


app = FastAPI(
    title="Alice Office Router",
    description="Central webhook router for LINE OA multi-tenant agent dispatch.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(line_channel_router)
app.include_router(local_channel_router)
app.include_router(oauth_router)
