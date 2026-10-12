"""TRAE 回调落点（无鉴权，浏览器 302 不带 key）。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth.session import SESSION_COOKIE, verify_session_token
from ..compat.openai.errors import error_payload
from ..provider.trae.events import UpstreamProtocolViolation
from .deps import Services

# 待完成回调的有效期：/authorize 无鉴权（浏览器 302 不带 key），长期挂着
# 的 pending 会被同网络的任何人都可用自己的 refreshToken 完成兑换——
# 换句话说，谁都能把自己的凭证塞进池子（落库归属还是发起登录的管理员）。
# 10 分钟足够走完一次浏览器登录。
PENDING_CALLBACK_TTL_SECONDS = 600


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/authorize")
    async def authorize(request: Request):
        """TRAE 浏览器 302 落点。

        回调不需要 API Key（浏览器不会带），因此这里不做鉴权，但：
        - 只接受**发起人本人**的回调（M7）
        - 待完成登录有 TTL，过期即作废并清槽
        - 只接受带 refreshToken 的链接，其他一律拒绝
        - 换到的凭证直接落库，响应里绝不回传 token

        注意：TRAE 回跳时只带 refreshToken/userInfo/userJwt，**不会回传
        登录 URL 里的 state**——machine/device id 从待完成登录的 state 里取，
        这正是 start_auth 把它们编码进 state 的原因（保证登录与落盘凭证一致）。

        因此不能用 state 匹配回调。改为按**发起人**绑定：回调是顶层浏览器
        导航，SameSite=Lax 会带上管理台的会话 Cookie，据会话用户名找到其待完成
        登录（M7）。这样：
        - 两个管理员并发发起登录不会互相覆盖（各按用户名分键）；
        - 同网段的其他人无法用自己的 refreshToken 顶掉别人进行中的登录；
        - 凭证记在发起人名下，不会被记错。
        """
        raw = str(request.url)
        provider = services.registry.get("trae")
        pending: dict[str, tuple[float, str]] = request.app.state.pending_callbacks
        if provider is None:
            return JSONResponse(status_code=400, content=error_payload(
                "no pending TRAE login in progress", "invalid_request", 400))
        # 会话 Cookie → 发起人（回调为顶层导航，Lax Cookie 会带上）
        verified = verify_session_token(
            request.cookies.get(SESSION_COOKIE, ""), services.settings.app_secret)
        username = verified[0] if verified is not None else ""
        entry = pending.get(username) if username else None
        if entry is None:
            return JSONResponse(status_code=400, content=error_payload(
                "no pending TRAE login in progress", "invalid_request", 400))
        started_at, state = entry
        if time.monotonic() - started_at > PENDING_CALLBACK_TTL_SECONDS:
            pending.pop(username, None)
            return JSONResponse(status_code=400, content=error_payload(
                "pending TRAE login expired, start a new one",
                "invalid_request", 400))
        if not request.query_params.get("refreshToken") and not request.query_params.get("userJwt"):
            return JSONResponse(status_code=400, content=error_payload(
                "callback missing refreshToken", "invalid_request", 400))
        # 先消费 pending，防止并发回调重复兑换同一登录
        pending.pop(username, None)
        try:
            credential_data = await provider.complete_callback(raw, state)
        except UpstreamProtocolViolation as error:
            return JSONResponse(status_code=400, content=error_payload(
                error.public_message, "invalid_credential", 400))
        credential_id = services.credentials.add(
            provider="trae", credential_data=credential_data,
            nickname=str(credential_data.get("nickname") or ""),
            added_by=username)
        services.schedule_probe(credential_id)
        return {"ok": True, "captured": True, "at": int(time.time())}

    return router
