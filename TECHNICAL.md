# Coding2API 技术方案
PROPOSAL.md 定方向，本文档定实现。描述**当前代码的现状**，不含历史沿革。

## 1. 技术栈定稿
| 层 | 选型 | 版本 | 说明 |
|---|---|---|---|
| 运行时 | Python | 3.12（`requires-python >=3.12`，CI/Dockerfile 均 3.12） | |
| 包管理 | uv（venv + pyproject.toml + uv.lock） | 锁定版本见 `uv.lock` | |
| Web | FastAPI + Uvicorn | 0.141.1 / 0.52.4 | |
| 配置 | pydantic-settings | 2.15.0 | `Settings` 为 frozen |
| HTTP | httpx 双客户端（流式/短请求分离）；`httpx[socks]` 提供按渠道 SOCKS5 代理 | 0.28.1 / socksio | 共享 `trust_env=False` |
| 数据库 | 标准库 sqlite3（WAL）+ 手写 SQL | 内置 | |
| 加密 | cryptography Fernet（凭证列） | 50.0.1 | |
| 测试 | pytest + pytest-cov + respx + 文件化 fixture | pytest 9.1.1 / respx 0.23.1 | |
| 前端 | React 19 + Tailwind 4 + shadcn/ui + @lobehub/icons + Vite 7 | 实例版本见 `web/package.json` | |

> 上表是选型定稿；小版本随依赖漂移，实际以 `pyproject.toml` / `uv.lock` / `web/package.json` 为准；仅主版本或兼容边界（`requires-python`、Node 主版本）变化时回改本表。

## 2. 目录结构与模块规格
按目录分组，行内列出该目录全部文件；描述按「／」与文件顺序对应。

| 路径 | 文件 | 职责 |
|---|---|---|
| `pyproject.toml` | — | uv 项目；`[tool.pytest.ini_options]` 设 coverage 目标 |
| 仓库根 | `Dockerfile`、`docker-compose.yml` | compose build context 依赖根目录 |
| `src/` | `main.py` `config.py` `runtime_settings.py` `version.py` | FastAPI 组装·lifespan·路由挂载（只做接线）／pydantic-settings 全部 env + `live()` 归一化标量·取值器／运行时配置覆盖层（DB 覆盖 > env + 白名单校验 + snapshot）／版本号（`pyproject.toml` 唯一真源，读不到回落常量） |
| `src/webapp/` | `limits.py` `security.py` `handlers.py` `logging.py` `static.py` | 请求体上限 ASGI 中间件（登录 8KB / 其余 16MB）／Host 白名单 + 安全响应头（CSP·nosniff）／异常 → HTTP 响应（稳定机器可读错误码）／root logger 配置／前端产物定位 + SPA catch-all（排除 `/api`、`/v1`） |
| `src/db/` | `schema.sql` `conn.py` `migrate.py` `repo.py` `crypto.py` | DDL 定稿（只加不改）／线程本地连接 + WAL + busy_timeout + `transaction()`／启动执行 `schema.sql` + `_MIGRATION_COLUMNS`·`_MIGRATION_DROPS`／凭证·API Key·用户·审计·告警·积分流水持久化（手写 SQL）／Fernet 加解密 |
| `src/auth/` | `users.py` `bootstrap.py` `session.py` `api_key.py` `csrf.py` `throttle.py` `rbac.py` `access.py` | DbUserStore + UsersFileStore（users.txt 导入）／启动引导（导入 → `ADMIN_USERNAMES` 提权 → 无活跃 admin 报错）／会话 Cookie 签发校验（手写 HMAC，payload 带 `session_epoch`）／`sk-` 生成 + SHA-256 摘要 + 常量时间校验／写操作 CSRF／登录限流（三级窗口 + PBKDF2 并发上限）／三角色 `admin`·`operator`·`viewer`／来源 IP 与模型白名单（纯函数） |
| `src/provider/` | `base.py` `proxy.py` `naming.py` `token_expiry.py` `openai_chunk.py` `fixtures/` | Provider 协议·Event·ErrKind·Quota·HealthScore·异常族／按渠道出站代理（解析 `PROVIDER_PROXIES` + `build_client`）／模型名归一（七渠道三字段）／到期提取（`expires_at` → JWT `exp`）／标准 OpenAI chunk 共享原语（choices 校验 + 空 tool_call 判定）／真实样本（`codebuddy/*.sse`、`trae/*.sse`） |
| `src/provider/codebuddy/` | `client.py` `intl.py` `events.py` `credential.py` `headers.py` `oauth.py` `checkin.py` `activity.py` `growth.py` `growth_runner.py` `refresh.py` | 上游 HTTP + SSE 流 + 额度探测 + 内容风控指纹清洗／国际版子类（仅覆渠道 id 与能力 None）／OpenAI 风格 SSE → Event + 状态与业务码分类／凭证类型与解析／上游技术常量与请求头／设备码轮询／签到 + 连续天数／活跃上报协议层／成长中心协议层（15 端点）／成长中心编排（7 类领取的顺序与失败判定）／token 刷新 + 多账号切换 |
| `src/provider/trae/` | `client.py` `events.py` `credential.py` `pricing.py` `backfill.py` `callback.py` | SOLO 上游 + 额度探测／自定义 SSE → Event／凭证解析（嵌套与扁平）+ 原子写回 + 签到设备号／单请求积分推算（上游 `token_usage` 不给积分）／历史明细推算积分回填与重算／登录 URL 构造 + 回调解析 |
| `src/provider/zen/` | `client.py` `events.py` | 上游 + 免费层门禁伪装 + 注入工具回包过滤 + `ZenProvider`／标准 OpenAI SSE → Event（无私有信封） |
| `src/provider/kilo/` | `client.py` `events.py` | 上游（标准 OpenAI，无门禁）+ `isFree` 过滤（不探活）／标准 OpenAI SSE → Event（`delta.reasoning` 思考通道） |
| `src/provider/qoder/` | `cosy.py` `client.py` `events.py` `credential.py` `auth.py` | COSY（自定义 Base64、AES/RSA、签名与会话头缓存）／上游 + 信封 SSE 解包 + 额度·签到／信封 SSE → 内层 chunk → Event + 状态与业务码分类／凭证解析（realm·组织·到期）／设备码 PKCE 登录（start/poll） |
| `src/provider/codearts/` | `signer.py` `dpop.py` `client.py` `events.py` `credential.py` `units.py` `backfill.py` `auth.py` `oauth.py` | 华为云 SDK-HMAC-SHA256（纯函数，便于固定向量测试）／ES256 P-256 DPoP JWS（低 S 归一化）／上游 + 私有 SSE 还原 + 福利·余额／SSE → 增量 Event（OpenAI chunk 与累计全文双形状）／凭证解析（AK/SK/STS/DPoP/refresh）／「套餐·剩余量」单位与到期解析／历史 usage 回填／OAuth2 PKCE 登录／回调链接粘贴换 token 适配 |
| `src/engine/` | `scheduler.py` `executor.py` `affinity.py` `continuation.py` `compress.py` `model_resolver.py` `sse.py` | 选号 + 冷却状态机 + pin（纯逻辑，操作 Candidate）／执行 + 模型目录收窄 + 轮换重试 + fallback 链 + 统计埋点／会话粘性／截断续写／上下文压缩裁剪（纯函数）／`"glm-5.2"`·`"glm-5.2@trae"`·`auto` → 候选集 + fallback 组解析／SSE 帧解析 + 行长上限 |
| `src/compat/` | `openai/{request,response,errors}.py` `responses/{request,response}.py` `anthropic/{request,response}.py` | ChatRequest 校验·chunk 聚合·OpenAI error shape／Responses 出口（Codex CLI 子集）／Anthropic Messages 出口（Claude Code 子集） |
| `src/tasks/` | `pacer.py` `quota_probe.py` `checkin.py` `growth.py` `activity.py` `refresh.py` `retention.py` `alerting.py` `status.py` `runner.py` | 全局节流器（随机区间 + 按桶并发上限 + 滑动窗口）／启动立即一轮 + 每 `QUOTA_PROBE_MINUTES`／全天每 10 分钟签到、成功即封账（支持渠道回填 `seal_until`）／成长中心（仅 CB）落 `growth_events`／活跃上报（CB 与 CB 国际版，默认关闭）／每 `REFRESH_INTERVAL_MINUTES` 预刷新／每 5 分钟小时汇总重算 + 90 天前明细清理／运维告警（四类规则 + webhook + 落库）／任务清单 `TASK_SPECS` + 进程内运行态／后台任务调度，接入应用生命周期 |
| `src/stats/` | `backfill.py` `collector.py` `query.py` | 历史成本一次性回填与重算（`scripts/backfill_cost.py` 复用）／`usage_events` 写入（脱敏）+ 小时汇总双写·重算／`overview`·`by-*`·`timeline`·`model-timeline`·`events` |
| `src/` 顶层 | `sorting.py` `benchmarks.py` `pricing.py` `model_match.py` | 列表排序 `sort`/`order` 白名单解析（db 与 api 共用）／OpenRouter 单一源：刊例价 + 明细元数据 + 能力指数 + 落盘快照／单请求成本估算（USD→CNY）／本地模型名 ↔ OpenRouter 条目匹配（仅 benchmarks·pricing 用） |
| `src/api/` | `deps.py` `endpoints.py` `chat.py` `responses.py` `messages.py` `context.py` `models.py` `model_merge.py` `model_catalog.py` `balance.py` `authorize.py` `admin_credentials.py` `admin_keys.py` `admin_settings.py` `admin_users.py` `admin_audit.py` `admin_alerts.py` `admin_model_catalog.py` `activate.py` `admin_stats.py` `admin_auth.py` `playground.py` `streaming.py` | Services 容器 + 鉴权·会话·CSRF 依赖／三个 /v1 出口共用的 `dispatch_compat` 骨架／`POST /v1/chat/completions`·`/v1/responses`·`/v1/messages`／请求入口的上下文压缩接线／`GET /v1/models`（按凭证加载 + SWR + 缓存·TTL + 落盘 + 黑名单 + 白名单）／模型归一·合并·命名·对外 id 消歧·别名表（纯函数）／模型目录落盘快照 + 别名预热／`GET /v1/user/balance`／`GET /authorize`（TRAE 回调落点）／凭证 CRUD·toggle·pin·probe·checkin·成长·账号切换／API Key CRUD（渠道绑定 + 双白名单 + 到期）／`GET`/`PUT /api/settings` + `GET /api/tasks`／用户管理／审计查询／告警回看／模型目录只读查看／一次性令牌激活流／统计查询／登录·登出·会话·自助改密 + 上游登录 start·poll·complete·cancel／会话调试端点（无需 API Key）／SSE 流包装（长空隙插心跳帧） |
| `src/audit/actions.py` | — | 审计动作常量 + 中文标签 |
| `web/` | — | React 前端 |
| `deploy/` | — | 各部署形态模板（launchd / newsyslog / logrotate / systemd） |
| `diagrams/` | — | 架构图 / 请求时序图 / 调度状态机（HTML 成品 + 源 JSON） |
| `scripts/` | — | hash_password / create_user / 回填与运维脚本 |
| `tests/` | — | 按 `src/` 子包镜像 |

## 3. 核心类型定义
### 3.1 中立事件层（`provider/base.py`）
```python
class EventKind(StrEnum):
    CONTENT = "content"        # 正文增量
    REASONING = "reasoning"    # 思考链增量
    TOOL_CALLS = "tool_calls"  # 工具调用增量（上游原样，index 由 engine 补齐）
    USAGE = "usage"            # prompt/completion/reasoning/cached tokens；credit 可空
    FINISH = "finish"          # finish_reason
    ERROR = "error"            # 流内业务错误（provider 解析时写 error_kind）

@dataclass(slots=True)
class Event:
    kind: EventKind
    content: str | None = None
    tool_calls: list[dict] | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    error_code: int | None = None
    error_message: str | None = None
    error_kind: ErrKind | None = None   # 流内错误的分类，executor 直接消费

@dataclass(slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None   # 输入中命中缓存的 token（缺省 None）
    credit: float | None = None        # 上游可选字段，两边都经常为 None
    credit_estimated: bool = False     # credit 是否为本服务推算（True 时展示标 ≈）
```

**事件名过滤**：TRAE 上游会发 `metadata` / `timing_cost` / `extra_info` / `progress_notice` 等无下游语义事件，`events.py` 的 `KNOWN_EVENT_NAMES` 之外的名字直接跳过。「解析失败不静默」针对的是**已知事件的畸形数据**：归一集内 JSON 非对象 / 字段类型不对仍抛 `UpstreamProtocolViolation`，不退回默认值。**tool_call 分片续块必须保留**：TRAE 上游工具调用按 `index` 分片，首片带 `function_call.name`、后续片只有 `arguments` 增量且**没有 name**。归一为 OpenAI 形状时只丢「无 name **且** arguments 为空」的噪声（`_is_blank_solo_tool_call`）；带实际 arguments 的续片保留，交下游 `ToolIndexState` 按 index 合并。若按「无 name 即丢弃」过滤，续片全被吃掉 → 客户端拼出截断 JSON → 工具报错 → 原样重试同轮 → 死循环。CodeBuddy/Zen/Kilo 的 `is_blank_tool_call` 同一判据（`provider/openai_chunk.py`）。
### 3.2 错误分类（决定冷却时长与作用域）
```python
class ErrKind(StrEnum):
    PLAN = "plan"        # 权益耗尽（1005）→ 冷却 12h
    CREDIT = "credit"    # 余额不足（402/14018）→ 冷却到次日 04:00（等签到恢复）
    SOFT = "soft"        # 限流/404 → 冷却 60s，不累计错误数
    DEAD = "dead"        # session 失效 → 硬禁用 disabled=1
    OTHER = "other"      # 其他 4xx/5xx → 累计，连续 3 次 → 冷却 10m
    INVALID = "invalid"  # 请求无效（模型不存在等）→ 不冷却凭证，跳过该上游；全拒 → 400
    MODEL = "model"      # 该模型用量超限（429+6004）→ 只冷却 (凭证, 模型)，10m 起翻倍封顶 2h
    CONCURRENCY = "concurrency"  # 并发会话打满 → 固定 60s 模型级短冷却，不翻倍
    BLOCKED = "blocked"  # 该账号无此模型（400/404+11102）→ (凭证, 模型) 负缓存，6h 起翻倍封顶 24h
    REQUEST = "request"  # 请求级错误（11101/11115/11128/11135）→ 零动作：不冷却、不累计，仅换号

MODEL_SCOPED_KINDS = frozenset({MODEL, BLOCKED, CONCURRENCY})
```

`MODEL_SCOPED_KINDS` 只写 `credential_model_cooldowns`（见 §6.3），不碰账号级 `cooling_until`，故同账号其他模型仍可选——这是两层冷却的核心：单个模型打满不能连累同账号其他模型。反之账号级冷却出现时会清空该凭证的模型级条目，否则「切模型」能绕过账号级限流。

| 上游信号 | CB | TRAE | Kilo | ErrKind |
|---|---|---|---|---|
| 权益耗尽 | `code=1005` | `code:1005` | — | PLAN |
| 余额不足 | 402 / `code=14018` | 402 / `code=14018` | — | CREDIT |
| 模型级限流 | 429 + `code=6004` | 429 + `code=6004` | 429 / 502/503/504（点名模型） | MODEL |
| 并发打满 | — | — | — | CONCURRENCY（CodeArts `TM.00001041`） |
| 该账号无此模型 | 400/404 + `code=11102` | 400/404 + `code=11102` | — | BLOCKED |
| 请求级错误 | 400 + 11101/`Unmarshal chat params failed`/11115/11128/11135 | — | 403 | REQUEST |
| 限流 | 429（无 6004） | 429（无 6004） | — | SOFT |
| 不存在 | 404 | 404 | — | SOFT（CodeArts 404 亦 SOFT） |
| 会话失效 | 401/403 | 401 | — | DEAD |
| 服务端错误 | 5xx | 5xx | 500 | OTHER |
| 请求自身无效 | 400 | 400（含 4001） | 400/404/422 | INVALID |
| 流内错误事件 | SSE error 事件 | `event:error` 业务码 | `{"error":{...}}` 帧 | 同上映射（单一来源：provider 解析时写 `Event.error_kind`，executor 直接消费；缺失回落 OTHER） |

> **zen / kilo 的 401 例外**：两者都无凭证，`401 Missing API key.` 只说明该模型需要付费 key，故 `classify_status(401)` 归 `INVALID` 而非 `DEAD`，避免一次强制付费模型请求把整条免费渠道硬禁用。

> **kilo 的 429 与 502/503/504 归 `MODEL`**：免费池是 OpenRouter 共享池转发，429 报错点名具体模型（`limit_source: upstream_provider_shared_pool`），429 消退后同一模型转 503 `no endpoints available`，两种情况下同一时刻其他免费模型仍可用，属模型级而非账号级。

