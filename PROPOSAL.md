# Coding2API 设计文档

> 结论：九个上游厂商、十个渠道（CodeBuddy 国内 / 国际、TRAE SOLO、OpenCode Zen、Kilo、Qoder、CodeArts、OfficeAce、小浣熊、灵犀）统一为一条 OpenAI 兼容出口，公共凭证池 + 统一模型名 + 自动调度 + 按人用量统计；实现见 [TECHNICAL.md](TECHNICAL.md)，使用与部署见 [README.md](README.md)。

本文只记仍成立的目标、上游事实、架构与约束。

## 1. 目标与非目标

**目标**：单一 OpenAI 兼容出口挂十个渠道；`模型@渠道` 精确指定；凭证 admin 集中维护、全员共享；调度器自动挑健康号，上游死亡自动冷却；按人统计用量与估算成本。

**非目标（硬边界）**：

- 不做按 token 的计量计费——上游订阅制，成本不随 token 线性增长；Key / 用户级配额只是可选旁路治理，默认全关、判定异常放行，**不是安全边界**。
- 不做通用 provider 网关——硬编码十渠道 + 一个可选的通用标准端点渠道 `generic`（部署者填 base_url）。
- 不做**上游积分 → 货币**换算——各渠道积分互不通用，分开记流水；成本只按刊例价估算，USD 落库并按可配汇率另记 CNY 展示值。
- 不做数据迁移、自更新、mTLS；不做管理台与端口的 IP 限制（交给反向代理）；不做全量请求审计。

## 2. 上游事实与协议要点

- 端点 `https://copilot.tencent.com`；聊天 `POST /v2/chat/completions`，**只支持流式**（非流式由本地聚合）；头 `Authorization` + `X-User-Id` + `X-Domain` + `X-Enterprise-Id` + `X-Department-Info`（部门名须 UTF-8 百分号编码）。认证为设备码：`POST /v2/plugin/auth/state?platform=CLI` 拿 `authUrl`/`state`，轮询 `POST /v2/plugin/auth/token?state=...`；账号切换 `/v2/plugin/login/account`、`/v2/plugin/accounts`。
- 额度：个人版 `POST /v2/billing/meter/get-user-resource`（`CycleCapacity*Precise`），企业版 `POST /v2/billing/meter/get-enterprise-user-usage`（`credit` 已用、`limitNum` 总额）。签到 `POST /billing/meter/daily-checkin`，状态 `POST /billing/meter/checkin-activity-status`。
- **成长中心**（前缀 `/v2/activity/growth`）：只读 `buddy/travel/status`、`buddy/travel/config`、`tasks`、`streak`、`redeem/summary`、`lottery/chances`、`buddy/quota`、`energy`；写入 `buddy/travel/claim`、`buddy/travel/depart`、`tasks/accept`、`/tasks/{code}/claim`、`makeup-cards/use`、`redeem`、`lottery/draw`、`buddy/open`。契约：`accept_status` 五态、`/tasks/accept` 收复数数组 `{"task_codes": [...]}`（传单数一律 400）、`/redeem` 的 `tier` 是档位标识字符串（`"7d"`/`"14d"`/`"28d"`，传档位名得 400）、实发字段为 `*_granted`；进程内 OAuth bearer 可直连，无需桌面端凭据文件。
- **凭证身份可能为空**：OAuth 路径下上游未回填 `account_uid`/`user_id`，同账号隔离必须回落到 `credential_id`，否则第二个账号会被静默跳过。**活跃上报**（`POST /v2/report`，默认关闭）：body 为事件数组（`eventCode=chat_request_send`），`userId` 必填——缺失时上游 HTTP 200 `code:0` 但静默丢弃；身份为空时回落 bearer JWT 的 `sub`。与积分、调度无关。
- **reasoning 客户端给什么透传什么**（默认口径），`reasoning_content` 不剥离（实测 71 份真实 dump）；`reasoning_effort` 缺失时补 `medium`、显式值不覆盖（不发该字段上游退化为「可见推演」：思考写进 `delta.content`、`reasoning_content` 恒空、`reasoning_tokens` 恒 0），`reasoning_tokens` 在 `completion_tokens_details` 里、顶层恒缺。**可选机制（默认全关，不改默认口径）**：出站 `REASONING_EFFORT_MAP`（按渠道改写客户端显式档位）、Anthropic 入站 `ANTHROPIC_THINKING_EFFORTS`（`thinking.budget_tokens` → 档位）、`FORCE_DISABLE_THINKING`（响应侧剥离思考内容，只过滤响应、不改请求体）。三者默认关 / 留空时行为与上面逐字节一致；内置默认矩阵仍不做（未核实上游接受性），见 TECHNICAL §3.11。**输出上限键名不对称**：CB 上游完全忽略 `max_completion_tokens`，只认 `max_tokens`；TRAE 对两个键都不生效——本网关不做键映射，客户端限额原样透传。
- 错误码：`6004` 模型用量超限（模型级）、`14003` 模型饱和（模型级，国际版 `Model busy. Please switch models or retry later`）、`11102` 该后端无此模型（负缓存）、`14018` 积分耗尽、`1005` 权益/套餐耗尽、`11101/11115/11128/11135` 请求级（不罚号）。`6004` 与 `14003` 都是「换模型即可用」，必须落模型级；落账号级 SOFT 会让整张凭证的其他模型一起被 60s 冷却。

