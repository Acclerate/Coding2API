# Coding2API × 灵码 (Lingma) 企业账号接入指南

> 记录从 Qoder CN IDE 提取灵码企业令牌到 Coding2API 本地网关的完整实现过程。
>
> 适用场景：通过通义灵码/Lingma 登录的 Qoder CN IDE 企业账号，需要暴露为 OpenAI 兼容 API 供 Claude Code、CC Switch 等工具接入。
>
> **实现日期**：2026-10-14
> **Coding2API 版本**：v0.4.3
> **关联文档**：[Coding2API本地部署计划.md](../../docs/05-工具使用参考/Coding2API本地部署计划.md)、[Qoder凭证获取与灵码接入方案.md](../../docs/05-工具使用参考/Qoder凭证获取与灵码接入方案.md)

---

## 1. 背景与问题

Qoder CN IDE（VS Code fork）支持通过**通义灵码 (Lingma)** 登录。Lingma 令牌（`pt-*` 前缀）在 `lingma-api.tongyi.aliyun.com` 上有效，但在 Qoder 默认网关 `gateway.qoder.com.cn` 上会被 401/403 拒绝。

Coding2API 的 Qoder 渠道代码中已预置 `lingma` realm 配置，但存在以下问题导致接入不工作：

1. **`_gateway_hosts` bug**：客户端默认网关（cn realm 的 `gateway.qoder.com.cn`）会覆盖 lingma/intl 凭证自己的网关 → 请求被路由到错误域名
2. **探测路径 bug**：`probe_quota` 使用固定的 `self.host`（qoder openapi），lingma 令牌打过去 401 → 管理台误报「凭证被拒绝」
3. **健康度误判**：探测成功但额度全 None 时，`health_score` 返回 `EXHAUSTED(-1)`，管理台显示「已耗尽」

---

## 2. 令牌提取（从 Qoder CN IDE）

### 2.1 定位 IDE 数据目录

Qoder CN IDE 的安装路径：

```
C:\Program Files\Qoder CN IDE
```

VS Code SecretStorage 加密数据目录：

```
C:\Users\<用户名>\AppData\Roaming\QoderCN\
```

### 2.2 提取加密令牌

**关键区别**：解密 `state.vscdb` 中的 secret 需要用 VS Code 用户数据目录的 `Local State` 文件，而非 Electron 应用数据目录。

```python
import json, os, sqlite3
from pathlib import Path
import win32crypt

# 1. 读取 master key（DPAPI 解密）
local_state_path = Path(r"C:\Users\Administrator\AppData\Roaming\QoderCN\Local State")
with open(local_state_path, "r") as f:
    local_state = json.load(f)

encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
# 跳过前缀 "DPAPI"
encrypted_key = encrypted_key[5:]
master_key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]

# 2. 读取 state.vscdb
vscdb_path = Path(r"C:\Users\Administrator\AppData\Roaming\QoderCN\state.vscdb")
conn = sqlite3.connect(str(vscdb_path))
cursor = conn.cursor()
cursor.execute(
    "SELECT value FROM ItemTable WHERE key = 'secret://aicoding.auth.userInfo'"
)
row = cursor.fetchone()
encrypted_secret = base64.b64decode(row[0])

# 3. AES-256-GCM 解密
import hashlib
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# key = SHA-256(master_key + "a1s2d3f4s5d6f7g8h9")  # VS Code 的 salt
key = hashlib.sha256(master_key + b"a1s2d3f4s5d6f7g8h9").digest()
nonce = encrypted_secret[:12]
tag = encrypted_secret[12:28]
ciphertext = encrypted_secret[28:]
aesgcm = AESGCM(key)
plaintext = aesgcm.decrypt(nonce, ciphertext + tag, None)
auth_info = json.loads(plaintext)

# auth_info = {
#     "token": "pt-xxxxxxxxxxxxx",
#     "refreshToken": "rt-xxxxxxxxxxxxx",
#     "login_source": "aliyun",
#     "userType": "enterprise_standard"
# }
```

### 2.3 验证令牌有效性

```bash
curl -H "Authorization: Bearer pt-xxx" \
  https://lingma-api.tongyi.aliyun.com/algo/api/v2/model/list?Encode=1
```

