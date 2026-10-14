"""用量统计查询（TECHNICAL §2 query.py）：overview / by-provider / timeline / events。"""

from __future__ import annotations

from typing import Any

from ..sorting import parse_sort_order, sql_order

METRIC_COLUMNS: dict[str, str] = {
    # metric 参数 → usage_hourly 上的取值表达式
    "requests": "requests",
    "tokens": "input_tokens + output_tokens",
    "latency": "latency_sum",      # 均值由调用方除以 ok_count
    "ttfb": "ttfb_sum",            # 同上
    # 成本默认口径是人民币（与本模块展示一致）；仅含可定价明细的小时才有值。
    "cost": "cost_cny_sum",
}


# 分组统计排序白名单：API 键 → 聚合列别名（`group` 指分组键列 `group_key`）。
# 默认按请求数降序（「哪家/哪个模型用得最多」才是分组表的用途），并列时按
# 分组键升序——次序稳定且与旧行为一致（旧代码就是按分组键升序）。
GROUP_SORT_COLUMNS: dict[str, str] = {
    "group": "group_key",
    "requests": "requests",
    "ok_count": "ok_count",
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cached_tokens": "cached_tokens",
    "credit": "credit_sum",
    "cost_cny": "cost_cny_sum",
}


# 逐请求明细排序白名单：API 键 → 列（带 `e.` 前缀，因查询有 JOIN）。
# `time` 是 rowid（插入序）；它保持降序时走游标分页，其余情况走 offset 分页。
EVENT_SORT_COLUMNS: dict[str, str] = {
    "time": "e.rowid",
    "ts": "e.ts",
    "username": "e.username",
    "provider": "e.provider",
    "credential": "e.credential_id",
    "model": "e.model",
    "ok": "e.ok",
    "input_tokens": "e.input_tokens",
    "output_tokens": "e.output_tokens",
    "cached_tokens": "e.cached_tokens",
    "credit": "e.credit",
    "cost_cny": "e.cost_cny",
    "ttfb_ms": "e.ttfb_ms",
    "latency_ms": "e.latency_ms",
}


# 分组键白名单：`_group_hourly` / `_group_events` 把 key 直接拼进 SQL（列名无法
# 参数绑定），调用点全是本模块内的字面量。留一份显式白名单并在入口断言，未来
# 若有人把外部输入当 key 传进来，会当场炸而不是拼出注入语句。
_GROUP_KEYS = frozenset({"provider", "model", "username", "credential_id"})

# CSV 导出单次上限：明细只留 90 天，但全量导出（几万行）会一次性拉进内存并
# 撑爆响应。超上限时明确告知「被截断、请收窄条件」，而不是静默给半份文件。
EXPORT_MAX_ROWS = 10000


def _group_order(sort: str | None, order: str | None) -> str:
    """分组统计的 `ORDER BY`：主列随 sort，并列时分组键升序（稳定次序）。"""
    key, desc = parse_sort_order(sort, order, GROUP_SORT_COLUMNS,
                                 default_key="requests", default_desc=True)
    column = GROUP_SORT_COLUMNS[key]
    if column == "group_key":
        return "group_key ASC"
    direction = "DESC" if desc else "ASC"
    return f"{column} {direction}, group_key ASC"