### 2.2 TRAE SOLO（`trae`）

- Host：Agent `https://trae-api-cn.mchost.guru`、UG `https://api.trae.cn`、OAuth `https://api.trae.com.cn`；聊天 `POST /api/agent/v3/llm_utils_chat`，模型 `POST /api/ide/v1/get_detail_param`，均仅流式。认证：浏览器登录 → 302 回调 → `ExchangeToken` → `GetUserInfo`；刷新 `POST /cloudide/api/v3/trae/oauth/ExchangeToken`（refreshToken 轮换）；凭证 JSON 里的 `apiHost` 是用户可控输入，导入时按官方地址白名单校验。签到 `/trae/api/v2/ug/checkin_credits/{status,claim}`；额度 `/trae/api/v2/pay/ide_user_ent_usage`。
- **签到成功必须「确认到账」，不能只看返回码**：`claim` 对当天已签过的账号也返回 `code:0 success`（幂等），判定需 `checked_in` 为真且回查 `credits` 确有增加。**签到 9074 按设备标识处理**：数字串是必要非充分条件；每次 claim 生成新的 16 位数字串，一轮内最多换号重试 2 次，其余交给 10 分钟周期。
- SSE 事件序列 `metadata` → `timing_cost` → `output`×N → `extra_info` → `token_usage` → `done`；`token_usage` 含 `cache_read_input_tokens` / `cache_creation_input_tokens`，映射为统计的 `cached_tokens`，**无 per-request credit**；流内 `error` 码 `1005` = 权益不足，`4008` = 请求额度/配额耗尽（实测 `Your requests have exceeded the quota.`，与 `14018` 同为 CREDIT：冷却到次日签到），`4001` = 参数/模型不可用，`3004` = 请求频率超限（实测 `your requests have exceeded the rate limit`，SOFT：60s 短冷却不累计三振——账号级滚动限流窗口滑过即恢复，累计会让单凭证渠道整条中冷却）。接受客户端传来的 `reasoning_effort`，不认 `thinking` 对象；`developer` 角色上游不认（静默空流），已归一为 `system`。

### 2.3 Qoder（`qoder`，真实账号）

- 域：国内 `openapi.qoder.com.cn` / 网关 `gateway.qoder.com.cn`；国际 `openapi.qoder.sh` / `api1.qoder.sh`。聊天 `POST {gateway}/algo/api/v2/service/pro/sse/agent_chat_generation`，**只支持流式**（信封式 SSE）。签名为自定义 Base64 变体（三段轮转 + 自定义字母表 + `=`→`$`），`Authorization: Bearer COSY.<payload_b64>.<md5sig>`，整套 `cosy-*` 请求头（含按 `uid` 稳定派生的 `cosy-machineid`/`cosy-machinetoken`）。登录为**设备码 PKCE（S256）**：`{website}/device/selectAccounts` 生成 challenge/nonce → 轮询 `GET {openapi}/api/v1/deviceToken/poll`（404/202 = 待授权）→ `POST /api/v1/deviceToken/refresh` 续期 → `GET /api/v1/userinfo` 补 `uid`/昵称/组织。
- 模型 `GET {gateway}/algo/api/v2/model/list?Encode=1`，**必须带整套 COSY 签名头**（裸 GET 403，带头 POST/PUT 被 400 拒绝，故方法固定 GET）；额度 `GET {openapi}/api/v2/quota/usage`（`userQuota` + `addOnQuota`）；套餐 `GET /api/v2/user/plan`。
- **入站消息归一**：`build_chat_body` 在转发前丢弃**孤立 `tool` 消息**（没有紧邻的、声明了该 `tool_call_id` 的 `assistant.tool_calls`）——上游对这类历史一律 400 `Messages with role 'tool' must be a response to a preceding message with 'tool_calls'`。只丢弃、不重排、不合并、不改 content；丢弃条数记 warning，不落消息正文。并行 tool_calls（一个 assistant 多条 tool）与 `/v1/responses` 拆出的连续多条 assistant 声明都按并集识别，避免误删真实结果。流内错误正文保留到 2000 字符（200 会截在转义 `details` 中间，根因永久丢失）。
- 签到（**活动制**）：`GET /sash/api/v1/me/campaigns` → 筛 `actionType=="CLAIM_BENEFIT"` 且 `claimStatus=="CLAIMABLE"` → `POST /sash/api/v1/me/campaigns/{campaignId}/claim`；**两个请求都必须带 `Cosy-ClientType: 10`**（缺则上游静默返回空 campaign 列表）；`replayed:true` 与 `BLOCKED`+`SAME_PERSON_ALREADY_CLAIMED` 归一为当日已签。旧 `/sash/api/v1/me/daily-check-in/{status,claim}` 仅作回退（当前旧 status 返回 `DISABLED`）；新协议不提供连续天数。**活动每日 10:00（UTC+8）刷新**，故封账到当前窗口结束而非自然日。**国际版该端点 404 → 本区域无此接口，不算错误**。
- **上游节点故障包成 400**：`[FAIL]node:… msg:Execution failed` 归模型级瞬时故障（只冷却该模型），不误判为模型不存在。

### 2.4 CodeArts（`codearts`，真实账号）

