# Coding2API

把 **CodeBuddy**（国内站 + 国际版）、**TRAE SOLO**、**OpenCode Zen**、**Kilo Gateway**、**Qoder**、**CodeArts** 七个上游渠道封装为 OpenAI 兼容 API，提供公共凭证池、统一模型名与调度、按人用量统计。

> [!WARNING]
> 逆向接口，仅供学习研究，未做安全审计；公网部署需反向代理 + 鉴权 + IP 白名单。

## 特性

- **OpenAI 兼容出口**：`/v1/chat/completions`（流式 + 非流式）、`/v1/responses`（Codex CLI）、`/v1/messages`（Anthropic / Claude Code）、`/v1/models`、`/v1/user/balance`（DeepSeek 兼容余额）。
- **统一调度**：扁平模型名按健康度自动选号，`模型@渠道` 强制指定。候选含免费渠道时**免费优先**，之后按三态健康度 + 分级冷却避开坏号，打平则**余额多者优先**。模型级限流只避让该模型（同账号其他模型立即可用）；某模型只有一个渠道持有且它在模型级冷却时，直接回「该模型暂不可用」。同一对话多轮粘住同一凭证，出错才轮换。
- **到期额度优先消化**：主窗口（默认 36h）内将过期的额度多者先用，避免过期浪费；打平再比 7 天窗口。额度单位统一为积分。
- **模型列表按渠道凭证加载**：只展示**当前有可用凭证**的渠道，未接入不出现幽灵模型；暂停或凭证失效时其模型暂时消失，恢复即回来。
- **公共凭证池**：admin 集中维护、全员共享、加密入库（`APP_SECRET`）；设备码登录、多账号切换、额度探测、每日签到、token 预刷新。
- **三角色账号体系**：`admin` / `operator` / `viewer`，用户存 SQLite；一次性激活链接自设密码、首登强制改密、改角色/停用即时吊销会话；登录与写操作留审计。
- **成长中心**（仅 CodeBuddy）：自动领 Buddy 旅行礼物、派 Buddy、领任务奖、断登补登、连登兑换、开盲盒；不可逆动作可用 `GROWTH_IRREVERSIBLE_ACTIONS=false` 关停；管理台可手动执行。
- **脱敏统计**：不存对话内容；明细 90 天、小时汇总永久；按人/渠道/模型/凭证可视化；按 OpenRouter 刊例价估算成本（人民币为主）。
- **管理台安全加固**：登录限流、CSRF 校验、请求体上限、Host 白名单。

协议层实现（私有信封、签名、门禁伪装等）见 [`TECHNICAL.md`](TECHNICAL.md)。

## 快速开始

### 前置要求

Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js 24+ 与 pnpm 10+（仅构建前端需要）。

### 本地运行

```bash
uv sync
# 建首个管理员（之后都在管理台「用户管理」里加人）
uv run python scripts/create_user.py admin --role admin
cd web && pnpm install && pnpm build && cd ..
APP_SECRET="换成你自己的随机字符串" \
  uv run python -m uvicorn src.main:build_app --factory --port 8000
```

打开 <http://127.0.0.1:8000> 登录管理台。

> `scripts/create_user.py` 直接写 SQLite（`--db` 或 `DATA_DIR`，默认 `data/coding2api.sqlite3`）。老部署若已有 `secrets/users.txt`，启动时自动导入一次（已存在的不覆盖）。

### Docker

```bash
cat > .env <<'EOF'
APP_SECRET=换成你自己的随机字符串
PUBLIC_BASE_URL=http://127.0.0.1:8000
EOF
docker compose build
docker compose up -d
# 建首个管理员（容器内执行；之后在管理台加人，无需再进容器）
docker compose exec coding2api python scripts/create_user.py admin --role admin
```

拉取 GHCR 镜像（推荐，跳过本地构建）：

```bash
docker compose pull
# 或指定版本：docker pull ghcr.io/robbsluo/coding2api:v0.4.3
```

推送新版本：打 tag `v*` 推到 main 即触发 publish workflow（`.github/workflows/publish.yml`），同时打 `<tag>` 与 `:latest` 到 GHCR；也可在 Actions 页面手动触发。

## 用户与角色

账号存在 SQLite 的 `users` 表里，管理台「用户管理」页（仅管理员可见）负责日常增删改。

| 角色 | 能做什么 |
|---|---|
| `admin` | 用户管理、任务与配置、凭证读写、全部统计、审计日志 |
| `operator` | 凭证读写（含导入 / 删除 / 启停 / 签到 / 成长 / 切换账号）、全部统计 |
| `viewer` | 只读：看仪表盘、凭证列表、统计与 Playground |

**加人**：管理员填用户名 + 角色，创建后得到**一次性激活链接**（有效 24 小时，只显示这一次）。把链接经可信渠道发给本人，对方打开 `/activate` 自设密码即可登录——系统里不存在「管理员知道但用户不知道」的初始密码。忘记密码同理：点「重置密码」生成新链接。

**改角色 / 停用**：即时生效——立刻作废该账号所有已签发会话 Cookie（重新登录即可）。停用是默认的「删人」方式（可逆，用量统计仍归属该用户名）；**硬删只在 CLI**（会让 `usage_events` 留下查不到用户名的孤儿记录）。

**自助改密**：右上角用户菜单 →「修改密码」，需要当前密码。

**防锁死**：不能停用/降级**最后一个活跃管理员**，也不能对自己降级或停用。要交接权限，先加另一个 `admin` 再降自己。

**CLI 兜底**（无 Web 场景 / 全部管理员失联时）：

```bash
python3 scripts/create_user.py alice --role admin   # 交互输入密码
python3 scripts/create_user.py --list
python3 scripts/create_user.py alice --delete --force   # 硬删，不可逆
python3 scripts/create_user.py --db data/x.sqlite3 ...  # 指定库
echo "$PASSWORD" | python3 scripts/create_user.py alice --role admin --password-stdin  # 密码不进 argv / shell 历史
```

`ADMIN_USERNAMES` 与 `secrets/users.txt` 只在**引导期**起作用（首个管理员、老部署升级）：启动时把 `users.txt` 里的用户导入 DB（已存在的不覆盖），再把 `ADMIN_USERNAMES` 点名的人提权为 `admin`；此后角色只来自 DB。

## 审计

「审计日志」页（仅管理员可见）记录登录与写操作，可按操作者/动作筛选、分页查看：**登录**（成败都记）、**账号变动**（新建、激活、改角色、禁用/启用、改密、重置密码、硬删）与**凭证写操作**（导入、删除、启停、指定 pin、复活、切换账号）。每条含时间、操作者、动作、对象、说明与来源 IP。**绝不包含密码或令牌明文**（一次性激活令牌明文只在创建响应出现一次，库里只有 SHA-256 摘要）。保留期与请求明细一致（默认 90 天），由后台清理任务裁剪。

## 使用

1. 「凭证管理」添加凭证：
   - **CodeBuddy**（含国际版）：设备码登录，或粘贴 `{"token":"..."}`。
   - **TRAE**：粘贴凭证 JSON（`accessToken`/`uid`/`refreshToken`）或回调链接（`apiHost` 只接受官方地址）。
   - **Qoder / CodeArts**：设备码 / OAuth2 授权码登录（CodeArts 授权后把打不开的本地回调链接粘回页面）。
   - **OpenCode Zen / Kilo Gateway**：无需添加——启动时自动出现虚拟凭证（被删可在同面板点「添加」补回）。
2. 「API Key」创建 `sk-...`（明文仅显示一次）。
3. 调用：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-你的key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'
```

`模型@渠道` 强制指定上游（如 `glm-5.2@trae`、`mimo-v2.5-free@zen`），不写则自动选健康渠道。任意 OpenAI 兼容客户端可直接接入（Base URL `http://127.0.0.1:8000/v1`、Key 用 `sk-...`、模型名以 `GET /v1/models` 为准）；「Playground」页用登录会话直接测试，无需 API Key。

### 模型名三字段

七条渠道每个模型统一为三个字段：

1. **原代号**（`raw_id`）：渠道请求时真正发的 key，**永不改动**。各渠道内部代号互不相同（Qoder `kmodel_latest` = TRAE `kimi-k3-1`），也可能带命名空间前缀（Kilo 的 `kilo-auto/free`）。
2. **归一键**：剥掉免费档标记（`-free` / 路径段 `free`）与命名空间前缀，再 slug 化——`kilo-auto`、`longcat-2.5-preview`。模型列表按它判断**哪个是同一个模型**并合并，故 Zen 的免费档能与 TRAE 的付费档并成一条。
3. **展示名**（`name`）：清洗后的可读文本，如 `LongCat 2.5 Preview`、`Kimi K3`。上游给了可读名就用（`Qwen3.8-Max` → `Qwen3.8 Max`），没有就从原代号派生；品牌与缩写按官方写法纠正（`DeepSeek` / `MiMo` / `GLM` / `GPT`）。

