import { useMemo, useState } from "react";
import { Activity, BarChart3, Coins, CreditCard, Download, Gauge, Timer, Wallet, X } from "lucide-react";
import { useSessionContext } from "../Layout";
import { useSort } from "../hooks/useSort";
import {
  useStatsByProvider,
  useStatsByModel,
  useStatsByUser,
  useStatsByCredential,
  useStatsEvents,
  useStatsModelTimeline,
  useStatsOverview,
  useStatsTimeline,
} from "../api/hooks";
import { formatCacheRate, formatCompact, formatCredit, formatLatency, formatMoney, formatNumber, formatTime, usageErrorLabel } from "../api/display";
import { ModelTrendChart } from "../components/ModelTrendChart";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { ProviderIcon } from "../components/ProviderIcon";
import { SortableHead } from "../components/SortableHead";
import { SortMenuHead } from "../components/SortMenuHead";
import { Tip } from "../components/Tip";
import { UsageChart } from "../components/UsageChart";
import { providerLabel } from "../api/providers";
import { api } from "../api/client";
import { buildCustomRange, toLocalInput } from "../lib/customRange";
import type { GroupStats, StatsDrill, UsageEventRow } from "../api/types";
import {
  Button,
  Empty,
  Field,
  Input,
  Metric,
  Notice,
  Panel,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Tabs,
} from "../ui";

const RANGES = [
  { value: "24h", label: "最近 24 小时", seconds: 86400 },
  { value: "7d", label: "最近 7 天", seconds: 7 * 86400 },
  { value: "30d", label: "最近 30 天", seconds: 30 * 86400 },
  { value: "all", label: "全部", seconds: 0 },
  // 自定义：两个 datetime-local 输入，起止任意。以「本地时区、精确到分钟」为准。
  { value: "custom", label: "自定义", seconds: -1 },
];

/** 图表指标筛选：默认请求次数；均值类（耗时/首字）后端按成功请求归一。 */
const METRICS = [
  { value: "requests", label: "请求次数", icon: <BarChart3 className="size-3.5" /> },
  { value: "tokens", label: "Token", icon: <Coins className="size-3.5" /> },
  { value: "cost", label: "成本", icon: <Wallet className="size-3.5" /> },
  { value: "latency", label: "耗时", icon: <Timer className="size-3.5" /> },
  { value: "ttfb", label: "首字延迟", icon: <Gauge className="size-3.5" /> },
];

/** 明细分页的可选每页条数。 */
const PAGE_SIZES = [10, 20, 50, 100];

/**
 * 请求明细的合并列：把同类列并成一列，默认值一眼可读，悬停展开全部子项。
 *
 * Token = 输入 / 输出 / 命中 / 命中率；延迟 = 耗时 / 首字。合并把列数从 14 压到
 * 约 10（窄屏卡片也少一半字段），排序能力靠列头下拉选子列保留（见 SortMenuHead）。
 */
const TOKEN_COLUMNS = [
  { key: "input_tokens", label: "输入" },
  { key: "output_tokens", label: "输出" },
  { key: "cached_tokens", label: "命中" },
  { key: "cache_rate", label: "命中率" },
] as const;

const LATENCY_COLUMNS = [
  { key: "latency_ms", label: "耗时" },
  { key: "ttfb_ms", label: "首字" },
] as const;

/**
 * 分组统计的维度。后端各维度的行结构一致（分组键 + 同一组公共指标），
 * 前端按 key 取出分组键列即可，表格本身不分叉。
 *
 * 用户/凭证维度标记 operatorOnly：viewer 只能看自己，恒是「我」一行，
 * 给这个 tab 只是噪音。
 */
const GROUP_DIMENSIONS = [
  { value: "provider", label: "按渠道", groupKey: "provider", label2: "渠道", operatorOnly: false },
  { value: "model", label: "按模型", groupKey: "model", label2: "模型", operatorOnly: false },
  { value: "user", label: "按用户", groupKey: "username", label2: "用户", operatorOnly: true },
  { value: "credential", label: "按凭证", groupKey: "credential_id", label2: "凭证", operatorOnly: true },
] as const;

/** 公共指标 + 至多一个分组键（键名随维度变化，用可选字段统一承载）。 */
type GroupRow = GroupStats & {
  provider?: string;
  model?: string;
  username?: string;
  credential_id?: string;
  credential_name?: string | null;
};