- 域：snap 引擎 `snap-access.cn-north-4.myhuaweicloud.com`、STS `sts.cn-north-4.myhuaweicloud.com`、福利网关 `opengw.developer.huaweicloud.com`、门户 `codearts.huaweicloud.com`。聊天 `POST /api/v2/chat/completions`（福利模型追加头 `maas_type: benefit`）；鉴权为华为云 **`SDK-HMAC-SHA256`**（AK/SK + `X-Security-Token`）。OAuth2 PKCE → `POST {snap-manager}/v1/oauth2/tokens` 换 AK/SK/security_token/refresh_token。模型内置 `GET {snap}/v1/model/builtin`（头 `Agent-Type: PromptCenter`）；福利 `GET {opengw}/api/v1/gateway/config`；领取 `POST /api/v1/benefit/claim`；余额 `GET /api/v1/user/tokens/balance`（每日池）；积分账 `GET {snap-manager}/v1/statistics/plugin`（头 `Agent-Type: PromptCenter`）。
- **令牌刷新是刚性的**：`refresh_token` 与 `client_id=codearts-agent` + DPoP 私钥三者绑定、**一次性**，刷新走 **DPoP ES256/P-256**，刷后必须回写新 `refresh_token`，由 token 预刷新任务独占轮转。续期凭据被上游作废时标记「需重新登录」并停止自动重试。
- **每日签到**：`GET {snap}/v1/ops/delivery?channel=IDE` → `POST /v1/ops/claim`（`{campaignId:1, idempotentKey, channel:"IDE"}`）→ `POST /v1/ops/confirm`（`{campaignId:1}`）；**`/v1/ops/*` 挂在 snap 引擎根、不带 `snap-manager` 前缀**。`status` 四态必须区分：`ELIGIBLE`→claim+confirm；`CLAIMED`（已领未确认，非终态）→只补 confirm；`CONFIRMED`/`CONSUMED`→已签不写。**claim 时积分即到账**，confirm 只推进状态；业务码走 HTTP 200，不能只看状态码。
- **单位折算**：上游余额按 token 计量（每日池 1000 万），统一折算为积分，**1 积分 = 10000 token**。该「积分」是为跨渠道排序合成的折算值，与上游签到/套餐积分同名不同物。**积分与每日 token 池是两套并行的账**：token 池给福利模型用，签到积分给系统内置模型用。**两本账都探测并展示**：`GET {snap}/snap-manager/v1/statistics/plugin`（前缀必带 `snap-manager`）的 `usageTotalPackageCredit` 进 `credentials.quota_credits_*`，分项包（基础/按需/福利）走 `quota_packages` 明细，套餐 `end_date` 登记到期阶梯（积分用不完作废，故也参与「快过期的先用」）；积分账**不参与调度口径的 health**（选号仍按每日池），管理台内置组另用派生的 `quota_credits_health` 让健康度与该组额度同源。**福利模型单请求扣池**：上游 usage 缺额度字段时按「输入 + 输出 token」补 `credit` 并标 `credit_estimated`（展示加 ≈）。
- **凭证页把 CodeArts 拆成两个渠道**（纯前端展示拆分，后端仍一个 `codearts` provider）：**CodeArts 内置**（可写，承载全部操作与积分账）与 **CodeArts 福利**（只读，只展示每日 token 池），同一批账号各出现一次，**每组只讲一本账**（内置组额度列＝积分账、流水读 `ledger=credits`；福利组＝每日池）。**到期积分也按账本拆**：积分那一段另存 `quota_credits_expiry_ladder`（选号仍读两本账合并的 `quota_expiry_ladder`），管理台 `quota_expiring_credits` ＝「合并阶梯 − 积分账段」＝每日池段、`quota_credits_expiring` ＝积分账段，福利 / 内置两组各显各的。**为什么不在后端真拆**：`refresh_token` 一次性且与 `client_id` + DPoP 私钥绑定，两条凭证行会各刷一次、第二条必报 `the refresh token has been used` 并烧掉票据。两本账的区分轴是**模型**（`is_benefit_model()` 逐请求判断），不是渠道。
- **并发上限**：上游硬限**每账号每约 60s 最多 3 个会话**，超出即 `400 TM.00001041`；配在途上限 `CODEARTS_MAX_CONCURRENCY` 与滑动窗口 `CODEARTS_REQUEST_WINDOW_SECONDS`。流式 `text` 是**累计全文（替换语义）**，解析层还原为增量事件。
- **流内业务错误分类**：业务错误内嵌在 HTTP 200 的 SSE 里（`InferHub.<码>`），由 executor 转成冷却 + 轮换。分类统一由 `Event.error_kind` 承载，`UpstreamStreamError.kind()` 与 `executor._event_kind` 共用 `provider.base.event_error_kind` 兜底（缺失 → `OTHER`），避免两处各写一份而漂移。非多模态模型收到图片（`InferHub.001001020.406 The request model is not multimodal`）归 `REQUEST`：请求侧错误不冷却、不累计、不落库，仍换号重试。

### 2.5 OpenCode Zen（`zen`，虚拟凭证）

