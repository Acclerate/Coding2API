import { useState } from "react";
import {
  Activity,
  AlertTriangle,
  CircleHelp,
  ExternalLink,
  RadioTower,
  RefreshCw,
} from "lucide-react";
import { useSessionContext } from "../Layout";
import { useUpstreamStatus } from "../api/hooks";
import {
  formatAgo,
  formatDuration,
  formatTime,
  upstreamLevelLabel,
  upstreamLevelTone,
} from "../api/display";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import {
  Badge,
  Checkbox,
  EmptyState,
  Label,
  Metric,
  Notice,
  Panel,
} from "../ui";
import type { UpstreamIncident, UpstreamSource } from "../api/types";

/**
 * 上游官方状态页聚合页（admin only）。
 *
 * 定位：本服务自己的冷却只能回答「这个号现在不行」，回答不了「是上游整体挂了
 * 还是我这边凭证 / 额度的问题」。这一页把各模型 / 网关提供方的官方状态并排
 * 摆出来，补上另一半信息。**只做解释，不做决策**——状态页滞后于我们自己的
 * 实时冷却，看它不该改变调度行为。
 *
 * 数据是后端周期抓取的内存快照（重启归零），前端只轮询回读。
 */

/** 分组标题与顺序：渠道级（与本站渠道一一对应）永远排前面。 */
const SCOPE_GROUPS = [
  { scope: "channel", label: "渠道（与本站入口一一对应）" },
  { scope: "model", label: "模型 / 上游（经渠道间接使用）" },
] as const;

/** 徽标：档位文字 + 色调；`source_stale` 单独加一枚，说明源侧自己声明数据过期。 */
function LevelBadge({ level }: { level: UpstreamSource["level"] }) {
  return (
    <Badge tone={upstreamLevelTone(level)} dot>
      {upstreamLevelLabel(level)}
    </Badge>
  );
}