`GET /v1/models` 里**所有条目的 `id` 都是归一键**，转发到某渠道时自动换回该渠道原代号，`by_provider.{渠道}.raw_id` 可见。**原代号、展示名、归一键三种写法都能直接请求**（`kmodel_latest`、`Kimi K3`、`kimi-k3` 指向同一模型）。同渠道内归一键重复时退回原代号，避免撞名误并。

### 流式 usage 与思考 token

- **流式带 usage 帧**：收尾时在 finish chunk 之后、`data: [DONE]` 之前补一帧 `choices: []` 的 usage（OpenAI 标准形态），只从流里读用量的客户端据此显示 tok/s、上下文占用率与会话 token 统计；上游没上报时不补（不发 0 占位），非流式出口的 `usage` 同理，未上报字段一律为 `null`。
- **思考 token 需客户端带 `reasoning_effort`**：CodeBuddy 渠道在客户端**未给**时补 `medium`（上游缺此字段会把整段思考以「可见推演」写进正文）；显式给的值原样透传、不被覆盖。

## 渠道

> CodeBuddy（含国际版）/ TRAE / Qoder / CodeArts 需填凭证；**OpenCode Zen 与 Kilo Gateway 是无凭证的免费层渠道**——池里是一条虚拟占位行（让调度 / 冷却 / 统计照常工作），额度列显示「免费层（无额度接口）」，健康度恒为「无探测」（未知 ≠ 耗尽）。虚拟行**删除后重启会复活**，永久停用请用「暂停」。

### OpenCode Zen 免费层（`zen`）