- 端点 `https://opencode.ai`，`/zen/v1/chat/completions` 与 `/zen/v1/models`。协议是标准 OpenAI SSE，**不逆向**。
- 渠道私有的是**免费层门禁伪装**：UA `opencode/<version>` ≥1.18.0（否则 426；无版本号 403）、`x-opencode-session`、body `stream:true`、body `tools` 必须同时含 `bash` 与 `read`。对策：只在缺失时补骨架工具；模型调用注入名时改写为客户端声明的同义工具（`bash`→`shell`/`execute`、`read`→`read_file`）并复制其 schema，无同义工具才丢弃并记 WARNING。
- **免费模型清单完全动态**：免鉴权 `/zen/v1/models` 返回全部模型（含付费）且不带免费标记，唯一权威信号是匿名可用性（付费恒 401 `Missing API key.`），按 `-free` 后缀收窄 + 并发探活只留 2xx，判活按 30 分钟缓存；免费模型标 **x0 倍率**（`credit_rate=0.0`）。
- **凭证用虚拟凭证行**：无凭证/无额度接口，`probe_quota` 恒 `probe_failed=True` → health「未知」而非耗尽；删除后重启复活，永久停用请用「暂停」。401 归 `INVALID`；**403 FreeTierError 抖动在客户端内退避重试一次**，否则单虚拟凭证下 REQUEST 类错误会耗尽轮换预算、直接 503。

### 2.6 Kilo Gateway（`kilo`，虚拟凭证）

- 端点 `https://api.kilo.ai/api/gateway`，`/chat/completions` + `/models`。**标准 OpenAI 兼容**，无私有信封、无门禁伪装。**免费模型有权威标记**：`/models` 每条带 `isFree` 布尔，据此直接过滤、**不做探活**（探活会真发推理、白耗网关级约 200 req/h/IP 的极小配额）；免费模型标 **x0 倍率**。思考字段是 **`delta.reasoning`**（不是 Zen 的 `reasoning_content`）。凭证用虚拟凭证行（同 Zen）。
- 错误分类：401→`INVALID`、400/404/422→`INVALID`、403→`REQUEST`、**429 与 502/503/504→`MODEL`（模型级冷却）**——实测 429/5xx 点名具体模型、同批其他免费模型仍 200，归账号级会因单模型问题冷却整条渠道；本包不自建熔断。

### 2.7 CodeBuddy 国际版（`codebuddy_intl`）

- 端点 `https://www.codebuddy.ai`。**与国内站同协议**（同一套 `/v2/chat/completions`、`/v3/config`、`/v2/plugin/auth/*`、`/v2/billing/*` 路径，同一 Keycloak realm），但是**两套独立账号体系**（各自注册、各自计费），故作为独立渠道存在。**为什么不是「改环境变量切站点」**：`CODEBUDDY_API_ENDPOINT` 是进程级单值，切过去会让所有存量国内站凭证立刻打到国际站（额度错配 / 403）；两站可同时持有凭证，各走各的端点、节流器与登录流程。
- **白名单刻意互不含**：`CODEBUDDY_INTL_ALLOWED_ENDPOINTS` 默认只有国际站主机，**不含**国内站主机。两站 Token 由不同网关签发，跨站发送即凭证泄露，填错会在**启动时**报错。
- **能力差异**：聊天、模型列表、额度探测、token 刷新、**活跃上报**全部启用；**每日签到、成长中心未启用**——签到是渠道侧就没有这个活动（`checkin-activity-status` 回 `active: false`），成长中心**无法验证**（整个 `/v2/activity/*` 前缀挂在网关认证插件后，连不存在的子路径也返回 401 而非 404）且含不可逆动作，不盲跑。活跃上报端点 `/v2/report` 与成长中心不是同一路径，实测国际站可用。
- **唯一协议行为差异：首条 system 强制注入**——国际站网关要求 `messages[0]` 是 system，否则整单 400 + `{"code":11128,"msg":"first message is not system prompt"}`；国内站无此要求。同名模型在两站是**一条**、同时挂两个渠道。

### 2.8 冲突与陷阱

| 问题 | 事实 | 对策 |
|---|---|---|
| `codebuddy` | `POST /v2/chat/completions`，**仅流式**；设备码认证 | `11140` 账号级风控（换号/重登无效）；`6004` / `14003` 模型级；`11102` / `11158` 负缓存；`11129` schema 拒收；`11115` 上下文超限；`11134` 供应商过载 → QUEUE；`14018` / `1005` 额度耗尽 |
| `codebuddy_intl` | 同协议、独立账号体系 | 首条必须 system（否则 `400/11128`）；指纹对齐官方 CLI 2.185.0 |
| `trae` | `POST /api/agent/v3/llm_utils_chat`，仅流式 | 签到须回查积分到账；`1005` / `4008` → CREDIT；`4001` 参数 / 模型不可用；两键 `max_tokens` 都不生效 |
| `qoder` | 私有 COSY 签名 + 信封 SSE | 保活帧 `body:"null"` 必须跳过；节点故障包成 400 → MODEL；401 一律 DEAD、403 多数 SOFT |
| `codearts` | `SDK-HMAC-SHA256` + DPoP | refresh_token 一次性且与 `client_id` + 私钥绑定；并发 3 会话（`TM.00001041`）；`001001020` 非多模态 → CAPABILITY；额度＝积分 |
| `officeace` | 聊天 Basic 不过期 / 控制面 V11+SDK 双签名约 2h | 限流走伪 200；`81004` / `81009` 模型级；`max_tokens` 需改写 |
| `raccoon` | 标准 OpenAI + 网页会话 JWT | 未知模型静默回落默认 → 双层校验；`429 user_model_rate_limited` → CONCURRENCY；缺 `x-client-device-id` 回 `100002 params_invalid_error`、会话不可信回 `400 200811 device_bind_required` |
| `lingxi` | WPS `wps_sid` cookie，一次对话三条请求 | 按 run 计费 → 缓存 1h；`usage.total_tokens` 是上下文大小需自算 |
| `zen` / `kilo` | 标准 OpenAI，虚拟凭证 | zen 需门禁伪装（UA / session / tools），充值后可落 Console API Key 切付费档（单槽覆盖）；kilo 有 `isFree` 无需探活 |
| `generic` | 标准 OpenAI，部署者自己的 key | 401/403 → `DEAD`（与 zen / kilo 相反） |

