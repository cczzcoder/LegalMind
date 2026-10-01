from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import engine
from app.modules.wiki.router import router as wiki_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(
    title="LegalMind API",
    description="法律知识库、版本化 Wiki 与证据约束问答",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(wiki_router, prefix="/api/v1")


@app.get("/health/live", tags=["health"])
async def live():
    return {
        "service": "LegalMind",
        "status": "ok",
    }


@app.get("/health/ready", tags=["health"])
async def ready():
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text("SELECT id FROM wiki_pages LIMIT 0")
            )
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable or migrations not applied",
        ) from None

    return {"status": "ready"}
