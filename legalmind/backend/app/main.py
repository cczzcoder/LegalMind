from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.config import get_settings
from app.core.database import engine
from app.core.errors import register_error_handling
from app.modules.answering.router import router as answering_router
from app.modules.documents.router import router as documents_router
from app.modules.identity.router import router as identity_router
from app.modules.retrieval.router import router as retrieval_router
from app.modules.sources.router import router as sources_router
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

# 只信任 TRUSTED_PROXIES 中的代理传来的 X-Forwarded-For；为空时始终使用直连地址。
# uvicorn 自带的代理头处理默认信任 127.0.0.1，启动时须加 --no-proxy-headers，由这里统一控制。
app.add_middleware(
    ProxyHeadersMiddleware,
    trusted_hosts=get_settings().trusted_proxy_list,
)

# trace_id 与统一错误体（设计 13、16.1）
register_error_handling(app)

app.include_router(identity_router, prefix="/api/v1")
app.include_router(wiki_router, prefix="/api/v1")
app.include_router(sources_router, prefix="/api/v1")
app.include_router(documents_router, prefix="/api/v1")
app.include_router(retrieval_router, prefix="/api/v1")
app.include_router(answering_router, prefix="/api/v1")


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
            await connection.execute(text("SELECT id FROM wiki_pages LIMIT 0"))
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable or migrations not applied",
        ) from None

    return {"status": "ready"}
