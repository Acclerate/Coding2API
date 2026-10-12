"""CodeBuddy 国际站（www.codebuddy.ai）渠道。

与国内站（copilot.tencent.com）**同协议**：同一套 `/v2/chat/completions`、
`/v3/config`、`/v2/plugin/auth/*`、`/v2/billing/*` 路径，同一个 Keycloak realm
（`copilot`）。实测（2026-10-12，匿名探针）两站路由表一致，所以这里直接复用
`CodeBuddyClient`，不复制一份协议代码。

**独立渠道而非「改 CODEBUDDY_API_ENDPOINT 切站点」**：endpoint 在本项目是进程级
单值，切过去会让存量国内站凭证全部打到国际站。国际版与国内版是两套账号体系
（各自注册、各自计费），需要同时持有。

能力差异（只保留**已验证存在**的路径）：

- 聊天 / 模型列表 / 额度探测 / 设备码登录 / token 刷新 / 签到 —— 全部启用。
  判据见 `docs/…` 记录：匿名请求命中这些路径返回网关 401（路由已注册），
  而未注册路径返回应用层 JSON 404（`/billing/meter/daily-checkin` 实测 401，
  `/billing/nope` 实测 404，两者可区分）。
- 成长中心 / 活跃上报 —— **关闭**。国际站网关把整个 `/v2/activity/*` 前缀挂在
  认证插件后面（实测 `/v2/activity/nope` 也返回 401，而非 404），因此 401 无法
  证明子路由存在；而成长中心含抽奖 / 连登兑换 / 盲盒等**不可逆**动作，在未验证
  的端点上盲跑会真实消耗账号权益。`growth` / `activity` 置 None：任务层与 API
  层都用 `getattr(provider, "growth", None)` 探测，置 None 即自动跳过，
  管理台的「成长中心」按钮也因 `credential.provider === "codebuddy"` 判定而不出现。
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import CodeBuddyProvider


@dataclass(slots=True)
class CodeBuddyIntlProvider(CodeBuddyProvider):
    """国际站渠道；唯一差异是渠道 id 与「无成长中心 / 无活跃上报」。

    保留 dataclass 字段覆盖而非另写一份实现：客户端、凭证解析、事件分类、
    签到、刷新全部共用国内站代码，行为差异只由下面两个 `None` 表达。
    """

    id: str = "codebuddy_intl"
    # 下面两项刻意覆盖父类方法为 None（不是删方法）：调用方一律用
    # `getattr(provider, "growth", None)` / `getattr(provider, "activity_scope", None)`
    # 探测能力，置 None 就是「本渠道没有该能力」，无需在任务层加渠道分支。
    growth = None
    activity = None
    activity_scope = None