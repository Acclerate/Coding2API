"""运维告警后台任务：四类规则 + webhook 推送 + 站内记录。

语义（与用户确认的范围一致）：

1. **凭证池耗尽**：池里有凭证但 `ready` 少于阈值——服务「活着但用不了」，
   `/health` 探针看不出来，只有池级视角能发现。severity=critical。
2. **后台任务连续失败**：某任务连续失败达到阈值（成功一轮清零）。
   severity=warning。
3. **token 临近到期**：某凭证的 access token 剩余时间落在窗口内。severity=warning。
4. **上游错误率骤升**：统计窗内失败占比超过阈值且样本数足够。severity=warning。

投递（用户选的「Webhook + 站内」）：
- **站内**：每条命中落 `alert_events`，管理台「站内告警记录」回看（重启不丢）。
- **Webhook**：配置 `ALERT_WEBHOOK_URL` 时 POST JSON；留空只留站内记录。
  多个地址用逗号分隔，全部成功才算 delivered。

为什么要静默窗：评估每 N 分钟一轮，而池耗尽 / token 到期这类状态会持续存在。
没有静默窗就会每轮落一行、每轮推一次，把记录和群聊都刷屏。同一条
`(rule, scope)` 在静默窗内只落库一次；窗口过后若仍命中，再报一次（提醒还在）。

纯函数 `evaluate_alerts` 与任务 `AlertTask` 分离：规则判定不碰 IO，测试可直接
喂快照；任务的 IO（读池/统计/落库/webhook）全部可注入，便于用假实现覆盖分支。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..db.repo import AlertRepository, CredentialRepository
from ..runtime_settings import blocked_webhook_host
from ..stats.collector import StatsCollector
from .status import TASK_BY_KEY, TaskStatusStore

logger = logging.getLogger(__name__)

# webhook 单次投递超时（秒）。远端挂掉时不能拖住后台循环。
_WEBHOOK_TIMEOUT = 10.0

# 一条告警的所有 webhook 地址共享的总投递预算（秒）：并发下最坏等单次超时，
# 这一层兜住「地址极多 / 自建 client 路径累积超时」把后台循环拖死的情况。
_WEBHOOK_TOTAL_BUDGET = 15.0

# 外发 webhook 的 detail 里需要脱敏的字段：`nickname` 是用户自定义标签，
# 实践中常填邮箱 / 手机号，站内记录留原文、外发一律打码（同源问题）。
_PII_DETAIL_KEYS = ("nickname",)
_PII_MASK = "***"

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"


def _redact_webhook_url(url: str) -> str:
    """webhook URL → 只留 scheme://host（Slack/飞书的 path 即密钥）。"""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return "<webhook>"
    return f"{parts.scheme}://{parts.netloc}"


def _redact_webhook_error(error: Exception) -> str:
    """httpx 异常文本里可能内嵌整条 URL（含密钥 path/query）：只留异常类名。

    httpx 的 `RequestError.__str__` 会带上请求 URL，落库后经 `GET /api/alerts`
    回显给管理台即等于把 webhook 密钥写进审计记录。
    """
    return f"{type(error).__name__}: webhook delivery failed"


def _public_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """外发 webhook 的 detail：把 PII 字段打码，站内记录不受影响。

    `nickname` 是用户自填标签，实践中常是邮箱/手机号。站内 `alert_events`
    属本机管理面，保留原文便于定位；一旦外发到第三方（Slack/飞书群）就该
    最小化，非空值一律替换为掩码，空值保持空串以免多出噪声字段。
    """
    public = dict(detail)
    for key in _PII_DETAIL_KEYS:
        if public.get(key):
            public[key] = _PII_MASK
    return public


@dataclass(frozen=True)
class Alert:
    """一条待投递的告警（规则判定结果，尚未落库/推送）。"""

    rule: str
    severity: str
    scope: str            # 具体对象：pool / 任务 key / 凭证 id
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    # 文案里内嵌的 PII 片段（如昵称）：站内记录留原文，外发 webhook 前打码。
    pii: tuple[str, ...] = ()

    @property
    def dedup_key(self) -> str:
        return f"{self.rule}:{self.scope}"

    def public_message(self) -> str:
        """外发文案：把 `pii` 里的片段替换为掩码。"""
        message = self.message
        for secret in self.pii:
            if secret:
                message = message.replace(secret, _PII_MASK)
        return message