接 [opencode.ai/zen](https://opencode.ai) 的**免费模型**，无需账号或登录。上游清单混着付费模型且不带免费标记，本服务按 `-free` 后缀收窄候选、再逐个探活，**只把匿名真正可用的免费模型**放进 `GET /v1/models`（判活缓存 30 分钟，不维护静态白名单；免费模型标 **x0**）。上游清单不提供模型名，展示名由模型 id 派生。

**免费层门禁**：上游要求伪装成官方客户端（UA 版本、会话头、`stream:true`、tools 含 `bash`/`read`），本服务自动满足；注入的 `bash`/`read` 是空壳，模型真调用时这类 tool_call 被过滤掉，不泄漏你没声明过的函数。UA 版本用 `ZEN_OPENCODE_VERSION` 调，端点用 `ZEN_API_ENDPOINT`（须在 `ZEN_ALLOWED_ENDPOINTS` 内）；付费模型即使强制 `模型@zen` 也只报「不可用」。

### Kilo Gateway 免费层（`kilo`）

接 [Kilo Gateway](https://kilo.ai)（`api.kilo.ai/api/gateway`）的**免费模型**，无需账号或登录。对外是**标准 OpenAI 兼容协议**，无私有信封、无门禁伪装；上游 `/models` 每条带权威 `isFree` 布尔，本服务**据此直接过滤**免费集、**不做探活**（探活白耗本就极小的免费配额）。免费模型标 **x0**。

**额度与限流**：免费层额度很小（网关级约 200 请求/小时/IP），实为 OpenRouter 免费池的转发；上游 **429 与 502/503/504 都是模型级**——只冷却被点名的那个模型，本服务不自建熔断。端点用 `KILO_API_ENDPOINT`（须在 `KILO_ALLOWED_ENDPOINTS` 内）。

### Qoder（`qoder`，真实账号）

接 [Qoder](https://qoder.com)，**需要真实账号**：在「凭证管理 → 登录渠道账号」点「登录 Qoder」，按提示在浏览器完成设备码授权（登录全程后端轮询完成，Token 不经过浏览器）。上游是私有 COSY 协议（自定义 Base64 编码 + 信封式 SSE），本服务负责签名与解包，对外仍是标准 OpenAI 协议。

**额度与签到**：支持额度探测（`GET /api/v2/quota/usage`）与每日签到（积分可累积）。签到为**活动制**（`/sash/api/v1/me/campaigns`，需带 `Cosy-ClientType`），旧 `daily-check-in` 仅作回退；本服务自动查询并领取可领活动，管理台可手动触发。**签到活动每日 10:00（UTC+8）刷新**（领取后 30 天有效），后台任务按该窗口封账：凌晨看到的「已领」只封到当天 09:59，10:00 新一轮出现后自动补签。新协议不提供连续天数，界面不再显示「连续 N 天」。**国际版没有签到接口**（该端点 404），记为「本区域无此接口」，不是错误。

**端点与节流**：默认国内版（`openapi.qoder.com.cn` + `gateway.qoder.com.cn`），国际版改 `QODER_API_ENDPOINT=https://openapi.qoder.sh` 并确认白名单含国际版域名（签名携带完整 `cosy-*` 头，白名单防误发）。默认 `QODER_CHAT_MIN_INTERVAL=5`（独立节流器），可在「任务与配置」热更。

**上游节点故障**：Qoder 有时把自身推理节点故障包成 400（`[FAIL]node:… msg:Execution failed`）。本服务识别为**模型级瞬时故障**——只冷却该模型，耗尽候选时返回「该模型暂时不可用」的 503（`no_healthy_credential`），而非误报「模型不存在」；稍后重试通常自愈。

### CodeArts（`codearts`，真实账号）

接 [华为云 CodeArts](https://codearts.huaweicloud.com)，**需要真实账号**：点「登录 CodeArts」，浏览器完成 OAuth2 授权后会跳到打不开的本地地址（`http://127.0.0.1:12800/oauth/callback?code=…`）——把**整条链接**粘回页面，服务端换取 STS 凭证（AK/SK + security_token + refresh_token + DPoP 私钥）并加密入库。

**令牌刷新是刚性的**：refresh_token 与 `client_id=codearts-agent`、DPoP 私钥三者绑定且**一次性**，刷新后必须回写新 refresh_token，否则账号失效；由 token 预刷新任务自动完成（DPoP ES256/P-256 签名）。预刷新窗口封顶 45 分钟且必须**宽于**轮询周期（`REFRESH_INTERVAL_MINUTES`，默认 30 分钟）。续期凭据被上游作废时，该凭证标记「需重新登录」并停止自动重试，重新登录后按「恢复」解除。

**每日签到**：上游有「每日签到领 1000 积分」活动（`GET /v1/ops/delivery` → `POST /v1/ops/claim` → `POST /v1/ops/confirm`）。后台任务每日自动领取，管理台可手动触发；`claim` 后**必须** `confirm`（`CLAIMED` 是「已领未确认」而非终态），本服务两步一起做。**这套积分与每日 token 池是两套并行的账**：token 池给福利模型（`deepseek-v4.1-flash` 等）用，积分给系统内置模型（GLM-5.2 / OpenPangu 等）用，互不通气。

**优先消耗**：每日池用完即弃，故当日剩余登记为「次日本地 0 点到期」的到期额度，调度器优先排在其它渠道之前。上游按 token 计量，管理台统一显示为**积分**（1 积分 = 10000 token，每日池满额 = 1000 积分）；升级前落库的历史数据由 `scripts/convert_codearts_credit_unit.py` 一次性折算。**该「积分」是为跨渠道排序合成的折算值，与上游签到/套餐积分同名但不同物**。

**签名 / 累计全文 SSE / 节流与并发**：上游要求华为云 `SDK-HMAC-SHA256`（AK/SK + `X-Security-Token`），白名单 `CODEARTS_ALLOWED_ENDPOINTS` 必须含 snap 引擎、STS、福利网关与门户四个主机。流式 `text` 是**累计全文（替换语义）**而非增量，本服务在解析层还原为增量事件，对客户端透明。默认 `CODEARTS_CHAT_MIN_INTERVAL=5`；上游硬限**每账号并发会话数 3**，故配在途上限 `CODEARTS_MAX_CONCURRENCY` 与滑动窗口 `CODEARTS_REQUEST_WINDOW_SECONDS`（每账号每 60s 最多启动 3 次），把突发挡在 `400 TM.00001041` 之前。均可热更。

### CodeBuddy 国际版（`codebuddy_intl`，真实账号）

接 CodeBuddy 国际站 [www.codebuddy.ai](https://www.codebuddy.ai)，**与国内站同协议**（同一套 `/v2/chat/completions`、`/v3/config`、`/v2/plugin/auth/*`、`/v2/billing/*` 路径，同一 Keycloak realm），但**是两套独立账号体系**：各自注册、各自计费，故作为**独立渠道**存在。登录链路与国内版完全一致（设备码，Token 不经过浏览器）。

**为什么不是「改一个环境变量切站点」**：`CODEBUDDY_API_ENDPOINT` 是进程级单值，切过去会让所有存量国内站凭证立刻打到国际站（额度错配 / 403）。两个站点可同时持有凭证，各走各的端点、节流器与登录流程。

**白名单刻意互不含**：`CODEBUDDY_INTL_ALLOWED_ENDPOINTS` 默认只有 `https://www.codebuddy.ai`，**不含**国内站主机。两站 Token 由不同网关签发，跨站发送即凭证泄露，因此把国内站地址填进来会在**启动时**直接报错。

**能力差异**：聊天、模型列表、额度探测、token 刷新、**活跃上报**全部启用；**每日签到、成长中心未启用**——签到是**渠道侧就没有这个活动**（`checkin-activity-status` 回 `active: false`），成长中心则**无法验证**（国际站把整个 `/v2/activity/*` 前缀挂在网关认证插件后，连不存在的子路径也返回 401 而非 404），而其含抽奖 / 连登兑换 / 盲盒等**不可逆**动作，不盲跑。管理台的「签到」「成长中心」按钮对该渠道不显示。活跃上报端点 `/v2/report` 与成长中心不是同一路径，实测国际站可用，故照常启用。

**同名模型不带渠道后缀**：两站共有的模型在 `/v1/models` 里是**一条**、同时挂两个渠道；`-x` / `-sg` 这类**真实存在的不同上游模型**仍各占一条。

**节流**：`CODEBUDDY_INTL_CHAT_MIN_INTERVAL` 默认 5s，**独立节流器**，与国内站互不排队，可热更。

### 按渠道出站代理（可选）

`PROVIDER_PROXIES` 给每个渠道单独指定出站代理：

```
PROVIDER_PROXIES="codebuddy=http://127.0.0.1:7890;qoder=socks5://127.0.0.1:1080"
```

格式 `渠道=代理URL;渠道2=代理URL2`，渠道取七个渠道名，协议支持 `http/https/socks5/socks5h`，留空 = 全部直连。作用于该渠道**全部出站请求**（聊天流、额度/模型拉取、后台任务与 OAuth 登录）。**启动期项**，改后需重启。**严格解析**：未知渠道 / 非法协议 / 缺 `=` 一律启动失败——「以为走了代理其实直连」比启动报错更糟。环境变量 `HTTP_PROXY` 等一律**不采信**（`trust_env=False`，防部署环境全局代理意外劫持带 Token 的上游请求）。

## 出口协议

### Responses API（Codex CLI）

`POST /v1/responses` 提供 Responses 子集，供 [Codex CLI](https://github.com/openai/codex) 这类只走 Responses 的客户端接入。与 `/v1/chat/completions` 共用同一套选号 / 冷却 / 轮换 / 统计与会话粘性，只换入站映射与出口翻译：

```bash
export CODING2API_KEY=sk-你的key
codex -c "model_providers.coding2api={ name='coding2api', base_url='http://127.0.0.1:8000/v1', wire_api='responses', env_key='CODING2API_KEY' }" \
      -c model_provider=coding2api \
      -c model='glm-5.2' \
      '你的任务'
```

支持文本、流式正文、思考摘要（`reasoning` item）、函数工具调用与 `finish_reason=length` → `response.incomplete`。`text.format` 映射到 chat 的 `response_format`（`text` / `json_object` 直通，`json_schema` 展开成 `{type, json_schema:{name, description?, schema, strict?}}`）。**不支持** `store=true`、`previous_response_id`（服务端无状态，不假装支持）、Responses 私有工具——一律显式 400，不静默降级。`include=["reasoning.encrypted_content"]`（Codex 每轮必带）接受但忽略。

### Anthropic Messages API（Claude Code）

`POST /v1/messages` 提供 Anthropic Messages 子集，供 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 接入。与其它出口共用同一套选号 / 冷却 / 轮换 / 统计与会话粘性：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_AUTH_TOKEN=sk-你的key     # 或 ANTHROPIC_API_KEY（走 x-api-key 头）
claude
```

**鉴权**：`x-api-key` 与 `Authorization: Bearer` 都接受。**流式**：`message_start` → `content_block_start/delta/stop` → `message_delta`（含 `stop_reason` 与 usage）→ `message_stop`；无 `[DONE]` 哨兵，thinking 块在 `content_block_stop` 前补 `signature_delta`。**缓存命中**：出口 usage 按 Anthropic 语义拆两笔——`input_tokens` 只计未命中，命中另记 `cache_read_input_tokens`（内部 `input_tokens` 是 OpenAI 口径的 `prompt_tokens`，含命中），Claude Code 据此显示缓存读 token。**`count_tokens`**：`POST /v1/messages/count_tokens` 本地估算输入 token（不转发上游）。**不支持**：图片 / 文档块、Anthropic 服务端工具一律显式 400。

### 余额查询

`GET /v1/user/balance` 兼容 DeepSeek 余额接口形状，Bearer `sk-...` 鉴权，供 Cherry Studio / cc-switch 等客户端显示余额。余额来自凭证池的额度探测缓存（`QUOTA_PROBE_MINUTES` 周期刷新），按可用凭证汇总，单位为上游 credits：

```json
{
  "is_available": true,
  "balance_infos": [{"currency": "credits", "total_balance": "60.00",
                     "granted_balance": "0.00", "topped_up_balance": "0.00"}],
  "balance_known": true,
  "providers": [{"provider": "codebuddy", "remaining": 50.0, "total": 150.0, "credentials": 2}]
}
```

池内凭证从未探测成功时 `balance_known` 为 `false`（余额未知，不是 0）。

### 池健康检查

`GET /health` 只回 `{"status":"ok"}`（进程存活，适合容器存活探针）；`GET /healthz` 额外给出凭证池计数，**并在 `ready == 0` 时回 HTTP 503**（就绪探针）——那是「进程活着但没有可用凭证」的状态，对话请求必然 503，负载均衡/外部监控应据此摘流量。计数在两种状态下都返回，监控可读到「为什么不可用」。两个端点都不鉴权。

```json
{
  "status": "ok", "service": "coding2api", "version": "0.4.3",
  "credentials": {"total": 5, "ready": 4, "cooling": 1, "paused": 0, "disabled": 0}
}
```

五类计数互斥且合计 = `total`，与调度器同一口径。`ready=0` 时 `/healthz` 返回 503 且 `status` 为 `unavailable`；请把**存活**探针配在 `/health`（恒 200），把 `/healthz` 用作就绪/告警——把容器 healthcheck 指到 `/healthz` 会在池子耗尽时把容器标成 unhealthy，而重启并不能修好一个没有可用凭证的池子。

> 升级注意：`/healthz` 的 HTTP 状态从「恒 200」变为「ready=0 时 503」。只判 HTTP 200 的外部监控会在此后开始报警，这是预期行为；若你只想要计数、不想被状态码影响，改读 `/health` + 自行解析计数即可。

## API Key 的绑定、白名单、来源 IP 与到期时间

创建 Key 时可限定渠道、模型、来源 IP 与到期时间，适合「按出口分发 Key」。

- **渠道绑定**：选定渠道后该 Key 只在该渠道凭证里选号；模型属另一渠道时直接 400 并指出实际归属（不静默改道）。留空 = 自动（跨渠道选健康凭证）。`模型@渠道` 与绑定冲突时同样 400。
- **来源 IP 白名单**：逗号分隔的 IP 或 CIDR（如 `203.0.113.9,10.0.0.0/8`），留空 = 不限制；写入时校验并规范化（`10.0.0.1` → `10.0.0.1/32`），来源不在白名单返回 403。**默认不采信 `X-Forwarded-For`**（客户端可写）；仅 `TRUST_PROXY=true` 时按 XFF 判定、取**最后一个**条目，故只适用于「本服务前恰好一层受信反代」。
- **模型白名单**：逗号分隔的模型名或 fnmatch glob（如 `glm-*,kimi-k3`），留空 = 不限制；匹配不区分大小写，`模型@渠道` 后缀不参与匹配，白名单外返回 400，`/v1/models` 也只列白名单内模型。
- **到期时间**：epoch 秒，留空 = 永不过期，到期后立即 401（与「Key 不存在」统一文案，不泄露可枚举信息）。

## 凭证暂停

凭证列表行内菜单的「暂停」只把该凭证摘出**对话流量**：后台的额度探测、token 预刷新、每日签到、成长中心、活跃上报照常运行（这些任务只认系统硬禁用 `disabled`）。适合「先不接聊天、但积分继续领」；「取消暂停」立即放回池子。与状态列的「已禁用」不同——那是渠道判定会话失效后的系统禁用，需重新登录后用「恢复」解除。同一开关也在 `POST /api/credentials/{id}/toggle`。

> OpenCode Zen / Kilo Gateway 是无凭证渠道，池里是一条虚拟占位行。**暂停它是永久停用的唯一方式**——删除后下次重启会被种子重新补上。暂停也会把该渠道从模型列表摘掉。

## 凭证批量运维

池子一大，「逐个点签到 / 探测」就不现实。凭证页提供多选 + 批量操作条：每行一个 checkbox、组头一个「全选本渠道」，勾选后顶部出现操作条（探测额度 / 每日签到 / 暂停 / 恢复 / 取消选择）。

- **后端批量端点**：`POST /api/credentials/batch`，body `{action: "probe"|"toggle"|"checkin"|"revive", ids: [...], enabled?}`（`enabled` 仅 `toggle` 用）。一次请求最多 200 条，重复 id 自动去重。
- **逐条隔离**：单条失败不影响其余条——凭证不存在、渠道不支持该能力、上游报错都只体现在该条结果里，其余条照常执行。响应为 `{action, total, succeeded, failed, results: [{id, ok, error?, detail?}]}`：`ok` 表示该条动作是否成功；`error` 表示这条根本没执行到上游（`credential not found` / `credential does not support ...` / `action_failed`），`detail` 是动作自身的返回体（probe 的剩余额度、checkin 的积分与状态）。
- **暂停是显式状态而非取反**：批量「暂停」对所有选中项传 `enabled=false`，不会让「已启用 + 已暂停」的混选走向相反方向。
- **只收已有单条端点的动作**：`refresh`（token 预刷新）没有单条入口且由后台按到期窗口驱动，批量强刷会白耗一次性票据；`delete` 破坏性太强，需要单独的二次确认流，都不走这个通道。
- **审计只记一条汇总**（`toggle` / `revive`）：批量暂停 50 个号是一次操作、一个意图，逐条落库会把审计流水刷成噪声。`probe` / `checkin` 与单条端点同为只读语义，不写审计。

## 凭证限额护栏

凭证级**可选**护栏，给「这个号别用超」一个显式开关：行内菜单「设置限额」配置，四个护栏各自独立、全部留空 = 不受限（老库升级后行为不变）。判定在**选号前**完成，触顶的凭证不再参与对话流量；后台任务（签到 / 探测 / 成长中心）照常运行，与「暂停」同口径。

- **保留积分**：账户剩余积分 **≤** 该值时整号停用——给号留底，避免把额度跑空。与当日用量无关；余额未知（渠道不上报）时不猜、不停用。
- **每日 token 上限**：当日累计 `输入 + 输出` token **达到**该值时整号停用。
- **每日积分上限**：当日累计 credit 达到该值时整号停用。
- **按模型 token 上限**：`{模型: 上限}`，某模型当日累计达到上限时**只停该模型**，其余模型照常可用。
- **当日 = 北京时间自然日**（与签到、活跃上报同一时区），0 点刷新计数，避免与上游「每日重置」出现半天错位。
- **无可用凭证 503**：当所有候选都因限额被跳过时，复用 `no_healthy_credential`（账号级原因，换个模型也未必能用）；与「模型级冷却」的 `model_unavailable` 区分开。
- 写入 `PUT /api/credentials/{id}/limits`（整体替换：只传要开的护栏，缺省字段 = 关闭该护栏；四个全关时删除配置行）。改动即时生效（写入后主动失效选号侧快照，不等 TTL）。审计动作 `credential.limits`。

> 实现注：判定需要「当日聚合用量」，那是每请求都变的动态数据，因此不进 `Candidate.is_selectable`（纯函数，只吃凭证自身状态），而在 executor 选号前用 `LimitGuard` 过滤；`LimitGuard` 对「限额表 + 当日用量」做进程内快照缓存（默认 15s），避免每次选号都查一次聚合。

## 凭证池的筛选与分组

凭证多了以后，列表页把「找到某个号」这件事拆成三层，默认首屏只铺渠道级总览：

- **筛选工具条**：搜索（昵称 / 渠道 / id，忽略大小写）× 渠道 × 状态三件套。三者是 **AND** 关系，纯前端收敛，不改后端排序（后端仍按 `sort` / `order` 给全量）。命中时显示「筛选后 N / 总数」，并提供「清空」；无匹配给空态而非空白表格。渠道 / 状态下拉里的计数来自当前全量，切换筛选不会自我缩窄。
- **组排序**：单独一个控件，只重排「渠道组」，不动组内行序（行序仍由列头 / 后端 `sort` 决定）。默认**额度从高到低**，按组内额度剩余合计排名，让还能打最多的渠道排前面；可切回**渠道顺序**（固定，30s 轮询不会让组头乱跳）。无额度接口的免费层垫底（不当 `0` 参与比较），额度相同（含都为 null）时回落到渠道顺序。
- **按渠道分组**：列表按渠道聚合，组头做成一张紧凑的「渠道仪表盘」——品牌图标 + 渠道名 + 账号数，加上**状态分布**（图标 + 计数，只列出现过的态，口径与行内状态徽章同一套 `credentialState`）、**额度合计**（占比环 + 数字；环是纯图形仪表，精确百分比写在「额度合计」标签行，对组内非 null 值求和，免费层 zen / kilo 无额度接口时显示 `—` 而不是 `0`，也不出环），以及**到期积分**（主窗口 1.5 天一块，次窗口 7 天仅在**比主窗口多**时再出一块——次窗口是主窗口的超集，数字相同说明没有超出 1.5 天的部分，CodeArts 每日池当天清零、两窗口恒等，就只列一块，不把同一个数字说两遍）。点组头折叠 / 展开该渠道，**默认全折叠**（首屏先看结构，要看具体账号再展开）；筛选生效时自动展开命中渠道，清空筛选回到折叠总览。组顺序默认「额度从高到低」，也可用工具条切到固定的全站渠道顺序（CodeBuddy、TRAE 在前），不随行排序键变化。渠道名只出现在组头，行内不重复。
- **列头跟着展开区走**：没有全局表头，列头（含排序按钮与列说明）渲染在每个展开的渠道组内部，并在滚动容器里吸顶——折叠态下组头本身就是总览，列名没有对应内容，只有展开某组时才需要「这一列是什么」的锚点。同一时间可能有多个组展开，各组列头共用同一套排序状态（点任一处都写回同一个后端 `sort` 参数）。**「成长中心」列只在支持的渠道（CodeBuddy）出现**，其余渠道留一个空占位格——列数保持恒定，否则 auto 布局会重算列宽、下面展开的组整列错位。
- **长列表可滚**：表格限高（70vh），滚到第 20 行时该组的吸顶列头与「操作」按钮仍在。

额度列的数字带虚线下划线：悬浮显示**本行的周期口径**（本周期剩余 / 账户剩余 / 每日额度池 / 免费层无额度接口），逐行的同款文案收进悬浮说明。

## 模型列表加载

`GET /v1/models` 与 Playground 的模型下拉只合并**当前有可用凭证**的渠道：该渠道至少有一条**未暂停、未会话失效**的记录。判定在缓存之前——没凭证的渠道不读缓存、不拉上游、不展示，故**从未接入**的渠道不出现「幽灵模型」；**全部暂停 / 凭证失效**时其模型暂时消失，恢复或补一条凭证后立刻回来；冷启动默认只有 `zen` / `kilo`（自带虚拟凭证）。冷却中的凭证仍算「有凭证」——只是暂时限流，列表不闪没。

**展示顺序**：CodeBuddy、TRAE、Qoder、CodeArts 的模型依次排在前面，组内按模型名字典序；多渠道模型按最高优先级渠道归位。纯展示排序，不改变调度选号。

**请求路由只认这份模型缓存列表**：模型目录能证明某模型归属哪些渠道时，候选渠道就只有它们——登记了该模型的渠道全部不可用（都在冷却 / 都被暂停）时直接返回 503，不再把请求逐个渠道试过去（各上游只会回 11102 / 4001 / 401，还会给渠道写下数小时的「该后端无此模型」负缓存）。模型完全不在目录里时仍按全部候选渠道路由；直连指定（`模型@渠道`）与 Key 渠道绑定不受影响。

### 模型目录落盘与启动即用

模型表除进程内 TTL 缓存（300s）外，每次拉取成功还写快照到 `DATA_DIR/model_catalog.json` 并在**启动时同步读回**：重启那一刻「模型 → 渠道」归属表就已就绪，扁平名请求不会在预热（zen 免费模型探活要十几秒）跑完前扇出打错渠道。快照存未过滤原始表，损坏 / 超 7 天只丢缓存、不影响启动。另有 `MODEL_CATALOG_MINUTES` 后台任务兜底刷新。

Playground 与 `GET /v1/models` 走 **stale-while-revalidate**：有缓存立刻回旧列表，TTL 到期的渠道丢后台异步刷新；只有某渠道**一条缓存都没有**时才同步等一次。代价是列表最多滞后一个 TTL。

### 管理台「模型列表」页

管理台「模型列表」页（`GET /api/model-catalog`）只读展示 OpenRouter 公开目录：刊例价（输入 / 输出 / 缓存读 / 缓存写，USD / 百万 token，可切人民币）、上下文·输出上限、输入→输出模态、能力（推理 / 工具调用 / 附件 / 结构化输出）、知识截止，以及 **Artificial Analysis 三项指数**（综合智能 / 编程 / 智能体，经 OpenRouter 公开接口，**非本服务实测**，不做排序）。目录缺失时页面显示空态，不影响聊天与成本。

OpenRouter 收录数百条，其中多数本服务没接。每行带 `available` 布尔（该模型是否**当前渠道真能提供**，口径与 `/v1/models` 一致），页面提供**默认关闭**的「只看当前渠道」开关，勾选后本地收窄到当前渠道获取到的模型（不重新请求）。

「能力分」列头可点排序，按**综合智能指数**（智）排；无分数的行排末尾（不冒充 0 分）。只给这一个分项排序入口——表头一列对应一个入口，三项各排会把表头挤成一排按钮。

### token 到期展示

凭证列表的 **token 剩余**列显示 access token 距到期还有多久，低于 `TOKEN_EXPIRY_WARNING_SECONDS`（默认 1 小时）标红。到期时间优先取上游 `expires_at`，缺失时回落 JWT `exp`；都取不到显示 `—`，**不猜本地 TTL**（否则是凭空捏造的预警）。

## 统计

### 口径

时间范围默认**最近 24 小时**（另有 7 天 / 30 天 / 全部）：打开统计页的第一问题是「这个池子现在怎么样」，7 天会把当天的波动摊平。

**明细失败类型**是受控枚举（后端 `CONTROLLED_ERROR_TYPES`，界面只展示中文翻译）：模型级冷却耗尽记「该模型暂时不可用」（`model_unavailable`，换个模型即可用），凭证池真耗尽才记「无可用凭证」（`no_healthy_credential`）——两者对外错误码相同（`no_healthy_credential` 503），仅统计口径区分。

**请求 ID（`X-Request-ID`）**：每个请求都在响应头带一个 `X-Request-ID`（任何响应都带，含被拒的 413 / 400）。入站 `X-Request-ID` 合法则沿用（便于与客户端日志对齐），否则自生成；合法字符集 `[A-Za-z0-9._:-]`、长度 1–128，越界即丢弃改用自生成（客户端可写，不校验会被塞入换行/超长串）。该 ID 随请求落进 `usage_events.request_id`，统计页「请求明细」的**时间列悬停**可见——用于把用户报的一个请求与这行明细对齐。老库历史行为 `NULL`（不回填）。**不透传到上游**：各渠道出站头语义不同（qoder 的 `X-Request-ID` 是 uid 派生的防重放值，trae/codearts 有指纹敏感头），统一改写会破坏既有契约。

统计页 **Credit 消耗**列：CodeBuddy 是上游**真值**；TRAE 的 `token_usage` 帧只有 token 数，credit 按官方单价**推算**并标 `≈`；CodeArts 福利模型按每日池 1:1 扣减推算，同样标 `≈`。单价表集中在 `src/provider/trae/pricing.py` 与 `src/provider/codearts/units.py`，未收录的模型不推算（显示 `—`）。历史明细可分别用 `scripts/backfill_trae_credit.py` / `scripts/convert_codearts_credit_unit.py` 补齐。

**成本（估算，≈）**：按 token × 公开刊例价估算，不是上游真实扣费。单价取自 **OpenRouter 公开模型目录**（`pricing.prompt` / `completion` / `input_cache_read`，USD / 百万 token），按 `(输入−命中)×输入价 + 命中×缓存价 + 输出×输出价` 折算美元，再按**写入时生效的汇率**（`USD_CNY_RATE`）折人民币。

- **写入时定值**：成本在写明细时算好落库（`usage_events.cost_usd` / `cost_cny`），历史行**不随价表或汇率重算**；上线前历史两列为 `NULL`，可用 `scripts/backfill_cost.py --apply` 补齐（默认预览、写库前自动备份、幂等）。
- **匹配不到就不计**：本地模型名按 OpenRouter 的 `id` / 展示名等值匹配（`src/model_match.py`，**命中不唯一就不配**），未匹配或未上报输入 token 的不计入，聚合值 `null` 显示 `—`（**不拿 0 冒充免费**），故成本是**下限**。

**缓存命中率**：**Token 消耗**卡片在 token 分项后追加缓存命中率 = 命中 token ÷ 输入 token（保留 1 位小数）；**未上报或输入为 0 时显示 `—`**。

### 积分记录

凭证**额度**列数字后的下箭头展开「积分记录」，记录**两次额度探测之间的净变化**，不是动作归因：签到、成长、对话都会改余额而上游不打日志，探测只能看到区间净变化，界面一律写「净变化」而非「签到 +5」。首次探测只建基线；余额没变不记；余额变「未知」仍记但不填变化量；保留 90 天。

### 分组维度与重新汇总

**分组统计**面板用 tabs 切四个维度（按渠道 / 按模型 / 按用户 / 按凭证），共用同一张表（请求数 / 成功数 / 成功率 / 输入 / 输出 / 命中缓存 / Credit / 成本），对应端点 `GET /api/stats/by-provider|by-model|by-user|by-credential`。默认按请求数降序，点列头可切换排序。**按用户 / 按凭证**只对 admin 与 operator 展示。**口径差异**：渠道 / 模型 / 用户三维读**小时汇总**（永久保留，最近 5 分钟内的请求尚未计入）；**凭证维度读明细表**（小时汇总主键里没有 `credential_id`），故只覆盖**最近 90 天**。

**点行钻取**：点分组表任意一行，下方「请求明细」即按该行钻取（渠道 / 模型 / 凭证按同名列过滤，用户维度走作用域参数），条件叠加、可多个维度层层收窄，面板上方显示当前过滤标签并可一键清除。`GET /api/stats/events` 因此接受 `provider` / `model` / `credential_id` 三个过滤参数（`AND` 语义）。

**导出 CSV**：明细面板右上「导出 CSV」（仅 admin / operator）按**当前时间范围 + 当前钻取过滤**导出逐请求明细，`GET /api/stats/events?format=csv`。文件带 UTF-8 BOM（Excel 认编码）、`\r\n` 换行、时间列 ISO 8601（UTC），并对以 `= + - @`（及制表符/回车）开头的**文本**单元格加前导单引号防公式注入（数值含负数原样）。单次导出上限 10000 行，超出截断并在响应头 `X-Export-Truncated: 1` 标出，页面提示收窄条件。

若历史数据偏小（老库升级前落的明细没被覆盖、或补过明细后没重算），用 `python3 scripts/rollup_hourly.py --apply` 一次性重算（默认预览、**写库前自动备份**、幂等）。脚本先报告缺口与两侧时间跨度，并提示**明细已过保留期无法还原**的区间。

### 列表排序

管理台各列表**列头可点排序**：点一次用该列的「首次点击方向」（数值列先降序、文本列先升序），再点同一列在升序 / 降序间切换。排序一律由**后端**完成——所有 `GET` 列表端点接受统一的 `sort`（白名单键）与 `order`（`asc` / `desc`）参数。**未知 `sort` 键或非法 `order` 值回落到默认排序，不报错**；`sort` 是白名单键而非 SQL 列名，注入面为零。

适用端点：`/api/credentials`、`/api/users`、`/api/api-keys`、`/api/audit`、`/api/alerts`、`/api/model-catalog`、`/api/credentials/{id}/credit-events`、`/api/credentials/{id}/accounts`、`/api/stats/by-*`、`/api/stats/events`（逐请求明细：默认按 rowid 游标分页返回 `next_before`；按其它列排序或时间升序时改用 `LIMIT/OFFSET` 并返回 `total`）。各端点的白名单键与默认排序见 `src/api/`。

## 后台任务

额度探测、token 预刷新、每日签到、成长中心、活跃上报、明细清理、数据库备份、渠道模型列表刷新、模型目录刷新（OpenRouter）、运维告警共 10 类，由 `TaskRunner` 自动调度，失败互不影响。每类任务的上次执行时间、最近结果与错误在管理台**「任务与配置」页**查看（30 秒自动刷新），运行态只存在于**本次进程**内，重启归零。

**渠道模型列表刷新**（`MODEL_CATALOG_MINUTES`，默认 30、下限 5）是兜底保鲜：模型表本身按「谁访问 `/v1/models` 或 Playground 谁刷新、TTL 300s」更新，这条只保证**没人访问时也会更新**。周期取 30 分钟是跟 zen 免费模型判活缓存对齐——再密也不会让 zen 多探活。

**成长中心**仅 CodeBuddy 有。Buddy 旅行 1–4 小时回来一次，故周期默认 60 分钟（`GROWTH_INTERVAL_MINUTES`）。抽奖 / 连登兑换 / 开 Buddy 盲盒 / 消耗补登卡属**不可逆动作**，`GROWTH_IRREVERSIBLE_ACTIONS=false` 可全部跳过。凭证列表「成长中心」列显示每个账号最近一轮结果，行内菜单可手动执行一次。

### 活跃上报（可选，默认关闭）

CodeBuddy 成长中心的「连登天数 / 活跃地图」按日统计客户端对话事件，账号长期只被本网关自动调用时会断连登。`ACTIVITY_REPORT_ENABLED=true` 时，后台任务在每天 `ACTIVITY_REPORT_HOUR`（默认 10 点）所在整点窗口为每个账号补发**一条** `chat_request_send` 事件续上连登——与积分、调度无关，上报失败不影响聊天请求。凭证行内菜单也可手动补报一次。适用于 CodeBuddy 与 CodeBuddy 国际版。

> ⚠️ **风险与限制**：官方活动条款禁止用模拟器 / 脚本篡改活动数据，处罚为**取消资格并追回已发礼品**。默认关闭，开启前请自行评估账号风险。事件名与请求形状依赖上游实现，**上游改版即失效**，不承诺任何收益，不作为可靠性功能。实测（2026-09-21）：缺少 `userId` 时上游返回 HTTP 200 `{"code":0}` 却**静默丢弃**；OAuth 凭证的 `user_id`/`account_uid` 可能为空，此时回落取 bearer JWT 的 `sub`，仍缺失则跳过（不编造）。

### 数据库备份与恢复

**后台自动备份**（`BACKUP_INTERVAL_MINUTES`，默认 1440 分钟 = 每日一次）把主库一致性快照写进 `DATA_DIR/backups/`，并只保留最近 `BACKUP_KEEP` 份（默认 7）。

- **用 `VACUUM INTO` 而不是复制文件**：WAL 模式下主库文件不含尚未 checkpoint 的已提交事务，直接 `cp` 会丢数据。`VACUUM INTO` 读一份一致性快照并重写成**紧凑、无 WAL 的独立库**，恢复时只需原子替换主库。
- **落盘即校验**：写完立刻 `PRAGMA integrity_check`，不过则删掉半成品并记日志——一个写坏的备份比没有备份更危险。产物 `0600`、目录 `0700`（库内是凭证密文与明文用量/审计）。
- **失败不影响服务**：备份是普通后台任务，异常只记日志、进任务卡片，不拖垮聊天请求。

**手动备份**（升级/迁移前留一份）：

```bash
python3 scripts/backup_db.py                 # 备份到 DATA_DIR/backups/，保留 7 份
python3 scripts/backup_db.py --keep 14       # 保留 14 份（0 = 不清理）
python3 scripts/backup_db.py --out /path/dir # 指定备份目录
```

**恢复**（**先停服**，脚本不做「服务是否在跑」探测——容器/权限都可能误判）：

```bash
python3 scripts/restore_db.py data/backups/coding2api-20261014-030000.sqlite3            # 预览：只校验，不改动
python3 scripts/restore_db.py data/backups/coding2api-20261014-030000.sqlite3 --apply    # 校验 → 安全副本 → 原子替换
```

恢复流程：校验备份完整性（不过直接拒绝，绝不把坏文件换进生产库）→ 给现有主库做一份 `.pre-restore-<时间戳>` 安全副本（同样走 `VACUUM INTO`，不漏 WAL 中未 checkpoint 的事务）→ 原子替换主库 → 删除 `-wal`/`-shm` 残留（它们属于旧库，留着会损坏新库）。**恢复后重启服务。**

## 配置

常用项如下；完整的可配置项见 `src/config.py`（权威），且**每一个都已透传到 `docker-compose.yml`**。容器里改监听地址用 `HOST`/`PORT`（`PORT` 同时决定宿主机映射端口）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_SECRET` | **必填** | 凭证加密密钥，≥16 字符；丢失 = 凭证全部作废，无密钥轮换。启动时拒绝低熵值（全同字符 / 周期重复 / 字符集过小） |
| `ADMIN_USERNAMES` | 空 | **仅引导期**：逗号分隔的用户名，首次启动时被提权为 `admin`；DB 里已有角色后不再生效 |
| `USERS_FILE` | `secrets/users.txt` | **仅引导期**：老式用户文件路径，启动时一次性导入 SQLite（幂等）。账号唯一源是 `users` 表 |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | 浏览器可达地址；TRAE 登录回调依赖它 |
| `DATA_DIR` | `./data` | SQLite 与运行数据目录 |
| `DEFAULT_MODEL` | `glm-5.2` | 模型为空/`auto` 时的默认 |
| `QUOTA_PROBE_MINUTES` | `60` | 额度探测周期（下限 1 分钟） |
| `REFRESH_INTERVAL_MINUTES` | `30` | token 预刷新轮询周期（下限 5 分钟）；短寿命渠道的预刷新窗口封顶必须**宽于**它 |
| `MODEL_CATALOG_MINUTES` | `30` | 渠道模型列表兜底刷新周期（下限 5 分钟） |
| `OPENROUTER_CATALOG_MINUTES` | `1440` | 模型目录刷新周期（下限 60 分钟）：后台从 OpenRouter 公开接口一次抓取刊例价（成本估算）、明细元数据（「模型列表」页）与 Artificial Analysis 三项指数（Playground /「模型列表」页 / `/v1/models` 能力分）并落盘；上游变动少，默认每日一次 |
| `OPENROUTER_MODELS_URL` | `https://openrouter.ai/api/v1/models` | 模型目录来源地址（匿名可读、无需 key）；改后需重启 |
| `USD_CNY_RATE` | `6.70` | 成本估算的美元兑人民币汇率；可热更，只影响之后写入的请求 |
| `GROWTH_INTERVAL_MINUTES` | `60` | 成长中心（仅 CodeBuddy）一轮领取的周期（下限 5 分钟） |
| `GROWTH_IRREVERSIBLE_ACTIONS` | `true` | 是否允许成长中心的不可逆动作：抽奖、连登兑换、开盲盒、消耗补登卡 |
| `ACTIVITY_REPORT_ENABLED` | `false` | 活跃上报（CodeBuddy 与国际版）：每天为账号补发一条对话事件续连登。**默认关闭**——条款禁止篡改活动数据，上游改版即失效 |
| `ACTIVITY_REPORT_HOUR` | `10` | 活跃上报的本地（北京）时间整点窗口；仅在开关开启时生效 |
| `ALERT_ENABLED` | `true` | 运维告警开关：后台周期评估四类风险（池耗尽 / 任务连续失败 / token 临近到期 / 上游错误率骤升），命中落 `alert_events` |
| `ALERT_WEBHOOK_URL` | 空 | 告警 Webhook：命中时 POST JSON（body 带 `status`：`firing` / `resolved`），多个用逗号分隔（≤16 个，仅 `http(s)`，拒内网/回环/链路本地/保留地址防 SSRF，投递前再按 DNS 解析复核一次、解析失败即跳过），并发投递、全部成功才算成功（总预算 15s）；请求恒带 `X-Coding2API-Signature: sha256=<HMAC-SHA256(APP_SECRET, body)>` 供收端验真；留空 = 只留站内记录 |
| `ALERT_INTERVAL_MINUTES` | `5` | 运维告警评估周期（下限 1 分钟） |
| `ALERT_SILENCE_MINUTES` | `30` | 同一 `(规则, 对象)` 的静默窗：窗口内只落库 / 推送一次；`0` 关闭静默 |
| `ALERT_FOR_TICKS` | `1` | 进入迟滞：同一告警连续命中这么多轮才真正告警（滤掉单轮毛刺）；`1` = 首轮即报 |
| `ALERT_CLEAR_TICKS` | `2` | 恢复迟滞：告警中的条目连续这么多轮不再命中才确认恢复（写 `resolved_at` 并外推 `status="resolved"` 通知），避免单轮抖动误撤 |
| `ALERT_POOL_READY_MIN` | `1` | 池耗尽阈值：可用凭证数少于该值时告警；`0` 关闭该规则 |
| `ALERT_TASK_FAILURES` | `3` | 后台任务连续失败达到该次数时告警（成功一轮清零）；`0` 关闭 |
| `ALERT_TOKEN_EXPIRY_HOURS` | `24` | token 剩余时间少于该小时数时告警；`0` 关闭 |
| `ALERT_ERROR_RATE_THRESHOLD` | `0.5` | 上游错误率阈值：统计窗内失败占比达到该值且样本足够时告警；`0` 关闭 |
| `ALERT_ERROR_RATE_MIN_REQUESTS` | `20` | 错误率最小样本数：窗内请求数少于该值不判 |
| `ALERT_ERROR_RATE_WINDOW_MINUTES` | `15` | 错误率统计窗（分钟），数据源为 `usage_events` 明细 |
| `BACKUP_INTERVAL_MINUTES` | `1440` | 数据库备份后台任务周期（分钟），下限 60；`VACUUM INTO` 一致性快照落 `DATA_DIR/backups/` |
| `BACKUP_KEEP` | `7` | 备份保留份数：只留最近这么多份，更旧的按时间删除；`0` 表示不清理 |
| `QUOTA_EXPIRY_WINDOW_SECONDS` | `129600` | 主到期排序窗口：把距到期 ≤ 该秒数的积分加总，多的账号先用；CodeBuddy 与 TRAE 都按包独立到期、都参与；`≤0` 关闭整套到期排序 |
| `QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS` | `604800` | 次到期排序窗口：主窗口打平时才比较；`≤0` 关闭该级 |
| `CONVERSATION_STICKY_SECONDS` | `3600` | 会话粘性 TTL：优先按请求体显式会话标识，无则回落消息前缀指纹；手动 pin 优先；带 `user_id` 时不派生前缀兜底键；`≤0` 关闭 |
| `MODEL_BLOCKLIST` | `custom_model_*,*sub*agent*,summary,browser_use_*,file_search_agent,default,hunyuan-image-*,hy4-preview-x,qwen3.8-flash,glyph-cluster` | 模型列表黑名单（fnmatch，仅影响列表展示）；按**归一后的对外写法**匹配（原代号、归一键、展示名、展示名归一键四种任一命中即滤） |
| `MODEL_FALLBACK_GROUPS` | `""` | 跨渠道 fallback 兼容组：格式 `组名=成员1,成员2;组名2=成员3`；成员可带 `@渠道`。请求任一成员时该成员置首、组内其余依次回退；主渠道全不可用才回退，**仅在本次尝试尚未产出任何响应帧前**切换。留空 = 不启用 |
| `ALLOWED_HOSTS` | 空 | Host 白名单，防 DNS rebinding |
| `ALLOW_TEST_HOST` | `false` | 额外放行 FastAPI TestClient 的默认 Host `testserver`。**仅测试用**，生产保持 `false` |
| `TRUST_PROXY` | `false` | 是否采信 `X-Forwarded-For` 判定来源 IP（白名单、登录限流与审计共用）。仅「本服务前恰好一层受信反代」时开启 |
| `ENABLE_DOCS` | `false` | 是否暴露 `/docs` 与 `/openapi.json`（默认关闭） |
| `DUMP_REQUEST_BODIES` | `false` | 诊断：把 `/v1` 原始请求体落盘到 `data/dumps/`（**含对话内容**，仅排查用） |
| `LOG_LEVEL` | `INFO` | 日志级别；审计日志是 INFO 级，调到 `WARNING` 会一并关掉 |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 监听地址与端口（compose 默认 `0.0.0.0`） |
| `CODEBUDDY_API_ENDPOINT` | `https://copilot.tencent.com` | CodeBuddy 上游地址；改动时必须同时加入 `CODEBUDDY_ALLOWED_ENDPOINTS` |
| `CODEBUDDY_ALLOWED_ENDPOINTS` | 官方两站（见 compose） | 上游端点白名单，真实 Token 只发往白名单内地址 |
| `CODEBUDDY_INTL_API_ENDPOINT` | `https://www.codebuddy.ai` | CodeBuddy 国际版上游地址；改动时必须同时加入 `CODEBUDDY_INTL_ALLOWED_ENDPOINTS` |
| `CODEBUDDY_INTL_ALLOWED_ENDPOINTS` | `https://www.codebuddy.ai` | 国际版白名单，**刻意不含**国内站主机（跨站发送即凭证泄露，填错启动失败） |
| `ZEN_API_ENDPOINT` | `https://opencode.ai` | OpenCode Zen 上游地址；改动时必须同时加入 `ZEN_ALLOWED_ENDPOINTS` |
| `ZEN_ALLOWED_ENDPOINTS` | `https://opencode.ai` | Zen 端点白名单 |
| `ZEN_OPENCODE_VERSION` | `1.18.0` | 门禁伪装用的 `opencode/<version>` UA 版本（低于阈值会被 426 拒绝） |
| `KILO_API_ENDPOINT` | `https://api.kilo.ai/api/gateway` | Kilo Gateway 上游地址；改动时必须同时加入 `KILO_ALLOWED_ENDPOINTS` |
| `KILO_ALLOWED_ENDPOINTS` | `https://api.kilo.ai/api/gateway` | Kilo 端点白名单 |
| `QODER_API_ENDPOINT` | `https://openapi.qoder.com.cn` | Qoder openapi 地址（额度/登录）；国际版设为 `https://openapi.qoder.sh` |
| `QODER_GATEWAY_ENDPOINT` | `https://gateway.qoder.com.cn` | Qoder 推理网关地址（聊天/模型发现）；国际版设为 `https://api1.qoder.sh` |
| `QODER_ALLOWED_ENDPOINTS` | 国内 openapi+gateway + 国际版（见 compose） | Qoder 端点白名单，带 COSY 签名的请求只发往白名单内地址 |
| `CODEARTS_API_ENDPOINT` | `https://snap-access.cn-north-4.myhuaweicloud.com` | CodeArts snap 引擎地址；改动时必须同时加入 `CODEARTS_ALLOWED_ENDPOINTS` |
| `CODEARTS_ALLOWED_ENDPOINTS` | snap 引擎 + STS + 福利网关 + 门户（见 compose） | CodeArts 端点白名单，AK/SK 签名请求只发往白名单内地址 |
| `PROVIDER_PROXIES` | `""` | **按渠道出站代理**（启动期项，改后需重启）；留空 = 全部直连 |
| `CODEBUDDY_CHAT_MIN_INTERVAL` | `5` | CB 聊天节流器最小间隔（秒）：按凭证分桶、**桶内允许并发**，只在同凭证顺序连发时补足间隔；`0` 关闭 |
| `CODEBUDDY_INTL_CHAT_MIN_INTERVAL` | `5` | CB 国际版聊天最小间隔（秒），**独立**节流器；`0` 关闭 |
| `ZEN_CHAT_MIN_INTERVAL` | `0` | Zen 聊天最小间隔（秒），独立节流器，默认关闭 |
| `KILO_CHAT_MIN_INTERVAL` | `0` | Kilo 聊天最小间隔（秒），独立节流器，默认关闭 |
| `QODER_CHAT_MIN_INTERVAL` | `5` | Qoder 聊天最小间隔（秒），独立节流器；`0` 关闭 |
| `CODEARTS_CHAT_MIN_INTERVAL` | `5` | CodeArts 聊天最小间隔（秒），独立节流器；`0` 关闭 |
| `CODEARTS_MAX_CONCURRENCY` | `3` | CodeArts 每账号**在途并发上限**：上升沿满额时请求挂起直到有请求结束；`0` 关闭 |
| `CODEARTS_REQUEST_WINDOW_SECONDS` | `60` | CodeArts **账号滑动窗口**（秒）：与上一项组合成「每账号每 60s 最多启动 3 次」；`0` 关闭窗口口径 |
| `CODEBUDDY_SANITIZE_CHANNEL_MARKERS` | `true` | 出站 `system`/`assistant` 正文命中「伪装其他厂商官方客户端」指纹串时替换为占位符（上游 11128 内容风控），只改出站副本；`false` 关闭 |
| `REFRESH_SKEW_HOURS` | `24` | token 到期前该小时数窗口内预刷新。短寿命渠道自行封顶（CodeArts STS 只有 2h，封顶 45min） |
| `TOKEN_EXPIRY_WARNING_SECONDS` | `3600` | 管理台 token 到期预警阈值；`≤0` 关闭预警。纯展示，不参与调度 |
| `PACER_MIN_SECONDS` / `PACER_MAX_SECONDS` | `5` / `20` | 全局节流器随机等待区间（秒） |
| `AUTO_CONTINUE_MAX` | `10` | 上游以 `finish_reason=length` 截断时同凭证自动续写的最多次数；`0` 关闭 |
| `CONTEXT_COMPRESS_ENABLED` | `true` | 按模型目录里的输入上限裁剪过长对话，避免撞上游硬限制；目录里查不到上限的模型不受影响 |
| `CONTEXT_COMPRESS_RESERVE_TOKENS` | `4096` | 压缩预算里为模型回复预留的输出 token 数 |
| `CONTEXT_COMPRESS_MIN_KEEP_MESSAGES` | `4` | 无论多长都保留的最近消息条数 |
| `CONTEXT_COMPRESS_SAFETY_RATIO` | `0.95` | 按模型上限的百分比计算压缩预算，给 token 估算误差留余量 |
| `UPSTREAM_COMPLETE_TIMEOUT_SECONDS` | `600` | 非流式聚合整体超时（秒）：超时按瞬态错误换号重试；`≤0` 关闭 |

### 管理台热更（「任务与配置」页）

上表中带「可热更」语义的 **41 项**可不改 `.env`、不重启，直接在管理台「任务与配置」页修改：

`DEFAULT_MODEL`、`MODEL_BLOCKLIST`、`CONTEXT_COMPRESS_ENABLED`、`CONTEXT_COMPRESS_RESERVE_TOKENS`、`CONTEXT_COMPRESS_MIN_KEEP_MESSAGES`、`CONTEXT_COMPRESS_SAFETY_RATIO`、`MODEL_FALLBACK_GROUPS`、`QUOTA_EXPIRY_WINDOW_SECONDS`、`QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS`、`CONVERSATION_STICKY_SECONDS`、`GROWTH_IRREVERSIBLE_ACTIONS`、`GROWTH_INTERVAL_MINUTES`、`REFRESH_INTERVAL_MINUTES`、`QUOTA_PROBE_MINUTES`、`MODEL_CATALOG_MINUTES`、`OPENROUTER_CATALOG_MINUTES`、`USD_CNY_RATE`、`CODEBUDDY_CHAT_MIN_INTERVAL`、`CODEBUDDY_INTL_CHAT_MIN_INTERVAL`、`ZEN_CHAT_MIN_INTERVAL`、`KILO_CHAT_MIN_INTERVAL`、`QODER_CHAT_MIN_INTERVAL`、`CODEARTS_CHAT_MIN_INTERVAL`、`CODEARTS_MAX_CONCURRENCY`、`CODEARTS_REQUEST_WINDOW_SECONDS`、`PACER_MIN_SECONDS`、`PACER_MAX_SECONDS`、`ACTIVITY_REPORT_ENABLED`、`ACTIVITY_REPORT_HOUR`、`ALERT_ENABLED`、`ALERT_WEBHOOK_URL`、`ALERT_INTERVAL_MINUTES`、`ALERT_SILENCE_MINUTES`、`ALERT_FOR_TICKS`、`ALERT_CLEAR_TICKS`、`ALERT_POOL_READY_MIN`、`ALERT_TASK_FAILURES`、`ALERT_TOKEN_EXPIRY_HOURS`、`ALERT_ERROR_RATE_THRESHOLD`、`ALERT_ERROR_RATE_MIN_REQUESTS`、`ALERT_ERROR_RATE_WINDOW_MINUTES`、`BACKUP_INTERVAL_MINUTES`、`BACKUP_KEEP`。

要点：**优先级 `DB 覆盖值 > .env`**——改过后 .env 对该项不再生效，页面标「DB 覆盖」，点「恢复默认」删掉覆盖行才回落 .env。值存 `runtime_settings` 表（纯 key/value），新增可热更项无需迁移；白名单外的 key、非法类型 / 越界值写入前即拒。启动期项（`APP_SECRET` / `PORT` / `DATA_DIR` / `USERS_FILE` / `OPENROUTER_MODELS_URL` / 上游端点白名单）**不在**白名单。接口 `GET /api/settings` 读快照、`PUT /api/settings` 写（admin + CSRF），传 `null` 恢复默认。该页同时展示**后台任务运行态**，页面按 tab 组织；运行态是**进程内**的（`GET /api/tasks`，admin，页面每 30 秒刷新），重启归零。

## 部署注意

- **挂载目录属主**：容器内以 uid 1001（`appuser`）运行，`./data` 与 `./secrets` 必须可写/可读，否则 SQLite 打不开：

  ```bash
  mkdir -p data secrets && sudo chown -R 1001:1001 data secrets
  ```

- **时区**：镜像默认 `TZ=Asia/Shanghai`；如需其它时区显式覆盖 `TZ`。

### 升级后必须重启后端

**改动 `src/` 后必须重启进程，否则出现「新前端 + 旧后端」错配。** 典型症状：管理台页面能打开（前端产物本就是静态文件），但调用新端点全部失败——旧后端没有该路由，未匹配的 `/api/*` 按约定返回 JSON `404`，前端当成通用失败，弹出与真实原因无关的提示。前端不需重启（后端每次请求现读 `web/dist`），**后端不是**——进程管理器只在进程**退出**时重新拉起，不监听源码变化。

```bash
docker compose up -d --force-recreate   # Docker / compose
sudo systemctl restart coding2api       # systemd
```

**确认升级已生效**（先查版本，再查路由）：

```bash
# 1) schema 版本已迁移（期望 19）且账号已导入
sqlite3 data/coding2api.sqlite3 "PRAGMA user_version; SELECT username, role, enabled FROM users;"
# 2) 路由存在：期望 401（未登录），404 = 旧后端进程
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/users
```

### 前端产物与后端在同一端口

后端会把前端产物放在 `/` 直接服务（`src/webapp/static.py` 每次请求现读磁盘），因此**只改前端时构建完刷新即可，无需重启后端**；`localhost:5173` 是 Vite 开发态（HMR），两者可并存。**这条只对前端成立**——改了 `src/` 下后端代码必须重启进程。

`scripts/build-web.sh` 是构建入口，会处理「依赖未装」与「node 不在 PATH」两类常见情况：

```bash
./scripts/build-web.sh            # 仅在产物过期时构建
./scripts/build-web.sh --force    # 无条件重建
```

> **部署相关操作记在本地文档**：当前开发机的进程管理方式、路径与工具版本等见 `docs/local-environment.md`。该目录已在 `.gitignore` 中，**克隆仓库的人不会有这个文件**。

## 日志

应用只写 **stdout / stderr**，不写文件、不自行轮转：部署形态与采集方式各异，都靠这两个 fd 对接，轮转交给平台工具。因此**日志不会自行停止增长**——按下面对应形态配一次即可。

| 部署形态 | 日志到哪 | 轮转机制 | 需做什么 |
|---|---|---|---|
| **Docker / compose** | `docker logs coding2api` | json-file 驱动（compose 配好 `10m × 5`） | **无需操作** |
| **Linux（systemd）** | `journalctl -u coding2api` | journald 自带 | **无需操作**（模板见 `deploy/systemd/`） |
| **Linux（非 systemd）** | 重定向到 `/var/log/coding2api/*.log` | `logrotate` | `sudo cp deploy/logrotate/coding2api /etc/logrotate.d/` |
| **裸跑** | 终端 stderr | 无 | 自己重定向并自备轮转 |

级别用 `LOG_LEVEL`（默认 `INFO`）。**审计日志是 INFO 级**，调到 `WARNING` 会把它一并关掉。

> 容器里的 `data/dumps/`（诊断开关 `DUMP_REQUEST_BODIES=true` 写入）不在 docker 日志体系内，由应用自行保持最多 200 份。

## 开发

```bash
# 后端：lint + 测试（行/分支覆盖门槛 100%）
uv run ruff check src tests scripts
uv run pytest -q --cov=src --cov-report=term --cov-fail-under=100

# 前端
cd web
pnpm exec tsc --noEmit
pnpm exec vitest run
pnpm build
```

## 文档

| 文档 | 内容 |
|---|---|
| [`PROPOSAL.md`](PROPOSAL.md) | 目标与非目标、上游可行性核实、架构、数据模型、安全边界 |
| [`TECHNICAL.md`](TECHNICAL.md) | 技术栈、模块规格、Provider 协议、请求时序、测试策略 |
| [`diagrams/coding2api-architecture.html`](diagrams/coding2api-architecture.html) | 系统架构图（浏览器打开） |
| [`diagrams/coding2api-request-sequence.html`](diagrams/coding2api-request-sequence.html) | 请求主链路时序图（浏览器打开） |
| [`diagrams/coding2api-credential-lifecycle.html`](diagrams/coding2api-credential-lifecycle.html) | 凭证调度状态机（浏览器打开） |

## 授权协议

MIT，见 [LICENSE](LICENSE)。借鉴的上游项目署名见 [NOTICE](NOTICE)。