/** 一条事故：进行中 / 已恢复 + 影响档位 + 更新时间 + 原文链接。 */
function IncidentRow({ incident }: { incident: UpstreamIncident }) {
  return (
    <li className="flex flex-col gap-1 border-t border-border/60 py-2 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={incident.ongoing ? "danger" : "muted"} dot>
          {incident.ongoing ? "进行中" : "已恢复"}
        </Badge>
        <span className="text-xs font-medium text-foreground">{incident.title}</span>
        <Badge tone={upstreamLevelTone(incident.level)}>
          {upstreamLevelLabel(incident.level)}
        </Badge>
        {incident.updated_at !== null && (
          <span className="text-xs text-muted-foreground tabular-nums">
            {formatTime(incident.updated_at)}
          </span>
        )}
        {incident.url && (
          <a
            href={incident.url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-0.5 text-xs text-primary hover:underline"
          >
            原文
            <ExternalLink className="size-3" />
          </a>
        )}
      </div>
      {incident.detail && (
        <p className="text-xs leading-relaxed text-muted-foreground">{incident.detail}</p>
      )}
    </li>
  );
}

/** 一个源的卡片：状态 + 官方页外链 + 组件明细 + 事故列表。 */
function SourceCard({ source }: { source: UpstreamSource }) {
  return (
    <Panel
      data-testid={`upstream-source-${source.key}`}
      title={source.label}
      action={
        <span className="flex flex-wrap items-center gap-2">
          <LevelBadge level={source.level} />
          {source.source_stale && <Badge tone="warn">源侧数据过期</Badge>}
          <a
            href={source.page_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
            data-testid={`upstream-link-${source.key}`}
          >
            官方状态页
            <ExternalLink className="size-3" />
          </a>
        </span>
      }
    >
      <div className="space-y-3">
        {source.error && (
          <div data-testid={`upstream-error-${source.key}`}>
            <Notice tone="muted">本次抓取失败：{source.error}</Notice>
          </div>
        )}

        {source.components.length > 0 && (
          <ul className="space-y-1.5">
            {source.components.map((component) => (
              <li
                key={component.name}
                className="flex flex-wrap items-baseline justify-between gap-2 text-xs"
              >
                <span className="text-foreground">{component.name}</span>
                <span className="flex items-baseline gap-2">
                  {component.detail && (
                    <span className="text-muted-foreground tabular-nums">
                      {component.detail}
                    </span>
                  )}
                  <LevelBadge level={component.level} />
                </span>
              </li>
            ))}
          </ul>
        )}

        {source.components.length === 0 && !source.error && (
          <p className="text-xs text-muted-foreground">
            该源不提供逐组件状态，只有整体档位与事故记录。
          </p>
        )}

        {source.incidents.length > 0 && (
          <div>
            <div className="mb-1 text-xs font-medium text-muted-foreground">近期事故</div>
            <ul>
              {source.incidents.map((incident, index) => (
                <IncidentRow key={`${incident.title}-${index}`} incident={incident} />
              ))}
            </ul>
          </div>
        )}
      </div>
    </Panel>
  );
}

export function UpstreamStatusPage() {
  const session = useSessionContext();
  // 60 秒轮询：上游状态是「现在怎么样」，用户停留时自动刷新。
  const { data, isLoading, isError } = useUpstreamStatus(session.username, 60_000);
  // 只看异常：默认关闭（正常源占多数，全量展示才有「全局都正常」的对照）。
  const [onlyAbnormal, setOnlyAbnormal] = useState(false);

  if (isLoading) {
    return (
      <div data-testid="upstream-status-page">
        <PageSkeleton variant="cards" />
      </div>
    );
  }

  const sources = data?.sources ?? [];
  // 「异常」= 非 operational（含抓取失败的 unknown）：与后端 SourceStatus.abnormal
  // 同一口径。抓不到也是一种要看的异常，不能因为它不是「上游挂了」就藏起来。
  const visible = onlyAbnormal
    ? sources.filter((source) => source.level !== "operational")
    : sources;

  return (
    <div className="space-y-6" data-testid="upstream-status-page">
      <PageHeader
        eyebrow="运维"
        title="上游状态"
        description="各模型 / 网关提供方的官方状态页聚合：判断报错是上游整体故障还是本服务的凭证 / 额度问题。只做解释不做决策——官方状态页滞后于本服务的实时冷却，看它不该改变调度行为。数据只在内存、重启归零。"
        icon={<RadioTower className="size-5" />}
      />

      {isError ? (
        <div data-testid="upstream-status-error">
          <Notice tone="danger">
            上游状态加载失败，请刷新重试；若刚升级服务，请确认后端已重启。
          </Notice>
        </div>
      ) : (
        <>
          <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Metric
              label="整体"
              value={
                data ? upstreamLevelLabel(data.summary.level) : "—"
              }
              tone={
                data && data.summary.abnormal > 0
                  ? "danger"
                  : data && data.summary.unknown > 0
                    ? "warn"
                    : "ok"
              }
              hint={data ? `最差的源决定整体档位（共 ${data.summary.total} 个源）` : undefined}
              icon={<Activity className="size-4" />}
            />
            <Metric
              label="正常"
              value={data?.summary.operational ?? "—"}
              tone="ok"
              hint="档位为 operational 的源数"
              icon={<RadioTower className="size-4" />}
            />
            <Metric
              label="异常"
              value={data?.summary.abnormal ?? "—"}
              tone="danger"
              hint="性能下降 / 部分中断 / 重大故障 / 维护中的源数"
              icon={<AlertTriangle className="size-4" />}
            />
            <Metric
              label="未知"
              value={data?.summary.unknown ?? "—"}
              tone="warn"
              hint="本次抓取失败、拿不到状态的源数（不代表上游真的挂了）"
              icon={<CircleHelp className="size-4" />}
            />
          </section>

          <Panel title="抓取状态">
            <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-xs text-muted-foreground">
              <span className="flex items-center gap-1.5">
                <RefreshCw className="size-3.5" />
                上次抓取：
                <span className="text-foreground">
                  {data?.fetched_at
                    ? `${formatAgo(data.fetched_at, data.server_time)}（${formatTime(data.fetched_at)}）`
                    : "尚未抓取"}
                </span>
              </span>
              <span>
                抓取周期：{data ? formatDuration(data.interval_seconds) : "—"}
              </span>
              <Label className="w-fit cursor-pointer gap-1.5 text-xs font-normal text-muted-foreground">
                <Checkbox
                  checked={onlyAbnormal}
                  data-testid="upstream-abnormal-toggle"
                  onCheckedChange={(value) => setOnlyAbnormal(value === true)}
                />
                只看异常
              </Label>
            </div>
            {data && !data.enabled && (
              <div data-testid="upstream-disabled" className="mt-3">
                <Notice tone="warn">
                  后台抓取已关闭（「任务与配置」可重新打开）。以下为最后一次快照，
                  可能已经过时。
                </Notice>
              </div>
            )}
          </Panel>

          {sources.length === 0 ? (
            <Panel title="上游状态">
              <EmptyState
                icon={<RadioTower className="size-5" />}
                title="尚未抓取到状态"
                description={
                  data?.enabled
                    ? "后台任务会在启动后立刻抓一轮（默认每 5 分钟一次），稍后刷新即可看到各上游的状态。"
                    : "后台抓取任务当前已关闭，可在「任务与配置」页重新打开。"
                }
                data-testid="no-upstream-status"
              />
            </Panel>
          ) : visible.length === 0 ? (
            <Panel title="上游状态">
              <EmptyState
                icon={<RadioTower className="size-5" />}
                title="当前没有异常源"
                description="所有上游状态正常。取消「只看异常」可查看全部源。"
                data-testid="no-upstream-abnormal"
              />
            </Panel>
          ) : (
            SCOPE_GROUPS.map(({ scope, label }) => {
              const rows = visible.filter((source) => source.scope === scope);
              if (rows.length === 0) return null;
              return (
                <section key={scope} className="space-y-3">
                  <h2 className="text-sm font-semibold tracking-tight text-foreground">
                    {label}
                  </h2>
                  <div className="grid gap-3 lg:grid-cols-2">
                    {rows.map((source) => (
                      <SourceCard key={source.key} source={source} />
                    ))}
                  </div>
                </section>
              );
            })
          )}

          <Notice tone="muted">
            官方状态页反映的是提供方的整体健康度，粒度比本服务的模型级冷却粗、且更新滞后。
            这里只做解释，不参与选号 / 熔断；本服务实时冷却见「凭证管理」页。
          </Notice>
        </>
      )}
    </div>
  );
}