> **CodeArts 的临时凭证过期归 SOFT**：`HTTP 400` + `APIG.0602 security token has expired`，识别在 `401/403 → DEAD` 之前（标记 `_STS_EXPIRED_MARKERS`），判 `SOFT`（账号级可恢复故障：下一轮预刷新换上新 STS 即自愈）。不能判 `DEAD`——硬禁用会让 `RefreshTask` 跳过该凭证，反而断掉唯一自愈路径。

**业务码一律从 `"code": N` 键值形态提取**（`provider.base.business_codes`），不搜裸数字：`"code":111020` 含 `11102` 子串，裸匹配会把无关响应判成模型错误。判定顺序即优先级，`429+6004` 必须先于 `429`、`402/14018` 必须先于状态码兜底。**内容风控指纹（11128）**：`400 + 11128 Illegal API invocation from an unapproved channel` 归 REQUEST，主因是**内容风控**——`system`/`assistant` 消息正文出现「伪装其他厂商官方客户端」的指纹串（如 Claude Code 系统提示身份声明行、billing 头字段名、git 上下文行）时整单拒绝。特征：与凭证无关（换号无效）、确定性复现、仅这两个角色的 `content` 命中（`tool_calls` 参数与 reasoning 均不触发）；会话一旦把指纹写进历史，后续每轮（含压缩请求本身）都持续 11128。处理：出站前 `sanitize_channel_markers` 把它替换为占位符（`CODEBUDDY_SANITIZE_CHANNEL_MARKERS=false` 关闭），客户端历史不受影响；`content` 为文本块列表时逐块处理。指纹清单在 `provider/codebuddy/client.py::CHANNEL_MARKERS`，此处刻意不写原文以免污染阅读本文件的 agent 会话。
### 3.3 三态健康度
```python
@dataclass(slots=True)
class Quota:
    remaining: float | None = None
    total: float | None = None
    cycle_end: int | None = None      # 最早到期 epoch；无到期信息为 None
    expiry_ladder: list[tuple[int, float]] | None = None  # [(到期 epoch, 该包剩余积分)]
    packages: list[dict] | None = None  # [{"name","total","used","end"}]，仅展示
    probed_at: int | None = None
    probe_failed: bool = False

UNKNOWN: int | None = None
EXHAUSTED = -1
# HealthScore：0-100 已知；None 未知；-1 已耗尽

def health_score(q: Quota | None) -> int | None:
    if q is None or q.probe_failed:
        return UNKNOWN
    if q.total is None or q.total <= 0:
        return EXHAUSTED
    return max(0, min(100, round((q.remaining or 0.0) / q.total * 100)))
```

调度排序：`known DESC > unknown（中性参与）> exhausted(-1)`。`probe_failed` 与「探测到 0」必须区分——Zen/Kilo 虚拟凭证恒返回 `probe_failed=True`（未知，**不是耗尽**）。**CB `remaining` 的取值口径**：个人版额度探测一律取逐包 `CycleCapacityRemainPrecise` 之和，**不得**用响应顶层的 `TotalDosage`——后者是上周期口径（本周期消耗记在 `CycleCapacityUsed`），采用它会出现 `remaining == total`、健康度恒 100、并与到期阶梯 / 额度明细自相矛盾。企业版仍走 `limitNum - credit`（另一接口，无此问题）。
### 3.4 截断续写
上游以 `finish_reason == "length"` 结束本轮流时，同凭证自动续写，最多 `AUTO_CONTINUE_MAX`（默认 10，0 关闭）。实现在 `src/engine/continuation.py` 的 `ContinuationStream`：包装上游事件流，截断则追加「已产出正文（含 reasoning）+ 续写指令」重发，并把输出上限两键归零（否则在同一处再次截断），跨轮累计 usage，末端补发一条累计 usage + 最后一轮真实 `finish_reason`（上游整轮未报 usage 时不补发，避免出口把「未上报」误报成 0）。做成事件流包装器而非 executor 内重跑：凭证固定，轮换 / 记账 / 统计零改动。**只实现 `length` 一个判据**：「仅 reasoning 无正文」只在客户端下发 `max_tokens` 时出现，而生产路径只发 `reasoning_effort`、本网关也不做 max 键映射，故该形态不可达；「空正文无工具调用」「代码块未闭合」无证据支撑，均不做——凭空加启发式会把「模型就是想空回」误判成截断。**输出上限键行为**：`max_completion_tokens` 被 CB 上游**完全忽略**，`max_tokens` 才生效（精确截断 + `length`）；两键同发时后者胜出；TRAE 对两个键**都不生效**；`enable_thinking: false` 亦被上游忽略（仍产 reasoning 且计入 `max_tokens`）。**`reasoning_effort` 缺失时补 `medium`**：不带该字段时 CB 上游把整段思考以「可见推演」写进 `delta.content`（`delta.reasoning_content` 恒空、`usage.reasoning_tokens` 恒 0），正文里混着自我质疑；带上任意档位即刻恢复独立思考通道。官方 CLI 的 dump 都带该字段，非官方 CLI 客户端不发，是唯一触发面。`stream_chat` 用 `setdefault` 补缺省、**不覆盖客户端显式值**。
### 3.5 会话粘性键
（`src/engine/affinity.py`）键优先级 `conversation_id` > `conversationId` > `prompt_cache_key`（同键名先 `metadata` 对象再请求体顶层）→ 无显式标识时回落「用户名 + 消息增量前缀指纹」；带 `user_id`（顶层或 `metadata` 内）时不派生兜底键（否则同用户的并行对话前缀可能相同，会被误钉到同一凭证）。`prompt_cache_key` 是 OpenAI 官方顶层参数、`metadata.user_id` 是 Anthropic Messages API 官方字段；`conversation_id`/`conversationId`/顶层 `user_id` 非标准键，作兼容探测接受（命中即用、未命中无害）。键里掺入用户名防止不同 API 用户串号；TTL（`CONVERSATION_STICKY_SECONDS`）内存态，重启后丢粘性只影响一轮选号，条目数上限 512 超量先淘汰过期再按最旧淘汰。
### 3.6 模型元数据与黑名单
**模型元数据**：两边上游的模型配置接口都直接给出推理元数据，网关透传为 `/v1/models` 的 OpenAI 额外字段：

| 字段 | CB 来源 | TRAE 来源 | 取值 |
|---|---|---|---|
| `supports_reasoning` | `supportsReasoning` | `display_config.model_capability`（`reasoning_model`→true、`chat_model`→false），缺失回落 `reasoning_effort_config.support_thinking` | true / false / null |
| `default_effort` | `reasoning.effort` | 无对应字段 | CB: `high` / `medium`；TRAE 恒 null |

`supported_efforts`（可接受档位集合）**不实现**：上游只给「默认档位」不给档位清单，凭空枚举 ChatGPT 三档属编造。CB 的 `onlyReasoning`/`canDisableThinking` 同样**不透传**（语义未经核实）。逐字段补缺（多上游同名模型先到先填、后到只补 None）。**黑名单**（`MODEL_BLOCKLIST`，fnmatch glob，只影响列表展示、直连不受影响）。默认模式含上游内部/代理模型与**确认不可用**的噪音模型：`custom_model_*` / `*sub*agent*` / `summary` / `browser_use_*` / `file_search_agent`，以及 `default`（HTTP 200 但零内容）、`hunyuan-image-*`（400 `11103` backend not supported）、`hy4-preview-x`（400 `11102` service info not found，下架）、`qwen3.8-flash`（Qoder `qfmodel` 400「Execution failed: null」）、`glyph-cluster`（Kilo 两次 121s 后 408）。**按归一后的对外写法匹配**（`_block_names`）：原代号、原代号归一键、展示名、展示名归一键四种写法取并集，用户照列表里看到的 `kimi-k3` / `Kimi K3` 写也能生效；保留原代号是因为默认规则带下划线（`custom_model_*`），归一键把下划线换成连字符后不命中；清洗后退化为空的名字不参与匹配。**非模型级问题不进黑名单**：CodeArts `deepseek-v4-*` / `glm-5.3-flash` 的 `InferHub.4291.200 insufficient quota` 与 `TM.00001041` 是账号级，Kilo `poolside/laguna-xs-2.1` / `nemotron-3-nano-omni` 的 429·502 与 `thinkingmachines/inkling-small` 的 `limit_rpd` 日配额都会自行恢复。**两条存疑未加**：Qoder `mmodel`（MiniMax-M2.7）回 inner body `null` 触发协议异常，但与 CB 正常的同名模型合并成一条，滤掉会连带隐藏可用渠道；zen `ling-3.1-flash-free` 两次 429，同样与 Kilo 可用的同名模型合并。**黑名单热更与列表缓存**：`MODEL_BLOCKLIST` 是热更项，但 `list_models` 的缓存只存**未过滤原始表**，过滤在每个出口现做（`_visible()`，命中缓存 / 成功拉取 / 失败兜底三条路径都过）；前端保存后显式 `invalidateQueries(["playground-models"])`，黑名单与列表两条缓存都立即生效，不依赖 TTL。**失败也进 TTL（负缓存）**：`model_list_fetched_at` 记**上次尝试**时间（成功或失败都刷新），TTL（300s）内不再打上游——有缓存用缓存，没缓存跳过该渠道。
### 3.7 Responses 出口（Codex CLI）
`POST /v1/responses`：`src/api/responses.py`（端点）+ `src/compat/responses/request.py`（入站映射）+ `src/compat/responses/response.py`（出站翻译）。与 `/v1/chat/completions` **共用同一个 executor**，选号 / 冷却 / 轮换 / 记账 / 会话粘性全部零改动；引擎侧唯一改动是 `Executor.stream(..., translator=...)` 注入缝——出口形状由 translator 决定，引擎不再硬编码 chat 形状的 SSE 帧。字段形状全部取自官方 `openai` Python SDK（3.x）由 OpenAPI 生成的类型定义，并用该 SDK 作客户端做端到端契约验证。**入站映射**（Responses → 内部 chat 载荷）：

| Responses | chat |
|---|---|
| `instructions` | 首条 `system` 消息 |
| `input` 字符串 | `user` 消息 |
| `input[].type=message`（`input_text`/`output_text` part） | `messages[]`；`developer` 角色改写为 `system` |
| `input[].type=function_call` | assistant 消息的 `tool_calls[]` |
| `input[].type=function_call_output` | `tool` 消息 |
| `input[].type=reasoning` | assistant 消息的 `reasoning_content`（仅明文部分） |
| `tools[].type=function`（**扁平** `{name, description, parameters}`） | 嵌套 `{type:function, function:{...}}` |
| `tool_choice` 字符串 / `{type:function,name}` | 同名 / 嵌套形式 |
| `max_output_tokens` | `max_tokens`（CB 上游只认这个键） |
| `reasoning.effort` | `reasoning_effort` |
| `text.format`（`{type,name,description?,schema,strict?}` 扁平） | `response_format`（`text`/`json_object` 直通；`json_schema` 展开成嵌套；缺 name/schema、未知 type 显式 400） |
| `temperature` / `top_p` / `parallel_tool_calls` / `prompt_cache_key` / `text.verbosity` | 同名透传 |

**出站事件序列**（流式）：`response.created` → `response.output_item.added`（message / reasoning / function_call）→ `response.content_part.added`（output_text）→ `response.output_text.delta × N` → `response.output_text.done` → `response.content_part.done` → `response.output_item.done` → `response.completed | response.incomplete`。思考内容用独立 `reasoning` item + `response.reasoning_summary_text.delta/.done`（不混进正文）；工具调用用 `function_call` item + `response.function_call_arguments.delta/.done`。**Responses 协议没有 `[DONE]` 哨兵**，终止事件本身就是流结束（`done_sent` 与之对齐，供 executor 区分「正常收尾」与「中途断开」）。流式已完成后再来的 FINISH/ERROR 一律忽略。`finish_reason=length` → `response.incomplete`（`incomplete_details.reason=max_output_tokens`），`content_filter` 同理；其余为 `completed`。流内错误以 `response.failed` 表达，错误码映射成 Codex CLI 认识的取值（`no_healthy_credential` → `server_is_overloaded`）。**刻意接受的取舍**：`include=["reasoning.encrypted_content"]`（Codex CLI 每轮必带）**接受并忽略**——400 会直接打死主客户端；其他 `include` 值才 400。`previous_response_id`（Codex 不发该字段）与 `store=true` 保持 400（本网关无状态）。Codex CLI 按 SSE 的 `event:` 行分派事件，故 `event:` 与 `data.type` 两个字段都必须发；`response.completed.response` 在它那边是强类型解析（`id` 必填、`usage` 含三个 token 字段），故按官方必填字段完整发出。Codex 回传历史里的 `reasoning`/`compaction` 只有密文、没有 chat 等价物，**有意无损丢弃**；其余 Responses 私有 item 类型（`local_shell_call`/`custom_tool_call` 等）显式 400。**验证状态**：以官方 `openai` SDK（流式 / 非流式 / 工具调用）作权威客户端跑通全部契约，并对真实 CB 上游冒烟；**未经真实 Codex CLI 端到端验证**（开发环境无 CLI）。
### 3.8 Anthropic Messages 出口（Claude Code）
`POST /v1/messages`（流式 + 非流式）与 `POST /v1/messages/count_tokens`，供只走 Anthropic 协议的客户端接入。与 `/v1/responses` 同一模式：新增 `compat/anthropic/{request,response}.py` + `api/messages.py`，**复用同一 executor**。**入站映射**（`request.py`，映射成内部 `ChatRequest.raw`，只实现 chat 子集）：
- `system`（字符串或 text 块数组）→ 一条 `system` 消息，置于最前；空则丢弃。
- `messages[].content` 为字符串时按角色直传；为块数组时：`text` → 文本、`tool_result` → `tool` 消息（`tool_use_id` → `tool_call_id`）、`image`/`document` → 显式 400。user 的连续块按类型**拆成多条**消息（文本与 tool_result 交替），assistant 的块**合并成一条**（`thinking` → `reasoning_content`、`tool_use` → `tool_calls`、`redacted_thinking` 丢弃）——与上游 chat 的消息模型对齐。
- `tools[].input_schema` → `function.parameters`；`tool_choice`：`auto`/`none` 直传，`any` → `required`，`tool` → `{"type":"function",...}`；`stop_sequences` → `stop`；`max_tokens`/`temperature`/`top_p` 直传，`top_k` 丢弃（chat 无等价物）。

**出口翻译**（`response.py`，实现 executor 的 `StreamSink` 协议）：`message_start` → `content_block_start`/`content_block_delta`/`content_block_stop`（文本 `text_delta`、思考 `thinking_delta`、工具 `input_json_delta`）→ `message_delta`（`stop_reason` + usage）→ `message_stop`。Anthropic 协议**没有 `[DONE]` 哨兵**，`message_stop` 即流结束；thinking 块在 `content_block_stop` 前补一个占位 `signature_delta`（客户端要求，本网关不产真签名）。错误用 `event: error`（`_error_type_for` 把上游错误码映射成 Anthropic 错误类型）。**usage 缓存字段**：内部 `Usage.input_tokens` 是 OpenAI 口径的 `prompt_tokens`（**含**缓存命中），而 Anthropic 语义里 `input_tokens` 与 `cache_read_input_tokens` / `cache_creation_input_tokens` 互斥（`input_tokens` 只计未命中）。故 `_usage_payload` 与 `completion_to_message` 统一拆分：命中时 `input_tokens = max(0, prompt_tokens − cached_tokens)` 并另记 `cache_read_input_tokens = cached_tokens`，不减会让 Claude Code 重复计费；命中为 0/None 时不补占位字段。**鉴权**：`deps.api_key_user_anthropic` 先读 `x-api-key`（Anthropic SDK 的 `ANTHROPIC_API_KEY`），为空再回落 `Authorization: Bearer`（`ANTHROPIC_AUTH_TOKEN`），两者共用 `_api_key_principal`。客户端 Base URL 填到根，SDK 自行拼 `/v1/messages`。`count_tokens` 用与上下文压缩同一套启发式估算本地计数（不转发上游）。
### 3.9 上下文压缩
**动机**：上游对输入长度有硬限制（CodeBuddy `11115 prompt is too long: … tokens > … maximum`）。反代若原样转发，长会话必然撞墙，客户端只看到裸的 400，而换号无意义（每个号上限一样）。**只做确定性的「估算 → 裁剪」**，不做「超限后压缩再重试」的放大路径。实现分两层：纯函数 `engine/compress.py` + 装配闭包 `api/context.py`（`build_context_compressor`，由 main.py 注入 `ExecutorDeps.context_compress`）。