**跨渠道事实**：

- **reasoning 客户端给什么透传什么**；`reasoning_effort` 缺失补 `medium`（不发则 CB 把思考写进 `delta.content`）。三项可选机制（`REASONING_EFFORT_MAP` / `ANTHROPIC_THINKING_EFFORTS` / `FORCE_DISABLE_THINKING`）默认全关，全关行为逐字节不变。
- **输出上限键名不对称**：CB 只认 `max_tokens`、TRAE 两键都不生效——不做键映射。
- **图片能力标志不可信**：入站统一归一 `image_url`，**不做模型能力门禁**，能否识图交上游裁决。
- **登录三轨**：`poll`（CB / Qoder / OfficeAce）、`callback`（TRAE 302 回 `/authorize`）、`paste`（CodeArts 本机回环监听不到）。同一账号重登按稳定身份**就地合并**，不新增重复行（重复行会绕过节流与统计口径）。
- **积分语义不同**：CB 有周期重置，TRAE / CodeArts / 灵犀各自权益包独立到期 → 健康分统一百分比，到期阶梯按渠道落库。

## 3. 架构决策

### 3.1 三平面单进程

一个 FastAPI 进程内三平面，共享同一份 SQLite（WAL + `busy_timeout=5000`）与 Provider 客户端：

- **请求面**（API Key 鉴权）：`/v1/chat/completions` · `/v1/responses` · `/v1/messages` · `/v1/models` · `/v1/user/balance` → 协议层 → 执行引擎 → 中立事件流 → 十渠道 Provider。
- **管理面**（会话 Cookie + RBAC）：React 管理台 → `/api/*`。
- **后台面**（无 HTTP 入口，`TaskRunner` 起 12 类循环）：额度探测、token 预刷新、签到、成长、活跃上报、明细清理、备份、模型列表兜底刷新、模型探活、OpenRouter 目录刷新、运维告警、上游状态聚合。

**为什么这样切**：请求面无状态、可并发，不知「人」是谁（只认 Key 归属用户）；两鉴权面互不替代（Key 进不了管理台，Cookie 进不了 `/v1`）；后台面与请求面共用 Provider 与节流器——风控按最小间隔生效，不能各发各的；三面共享一份 SQLite，升级只重启一个进程。

### 3.2 Provider 边界与会话粘性

Provider 只管上游私有部分（发请求、解析事件、分类错误、凭证生命周期、额度探测）；调度、冷却、重试、统计全在共享引擎。接口三类：凭证生命周期、`probe_quota`、执行与分类。

**会话粘性三级**：body 显式键（`conversation_id` / `prompt_cache_key`）> 请求头（`x-session-id` 族，须匹配 `[A-Za-z0-9._:-]{1,128}`）> 消息增量前缀指纹。头夹中间：body 最精确，头比指纹可靠；头值掺 `username` 再哈希，否则可猜头值占用别人账号；带 `user_id` 时不派生前缀兜底键（同用户并行对话前缀可能相同）。**请求级错误（`REQUEST` / `CONTEXT` / `NETWORK`）换号后不重绑**——上游前缀缓存按账号隔离，误重绑会让命中率从 97–99% 掉到 0–12%。

### 3.3 调度与冷却

选号顺序：**手动 pin**（绝对过滤，不是同档提权）→ **免费优先**（`credit_rate == 0`，`None` 不算免费；x0 会被实测扣费校准）→ **在途最少**（并发拆分，静态指标会全压同一个号）→ **到期额度两级**（36h 主窗口，7 天次窗口）→ **健康度三态**（`known > unknown > exhausted`，探测失败算 0 分会让其永远轮不到）→ `credential_id` 稳定。

**冷却必须分两层**：`429 + 6004` 是「此模型在当前账号用超」，`400/404 + 11102` 是「当前账号没此模型」；一律记账号级 → 一次模型级限流踢掉整号。故账号级写 `cooling_until`（带来源 `cooling_kind`，`revive` 默认保留上游点名的限流 / 额度类），模型级另建 `credential_model_cooldowns`；账号级冷却出现时清该凭证模型级条目，防「切模型」绕过。