/** 凭证文案：昵称优先，回落 ID 前 12 位（ID 很长）。分组表与明细表共用，
 *  保证同一凭证在两处显示成同一个名字。 */
function credentialCellLabel(name: string | null | undefined, id: string | null | undefined) {
  return name ?? (id ? id.slice(0, 12) : "—");
}

/** 明细状态列：成功固定文案；失败展示受控错误类型的中文说明（脱敏，不含原始错误体）。 */
function statusCell(row: UsageEventRow) {
  if (row.ok) {
    return <span className="text-ok">成功</span>;
  }
  return <span className="text-destructive">{usageErrorLabel(row.error_type)}</span>;
}

/**
 * Token 合并列：默认「输入 / 输出 · 命中率」，悬停展开命中 token。
 *
 * 命中率复用 `formatCacheRate`（未上报 / 输入为 0 显示 —）；悬停详情用 `Tip`，
 * 窄屏卡片模式下同样可用（点击单元格即可唤出）。
 */
function tokenCell(row: UsageEventRow) {
  const rate = formatCacheRate(row.cached_tokens, row.input_tokens);
  return (
    <Tip content={
      `输入 ${formatCompact(row.input_tokens)} · 输出 ${formatCompact(row.output_tokens)}`
      + ` · 命中 ${formatCompact(row.cached_tokens)} · 命中率 ${rate}`
    }>
      <span className="tabular-nums">
        {formatCompact(row.input_tokens)} / {formatCompact(row.output_tokens)}
        <span className="text-muted-foreground"> · {rate}</span>
      </span>
    </Tip>
  );
}

/** 延迟合并列：默认「耗时 / 首字」，悬停补字段名（两个都是 ms/s 的数字，靠提示消歧）。 */
function latencyCell(row: UsageEventRow) {
  return (
    <Tip content={`耗时 ${formatLatency(row.latency_ms)} · 首字延迟 ${formatLatency(row.ttfb_ms)}`}>
      <span className="tabular-nums">
        {formatLatency(row.latency_ms)}
        <span className="text-muted-foreground"> / {formatLatency(row.ttfb_ms)}</span>
      </span>
    </Tip>
  );
}

