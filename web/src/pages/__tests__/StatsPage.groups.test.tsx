import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StatsPage } from "../StatsPage";
import { jsonResponse, mockFetch, renderPage, settle, userEvent } from "@/test-utils";
import {
  ADMIN,
  CREDENTIALS,
  EVENTS_EMPTY,
  EVENTS_PAGE1,
  EVENTS_PAGE2,
  MODEL_TIMELINE,
  MODELS,
  OVERVIEW,
  PROVIDERS,
  READER,
  USERS,
} from "@/test-utils/stats";

describe("StatsPage 分组与明细", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("请求明细分页：下一页按 before 取数，上一页回退，换每页数量重置", async () => {
    const fetchSpy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": (url: string) =>
        jsonResponse(url.includes("before=") ? EVENTS_PAGE2 : EVENTS_PAGE1),
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();
    expect(screen.getByTestId("events-table")).toHaveTextContent("glm-5.2");
    expect(screen.getByTestId("events-page-info")).toHaveTextContent("第 1 页");

    await userEvent.click(screen.getByTestId("events-next"));
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("before=2"))).toBe(true),
    );
    // 跨过 1 秒再断言：since 锚定后翻页状态不会被「范围变化重置」打回第一页
    await new Promise((resolve) => setTimeout(resolve, 1200));
    expect(screen.getByTestId("events-table")).toHaveTextContent("kimi-k3");
    expect(screen.getByTestId("events-page-info")).toHaveTextContent("第 2 页");
    expect(screen.getByTestId("events-next")).toBeDisabled();          // 到底
    expect(screen.getByTestId("events-prev")).toBeEnabled();
    // 不带 before 的首页请求只应出现过一次（无重置抖动）
    const firstPageCalls = fetchSpy.mock.calls.filter(
      ([url]) => String(url).includes("/api/stats/events") && !String(url).includes("before="),
    );
    expect(firstPageCalls).toHaveLength(1);

    await userEvent.click(screen.getByTestId("events-prev"));
    await waitFor(() =>
      expect(screen.getByTestId("events-table")).toHaveTextContent("glm-5.2"),
    );

    await userEvent.selectOptions(screen.getByTestId("events-page-size"), "50");
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("limit=50"))).toBe(true),
    );
    expect(screen.getByTestId("events-page-info")).toHaveTextContent("第 1 页");

    // 翻到第 2 页后切时间范围 → 事件驱动重置回第 1 页（且不因 since 漂移误重置）
    await userEvent.click(screen.getByTestId("events-next"));
    await waitFor(() =>
      expect(screen.getByTestId("events-page-info")).toHaveTextContent("第 2 页"),
    );
    await new Promise((resolve) => setTimeout(resolve, 1200));
    await userEvent.click(screen.getByTestId("range-tabs-24h"));
    await waitFor(() =>
      expect(screen.getByTestId("events-page-info")).toHaveTextContent("第 1 页"),
    );
    expect(screen.getByTestId("events-table")).toHaveTextContent("glm-5.2");
  });

  it("分组统计排序：默认请求数降序，点表头带 sort/order 重新取数", async () => {
    const spy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // 默认按请求数降序（后端默认，前端也显式下发）
    const providerCall = spy.mock.calls
      .map((call) => String(call[0]))
      .find((url) => url.includes("/api/stats/by-provider"))!;
    expect(providerCall).toContain("sort=requests");
    expect(providerCall).toContain("order=desc");

    // 点「渠道」列头 → 按分组键升序重新取数
    await userEvent.click(screen.getByTestId("sort-group"));
    await waitFor(() => {
      const last = spy.mock.calls
        .map((call) => String(call[0]))
        .filter((url) => url.includes("/api/stats/by-provider"))
        .at(-1)!;
      expect(last).toContain("sort=group");
    });
  });

  it("请求明细排序：换列从 rowid 游标切到 offset 分页", async () => {
    const spy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": { events: EVENTS_PAGE1.events, next_before: null, total: 7 },
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // 默认（rowid 降序）走游标分页：请求不带 offset
    const first = spy.mock.calls.map((call) => String(call[0]))
      .find((url) => url.includes("/api/stats/events"))!;
    expect(first).toContain("sort=time");
    expect(first).not.toContain("offset=");

    // 点「模型」列头 → 走 offset 分页（offset 从 0 起）
    await userEvent.click(screen.getByTestId("event-sort-model"));
    await waitFor(() => {
      const last = spy.mock.calls.map((call) => String(call[0]))
        .filter((url) => url.includes("/api/stats/events")).at(-1)!;
      expect(last).toContain("sort=model");
      expect(last).toContain("offset=0");
    });
    // total=7 > 本页 2 条 → 下一页可用（offset 模式的 hasNextPage）
    expect(screen.getByTestId("events-next")).toBeEnabled();
  });

  it("非管理员明细表无用户列", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_PAGE1,
    });
    renderPage(<StatsPage />, READER);
    await settle();

    const table = screen.getByTestId("events-table");
    expect(within(table).queryByText("用户")).not.toBeInTheDocument();
    expect(within(table).queryByText("root")).not.toBeInTheDocument();
  });

  it("分组统计：默认按渠道，切换 tabs 依次展示模型/用户/凭证", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // 默认选中「按渠道」
    expect(screen.getByTestId("group-tabs-provider")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("group-table")).toHaveTextContent("TRAE");
    expect(screen.getByTestId("group-table")).toHaveTextContent("CodeBuddy");

    // 切模型：行换成模型名
    await userEvent.click(screen.getByTestId("group-tabs-model"));
    await waitFor(() => expect(within(screen.getByTestId("group-table")).getByText("glm-5.2"))
      .toBeInTheDocument());
    expect(screen.getByTestId("group-table")).toHaveTextContent("kimi-k3");

    // 切用户
    await userEvent.click(screen.getByTestId("group-tabs-user"));
    await waitFor(() => expect(within(screen.getByTestId("group-table")).getByText("alice"))
      .toBeInTheDocument());
    expect(screen.getByTestId("group-table")).toHaveTextContent("bob");

    // 切凭证：昵称优先（无昵称回落到 ID 前 12 位）
    await userEvent.click(screen.getByTestId("group-tabs-credential"));
    await waitFor(() => expect(screen.getByTestId("group-table"))
      .toHaveTextContent("主账号"));
    expect(screen.getByTestId("group-table")).toHaveTextContent("cred_zzz9998");
    expect(within(screen.getByTestId("group-table")).queryByText("cred_zzz999888777"))
      .not.toBeInTheDocument();
  });

  it("分组统计：各维度都给出成功率、命中缓存与成本", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // 按模型：glm-5.2 38/40 = 95.0%，命中缓存 100，成本 ¥5.36
    await userEvent.click(screen.getByTestId("group-tabs-model"));
    await settle();
    let row = within(screen.getByTestId("group-table")).getByText("glm-5.2").closest("tr")!;
    expect(row).toHaveTextContent("95.0%");
    expect(row).toHaveTextContent("100");
    expect(row).toHaveTextContent("¥5.36");
    // kimi-k3 28/30 = 93.3%，推算成本标 ≈
    row = within(screen.getByTestId("group-table")).getByText("kimi-k3").closest("tr")!;
    expect(row).toHaveTextContent("93.3%");
    expect(row).toHaveTextContent("≈¥2.68");

    // 按用户：alice 76/80 = 95.0%
    await userEvent.click(screen.getByTestId("group-tabs-user"));
    await settle();
    row = within(screen.getByTestId("group-table")).getByText("alice").closest("tr")!;
    expect(row).toHaveTextContent("95.0%");
    expect(row).toHaveTextContent("¥8.04");

    // 按凭证：主账号行 68/70 = 97.1%
    await userEvent.click(screen.getByTestId("group-tabs-credential"));
    await settle();
    row = within(screen.getByTestId("group-table")).getByText("主账号").closest("tr")!;
    expect(row).toHaveTextContent("97.1%");
    expect(row).toHaveTextContent("≈¥3.35");
  });

  it("分组统计：凭证名与请求明细同口径，且带渠道 icon", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_PAGE1,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    await userEvent.click(screen.getByTestId("group-tabs-credential"));
    await settle();

    const groupTable = screen.getByTestId("group-table");
    // 同一个凭证：分组表与明细表都显示昵称「主账号」，完整 ID 落在 title 里
    const groupCell = within(groupTable).getByText("主账号").closest("td")!;
    expect(groupCell).toHaveAttribute("title", "cred_abc123");
    const eventCell = within(screen.getByTestId("events-table")).getByText("主账号")
      .closest("td")!;
    expect(eventCell).toHaveAttribute("title", "cred_abc123");
    // 凭证名前有渠道 icon（品牌 svg）
    expect(groupCell.querySelector("svg")).not.toBeNull();
    // 无昵称的行回落 ID 前 12 位，title 给完整 ID
    const fallbackCell = within(groupTable).getByText("cred_zzz9998").closest("td")!;
    expect(fallbackCell).toHaveAttribute("title", "cred_zzz999888777");
    expect(fallbackCell.querySelector("svg")).not.toBeNull();
  });

  it("分组统计：只读用户看不到按用户/按凭证 tab", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, READER);
    await settle();

    expect(screen.getByTestId("group-tabs-provider")).toBeInTheDocument();
    expect(screen.getByTestId("group-tabs-model")).toBeInTheDocument();
    expect(screen.queryByTestId("group-tabs-user")).not.toBeInTheDocument();
    expect(screen.queryByTestId("group-tabs-credential")).not.toBeInTheDocument();
  });
});