成功返回模型列表即表示令牌有效。

---

## 3. 凭证导入 Coding2API

### 3.1 管理台登录

访问 `http://127.0.0.1:8321`，用管理员账号登录。

### 3.2 导入凭证

**必须设置 `realm=lingma`**，否则默认走 cn realm（`gateway.qoder.com.cn`），令牌会被拒绝。

```bash
curl -X POST http://127.0.0.1:8321/api/credentials \
  -H "Content-Type: application/json" \
  -b "session=<admin_cookie>" \
  -d '{
    "provider": "qoder",
    "data": {
      "access_token": "pt-xxx",
      "refresh_token": "rt-xxx",
      "realm": "lingma",
      "auth_source": "manual"
    }
  }'
```

返回：

```json
{
  "id": "cred_6a9dce165f2c4cd4",
  "provider": "qoder",
  "nickname": "Lingma 企业账号（宋皓阳）",
  "realm": "lingma"
}
```

> `auth_source=manual` 表示不做 deviceToken 刷新（Lingma 令牌到期需重新从 IDE 提取）。

---

## 4. 代码修复（三个补丁）

### 4.1 修复 `_gateway_hosts`（client.py L262-266）

**根因**：`_gateway_hosts` 的"客户端网关顶掉区域主选"逻辑对 lingma/intl 凭证是"错域"替换——会把请求全部路由到 cn 网关。

**修复**：仅当凭证属于默认区域，或客户端网关是显式自定义值（不在默认区域主机清单内）时才替换。

```python
default_hosts = gateway_candidates(qoder_events.DEFAULT_REALM)
if (configured and configured[0] != self.gateway
        and (realm == qoder_events.DEFAULT_REALM
             or self.gateway not in default_hosts)):
    configured = [self.gateway, *configured[1:]]
```

### 4.2 修复探测路径（`__init__.py` L102-108）

**根因**：`probe_quota` 使用固定的 `self.host`（`openapi.qoder.com.cn`），lingma 令牌打过去 401。

**修复**：lingma realm 改用模型发现做健康检查。

```python
async def probe_quota(self, credential_data: dict) -> Quota:
    credential = QoderCredential.from_dict(credential_data)
    if credential.realm == "lingma":
        await self.client.fetch_models(credential)  # 通过 _gateway_hosts 路由到 lingma 网关
        return Quota(probed_at=int(time.time()))
    return await self.client.probe_quota(credential)
```

### 4.3 修复健康度计算（base.py L243-244）

**根因**：`health_score` 把 `total=None` 一律判为 `EXHAUSTED(-1)`，但 lingma 是"探测成功、额度未知"。

**修复**：探测成功但额度字段全为空时返回 `UNKNOWN(None)`。

```python
def health_score(quota: Quota | None) -> int | None:
    if quota is None or quota.probe_failed:
        return UNKNOWN
    # 探测成功但额度字段全为空（如 lingma 无 openapi 额度端点）→ 未知，非耗尽
    if quota.remaining is None and quota.total is None:
        return UNKNOWN
    if quota.total is None or quota.total <= 0:
        return EXHAUSTED
    ...
```

---

## 5. 验证修复

### 5.1 探测验证

```bash
curl -X POST http://127.0.0.1:8321/api/credentials/cred_xxx/probe \
  -b "session=<admin_cookie>"
```

**预期结果**：

```json
{
  "probed": true,
  "remaining": null,
  "total": null,
  "cycle_end": null
}
```

- `probed: true` — 凭证存活
- `health: null`（UNKNOWN）— 不再误报 EXHAUSTED
- `quota_probed_at` — 时间戳已记录

### 5.2 聊天验证

```bash
curl http://127.0.0.1:8321/v1/chat/completions \
  -H "Authorization: Bearer sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

**预期结果**：正常返回流式响应，确认请求已通过 `_gateway_hosts` 路由到 `lingma-api.tongyi.aliyun.com`。

---

## 6. CC Switch 接入配置

### 6.1 创建 API Key

在 Coding2API 管理台「API Key 管理」创建一个新的 Key，或：

```bash
curl -X POST http://127.0.0.1:8321/api/api-keys \
  -H "Content-Type: application/json" \
  -b "session=<admin_cookie>" \
  -d '{"name": "cc-switch"}'