时长：`PLAN` 12h、`SOFT` 60s、`OTHER` 连续 3 次→10m、`MODEL` 10m 起翻倍封顶 2h、`BLOCKED` 6h 起封顶 24h、`CONCURRENCY` 首枪 60s 且同一命中链才翻倍（封顶默认 5min / raccoon 30min）、`CREDIT` 到次日 04:00、`RESTRICTED` 2h 起按命中次数翻倍（封顶 7 天；同渠道窗口内第二个不同凭证再中招 → 该渠道整体 24h）。**上游点名的恢复时刻优先于常量**：`Retry-After` 或正文 `reset at … UTC+8` 解析后钳在 `[now + 常量, now + 24h]`；CB `429+6004` 头缺失时只能读正文。

**传输层失败归 SOFT**：`httpx.TransportError`、`ssl.SSLError`、`UpstreamTransportError` 统一在 `executor._classify` 兜住；不泛化到 `OSError`，`ssl.SSLError` 必须单列（否则逃成 `internal_error`：不换号、不冷却、统计零痕迹）。

**「窗口内积分总量」而非单一到期日**：CB 额度是几十个各自到期的小包，只存一个日期没有区分度；落库到期阶梯 `[(到期 epoch, 包剩余积分)]`，过滤 `end > now` 且排除已用完，两级窗口才够区分。展示用 `quota_packages` 与调度用 `quota_expiry_ladder` 分开存。

### 3.4 模型名与出口

三字段派生：**原代号**（`raw_id`，转发时经别名表换回，永不改动）、**归一键**（合并键 / 对外 id）、**展示名**。`/v1/models` 的 `id` 一律归一键，原代号进 `by_provider.{渠道}.raw_id`；同模型异名由 `MODEL_SYNONYMS` 收敛，只登记人工核实；哨兵名（`auto` / `default`）不跨渠道合并。

中性事件层（`EventKind`：`content` / `reasoning` / `tool_calls` / `usage` / `finish` / `error`）之上挂三个出口适配器：OpenAI chat、Responses（Codex CLI）、Anthropic Messages（Claude Code）。**入口语义收口**：`response_format` 只放行 `text` / `json_object` / `json_schema`；Anthropic `cache_control` 默认原样透传，只校验结构，无标记时映射逐字节不变。

### 3.5 数据落库边界

- **落库**：调度状态、到期阶梯、签到审计（`checkin_events`）、竞态敏感的凭证状态。重启不丢。
- **进程内**：签到去重、模型列表缓存、任务运行态、迟滞计数——重启归零，**不编造记录**。去重是运行态，与审计日志是两回事。
- **文件**：模型目录 `DATA_DIR/model_catalog.json` 与 `openrouter_catalog.json`（默认 `DATA_DIR=./data`）。
- **脱敏纪律**：不存提示词、回答、请求头、Token、工具参数、原始错误体、会话 ID。唯一例外 `DUMP_REQUEST_BODIES=true`（默认关，有界保留 200 份，不得长期开启）。
- **schema 变更纪律**：`schema.sql` 只加不改；新增列走幂等补列，删表须同时补删表迁移并升版本（`CREATE TABLE IF NOT EXISTS` 对老库无效，不补删则遗留死表）。

### 3.6 观测与告警口径

- **失败归因按尝试计**：硬失败每次尝试一行，限流 / 额度 / 超时 / 上下文等每次请求一行且类型取最后一次错误的真实归因；`failures − rescued` 是未被救回的失败尝试数。
- **九类告警**：池耗尽、任务连续失败、token 到期、上游错误率、单 Key 用量暴涨、非工作时段请求、预算分层、渠道零成功、签到连续失败。后两类补的是既有规则的两个盲区：`channel_dead` 绕开错误率被 fallback 稀释，`checkin_failing` 绕开「任务跑成功但上游回失败」不计任务失败。
- **上游状态聚合只做解释不做决策**，不进选号与熔断；只存内存不落盘（重启后拿几小时前的「一切正常」比「尚未抓取」更危险）。


## 4. 关键取舍与否决理由

| 决策 | 否决的替代 | 理由 |
|---|---|---|
| 同步 sqlite3 + 手写 SQL | aiosqlite / ORM | 本地微秒级操作，asyncio 封装与 ORM 开销大于收益（十几张表规模） |
| 单进程三平面共享一份 SQLite | 管理面独立服务 | 两面共用执行引擎与仓储；升级只重启一个进程 |
| 双 httpx 客户端 | 单客户端通吃 | 聊天流需 `read=None` 防长流截断，短请求需总超时防悬挂 |
| 账号级 + 模型级两层冷却 | 只记账号级 | 一次模型级限流会踢掉整号 |
| `codebuddy_intl` 独立渠道 | 切 `CODEBUDDY_API_ENDPOINT` | endpoint 是进程级单值，切过去会让存量国内凭证打到国际站（两站账号与额度不通用） |
| Zen / Kilo 用虚拟凭证行 | 每环节加「无凭证 provider」分支 | 调度 / 冷却 / 统计 / 粘性全按 `credentials` 行工作，种一条空凭证代价更小 |
| Zen 注入 `bash` / `read` | 直接报错要求客户端带工具 | 「对上游撒谎、对客户端诚实」：只补缺，有同义工具就改写透传，没有才过滤并收敛 `finish_reason` |
| 三轨 OAuth，`/authorize` 靠会话 + `loginTraceID` 双绑定 | 只靠 Origin / Referer 判同源 | 回调本就跨源 302，`no-referrer` 可绕过 |
| effort 映射默认不做，只 `setdefault` 补 `medium` | 内置渠道默认矩阵 | 未实测即编造；改为默认关闭的三项机制 |
| 批量运维只收已存在的单条动作 | 批量 `refresh` / `delete` | `refresh` 强刷白耗一次性票据；`delete` 需二次确认。批量 `toggle` 传显式 `enabled` 而非取反（混选会让一半号反向） |
| 统计以 `usage_hourly` 为准 | 全部读明细 | 小时汇总永久、明细 90 天；代价是最近 ≤5 分钟未汇总不计入 |
| 成本估算为下限 | 拿 0 冒充免费 / 猜价 | 匹配不到价表或未上报输入 token 就不计，`null` 显示 `—` |
| 批量审计只记一条汇总 | 逐条审计 | 50 个号是一次操作、一个意图 |
| 窄屏表格卡片化改样式不改 DOM | 按视口渲染两套 JSX | 页面测试按 testid 断言，DOM 一变换语义就没了 |
| 凭证页一个渠道一个组，后端不拆行 | 每条 token 一行 | `refresh_token` 一次性且绑定 `client_id` + DPoP 私钥，拆行会二次刷新并烧票 |
| 内置检索工具独立出口 | 只在 Responses 里映射 `web_search` | 先有可直连的出口，再谈把托管工具映射过来（未做） |

