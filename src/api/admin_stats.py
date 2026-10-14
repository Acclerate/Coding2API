"""用量统计查询：overview / by-provider / timeline（admin 可跨用户聚合）。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ..auth.rbac import Principal, require_operator
from ..stats.csv import CSV_BOM, EXPORT_HEADERS, csv_line, event_cells
from ..stats.query import EXPORT_MAX_ROWS
from .deps import Services, principal_from_request


def _scope(principal: Principal, username: str | None) -> str | None:
    """admin/operator 传 username 可查全局/指定人；viewer 永远只能看自己。

    operator 是「凭证运维」角色，按 PROPOSAL §3.7 与 README 的角色表应能看
    全量统计（否则看不到自己维护的渠道整体健康）；viewer 才是严格自负。
    """
    return username if principal.is_operator else principal.username


def _export_filename(now: int) -> str:
    stamp = datetime.fromtimestamp(now, tz=UTC).strftime("%Y%m%d-%H%M%S")
    return f"coding2api-events-{stamp}Z.csv"


def _csv_stream(rows: list[dict]) -> Iterator[str]:
    """把明细行编成 CSV 文本块（含 BOM 与表头）。"""
    yield CSV_BOM + csv_line(list(EXPORT_HEADERS))
    for row in rows:
        yield csv_line(event_cells(row))


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    stats_query = services.stats_query

    @router.get("/api/stats/overview")
    async def stats_overview(principal=Depends(principal_from_request),
                             username: str | None = None, since: int | None = None,
                             until: int | None = None):
        return stats_query.overview(username=_scope(principal, username), since=since,
                                    until=until)

    @router.get("/api/stats/by-provider")
    async def stats_by_provider(principal=Depends(principal_from_request),
                                username: str | None = None, since: int | None = None,
                                until: int | None = None,
                                sort: str | None = None, order: str | None = None):
        return {"providers": stats_query.by_provider(
            username=_scope(principal, username), since=since, until=until,
            sort=sort, order=order)}

    @router.get("/api/stats/by-model")
    async def stats_by_model(principal=Depends(principal_from_request),
                             username: str | None = None, since: int | None = None,
                             until: int | None = None,
                             sort: str | None = None, order: str | None = None):
        return {"models": stats_query.by_model(
            username=_scope(principal, username), since=since, until=until,
            sort=sort, order=order)}

    @router.get("/api/stats/timeline")
    async def stats_timeline(principal=Depends(principal_from_request),
                             username: str | None = None, since: int | None = None,
                             until: int | None = None,
                             metric: str = "requests"):
        return {"points": stats_query.timeline(
            username=_scope(principal, username), since=since, until=until, metric=metric)}

    @router.get("/api/stats/model-timeline")
    async def stats_model_timeline(principal=Depends(principal_from_request),
                                   username: str | None = None, since: int | None = None,
                                   until: int | None = None,
                                   metric: str = "requests"):
        return stats_query.model_timeline(
            username=_scope(principal, username), since=since, until=until, metric=metric)

    @router.get("/api/stats/by-user")
    async def stats_by_user(principal=Depends(principal_from_request),
                            username: str | None = None, since: int | None = None,
                            until: int | None = None,
                            sort: str | None = None, order: str | None = None):
        return {"users": stats_query.by_user(
            username=_scope(principal, username), since=since, until=until,
            sort=sort, order=order)}

    @router.get("/api/stats/by-credential")
    async def stats_by_credential(principal=Depends(principal_from_request),
                                  username: str | None = None, since: int | None = None,
                                  until: int | None = None,
                                  sort: str | None = None, order: str | None = None):
        # 凭证昵称来自共享池（常含邮箱/手机）：仅 admin/operator 可见，
        # 与明细端点同口径；provider 无隐私顾虑，无条件下发（前端画渠道 icon）
        return {"credentials": stats_query.by_credential(
            username=_scope(principal, username), since=since, until=until,
            sort=sort, order=order,
            include_credential_name=principal.is_operator)}

    @router.get("/api/stats/events")
    async def stats_events(principal=Depends(principal_from_request),
                           username: str | None = None, since: int | None = None,
                           until: int | None = None,
                           before: int | None = None, limit: int = 50,
                           offset: int = 0, sort: str | None = None,
                           order: str | None = None,
                           provider: str | None = None, model: str | None = None,
                           credential_id: str | None = None,
                           format: str = "json"):
        # 明细保留 90 天；单页上限 200，防止一次拉爆
        # 凭证昵称来自共享池：仅 admin/operator 可见，viewer 只看自己的记录
        scope = _scope(principal, username)
        names = principal.is_operator
        if format == "csv":
            # 导出仅 admin/operator：整份明细落盘比翻页浏览的暴露面大得多，
            # 且文件名/内容都带池级信息，viewer 不该有。
            require_operator(principal)
            rows = stats_query.export_events(
                username=scope, since=since, until=until, include_credential_name=names,
                provider=provider, model=model, credential_id=credential_id)
            truncated = len(rows) > EXPORT_MAX_ROWS
            rows = rows[:EXPORT_MAX_ROWS]
            now = int(datetime.now(tz=UTC).timestamp())
            headers = {
                "Content-Disposition": f'attachment; filename="{_export_filename(now)}"',
                "X-Export-Row-Count": str(len(rows)),
                "X-Export-Truncated": "1" if truncated else "0",
                "Cache-Control": "no-store",
            }
            return StreamingResponse(
                _csv_stream(rows), media_type="text/csv; charset=utf-8", headers=headers)
        return stats_query.events(username=scope, since=since, until=until,
                                  before=before, limit=max(1, min(limit, 200)),
                                  offset=max(0, offset), sort=sort, order=order,
                                  provider=provider, model=model,
                                  credential_id=credential_id,
                                  include_credential_name=names)

    return router