```

### 6.2 CC Switch Claude Code 配置

1. 打开 CC Switch → 侧栏选 **Claude Code** → 添加供应商 → **自定义配置**
2. 填写：
   - **名称**：`Coding2API 本地`
   - **API Key**：上一步创建的 `sk-xxx`
   - **请求地址**：`http://127.0.0.1:8321`（**不带** `/v1`）
3. 勾选 **「需要模型映射」**
4. **API 格式**：`OpenAI Chat Completions`
5. **模型映射**（Claude 档位 → Coding2API 实际模型）：

| Claude 档位 | 推荐映射 |
|-------------|----------|
| **Sonnet** | `deepseek-v4-pro` |
| **Opus** | `qwen3-max` |
| **Haiku** | `deepseek-v4-flash` |

6. 保存 → 路由页开 **Claude Code 开关** → 重启 Claude Code

### 6.3 CC Switch Codex 配置

1. 侧栏选 **Codex** → 添加供应商 → **自定义配置**
2. 填写：
   - **名称**：`Coding2API 本地`
   - **API Key**：`sk-xxx`
   - **请求地址**：`http://127.0.0.1:8321/v1`（**带** `/v1`）
   - **模型**：`deepseek-v4-pro`（或从获取列表中选择）
3. **上游格式**：`Chat Completions`
4. 保存 → 路由页开 **Codex 开关** → 重启 Codex

---

## 7. 开机自启

### 7.1 启动脚本