- **token 估算**：中文 0.55 tok/字、数字 0.33、其他 0.25。刻意不用「3 字符 ≈ 1 token」的英文口径——它会把中文低估约 1.6 倍，于是「以为装得下、其实装不下」，压缩根本不触发。
- **预算**：`模型上限 × safety_ratio − reserve_for_output`。
- **裁剪**：`system` 永久保留（丢了会改变模型行为）；`assistant.tool_calls` 与其后连续的 `tool` 结果**同组同生共死**（只删一半会让上游报 tool_call_id 找不到）；至少保留最近 `min_keep_messages` 条；其外从最新往最老贪心回填。裁剪后仍超限则截断最长的**非 system** 消息内容（留头尾 + 标记）。
- **未知上限不压缩**：模型目录里查不到 `max_input_tokens` 时直接跳过——宁可不裁剪，也不拿一个猜的数字去砍用户上下文。故本功能对未知模型**零副作用**。
- **多渠道按实际渠道取值**：同名模型可能挂多个渠道、各自上限不同（qoder 180K vs codebuddy 1M）。压缩从 API 入口移到 **executor 选号之后**（`ExecutorDeps.context_compress`），按实际服务渠道的上限裁剪——路由前只能取 min 的口径会把大窗口渠道的会话反复误裁，前缀不稳定击穿上游前缀缓存。压缩实现无副作用：每轮从原文重压（换号/回退可能落到不同窗口的渠道），affinity 指纹读未压缩原文。

热更项 `context_compress_enabled`（默认 true）/ `context_compress_reserve_tokens`（4096）/ `context_compress_min_keep_messages`（4）/ `context_compress_safety_ratio`（0.95）。
### 3.10 API Key 的绑定、白名单、来源 IP 与到期时间
在 Key 级策略上叠加四项（**不做配额**），`api_keys` 增四列：

| 列 | 取值 | 空值语义 |
|---|---|---|
| `provider_binding` | 见 `KNOWN_PROVIDERS`（七渠道） | 空 = 自动（跨渠道选健康凭证） |
| `allowed_ips` | 逗号分隔 IP/CIDR | 空 = 不限制来源 IP |
| `allowed_models` | fnmatch glob、逗号分隔 | 空 = 不限制 |
| `expires_at` | epoch 秒 | NULL = 永不过期 |

- **渠道绑定**：选定渠道后该 Key 只在该渠道凭证里选号；模型属另一渠道时直接 400 并指出实际归属（不静默改道）。`模型@渠道` 与绑定冲突时同样 400。执行在 `Executor._apply_binding`（`resolve_target` 内），与 `@provider` 强制指定共用收窄路径；绑定把候选固定为该渠道并置 `forced`（跳过模型目录收窄）。目录未就绪时不做归属判断，保守放行。
- **来源 IP 判定**在 `deps.api_key_user`（鉴权当场判掉）：**默认不采信 `X-Forwarded-For`**（客户端可写）；仅 `TRUST_PROXY=true` 时按 XFF 判定、取**最后一个**条目。策略是纯函数（`auth/access.py`）：写入时 `normalize_allowed_ips` 校验规范化（`10.0.0.1` → `10.0.0.1/32`）、非法值 400；读取宽松解析、脏条目丢弃，整份白名单一条都解析不出则**拒绝**（fail closed）。命中失败返回 **403**（与 401「凭证无效」区分）。
- **模型白名单**：三个 /v1 出口（chat / responses / messages）解析后校验；省略模型名（空 / `auto`）时按 `default_model` 判定，**不能借空名绕过**；匹配大小写不敏感，`模型@渠道` 后缀不参与匹配；`/v1/models` 也按白名单过滤展示。写入时 `normalize_allowed_models` 校验。
- **到期时间**：`deps._api_key_principal` 在鉴权当场判过期（过期与「Key 不存在」**统一 401 文案**，不泄露可枚举信息）。

`ApiKeyPrincipal` 返回 `(username, key_id, provider_binding, allowed_models)`；`ApiKeyRepository.verify` 保留为只回用户名的薄封装。

## 4. Provider 协议
```python
class Provider(Protocol):
    id: str

    # 凭证生命周期（上游协议私有，必然在 provider 内）
    def start_auth(self, callback_url: str) -> AuthSession: ...   # flow=poll|callback|paste
    def poll_auth(self, state: str) -> AuthResult | None: ...     # 仅 poll 轨道
    async def complete_callback(self, raw_url: str, state: str) -> dict: ...  # 仅 TRAE
    def import_credential(self, raw: dict) -> dict: ...
    def refresh(self, credential_data: dict) -> dict: ...

    # 额度探测（调度器依赖：健康度 + 到期阶梯）
    async def probe_quota(self, credential_data: dict) -> Quota: ...

    # 执行与分类
    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]: ...
    def classify(self, status: int, body: bytes) -> ErrKind: ...
    def list_models(self, credential_data: dict) -> list[Model]: ...
    async def aclose(self) -> None: ...
```

> 上表与 `src/provider/base.py` 的 `Provider` 协议严格一致；两处定义必须同步（改任一处必须同步另一处）。

约定：
- 凭证在引擎与 provider 之间以 dict 传递（解密后的 `data_enc`）；`credential_from` 等能力把 dict 还原为 provider 私有 dataclass，provider 不接触 sqlite。
- `stream_chat` 只产出 `Event`，产出前先做 HTTP 状态码检查；`classify` 由 executor 调用。
- 可选能力**不实现即不定义**（不是抛 `NotImplementedError`）：调用方用 `getattr`/`hasattr` 探测，缺失时返回 400「该凭证不支持此操作」，而不是 500。当前可选集：`start_auth`/`poll_auth`（仅支持 poll 的 provider）、`complete_callback`（仅 TRAE）、`list_accounts`/`switch_account`（仅 CodeBuddy）、`credential_from`/`checkin_scope`（刷新与签到任务的能力探测）、`checkin`/`checkin_status`（签到）、`growth`（成长中心，仅 CodeBuddy）、`host`（展示用）。
- 七条渠道中 CodeBuddy / TRAE / Zen / Kilo / Qoder 共用 `engine/sse.py` 的帧解析器（SSE 规范层），事件语义各自映射；CodeArts 的私有「累计全文」形状无法用 `FrameAssembler` 表达，自实现 `iter_data_lines`，只复用 `engine/sse` 的 `MAX_SSE_LINE_BYTES` / `SSEFrameTooLarge`。
- Zen 与 Kilo 是最「薄」的两个 provider：标准 OpenAI SSE，`events.py` 无需私有信封解析。两者都**只实现** `import_credential`/`classify`/`probe_quota`/`list_models`/`stream_chat`/`aclose`，签到 / 成长 / 刷新一律靠「方法缺失」自然跳过。

## 5. 各渠道协议实现
### 5.1 OpenCode Zen 免费层（`zen`）
Zen 免费层是**标准 OpenAI 流式协议**（`chat.completion.chunk` + `data: [DONE]`），`provider/zen/events.py` 很薄：`content` / `reasoning_content` / `tool_calls` / `usage` / `finish_reason` / 流内 `error` 各一条映射。唯一要留意的是**最后一段正文与 `finish_reason` 会同帧**，故 `parse_all_events` 必须能在同一帧里同时产出 CONTENT 与 FINISH。上游 `[DONE]` 之后还跟一帧 `{"choices": [], "cost": "0"}`，无下游语义，跳过。**渠道私有的只有门禁伪装**（集中在 `zen/client.py`）：

| 条件 | 要求 | 违反后果 |
|---|---|---|
| `User-Agent` | `opencode/<version>`，version ≥ 1.18.0 | 低于阈值 → 426；无版本号 → 403 |
| `x-opencode-session` | `^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$`，且**前 12 位 hex 须解码回近期时间戳** | 403 |
| body `stream` | 必须 `true` | 403 |
| body `tools` | 必须**同时**含 name 为 `bash` 与 `read` 的工具（参数可为空壳） | 403 |
| `Authorization` | `Bearer public` 可带可不带（匿名也通） | 带伪造 key → 401 |

> **会话前缀必须编码近期时间**：门禁不只校验 session 的**形态**，还要求 12 位 hex 前缀
> 解码回**近期时间**，否则一律 `403 FreeTierError`（随机 hex 形态合规但语义不对）。
> 编码照官方 `Identifier.create`（`sst/opencode` `src/id/id.ts`）：把毫秒时间戳左移
> 12 位（`ms * 0x1000`）再 `+1`，**按位取反**，取低 48 位写成 12 位小写十六进制；
> 后 14 位内容自由。复刻见 `new_session_id` / `_session_time_prefix`。**门禁在鉴权之前**：
> 带真实 `oc_sk_*` key 反而 401，`Bearer public` 才 200，故本渠道始终不带用户 Token。

UA 版本走 `ZEN_OPENCODE_VERSION`（上游改阈值改 env，不硬编码）；`ZEN_API_ENDPOINT` 走端点白名单（`ZEN_ALLOWED_ENDPOINTS`）。Zen **不携带任何用户 Token**，白名单只为防误配。**403 FreeTierError 抖动在客户端内重试一次**：上游偶发对同一请求回 `403 {"type":"error","error":{"type":"FreeTierError"}}`（门禁四项每次都满足，数秒后的同请求即 200）。403 归 `ErrKind.REQUEST` → 不冷却也不跳过渠道，但 zen 只有**一条虚拟凭证**，轮换候选随即耗尽 → 对外 503。故 `stream_chat` 在客户端内部先消化抖动：退避 `GATE_403_RETRY_DELAY`（1s）后换新 session id、关旧连接重发一次（`GATE_403_RETRIES=1`），重试仍 403 才上抛。只对 403 重试，其余状态码仍一次上抛。**注入空壳 + 改写/过滤回包**：门禁要求 tools 含 `bash`/`read`，但这两个可能是**我们伪造的**、客户端从没声明过（如 OpenCode 的执行工具叫 `shell`）。模型偏好调 `bash` 这个名字，直接吞掉会让客户端收到「空正文 + stop」提前结束回合。故两级处理：① 客户端声明了同义工具（`TOOL_SYNONYMS`，`bash`→`shell`/`execute`、`read`→`read_file`）→ 把注入名的 tool_call 函数名**改写**成同义工具原样透传，注入时还复制同义工具的 description/parameters；② 没有同义工具 → 才整块丢弃并记 WARNING。实现只在**缺失时**补骨架工具（用户自带同名工具 → 不注入、不改写、不过滤，绝不误伤）；处理按 `index` 跟踪流式 tool_call 分片；若整条流无任何保留的真 tool_call，则把 `finish_reason=tool_calls` 收敛为 `stop`。**免费模型清单完全动态（现拉现探）**：上游 `/zen/v1/models` 免鉴权，但返回**全部**模型（含 70 多个付费模型），且**不带任何免费/付费标记**（`owned_by` 恒 `opencode`；亦无 cost 字段）。唯一权威信号是**匿名可用性**：付费模型恒 401 `Missing API key.`，免费模型永不 401。故两步过滤：① 按 `-free` 后缀收窄候选；② 对候选并发探活，**只保留 2xx**（已下线 400、区域限制 403、上游故障 5xx、超时都剔除）。无静态白名单，上游增删免费模型自动跟随；探活结果为空时抛协议错，让 `/v1/models` 用上次成功的缓存兜底。**判活结果缓存 30 分钟**（`MODELS_CACHE_TTL_SECONDS`，比服务层 `MODEL_LIST_TTL_SECONDS` 300s 长一档）：探活是列表链路里最贵的一步（逐个真发一次推理，总耗时等于最慢那个），服务层每 5 分钟到期重拉列表时直接复用。**免费模型显式 x0 倍率**：`fetch_models` 给每个存活候选 `credit_rate=0.0`（而非 `None`）。

**虚拟凭证行**：Zen 无凭证、无额度接口。池里种一条空凭证（`credential_data={}`，`added_by="system"`），复用现有调度 / 冷却 / 统计 / 会话粘性。`probe_quota` 恒返回 `Quota(probe_failed=True)` → `health_score` 返回 `None`（**未知**，不是 `EXHAUSTED=-1`）——这点很关键：若返回 `total=None`，`health_score` 会判定为「已耗尽」而把这条免费渠道错误降级。种子幂等（已有 zen 凭证则不补），**用户删除后重启会复活**，永久停用请用「暂停」。只为默认装配路径种子（测试注入自定义 registry 时不多出凭证行）。
### 5.2 Kilo Gateway 免费层（`kilo`）
Kilo Gateway（`api.kilo.ai/api/gateway`）对外是**标准 OpenAI 兼容协议**，既无私有信封，也**没有 Zen 那种门禁伪装**。`provider/kilo/events.py` 与 Zen 一样薄，唯一区别是**思考字段在 `delta.reasoning`**（Zen 是 `reasoning_content`），另带 `reasoning_details` 结构，忽略即可。`parse_all_events` 同样处理同帧 finish，并额外补 usage（Kilo 的 `usage` 常与收尾 `choices` 同帧）。**免费模型有权威标记、故不做探活（与 Zen 相反）**：`/models` 每个条目带 `isFree` 布尔（含 `kilo-auto/free`、`stealth/space-bunny-alpha` 等**无 `:free` 后缀**者）。故直接按 `isFree is True` 过滤，**不做探活**——探活会白耗本就极小的免费配额（网关级约 200 req/h/IP），且结果随上游免费池（实为 OpenRouter 免费池转发）波动不稳定。无静态白名单；过滤结果为空时抛协议错，让 `/v1/models` 用上次成功的缓存兜底。元数据（`name` / `context_length` / `top_provider.max_completion_tokens` / `supported_parameters` 含 `tools` / `architecture.input_modalities` 含 `image`）透传为中立 `Model`；免费模型显式标 **x0 倍率**。**虚拟凭证行**（同 Zen）：`credential_data={}`、`added_by="system"`，`probe_quota` 恒 `Quota(probe_failed=True)` → `None`。种子幂等、**删除后重启复活**、永久停用请「暂停」。**错误分类**：401→`INVALID`（无凭证，401 只表示该模型需要付费 key/BYOK）、400/404/422→`INVALID`、403→`REQUEST`、**429 与 502/503/504→`MODEL`**（见 §3.2）。限流交引擎处理，本包不自建熔断。**独立 pacer**：`KILO_CHAT_MIN_INTERVAL`（热更项，默认 0 = 不节流）走独立 `Pacer`，与 zen / CB / TRAE 互不排队；`stream_chat` 在 `finally` 里 `pacer.release("kilo")` 归还并发名额。
### 5.3 Qoder（`qoder`）
**与 zen/kilo 的本质区别**：Qoder 是**真实账号渠道**，凭证必须登录获得、加密入库，走与 CB 相同的设备码轮询登录轨道，但**不种虚拟凭证**——没有 token 就是不可用。**私有 COSY 协议**：推理 `POST {gateway}/algo/api/v2/service/pro/sse/agent_chat_generation`。请求体不是 JSON，而是**自定义 Base64 变体**（三段轮转 + 自定义字母表 + `=`→`$`）；认证不是标准 Bearer，而是 `Authorization: Bearer COSY.<payload_b64>.<md5sig>` 加上整套 `cosy-*` 头。签名串为 `md5(payload_b64 + "\n" + cosy_key + "\n" + date + "\n" + body + "\n" + path)`（`path` 去掉 `/algo` 前缀，query 不参与签名）；`payload` 是键排序紧凑 JSON `{cosyVersion,ideVersion,info,requestId,version}`；`cosy_key` 由临时 AES 密钥经服务端 RSA 公钥加密得到，`info` 是该临时密钥 AES-128-CBC 加密的身份 JSON。密码学复用项目已有 `cryptography`（AES-128-CBC + RSA PKCS1v15）。`cosy-machineid`/`cosy-machinetoken` 按 `uid` 稳定派生（同一账号长期同一虚拟设备，避免随机机器码触发风控）。**信封式 SSE**：响应每行是 `data:{"headers":…,"body":"<内层 OpenAI chunk>","statusCodeValue":200}`——即**外层信封包内层 chunk**。解析器逐帧解出 `body`，`body=="[DONE]"` 结束，`statusCodeValue != 200` 判上游错误（HTTP 仍可能 200）。内层仍是标准 OpenAI chunk，事件映射与 kilo 近似（`clean_delta` 顺带剔除网关补的空噪声 delta：`extra_fields`/`refusal`/空 `reasoning_content`/空 `tool_calls`）。**模型发现**：`GET {gateway}/algo/api/v2/model/list?Encode=1`，**必须带整套 COSY 签名头**（签名 body 为 `qoder_encode("")`）。裸 GET（无 COSY 头）会 403；方法固定 GET（带头用 POST/PUT 会被上游 400「Request method ... not supported」拒绝）。清单条目带 `key`（内部代号，如 `qmodel_38max`）与 `display_name`（可读名，如 `Qwen3.8-Max`）：`id` 用 `key`、`name` 用 `display_name`。**`price_factor` 即「Credit 消耗倍率」**：来自服务端模型列表的 `price_factor`，与全站 `credit_rate` 同义（越小越省），直接映射；免费模型上游给 `0.0`。注意它是**相对倍率**，不是每次任务的固定积分。