## 5. 安全边界（审查结论）

| 面 | 结论 |
|---|---|
| 上游端点 | 各渠道独立 `*_ALLOWED_ENDPOINTS`；CB 国际白名单**刻意不含国内站**（只校验「在名单里」不够，Token 会照样跨站发）；TRAE `apiHost` 用户可控 → 导入按白名单拒；`generic` 非回环强制 https（明文 http 只允许回环，防 API key 在链路上裸奔）；officeAce `model_base_url` **出口按 DNS 复核**（每个解析结果过 SSRF 黑名单，解析失败按阻断）——它带的是不过期的 Basic 凭证 |
| 凭证加密 | Fernet + `APP_SECRET`（**建议 ≥32 字符随机串**，低于 16 或低熵/已知占位值拒启动）；**丢失 = 凭证全部不可解**，不做轮换；解密失败给可行动错误码，批处理路径跳过该凭证 |
| API Key | 仅存摘要，明文只回显一次；可限渠道 / IP（默认不信 `X-Forwarded-For`，仅 `TRUST_PROXY=true` 取最后一个，且 `TRUSTED_PROXIES` 非空时只对名单内对端解析）/ 模型（无法判定时 fail closed）/ 到期；发删与导出都进审计；`daily_cost_usd` 在存在无价请求时 fail closed（量不出成本就不声称有配额） |
| 用户级策略 | 会话 Cookie 只回答「你是谁」，Playground 是免 Key 直连 → 模型白名单与配额必须落在人身上，否则 viewer 用 Cookie 就能绕过 Key 的限制；Playground 的模型列表也按用户白名单裁剪（列表侧不能比调用侧更宽） |
| kill switch | 拦在鉴权依赖而非各端点（新端点不会漏拦）；管理台与登录保持可用（否则开了就关不上） |
| 会话 | `SameSite=Lax` + 写操作 CSRF；`Secure` 按实际协议（登出删除 Cookie 同属性）；无 Cookie 的写端点也做跨站防护；吊销用 `session_epoch` 自增而非会话表，**登出也 bump**（被窃 Cookie 立即失效，不必等 12h 过期） |
| 登录限流 | IP / 用户名两级固定窗口 + PBKDF2 并发上限；**回环只在没有 `X-Forwarded-For` 时豁免**（反代回源的对端恒为 127.0.0.1，无条件豁免会让 IP 维度全废） |
| 错误与日志 | 错误响应不回流上游正文；未匹配 `/api`、`/v1` 返回 JSON 404（任何方法）；`ClientDisconnect` → 499；日志页 `redact_or_drop` 整行过滤，宁可漏一条也不把凭证推进浏览器；日志落库走**有界队列 + 后台线程**（队列满即丢，绝不在事件循环上抢写锁） |
| 输入 | 请求体上限（登录 8KB / 备份恢复 512MB / 其余 16MB）；备份上传**流式落盘**（不把 512MB 读进内存）；拒绝越界数值与 `NaN` / `Infinity`（含 `1e999`）；SSE 单行 / 单帧缓冲有界；非 2xx 错误体与短接口成功体都有界读取 |
| 观测出口 | `/metrics` 在 `METRICS_TOKEN` 留空时**只放行回环对端**；公网入口模板（Caddy / nginx）另已摘除该路径 |
| 热更 | 写入即校验（float 拒 NaN/±inf、长度上限）；webhook 只接受 `http(s)` 且做 SSRF 校验（写入期 + 投递前按 DNS 复核） |

**明确不做**：mTLS；管理台与端口的 IP 限制（交给反向代理）；全量请求审计。

## 6. 风险清单

| 风险 | 等级 | 对策 |
|---|---|---|
| CB 协议复杂度高 | 高 | 先 bearer-only 跑通再补 OAuth；不可逆动作用配置开关总控 |
| 上游 SSE / 私有协议变更 | 中 | 样本存 fixture 做契约测试；解析失败不静默（`UpstreamProtocolViolation`） |
| 模型 ID 撞车导致路由错误 | 中 | 扁平名 + 归一键合并 + 健康度路由 + `@渠道`；统计行强制带 provider |
| 未知模型静默回落 | 中 | 发请求前按目录校验 + 流首帧比对回显 `model`（raccoon 实测会回 200 + 默认模型） |
| 上游 credit 不保证可得 | 低 | credit 仅辅助展示；健康度只依赖额度探测接口 |

