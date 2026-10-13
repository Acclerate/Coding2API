"""Host 白名单与安全响应头（PROPOSAL §6）。

拆出来是为了让 build_app 只负责装配；这两个策略本身与业务无关，
改动理由也不同（安全策略调整 vs 服务组装）。
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..compat.openai.errors import error_payload
from ..config import Settings


def _hostname(host_header: str) -> str:
    """从 Host/Origin 头取出主机名（小写，剥端口与 IPv6 方括号）。

    IPv6 字面量形如 `[::1]:8000`：先按 `]` 切开取括号内，否则 `split(":")`
    会在第一个冒号处截断成空串。`csrf._same_host` 用同一套规则（见
    `test_security.py` 的 IPv6 用例）。
    """
    text = host_header.strip()
    if text.startswith("["):
        name, _, _ = text.partition("]")
        return name.lstrip("[").strip().lower()
    return text.split(":", 1)[0].strip().lower()


def host_allowed(host_header: str, settings: Settings) -> bool:
    """Host 白名单（防 DNS rebinding）。

    ALLOWED_HOSTS 配置优先（逗号分隔）；未配置时放行本地回环与 PUBLIC_BASE_URL
    的主机。`testserver`（FastAPI TestClient 默认 Host）只在显式开启
    `ALLOW_TEST_HOST` 时放行——单标签主机名可能被本地 DNS 搜索域解析到本服务，
    生产默认放行等于给 DNS rebinding 留免校验入口。
    只比较主机名，忽略端口。
    """
    hostname = _hostname(host_header)
    if settings.allowed_hosts:
        allowed = {_hostname(h) for h in settings.allowed_hosts.split(",") if h.strip()}
        return hostname in allowed
    allowed = {"localhost", "127.0.0.1", "::1"}
    if settings.allow_test_host:
        allowed.add("testserver")
    base = settings.public_base_url
    if base.startswith(("http://", "https://")):
        base = base.split("://", 1)[1]
    path_host = _hostname(base.split("/", 1)[0])
    if path_host:
        allowed.add(path_host)
    return hostname in allowed


def content_security_policy(settings: Settings) -> str:
    """CSP：默认只允许同源资源，顺带堵住 object/base-uri/form-action（L8）。

    `script-src 'self'` 之所以能收这么紧：主题初始化已从内联脚本挪到
    `web/public/theme-init.js`（同步脚本，渲染阻塞、不会闪烁），页面里再无内联
    JS。`style-src` 保留 `'unsafe-inline'`——React 的 `style={}` 会写行内 style
    属性，去掉会直接打断渲染。

    `enable_docs` 打开时 Swagger UI 从 jsdelivr 拉资源，必须放行该 CDN，
    否则 /docs 白屏（默认关闭，不影响生产策略）。
    """
    script_src = "'self'"
    style_src = "'self' 'unsafe-inline'"
    if settings.enable_docs:
        script_src += " https://cdn.jsdelivr.net"
        style_src += " https://cdn.jsdelivr.net"
    return (
        "default-src 'self'; "
        f"script-src {script_src}; "
        f"style-src {style_src}; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; "
        "form-action 'self'")


async def security_middleware(request: Request, call_next):
    """Host 校验 + 安全响应头。请求体上限由 BodySizeLimitMiddleware 处理。

    注册方式（app.middleware("http") 装饰器）留在 build_app，因为那是
    FastAPI 的接线细节；返回什么由这里决定。
    """
    host_header = request.headers.get("host", "")
    if not host_allowed(host_header, request.app.state.settings):
        return JSONResponse(status_code=400,
                            content=error_payload("invalid host header",
                                                  "invalid_request", 400))
    # 纯读 content-length 会被 chunked 请求绕过，因此上限在 ASGI 层做
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        content_security_policy(request.app.state.settings))
    # 只跨源剥离 Referer，同源保留：CSRF 校验依赖同源 Referer（`same-origin`
    # 正好两者兼顾——`no-referrer` 会让同源写请求丢掉 Referer 而被误判跨源）。
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.app.state.settings.public_base_url.startswith("https://"):
        # 仅 https 部署下发 HSTS：明文部署发了无意义，还会预锁本地 http 访问
        response.headers.setdefault("Strict-Transport-Security",
                                    "max-age=31536000; includeSubDomains")
    return response