**签到（活动制）**：`GET /sash/api/v1/me/campaigns`（**必须带 `Cosy-ClientType: 10`**，否则上游静默返回 `campaigns:[]`）→ 筛 `actionType=="CLAIM_BENEFIT"` 且 `claimStatus=="CLAIMABLE"`（`VIEW_DETAILS` 等展示位也是 `CLAIMABLE`，不筛 actionType 会领错活动）→ `POST /sash/api/v1/me/campaigns/{campaignId}/claim`（同样带 `Cosy-ClientType: 10`）。claim 响应 `replayed:true` 是幂等重放、`status=="BLOCKED" && failureCode=="SAME_PERSON_ALREADY_CLAIMED"` 是同一自然人已领，两者都归一为 `already_checked_in=True`；其它 `BLOCKED` 才是真失败。**新协议不提供连续天数**，`streak_days` 恒为 `None`。仅当活动制接口本身 404/405/410 时才整体回退旧的 `daily-check-in` 流程；活动制与旧接口都不可用才算「本区域无此接口」，记为 skipped（`ok=False` 但不算失败）——**国际版该端点 404**。**签到窗口 ≠ 自然日，故按窗口封账**：上游签到活动每日 **10:00（UTC+8）** 才轮换 `campaignId`，当前窗口 `startAt 10:00 → endAt 次日 09:59`。按自然日封账会在凌晨把上一轮窗口的 `CLAIMED` 判成「今日已签到」而封掉整天，故 `CheckinResult.seal_until`（epoch 秒，缺省 None = 当天结束）由 Qoder 的活动制路径回填 `checkin_window_end(payload)` = 签到类活动里**最晚**的 `endAt`（展示位 `VIEW_DETAILS` 的 `endAt` 不采信，缺失/非数值/0 → None 回落原口径）。`CheckinTask._done_scopes` 为 `dict[done_key → 截止时刻]`，跳过判据 `_sealed()`（`until > now`）；旧 `daily-check-in` 回退路径与其它渠道仍走自然日封账。**额度**：`GET {openapi}/api/v2/quota/usage` 的 `userQuota` + `addOnQuota` 合成 `remaining`/`total`，`cycle_end` 取最早 `expiresAt`。

**上游节点故障归类**：Qoder 把**自身推理节点故障**包成 400——`{"code":"400","message":"[FAIL]node:… msg:Execution failed: null"}`（HTTP 与信封 `statusCodeValue` 都是 400），而同批其他模型正常。这与「模型不存在 / 请求无效」的 400 语义不同：换凭证不解决但会自愈。故 `classify_error_code` 对 400/404/422 增加响应体判据：命中 `NODE_FAILURE_MARKERS = ("[FAIL]node:", "Execution failed")` → `ErrKind.MODEL`（模型级瞬时冷却），否则维持 `INVALID`。安全前提：探测未知模型名（乱码/空串）上游既不返回 ERROR 也不带该标记，故不会误伤。耗尽轮换且**全部候选都因该模型处于模型级冷却**时，503 文案用 `model 'x' temporarily unavailable on upstream`（可附相近模型建议）而非 `all credentials unavailable`。**节点白名单**：`QODER_ALLOWED_ENDPOINTS` 同时含国内 openapi+gateway 与国际版；`_qoder_endpoint` 启动时校验。`QODER_CHAT_MIN_INTERVAL`（热更项，默认 5s）走独立 pacer。
### 5.4 CodeArts（`codearts`）
**华为云签名体系 + 私有 SSE 帧**。**SDK-HMAC-SHA256 签名**：鉴权用 AK/SK + `X-Security-Token`，`Authorization: SDK-HMAC-SHA256 Access=<AK>, SignedHeaders=…, Signature=…`。三处必须逐字节对齐：signedHeaders 是**请求全部头**（小写、字典序）；CanonicalURI **每段单独 percent-encode 且末尾补 `/`**（漏尾斜杠是 APIG.0301 的常见原因）；payload hash 取 `X-Sdk-Content-Sha256`。`signer.py` 写成纯函数（不含「当前时间」）以便固定向量测试。**DPoP 令牌刷新（刚性约束）**：`POST {sts}/v1/oauth2/tokens` `grant_type=refresh_token` 需带 **DPoP(ES256/P-256) JWS**。refresh_token 与 `client_id=codearts-agent` + DPoP 私钥**三者绑定、一次性**——刷新成功必须把新 `refresh_token` 回写凭证（`RefreshTask` 先落库再同步账号）。ES256 用 `cryptography`（`ec.SECP256R1` + `ECDSA(SHA256)`）并**低 S 归一化**（`s > n/2` 时取 `n - s`）——华为 STS 校验要求低 S，而 `cryptography` 不保证（实测 2000 次签名有 991 次 high-S）。私钥必须随 refresh_token 一起持久化并原样复用（STS 把 refresh_token 绑定在首次换取时的 DPoP 公钥上）。**刷新节流**：`REFRESH_SKEW_HOURS` 默认 24h ≫ STS 寿命 2h，而 `needs_refresh` 语义是「离到期还剩 skew 秒就刷」，故给 `CodeArtsCredential` 加 `refresh_skew_cap_seconds = 2700`，`needs_refresh` 取 `min(skew, cap)`：把「一进窗口每轮都刷」降成「离到期 45min 内才刷」。**窗口必须宽于轮询周期**（`REFRESH_INTERVAL_MINUTES`，默认 30min）：窗口 ≤ 周期时轮询点整轮落在窗口之外，凭证拖到到期才刷（实测封顶 30min、周期 60min 时在距到期约 26s 才刷新，上游回 `APIG.0602`）。封顶 45min > 30min 周期留余量；轮询粒度下实际在**剩余约 30min** 处刷新。

**SSE 帧（两种形状同时兼容，按字段分派）**：逐行 `data:` JSON（行间有空行；也有不带 `data:` 前缀的裸 JSON 行），最后由 `data:[DONE]` 结束。**v2 `/api/v2/chat/completions` 是标准 OpenAI chunk**：`{"choices":[{"delta":{"content":…,"reasoning_content":…,"tool_calls":…},"finish_reason":…}]}`，增量在 `delta`，收尾帧 `choices:[]` + `usage` 单独给 token 数；带 `tool_stream:true` 时工具调用分片在 `delta.tool_calls`。旧形状则是 `{"text":"<累计全文>"}`（**替换语义**，用 `TextSnapshot` 做差：新文本以旧文本为前缀 → 只发新增后缀；否则整段重发）+ 结束帧 `{"text":"[DONE]","error_code":"0"}`。错误有两条路：HTTP 非 2xx，或流内 `error_code`（形如 `ChatAgent.*` / `TM.00001041`，HTTP 仍 200）。业务码是**字符串**，中立 `Event.error_code` 只有 int 槽位，故原始码进 `error_message`、分类进 `error_kind`。**每日签到**：三个端点都挂 **snap 引擎根上、不带 `snap-manager` 前缀**（`/v1/statistics/plugin`、`/v1/current/user` 反而必须带——两套前缀混用容易扫不到）：

| 用途 | 端点 | 备注 |
|---|---|---|
| 查活动 | `GET {snap}/v1/ops/delivery?channel=IDE` | 回 `data.items[]` |
| 领取 | `POST {snap}/v1/ops/claim` | body `{campaignId:1, idempotentKey:"claim_1_<ms>", channel:"IDE"}` |
| 确认 | `POST {snap}/v1/ops/confirm` | body `{campaignId:1}` |

`idempotentKey` 格式由前端 JS 定死（`claim_<campaignId>_<Date.now()>`）；请求头 `agent-type: PromptCenter`。**四种 status 必须区分**：`ELIGIBLE`（可领）→ claim + confirm；**`CLAIMED`（已领未确认，非终态）→ 只补 confirm**；`CONFIRMED` / `CONSUMED` → 已签，不碰写接口。**claim 时积分即到账**，confirm 只推进状态，故 confirm 失败不丢积分、但 `checkin()` 仍返回 `ok=False` 让任务下一轮补。活动不存在 / 已过期一律 `ok=True`（不是错误）；`ELIGIBLE` 但 `claimable=false` 或未知 status 归 `ok=False` 重试。业务码走 HTTP 200（如 `40001 尚未到达权益刷新时间`），不能只看状态码。`seal_until` 留空（上游只给活动总 `endTime`、无每日刷新窗口）。`checkin_scope` 用 `uid` 隔离，身份未知回落空串。**签到之外的活动操作未接入**。**积分与 token 池是两套并行的账**：`GET {snap}/snap-manager/v1/statistics/plugin` 回 `metrics[]`（`usageTotalPackageCredit` 等各有 `package_credit_amount/used/remain`）。福利模型（`deepseek-v4.1-flash` 等）消耗 **0 积分**（走每日 token 池），内置模型（`openpangu-2.0-flash` 等）走积分。官方只给「模型积分消耗系数」（GLM-5.2 0.7 / OpenPangu-2.0-Flash 0.32），**没给公式**，无法从 `input_price`/`ratio` 归约 → **不猜**，内置模型逐请求 `credit` 保持 `None`，真实消耗以余额接口为准。**积分余额接口未接入**。**额度是每日 token 池**：`GET {opengw}/api/v1/user/tokens/balance` 返回 `daily_token_limit`（1000 万）/ `daily_tokens_used`，当日 0 点清零、不累计。「保活」**只由 `RefreshTask` 承担**（先落库再同步）：`refresh_token` 一次性，额度探测等旁路若也顺手刷新，同一 token 会被两处各消费一次、且旁路结果不落库 → 库里 token 被烧成废票（实测由此把渠道打成 `APIG.0602`）。`probe_quota` 因此为**只读余额**，不做保活刷新。福利模型发现（`{opengw}/api/v1/gateway/config`）与 Token 领取（`POST /api/v1/benefit/claim`，幂等）在探测时顺带完成。

**单位折算（token → 积分）**：每日池满额 1000 万 token ≡ 1000 积分，1 积分 = 10000 token，定义只在 `units.py`（`TOKENS_PER_CREDIT` / `tokens_to_credits`）。折算在两处：`parse_balance` 折 `remaining`/`total`/`expiry_ladder`，`_fill_estimated_credit` 折单请求 `credit`。**该「积分」是为跨渠道排序合成的折算值，与上游签到/套餐积分同名但不同物**。**优先消耗（用完即弃）**：`parse_balance` 把它登记成与 CodeBuddy/TRAE 同构的 `expiry_ladder`（到期点＝次日本地 0 点，金额＝当日剩余），故调度器「窗口内即将到期额度多者先用」的一级指标恒把 CodeArts 排在其它渠道之前。**单请求扣池**：福利模型虽不给倍率但**确实消耗每日池**（实测按每日 token 池 **1:1** 扣减），故 `_fill_estimated_credit` 在流式事件上把 `credit` 补成「输入 + 输出 token 折成的积分」并标 `credit_estimated`。**只补福利模型**：内置模型不扣这条每日池（走 `credit[]` 的付费倍率）。上游将来直接回传 `credit` 时不覆盖。**倍率（`credit_rate`）**：内置模型 `GET {snap}/v1/model/builtin` 的每条目带 `credit[]`，取首档 `ratio_display`（如 `"0.7x"`）；**福利模型不给倍率**（`credit_rate=None`，它走每日免费池，标 `0.0` 会被前端渲染成 zen/kilo 式「免费」）。**登录**：OAuth2 PKCE → `POST {snap-manager}/v1/oauth2/tokens`（authorization_code）换 `{access_key_id, secret_access_key, security_token, expiration, refresh_token}`，DPoP 私钥随 credential 一起生成并加密入库。门户把授权码 302 回 `http://127.0.0.1:{port}/oauth/callback`——用户本机地址，服务端监听不到；故本渠道不用 poll 轨道，而走「paste 轨道」：前端展示授权页后让用户把浏览器地址栏里那条打不开的回调链接粘回，走 `POST /api/auth/upstream/complete` 由服务端用 code + 登录时登记的 PKCE `code_verifier`/DPoP 私钥换 token。登录后补身份：`CodeArtsOAuth._finish` 用签名身份接口（`caller_identity`，按区域不可用时回落 `current_user`）尽力补 `uid`/`user_name`/`nickname`（只补缺失项）；失败不阻断登录。

**一次性票终态（`UpstreamReloginRequired`）**：`refresh_token` 被消费 / 绑定项不符后，每次重试都是同一张废票。CodeArts 令牌端点在 `client.refresh_token` 捕获 `TokenEndpointError` 后经 `events.relogin_required(body)` 判定——只认 `_RELOGIN_MARKERS`（`STS5.1806 the refresh token has been used` / `invalid client id` / `InvalidDPoPHeader` / `invalid_grant`）这类**上游点名**终态；网络 / 5xx / 限流 / 绑定项齐全的普通 4xx 仍走可重试路径（误判终态会让一次偶发故障永久废掉一条好凭证）。`RefreshTask` 捕获后调 `mark_relogin_required`：写可操作的 `disabled_reason`（「刷新令牌已失效，需重新登录」）、硬禁用、清模型级冷却；下轮 `candidates()` 计入 skipped 不再重试。**并发上限与滑动窗口**：上游对**每账号并发会话数**有硬限（实测 3），超出直接 `HTTP 400` + `TM.00001041 并发会话数已达上限(3个)`。`Pacer` 按桶在途上限 `max_concurrency`（满则挂起等 `release` 让位；0/None 保持旧行为），CodeArts pacer 装配 `lambda: runtime.codearts_max_concurrency`（热更项，默认 3）。但上游限制的实为「**每账号每约 60s 最多 3 个会话**」（会话在 HTTP 流结束后仍滞留数十秒，实测约 68s 才恢复），纯在途上限挡不住突发，故 `Pacer` 再加**滑动窗口**口径：`window_seconds > 0` 时与 `max_concurrency` 组合成「同桶最近 `window_seconds` 秒内最多放行 N 次启动」，窗口内满额则挂起到最早一次启动滑出窗口。CodeArts pacer 装配 `window_seconds=lambda: runtime.codearts_request_window_seconds`（默认 60s；`0` 关闭窗口口径退回纯在途上限）。窗口准入**同时**受窗口与在途两个闸门约束；窗口模式配 `max_concurrency=0` 时不生效。**名额归还必须显式关闭上游流**：`async for ... break` **不关闭** async generator（CPython 只在耗尽 / 显式 `aclose()` / GC 的 asyncgen finalizer 时才跑 `finally`）。executor 遇 `EventKind.ERROR` 的 `break` 于是让节流名额推迟归还；轮换重试每次重新 `wait_turn`，`_inflight` 单调累积，满 `max_concurrency` 后新请求在 `wait_turn` 无限阻塞——表现为「用了三次就限制」而非「并发三」。故 `provider.base.aclose_stream` 统一关闭，`stream_guarded` / `stream` / `_stream_loop` / `complete` / `ContinuationStream.__aiter__` 五处提前结束消费处全部显式关闭上游流。**节点白名单**：`CODEARTS_ALLOWED_ENDPOINTS` 含 snap 引擎、STS、福利网关、门户四个主机；AK/SK 签名请求只发往白名单。`CODEARTS_CHAT_MIN_INTERVAL`（热更项，默认 5s）独立 pacer。
### 5.5 CodeBuddy 国际版（`codebuddy_intl`）
**独立渠道而非「改 `CODEBUDDY_API_ENDPOINT` 切站点」**：endpoint 在本项目是**进程级单值**（`Settings` 单字段，装配时注入一个 client）。切过去会让所有存量国内站凭证立刻打到国际站——两站是各自注册的账号体系，额度与模型清单都不通用，表现为额度错配或 403。国际站作为并列渠道存在，两站凭证可同时在池里，各走各的端点、pacer 与登录流程。**协议零复制**：两站是**同一套后端**——同一 Keycloak realm（`copilot`）、同一组路径（`/v2/chat/completions`、`/v3/config`、`/v2/plugin/auth/*`、`/v2/billing/*`、`/billing/meter/*`）。故 `CodeBuddyIntlProvider` 是 `CodeBuddyProvider` 的**数据类子类**（`src/provider/codebuddy/intl.py`），只覆盖渠道 id 与三项能力；协议解析、凭证规范化、事件分类、签到、刷新全部共用国内站代码。**路由存在性的判据**：国际站网关对已注册路径回 `401`（openresty/APISIX 认证插件），对未注册路径回应用层 JSON `{"error_msg":"404 Route Not Found"}`，两者可区分。**但前缀级挂认证时 401 不携带信息**（`/v2/activity/*` 整个前缀挂认证，不存在的子路径也返回 401），此时必须另找同前缀下的对照路径。**唯一的行为差异：首条 system 强制注入**。国际站网关要求 `messages[0]` 是 system prompt，否则整单 `400` + `{"code":11128,"msg":"first message is not system prompt"}`。国内站无此要求。处置是 client 构造参数 `require_leading_system`，**只在国际站装配时开**：`ensure_leading_system` 仅在首条非 system 时补一条 `DEFAULT_LEADING_SYSTEM = "You are a helpful assistant."`。三条约束：① 判首条必须在 `developer → system` 归一**之后**（PI 等客户端把 system 转成 developer，归一已满足要求）；② 客户端自带 system 时不注入、不覆盖、不重排；③ 正文取中性最小句且不带任何厂商指纹，否则撞 11128 内容风控。注入只改 deepcopy 出的出站副本，不穿透回 `request.raw`（会话粘性按原文算指纹）。**能力边界**：
- **签到在渠道侧就不存在**：路径存在（`/billing/meter/checkin-activity-status` 回 `code=0` 而非 404），但活动本身没有（`active: false`，`season`/`theme_name`/`start_time`/`end_time`/`activity_name` 全空；CLAIM 回 `{"code":10001,"msg":"签到活动未开启或已过期"}`）。全空是关键判据——这是**渠道级缺席**。处置：`checkin` / `checkin_scope` / `checkin_status` 三项置 `None`（三个探测点都要关）。**为什么必须关**：保留只会让后台任务每 10 分钟失败一次，连续失败触发 `task_failed` 噪音告警，把真实告警淹掉。
- **成长中心未验证**：整个 `/v2/activity/*` 前缀挂在认证插件后（不存在的子路径也返回 401 而非 404），401 无法证明子路由存在；而成长中心含抽奖 / 连登兑换 / 盲盒等**不可逆**动作，在未验证的端点上盲跑会真实消耗账号权益。处置：`growth` 置 `None`——调用方一律用 `getattr(provider, "growth", None)` 探测，置 `None` 即自动跳过，无需在任务层加渠道分支；管理台的菜单项本就按 `credential.provider === "codebuddy"` 判定，对本渠道天然不出现。
- **活跃上报启用**：上报打的端点是 `/v2/report`，与成长中心的 `/v2/activity/*` **不是同一路径**——国际站 `/v2/report` 带凭证回 `200 {"code":0}`，同批探针里 `/v2/report-nope` 回应用层 `404 Route Not Found`（两者可区分）。`activity_scope` 含 endpoint（`https://www.codebuddy.ai|<sub>`），故两站不会互相占位。

