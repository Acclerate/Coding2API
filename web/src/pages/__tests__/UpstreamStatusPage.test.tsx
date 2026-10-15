import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { UpstreamStatusPage } from "../UpstreamStatusPage";
import { renderPage, settle } from "@/test-utils";
import type { UpstreamSource, UpstreamStatusResponse } from "@/api/types";

function stubFetch(body: unknown, status = 200) {
  const spy = vi.fn(async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", spy);
  return spy;
}

function source(overrides: Partial<UpstreamSource> = {}): UpstreamSource {
  return {
    key: "kilo",
    label: "Kilo",
    scope: "channel",
    page_url: "https://status.kilo.ai",
    related_providers: ["kilo"],
    level: "operational",
    fetched_at: 1_700_000_000,
    error: null,
    source_stale: false,
    components: [],
    incidents: [],
    ...overrides,
  };
}

function payload(overrides: Partial<UpstreamStatusResponse> = {}): UpstreamStatusResponse {
  return {
    summary: { level: "operational", operational: 1, abnormal: 0, unknown: 0, total: 1 },
    fetched_at: 1_700_000_000,
    sources: [source()],
    enabled: true,
    interval_seconds: 300,
    server_time: 1_700_000_030,
    ...overrides,
  };
}

describe("UpstreamStatusPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染概览四格与源的档位、官方页外链", async () => {
    stubFetch(payload({
      summary: { level: "major", operational: 1, abnormal: 1, unknown: 0, total: 2 },
      sources: [
        source(),
        source({ key: "openai", label: "OpenAI", scope: "model",
                 page_url: "https://status.openai.com", level: "major",
                 related_providers: [] }),
      ],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("upstream-status-page")).toBeInTheDocument();
    // 整体档位取最差源 → 概览第一格显示「重大故障」
    expect(screen.getByText("最差的源决定整体档位（共 2 个源）")).toBeInTheDocument();
    const card = screen.getByTestId("upstream-source-openai");
    expect(card).toHaveTextContent("OpenAI");
    expect(card).toHaveTextContent("重大故障");
    const link = screen.getByTestId("upstream-link-openai");
    expect(link).toHaveAttribute("href", "https://status.openai.com");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("渠道组排在模型组之前，两组标题都出现", async () => {
    stubFetch(payload({
      sources: [
        source(),
        source({ key: "openai", label: "OpenAI", scope: "model", related_providers: [] }),
      ],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    const channel = screen.getByText("渠道（与本站入口一一对应）");
    const model = screen.getByText("模型 / 上游（经渠道间接使用）");
    // compareDocumentPosition：渠道标题必须先于模型标题出现
    expect(channel.compareDocumentPosition(model) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
  });

  it("组件明细与事故列表：进行中 / 已恢复、原文链接", async () => {
    stubFetch(payload({
      sources: [source({
        level: "partial",
        components: [
          { name: "Gateway", level: "partial", detail: "TTFT 2.3s" },
          { name: "Embeddings", level: "operational", detail: null },
        ],
        incidents: [
          { title: "Elevated errors", level: "major", ongoing: true,
            updated_at: 1_700_000_000, url: "https://stspg.io/x", detail: "正在调查" },
          { title: "旧事故", level: "degraded", ongoing: false,
            updated_at: null, url: null, detail: null },
        ],
      })],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    const card = screen.getByTestId("upstream-source-kilo");
    expect(card).toHaveTextContent("Gateway");
    expect(card).toHaveTextContent("TTFT 2.3s");
    expect(card).toHaveTextContent("Elevated errors");
    expect(card).toHaveTextContent("进行中");
    expect(card).toHaveTextContent("已恢复");
    expect(card).toHaveTextContent("正在调查");
    expect(screen.getByRole("link", { name: /原文/ }))
      .toHaveAttribute("href", "https://stspg.io/x");
  });

  it("抓取失败的源显示错误原因，而不是空卡片", async () => {
    stubFetch(payload({
      summary: { level: "unknown", operational: 0, abnormal: 1, unknown: 1, total: 1 },
      sources: [source({ level: "unknown", error: "连接超时" })],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    const error = screen.getByTestId("upstream-error-kilo");
    expect(error).toHaveTextContent("连接超时");
  });

  it("源侧声明数据过期时给出提示", async () => {
    stubFetch(payload({ sources: [source({ source_stale: true })] }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("upstream-source-kilo")).toHaveTextContent("源侧数据过期");
  });

  it("「只看异常」默认关闭；开启后只留非正常源", async () => {
    stubFetch(payload({
      sources: [
        source(),
        source({ key: "openai", label: "OpenAI", scope: "model",
                 related_providers: [], level: "major" }),
      ],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    // 默认两个都在
    expect(screen.getByTestId("upstream-source-kilo")).toBeInTheDocument();
    expect(screen.getByTestId("upstream-source-openai")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("upstream-abnormal-toggle"));

    await waitFor(() =>
      expect(screen.queryByTestId("upstream-source-kilo")).not.toBeInTheDocument());
    expect(screen.getByTestId("upstream-source-openai")).toBeInTheDocument();
  });

  it("全部正常时开启「只看异常」显示空态而非空白", async () => {
    stubFetch(payload());
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    await userEvent.click(screen.getByTestId("upstream-abnormal-toggle"));

    expect(await screen.findByTestId("no-upstream-abnormal")).toBeInTheDocument();
  });

  it("尚未抓取（无源）时显示引导空态", async () => {
    stubFetch(payload({
      summary: { level: "unknown", operational: 0, abnormal: 0, unknown: 0, total: 0 },
      fetched_at: null,
      sources: [],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("no-upstream-status")).toBeInTheDocument();
    expect(screen.getByTestId("upstream-status-page")).toHaveTextContent("尚未抓取");
  });

  it("任务关闭时给出过时快照提示", async () => {
    stubFetch(payload({ enabled: false }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("upstream-disabled")).toBeInTheDocument();
  });

  it("请求失败显示错误提示", async () => {
    stubFetch({ error: { code: "boom", message: "挂了" } }, 500);
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    expect(await screen.findByTestId("upstream-status-error")).toBeInTheDocument();
  });
});