## 7. 路线图未完成项

- **`generic` 多渠道**：一个部署只配一条（渠道 id 固定 `generic`）；多接几家需把 `KNOWN_PROVIDERS` 动态化，未做。
- **Zen 客户端档位**：`ZEN_CLIENT_PROFILE=v2`（复刻官方 2.x 指纹）已实现但默认不切——2026-11-19 上游免费额度耗尽，两档均无法端到端复验，待额度恢复后 A/B。
- **Responses / Anthropic 出口验证**：仅按官方 SDK 做契约验证，**未经真实 Codex CLI 端到端**。
- **`cache_control` 透传实测覆盖**：6 家已验证；zen 因 `FreeTierError` 不能验证、OfficeAce 未跑探针，小浣熊与灵犀未验证。
- **内置检索工具**：国际站是否同样挂载 `/agenttool/*`、搜索 / 抓取是否消耗积分，均未核实；Responses 的 `web_search` 托管工具仍未映射过来。
- **告警「多 IP 同 Key」**：不存 IP，故该规则未实现。
- **CodeArts 签到之外的活动操作**：未接入。
- **上游状态聚合**：只做解释不做决策，不进选号 / 熔断（不是缺陷，是边界）。

## 8. fork 待办：Qoder 国内/国际双区域同池共存

本 fork 在 `my/main` 上维护 Qoder 双区域共存的待办，合并上游（2026-11-21，含
v0.8.0 之后的全部演进）后此问题仍未解决，优先核对这里。

上游对 Qoder 国际版的支持是「按凭证 realm」——聊天网关已按凭证取址（intl 走
api1→api2→api3 故障切换），但三处仍是**进程级单值**，故一次只能完整服务一个
区域（env 指向哪边，另一边的对应能力就失效）：

| 断点 | 位置 | 现状 |
|---|---|---|
| openapi 业务端点（额度/签到/token 刷新） | `provider/qoder/client.py` 各方法（`self.host`） | 全局单 host；响应头已按凭证 realm 取（`build_openapi_headers`），URL 未跟上——半成品状态 |
| 登录区域 | `main.py` `_upstream_auth`：`QoderOAuth(detect_realm_from_domain(host))` | 由 `QODER_API_ENDPOINT` 推导，一个实例一个区域（`QoderOAuth` docstring 已预留「两区域用两个实例」的说法，未接线） |
| 网关覆盖 | `client.py` `_gateway_hosts` 的替换条件 | 自定义 gateway 对**默认区域（cn）凭证**也替换首项——env 指向国际版时 cn 凭证的聊天会被带去国际网关（403 code 105） |

**实施方案（B：补齐按凭证 realm，不动渠道结构）**：

1. openapi URL 从 `self.host` 改为 `credential.realm_config().openapi`（`realm_config()`
   现成，headers 已经这么做，补齐 URL 即可）
2. `_gateway_hosts` 的覆盖条件收敛为「自定义 gateway 只对所属区域的凭证生效」
3. 登录入口带区域选择：`_upstream_auth` 为 qoder 建两个 `QoderOAuth` 实例
   （cn / intl），前端「登录 Qoder」旁加「登录 Qoder 国际版」

**明确不做 A 方案**（复制 `qoder_intl` 独立渠道，同 CodeBuddy 国际版模式）：要动
调度渠道表、模型排序、前端与一批测试，且与上游分歧大——每次合并上游都要重解
冲突（2026-10-20 合并 v0.8.0 时模型列表区已为此付出 16 文件的冲突代价）。

**不改代码的临时同池法**：env 保持国内默认，用「JSON 导入」手工导入国际版凭证
（JSON 带 `"realm": "intl"` 或 `"domain": "qoder.com"`）——聊天与模型列表正常
（网关按凭证走），代价是额度探测失败（显示「无探测」）且无自动刷新（token
到期需重导）。

**2026-11-21 合并注记**：本次合并已把模型列表子系统切换为上游实现（原「每模型
一条、对外 id 带渠道后缀」设计被上游的归一合并 + 别名表机制取代）；上游的
`MODEL_ALIASES`、`GET /v1/models/{id}`、条目 `cost` / `reasoning_efforts` /
`benefit` 字段均已生效。钉渠道可用 `模型@渠道` 写法。同步修复的合并回归与
平台适配（详见 git log 2026-11-21）：

- `runner.py` 补回被上游覆盖的 `checkin_enabled` 两处消费点（`_sync_checkin`
  短路 + `_task_enabled` 分支），否则 `CHECKIN_ENABLED` 配置/热更彻底失效；
- `src/compat/anthropic/request.py` 清除旧「system 扁平化」死代码链（上游
  就地保留语义生效，fork 原实现成为不可达分支）；
- 平台门禁收录 `win32`：`instance_lock` 补 `msvcrt.locking` 强制区域锁、
  `private_files` ACL 白名单放行 `OWNER RIGHTS`（S-1-3-4）——维持 Windows
  裸机部署（`start_coding2api.bat`）不被上游的 POSIX-only 收窄掐断。