**模型合并：修渠道内重名导致的伪渠道后缀**：上游把多个模型共用一个展示名是常态（CB 的 `hy4-preview` / `hy4-preview-x` 都叫「Hy4 preview」），`_merge_key` 一遇到渠道内展示名重名就退回**渠道限定键**，代价是这些模型**在本渠道内**就断了跨渠道合并。改判据：重名时若本渠道**存在某个 id 归一后等于该 label key 的模型**，就用这个归一键当合并键（重名者 id 不同故自然分开，且不算「按名入键」故不登记歧义别名）；没有 id 自洽候选时（TRAE 的 `sagitta` / `aquila` 组）仍退回限定键。注意 `-x` / `-sg` 是**真实存在的不同上游模型**（id 不同、`max_input_tokens` 有差异），不能被合并掉。**白名单互不含**：`CODEBUDDY_INTL_ALLOWED_ENDPOINTS` 默认只有 `https://www.codebuddy.ai`，**不含**国内站主机。两站 Token 由不同网关签发，跨站发送即凭证泄露，故把国内站地址填进 `CODEBUDDY_INTL_API_ENDPOINT` 会在**启动时**抛 `ValueError`。**pacer 与 OAuth 各自独立**：`CODEBUDDY_INTL_CHAT_MIN_INTERVAL`（热更项，默认 5s）装一个独立 `Pacer`——共用会让两站请求排进同一条队列互相空等最小间隔。`_upstream_auth` 对两个渠道各建一套 `CodeBuddyOAuth`（登录域名不同）。`_pacer_key` 带渠道 id 构造，即便将来共用 Pacer 也不会互相占位。

## 6. 调度器
### 6.1 选号规格
```python
class Scheduler:
    MAX_ROTATE = 3
    EXPIRY_WINDOW = 36h          # 主到期排序窗口，QUOTA_EXPIRY_WINDOW_SECONDS 覆盖；≤0 关闭整套
    SECONDARY_EXPIRY_WINDOW = 7d # 次到期排序窗口（主窗口打平时才比较），QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS 覆盖
    COOLDOWN = {ErrKind.PLAN: 12h, ErrKind.SOFT: 60s, ErrKind.OTHER: 10m}
    ERR_THRESHOLD = 3                    # 连续 OTHER 错误 → 冷却

    def select(self, candidates, tried: set[str], now: int) -> str | None: ...
    def note_success(self, cred_id: str) -> None: ...
    def note_error(self, cred_id, kind, now, *, model=None) -> ErrorOutcome: ...
```

排序键（`_free_first_rank` 起，字典序）：

1. **免费优先档**：候选中存在 `credit_rate == 0` 的渠道时，免费渠道整体排到所有付费渠道之前（`None` = 未知不算免费）。仅当本次候选带有倍率数据（executor 从模型目录现查注入）才生效；候选池无免费渠道时该档恒 0。
2. **到期额度排序（两级字典序）**：`expiry_ladder` 中「距到期 ≤ 主窗口」（`QUOTA_EXPIRY_WINDOW_SECONDS`，默认 36h）的额度加总，多的先用；打平再比次窗口（默认 7 天）；渠道无到期信息（如 CB 企业版）计 0；主窗口 ≤0 时次窗口一并失效（`expiry_windows()` 统一折算）。CodeBuddy、TRAE（按包独立到期）与 CodeArts（每日池）均落阶梯参与此排序。
3. **健康度三态**：`known(0-100) DESC > unknown > exhausted(-1)`。
4. **账户剩余积分**（`quota_remaining`）多者优先——只作健康度的**打平键**，不会越级压过低健康度的高余额号。
5. 仍同分按 `credential_id` 稳定。

**手动 pin 优先**：存在 `pinned` 凭证时只在 pinned 之间排序（管理员显式指定优先于其余排序）。到期积分只算一处：`expiring_credits()`。选号走 `Candidate.expiry_credits()`（主/次两级各调一次），管理台列表走 `GET /api/credentials` 的 `quota_expiring_credits` / `quota_expiring_credits_secondary`（两个窗口值随响应返回），两处共用同一实现与同一个 `expiry_windows()` 折算，界面数字与选号顺序不会漂移。渠道无到期信息时返回 `null`（不显示）；窗口关闭或确实无积分临近过期时返回 `0`（同样不显示）。

> **TRAE 也落 `quota_expiry_ladder`**：权益包各自独立到期（每月登录积分按月、签到奖励各有到期日），与 CodeBuddy 同构，两列同时填、口径统一为「未过期 + 有余额」。`quota_cycle_end`（单值「最早到期」）TRAE 仍为 `NULL`：各包未必共享一个重置点，阶梯已表达「哪些包何时到期」。
### 6.2 候选收窄
`Executor._narrow_providers`：**候选只信模型目录缓存列表**。目录能证明某模型归属哪些渠道时，候选渠道就只有它们——登记了该模型的渠道全部不可用（都在冷却 / 都被暂停）时直接返回 503，不再把请求逐个渠道试过去（各上游只会回 11102 / 4001 / 401，还会给渠道写下数小时的「该后端无此模型」负缓存）。目录未就绪 / 模型完全不在目录里时仍按全部候选渠道路由（保守）；`模型@渠道` 强制指定与 Key 渠道绑定不受影响。新鲜度上限是模型列表 TTL 300s + 后台兜底刷新——新渠道最坏 300s 后进候选，不存在「漏试真正持有它的新渠道」。
### 6.3 模型级冷却
`credential_model_cooldowns(credential_id, model, cooling_until, hits, reason)` 按 **(凭证, 模型)** 独立建表——账号级 `cooling_until` 放不下「同账号其他模型仍可用」这层语义。

- 登记的名字是**该凭证所属上游的原始模型名**（`_upstream_model(provider_id, model)`，未知则原样），因为 CB 与 TRAE 的注册名大小写变体不同，用归一名会漏判。
- 选号路径：`executor._select` 逐凭证过滤 `is_selectable(now, 该凭证的模型名)` 并把模型目录里当前模型的倍率注入候选快照（`Candidate.credit_rate`），再交给 `Scheduler.select`；同时完成模型收窄、粘性命中与排序（`_sticky(...) or scheduler.select(...)`）。
- 退避：`MODEL` 基数 10m 起翻倍、封顶 2h；`BLOCKED` 6h 起翻倍、封顶 24h；`CONCURRENCY` 固定 60s 短冷却、不翻倍（打满是瞬态信号）。换 reason 重新计数（基数与封顶不同，沿用对方 hits 会得到错误的第三种时长）。
- 清除：`save_success(..., model=)` 只删 `reason='blocked'`（模型限流按上游重置，成功一次不代表限制解除）；`revive` / 删除凭证 / 账号级冷却出现都清模型条目。
- 回流：留存任务每轮 `purge_expired_model_cooldowns()` 回收过期行；管理台列表只下发未过期条目。
- 无模型名可归因时（流内事件未带 model）退化为账号级 SOFT，不写孤儿记录。
- **进入模型级冷却的来源不止「模型限流」**：Qoder 把自己节点故障包成 400（见 §5.3）也归 `MODEL`，同样只锁该模型。耗尽轮换时若**所有候选都只因该模型的模型级冷却被排除**（`_all_model_cooled`），503 文案用 `model 'x' temporarily unavailable on upstream` 而非 `all credentials unavailable`——避免把「换个模型就好」误导成「整池凭证挂了」。**统计同口径分流**（`_exhaust_error_type`，与文案同一判据）：该情形落 `error_type=model_unavailable`（界面「该模型暂时不可用」），其余耗尽仍 `no_healthy_credential`（「无可用凭证」）；**对外错误码不变**（仍 `no_healthy_credential`，Anthropic/Responses 出口的错误码映射依赖它）。
### 6.4 跨渠道 fallback 兼容组
**配置**（热更项 `MODEL_FALLBACK_GROUPS`，默认 `""` 关闭）：`组名=成员1,成员2;组名2=成员3`。

- **纯函数层**（`model_resolver.py`）：`parse_fallback_groups` 宽容解析（空段 / 缺 `=` / 空组名 / 空成员一律跳过，一行坏配置不会让网关起不来）；`ordered_fallback_chain(model, groups)` 只做**映射**不校验模型是否存在：组名是**入口别名、不进链**，请求成员时该成员置首、组内其余成员按配置顺序跟随（大小写不敏感）。
- **执行层**（`executor.py`）：`_fallback_chain(target)` 把组解析成 `ModelTarget` 元组。
  - `@渠道` 与 API Key 渠道绑定（`target.forced`）**不参与回退**。
  - 目录可用时**剔除不在任何候选渠道登记的回退成员**（「兼容组白名单」）；目录未就绪则全部放行。成员按名去重。
  - 链首就是用户请求的那个模型时无论目录是否认识都保留。
- **非流式** `complete` 逐链项走完整的选号 / 冷却 / 轮换 / 会话粘性 / 统计，捕获 `NoHealthyCredential` / `InvalidRequest` 继续下一项，整条链耗尽才以最后一项的错误抛出。
- **流式** `stream` 逐链项跑 `_stream_loop`，**只有在本链项尚未产出任何响应帧之前**才允许切换——已出帧后换模型会让客户端看到两个模型的混合输出。用私有信号 `_ModelExhausted` 在链内传递「本项未出帧即用尽」；整条链都用尽时用**最后链项**的 `translator` 产出终帧。
- `preflight` 按**整条链**判断是否有注册上游。

## 7. 请求时序（chat completions 主链路）
> 可视化版本：[`diagrams/coding2api-request-sequence.html`](diagrams/coding2api-request-sequence.html)。`POST /v1/responses` 与主链路同：先把请求体映射成等价的 `ChatRequest`，其后选号 / 轮换 / 记账 / 粘性完全一致，出口形状由注入的 translator 决定。

```
客户端 → POST /v1/chat/completions (Bearer sk-)
  1. deps.require_api_key：摘要查 api_keys 表 → username（+ IP/到期/模型白名单校验）
  2. request.py：校验 body → ChatRequest；model_resolver 解析候选集
     - "glm-5.2" → 各 provider 都可能；"glm-5.2@trae" → 仅 trae；auto/空 → DEFAULT_MODEL
  3. 选号（executor._select → scheduler.select）：
     a. 候选 = 模型目录能证明归属时收窄到登记了该模型的渠道（_narrow_providers）；
        收窄后若没有任何可用凭证 → 直接判「无可用凭证」出 503（候选只信模型目录）
     b. 模型级冷却过滤：逐凭证按自己所属上游的原始模型名查 (凭证, 模型) 冷却表
     c. 会话粘性：存在可选的 pinned 凭证时跳过（pin 优先），否则粘性命中且仍可选的
        凭证直接复用、不参与排序
     d. 过滤 healthy（enabled=1, disabled=0, 非冷却中）。enabled=0 只作用于本条对话
        路径：后台任务只检查 disabled
     e. 免费优先档 → 到期额度两级排序 → 健康度三态 → 账户剩余积分 → credential_id
  4. executor：解密凭证 → 按实际渠道压缩上下文 → provider.stream_chat()（≤ AUTO_CONTINUE_MAX 续写）
     - 上游 HTTP ≥400 → classify → scheduler.note_error → tried 加入 → 回到 3（最多 3 次）
     - 流内 Event.ERROR → 同上映射 → 注入出口错误帧 + 冷却 + 轮换
     - 上游 400（INVALID，如模型不存在）不冷却凭证，跳过该上游全部凭证；
       全拒时 400 invalid_request（未知模型名 ≡ 无上游提供，不走 503）
     - REQUEST 类换号但不落库：既不冷却也不累计，否则会顺手把已有的 cooling_until 写成 NULL
  5. response.py：Event → OpenAI chunk（流式）或聚合（非流式）
     - 首块补 role:assistant；上游无 index 的 tool_calls 补稳定 index
     - 收尾帧序列：finish chunk → usage 帧（choices: []）→ [DONE]；上游未报 usage
       则不补该帧（不发 0 占位）。非流式/流式收尾共用 usage_payload()
     - 直连 CodeArts v2 每 chunk 都带 usage，取收尾帧真值（取第一个会记成 0/空）
  6. stats.collector：写 usage_events（username/provider/model/tokens/latency/ttfb/ok）
  7. scheduler.note_success：清 err_count，并把本对话重新粘到实际服务的凭证
```

客户端断连：生成器被关闭/取消时统计 `error_type=client_disconnect`，关闭上游流；例外：`[DONE]` 已产出后的收尾断开（客户端拿到回调即关连接，框架在结束帧 `more_body=False` 前有一拍竞态会把它判为断开）按成功记账。

## 8. 数据库
DDL 以 `src/db/schema.sql` 为准。当前 `SCHEMA_VERSION = 18`，共 11 张表：`api_keys` / `credentials` / `users` / `audit_events` / `usage_events` / `usage_hourly` / `growth_events` / `credit_events` / `credential_model_cooldowns` / `runtime_settings` / `alert_events`。

```sql
-- conn.py 打开时执行
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;      -- 仅 api_keys 之外无外键（users 与 api_keys 仍在应用层校验）
```

- 连接：`threading.local()` 每线程一个 `sqlite3.Connection(row_factory=sqlite3.Row)`，引擎与 FastAPI 线程池各持自己的连接。写入统一走 `Database.transaction()`（正常提交、异常回滚），无应用层写锁，并发由 SQLite 串行化（WAL + `busy_timeout=5000` 下短写足够）。
- 加密：`Fernet(base64.urlsafe_b64encode(sha256(APP_SECRET).digest()))`；APP_SECRET 丢失 = 凭证全部不可解，只能重录。启动时按熵拒掉弱密钥（长度 ≥16、不同字符数 ≥8，另拒重复字符 / 短周期重复串）。
- 运行时配置覆盖层 `runtime_settings(key, value, updated_at)`：纯 key/value，只存「管理台改过」的 key，未出现的 key 回落 env。键集合随版本演进（新增可热更项无需迁移）；值的类型与范围由 `HOT_SETTINGS` 白名单校验，白名单外/类型非法的行在读取时被忽略并记日志。
- 模型列表与模型目录不占表：模型列表是「进程内 TTL 缓存 + `DATA_DIR/model_catalog.json` 落盘快照」，OpenRouter 目录是 `DATA_DIR/openrouter_catalog.json`（见 §9.4）；签到去重由 `CheckinTask` 的当日作用域集合实现。