`start_coding2api.bat`（已复制到 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`）。
日志写入 `D:\privategit\github\Coding2API\logs\coding2api_startup.log`（UTF-8、无 BOM），
同时逐行输出到控制台（PowerShell tee；禁止把日志放到 C 盘）：

```batch
@echo off
chcp 65001 >nul
cd /d "D:\privategit\github\Coding2API"
set LOG_DIR=D:\privategit\github\Coding2API\logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set LOG_FILE=%LOG_DIR%\coding2api_startup.log
set PYTHONIOENCODING=utf-8
echo === uvicorn session start at %date% %time% === >> "%LOG_FILE%"
powershell -NoProfile -Command "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); & 'C:\Users\Administrator\.local\bin\uv.exe' run -- uvicorn src.main:build_app --factory --host 127.0.0.1 --port 8321 2>&1 | ForEach-Object { $line = [string]$_; [IO.File]::AppendAllText('%LOG_FILE%', $line + [char]13 + [char]10, [Text.UTF8Encoding]::new($false)); $line }; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 }"
echo [%date% %time%] 服务已退出，退出码: %errorlevel%
```

> 注意：不要用 `| findstr ""` 之类当 tee——空搜索串会让 findstr 立即退出、
> 打断管道，之后每条日志写入都报 `OSError: [Errno 22]`（`--- Logging error ---
> ` 刷屏）。也不要把日志重定向到 `%TEMP%`（C 盘）。
> `set PYTHONIOENCODING=utf-8` 必须保留：Startup/cmd 启动的服务环境里没有
> PYTHONUTF8/PYTHONIOENCODING 时，Python 会按 GBK 写 stdio，中文日志全部乱码。

### 7.2 停止脚本

`stop_coding2api.bat`（双击即停）。与启动脚本同样**全 ASCII**（UTF-8 中文 + chcp 65001 混用会被 cmd.exe 误解析，教训见 §7.1）：

```batch
@echo off
:: ASCII-only on purpose -- same lesson as start_coding2api.bat.
chcp 65001 >nul
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8321.*LISTENING"') do (
    echo Found PID: %%a
    taskkill /PID %%a /T >nul 2>&1
    if errorlevel 1 (
        taskkill /PID %%a /T /F >nul 2>&1
    )
    echo [OK] PID %%a terminated.
    timeout /t 2 /nobreak >nul
    exit /b 0
)
echo [HINT] No process listening on port 8321 -- service may not be running.
pause
```

---

## 8. 常见问题

### 8.1 管理台显示「凭证被拒绝」

**排查**：
1. 确认凭证 `realm` 是否为 `lingma`
2. 确认 `_gateway_hosts` 修复已加载（重启服务）
3. 确认 `probe_quota` 修复已加载（lingma 改用 `fetch_models` 探测）

### 8.2 聊天返回 403 code 105

**排查**：`_gateway_hosts` bug 未修复，请求被路由到 `gateway.qoder.com.cn`。检查 client.py L262-266 的修复是否生效。

### 8.3 令牌过期

**处理**：Lingma 令牌（`pt-*`）有效期较短，到期后需重新从 IDE 提取并导入。auth_source=manual 的凭证不会自动刷新。

---

## 9. 文件变更清单

| 文件 | 变更 | 说明 |
|------|------|------|
| `src/provider/qoder/client.py` L262-266 | 修复 `_gateway_hosts` | 默认区域网关不顶掉 lingma/intl 凭证的网关 |
| `src/provider/qoder/__init__.py` L102-108 | 修复 `probe_quota` | lingma realm 改用 `fetch_models` 做健康检查 |
| `src/provider/base.py` L243-244 | 修复 `health_score` | 探测成功但额度全 None → UNKNOWN，非 EXHAUSTED |
| `src/provider/qoder/__init__.py` L108 | 修复 `probed_at` | lingma 探测返回 `Quota(probed_at=...)` |
| `start_coding2api.bat` | 新增 | 开机自启脚本（已复制到 Startup 目录） |
| `stop_coding2api.bat` | 新增 | 停止脚本 |

---

## 10. 实现验证结果（2026-10-14）

### 10.1 服务状态

```
GET /health    → {"status":"ok"}
GET /healthz   → total=4  ready=1  cooling=0  paused=3  disabled=0
```

4 个凭证中：1 个 lingma 企业账号就绪，3 个手动暂停（其他渠道账号）。

### 10.2 可用模型（GET /v1/models 实际返回）

```
org-auto          qwen3-max           qwen3.7-max
deepseek-v4-pro   qwen3.7-plus        qwen3-coder-plus
deepseek-v4-flash qwen3.6-flash       glm-5.2
kimi-k2.6         minimax-m2.7
```

共 11 个模型，涵盖 Qoder CN（COSY）、CodeBuddy、Zen/Kilo 等多渠道。

### 10.3 Claude Code 实际配置

配置文件：`%USERPROFILE%\.claude\settings.json` 的 `env` 段：

| Claude Code 层级 | 映射模型 | 说明 |
|------------------|----------|------|
| Sonnet（主力） | `org-auto` | 跨渠道自动选号 |
| Opus | `deepseek-v4-pro` | DeepSeek 渠道 |
| Fable | `glm-5.2` | GLM 渠道 |
| Haiku | `deepseek-v4-flash` | DeepSeek 渠道 |
| Subagent | `org-auto` | 子 Agent 同主力 |

> **与 §6.2 CC Switch 映射的差异**：CC Switch 配置中 Sonnet 映射为 `deepseek-v4-pro`，但实际 Claude Code settings.json 中 Sonnet 映射为 `org-auto`（自动选号）。两者都是有效配置，`org-auto` 的优势是自动跨渠道选健康凭证，`deepseek-v4-pro` 的优势是固定渠道、行为可预测。

### 10.4 流量链路

```
Claude Code v2.1.296
    → settings.json env (ANTHROPIC_BASE_URL=http://127.0.0.1:8321)
    → Coding2API /v1/messages (Anthropic Messages API)
    → 凭证池选号（org-auto → 健康度最高的 lingma 凭证）
    → _gateway_hosts 路由到 lingma-api.tongyi.aliyun.com
    → COSY 签名 + WAF 编码 + 双层 SSE 翻译
    → 返回标准 Anthropic Messages 格式
```

### 10.5 开机自启状态

使用 §7.1 的 `start_coding2api.bat` + Startup 目录方式（非 schtasks）。开机后 Coding2API 自动启动，日志写入 `D:\privategit\github\Coding2API\logs\coding2api_startup.log`（同时输出到控制台）。

### 10.6 待跟进项

- [ ] 令牌过期后重新提取流程的自动化（当前需手动重新执行 §2）
- [ ] 通义灵码 VS Code 插件安装与 Coding2API 端点配置
- [ ] 3 个暂停凭证的恢复与多账号轮转测试
