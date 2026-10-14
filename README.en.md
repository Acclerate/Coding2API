# Coding2API

Unified OpenAI-compatible gateway for the **CodeBuddy** (CN site + international site),
**TRAE SOLO**, **OpenCode Zen**, **Kilo Gateway**, **Qoder**, and **CodeArts** coding-agent
upstreams, with a shared credential pool, unified scheduling, and per-user usage stats.

> [!WARNING]
> This project bridges reverse-engineered third-party endpoints; study and research use
> only. Not security-audited. Do not expose it to the public internet without a reverse
> proxy, authentication, and an IP allowlist.

## Features

- **OpenAI-compatible surface**: `/v1/chat/completions` (streaming + non-streaming), `/v1/responses` (for the Codex CLI), `/v1/messages` (Anthropic Messages / Claude Code), `/v1/models`, `/v1/user/balance` (DeepSeek-compatible).
- **Unified scheduling**: bare model names auto-route by health, `model@provider` pins an upstream. When free channels are candidates, **free wins**; then three-state health with tiered cooldowns, ties broken by **largest balance first**. A model-scoped limit sidelines only that model (the same account's other models stay available); a model held by a single cooling channel returns "model temporarily unavailable" instead of fanning out. A multi-turn conversation keeps one credential and rotates only on error.
- **Expiry-aware burn**: credits expiring within the main window (default 36h) burn largest-first (near-expiry quota not wasted), ties fall to a 7-day window. Units unify to credits.
- **Credential-gated model list**: only channels with a currently usable credential are fetched and shown, so a never-connected channel never shows phantom models; pausing or losing credentials hides a channel's models until it recovers.
- **Shared credential pool**: admins maintain credentials, everyone shares them, encrypted at rest (`APP_SECRET`); device-code login, account switching, quota probing, daily check-in, token pre-refresh.
- **Three-role accounts**: `admin` / `operator` / `viewer` in SQLite; one-time activation links (no shared initial secret), forced password change on first login, changing a role or disabling an account revokes its sessions immediately; logins and writes are audited.
- **Growth center** (CodeBuddy only): travel gifts, Buddy dispatch, task rewards, streak repair, streak redemption, blind boxes; irreversible steps off via `GROWTH_IRREVERSIBLE_ACTIONS=false`; runnable from the admin UI.
- **Privacy-preserving stats**: no conversation content stored; 90-day request detail, permanent hourly rollups; per user / channel / model / credential views; cost estimated from OpenRouter list prices (CNY primary).
- **Admin security**: login rate limiting, CSRF checks, body limits, Host allowlist.

Protocol-level implementation (private envelopes, signing, gate spoofing) is in [`TECHNICAL.md`](TECHNICAL.md) (Chinese).

## Quick start

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 24+ and pnpm 10+ (frontend build only).

```bash
uv sync
# Create the first admin (add the rest from the "Users" page later)
uv run python scripts/create_user.py admin --role admin
cd web && pnpm install && pnpm build && cd ..
APP_SECRET="pick-a-random-string" \
  uv run python -m uvicorn src.main:build_app --factory --port 8000
```

Open <http://127.0.0.1:8000> and sign in.

> `scripts/create_user.py` writes directly to SQLite (`--db`, or `DATA_DIR`; default `data/coding2api.sqlite3`). On startup, an existing `secrets/users.txt` is imported once (existing usernames are never overwritten).

### Docker

```bash
cat > .env <<'EOF'
APP_SECRET=pick-a-random-string
PUBLIC_BASE_URL=http://127.0.0.1:8000
EOF
docker compose build
docker compose up -d
docker compose exec coding2api python scripts/create_user.py admin --role admin
```

Pull the GHCR image instead of building:

```bash
docker compose pull
# or pin a version: docker pull ghcr.io/robbsluo/coding2api:v0.4.3
```

Pushing a `v*` tag to main triggers the publish workflow (`.github/workflows/publish.yml`), tagging both `<tag>` and `:latest` on GHCR.

## Users and roles

Accounts live in SQLite's `users` table; the *Users* page (admins only) handles day-to-day management.

| Role | Can do |
|---|---|
| `admin` | User management, tasks & settings, credential read/write, all stats, audit log |
| `operator` | Credential read/write (import / delete / pause / check-in / growth / account switch), all stats |
| `viewer` | Read-only: dashboard, credential list, stats, Playground |

**Adding users**: an admin sets a username + role and gets a **one-time activation link** (valid 24h, shown once). The user opens `/activate` and sets their own password — no "admin knows an initial password" secret exists. "Reset password" issues a new link.

**Role change / disable**: effective immediately — all issued session cookies for that account are invalidated (re-login required). Disabling is the default way to "remove" someone (reversible, usage still attributed to the username); **hard delete is CLI-only** (it would leave orphan `usage_events` rows).

**Self-service password change**: user menu → *Change password*, requires the current password.

**Lockout guard**: you cannot disable or demote the **last active admin**, nor demote or disable yourself. Add another `admin` first.

**CLI fallback**:

```bash
python3 scripts/create_user.py alice --role admin   # interactive password
python3 scripts/create_user.py --list
python3 scripts/create_user.py alice --delete --force   # hard delete, irreversible
echo "$PASSWORD" | python3 scripts/create_user.py alice --role admin --password-stdin  # password off argv / shell history
```

`ADMIN_USERNAMES` and `secrets/users.txt` are **bootstrap-only**: on startup, `users.txt` is imported into the DB and the named users are promoted to `admin`; afterwards roles come from the DB alone.

## Audit log

The *Audit log* page (admins only) records logins and writes, filterable by actor/action and paginated: **logins** (success and failure), **account changes** (create, activate, role change, enable/disable, password change/reset, hard delete) and **credential writes** (import, delete, pause, pin, revive, account switch). Each row has time, actor, action, target, detail and source IP. **No passwords or token plaintext** (the one-time activation token appears once in the create response; only its SHA-256 digest is stored). Retention matches request detail (90 days), trimmed by the background cleanup task.

## Usage

1. **Credentials** page — add credentials:
   - **CodeBuddy** (incl. international): device-code login, or paste `{"token":"..."}`.
   - **TRAE**: paste the credential JSON (`accessToken`/`uid`/`refreshToken`) or the callback URL (`apiHost` must be an official address).
   - **Qoder / CodeArts**: device-code / OAuth2 authorization-code login (CodeArts: paste the unreachable local callback URL back into the page).
   - **OpenCode Zen / Kilo Gateway**: nothing to add — a virtual credential is seeded at startup (re-added from the same panel if deleted).
2. **API keys** — create an `sk-...` (plaintext shown once).
3. Call it:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"hello"}]}'
```

`model@provider` pins an upstream (`glm-5.2@trae`, `mimo-v2.5-free@zen`); bare model names route automatically. Any OpenAI-compatible client can connect (Base URL `http://127.0.0.1:8000/v1`, key `sk-...`, model names per `GET /v1/models`); the *Playground* page tests with the session login, no API key.