**迁移纪律**（`migrate.py`）：
- 启动时读 `schema.sql` 逐条 `CREATE TABLE IF NOT EXISTS`（只加不改）。
- 新增**列**写进 `migrate._MIGRATION_COLUMNS`，走 `ALTER TABLE ... ADD COLUMN`（列已存在时忽略，老库幂等补列）；同时 `SCHEMA_VERSION + 1`。
- 新增**表**不需要 `_MIGRATION_COLUMNS`：`CREATE TABLE IF NOT EXISTS` 对老库同样执行，建表即完成迁移，只需 `SCHEMA_VERSION + 1` 并补一条老库升级测试。
- 删除**表**写进 `migrate._MIGRATION_DROPS`，走 `DROP TABLE IF EXISTS`（schema.sql 里删定义不会作用于老库，遗留表必须显式删）；同时 `SCHEMA_VERSION + 1`。
- 版本记在 `PRAGMA user_version`；库版本高于本程序支持时启动报错。
- 删除凭证时显式清理其模型级冷却行（无外键级联），否则重建同 id 凭证会继承旧的模型冷却。
- **升级只在启动时发生**：迁移与 bootstrap 都挂在 `build_app` 里，所以「改了 schema 但没重启」＝老库 + 新前端。

## 9. 模型目录与模型名
### 9.1 三字段与归一键
七条渠道（codebuddy / codebuddy_intl / trae / zen / kilo / qoder / codearts）的每个模型统一成三个字段，规则集中在渠道中立的 `src/provider/naming.py`（纯函数，不 import 任何 provider 子模块）：

| 字段 | 函数 | 例 |
|---|---|---|
| 原代号 `raw_id`（渠道请求 key，**永不改动**） | — | `kmodel_latest`、`kilo-auto/free`、`longcat-2.5-preview-free` |
| 归一键（合并键 / 多渠道对外 id） | `normalize_model_key` | `kilo-auto`、`longcat-2.5-preview`、`nemotron-3-ultra` |
| 展示名 `name` | `display_model_name` | `LongCat 2.5 Preview`、`Qwen3.8 Max` |

`normalize_model_key` 剥掉两类上游噪声再 slug 化（空格/下划线/冒号/括号 → 连字符、小写）。**免费标记**是可用性命名约定、不是模型名的一部分，四种形态：尾缀 `-free`/`_free`（Zen）、路径段 `.../free`（Kilo）、冒号尾缀 `:free`、展示名括号词 `(free)`；同性质还有新版标记 `new`。**命名空间前缀**不是模型身份：路径式 `厂商/模型` 取**最后一段**，冒号式 `厂商: 模型`（要求冒号后有空白）削掉前缀。只删「整段/整词」标记（`freeplay`/`newson` 内嵌形式不动）。点号**不是**分隔（`glm-5.2` 压成 `glm-5-2` 就换了模型）；非 ASCII 字母数字保留。剥完只剩噪声时回退原串小写，调用方据此回退展示原 id，不产出空键。`display_model_name` 清洗上游可读名（`Qwen3.8-Max` → `Qwen3.8 Max`、`NVIDIA: Nemotron 3 Ultra (free)` → `Nemotron 3 Ultra`）或由 id 派生；品牌与缩写按表纠正（机械 title-case 会把 `DeepSeek`/`MiMo`/`GLM`/`GPT` 写错）。**幂等**：清洗结果再洗不变。**合并键 = 归一键，七渠道统一**：同一模型各渠道内部代号互不相同（Qoder `kmodel_latest` = TRAE `kimi-k3-1` = CodeBuddy `kimi-k3`），纯 id 规则无法对齐，只能靠展示名——归一键即展示名再 slug 化，两路径永远同源。zen / kilo 也参与合并：两条免费渠道各用不同命名约定与**不同 id 规格**（Zen `nemotron-3-ultra-free` vs Kilo `nvidia/nemotron-3-ultra-550b-a55b:free`），id 对不上，但展示名都清洗成「Nemotron 3 Ultra」，走展示名能并成一条。**同模型异名显式规范**（`MODEL_SYNONYMS`）：清洗规则对齐不了「上游给同一模型起了不带版本号的名字」（Qoder `dfmodel` 上游名只叫 `DeepSeek-Flash`，别家叫 `DeepSeek-V4.1-Flash`）。表把「异名归一键 → (规范归一键, 规范展示名)」，键与展示名一起收敛。**只登记人工核实的同模型异名，不做模糊匹配**。**哨兵名不跨渠道合并**（`_SENTINEL_LABELS` = `auto` / `default`）：这些是各上游自己的「自动路由 / 默认模型」占位，语义只在本渠道内成立（Kilo 的 `kilo-auto/free` 与 Qoder 的 `auto` 都归一成 `auto` 却是完全不同的东西），故退回原 id 的归一键、各自单列。**同渠道内归一键重复 → 退回原 id 的归一键**；该渠道也不登记歧义的名字别名。

**对外 id 一律是归一键**（`_finalize` / `_disambiguate` / `_register_aliases`）：多渠道取最短展示名的归一键；单渠道取该条展示名的归一键；兜底/哨兵条目取**原代号的归一键**。**原代号不在对外 id 里露面**，但转发时一律经 `services.model_aliases` 映射回该渠道自己登记的原代号（由 `executor.upstream_model_name` 消费）；每渠道登记三条键——对外 id、原代号、展示名（原样与 slug 形式），用户按任一种写法都能直连命中。**对外 id 撞车的消歧**（`_disambiguate`）：按 `_sort_key` 序保留先到者，后者按 `_fallback_ids` 依次取（原代号归一键 → 加渠道名后缀 → 数字后缀）。**兜底/哨兵条目用渠道限定键隔离**（`_merge_key` 的 `_scoped_key` = `渠道\0归一原代号`），但对外 id 仍是原代号的归一键。**展示顺序（CB / TR 优先）**：合并后按 `_sort_key(entry)` 排序——渠道权重 `_PROVIDER_RANK`（codebuddy 0 → codebuddy_intl 1 → trae 2 → qoder 3 → codearts 4 → 其余 5），多渠道条目取 `min` 权重，同级再按**归一键**字典序（`_sort_key` 在 `_finalize` 之前跑，此时对外 id 未算出，而 `_disambiguate` 需要确定顺序决定撞 id 时谁保留）。仅影响展示顺序，调度选号（`model_resolver` 的 `KNOWN_PROVIDERS` 顺序）不变。
### 9.2 按凭证加载与 SWR
`list_models` 先用 `credential_providers()` 求「当前有可用凭证」的渠道集合（`candidates(selectable_only=True)`：未暂停、未硬禁用；冷却中的仍算有凭证，避免限流时列表闪没），循环里 `provider_id not in connected` 直接 `continue`——**没凭证的渠道不读缓存、不拉上游、不展示**。zen / kilo 自带虚拟凭证，不受影响。**失败也进 TTL（负缓存）**：`model_list_fetched_at` 记上次尝试时间（成功或失败都刷新），TTL 内不再打上游。**HTTP 出口 stale-while-revalidate**：TTL 到期后 `GET /v1/models` 与 `GET /api/playground/models` 走 `serve_models`：**有缓存直接回旧列表**，过期渠道丢后台（`_schedule_refresh`）异步刷新。两条纪律：① **某渠道一条缓存都没有时才同步等它**（冷启动无快照 / 新接入渠道），否则列表会缺一条渠道；② 后台刷新收敛在 `services.model_refresh_tasks`，`model_refreshing` 去重，`lifespan` 关闭时逐个 `cancel()` + `await`。代价：列表最多滞后一个 TTL。后台预热 `_warm_model_list` 与兜底循环仍走同步 `list_models`。**逐渠道增量 publish**：每拉完一条渠道就 `publish_aliases()` 一次（从 `model_list_cache` 重建 + 就地更新，executor 闭包引用同一 dict），不再等最慢那条（zen 逐个免费模型真发探活可占 12–15s，期间别名表为空会让 `executor._narrow_providers` 拿不到归属而按「全部渠道」放行）。合并逻辑收敛到 `merged_entries()`，缓存兜底 / TTL 复用 / 落盘恢复三条路径共用。
### 9.3 落盘快照与启动回灌
进程内缓存重启即丢，故每次成功拉取后把**未过滤原始表**原子写进 `DATA_DIR/model_catalog.json`（tmp + `os.replace`，格式 `{"version":1,"providers":{pid:{"saved_at":…,"models":[…]}}}`，序列化字段取 `dataclasses.fields(Model)`）。启动时 `main._restore_model_list` **同步**读回并立即 publish 别名表（`restore_model_catalog`，零上游请求、异常只记日志），预热退化为纯后台刷新；快照 `saved_at` 随表交回、折进 `model_list_fetched_at`，故预热（TTL 门禁）不会重拉刚恢复的表。三条纪律：① 落盘/读回一律宽容（损坏、版本不符、字段缺失、记录非 dict 只丢该渠道，`saved_at` 超 7 天整条丢弃）；② 存原始表不过滤（黑名单热更立即生效）；③ 恢复只覆盖「已注册且当前有可用凭证」的渠道——用户暂停的渠道不因快照复活。**启动预热不再阻塞启动**：`lifespan` 把预热丢给后台任务，不在 `yield` 前 `await`（zen 最慢探活可占十几秒，内联会让应用不响应 `/health`）。另有 `MODEL_CATALOG_MINUTES` 后台任务兜底刷新（保证纯 API 部署也真刷新）。
### 9.4 模型目录（OpenRouter 单一来源：价格 + 明细 + 能力分）
**数据源**：`https://openrouter.ai/api/v1/models`（`OPENROUTER_MODELS_URL`，匿名可读、无需 key）。一次抓取同时得到刊例价（`pricing`）、明细元数据（`architecture` / `top_provider` / `supported_parameters`）与 Artificial Analysis 三项指数（`benchmarks.artificial_analysis`），供成本估算、「模型列表」页与 Playground 能力分三处使用。**架构**（后台抓 → 落盘 → `app.state` → 每请求现读）：
- 后台任务 `openrouter_catalog`，周期由 `OPENROUTER_CATALOG_MINUTES` 控制（默认 1440 分钟、下限 60，可热更）。一轮抓取建三张表：价表（`build_price_table`）、明细目录（`build_model_catalog`）、能力表（`build_benchmark_table`）。拉到空价表视为失败，不覆盖已有三张表。

**能力分口径**：Artificial Analysis 的三项指数（`intelligence_index` 综合智能 / `coding_index` 编程 / `agentic_index` 智能体），经 OpenRouter 公开接口分发。AA 官方 API 需 key，故不采用；上游字段类型意外时安静跳过，绝不让模型列表挂掉。
- **失败安静降级**：网络 / 结构坏 / 空表 / 快照损坏 / 版本不符 / 超龄，一律退化为空表，条目**不带** `benchmarks` 字段（不是 `null`），页面显示 `—`；模型列表与聊天不受影响。
- 落盘 `data/openrouter_catalog.json`（`CATALOG_VERSION=2`，三张表同一个文件、同一个 `saved_at`，超 `MAX_AGE_SECONDS`=7 天视为过期 → 全退空），启动时同步回灌（`load_catalog_snapshot`，零上游请求）；**无快照时**启动后台补拉一次（`_warm_model_catalog`）。
- 挂 `app.state.price_table` / `app.state.models_dev_catalog` / `app.state.model_benchmarks` / `app.state.price_saved_at`。刷新是**就地 `clear()` + `update()`**，不换引用（每请求现读，换引用会让路由读到旧表）。

**匹配纪律**（`src/model_match.py`，价表 / 明细 / 能力排行共用同一套）：本项目对外的模型 id 是归一键（`kimi-k3`），OpenRouter 用 `moonshotai/kimi-k3` + 展示名 `MoonshotAI: Kimi K3`。`match_keys(*texts)` 把任意写法展开成候选键集合（归一键 + `_ALIAS_RULES` 显式等价 + 剥 `_DROP_SUFFIXES` 尾缀 + 版本号点/连字符互换），`lookup` **只做等值匹配**、命中不唯一返回 `None`（`glm-5.3` 绝不匹配 `glm-5.3-flash`）。规则全部显式登记，每条附实测来源，不做启发式推断。**展示范围**（三处，同一份表同一口径）：`GET /v1/models` 每行可选 `benchmarks` 字段；`GET /api/playground/models` 同上；`GET /api/model-catalog` 每行可选 `benchmarks` + `available` + 顶层 `saved_at`。**`available`** 是「当前渠道真能提供」的布尔（口径与 `/v1/models` 一致：只算有可用凭证、过当前黑名单的渠道），用同一套候选键与渠道侧条目求交集——「模型列表」页据此提供默认关闭的「只看当前渠道」过滤。**排序**：`sort=intelligence` 按综合智能指数排（无分数的行排末尾，不冒充 0 分），只给这一个分项入口（一列对应一个入口，三项各排会挤爆表头）。**UI**：Playground 模型选择器行内徽章 + 选中模型的能力分卡；「模型列表」页「能力分」列（三行：智 / 编 / 体，列头可点排序）+ 「能力分来源」指标卡。**不做排序**——分数是第三方成绩，排序等于替用户下结论。每处都标注「Artificial Analysis 指数，经 OpenRouter 公开接口；非本服务实测」。**已知取舍**：`family` / `open_weights` / `release_date` 上游不提供（后端固定给 `null` / `false`，页面只展示「知识截止」）。**降级**：拉取失败 / 快照损坏 / 版本不符 / 超龄，一律退化为空表，条目**不带** `benchmarks` 字段（不是 `null`），页面显示 `—`。
### 9.5 token 到期展示与预警
**到期时间的来源**（决定实现，不能只读凭证字段）：TRAE 凭证 JSON 的 `expires_at` 有值（= JWT `exp`）；**CodeBuddy 恒为 0**（OAuth 登录与刷新响应都不带 `expires_at`），只能从 bearer token 的 JWT `exp` 解析；CodeArts 用 `expiration`（不是 `expires_at`），签发时间借 `refresh_token` JWT 的 `iat`。实现集中在 `provider/token_expiry.py`（渠道中立，刻意不 import provider 子模块，否则 `provider.codebuddy.events → engine.sse` 会被拖进 `db` 层）：

- `credential_token_times(data)`：返回 `(签发, 到期)` 两个 epoch。到期优先显式 `expires_at`/`expiresAt`/`expiration`，缺失 / 非法时遍历可能的 token 键解析 JWT `exp`；签发时间只来自 JWT `iat`。各自拿不到时返回 **0 = 未知**。**不猜本地 TTL**——捏造的到期时间会让管理台显示假预警。**只借 `iat` 的兜底**：CodeArts 的 access token 是 STS AK/SK（非 JWT），但签发它的 STS 响应同时轮转出一张 `refresh_token` JWT，其 `iat` 就是这组临时凭证的签发时刻，故 access token 键都取不到信息时再遍历 `_ISSUED_ONLY_KEYS` **只借 `iat`、不借 `exp`**（后者的 30 天寿命会把 2h 的临时凭证画成 30 天）。
- `jwt_times(token)` / `jwt_expiry(token)`：只 base64url 解码不验签；非 JWT / 结构异常 / claim 非法一律 0。`normalize_epoch()`：毫秒时间戳归一。

