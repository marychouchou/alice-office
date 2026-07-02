from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from alice_office_router.router import router

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
    yield
    logger.info("Alice Office Router shutting down.")


app = FastAPI(
    title="Alice Office Router",
    description="Central webhook router for LINE OA multi-tenant agent dispatch.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)