### Three model-name fields

Every model on the seven channels is unified into three fields:

1. **Raw id** (`raw_id`): the key actually sent upstream, **never changed**. Raw ids differ per channel (Qoder `kmodel_latest` = TRAE `kimi-k3-1`) and may carry a namespace prefix (Kilo's `kilo-auto/free`).
2. **Canonical key**: free-tier markers (`-free` / path segment `free`) and namespace prefixes stripped, then slugified — `kilo-auto`, `longcat-2.5-preview`. The list merges entries by it, so Zen's free tier and TRAE's paid tier collapse into one row.
3. **Display name** (`name`): cleaned readable text, from upstream when given (`Qwen3.8-Max` → `Qwen3.8 Max`), otherwise derived; brand casing normalized (`DeepSeek` / `MiMo` / `GLM` / `GPT`).

Every `id` in `GET /v1/models` is the canonical key; requests are translated back to the channel's raw id (`by_provider.{channel}.raw_id`). **All three spellings are directly requestable** (`kmodel_latest`, `Kimi K3`, `kimi-k3` all resolve to the same model).

### Streaming usage and reasoning tokens

- **Streaming usage frame**: after the finish chunk and before `data: [DONE]`, a `choices: []` usage frame (standard OpenAI shape) lets stream-reading clients show tok/s, context occupancy and session totals; nothing is sent when the upstream reported no usage (never a fake `0`), and unreported fields are `null`.
- **Reasoning tokens need `reasoning_effort`**: CodeBuddy defaults to `medium` when the client omits it (the upstream would otherwise write the whole reasoning pass into the visible answer); explicit values pass through untouched.

### Playground advanced parameters

The Playground request form carries optional `temperature` / `max_tokens` / `reasoning_effort` inputs; **leaving one empty omits the key entirely** (each channel then applies its own default). Client-side validation matches `compat/openai/request.py` (`temperature` up to 1000, `max_tokens` a positive integer up to 4,000,000) so nothing passes the UI only to be rejected as a 400. The `reasoning_effort` control appears only when the model metadata reports `supports_reasoning` or a `default_effort`, offers the standard levels (`low`/`medium`/`high`, plus the upstream-declared default if it is outside that set), and starts at "not sent" — the upstream decides whether a given level is acceptable.

## Channels

> CodeBuddy (incl. international) / TRAE / Qoder / CodeArts need credentials; **OpenCode Zen and Kilo Gateway are credential-less free tiers** — the pool holds one virtual placeholder row (so scheduling / cooldown / stats work as usual), the quota column reads "free tier (no quota API)", and health stays "no probe" (unknown ≠ exhausted). The virtual row **re-seeds on restart after deletion**; use *pause* to disable permanently.

### OpenCode Zen free tier (`zen`)

Free models at [opencode.ai/zen](https://opencode.ai), no account or login. The upstream list mixes unmarked paid models, so the gateway narrows by the `-free` suffix and probes each candidate, exposing **only free models that answer anonymously** (liveness cached 30 min, no static allowlist; free models marked **x0**). The upstream list has no display names, so names are derived from the id.

**Free-tier gate**: the upstream wants to believe it is talking to the official client (UA version, session header, `stream:true`, `tools` with `bash`/`read`). The gateway satisfies this; the injected `bash`/`read` are empty shells and their calls are filtered out, so you never see functions you did not declare. UA version via `ZEN_OPENCODE_VERSION`, endpoint via `ZEN_API_ENDPOINT` (must be inside `ZEN_ALLOWED_ENDPOINTS`). Paid models return "unavailable" even when pinned.

### Kilo Gateway free tier (`kilo`)

Free models at [Kilo Gateway](https://kilo.ai) (`api.kilo.ai/api/gateway`), no account or login. Standard OpenAI protocol, no private envelope or gate spoofing; the upstream `/models` carries an authoritative `isFree` flag, so the gateway filters on it directly and **does not probe** (probing burns the tiny quota). Free models marked **x0**.

**Quota and limits**: the free tier is small (gateway-wide ~200 requests/hour/IP) and is a forward of OpenRouter's free pool; upstream **429 and 502/503/504 are model-scoped** — only the named model cools, and the gateway adds no circuit breaker. Endpoint via `KILO_API_ENDPOINT` (must be inside `KILO_ALLOWED_ENDPOINTS`).

### Qoder (`qoder`, real account)

[Qoder](https://qoder.com), **real account** required: click *Log in to Qoder*, complete device-code authorization in the browser (the backend polls; the token never touches the browser). The upstream speaks a private COSY protocol (custom Base64 + envelope SSE), signed and unwrapped by the gateway; the surface stays standard OpenAI.

**Quota and check-in**: quota probing (`GET /api/v2/quota/usage`) and campaign-based daily check-in. Check-in uses campaigns (`/sash/api/v1/me/campaigns`, needs `Cosy-ClientType`) with the old `daily-check-in` as fallback; claims run automatically and can be triggered from the admin UI. Campaigns **refresh daily at 10:00 (UTC+8)** (valid 30 days after claiming) and the task seals per that window, so an "already claimed" just after midnight seals only until 09:59 and the new campaign is claimed at 10:00. No streak count is shown. **The international site has no check-in** (404), reported as "no such endpoint in this region", not an error.

**Endpoints and pacing**: CN site by default (`openapi.qoder.com.cn` + `gateway.qoder.com.cn`); international via `QODER_API_ENDPOINT=https://openapi.qoder.sh` with the whitelist extended (signing carries full `cosy-*` headers, so the whitelist prevents misdelivery). Default `QODER_CHAT_MIN_INTERVAL=5` (own pacer), hot-updatable.

**Upstream node failures**: Qoder sometimes wraps its own inference-node failures in a 400 (`[FAIL]node:… msg:Execution failed`). The gateway treats these as a **model-scoped transient fault** — only that model cools, and an exhausted candidate set returns a "model temporarily unavailable" 503 (`no_healthy_credential`), not a missing-model error.

### CodeArts (`codearts`, real account)

[Huawei Cloud CodeArts](https://codearts.huaweicloud.com), **real account** required: *Log in to CodeArts*, complete OAuth2 authorization, then paste the **unreachable local URL** (`http://127.0.0.1:12800/oauth/callback?code=…`) back into the page; the server exchanges it for STS credentials (AK/SK + security_token + refresh_token + DPoP key) and stores them encrypted.

**Token refresh is rigid**: refresh_token, `client_id=codearts-agent` and the DPoP key are bound together and **single-use** — the new refresh_token must be written back or the account breaks; the pre-refresh task does this automatically (DPoP ES256/P-256). The pre-refresh window is capped at 45 minutes and must be **wider** than the polling interval (`REFRESH_INTERVAL_MINUTES`, default 30 min). A revoked renewal marks the credential "re-login required" and stops automatic retries until restored.

**Daily check-in**: a "1000 credits/day" campaign (`GET /v1/ops/delivery` → `POST /v1/ops/claim` → `POST /v1/ops/confirm`). The task claims automatically and can be triggered from the admin UI; `claim` alone is **not** enough (`CLAIMED` is a "claimed but unconfirmed" state), so both steps always run. This credit ledger is **separate from** the daily token pool: the pool feeds benefit models (`deepseek-v4.1-flash`), the credits feed built-in models (GLM-5.2 / OpenPangu).

**Burn first**: the daily pool does not roll over, so the day's remainder is registered as midnight-expiring quota and scheduled **before** other channels. The upstream meters tokens, normalized to **credits** on the admin UI (1 credit = 10,000 tokens; a full daily pool = 1000 credits); pre-upgrade history is converted by `scripts/convert_codearts_credit_unit.py`. **This "credit" is a synthetic unit for cross-channel ordering, not the upstream check-in/plan credits.**

**Signing / cumulative SSE / pacing**: the upstream requires Huawei `SDK-HMAC-SHA256` (AK/SK + `X-Security-Token`), and `CODEARTS_ALLOWED_ENDPOINTS` must include the snap engine, STS, benefit gateway and portal hosts. Streaming `text` is **cumulative (replace semantics)**, reduced to incremental events for clients. Default `CODEARTS_CHAT_MIN_INTERVAL=5`; the upstream hard-limits **3 concurrent sessions per account**, so an in-flight cap (`CODEARTS_MAX_CONCURRENCY`) and a sliding window (`CODEARTS_REQUEST_WINDOW_SECONDS`, 3 starts per 60s) keep bursts ahead of `400 TM.00001041`. All hot-updatable.

### CodeBuddy international site (`codebuddy_intl`, real account)

The international site [www.codebuddy.ai](https://www.codebuddy.ai), **same protocol** as the CN site (same `/v2/chat/completions`, `/v3/config`, `/v2/plugin/auth/*`, `/v2/billing/*` routes, same Keycloak realm) but a **separate account system** (separate signup, billing) — hence a **separate channel**. Login is identical to the CN site (device code, token never in the browser).

**Not a site switch**: `CODEBUDDY_API_ENDPOINT` is process-wide, so flipping it would send every CN-site credential to the international site (quota mismatch / 403). Both sites can hold credentials, each with its own endpoint, pacer and login flow.

**Whitelists deliberately exclude each other**: `CODEBUDDY_INTL_ALLOWED_ENDPOINTS` holds only `https://www.codebuddy.ai`; the two sites' tokens are issued by different gateways, so a cross-site send is credential leakage and pointing the international endpoint at the CN host fails **at startup**.

**Feature split**: chat, model list, quota probe, token refresh and **activity reporting** are enabled; **daily check-in and the growth center are off** — check-in simply **doesn't exist on that site** (`checkin-activity-status` returns `active: false`), and the growth center **cannot be verified** (the international gateway puts the whole `/v2/activity/*` prefix behind its auth plugin, answering 401 rather than 404) while performing irreversible actions. Both menu entries are hidden for this channel. Activity reporting posts to `/v2/report`, a different path, verified working, so it stays enabled.

**No channel suffix on shared model names**: models on both sites are listed **once** with both providers attached; genuinely different upstream models (`-x`, `-sg`) still get their own entry.

**Pacing**: `CODEBUDDY_INTL_CHAT_MIN_INTERVAL` defaults to 5s on its **own** pacer (sharing one would make the two sites queue behind each other), hot-updatable.

### Per-channel outbound proxy (optional)

`PROVIDER_PROXIES` gives each channel its own egress proxy:

```
PROVIDER_PROXIES="codebuddy=http://127.0.0.1:7890;qoder=socks5://127.0.0.1:1080"
```

Format `channel=url;channel2=url2`, schemes `http/https/socks5/socks5h`, empty = all direct. It covers **all** of that channel's outbound requests (chat streaming, quota/model fetches, background tasks and OAuth login). **Startup-only** (restart required). Parsing is strict: an unknown channel, unsupported scheme or missing `=` fails startup — "thought it used a proxy but went direct" is worse than a startup error. Environment proxies (`HTTP_PROXY`, …) are **never honoured** (`trust_env=False`), so a deployment-wide proxy cannot silently hijack token-bearing requests.

## Outbound protocols

### Responses API (Codex CLI)

`POST /v1/responses` serves a Responses subset for clients that only speak the Responses API, such as the [Codex CLI](https://github.com/openai/codex). It shares the same credential selection, cooldown, rotation, accounting and session affinity as `/v1/chat/completions`; only the inbound mapping and outbound translation differ:

```bash
export CODING2API_KEY=sk-your-key
codex -c "model_providers.coding2api={ name='coding2api', base_url='http://127.0.0.1:8000/v1', wire_api='responses', env_key='CODING2API_KEY' }" \
      -c model_provider=coding2api \
      -c model='glm-5.2' \
      'your task'
```

Supports streaming text, reasoning summaries, function tool calls and `finish_reason=length` → `response.incomplete`. `text.format` maps to chat's `response_format` (`text` / `json_object` pass through; `json_schema` is expanded). **Not supported**: `store=true`, `previous_response_id` (stateless server, not faked), Responses-only tools — explicit 400, never silently degraded. `include=["reasoning.encrypted_content"]` is accepted and ignored.

### Anthropic Messages API (Claude Code)

`POST /v1/messages` serves an Anthropic Messages subset for [Claude Code](https://docs.anthropic.com/en/docs/claude-code):

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_AUTH_TOKEN=sk-your-key     # or ANTHROPIC_API_KEY (sent via x-api-key)
claude
```

**Auth**: both `x-api-key` and `Authorization: Bearer` are accepted. **Streaming**: `message_start` → `content_block_start/delta/stop` → `message_delta` (with `stop_reason` and usage) → `message_stop`; no `[DONE]` sentinel, and thinking blocks get a `signature_delta` before `content_block_stop`. **Cache hit**: usage is split Anthropic-style — `input_tokens` counts only misses, hits are reported separately as `cache_read_input_tokens` (the internal `input_tokens` is OpenAI's `prompt_tokens`, which includes hits), so Claude Code shows cached tokens instead of always 0. **`count_tokens`**: `POST /v1/messages/count_tokens` estimates input tokens locally. **Not supported**: image/document blocks and Anthropic-only server tools — explicit 400.

### CC Switch one-click import

After creating a key, the API Keys page can generate [CC Switch](https://github.com/farion1231/cc-switch) `ccswitch://` import deep links (one button each for Claude Code / Codex CLI / OpenCode) that write the endpoint, key and model into the desktop client after a confirmation dialog. A deep link **only fires on a user click** (browsers block automatic redirects) and **carries the key in cleartext**, so it is only usable while the created key is still displayed. Protocol and `app` / `apiFormat` values: see CC Switch's "Deep Link Protocol" manual; requires CC Switch v4.0.10+. Note the **endpoint form depends on `apiFormat`**: the Anthropic format (Claude Code) takes the bare origin **without `/v1`**, the two OpenAI formats take `/v1`. This gateway has no Gemini egress, so no `app=gemini` entry is offered.

### Balance query

`GET /v1/user/balance` is DeepSeek-shaped, Bearer `sk-...`, for clients like Cherry Studio / cc-switch. Balances come from the credential pool's quota-probe cache (`QUOTA_PROBE_MINUTES`), aggregated over usable credentials, in upstream credits. `balance_known` is `false` when no credential has ever probed successfully (unknown, not 0).

### Pool health

`GET /health` is a pure liveness probe. `GET /healthz` adds credential-pool counts (`total` / `ready` / `cooling` / `paused` / `disabled`), so monitors can alert when the pool is exhausted (`ready=0`: alive but unusable). Both are unauthenticated; buckets are mutually exclusive and sum to `total`.

### Prometheus metrics (`GET /metrics`)

`GET /metrics` serves the Prometheus text format (`text/plain; version=0.0.4`) for direct scraping. On by default, **unauthenticated** (same stance as `/healthz`); `METRICS_ENABLED=false` (hot-updatable) makes the endpoint return 404.

Five metric families:

| Metric | Type | Labels | Source |
| --- | --- | --- | --- |
| `coding2api_requests_total` | counter | `provider` / `model` / `status`(ok\|error) | `usage_hourly` (permanent, monotonic across restarts) |
| `coding2api_tokens_total` | counter | `provider` / `type`(input\|output\|reasoning\|cached) | `usage_hourly` |
| `coding2api_credentials` | gauge | `state`(ready\|cooling\|paused\|disabled) | credential pool (same stance as `/healthz`) |
| `coding2api_upstream_errors_total` | counter | `provider` / `kind` | in-process counter (resets on restart) |
| `coding2api_task_failures_total` | counter | `task` | in-process task state (resets on restart) |

**No high-cardinality labels**: no `credential_id` / `api_key_id` / `username`. `requests_total` / `tokens_total` read the **permanent** hourly rollup (not the 90-day detail table), so scraping never scans details and counts stay monotonic; the in-process counters reset on restart, which Prometheus handles as a counter reset.

## API key policy

A key can be restricted by channel, model, source IP and expiry:

- **Channel binding**: pins the key to one channel; a model owned by another channel gets a 400 naming the real owner (no silent reroute). Empty = automatic. A `model@channel` conflicting with the binding is also a 400.
- **Source IP allowlist**: comma-separated IP/CIDR (empty = unrestricted), normalized on write; an off-list source gets 403. `X-Forwarded-For` is **ignored by default**; only with `TRUST_PROXY=true` is the *last* entry used (exactly one trusted reverse proxy).
- **Model allowlist**: comma-separated names or fnmatch globs (e.g. `glm-*,kimi-k3`), case-insensitive, `@channel` suffix excluded; out-of-list models get a 400 and `/v1/models` lists only allowlisted models.
- **Expiry**: epoch seconds; empty = never; an expired key gets a 401 (same copy as "key not found", so nothing is enumerable).

## Credential pause

The per-row *Pause* action removes a credential from **chat traffic only**: quota probing, token pre-refresh, daily check-in, growth center and activity reporting keep running (they honour only the system hard-disable `disabled`). Use it to "stop chatting but keep earning". *Resume* returns it immediately. Different from *Disabled* (the upstream rejected the session; needs re-login + *Restore*). Same toggle at `POST /api/credentials/{id}/toggle`.

> Zen / Kilo are credential-less: *pause* is the only way to disable them permanently (deletion re-seeds on restart). Pausing also hides the channel's models.

## Per-credential in-flight cap

`MAX_INFLIGHT_PER_CREDENTIAL` (default `0` = unlimited) stops a credential from being selected once its concurrent request count reaches the cap, keeping a single account from being hammered into upstream rate limits or bans; it is global across channels (orthogonal to the per-channel `CODEARTS_MAX_CONCURRENCY`).

- The counter is **in-process** (single-instance semantics, like the `/api/tasks` state).
- Leases release when the request ends (success, rotation, error, client disconnect), with a **600s TTL** reclaiming any that leak—better to occasionally admit one extra request than to silently retire a credential.
- When every candidate is at its cap, the response is the same "no healthy credential" 503 as the limits guard.
- Hot-updatable on the *Tasks & settings* page; the next request picks it up (no snapshot cache).

## Credential pool: filtering and grouping

Once the pool grows, the page splits "find that one account" into three layers, while the first screen shows only the channel-level overview:

- **Filter bar**: search (nickname / channel / id, case-insensitive) × channel × state × tag. The four are **AND**-combined and purely client-side — the backend still returns the full set ordered by `sort` / `order`. When any filter is active it shows "showing N / total" plus a *Clear* action; no match yields an empty state rather than a blank table. Counts in the channel / state / tag dropdowns come from the full set, so narrowing one filter never re-narrows the others. The **tag dropdown renders only when some credential actually carries a tag** (no permanent "all tags" control otherwise), and tags are a structured dimension that **does not participate in the search term** — otherwise finding a nickname that happens to be "prod" would be impossible.
- **Tags**: each credential can carry any number of ops tags (comma-separated, up to 16 tags of ≤ 24 chars; half-width and full-width commas and semicolons all separate), edited via the row's *More → Set tags*. Tags are **display/filter only** — they never affect selection and never change request behaviour, since using them as policy would make a rename silently change traffic. A row shows at most two tags, collapsing the rest into `+N` with the full list in the hover title.
- **Group sort**: a separate control reorders only the channel *groups* (never the rows inside them, which still follow the column header / backend `sort`). Default is **quota high→low**, ranked by the group's summed remaining quota so the channels that can still serve the most surface first; switchable to **channel order** (fixed, so 30s polling cannot make headers jump). Channels with no quota API (free tier) sort last rather than being treated as `0`, and ties fall back to channel order.
- **Grouped by channel**: rows aggregate per channel; each group header is a compact **channel dashboard** — brand icon + channel name + account count, plus a **state breakdown** (icon + count, listing only states that occur, using the same `credentialState` as the state badge), a **total quota** figure (a share ring plus the number; the ring is a pure gauge and the exact percentage sits on the "total quota" label line; sums the group's non-null values, and the free tier zen / kilo shows `—` rather than `0` with no ring), and an **expiring credits** figure (one block for the primary 1.5-day window; the secondary 7-day window appears only when it holds **more** than the primary — the 7-day window is a superset, so an equal number means nothing expires beyond 1.5 days; CodeArts' daily pool zeroes at midnight and the two windows coincide, so only one block shows rather than repeating the same number). Clicking a header collapses / expands that channel — **collapsed by default** (see structure first, expand to inspect accounts); an active filter auto-expands matching channels, and clearing it returns to the collapsed overview. Group order is "quota high→low" by default and switchable from the toolbar to the fixed site-wide channel order (CodeBuddy, TRAE first); it does not follow the sort key. The channel name appears only in the header, never repeated per row.
- **Column headers follow the expanded region**: there is no global table header; the column row (with sort buttons and column hints) renders inside each expanded channel group and sticks while the container scrolls. When collapsed, the group header *is* the overview and the column names have nothing to label; only an expanded group needs the "what is this column" anchor. Several groups can be expanded at once, and every group's header shares one sort state (clicking any of them writes the same backend `sort` parameter). The **"growth center" column appears only for channels that support it** (CodeBuddy); other channels get an empty placeholder cell — the column count stays constant, otherwise auto layout recomputes the widths and groups expanded below misalign.
- **Scrollable long list**: the table is capped at 70vh, and the group's sticky column header and row actions stay visible deep in the list.

The quota number is dotted-underlined: hovering reveals **that row's cycle semantics** (this-cycle remainder / account balance / daily pool / free tier with no quota API); the per-row caption lives in that hover hint.

## Model list loading

`GET /v1/models` and the Playground dropdown merge only channels with a **currently usable credential** (at least one not-paused, not-dead row). The check runs before the cache, so a never-connected channel never shows phantom models; pausing or losing credentials hides a channel's models until it recovers; a cold start lists only `zen` / `kilo` (their virtual credentials are seeded). A cooling credential still counts — it is only temporarily limited, so the list does not flicker.

**Ordering**: CodeBuddy, TRAE, Qoder and CodeArts models come first, dictionary-ordered within a group; multi-channel models take their highest-priority channel's slot. Display-only — it does not change scheduling.

**Routing honours this cache**: when the catalog proves which channels own a model, those are the only candidates — if all are unavailable (cooling / paused) the request gets a 503 instead of fanning out (each upstream would only answer 11102 / 4001 / 401 and write hours-long "no such model" negative caches). A model absent from the catalog still routes to all candidates; pinned `model@channel` and key channel binding are unaffected.

### Catalog snapshot and startup

Besides the in-process TTL cache (300s), every successful fetch is snapshotted to `DATA_DIR/model_catalog.json` and **read back synchronously at startup**, so the model→channel table is ready in the first second (flat model names no longer fan out while zen's 10+ second probes run). A corrupt or >7-day-old snapshot is just dropped. `MODEL_CATALOG_MINUTES` provides a background refresh so the catalog stays fresh even when nobody calls `/v1/models`.

Playground and `GET /v1/models` use **stale-while-revalidate**: with a cache they return the old list immediately and refresh in the background; only a channel with **no cache at all** is awaited synchronously. The list lags by at most one TTL.

### Admin *Model list* page

The admin *Model list* page (`GET /api/model-catalog`) is a read-only view of the public OpenRouter catalog: prices (input / output / cache read / cache write, USD per million tokens, switchable to CNY), context/output limits, input→output modalities, capabilities (reasoning / tool calling / attachments / structured output), knowledge cutoff, and the **Artificial Analysis indices** (intelligence / coding / agentic, via the public OpenRouter API — **not measured by this service**, never sorted). A missing catalog shows an empty state and does not affect chat or cost.

OpenRouter lists hundreds of models and most are not connected here. Each row carries an `available` boolean (whether the model is **currently served by a channel with a usable credential**, same rule as `/v1/models`), and the page offers an **off-by-default** "only current channels" toggle that narrows the table locally to models fetched from current channels (no extra request).

The 能力分 (capability score) header is sortable by the **intelligence index** (智); rows without a score sort last (never treated as 0). Only this one sub-score has a sort entry — one header column maps to one entry, and sorting all three would crowd the header into a row of buttons.

### Token expiry visibility

A "token remaining" column, red below `TOKEN_EXPIRY_WARNING_SECONDS` (default 1h). Expiry uses explicit `expires_at`, falling back to the JWT `exp`; both missing → `—`, **never guessed**.

## Stats

### Scope

The time range defaults to the **last 24 hours** (also 7 days / 30 days / all): the first question when opening the stats page is "how is the pool doing right now", and 7 days flattens out the day's variation.

### Credit, cost and cache hit rate

The **Credit** column: CodeBuddy is the real upstream value; TRAE's `token_usage` frame carries only tokens, so its credit is **estimated** from official unit prices (`≈`); CodeArts benefit models are estimated 1:1 against the daily pool (`≈`). Tables live in `src/provider/trae/pricing.py` and `src/provider/codearts/units.py`; unmatched models show `—`.

**Cost (est.)** estimates tokens × public list prices, not the real charge. Prices come from the **OpenRouter public catalog** (USD per million tokens), computed as `(input − cached) × input + cached × cache_read + output × output`, converted to CNY at the **rate in effect when the row was written** (`USD_CNY_RATE`). Costs are **fixed at write time** (`usage_events.cost_usd` / `cost_cny`) and **never recomputed**; pre-upgrade rows are `NULL` and can be backfilled with `scripts/backfill_cost.py --apply`. Matching compares canonical keys for equality and **drops ambiguous hits**; unmatched models or requests without input tokens contribute nothing and aggregates render `—` (never a fake `0`), so the figure is a **lower bound**.

**Cache hit rate**: the *Token usage* card adds cached ÷ input (1 decimal), from the upstream `cached_tokens`; `—` when never reported or input is 0.

### Credit record

The drawer under a credential's quota number lists the **net change between consecutive quota probes**, attributing nothing to check-in / growth / chat (the upstream logs nothing), so the UI says "net change". The first probe records only a baseline; unchanged balances are skipped; a balance going unknown still records a row. 90-day retention.

### Grouping and re-rollup

The **Grouped stats** panel tabs four dimensions (channel / model / user / credential) over one table (requests / ok / success rate / input / output / cached / credit / cost), endpoints `GET /api/stats/by-provider|by-model|by-user|by-credential`. Default sort is requests descending; **user and credential tabs are admin/operator only**. **Scope differs**: channel / model / user read the **hourly rollup** (permanent; the last 5 minutes are not yet counted); the **credential** dimension reads request detail (the rollup key has no `credential_id`), covering the **last 90 days**.

**Click-to-drill**: clicking any group row drills the **Request detail** panel below into that dimension (channel / model / credential filter by the same-named column; the user dimension goes through the scope parameter). Conditions stack across dimensions, the active filters show as chips above the panel, and one click clears them. `GET /api/stats/events` therefore accepts `provider` / `model` / `credential_id` filters (AND semantics).

**CSV export**: the **Export CSV** button (admin/operator only) exports request detail for the **current time range and drill filters**, via `GET /api/stats/events?format=csv`. The file carries a UTF-8 BOM (so Excel detects the encoding), `\r\n` line endings, ISO 8601 (UTC) timestamps, and prefixes a leading apostrophe on **text** cells starting with `= + - @` (or tab/CR) to block spreadsheet formula injection (numbers, negatives included, are left as-is). Export is capped at 10000 rows; beyond that it truncates and flags `X-Export-Truncated: 1` in the response headers, and the page tells the user to narrow the conditions.

If history looks light (pre-upgrade detail not covered, or detail backfilled without re-rolling), run `python3 scripts/rollup_hourly.py --apply` (preview by default, backs up before writing, idempotent). It reports gaps and warns about intervals whose detail has passed the 90-day retention and cannot be restored.

### List sorting

Admin lists are **sortable by clicking column headers**: the first click uses the column's natural direction (numbers descending, text ascending), subsequent clicks toggle. Sorting is done by the **backend** — every `GET` list endpoint accepts `sort` (whitelist key) and `order` (`asc` / `desc`). An unknown `sort` key or invalid `order` falls back to the endpoint's default (no error); `sort` is a whitelist key, never a SQL column, so the injection surface is zero.

## Background tasks

Nine tasks — quota probe, token pre-refresh, daily check-in, growth center, activity reporting, detail cleanup, channel catalog refresh, OpenRouter catalog refresh, and operations alerts — are scheduled by `TaskRunner`, failing independently. Each task's last run, latest result and error appear on the *Tasks & settings* page (30s refresh); task state is **in-process only** and resets on restart.

**Channel catalog refresh** (`MODEL_CATALOG_MINUTES`, default 30, floor 5) keeps the catalog fresh when nobody visits `/v1/models`; 30 minutes aligns with zen's 30-minute liveness cache, so going denser would only hit other channels' `/models` for nothing.

**Growth center** is CodeBuddy-only; Buddy travels return every 1–4 hours, so the default cycle is 60 minutes (`GROWTH_INTERVAL_MINUTES`). Lottery / streak redemption / blind boxes / streak-repair cards are **irreversible** and skipped when `GROWTH_IRREVERSIBLE_ACTIONS=false`.

### Activity reporting (optional, off by default)

The growth center's streak counts daily client chat events; an account driven only by the gateway would break the streak. With `ACTIVITY_REPORT_ENABLED=true`, a task posts **one** `chat_request_send` event per account per day at `ACTIVITY_REPORT_HOUR` (default 10:00) to keep the streak alive — unrelated to credits or scheduling, and failures do not affect chat. Applies to CodeBuddy and CodeBuddy International.

> ⚠️ **Risk**: the official terms forbid scripted tampering with activity data (disqualification + gift clawback). Off by default; evaluate the account risk first. Event names and request shapes depend on the upstream and **break without notice** — not a reliability feature. Measured (2026-09-21): without `userId` the upstream returns HTTP 200 `{"code":0}` and **silently drops** the event; when a credential's `user_id`/`account_uid` is empty, the gateway falls back to the bearer JWT `sub`, and skips if still missing (never fabricated).

## Configuration

Key variables below; the full list is in `src/config.py` (authoritative), and **every one is plumbed through `docker-compose.yml`**. In containers use `HOST`/`PORT` to change the listen address (`PORT` also sets the host mapping).

| Variable | Default | Notes |
|---|---|---|
| `APP_SECRET` | **required** | Credential encryption key, ≥16 chars; loss = all credentials void, no rotation. Low-entropy values are rejected at startup |
| `ADMIN_USERNAMES` | empty | **Bootstrap-only**: users promoted to `admin` on first start; inert once the DB has roles |
| `USERS_FILE` | `secrets/users.txt` | **Bootstrap-only**: legacy user file imported once (idempotent) |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | Browser-reachable address; TRAE login callback depends on it |
| `DATA_DIR` | `./data` | SQLite and runtime data directory |
| `DEFAULT_MODEL` | `glm-5.2` | Model when empty/`auto` |
| `QUOTA_PROBE_MINUTES` | `60` | Quota probe interval (floor 1) |
| `REFRESH_INTERVAL_MINUTES` | `30` | Token pre-refresh polling interval (floor 5); short-lived channels cap their window wider than this |
| `MODEL_CATALOG_MINUTES` | `30` | Channel catalog fallback refresh (floor 5) |
| `OPENROUTER_CATALOG_MINUTES` | `1440` | Catalog refresh (floor 60): one fetch from the OpenRouter public API covers prices, metadata and the Artificial Analysis capability indices |
| `OPENROUTER_MODELS_URL` | `https://openrouter.ai/api/v1/models` | Catalog source (anonymous, no key); restart required |
| `USD_CNY_RATE` | `6.70` | Cost-estimate FX rate; hot-updatable, affects only later writes |
| `GROWTH_INTERVAL_MINUTES` | `60` | Growth-center cycle (CodeBuddy only; floor 5) |
| `GROWTH_IRREVERSIBLE_ACTIONS` | `true` | Allow lottery, streak redemption, blind boxes, repair cards |
| `ACTIVITY_REPORT_ENABLED` | `false` | Post one chat event per account per day. **Off by default** — terms forbid tampering |
| `ACTIVITY_REPORT_HOUR` | `10` | Activity hour (local Beijing time) |
| `ALERT_ENABLED` | `true` | Operations-alert evaluation (pool exhausted / repeated task failures / token near expiry / error-rate spike), persisted to `alert_events` |
| `ALERT_WEBHOOK_URL` | empty | Webhook POST (JSON, body carries `status`: `firing` / `resolved`), comma-separated (≤16, `http(s)` only, private/loopback/link-local blocked against SSRF, re-checked against DNS resolution right before delivery and skipped if it fails to resolve), concurrent delivery, all must succeed (15s budget); requests always carry `X-Coding2API-Signature: sha256=<HMAC-SHA256(APP_SECRET, body)>`; empty = in-app records only |
| `ALERT_INTERVAL_MINUTES` | `5` | Alert evaluation cycle (floor 1) |
| `ALERT_SILENCE_MINUTES` | `30` | Silence window per `(rule, target)`; `0` disables |
| `ALERT_FOR_TICKS` | `1` | Enter-hysteresis: consecutive hits before actually alerting; `1` = fire immediately |
| `ALERT_CLEAR_TICKS` | `2` | Clear-hysteresis: consecutive misses before confirming recovery (writes `resolved_at`, pushes a `status="resolved"` notice) |
| `ALERT_POOL_READY_MIN` | `1` | Usable-credential threshold; `0` disables the rule |
| `ALERT_TASK_FAILURES` | `3` | Consecutive task failures before alerting; `0` disables |
| `ALERT_TOKEN_EXPIRY_HOURS` | `24` | Alert when a token's remaining lifetime is below this; `0` disables |
| `ALERT_ERROR_RATE_THRESHOLD` | `0.5` | Upstream error-rate threshold; `0` disables |
| `ALERT_ERROR_RATE_MIN_REQUESTS` | `20` | Minimum sample size |
| `ALERT_ERROR_RATE_WINDOW_MINUTES` | `15` | Error-rate window (from `usage_events` detail) |
| `QUOTA_EXPIRY_WINDOW_SECONDS` | `129600` | Main expiry window: credits expiring within it burn account-first; `≤0` disables expiry sorting |
| `QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS` | `604800` | Tie-breaking window; `≤0` disables |
| `CONVERSATION_STICKY_SECONDS` | `3600` | Session-affinity TTL (explicit id, else prefix fingerprint; pin wins); `≤0` disables |
| `MODEL_BLOCKLIST` | `custom_model_*,*sub*agent*,summary,browser_use_*,file_search_agent,default,hunyuan-image-*,hy4-preview-x,qwen3.8-flash,glyph-cluster` | Model-list blocklist (fnmatch, display only), matched on the outward spellings |
| `MODEL_FALLBACK_GROUPS` | `""` | Cross-channel fallback groups: `name=m1,m2;name2=m3`; members may carry `@channel`; switches only **before any frame is emitted** |
| `ALLOWED_HOSTS` | empty | Host allowlist (DNS rebinding) |
| `ALLOW_TEST_HOST` | `false` | Also allow the TestClient host `testserver`. **Tests only** |
| `TRUST_PROXY` | `false` | Honour `X-Forwarded-For` (last entry); exactly one trusted proxy only |
| `ENABLE_DOCS` | `false` | Expose `/docs` and `/openapi.json` |
| `DUMP_REQUEST_BODIES` | `false` | Diagnostic: dump `/v1` bodies to `data/dumps/` (**contains conversation content**) |
| `LOG_LEVEL` | `INFO` | Audit log is INFO — switching to `WARNING` turns it off |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Listen address/port (compose defaults to `0.0.0.0`) |
| `CODEBUDDY_API_ENDPOINT` | `https://copilot.tencent.com` | CodeBuddy upstream; add it to `CODEBUDDY_ALLOWED_ENDPOINTS` when changed |
| `CODEBUDDY_ALLOWED_ENDPOINTS` | both official sites (see compose) | Token only goes to allowlisted hosts |
| `CODEBUDDY_INTL_API_ENDPOINT` | `https://www.codebuddy.ai` | International upstream; add it to its own allowlist |
| `CODEBUDDY_INTL_ALLOWED_ENDPOINTS` | `https://www.codebuddy.ai` | **Deliberately excludes** the CN host (cross-site = leakage; misconfig fails startup) |
| `ZEN_API_ENDPOINT` / `ZEN_ALLOWED_ENDPOINTS` | `https://opencode.ai` | Zen endpoint + allowlist |
| `ZEN_OPENCODE_VERSION` | `1.18.0` | Spoofed `opencode/<version>` UA (below threshold → 426) |
| `KILO_API_ENDPOINT` / `KILO_ALLOWED_ENDPOINTS` | `https://api.kilo.ai/api/gateway` | Kilo endpoint + allowlist |
| `QODER_API_ENDPOINT` / `QODER_GATEWAY_ENDPOINT` | `https://openapi.qoder.com.cn` / `https://gateway.qoder.com.cn` | Qoder openapi (quota/login) + gateway (chat); international: `openapi.qoder.sh` / `api1.qoder.sh` |
| `QODER_ALLOWED_ENDPOINTS` | CN + international (see compose) | COSY-signed requests only to allowlisted hosts |
| `CODEARTS_API_ENDPOINT` | `https://snap-access.cn-north-4.myhuaweicloud.com` | CodeArts snap engine; add it to its allowlist |
| `CODEARTS_ALLOWED_ENDPOINTS` | snap + STS + benefit gateway + portal (see compose) | AK/SK-signed requests only to allowlisted hosts |
| `PROVIDER_PROXIES` | `""` | Per-channel egress proxy (startup-only); empty = all direct |
| `CODEBUDDY_CHAT_MIN_INTERVAL` | `5` | CB pacer (seconds), per-credential bucket; `0` disables |
| `CODEBUDDY_INTL_CHAT_MIN_INTERVAL` | `5` | CB international pacer, **independent**; `0` disables |
| `ZEN_CHAT_MIN_INTERVAL` / `KILO_CHAT_MIN_INTERVAL` | `0` | Zen / Kilo pacers, independent, off by default |
| `QODER_CHAT_MIN_INTERVAL` / `CODEARTS_CHAT_MIN_INTERVAL` | `5` | Qoder / CodeArts pacers; `0` disables |
| `CODEARTS_MAX_CONCURRENCY` | `3` | CodeArts in-flight cap per account; `0` disables |
| `CODEARTS_REQUEST_WINDOW_SECONDS` | `60` | CodeArts sliding window (3 starts / 60s with the cap above); `0` disables |
| `CODEBUDDY_SANITIZE_CHANNEL_MARKERS` | `true` | Neutralize official-client fingerprints in outbound `system`/`assistant` text (upstream 11128 content filter); outbound copy only |
| `REFRESH_SKEW_HOURS` | `24` | Pre-refresh window; short-lived channels cap it (CodeArts 45 min) |
| `TOKEN_EXPIRY_WARNING_SECONDS` | `3600` | Admin warning threshold; `≤0` disables (display only) |
| `PACER_MIN_SECONDS` / `PACER_MAX_SECONDS` | `5` / `20` | Global pacer random wait (seconds). When the bounds are equal (the `*_CHAT_MIN_INTERVAL` shape) the wait is additionally jittered **upwards** by ±25% so instances do not fire in lockstep; the lower bound is never breached |
| `AUTO_CONTINUE_MAX` | `10` | Auto-continue attempts on `finish_reason=length`; `0` disables |
| `CONTEXT_COMPRESS_ENABLED` | `true` | Trim over-long conversations to the catalog's input limit; models without a known limit are untouched |
| `CONTEXT_COMPRESS_RESERVE_TOKENS` | `4096` | Output tokens reserved by the compression budget |
| `CONTEXT_COMPRESS_MIN_KEEP_MESSAGES` | `4` | Recent messages always kept |
| `CONTEXT_COMPRESS_SAFETY_RATIO` | `0.95` | Budget as a fraction of the model limit |
| `UPSTREAM_COMPLETE_TIMEOUT_SECONDS` | `600` | Non-streaming aggregate timeout (retried as transient); `≤0` disables |
| `MAX_INFLIGHT_PER_CREDENTIAL` | `0` | Per-credential in-flight cap (global across channels): a credential at its cap stops being selected; `0` = unlimited. Leases release when the request ends, with a 600s TTL guarding leaked ones |
| `METRICS_ENABLED` | `true` | Expose `GET /metrics` (Prometheus text format, **unauthenticated**, same stance as `/healthz`); `false` disables it (404). Hot-updatable |

### Runtime settings (*Tasks & settings* page)

**45 settings** apply immediately without editing `.env` or restarting: default model, blocklist, context-compression knobs, fallback groups, both expiry windows, sticky TTL, the per-credential in-flight cap, growth toggles/interval, refresh/probe/catalog intervals, `USD_CNY_RATE`, every per-channel chat interval, CodeArts concurrency/window, pacer bounds, activity toggle/hour, all alerting rules and `METRICS_ENABLED`.

**DB overrides beat `.env`**: once changed, `.env` no longer applies to that key; the row is marked "DB override" and *Reset* removes the override. Values live in the `runtime_settings` table (plain key/value), so adding a hot-updatable key needs no migration; unknown keys and invalid/out-of-range values are rejected on write. Startup-only knobs (`APP_SECRET`, `PORT`, `DATA_DIR`, `USERS_FILE`, `OPENROUTER_MODELS_URL`, endpoint allowlists) are **excluded**. API: `GET /api/settings`, `PUT /api/settings` (admin + CSRF; `null` resets). The page also shows the 9 tasks' state (`GET /api/tasks`, admin, 30s refresh), in-process only.

## Deployment notes

- **Mount ownership**: the container runs as uid 1001 (`appuser`); `./data` and `./secrets` must be writable/readable or SQLite cannot open:

  ```bash
  mkdir -p data secrets && sudo chown -R 1001:1001 data secrets
  ```

- **Timezone**: the image defaults to `TZ=Asia/Shanghai`.

### Restart the backend after upgrading

**Changing anything under `src/` requires a process restart, or you get a "new frontend against an old backend" mismatch.** Typical symptom: the admin UI loads (frontend assets are static) but new endpoints fail — the old process lacks the route, unmatched `/api/*` returns a JSON `404`, and the client collapses that into an unrelated error. The frontend never needs a restart (the backend reads `web/dist` per request), but the **backend does** — a process manager restarts on *exit*, not on source change.

```bash
docker compose up -d --force-recreate   # Docker / compose
sudo systemctl restart coding2api       # systemd
```

Confirm the upgrade took effect (version first, then routes):

```bash
sqlite3 data/coding2api.sqlite3 "PRAGMA user_version; SELECT username, role, enabled FROM users;"   # expect user_version 22
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/users   # expect 401; 404 = old backend
```

### Frontend assets on the same port

The backend serves the frontend build at `/` (reading from disk per request), so a frontend-only change just needs a rebuild and a browser refresh; `localhost:5173` is the Vite dev server. `scripts/build-web.sh` builds only when stale, or unconditionally with `--force`.

> Deployment specifics for the development machine (process management, paths, tool versions) live in `docs/local-environment.md`, which is gitignored and absent from clones.

## Logs

The app writes **stdout / stderr** only, with no file logging or self-rotation, so the logs never stop growing on their own — configure rotation once per deployment shape.

| Deployment | Where | Rotation | Action |
|---|---|---|---|
| **Docker / compose** | `docker logs coding2api` | json-file (`10m × 5`, configured) | **none** |
| **Linux (systemd)** | `journalctl -u coding2api` | journald | **none** (template in `deploy/systemd/`) |
| **Linux (non-systemd)** | `/var/log/coding2api/*.log` | `logrotate` | `sudo cp deploy/logrotate/coding2api /etc/logrotate.d/` |
| **Bare** | terminal stderr | none | redirect and rotate yourself |

Level via `LOG_LEVEL` (default `INFO`); the audit log is INFO, so `WARNING` turns it off.

## Development

```bash
# Backend: lint + tests (100% line/branch coverage gate)
uv run ruff check src tests scripts
uv run pytest -q --cov=src --cov-report=term --cov-fail-under=100

# Frontend
cd web
pnpm exec tsc --noEmit
pnpm exec vitest run
pnpm build
```

## Documentation

Detailed documentation is written in Chinese:

| Document | Content |
|---|---|
| [`PROPOSAL.md`](PROPOSAL.md) | Scope, upstream feasibility findings, architecture, data model, security boundary |
| [`TECHNICAL.md`](TECHNICAL.md) | Stack, module specs, provider protocol, request flow, testing |
| [`diagrams/coding2api-architecture.html`](diagrams/coding2api-architecture.html) | Architecture diagram |
| [`diagrams/coding2api-request-sequence.html`](diagrams/coding2api-request-sequence.html) | Request main-chain sequence diagram |
| [`diagrams/coding2api-credential-lifecycle.html`](diagrams/coding2api-credential-lifecycle.html) | Credential scheduling lifecycle |

## License

MIT — see [LICENSE](LICENSE). Attribution for the upstream projects this work learned from is in [NOTICE](NOTICE).