**落库**：`credentials` 两列——`token_expires_at`（`add()` 与 `save_credential_data()` 都写回；JWT 派生值**不写回凭证 JSON**，否则刷新换到新 token 后旧派生值会残留成「权威」到期时间）与 `token_issued_at`（仅落库供诊断）。**老库升级不批量回填**：改在列表读到时按需从 `data_enc` 派生（`_token_times_from_blob()`，解密 / 解析失败返回 `(0, 0)`）；一旦写回就只读列值。**读路径**：`list_all()` 只下发绝对 epoch，**不代前端算剩余秒数**（服务端算好的「剩余」不随页面 tick 更新）。剩余时间与预警判定由前端 `tokenExpiryView()` 用同一时钟现算，阈值由 `TOKEN_EXPIRY_WARNING_SECONDS` 经列表接口下发。**短寿命凭证不标「即将到期」**：整段寿命（`expires − issued`）≤ 预警窗口的 2 倍时，该 token 天生就在窗口附近（CodeArts STS 寿命 2h、窗口 1h），恒标红只会淹掉真告警；**真过期（剩余 ≤0）仍标红**。到期未知（0）时显示 `—`，**绝不当成已过期**。接口：`GET /api/credentials` 响应新增 `token_expiry_warning_seconds`，每条凭证新增 `token_expires_at` 与 `token_issued_at`（均为 0 表示未知）。
### 9.6 积分变动流水
**做法：在额度探测写回时比对余额，只增记一条**（`credit_events`）。写入放在 `save_quota` 的**同一个事务**里：分两次读改写会与并发探测交错，记出「before 是别人写过的值」的错行。**归因纪律**：diff 只能看到 `[上次探测, 本次探测]` 区间的**净变化**，其间签到、成长领取与对话消耗可能同时发生，无法区分谁贡献了多少。所以 `source` 列**只表达归因已知度**：`observed` = 两次探测之间的净变化（delta 可正可负或 NULL）；`sync` = 首次建立基线、没有对照（delta 为 NULL）。前端文案一律说「净变化」，不写「签到 +5」。**只记有信息量的行**：首次探测记 `sync`（delta NULL）；余额未变**不记**；余额变化记 `observed`；任一端未知时记（delta NULL，「余额变未知」本身是该追的异常）；两端都未知不记；凭证被并发删除不记。`window_start` 记的是**上次探测时刻**（不是「上次有变动」）。接口：`GET /api/credentials/{id}/credit-events?limit=20`（管理员；未知凭证 400 而不是空列表）。保留期由 `RetentionTask` 按 `usage_events` 同一保留期（90 天）回收，报告里体现为 `purged_credit_events`。
### 9.7 用量统计与成本
- **口径**：`overview` / `by_provider` / `by_model` / `by_user` / `timeline` / `model-timeline` 均读**小时汇总**（`usage_hourly`，永久保留），只有 `events`（逐请求明细）读 `usage_events`（90 天）。统一口径让选「全部」时总览与图表同值。代价：最近 ≤5 分钟未进汇总的请求不计入。**凭证维度读明细表**（小时汇总主键里没有 `credential_id`），故只覆盖最近 90 天。
- **小时汇总双写**：`record()` 写明细的同时增量累加当前小时行，新请求立即可见于统计页；`rollup_hourly`（`RetentionTask` 每 5 分钟）全量重算作对账，两者幂等一致。
- **TRAE credit 为推算值**（`credit_estimated`）：TRAE 上游 `token_usage` 只给 token 数、无单请求积分，按官方计费公式（`(输入−缓存)×输入价 + 输出×输出价 + 缓存×缓存价`，单价为积分/百万，见 `src/provider/trae/pricing.py`）折算；展示层加 `≈`。CodeBuddy 的 credit 是上游真值，恒不标推算。与「刊例 × 折扣」不符的模型走 `pricing.MEASURED_EFFECTIVE_OVERRIDE`（缓存命中价需单独实测，如 DeepSeek-V4.1-Flash 实测缓存价高于刊例）。
- **成本（估算）**：按 `token × 公开刊例价` 估算，不是上游真实扣费。单价取自 OpenRouter 公开目录（`pricing.prompt` / `completion` / `input_cache_read`，抓取时乘 1e6 转 USD / 百万 token），按 `(输入−命中)×输入价 + 命中×缓存价 + 输出×输出价` 折算美元，再按**写入时生效的汇率**（`USD_CNY_RATE`）折人民币。**写入时定值**（`usage_events.cost_usd` / `cost_cny`），历史行不随价表 / 汇率重算。**匹配不到就不计**（未匹配或未上报输入 token 的 `cost_known=0` → 回 `None`、显示 `—`，不拿 0 冒充免费），故成本是**下限**。
- **缓存命中率** = 命中 token ÷ 输入 token（保留 1 位小数）；未上报或输入为 0 时显示 `—`。
- **延迟均值只算成功请求**（分子 `SUM(latency_ms WHERE ok=1)` 与分母 `ok_count` 配对）。
- **回填脚本**：`scripts/backfill_trae_credit.py`（TRAE 推算积分）、`scripts/convert_codearts_credit_unit.py`（CodeArts token→积分，**只能跑一次**）、`scripts/backfill_cost.py`（成本，默认预览、写库前自动备份、幂等）、`scripts/rollup_hourly.py`（小时汇总重算）。均先报告缺口，再 `--apply` 写库。
### 9.8 列表排序
所有 `GET` 列表端点接受统一的 `sort`（白名单键）+ `order`（`asc`/`desc`）。**未知 `sort` 键或非法 `order` 值回落到默认排序，不报错**；`sort` 是白名单键而非 SQL 列名，SQL 片段只由受控常量拼出，用户输入永不进 SQL，注入面为零。契约在 `src/sorting.py`（db 与 api 共用，避免 db → api 反向依赖）：`parse_sort_order`（唯一解析入口）、`sql_order`（`tiebreak` 兜底稳定次序）、`sort_rows`（组装后再排，`None` 值无论升降序都排**最后**）。逐请求明细默认（`time` 降序）走 rowid 游标分页返回 `next_before`；按其它列排序 / 时间升序 / 显式 `offset` 时改用 `LIMIT/OFFSET` 并返回 `total`。适用端点：`/api/credentials`、`/api/users`、`/api/api-keys`、`/api/audit`、`/api/alerts`、`/api/model-catalog`、`/api/credentials/{id}/credit-events`、`/api/credentials/{id}/accounts`、`/api/stats/by-*`、`/api/stats/events`。

## 10. 后台任务
`TaskRunner`（`runner.py`）每类任务一个独立 asyncio 循环，失败只记日志不拖垮服务；间隔有安全下限，避免打爆上游。`Pacer` 有两种模式：后台任务用**严格模式**（默认，单锁串行化跨任务/跨凭证的相邻请求）；聊天用**并发模式**（`allow_concurrent=True`），按「渠道 + 凭证身份」分桶并在桶内放行并发——同渠道同模型的并发请求不再被逐级串行化，只有同凭证「上一请求已结束、紧接着又来一个」的顺序连发才补足最小间隔。请求结束由 provider `stream_chat` 的 `finally` 调 `release(key)` 归还名额。

| 任务 | 周期 | 行为 |
|---|---|---|
| 额度探测（quota_probe） | 启动立即一轮（不节流）+ 每 `QUOTA_PROBE_MINUTES`（默认 60） | 探测剩余额度 → 写 `credentials.quota_*` / `quota_expiry_ladder` / `quota_packages` / `health` |
| token 预刷新（refresh） | 每 `REFRESH_INTERVAL_MINUTES`（默认 30，下限 5） | 到期前 `REFRESH_SKEW_HOURS`（默认 24h）窗口内轮换 refresh token；短寿命渠道自行封顶窗口（CodeArts `refresh_skew_cap_seconds=2700`）——封顶值必须**严格宽于**本周期；上游明确回 `UpstreamReloginRequired` 时标记「需重新登录」并硬禁用 |
| 每日签到（checkin） | 每 10 分钟（全天） | 成功即封账该凭证（`日期:scope` → 截止时刻，进程内内存态，跨天清理）；默认封到本地当日结束，渠道可回填 `CheckinResult.seal_until`（Qoder 每日 10:00 换窗口）；失败持续重试 |
| 成长中心（growth） | 每 `GROWTH_INTERVAL_MINUTES`（默认 60，下限 5） | 仅 CodeBuddy：7 类领取；结果落 `growth_events` + 回写 `credentials.growth_last_result` |
| 活跃上报（activity，默认关闭） | 每 10 分钟醒一次，仅 `ACTIVITY_REPORT_HOUR`（默认 10 点）窗口内执行 | CodeBuddy 与 CodeBuddy 国际版：补发一条 `chat_request_send` 续连登；按「endpoint + userId」隔离、当日封账；成功落一行 `growth_events` |
| 明细清理（retention） | 每 5 分钟 | `usage_events` 全量重算小时汇总（幂等 upsert，与 record 的增量双写对账）+ 90 天前明细清理；同期限回收 `credit_events` / `audit_events` / `alert_events` / `growth_events`（否则无界增长）。整轮跑在 `asyncio.to_thread`（全表 GROUP BY 是同步 SQLite，直接在事件循环执行会卡住并发请求） |
| 渠道模型列表刷新（注入协程，`api/models.py::list_models`） | 每 `MODEL_CATALOG_MINUTES`（默认 30，下限 5） | 兜底重拉各渠道模型表（TTL 门禁 + 逐渠道 publish + 落盘快照）。周期跟 zen 判活缓存（30 分钟）对齐——更密不会让 zen 多探一次 |
| 模型目录刷新（OpenRouter，注入协程） | 每 `OPENROUTER_CATALOG_MINUTES`（默认 1440，下限 60） | 拉 OpenRouter → 建三张表 → 落盘 → 就地换入 |
| 运维告警（alert） | 每 `ALERT_INTERVAL_MINUTES`（默认 5，下限 1） | 评估四类风险（池耗尽 / 任务连续失败 / token 临近到期 / 上游错误率骤升），命中落 `alert_events` 并可推送 webhook；同 `(规则, 对象)` 在静默窗内只报一次 |

**渠道模型列表刷新与模型目录刷新是「注入协程」而非 tasks 模块**：`tasks/` 不 import `api/` / `benchmarks`（反向依赖会把 HTTP 层拖进后台任务），所以 `TaskRunner` 接的是 `Callable[[], Awaitable[object]]`，由 `main.lifespan` 闭包注入；`None` 时不装配这条循环，管理台也不展示该卡片。**并发保护**：HTTP 出口与后台循环都会调 `list_models`，故 `api/models.py` 里 `_refresh_providers` 整段持模块级 `asyncio.Lock`（粒度是「整次列表刷新」而非单渠道——跨渠道合并与别名表 publish 需要看到一致的全集）。**签到 / 成长中心的「同账号」隔离键**：`checkin_scope(data) or f"credential|{credential_id}"`（provider 在身份未知时返回空串，共享空 scope 会让第二个账号被 `seen` 集合永久跳过）。**记录口径**：`_guarded` 在任务返回 `None` 时**不入账**（`sleep` 到点但 `due()=False` 是 no-op，若当成一次成功执行，页面会显示「签到刚跑过」而当天其实一次都没签）。`TaskRun` 只在真实执行（返回非 `None`）或抛异常时写入；异常也入账（`last_error`）。计数 `runs` 同样只涨真实执行。运行态只存在于**本次进程**内，重启归零。**任务清单**：`tasks/status.py` 的 `TASK_SPECS` 是静态描述（key / 名称 / 一句话说明），与 `TaskRunner.start()` 建立的循环一一对应；周期与开关**运行时现算**（`TaskRunner.task_status()` 读热更值）。未装配的任务不出现在清单里。接口 `GET /api/tasks`（admin）返回 `{tasks: [...], server_time}`，每条含 `key`/`name`/`description`/`interval_seconds`/`enabled`/`runs`/`last_started_at`/`last_finished_at`/`last_ok`/`last_report`/`last_error`。带 `server_time` 是为了让前端用**服务端时钟**算「距今多久」。

**成长中心的失败判定**（三层分开）：协议层（`growth.py`）非 2xx 抛 `GrowthRejected`；业务码非 0 / 缺 data / 非 JSON 抛 `UpstreamProtocolViolation`（HTTP 200 也可能是失败）。编排层（`growth_runner.py`）4xx 业务规则（名额用完、未解锁、抽奖没次数、能量不足）记 `IDLE` 而非 `FAILED`；5xx 与协议违规记 `FAILED`；401/403 立即置 `session_dead` 并停止后续请求。结论：`failed 且 gained=False` 才算整体失败（部分成功仍是成功）。接单失败分三类：`prerequisite not met: <task_code>` 按原因归并成一条并标 `reportable=True`（用户需知道去做什么），记 IDLE；`task does not require acceptance` 正常应答，不产生任何步骤；其余逐条记 FAILED。`GrowthStep.reportable` 控制该步是否进「一行汇报」。**任务契约是五态**：`not_accepted` → 接单、`accepted`/`in_progress` → 等用户完成、`completed` → 领奖、`claimed` → 跳过。接单 `POST /tasks/accept` body 是**复数数组** `{"task_codes": [...]}`；领奖 `POST /tasks/{task_code}/claim`。**403 不总是登录失效**：未解锁档位返回 403 +「连续登录天数不足」，必须先于 session 判定处理。不可逆动作（抽奖 / 连登兑换 / 开 Buddy 盲盒 / 消耗补登卡）由 `GROWTH_IRREVERSIBLE_ACTIONS` 总开关控制，**手动入口与定时任务读同一个开关**；开关只挡「消耗」，不挡「查询」（`/streak` 是只读接口）。**清理切点必顶对齐到小时边界**（`purge_expired`）：`rollup_hourly` 对整行是 REPLACE 语义，只有保证「仍有明细的小时保有全部明细」重算才精确；若边界小时只删一半，下一轮 rollup 会把汇总行覆盖成剩下那一半，被删部分永久丢失。代价：明细最多多留 1 小时。**运维告警**：规则判定是纯函数（`tasks/alerting.evaluate_alerts`），阈值统一用「≤0 即关闭该规则」表达。

| 规则 | 触发 | 信号来源 |
|---|---|---|
| `pool_empty`（critical） | `total>0` 且 `ready < ALERT_POOL_READY_MIN` | `CredentialRepository.pool_counts`（与 `/healthz` 同口径） |
| `task_failed` | 某任务连续失败 ≥ `ALERT_TASK_FAILURES` | `TaskStatusStore.failing`（成功一轮清零） |
| `token_expiring` | token 剩余落在 `(now, now+ALERT_TOKEN_EXPIRY_HOURS]` | `CredentialRepository.expiring_tokens` |
| `error_rate` | 窗内失败占比 ≥ `ALERT_ERROR_RATE_THRESHOLD` 且样本 ≥ `ALERT_ERROR_RATE_MIN_REQUESTS` | `StatsCollector.window_error_rate` |

- `pool_empty` 取 `total>0`：池里一个凭证都没有是「没配」而非「耗尽」，不报。
- `task_failed` 只列**真跑过且达阈值**的 key：从未运行的任务不在 `_failure_streaks` 里，不会被误报。
- `token_expiring`：`token_expires_at` 为 NULL 时从密文按需派生；派生不出（0）视为未知、不算「即将到期」；硬禁用凭证排除；**寿命本就短于窗口**的凭证排除（CodeArts STS 只有 2h，而默认窗口 24h，恒「即将到期」只会把真告警淹掉）。
- `error_rate` 读 `usage_events` **明细**而非小时汇总（告警窗是分钟级，小时粒度要么整点才更新、要么跨小时口径错乱）。

**投递**：站内每条命中落 `alert_events`（管理台倒序回看；落库而非只留内存，因为告警的价值恰在「错过的那段时间发生了什么」，保留期由 `RetentionTask` 按 90 天清理）；webhook 配置 `ALERT_WEBHOOK_URL` 时逐地址 POST JSON（多个逗号分隔，全部成功才算 delivered），留空只留站内记录，**投递失败只记 `delivery_error`、绝不抛错**。**静默去重**：同一 `(rule, scope)` 在 `ALERT_SILENCE_MINUTES` 窗内只落库 / 推送一次。`AlertTask` **与运行态共享同一个 `TaskStatusStore`**——若自建 store，「任务连续失败」规则会永远读到 0 而静默失效。

## 11. 出口辅助端点与错误语义
### 11.1 余额与健康检查
- `GET /v1/user/balance`：兼容 DeepSeek 余额接口形状，Bearer `sk-...` 鉴权，供 Cherry Studio / cc-switch 等客户端显示余额。余额来自凭证池的额度探测缓存（`QUOTA_PROBE_MINUTES` 周期刷新），按可用凭证汇总，单位为上游 credits。池内凭证从未探测成功时 `balance_known` 为 `false`（余额未知，不是 0）。
- `GET /health`：只回 `{"status":"ok"}`（进程存活，适合容器存活探针），不鉴权。
- `GET /healthz`：额外给出凭证池计数，供外部监控在池子耗尽时提前告警——那是「服务活着但用不了」的状态。两个端点都不鉴权。

```json
{
  "status": "ok", "service": "coding2api", "version": "0.4.3",
  "credentials": {"total": 5, "ready": 4, "cooling": 1, "paused": 0, "disabled": 0}
}
```

五类计数互斥且合计 = `total`，判定优先级 `disabled → paused → cooling → ready`（系统禁用 / 用户暂停优先于冷却）。口径**复用调度器的 `Candidate.is_selectable`**（`CredentialRepository.pool_counts`），不另写 SQL——两套口径一旦分叉，健康检查会报出与实际选号不符的「可用数」。`ready=0` 时对话请求直接返回 503。
### 11.2 面向用户的错误语义
管理端点返回的失败原因必须是**稳定的机器可读枚举**，不能是 Python 异常类名（类名是实现细节，重构时会漂移）。`POST /api/credentials/{id}/probe` 的失败响应：

```json
{ "probed": false, "reason": "credential_rejected", "detail": "upstream http 401" }
```

