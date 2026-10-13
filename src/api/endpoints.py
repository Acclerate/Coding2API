"""三个 /v1 兼容出口（chat / responses / messages）共用的请求骨架。

三者只差「入站映射」与「出口翻译」两处，其余逐行相同：读体 → 解析 →
模型白名单 → 流式（preflight + translator）或非流式（转换形状）。此前每份
都复写这十几行，改一处（如白名单报错文案、preflight 时机）要改三遍，且容易
漏改导致某个出口的 400 语义与其他不一致。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth.access import model_allowed
from ..compat.openai.request import ChatRequest, InvalidRequest
from ..config import Settings
from ..engine.executor import Executor, StreamSink
from ..runtime_settings import RuntimeSettings
from .deps import ApiKeyPrincipal, read_json_body
from .streaming import with_keepalive


def ensure_model_allowed(chat_request: ChatRequest, principal: ApiKeyPrincipal,
                         settings: Settings | RuntimeSettings) -> None:
    """模型白名单校验：不通过即 400（三个出口与 count_tokens 同一口径）。"""
    if not model_allowed(chat_request.model, principal.allowed_models,
                         default_model=settings.default_model):
        raise InvalidRequest(
            f"model {chat_request.model!r} is not allowed for this api key")


async def dispatch_compat(
        executor: Executor, settings: Settings | RuntimeSettings,
        request: Request, principal: ApiKeyPrincipal, *,
        parse: Callable[[Any], ChatRequest],
        translator_for: Callable[[str], StreamSink | None] | None = None,
        convert: Callable[[dict[str, Any]], Any] | None = None,
        on_body: Callable[[Any], None] | None = None,
) -> JSONResponse | StreamingResponse:
    """读体 → 解析 → 白名单 → 按 stream 分派到流式或非流式出口。

    `translator_for(model)` 造出口协议的中立事件翻译器（chat 出口为 None，
    即用默认 chat 形状）；`convert(completion)` 把非流式 completion 转成该
    协议的响应体（chat 出口为 None，直接回 completion）；`on_body` 是解析前
    的原始体钩子（chat 出口用它做诊断落盘）。
    """
    body = await read_json_body(request)
    if on_body is not None:
        on_body(body)
    chat_request = parse(body)
    ensure_model_allowed(chat_request, principal, settings)
    binding = principal.provider_binding
    if chat_request.stream:
        # 前置校验：在 200 响应头发出前拒绝不可能成功的请求（否则只能断流）
        target = executor.preflight(chat_request, binding)
        translator = translator_for(target.model) if translator_for else None
        return StreamingResponse(
            with_keepalive(executor.stream_guarded(chat_request,
                                                   username=principal.username,
                                                   translator=translator,
                                                   provider_binding=binding)),
            media_type="text/event-stream")
    completion = await executor.complete(chat_request, username=principal.username,
                                         provider_binding=binding)
    return JSONResponse(convert(completion) if convert else completion)