class StatsQuery:
    def __init__(self, db) -> None:
        self._db = db

    def _metric_sql(self, metric: str) -> tuple[str, bool]:
        """返回 (取值表达式, 是否均值语义)。非法 metric 回退 requests。"""
        expr = METRIC_COLUMNS.get(metric, "requests")
        return expr, metric in ("latency", "ttfb")

    def _metric_value(self, metric: str, expr_value, ok_count, cost_known: int = 0) -> Any:
        """把 SQL 原始值按 metric 归一：均值类除以 ok_count，无成功则 0。

        成本是**部分可定价**的：某小时/模型若一条明细都没匹配到价表
        （`cost_known=0`），聚合值只是 0，直接展示会被读成「免费」；这里回 None
        让前端显示 —。只要有一条可定价，就按可定价部分求和（宁可少算不虚报）。
        """
        if metric in ("latency", "ttfb"):
            return round(expr_value / ok_count) if ok_count else 0
        if metric == "cost" and not cost_known:
            return None
        return expr_value

    @staticmethod
    def _where(*, username: str | None = None, provider: str | None = None,
               model: str | None = None, credential_id: str | None = None,
               since: int | None = None, since_col: str = "ts",
               until: int | None = None, until_col: str = "ts",
               before: int | None = None, before_col: str = "rowid",
               alias: str = "") -> tuple[str, list[Any]]:
        """拼 WHERE 子句与参数。alias 为 JOIN 时的表前缀（如 "e."）。

        `since` 与 `until` 是**同一个时间窗的两端**（半开区间 `[since, until)`），
        区别只在取值列：明细表用 `ts`，小时汇总用 `hour_utc`。两者可单独给
        （只给一端 = 单边不限），也可都不给（全部，与历史行为一致）。

        `before` 是**游标**不是时间：它按 `rowid`（插入序）取「更小的一页」，
        与 90 天时间窗无关，故与 `until` 分开、各用一列。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if username is not None:
            clauses.append(f"{alias}username = ?")
            params.append(username)
        if provider is not None:
            clauses.append(f"{alias}provider = ?")
            params.append(provider)
        if model is not None:
            clauses.append(f"{alias}model = ?")
            params.append(model)
        if credential_id is not None:
            clauses.append(f"{alias}credential_id = ?")
            params.append(credential_id)
        if since is not None:
            clauses.append(f"{alias}{since_col} >= ?")
            params.append(since)
        if until is not None:
            # 半开区间上界：`until` 本身不计入（与 since 的闭下界区分），
            # 前端按此把「结束时间」当作瞬时右端点，前后两次查询不重叠
            clauses.append(f"{alias}{until_col} < ?")
            params.append(until)
        if before is not None:
            clauses.append(f"{alias}{before_col} < ?")
            params.append(before)
        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params

    def overview(self, *, username: str | None = None, provider: str | None = None,
                 since: int | None = None, until: int | None = None) -> dict[str, Any]:
        """总览。admin 传 username=None 看全局；普通用户只看自己。

        数据源是 `usage_hourly`（与 timeline/model-timeline 同源）：
        明细只留 90 天，读明细会让选「全部」时总览小于图表；小时汇总永久保留。
        代价：最近 ≤5 分钟未进汇总的请求不计入（retention 每 5 分钟 rollup），
        刷新一次即可。延迟类均值统一按 `ok_count` 归一（与图表同口径）。

        `since` / `until`：自定义时间窗两端（半开 `[since, until)`），按小时桶
        过滤，与 `timeline` 同口径。
        """
        where, params = self._where(username=username, provider=provider,
                                    since=since, since_col="hour_utc",
                                    until=until, until_col="hour_utc")

        row = self._db.connect().execute(
            f"""
            SELECT COALESCE(SUM(requests), 0) AS requests,
                   COALESCE(SUM(ok_count), 0) AS ok_count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                   SUM(cached_tokens) AS cached_tokens,
                   SUM(cached_known) AS cached_known,
                   SUM(credit_sum) AS credit_sum,
                   SUM(credit_known) AS credit_known,
                   SUM(credit_estimated_known) AS credit_estimated_known,
                   SUM(cost_usd_sum) AS cost_usd_sum,
                   SUM(cost_cny_sum) AS cost_cny_sum,
                   SUM(cost_known) AS cost_known,
                   SUM(latency_sum) AS latency_sum,
                   SUM(ttfb_sum) AS ttfb_sum
            FROM usage_hourly {where}
            """, params).fetchone()
        requests = row["requests"] or 0
        ok_count = row["ok_count"] or 0
        # 均值口径：只除成功请求（失败请求的 latency 会拉偏"典型耗时"，
        # 且与图表 latency_sum/ok_count 同一致），无成功则 None
        return {
            "requests": requests,
            "ok_count": ok_count,
            "success_rate": (ok_count / requests) if requests else None,
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "reasoning_tokens": row["reasoning_tokens"],
            # 缓存命中：任一明细上报过才可信，否则 None（前端显示 —）
            "cached_tokens": row["cached_tokens"] if row["cached_known"] else None,
            "credit": row["credit_sum"] if row["credit_known"] else None,
            # 汇总 credit 中含推算值时标 ≈（全部渠道任一为推算即标，保守）
            "credit_estimated": bool(row["credit_estimated_known"]),
            # 成本：只统计匹配到定价的明细（cost_known 条数>0 才可信）
            "cost_usd": row["cost_usd_sum"] if row["cost_known"] else None,
            "cost_cny": row["cost_cny_sum"] if row["cost_known"] else None,
            "avg_latency_ms": round(row["latency_sum"] / ok_count) if ok_count else None,
            "avg_ttfb_ms": round(row["ttfb_sum"] / ok_count) if ok_count else None,
        }

    def metrics_provider_model(self) -> list[tuple[str, str, int, int]]:
        """按 (渠道, 模型) 汇总请求数 / 成功数（/metrics 的 requests_total）。

        读**永久保留**的 `usage_hourly`（与总览同源）：明细只留 90 天，
        用它算累计计数器会随裁剪下降。返回 (provider, model, requests, ok_count)，
        由调用方拆成 status=ok / error 两条样本。排序稳定（provider, model）。
        """
        rows = self._db.connect().execute(
            "SELECT provider, model, COALESCE(SUM(requests), 0) AS requests, "
            "COALESCE(SUM(ok_count), 0) AS ok_count "
            "FROM usage_hourly GROUP BY provider, model "
            "ORDER BY provider, model").fetchall()
        return [(row["provider"], row["model"], int(row["requests"]),
                 int(row["ok_count"])) for row in rows]

    def metrics_provider_tokens(self) -> list[tuple[str, int, int, int, int]]:
        """按渠道汇总各 token 类别（/metrics 的 tokens_total）。

        返回 (provider, input, output, reasoning, cached)，排序稳定（provider）。
        与 requests 同为 `usage_hourly`（永久）上的累计值。
        """
        rows = self._db.connect().execute(
            "SELECT provider, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens, "
            "COALESCE(SUM(cached_tokens), 0) AS cached_tokens "
            "FROM usage_hourly GROUP BY provider ORDER BY provider").fetchall()
        return [(row["provider"], int(row["input_tokens"]), int(row["output_tokens"]),
                 int(row["reasoning_tokens"]), int(row["cached_tokens"]))
                for row in rows]

    @staticmethod
    def _group_row(row: Any, key: str, extra: tuple[str, ...] = ()) -> dict[str, Any]:
        """把一行聚合结果转成 {分组键: ...} + 公共指标的 dict（各分组维度共用）。

        `extra`：维度特有的展示列（如凭证维度的 provider / credential_name），
        SQL 已算好，这里原样带出。
        """
        return {
            key: row["group_key"],
            "requests": row["requests"],
            "ok_count": row["ok_count"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            # 缓存命中：任一明细上报过才可信，否则 None（前端显示 —）
            "cached_tokens": row["cached_tokens"] if row["cached_known"] else None,
            "credit": row["credit_sum"] if row["credit_known"] else None,
            "credit_estimated": bool(row["credit_estimated_known"]),
            "cost_usd": row["cost_usd_sum"] if row["cost_known"] else None,
            "cost_cny": row["cost_cny_sum"] if row["cost_known"] else None,
            **{name: row[name] for name in extra},
        }

    def _group_hourly(self, key: str, *, username: str | None = None,
                      since: int | None = None, until: int | None = None,
                      sort: str | None = None,
                      order: str | None = None) -> list[dict[str, Any]]:
        """在小时汇总上按 `key` 列聚合（渠道 / 模型 / 用户三个维度共用）。"""
        if key not in _GROUP_KEYS:
            raise ValueError(f"非法分组键: {key!r}")
        where, params = self._where(username=username, since=since, since_col="hour_utc",
                                    until=until, until_col="hour_utc")
        rows = self._db.connect().execute(
            f"""
            SELECT {key} AS group_key, COALESCE(SUM(requests), 0) AS requests,
                   COALESCE(SUM(ok_count), 0) AS ok_count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   SUM(cached_tokens) AS cached_tokens,
                   SUM(cached_known) AS cached_known,
                   SUM(credit_sum) AS credit_sum,
                   SUM(credit_known) AS credit_known,
                   SUM(credit_estimated_known) AS credit_estimated_known,
                   SUM(cost_usd_sum) AS cost_usd_sum,
                   SUM(cost_cny_sum) AS cost_cny_sum,
                   SUM(cost_known) AS cost_known
            FROM usage_hourly {where} GROUP BY {key} ORDER BY {_group_order(sort, order)}
            """, params).fetchall()
        return [self._group_row(row, key) for row in rows]

    def _group_events(self, key: str, *, username: str | None = None,
                      since: int | None = None, until: int | None = None,
                      sort: str | None = None,
                      order: str | None = None, include_credential_name: bool = False,
                      ) -> list[dict[str, Any]]:
        """在明细上按 `key` 列聚合。

        仅凭证维度需要：小时汇总的主键里没有 credential_id，无法还原。代价是
        数据源只保留 90 天（选「全部」时凭证分组比其它维度少），且无凭证的
        预热失败等请求被排除（它们没有可归属的凭证）。

        `include_credential_name`：随行下发凭证昵称与渠道，前端据此在凭证名前
        画渠道 icon 并与请求明细显示同一个名字。昵称取自**全局共享池**（常含
        邮箱/手机），只对 admin/operator 返回（与 `events()` 的 约束一致）。
        """
        if key not in _GROUP_KEYS:
            raise ValueError(f"非法分组键: {key!r}")
        where, params = self._where(username=username, since=since, since_col="ts",
                                    until=until, until_col="ts", alias="e.")
        # 分组键为空的行（无凭证的预热失败等）没有可归属对象，不参与分组；
        # _where 无条件时返回空串，这里补 WHERE 关键词
        clause = f"{where} AND e.{key} IS NOT NULL" if where else f"WHERE e.{key} IS NOT NULL"
        # 渠道优先取凭证当前所属渠道；凭证已被硬删除则回退该组明细里的渠道。
        # 同组跨渠道（凭证改过 provider）时 MAX 取字典序最大的那个，仅用于画
        # icon，不参与统计口径。
        name_expr = "NULLIF(c.nickname, '')" if include_credential_name else "NULL"
        rows = self._db.connect().execute(
            f"""
            SELECT e.{key} AS group_key, COUNT(*) AS requests,
                   SUM(e.ok) AS ok_count,
                   COALESCE(SUM(e.input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(e.output_tokens), 0) AS output_tokens,
                   SUM(e.cached_tokens) AS cached_tokens,
                   SUM(CASE WHEN e.cached_tokens IS NULL THEN 0 ELSE 1 END) AS cached_known,
                   COALESCE(SUM(e.credit), 0) AS credit_sum,
                   SUM(CASE WHEN e.credit IS NULL THEN 0 ELSE 1 END) AS credit_known,
                   SUM(CASE WHEN e.credit_estimated = 1 AND e.credit IS NOT NULL
                            THEN 1 ELSE 0 END) AS credit_estimated_known,
                   COALESCE(SUM(e.cost_usd), 0) AS cost_usd_sum,
                   COALESCE(SUM(e.cost_cny), 0) AS cost_cny_sum,
                   SUM(CASE WHEN e.cost_usd IS NULL THEN 0 ELSE 1 END) AS cost_known,
                   COALESCE(c.provider, MAX(e.provider)) AS provider,
                   {name_expr} AS credential_name
            FROM usage_events e
            LEFT JOIN credentials c ON c.id = e.credential_id
            {clause}
            GROUP BY e.{key} ORDER BY {_group_order(sort, order)}
            """, params).fetchall()
        return [self._group_row(row, key, ("provider", "credential_name")) for row in rows]

    def by_provider(self, *, username: str | None = None,
                    since: int | None = None, until: int | None = None,
                    sort: str | None = None,
                    order: str | None = None) -> list[dict[str, Any]]:
        """按渠道聚合（读小时汇总，与总览/图表同源）。"""
        return self._group_hourly("provider", username=username, since=since, until=until,
                                  sort=sort, order=order)

    def by_model(self, *, username: str | None = None,
                 since: int | None = None, until: int | None = None,
                 sort: str | None = None,
                 order: str | None = None) -> list[dict[str, Any]]:
        """按模型聚合（读小时汇总，与总览/图表同源）。"""
        return self._group_hourly("model", username=username, since=since, until=until,
                                  sort=sort, order=order)

    def by_user(self, *, username: str | None = None,
                since: int | None = None, until: int | None = None,
                sort: str | None = None,
                order: str | None = None) -> list[dict[str, Any]]:
        """按用户聚合（读小时汇总，与总览/图表同源）。"""
        return self._group_hourly("username", username=username, since=since, until=until,
                                  sort=sort, order=order)

    def by_credential(self, *, username: str | None = None,
                      since: int | None = None, until: int | None = None,
                      sort: str | None = None,
                      order: str | None = None,
                      include_credential_name: bool = False) -> list[dict[str, Any]]:
        """按凭证聚合（读明细表：小时汇总主键里没有 credential_id）。

        行里带 `provider`（画渠道 icon）与 `credential_name`（与请求明细同口径
        显示凭证名；未授权/无昵称/凭证已删除时为 None，前端回退显示 ID 前 12 位）。
        """
        return self._group_events("credential_id", username=username, since=since,
                                  until=until, sort=sort, order=order,
                                  include_credential_name=include_credential_name)

    def timeline(self, *, username: str | None = None,
                 since: int | None = None, until: int | None = None,
                 metric: str = "requests") -> list[dict[str, Any]]:
        """按小时的指标时间序列（usage_hourly 聚合，跨 model 汇总）。

        每个点含当日出现过的各渠道指标值（按 metric 变化：请求数 / 总 token /
        平均耗时 ms / 平均首字延迟 ms），供前端绘制曲线。渠道集合动态取自
        数据（新增渠道无需改 SQL），返回结构形如 {hour, codebuddy, trae, qoder}；
        某渠道在该小时没有数据时补 0，保证同一批点的键集合一致，前端画线不
        会因缺键断线。
        """
        expr, _as_mean = self._metric_sql(metric)
        where, params = self._where(username=username, since=since, since_col="hour_utc",
                                    until=until, until_col="hour_utc")
        rows = self._db.connect().execute(
            f"""
            SELECT hour_utc, provider,
                   SUM({expr}) AS value, SUM(ok_count) AS ok_count,
                   SUM(cost_known) AS cost_known
            FROM usage_hourly {where}
            GROUP BY hour_utc, provider
            ORDER BY hour_utc
            """, params).fetchall()
        raw: dict[int, dict[str, Any]] = {}
        providers: set[str] = set()
        for row in rows:
            hour = row["hour_utc"]
            provider = row["provider"]
            providers.add(provider)
            raw.setdefault(hour, {})[provider] = self._metric_value(
                metric, row["value"], row["ok_count"], row["cost_known"])
        # 渠道按名排序：同一批点的键顺序稳定（与渠道注册顺序无关）
        ordered = sorted(providers)
        return [
            {"hour": hour, **{p: raw[hour].get(p, 0) for p in ordered}}
            for hour in sorted(raw)
        ]

    def events(self, *, username: str | None = None, since: int | None = None,
               until: int | None = None,
               before: int | None = None, limit: int = 50,
               include_credential_name: bool = False, sort: str | None = None,
               order: str | None = None, offset: int = 0,
               provider: str | None = None, model: str | None = None,
               credential_id: str | None = None) -> dict[str, Any]:
        """逐请求明细（默认新→旧，rowid 游标分页）。明细仅保留 90 天。

        **两种分页模式**：

        - 默认（`sort=time` 降序、`offset=0`）：rowid 即插入序，稳定且可比大小，
          游标翻页不漏不重；返回 `next_before` 供下一页取「rowid 更小」的记录，
          null 表示到底。
        - 按其它列排序、或时间升序、或显式给 `offset`：改用 `LIMIT/OFFSET` 分页，
          额外返回 `total`（总条数）供前端算页数；`next_before` 恒为 null。

        之所以分两套：任意列排序与「rowid 游标」不兼容（游标只在按 rowid 走时
        成立）。明细只留 90 天，offset 的深翻页成本可接受。

        `provider` / `model` / `credential_id`：钻取过滤（分组表点行 → 只看该
        渠道/模型/凭证的明细）。三者独立、可叠加，`AND` 语义。

        `include_credential_name`：凭证昵称取自**全局共享池**（常含邮箱/手机），
        只对 admin/operator 返回；viewer 即便只看自己的记录也不该看到池里他人
        凭证的昵称。
        """
        key, desc = parse_sort_order(sort, order, EVENT_SORT_COLUMNS,
                                     default_key="time", default_desc=True)
        cursor_mode = key == "time" and desc and offset == 0
        clause = sql_order(sort, order, EVENT_SORT_COLUMNS, default_key="time",
                           default_desc=True, tiebreak="e.rowid")
        name_expr = ("NULLIF(c.nickname, '')" if include_credential_name else "NULL")
        columns = (
            "e.rowid, e.ts, e.username, e.provider, e.credential_id, e.model, "
            "e.ok, e.error_type, e.input_tokens, e.output_tokens, "
            "e.reasoning_tokens, e.cached_tokens, e.credit, e.credit_estimated, "
            "e.cost_usd, e.cost_cny, e.latency_ms, "
            f"e.ttfb_ms, e.request_id, {name_expr} AS credential_name"
        )
        filters = {"provider": provider, "model": model, "credential_id": credential_id}
        if cursor_mode:
            where, params = self._where(username=username, since=since, until=until,
                                        before=before, alias="e.", **filters)
            rows = self._db.connect().execute(
                f"""
                SELECT {columns}
                FROM usage_events e
                LEFT JOIN credentials c ON c.id = e.credential_id
                {where}
                ORDER BY {clause} LIMIT ?
                """, [*params, limit + 1]).fetchall()
            has_more = len(rows) > limit
            page = rows[:limit]
            return {
                "events": [dict(row) for row in page],
                "next_before": page[-1]["rowid"] if has_more and page else None,
                "total": None,
            }
        # offset 模式：before 不参与（游标语义仅对 rowid 降序成立）
        where, params = self._where(username=username, since=since, until=until,
                                    alias="e.", **filters)
        total = self._db.connect().execute(
            f"SELECT COUNT(*) AS n FROM usage_events e {where}", params).fetchone()["n"]
        rows = self._db.connect().execute(
            f"""
            SELECT {columns}
            FROM usage_events e
            LEFT JOIN credentials c ON c.id = e.credential_id
            {where}
            ORDER BY {clause} LIMIT ? OFFSET ?
            """, [*params, limit, max(0, offset)]).fetchall()
        return {
            "events": [dict(row) for row in rows],
            "next_before": None,
            "total": int(total),
        }

    def export_events(self, *, username: str | None = None, since: int | None = None,
                      until: int | None = None,
                      include_credential_name: bool = False,
                      provider: str | None = None, model: str | None = None,
                      credential_id: str | None = None,
                      limit: int = EXPORT_MAX_ROWS) -> list[dict[str, Any]]:
        """导出用明细（旧→新，按 rowid 升序），最多 `limit + 1` 条。

        与 `events()` 的差异：固定**升序**（导出文件从头读起更符合直觉）、不做
        分页、不带 `rowid` 语义。多取一条供调用方判断是否被截断（超上限时明确
        告知用户「收窄条件」，而不是静默给一份不全的文件）。
        """
        where, params = self._where(
            username=username, since=since, until=until, alias="e.",
            provider=provider, model=model, credential_id=credential_id)
        name_expr = ("NULLIF(c.nickname, '')" if include_credential_name else "NULL")
        rows = self._db.connect().execute(
            f"""
            SELECT e.ts, e.username, e.provider, e.credential_id, e.model,
                   e.ok, e.error_type, e.input_tokens, e.output_tokens,
                   e.reasoning_tokens, e.cached_tokens, e.credit, e.credit_estimated,
                   e.cost_usd, e.cost_cny, e.ttfb_ms, e.latency_ms, e.request_id,
                   {name_expr} AS credential_name
            FROM usage_events e
            LEFT JOIN credentials c ON c.id = e.credential_id
            {where}
            ORDER BY e.rowid ASC LIMIT ?
            """, [*params, limit + 1]).fetchall()
        return [dict(row) for row in rows]

    def model_timeline(self, *, username: str | None = None,
                       since: int | None = None, until: int | None = None,
                       top: int = 6,
                       metric: str = "requests") -> dict[str, Any]:
        """按小时的各模型指标趋势（usage_hourly 聚合）。

        Top N 模型按「请求数」排序（排序口径固定，与图表指标解耦）；
        曲线值按 metric 变化（请求数 / 总 token / 平均耗时 / 平均首字延迟）。
        返回结构 {models, points} 不变。
        """
        expr, as_mean = self._metric_sql(metric)
        where, params = self._where(username=username, since=since, since_col="hour_utc",
                                    until=until, until_col="hour_utc")
        rows = self._db.connect().execute(
            f"""
            SELECT hour_utc, model, SUM(requests) AS requests,
                   SUM({expr}) AS value, SUM(ok_count) AS ok_count,
                   SUM(cost_known) AS cost_known
            FROM usage_hourly {where}
            GROUP BY hour_utc, model
            ORDER BY hour_utc
            """, params).fetchall()
        totals: dict[str, int] = {}
        series: dict[str, dict[int, int]] = {}
        hours: list[int] = []
        for row in rows:
            hour, model = row["hour_utc"], row["model"]
            if not hours or hours[-1] != hour:
                hours.append(hour)
            totals[model] = totals.get(model, 0) + row["requests"]
            series.setdefault(model, {})[hour] = (
                self._metric_value(metric, row["value"], row["ok_count"], row["cost_known"]))
        # Top N 模型：按总量降序；总量相同按名字稳定排序
        top_models = sorted(totals, key=lambda m: (-totals[m], m))[:top]
        points = [
            {"hour": hour, **{m: series.get(m, {}).get(hour, 0) for m in top_models}}
            for hour in hours
        ]
        return {"models": top_models, "points": points}
