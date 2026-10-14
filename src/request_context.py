"""请求 ID 的生成、校验与跨层传递（PROPOSAL §6）。

为什么用 contextvar 而不是往每个函数签名里加参数：请求 ID 要在**统计写入**
（`StatsCollector.record`）时落库，而调用链是
`chat 路由 → executor → stats_collector`——中间隔着引擎层，不该为了一个
可观测性字段把参数一路透传（会污染所有中间层签名，且新增调用点容易漏传）。
contextvar 由中间件在请求入口设置，同一 asyncio 任务及其子任务可见。

纪律：
- 入站 `X-Request-ID` 是**客户端可写**的输入，必须按白名单字符集与长度校验
  后才采信（否则日志/DB 会被人塞进换行、超长串）。不合法就丢弃、改用自生成。
- 自生成用 `uuid4().hex`（32 位十六进制）：与 `usage_events.id` 同风格，
  无连字符便于日志里整段 grep。
- 出站**不**透传到上游：各渠道的出站头语义不同（qoder 的 `X-Request-ID` 是
  uid 派生的防重放值、trae/codearts 有指纹敏感头），统一改写会破坏既有契约。
  本字段的用途是本服务侧「响应头 + 统计」的关联，不冒充上游追踪。
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

# 采信的入站 X-Request-ID 字符集：可见 ASCII 里去掉空格与引号类，长度 1-128。
# 128 足够容纳常见实现（UUID、base62 trace id），又不至于让一列被滥用成日志。
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# 响应头名（与入站同名，回显或生成值）。
REQUEST_ID_HEADER = "X-Request-ID"

_current: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    """生成一个新的请求 ID（32 位十六进制，无连字符）。"""
    return uuid.uuid4().hex


def sanitize_request_id(raw: str | None) -> str | None:
    """校验入站请求 ID；不合法（含换行/超长/空）返回 None，由调用方自生成。"""
    if not raw:
        return None
    return raw if REQUEST_ID_PATTERN.match(raw) else None


def set_request_id(value: str | None) -> None:
    """在当前上下文设置请求 ID（中间件在调用下游之前调用）。

    允许 None 是为了测试/后台线程能把上下文复原（默认值即 None）。
    """
    _current.set(value)


def get_request_id() -> str | None:
    """读当前上下文的请求 ID；不在请求内（后台任务、脚本）返回 None。"""
    return _current.get()


def resolve_request_id(header: str | None) -> str:
    """入站头 → 最终请求 ID：合法则沿用（便于与客户端日志对齐），否则自生成。"""
    return sanitize_request_id(header) or new_request_id()