def _humanize(seconds: int) -> str:
    """剩余秒数 → 人类可读（小时/分钟），用于告警文案。"""
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} 小时"
    if seconds >= 60:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def evaluate_alerts(
    *,
    pool: dict[str, int],
    pool_ready_min: int,
    failing_tasks: list[tuple[str, str, int]],
    expiring: list[dict[str, Any]],
    error_counts: tuple[int, int],
    error_rate_threshold: float,
    error_rate_min_requests: int,
) -> list[Alert]:
    """按四类规则把信号快照判成告警列表（纯函数，不碰 IO）。

    各规则的开关统一用「阈值 ≤ 0 即关闭」表达，不另设布尔开关：少一个字段
    就少一处「开关开着但阈值没配」的静默状态。

    - `failing_tasks`：`(key, name, streak)`，调用方已按阈值过滤。
    - `expiring`：`[{id, nickname, token_expires_at, ...}]`，已按窗口过滤。
    - `error_counts`：`(requests, failed)`，窗口口径由调用方给定。
    """
    alerts: list[Alert] = []

    total = pool.get("total", 0)
    ready = pool.get("ready", 0)
    if pool_ready_min > 0 and total > 0 and ready < pool_ready_min:
        alerts.append(Alert(
            rule="pool_empty", severity=SEVERITY_CRITICAL, scope="pool",
            message=(f"凭证池可用数为 {ready}（少于阈值 {pool_ready_min}）："
                     f"共 {total} 个凭证，冷却 {pool.get('cooling', 0)}、"
                     f"暂停 {pool.get('paused', 0)}、禁用 {pool.get('disabled', 0)}"),
            detail={"total": total, "ready": ready, "cooling": pool.get("cooling", 0),
                    "paused": pool.get("paused", 0), "disabled": pool.get("disabled", 0),
                    "threshold": pool_ready_min},
        ))

    for key, name, streak in failing_tasks:
        alerts.append(Alert(
            rule="task_failed", severity=SEVERITY_WARNING, scope=key,
            message=f"后台任务「{name}」连续失败 {streak} 次",
            detail={"task": key, "name": name, "streak": streak},
        ))

    for row in expiring:
        expires_at = int(row["token_expires_at"])
        nickname = row.get("nickname") or ""
        label = nickname or row["id"]
        alerts.append(Alert(
            rule="token_expiring", severity=SEVERITY_WARNING, scope=row["id"],
            message=(f"凭证「{label}」的 token 将在 {_humanize(int(row['remaining']))} 后"
                     f"到期（{time.strftime('%Y-%m-%d %H:%M', time.localtime(expires_at))}）"),
            detail={"credential_id": row["id"], "provider": row.get("provider", ""),
                    "nickname": nickname, "token_expires_at": expires_at,
                    "remaining": int(row["remaining"])},
            # 文案里的昵称片段外发时打码（detail 由 _public_detail 处理）。
            pii=(nickname,) if nickname else (),
        ))

    requests, failed = error_counts
    if (error_rate_threshold > 0 and requests >= error_rate_min_requests
            and requests > 0):
        rate = failed / requests
        if rate >= error_rate_threshold:
            alerts.append(Alert(
                rule="error_rate", severity=SEVERITY_WARNING, scope="pool",
                message=(f"近窗口上游错误率 {rate * 100:.0f}%"
                         f"（{failed}/{requests} 失败，阈值 {error_rate_threshold * 100:.0f}%）"),
                detail={"requests": requests, "failed": failed,
                        "rate": round(rate, 4), "threshold": error_rate_threshold},
            ))

    return alerts


