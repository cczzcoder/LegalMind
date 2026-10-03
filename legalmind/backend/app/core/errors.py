"""统一错误响应与请求追踪 ID（设计 13、16.1）。

错误体固定包含 `code`、`message` 和 `trace_id`；校验错误额外带 `errors`。
trace_id 由中间件为每个请求生成，写入 scope 并回写到 X-Trace-Id 响应头，
未处理异常的日志也带上它，便于把用户看到的报错与服务端日志对上。
"""

import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

TRACE_HEADER = "X-Trace-Id"

# 稳定的机器可读错误码；未列出的状态回落到 http_<status>
ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "unprocessable_entity",
    429: "too_many_requests",
    500: "internal_error",
    503: "service_unavailable",
}


class TraceIdMiddleware:
    """为每个请求生成 trace_id。

    用纯 ASGI 中间件而非 BaseHTTPMiddleware：后者会缓冲响应体，
    将来接入问答进度 SSE 时会阻塞流式响应。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        trace_id = uuid4().hex
        scope.setdefault("state", {})["trace_id"] = trace_id

        async def send_with_trace(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append(TRACE_HEADER, trace_id)
            await send(message)

        await self.app(scope, receive, send_with_trace)


def trace_id_of(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


def error_response(
    status_code: int,
    message: str,
    request: Request,
    *,
    errors: list | None = None,
    headers: dict | None = None,
) -> JSONResponse:
    body: dict = {
        "code": ERROR_CODES.get(status_code, f"http_{status_code}"),
        "message": message,
        "trace_id": trace_id_of(request),
    }
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def register_error_handling(app: FastAPI) -> None:
    """注册 trace_id 中间件与统一异常处理器。"""
    app.add_middleware(TraceIdMiddleware)

    # 注册在 Starlette 基类上：FastAPI 的 HTTPException 是它的子类，
    # 路由未匹配时抛出的也是基类，两者都要走统一错误体
    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException):
        # 业务异常用字符串描述；其他类型不透出，避免泄露内部结构
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return error_response(exc.status_code, message, request, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        return error_response(
            422,
            "Request validation failed",
            request,
            errors=jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        # 只把通用描述和 trace_id 返回客户端，细节留在服务端日志
        logger.exception("unhandled error trace_id=%s", trace_id_of(request))
        return error_response(500, "Internal server error", request)