export function StatsPage() {
  const session = useSessionContext();
  // 默认最近 24 小时：「这个池子现在怎么样」是打开用量统计的第一问题，
  // 7 天会把当天的波动摊平（与凭证池页的实时气质一致）。
  const [range, setRange] = useState("24h");
  // 自定义范围的原始输入（`datetime-local` 文本）。只在 range==="custom" 时参与取数。
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const [metric, setMetric] = useState("requests");
  const [groupDimension, setGroupDimension] = useState("provider");
  // 分组统计默认按请求数降序（「用得最多」才是分组表的用途）
  const groupSort = useSort("requests", "desc", { group: "asc" });
  // 明细默认按时间（rowid）降序 → 走游标分页；换列则切 offset 分页
  const eventSort = useSort("time", "desc");
  // 明细分页：cursors[i] = 第 i 页的 before 游标（第 0 页为 undefined）；
  // rowid 单调递增，游标栈支持双向翻页且不漏不重
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [cursors, setCursors] = useState<(number | undefined)[]>([undefined]);
  // 钻取：点分组表的行 → 只看该渠道/模型/用户/凭证的明细。空对象 = 未钻取。
  const [drill, setDrill] = useState<StatsDrill>({});
  // 导出反馈（行数/截断/失败），一次性展示，再次导出即覆盖
  const [exportNotice, setExportNotice] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  // 游标分页只在「按 rowid 降序」时成立：换列即改用 offset 分页
  const cursorMode = eventSort.sort === "time" && eventSort.order === "desc";
  const eventBefore = cursorMode
    ? cursors[Math.min(pageIndex, cursors.length - 1)]
    : undefined;
  // cursor 模式不发 offset（省一个无意义参数，也让后端走游标分支更明确）
  const eventOffset = cursorMode ? undefined : pageIndex * pageSize;

  const seconds = RANGES.find((item) => item.value === range)?.seconds ?? 0;
  // since 必须锚定：若每次渲染都重算 Date.now()-seconds，值会随时间漂移，
  // 导致 queryKey 抖动重复请求，且「范围变化回第一页」的 effect 把翻页打回第一页
  const since = useMemo(
    () => (seconds > 0 ? Math.floor(Date.now() / 1000) - seconds : undefined),
    [seconds],
  );
  // 自定义范围：解析失败 / 起点≥终点时为 null（端点会提示，且不参与取数）
  const custom = useMemo(
    () => (range === "custom" ? buildCustomRange(customFrom, customTo) : null),
    [range, customFrom, customTo],
  );
  // 自定义模式下以解析结果为准；否则用预设范围的 since（无 until）
  const effectiveSince = range === "custom" ? custom?.since : since;
  const effectiveUntil = range === "custom" ? custom?.until : undefined;
  // 自定义输入非法（两个框都填了却解析不出）时，提示用户而不是静默退回全量
  const customInvalid = range === "custom" && custom === null
    && (customFrom.trim() !== "" || customTo.trim() !== "");

  const events = useStatsEvents(
    drill.username, effectiveSince, effectiveUntil, eventBefore, pageSize,
    eventSort.sort, eventSort.order, eventOffset,
    { provider: drill.provider, model: drill.model,
      credential_id: drill.credential_id });
  const eventRows = events.data?.events ?? [];
  // 游标模式看 next_before；offset 模式看 total（还有下一页 = 已加载 < 总数）
  const hasNextPage = cursorMode
    ? events.data
      ? events.data.next_before !== null
      : false
    : eventRows.length + pageIndex * pageSize < (events.data?.total ?? 0);

  // 切时间范围 = 换了数据集：事件驱动重置分页（不监听 since 派生值，
  // 否则值随时间漂移会把正常翻页误重置）
  const changeRange = (value: string) => {
    // 首次切到自定义且两个框都空：预填「最近 24 小时」，否则会先按全量查一次
    if (value === "custom" && !customFrom && !customTo) {
      const now = new Date();
      const start = new Date(now.getTime() - 86400 * 1000);
      setCustomFrom(toLocalInput(start));
      setCustomTo(toLocalInput(now));
    }
    setRange(value);
    setPageIndex(0);
    setCursors([undefined]);
  };

  const changePageSize = (size: number) => {
    setPageSize(size);
    setPageIndex(0);
    setCursors([undefined]);
  };

  const toggleEventSort = (columnKey: string) => {
    eventSort.toggle(columnKey);
    setPageIndex(0);
    setCursors([undefined]);
  };

  /**
   * 应用钻取：**合并**而非替换——分组表各维度独立（渠道/模型/用户/凭证），
   * 依次点行即可层层收窄（provider=trae AND model=glm-5.2），这正是钻取
   * 相对「跳转看全量」的价值。换过滤即换数据集，分页回到第一页。
   */
  const applyDrill = (next: StatsDrill) => {
    setDrill((prev) => ({ ...prev, ...next }));
    setPageIndex(0);
    setCursors([undefined]);
  };

  const clearDrill = () => {
    setDrill({});
    setPageIndex(0);
    setCursors([undefined]);
  };

  const drillLabels: string[] = [];
  if (drill.username) drillLabels.push(`用户：${drill.username}`);
  if (drill.provider) drillLabels.push(`渠道：${providerLabel(drill.provider)}`);
  if (drill.model) drillLabels.push(`模型：${drill.model}`);
  if (drill.credential_id) drillLabels.push(`凭证：${drill.credential_id.slice(0, 12)}`);

  const exportCsv = async () => {
    setExporting(true);
    setExportNotice(null);
    try {
      const { blob, rowCount, truncated } = await api.statsEventsCsv(
        drill.username, effectiveSince, effectiveUntil,
        { provider: drill.provider, model: drill.model,
          credential_id: drill.credential_id });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `coding2api-events-${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      setExportNotice(truncated
        ? `已导出 ${rowCount} 条，达到上限被截断；请缩小时间范围或加筛选后重试。`
        : `已导出 ${rowCount} 条明细。`);
    } catch (error) {
      setExportNotice(error instanceof Error ? `导出失败：${error.message}` : "导出失败。");
    } finally {
      setExporting(false);
    }
  };

  const overview = useStatsOverview(session.username, undefined, effectiveSince,
                                    effectiveUntil);
  const byProvider = useStatsByProvider(
    session.username, undefined, effectiveSince, effectiveUntil,
    groupSort.sort, groupSort.order);
  const byModel = useStatsByModel(
    session.username, undefined, effectiveSince, effectiveUntil,
    groupSort.sort, groupSort.order);
  const byUser = useStatsByUser(
    session.username, undefined, effectiveSince, effectiveUntil,
    groupSort.sort, groupSort.order);
  const byCredential = useStatsByCredential(
    session.username, undefined, effectiveSince, effectiveUntil,
    groupSort.sort, groupSort.order);
  const timeline = useStatsTimeline(session.username, undefined, effectiveSince,
                                    effectiveUntil, metric);
  const modelTimeline = useStatsModelTimeline(session.username, undefined, effectiveSince,
                                              effectiveUntil, metric);

  // 四个维度一次性取数（各维度行结构一致），切 tab 只换渲染、不再发请求；
  // viewer 看不到用户/凭证 tab，若状态残留则回落到渠道维度
  const groupDimensions = GROUP_DIMENSIONS.filter(
    (item) => session.is_operator || !item.operatorOnly);
  const group = groupDimensions.find((item) => item.value === groupDimension)
    ?? groupDimensions[0];
  const groupKey = group.groupKey;
  const groupLabel = group.label2;
  const groupRows: GroupRow[] = group.value === "provider"
    ? (byProvider.data?.providers ?? [])
    : group.value === "model"
      ? (byModel.data?.models ?? [])
      : group.value === "user"
        ? (byUser.data?.users ?? [])
        : (byCredential.data?.credentials ?? []);

  if (overview.isLoading) {
    return (
      <div data-testid="stats-page">
        <PageSkeleton variant="chart" rows={4} />
      </div>
    );
  }
  const stats = overview.data;
  const rate =
    stats?.success_rate === null || stats?.success_rate === undefined
      ? null
      : stats.success_rate;
  const rateText = rate === null ? "—" : `${(rate * 100).toFixed(1)}%`;
  const totalTokens = (stats?.input_tokens ?? 0) + (stats?.output_tokens ?? 0);
  // 输入缓存：命中 = 上报的 cached_tokens；未命中 = 输入 − 命中（缺上报则显示 —）
  const cached = stats?.cached_tokens ?? null;
  const hitText = cached === null ? "—" : formatCompact(cached);
  const missText =
    cached === null || stats?.input_tokens === null || stats?.input_tokens === undefined
      ? "—"
      : formatCompact(stats.input_tokens - cached);
  const tokenHint =
    `输入 ${formatCompact(stats?.input_tokens)}（命中 ${hitText} · 未命中 ${missText}）`
    + ` · 输出 ${formatCompact(stats?.output_tokens)} · 推理 ${formatCompact(stats?.reasoning_tokens)}`
    + ` · 缓存命中率 ${formatCacheRate(cached, stats?.input_tokens)}`;

  return (
    <div className="space-y-6" data-testid="stats-page">
      <PageHeader
        eyebrow="控制台"
        title="用量统计"
        description="按时间范围查看请求量、成功率与 token 消耗；数据按 API Key 归属用户统计，管理员与操作员可看全量，只读用户只能看自己。"
        icon={<BarChart3 className="size-5" />}
      />

      <Panel title="筛选">
        {/* 「时间范围」label 后紧接 tabs（左右相邻，不拉开两端） */}
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-sm font-medium text-muted-foreground">时间范围</span>
          <Tabs
            value={range}
            options={RANGES.map(({ value, label }) => ({ value, label }))}
            onChange={changeRange}
            testId="range-tabs"
          />
        </div>
        {/* 自定义范围：两个 datetime-local，精确到分钟、按浏览器本地时区解析。
            起点晚于终点时提示并暂停取数（见 buildCustomRange）。 */}
        {range === "custom" && (
          <div className="mt-3 flex flex-wrap items-end gap-3" data-testid="custom-range">
            <Field label="开始时间">
              <Input
                type="datetime-local"
                value={customFrom}
                data-testid="custom-from"
                className="w-52"
                onChange={(event) => {
                  setCustomFrom(event.target.value);
                  setPageIndex(0);
                  setCursors([undefined]);
                }}
              />
            </Field>
            <Field label="结束时间">
              <Input
                type="datetime-local"
                value={customTo}
                data-testid="custom-to"
                className="w-52"
                onChange={(event) => {
                  setCustomTo(event.target.value);
                  setPageIndex(0);
                  setCursors([undefined]);
                }}
              />
            </Field>
            {customInvalid && (
              <p className="pb-2 text-xs text-destructive" data-testid="custom-range-invalid">
                开始时间须早于结束时间，且格式有效；已暂停取数。
              </p>
            )}
          </div>
        )}
      </Panel>

      <section
        aria-label="用量概览统计"
        className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5"
      >
        <Metric label="请求数" value={formatNumber(stats?.requests)} tone="ok"
                hint={`成功率 ${rateText}`} icon={<Activity className="size-4" />} />
        <Metric
          label="Token 消耗"
          value={formatCompact(totalTokens)}
          hint={tokenHint}
          icon={<Coins className="size-4" />}
        />
        <Metric
          label="平均耗时"
          value={formatLatency(stats?.avg_latency_ms)}
          hint={`首字延迟 ${formatLatency(stats?.avg_ttfb_ms)}`}
          icon={<Timer className="size-4" />}
        />
        <Metric
          label="Credit 消耗"
          value={formatCredit(stats?.credit, stats?.credit_estimated)}
          hint="渠道可选字段，可能不返回；≈ 为推算值（TRAE 按单价、CodeArts 福利模型按每日池）"
          icon={<CreditCard className="size-4" />}
        />
        <Metric
          label="成本（估算）"
          value={formatMoney(stats?.cost_cny, "CNY")}
          hint={`美元 ${formatMoney(stats?.cost_usd, "USD")} · 按 OpenRouter 刊例价折算，未匹配定价的请求不计`}
          icon={<Wallet className="size-4" />}
        />
      </section>

      <section className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-medium text-muted-foreground">图表指标</h2>
        <Tabs
          value={metric}
          options={METRICS.map(({ value, label }) => ({ value, label }))}
          onChange={setMetric}
          testId="metric-tabs"
        />
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <Panel title="请求量趋势">
          {(timeline.data?.points.length ?? 0) === 0 ? (
            <Empty>该范围内没有小时汇总数据</Empty>
          ) : (
            <UsageChart points={timeline.data?.points ?? []} metric={metric} />
          )}
        </Panel>

        <Panel title="按模型趋势" action={
          modelTimeline.data?.models.length ? (
            <span className="text-xs text-muted-foreground">
              请求量 Top {modelTimeline.data.models.length} 模型
            </span>
          ) : undefined
        }>
          {(modelTimeline.data?.points.length ?? 0) === 0 ? (
            <Empty data-testid="no-model-trend">该范围内没有小时汇总数据</Empty>
          ) : (
            <ModelTrendChart
              points={modelTimeline.data?.points ?? []}
              models={modelTimeline.data?.models ?? []}
              metric={metric}
            />
          )}
        </Panel>
      </section>

      <Panel title="分组统计" action={
        <Tabs
          value={groupDimension}
          options={groupDimensions.map(({ value, label }) => ({ value, label }))}
          onChange={setGroupDimension}
          testId="group-tabs"
        />
      }>
        {groupRows.length === 0 ? (
          <Empty data-testid="no-group-stats">该范围内没有请求</Empty>
        ) : (
          <Table data-testid="group-table" cards>
            <TableHeader>
              <TableRow>
                <SortableHead label={group.value === "provider" ? "渠道" : groupLabel}
                              columnKey="group" active={groupSort.sort === "group"}
                              direction={groupSort.order} onToggle={groupSort.toggle}
                              testId="sort-group" />
                <SortableHead label="请求数" columnKey="requests"
                              active={groupSort.sort === "requests"} direction={groupSort.order}
                              onToggle={groupSort.toggle} align="right" testId="sort-requests" />
                <SortableHead label="成功数" columnKey="ok_count"
                              active={groupSort.sort === "ok_count"} direction={groupSort.order}
                              onToggle={groupSort.toggle} align="right" testId="sort-ok_count" />
                <TableHead className="text-right">成功率</TableHead>
                <SortableHead label="输入 token" columnKey="input_tokens"
                              active={groupSort.sort === "input_tokens"}
                              direction={groupSort.order} onToggle={groupSort.toggle}
                              align="right" testId="sort-input_tokens" />
                <SortableHead label="输出 token" columnKey="output_tokens"
                              active={groupSort.sort === "output_tokens"}
                              direction={groupSort.order} onToggle={groupSort.toggle}
                              align="right" testId="sort-output_tokens" />
                <SortableHead label="命中缓存" columnKey="cached_tokens"
                              active={groupSort.sort === "cached_tokens"}
                              direction={groupSort.order} onToggle={groupSort.toggle}
                              align="right" testId="sort-cached_tokens" />
                {/* 命中率 = 命中 ÷ 输入，派生列不排序（与「成功率」同例） */}
                <TableHead className="text-right">命中率</TableHead>
                <SortableHead label="Credit" columnKey="credit"
                              active={groupSort.sort === "credit"} direction={groupSort.order}
                              onToggle={groupSort.toggle} align="right" testId="sort-credit" />
                <SortableHead label="成本" columnKey="cost_cny"
                              active={groupSort.sort === "cost_cny"} direction={groupSort.order}
                              onToggle={groupSort.toggle} align="right" testId="sort-cost_cny" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {groupRows.map((row) => {
                // 点行 = 钻取到该维度的明细；分组键 → 明细过滤字段的映射：
                // provider/model/credential_id 直接同名，用户维度走作用域 username
                const keyValue = String(row[groupKey]);
                const next: StatsDrill = group.value === "user"
                  ? { username: keyValue }
                  : group.value === "provider" ? { provider: keyValue }
                    : group.value === "model" ? { model: keyValue }
                      : { credential_id: keyValue };
                return (
                <TableRow key={keyValue}
                          data-testid={`group-row-${keyValue}`}
                          className="cursor-pointer"
                          tabIndex={0}
                          role="button"
                          aria-label={`查看 ${keyValue} 的请求明细`}
                          onClick={() => applyDrill(next)}
                          onKeyDown={(event) => {
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault();
                              applyDrill(next);
                            }
                          }}>
                  <TableCell label={group.value === "provider" ? "渠道" : groupLabel}
                             className="max-w-48 truncate"
                             title={groupKey === "credential_id"
                               ? (row.credential_id ?? undefined)
                               : keyValue}>
                    {groupKey === "provider" ? (
                      <span className="inline-flex items-center gap-1.5">
                        <ProviderIcon provider={String(row.provider)} size={13} />
                        {providerLabel(String(row.provider))}
                      </span>
                    ) : groupKey === "credential_id" ? (
                      // 渠道 icon + 凭证名：与请求明细的凭证列同口径
                      <span className="inline-flex items-center gap-1.5">
                        <ProviderIcon provider={String(row.provider)} size={13} />
                        {credentialCellLabel(row.credential_name, row.credential_id)}
                      </span>
                    ) : (
                      keyValue
                    )}
                  </TableCell>
                  <TableCell label="请求数" className="text-right tabular-nums">
                    {formatNumber(row.requests)}
                  </TableCell>
                  <TableCell label="成功数" className="text-right tabular-nums">
                    {formatNumber(row.ok_count)}
                  </TableCell>
                  {/* 成功率：请求数为 0 无意义（不渲染 0% 冒充数据） */}
                  <TableCell label="成功率" className="text-right tabular-nums" data-testid="group-success-rate">
                    {row.requests > 0
                      ? `${((row.ok_count / row.requests) * 100).toFixed(1)}%`
                      : "—"}
                  </TableCell>
                  <TableCell label="输入 token" className="text-right tabular-nums">
                    {formatCompact(row.input_tokens)}
                  </TableCell>
                  <TableCell label="输出 token" className="text-right tabular-nums">
                    {formatCompact(row.output_tokens)}
                  </TableCell>
                  <TableCell label="命中缓存" className="text-right tabular-nums">
                    {formatCompact(row.cached_tokens)}
                  </TableCell>
                  <TableCell label="命中率" className="text-right tabular-nums"
                             data-testid="group-cache-rate">
                    {formatCacheRate(row.cached_tokens, row.input_tokens)}
                  </TableCell>
                  <TableCell label="Credit" className="text-right tabular-nums">
                    {formatCredit(row.credit, row.credit_estimated)}
                  </TableCell>
                  <TableCell label="成本" className="text-right tabular-nums"
                             title={`美元 ${formatMoney(row.cost_usd, "USD")}`}>
                    {formatMoney(row.cost_cny, "CNY")}
                  </TableCell>
                </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
        <p className="mt-2 text-xs text-muted-foreground">
          {groupKey === "credential_id"
            ? "凭证分组取自逐请求明细（仅保留 90 天），无凭证的预热失败等请求不计入。"
            : "各维度数据源为小时汇总（永久保留），最近 5 分钟内的请求可能尚未计入。"}
          {" "}点任意一行可钻取到下方「请求明细」。
        </p>
      </Panel>

      <Panel data-testid="events-panel" title="请求明细" action={
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span>共 {formatNumber(overview.data?.requests)} 条 · 保留 90 天</span>
          <label className="flex items-center gap-1.5">
            每页
            <Select
              value={String(pageSize)}
              data-testid="events-page-size"
              className="h-7 w-20 text-xs"
              onChange={(event) => changePageSize(Number(event.target.value))}
            >
              {PAGE_SIZES.map((size) => (
                <option key={size} value={size}>{size} 条</option>
              ))}
            </Select>
          </label>
          {/* 导出限 admin/operator：整份明细落盘比翻页浏览的暴露面大 */}
          {session.is_operator && (
            <Button size="sm" variant="ghost" data-testid="events-export"
                    disabled={exporting} onClick={exportCsv}>
              <Download className="size-3.5" />
              {exporting ? "导出中…" : "导出 CSV"}
            </Button>
          )}
        </div>
      }>
        {drillLabels.length > 0 && (
          <div className="mb-3 flex flex-wrap items-center gap-2" data-testid="drill-filters">
            <span className="text-xs text-muted-foreground">钻取过滤</span>
            {drillLabels.map((label) => (
              <span key={label}
                    className="inline-flex items-center rounded-md bg-secondary px-2 py-0.5 text-xs text-secondary-foreground">
                {label}
              </span>
            ))}
            <Button size="sm" variant="ghost" data-testid="drill-clear"
                    onClick={clearDrill}>
              <X className="size-3.5" />
              清除
            </Button>
          </div>
        )}
        {exportNotice && (
          <div data-testid="export-notice">
            <Notice tone="muted" className="mb-3 text-xs">{exportNotice}</Notice>
          </div>
        )}
        {events.isError ? (
          <Notice tone="danger" data-testid="events-error">
            请求明细加载失败，请刷新重试；若刚升级服务，请确认后端已重启加载新端点。
          </Notice>
        ) : eventRows.length === 0 ? (
          <Empty data-testid="no-events">该范围内没有请求明细</Empty>
        ) : (
          <>
            <Table data-testid="events-table" cards>
              <TableHeader>
                <TableRow>
                  <SortableHead label="时间" columnKey="ts" active={eventSort.sort === "ts"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                testId="event-sort-ts" />
                  {session.is_operator && (
                    <SortableHead label="用户" columnKey="username"
                                  active={eventSort.sort === "username"}
                                  direction={eventSort.order} onToggle={toggleEventSort}
                                  testId="event-sort-username" />
                  )}
                  <SortableHead label="渠道" columnKey="provider"
                                active={eventSort.sort === "provider"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                testId="event-sort-provider" />
                  <SortableHead label="凭证" columnKey="credential"
                                active={eventSort.sort === "credential"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                testId="event-sort-credential" />
                  <SortableHead label="模型" columnKey="model" active={eventSort.sort === "model"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                testId="event-sort-model" />
                  <SortableHead label="状态" columnKey="ok" active={eventSort.sort === "ok"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                testId="event-sort-ok" />
                  {/* Token 合并列：输入/输出/命中/命中率，列头下拉选子列排序 */}
                  <SortMenuHead label="Token" options={[...TOKEN_COLUMNS]}
                                activeKey={eventSort.sort} direction={eventSort.order}
                                onToggle={toggleEventSort} testId="event-sort-token" />
                  <SortableHead label="Credit" columnKey="credit"
                                active={eventSort.sort === "credit"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                align="right" testId="event-sort-credit" />
                  <SortableHead label="成本" columnKey="cost_cny"
                                active={eventSort.sort === "cost_cny"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                align="right" testId="event-sort-cost_cny" />
                  {/* 延迟合并列：耗时/首字，列头下拉选子列排序 */}
                  <SortMenuHead label="延迟" options={[...LATENCY_COLUMNS]}
                                activeKey={eventSort.sort} direction={eventSort.order}
                                onToggle={toggleEventSort} testId="event-sort-latency" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {eventRows.map((row) => (
                  <TableRow key={row.rowid} data-testid="event-row">
                    <TableCell label="时间" className="whitespace-nowrap tabular-nums"
                               title={row.request_id ? `请求 ID：${row.request_id}` : undefined}>
                      {formatTime(row.ts)}
                    </TableCell>
                    {session.is_operator && (
                      <TableCell label="用户">{row.username}</TableCell>
                    )}
                    <TableCell label="渠道">
                      <span className="inline-flex items-center gap-1.5">
                        <ProviderIcon provider={row.provider} size={13} />
                        {providerLabel(row.provider)}
                      </span>
                    </TableCell>
                    <TableCell label="凭证" className="max-w-40 truncate" title={row.credential_id ?? undefined}>
                      {credentialCellLabel(row.credential_name, row.credential_id)}
                    </TableCell>
                    <TableCell label="模型" className="max-w-48 truncate" title={row.model}>
                      {row.model}
                    </TableCell>
                    <TableCell label="状态">{statusCell(row)}</TableCell>
                    <TableCell label="Token" className="text-right">
                      {tokenCell(row)}
                    </TableCell>
                    <TableCell label="Credit" className="text-right tabular-nums">
                      {formatCredit(row.credit, row.credit_estimated)}
                    </TableCell>
                    <TableCell label="成本" className="text-right tabular-nums"
                               title={`美元 ${formatMoney(row.cost_usd, "USD")}`}>
                      {formatMoney(row.cost_cny, "CNY")}
                    </TableCell>
                    <TableCell label="延迟" className="text-right">
                      {latencyCell(row)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {eventRows.length > 0 && (
              <div className="mt-3 flex items-center justify-between">
                <span className="text-xs text-muted-foreground" data-testid="events-page-info">
                  第 {pageIndex + 1} 页
                </span>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="ghost"
                    data-testid="events-prev"
                    disabled={pageIndex === 0 || events.isFetching}
                    onClick={() => setPageIndex((p) => Math.max(0, p - 1))}
                  >
                    上一页
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    data-testid="events-next"
                    disabled={!hasNextPage || events.isFetching}
                    onClick={() => {
                      if (cursorMode) {
                        const nextBefore = events.data?.next_before;
                        if (nextBefore === null || nextBefore === undefined) return;
                        setCursors((prev) => {
                          const next = prev.slice();
                          next[pageIndex + 1] = nextBefore;
                          return next;
                        });
                      }
                      setPageIndex((p) => p + 1);
                    }}
                  >
                    下一页
                  </Button>
                </div>
              </div>
            )}          </>
        )}
      </Panel>

      <Notice tone="muted" className="items-start gap-1 px-3 py-2.5 text-xs">
        <p>
          隐私：不保存提示词、回答、请求头、Token、工具参数与原始错误体；逐请求明细保留 90 天，小时汇总永久保留；按 API Key 归属用户统计。
        </p>
        <p>
          credit 为渠道可选字段，经常不返回；TRAE 上游不给单请求积分，带 ≈ 的数值是按官方单价折算；CodeArts 福利模型按每日 token 池 1:1 扣减后折成积分（1 积分 = 10000 token），带 ≈ 的值即该请求折算后的积分（CodeBuddy 为上游返回的真值）。健康度只依赖额度探测接口，主指标是 token 数。缓存命中率 = 命中 token ÷ 输入 token，仅在上报过缓存时展示。
        </p>
        <p>
          成本为估算：按 OpenRouter 的模型刊例价（USD / 百万 token）与本服务写入时生效的美元汇率折算，展示人民币、括号内为美元；不是上游真实扣费，未匹配到定价的模型不计入（显示 —）。汇率可在「任务与配置」页热更，仅影响之后写入的请求，历史成本不重算。
        </p>
      </Notice>
    </div>
  );
}