| `reason` | 含义 | 用户该做什么 |
|---|---|---|
| `credential_rejected` | 上游 401/403 拒绝凭证 | 重新登录该账号 |
| `rate_limited` | 上游 429 | 稍后重试 |
| `upstream_unavailable` | 上游 5xx | 等待上游恢复，与本账号无关 |
| `upstream_rejected` | 其他 4xx | 检查账号状态 |
| `upstream_response_invalid` | 响应结构不符 | 可能是官方接口变更 |
| `upstream_timeout` | 请求超时 | 重试 |
| `network_unreachable` | 连不上渠道服务器（DNS / 连接被拒 / 建连超时 / TLS 中断） | 检查本机网络或代理，确认渠道域名可达 |
| `unknown_error` | 未归类 | 查 `detail` |

`detail` 保留原始错误摘要**仅供排查**，界面不得把它当作主提示展示。前端 `probeFailureLabel()` 把 `reason` 翻成中文并对未知值兜底到 `unknown_error`，不得透传原始字符串。对外 400/503 文案同理只带受控标识（HTTP 状态码 / 业务码 / 类名），**不回显上游正文**（可能含上游内部 schema，甚至回显的凭证片段）。

## 12. 测试策略
**目录组织**：`tests/` 按 `src/` 子包镜像（`api/ auth/ compat/ config/ db/ deploy/ engine/ provider/<渠道>/ scripts/ stats/ tasks/ webapp/`），每个目录一个 `__init__.py`。跨模块回归与纯补覆盖用例集中在 `tests/regression/`。逐字节等价的常量/纯函数收敛到 `tests/helpers/`；夹具按作用域放在 `tests/conftest.py` 或目录级 `conftest.py`；测试私有替身类放非收集模块 `_support.py`（不得以 `test_` 开头）。**真连上游豁免**：默认全部用例走 mock；需要真实探活上游的用例用 marker 显式标注（`@pytest.mark.real_upstream("zen" | "kilo" | "openrouter")`），`conftest` 据 marker 放行对应网络访问。marker 已在 `pyproject.toml` 注册（`--strict-markers` 生效，新增必须注册）。

| 目标 | 覆盖 | 方式 |
|---|---|---|
| 调度器（到期指标/冷却/三态排序/轮换/pin） | 100% | 纯单元，注入假 provider |
| 鉴权（users/apikey/session/rbac） | 100% | 单元 + FastAPI TestClient |
| SSE 帧解析、provider 事件映射、OpenAI 协议适配、OAuth/回调解析 | 100% | 单元 + 真实样本 fixture 契约测试（codebuddy / trae） |
| HTTP 客户端 | 100% | respx mock 状态码 + body → classify 断言 |
| 统计/查询 | 100% | sqlite 内存库集成 |
| 其余 | 100% | — |

**全量 100%（行 + 分支）是硬门槛**：CI 里 `pytest --cov-fail-under=100`，新增/修改的代码必须带测试，缺口一律补测试解决，不用 pragma / 排除达标。允许的例外（必须带注释说明为何不可达）：src/ 中「按构造不可达」的防御兜底（`__main__` 入口、协议演进防御分支、前置 return 已排除的死分支）；tests/ 中测试替身实现了宽于本用例所需的接口方法。**平台差异分支不用 pragma，必须 monkeypatch 测全**（`platform.system`/`machine` 等；本地 macOS 通过不代表 CI 的 ubuntu-latest 通过）。**测试不得依赖本地 `.env`**：CI 从不带 `.env`，而本地 `.env` 会经 pydantic-settings 补上 `APP_SECRET` 等必填项。任何构造 `Settings()` 的测试都要**显式传值**（`Settings(_env_file=None, APP_SECRET=...)` 或 `setenv`），否则本地绿、CI 红。**验证手法**：临时把 `.env` 移走再跑全量，通过才算数。**fixture**：存于 `src/provider/fixtures/`（真实 SSE/JSON 样本，覆盖正文、思考、工具调用、错误码与额度），断言两个方向：**解析正确**（样本 → 期望 Event）与**不静默**（畸形样本 → `UpstreamProtocolViolation`）。当前只有 codebuddy / trae 有落盘样本；zen / kilo / qoder / codearts 的协议已用逐帧单测覆盖（含畸形帧），但**尚未补真实样本文件**——需拿到上游实际抓包后按同一契约补，避免用合成样本冒充「真实样本」。

## 13. 部署与运行时配置热更
### 13.1 运行时配置覆盖层
**分层**：

| 类别 | 例子 | 位置 | 变更方式 |
|---|---|---|---|
| 启动期不可变项 | `APP_SECRET` / `HOST` / `PORT` / `DATA_DIR` / `USERS_FILE` / 上游端点白名单 / `PROVIDER_PROXIES` | `config.Settings`（frozen） | 改 env + 重启；**不进白名单** |
| 运行时可覆盖项 | `runtime_settings.HOT_SETTINGS`（39 项） | `RuntimeSettings` 覆盖层 | 管理台改，立即生效 |

启动期项拒绝热更的原因：它们决定进程如何启动（监听地址、加密密钥、上游白名单）或作用于连接池；运行期变更只会让「当前进程」与「磁盘配置」静默分叉。**生效优先级**：`runtime_settings` 表（DB 覆盖）> `.env`（默认值来源）。覆盖行只有被管理台改过的 key；「恢复默认」= 删除该行，不是写入 env 当前值。**读取机制**：`RuntimeSettings` 对热更 key 返回覆盖值，其余属性 `__getattr__` 透明委托给 `Settings`——调用方仍写 `settings.default_model`。覆盖值在内存缓存，写入后 `reload()` 刷新；读取路径不查库。**消费端**：`config.live(value)` 把「标量或零参 callable」统一成取值器；生产装配传 `lambda: runtime.xxx`（每次读当前值），测试与一次性任务传标量。已接线的热更点：`Scheduler.expiry_window` / `secondary_expiry_window`（读取时跑 `expiry_windows()` 归一化）、`ConversationAffinity.ttl_seconds`、`Pacer.min/max_seconds`（校验从构造期挪到读取期）、`Executor.default_model` / `max_auto_continues`、`GrowthTask.allow_irreversible`、`TaskRunner` 周期与活跃上报开关时点、上下文压缩四项、`USD_CNY_RATE`。**校验**：白名单外 key 直接拒绝；类型与范围（min/max）写入前校验；跨字段组合（`pacer_min ≤ pacer_max`）用「这一批写完之后」的对端值比较，失败则整批不落库。表里坏行读取时跳过并记警告。**接口**：`GET /api/settings`（admin）返回 `snapshot()`（`key`/`env_name`/`label`/`description`/`kind`/`value`/`default`/`overridden`/`task`/`group`）+ 覆盖计数 + 网关卡组表 `groups`；`PUT /api/settings`（admin + CSRF）body `{"values": {key: 标量 | null}}`，`null` 表示恢复默认。写操作记审计日志。`snapshot()` 里的 `task` 是**配置项 → 后台任务 key** 的归属（`null` = 非任务项），`group` 是**非任务项 → 网关卡组 key** 的归属（`GATEWAY_GROUPS`：模型路由 / 选号与会话 / 渠道节流 / 后台任务节流）。不变量：每项**要么**属于一个任务，**要么**落在一个网关卡组，两者互斥且都有归属（测试守住，否则前端会漏渲染该配置项）。
### 13.2 后台任务可视化（管理台「任务与配置」页）
`SettingsPage` 用一排 tabs 控制信息量：每个任务一个 tab（该任务运行态 + 配置项），无任务归属的配置按后端下发的网关卡组各占一个 tab；一屏只呈现一块。没有配置项的分组不出 tab；归属对不上的项落进「其他」tab，绝不吞掉配置。保存按钮与「N 项待保存」常驻面板顶部，草稿跨 tab 保留、一次提交全部改动。`useTasks` 以 `refetchInterval: 30_000` 自动刷新（运行态是随时间变化的观测量），`/api/settings` 不自动刷新。
### 13.3 按渠道出站代理
`PROVIDER_PROXIES`（启动期项，默认 `""` 直连）：`渠道=代理URL;渠道2=代理URL2`，渠道取 `KNOWN_PROVIDERS`，协议 `http/https/socks5/socks5h`。作用于该渠道**全部出站请求**（聊天流、额度/模型拉取、后台任务与 OAuth 登录）。**严格解析**：未知渠道 / 非法协议 / 缺 `=` / 空 URL 一律 `ValueError`，只忽略空段——代理常带合规 / 隐私意图，「以为走了代理其实直连」是静默的安全问题，启动报错远好过静默直连。工厂 `build_client(timeout, proxy)` 统一构造 `httpx.AsyncClient(..., trust_env=False, proxy=proxy)`；各 provider client 与三个 OAuth 流（CodeBuddy/Qoder/CodeArts）增 `proxy` 参数。**为什么启动期而非热更**：代理作用于连接池，运行中改值需重建在途连接池（6 个客户端 + 3 个 OAuth 流）。
### 13.4 重启是必须的
**静态产物与进程内代码的刷新方式不同**：

- **前端**：后端对 `web/dist` 用 `FileResponse` **每次请求现读磁盘**，重新构建后刷新浏览器即生效，无需重启。
- **后端**：`src/` 下的一切（路由、依赖、`build_app` 装配）只在**进程启动时**装载。进程管理器只在进程**退出**时重新拉起、**不监听源码变化**。

两者可独立更新，于是会出现 **「新前端 + 旧后端」**的错配：页面能打开（静态文件是新的），但新端点全部失败——旧后端没有该路由，未匹配的 `/api/*` 按约定返回 JSON `404`（`webapp/static.py` 的 SPA catch-all 排除 `/api` 与 `/v1`），前端把它归一成**无 `message` 的通用失败**，弹出与实际原因无关的兜底文案。

```bash
docker compose up -d --force-recreate   # Docker / compose
sudo systemctl restart coding2api       # systemd
```

**诊断顺序**（先确认版本，再查业务）：

```bash
# 1) schema 版本已迁移（期望 18）且账号已导入
sqlite3 data/coding2api.sqlite3 "PRAGMA user_version; SELECT username, role, enabled FROM users;"
# 2) 路由存在：期望 401（未登录），404 = 旧后端进程
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/users
```

**结论**：改 `src/` 必须重启进程；改 `web/` 只需重新构建。升级 schema 时重启同时完成迁移与引导（bootstrap 幂等）。
### 13.5 日志
应用只写 **stdout / stderr**，不写文件、不自行轮转：部署形态与采集方式各异，都靠这两个 fd 对接，轮转交给平台工具（Docker json-file 驱动 / journald 自带 / logrotate，模板见 `deploy/`）。**必须在 `build_app` 里配 root logger**：uvicorn 默认 `LOGGING_CONFIG` 只配 `uvicorn` / `uvicorn.access`（`propagate=false`），**从不配 root**；root 默认 `WARNING` 且无 handler，导致 `logging.getLogger(__name__)` 的 INFO 静默丢失。生产路径 `uvicorn src.main:build_app --factory` 不经过 `run()`，所以配置必须挂在 `build_app`（幂等，见 `src/webapp/logging.py`）。级别用 `LOG_LEVEL`（默认 `INFO`）；**审计日志是 INFO 级**，调到 `WARNING` 会把它一并关掉。
### 13.6 账号体系与审计
账号存在 SQLite 的 `users` 表（`admin` / `operator` / `viewer` 三角色）。**三层引导**：① 导入 `users.txt`；② 把 `ADMIN_USERNAMES` 里点名的用户提权为 `admin`（该 env 只剩**引导期**语义）；③ **仍无活跃 admin 则启动失败**。`require_admin` 管用户管理与配置，`require_operator` 管凭证写操作；`Principal.is_operator` 是派生 property，统计口径同样以 `is_operator` 为界。**会话吊销不建会话表**：`users.session_epoch` 进签名 Cookie 的 `ep` 声明，改密 / 降级 / 禁用/启用 / 硬删一律 `session_epoch + 1`，`principal_from_request` 每请求比一次，旧 Cookie 当场失效。**角色不进 Cookie，每请求现读 DB**（塞进已签名 Cookie 会让降级延迟生效，与「降级必须立即可信」冲突）。向后兼容：老 Cookie 无 `ep` 按 0 处理。**一次性激活令牌**：管理台建号/重置密码时写随机占位密码 + 32 字节 URL-safe 令牌，库里只存 SHA-256 摘要 + 过期时间（TTL 24h），明文只在创建/重置响应里回显一次，前端拼成 `/activate?token=...`。用户在 `/activate` 自设密码即完成激活。**首登强制改密**：`must_change_password=1` 时 `principal_from_request` 用**精确白名单**（`GET /api/auth/session`、`POST /api/auth/password`、`POST /api/auth/logout`）放行，其余端点返回 403 `password_change_required`。**审计**：`audit_events(id, ts, actor, action, target, detail, ip, ok)`，动作常量集中在 `audit/actions.py`。覆盖登录成功/失败、账号建/激活/改角色/启停/改密/重置/硬删、凭证导入/删除/启停/pin/复活/切换账号。**绝不记密码或令牌明文**。保留期与 `usage_events` 同入口，由 `RetentionTask` 统一裁剪。**防锁死**三层：Web 端 `_guard_last_admin` + `SelfTargetError`；CLI 端删最后活跃 admin 拒绝；bootstrap 无活跃 admin 启动失败。硬删只在 `scripts/create_user.py --delete --force`（`usage_events.username` 是裸文本无外键，硬删会留下查不到用户名的孤儿统计，故 UI 不暴露 DELETE）。

## 14. 已知取舍备忘
- **同步 sqlite3 而非 aiosqlite**：本地微秒级操作，asyncio 封装开销大于收益。
- **双 httpx 客户端**：聊天流 `read=None` 防长流截断；短请求总超时 30s 防悬挂；共享 `trust_env=False`。非流式由引擎聚合同一流式上游（无独立 HTTP），悬挂兜底是引擎层聚合整体超时（`UPSTREAM_COMPLETE_TIMEOUT_SECONDS`，默认 600s，超时按瞬态错误换号重试）与事件条数上限（`complete_max_events`，防「快而无穷」）。
- **手写 SQL 而非 ORM**：11 张表规模下 ORM 收益为负。
- **polling OAuth 不转回调**：上游协议决定；TRAE 回调走主端口 + `PUBLIC_BASE_URL`。`/authorize` 无鉴权（浏览器 302 不带 key），防滥用靠两条：无进行中登录一律拒绝；待完成登录 600s TTL（长期挂着的 pending 会被同网络任何人用自己的 refreshToken 兑换——凭证入池、归属记为发起登录的管理员）。`app.state` 只保留 pending 的 state，不驻留含 refreshToken 的完整回调 URL。
- **Responses / Anthropic 出口只做各自客户端的子集**：不做 `store=true` / `previous_response_id`（服务端无状态）；Responses 的 `include=["reasoning.encrypted_content"]` 接受并忽略，终止事件不发 `[DONE]` 哨兵。Responses 出口经官方 SDK 契约验证并对真实 CB 上游冒烟，**未经真实 Codex CLI 端到端验证**。
- **effort 档位映射不做，但缺省补值**：客户端给什么档位上游都接受，硬编码模型白名单与实际命名无关；但客户端**完全不发** `reasoning_effort` 时上游退化成「可见推演」，故 `stream_chat` 用 `setdefault` 补 `medium`——只补缺省，不映射档位、不覆盖显式值。
- **Zen / Kilo 无凭证渠道用「虚拟凭证行」而非特判**：调度 / 冷却 / 统计 / 粘性全部按 `credentials` 行工作，种一条空凭证（`added_by="system"`）比在每个环节加「无凭证 provider」分支代价小，也让它天然出现在管理台凭证池里可暂停 / 探测 / 删除。`probe_quota` 恒 `probe_failed=True`（health `None` = 未知，**不是耗尽**）。种子只在默认装配路径执行且幂等；删除后重启复活是刻意的。
- **Zen 门禁伪装注入 `bash`/`read` 是「对上游撒谎、对客户端诚实」**：上游要求 tools 含这两者，客户端未必声明过，所以注入只补缺。回包对**本次注入的名字**按 `index` 跟踪分片：客户端有同义工具就改写成同名透传，没有才过滤并收敛 `finish_reason`。用户自带同名工具时既不注入也不处理。
- **内容风控指纹清洗只改出站副本**：`system`/`assistant` 正文命中指纹串时替换为占位符，客户端历史不受影响，避免 11128 持续拒绝。
- **统计一律以 `usage_hourly` 为准**：`overview` / `by_provider` / `timeline` / `model-timeline` 均读小时汇总，只有 `events` 读明细。代价：最近 ≤5 分钟未进汇总的请求不计入，刷新一次即可。
- **成本是下限**：匹配不到刊例价或未上报输入 token 的不计入（聚合值 `null` 显示 `—`，不拿 0 冒充免费），历史行不随价表 / 汇率重算。
- **应用日志只写 stderr，轮转交给平台**：应用自己写文件会与平台轮转争抢同一文件，容器里还会写进镜像层。