class AlertTask:
    """运维告警一轮：收集信号 → 判规则 → 静默去重 → 落库 + 推送。

    全部依赖可注入（取值器用零参 callable，热更配置每轮现读）。`now` 与
    `client` 供测试注入固定时钟与 httpx.MockTransport。
    """

    def __init__(
        self,
        credentials: CredentialRepository,
        alerts: AlertRepository,
        stats: StatsCollector,
        *,
        task_status: TaskStatusStore,
        enabled: Callable[[], bool] = lambda: True,
        webhook_url: Callable[[], str] = lambda: "",
        pool_ready_min: Callable[[], int] = lambda: 1,
        task_failures: Callable[[], int] = lambda: 3,
        token_expiry_hours: Callable[[], int] = lambda: 24,
        error_rate_threshold: Callable[[], float] = lambda: 0.5,
        error_rate_min_requests: Callable[[], int] = lambda: 20,
        error_rate_window_minutes: Callable[[], int] = lambda: 15,
        silence_minutes: Callable[[], int] = lambda: 30,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], int] | None = None,
    ) -> None:
        self._credentials = credentials
        self._alerts = alerts
        self._stats = stats
        self._task_status = task_status
        self._enabled = enabled
        self._webhook_url = webhook_url
        self._pool_ready_min = pool_ready_min
        self._task_failures = task_failures
        self._token_expiry_hours = token_expiry_hours
        self._error_rate_threshold = error_rate_threshold
        self._error_rate_min_requests = error_rate_min_requests
        self._error_rate_window_minutes = error_rate_window_minutes
        self._silence_minutes = silence_minutes
        self._client = client
        self._now = now or (lambda: int(time.time()))

    async def run_once(self) -> dict[str, Any] | None:
        """评估一轮；返回运行态摘要，关闭时返回 None（no-op，不覆盖上次结果）。"""
        if not self._enabled():
            return None
        now = int(self._now())
        threshold = self._task_failures()
        failing = [
            (key, TASK_BY_KEY[key].name if key in TASK_BY_KEY else key, streak)
            for key, streak in self._task_status.failing(threshold)
        ]
        window_minutes = max(0, self._error_rate_window_minutes())
        expiring_seconds = max(0, self._token_expiry_hours()) * 3600
        alerts = evaluate_alerts(
            pool=self._credentials.pool_counts(now),
            pool_ready_min=self._pool_ready_min(),
            failing_tasks=failing,
            expiring=self._expiring(now, expiring_seconds),
            error_counts=self._stats.window_error_rate(
                since=now - window_minutes * 60),
            error_rate_threshold=self._error_rate_threshold(),
            error_rate_min_requests=self._error_rate_min_requests(),
        )
        return await self._dispatch(alerts, now)

    def _expiring(self, now: int, within_seconds: int) -> list[dict[str, Any]]:
        """token 即将到期的凭证，补上 `remaining` 供文案与 detail 使用。"""
        if within_seconds <= 0:
            return []
        rows = self._credentials.expiring_tokens(within_seconds=within_seconds, now=now)
        return [{**row, "remaining": int(row["token_expires_at"]) - now} for row in rows]

    async def _dispatch(self, alerts: list[Alert], now: int) -> dict[str, Any]:
        """静默去重后逐条落库 + 推送；返回本轮摘要。"""
        silence = max(0, self._silence_minutes()) * 60
        fired = suppressed = 0
        delivered = 0
        for alert in alerts:
            last = self._alerts.last_ts(alert.rule, alert.scope)
            if last is not None and silence > 0 and now - last < silence:
                suppressed += 1
                continue
            ok, error = await self._deliver(alert, now)
            self._alerts.record(
                rule=alert.rule, severity=alert.severity, scope=alert.scope,
                message=alert.message,
                detail=json.dumps(alert.detail, ensure_ascii=False),
                delivered=ok, delivery_error=error, now=now,
            )
            fired += 1
            if ok:
                delivered += 1
        return {"evaluated": len(alerts), "fired": fired,
                "suppressed": suppressed, "delivered": delivered}

    async def _deliver(self, alert: Alert, now: int) -> tuple[bool, str | None]:
        """推送 webhook；未配置地址时视为「仅站内」，返回 (False, None)。

        投递失败只告警不抛错：一条 webhook 不能把后台任务判失败，否则
        「webhook 挂了」会被误报成「告警任务连续失败」，制造假信号。

        多个地址**并发**投递并套一层总预算：串行时 N 个地址最坏要等
        N × 单次超时，地址一多就能把后台循环拖到「连续失败」。并发下最坏
        只等单次超时；总预算再兜一层，防止 `_post` 的自建 client 路径
        （其 timeout 是每请求的）在地址很多时累积超时。
        """
        urls = [u.strip() for u in self._webhook_url().split(",") if u.strip()]
        if not urls:
            return False, None
        payload = {
            "service": "coding2api", "rule": alert.rule, "severity": alert.severity,
            "scope": alert.scope, "message": alert.public_message(),
            "detail": _public_detail(alert.detail), "ts": now,
        }
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(self._deliver_one(url, payload) for url in urls)),
                timeout=_WEBHOOK_TOTAL_BUDGET,
            )
        except TimeoutError:
            logger.warning("告警 webhook 投递超过总预算 %.0f 秒，本轮放弃",
                           _WEBHOOK_TOTAL_BUDGET)
            return False, "TimeoutError: webhook delivery budget exceeded"
        errors = [error for error in results if error is not None]
        return (not errors), (errors[0] if errors else None)

    async def _deliver_one(self, url: str, payload: dict[str, Any]) -> str | None:
        """投递单个地址；失败只记日志并返回脱敏后的错误串，不抛出。

        webhook path/query 常即密钥（Slack/飞书），日志与落库都只留 host，
        绝不带上整条 URL 或 httpx 异常里内嵌的 URL。
        """
        if await asyncio.to_thread(blocked_webhook_host, url):
            # 写入期只能拦字面量 IP（域名当时解析不了），这里在**投递前**按
            # 实际解析结果复核一次，堵住「域名指向 127.0.0.1 / 169.254.169.254
            # / 内网 DNS 名」的 SSRF。解析失败按阻断处理（fail closed）。
            logger.warning("告警 webhook 解析到内网/保留地址，已跳过 %s",
                           _redact_webhook_url(url))
            return "BlockedWebhookHost: resolves to a private address"
        try:
            await self._post(url, payload)
        except Exception as exc:  # noqa: BLE001 - 单点失败不影响其余地址
            error = _redact_webhook_error(exc)
            logger.warning("告警 webhook 投递失败 %s: %s",
                           _redact_webhook_url(url), error)
            return error
        return None

    async def _post(self, url: str, payload: dict[str, Any]) -> None:
        if self._client is not None:
            response = await self._client.post(url, json=payload)
        else:
            # trust_env=False 与 provider/proxy.build_client 同口径：告警 JSON
            # 带池子健康详情，不能被宿主环境代理静默转手。
            async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT,
                                         trust_env=False) as client:
                response = await client.post(url, json=payload)
        response.raise_for_status()
