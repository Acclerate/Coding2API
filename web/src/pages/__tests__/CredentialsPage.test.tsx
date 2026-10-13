import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CredentialsPage } from "../CredentialsPage";
import { jsonResponse, makeCredential, mockFetch, renderPage, settle, userEvent } from "@/test-utils";

const ADMIN = { username: "root", is_admin: true } as const;
const READER = { username: "guest", is_admin: false } as const;
// 运维角色：能写凭证但非管理员（is_operator 由后端按 role 派生）
const OPERATOR = { username: "op", is_admin: false, role: "operator" } as const;

function listBody(credentials: unknown[], isAdmin = true, isOperator = isAdmin) {
  return { credentials, expiry_window_seconds: 129600,
            expiry_secondary_window_seconds: 604800,
            token_expiry_warning_seconds: 3600,
            viewer: isAdmin ? "root" : "guest", is_admin: isAdmin,
            is_operator: isOperator };
}

/** 添加凭证收进对话框：需要操作的用例先点开。 */
async function openAddDialog() {
  await userEvent.click(screen.getByTestId("open-add-dialog"));
  await screen.findByTestId("add-credential-dialog");
}

describe("CredentialsPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染凭证列表与四态健康度（免费层 null 归「无探测」）", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "a", health: 62 }),
        makeCredential({ id: "b", nickname: "未探测号", health: null, provider: "trae" }),
        makeCredential({ id: "d", nickname: "免费层号", health: null, provider: "zen" }),
        makeCredential({ id: "c", nickname: "耗尽号", health: -1 }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    const table = screen.getByTestId("credentials-table");
    expect(within(table).getByText("62%")).toBeInTheDocument();
    expect(within(table).getByText("未探测")).toBeInTheDocument();
    expect(within(table).getByText("无探测")).toBeInTheDocument();
    expect(within(table).getByText("已耗尽")).toBeInTheDocument();
  });

  it("非管理员隐藏所有写操作并显示只读横幅", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential()], false) });
    renderPage(<CredentialsPage />, READER);
    await settle();

    expect(screen.getByTestId("readonly-banner")).toHaveTextContent("只读模式");
    expect(screen.queryByTestId("actions-cred_1")).not.toBeInTheDocument();
    // 积分记录也要藏：它只藏入口，后端仍会 403，但只读视图不该给出可点假象
    expect(screen.queryByTestId("credits-cred_1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("import-submit")).not.toBeInTheDocument();
    expect(screen.queryByTestId("start-login")).not.toBeInTheDocument();
  });

  it("运维角色（非管理员）可见写操作，与后端 require_operator 同口径", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential()], false, true) });
    renderPage(<CredentialsPage />, OPERATOR);
    await settle();

    expect(screen.queryByTestId("readonly-banner")).not.toBeInTheDocument();
    expect(screen.getByTestId("actions-cred_1")).toBeInTheDocument();
    expect(screen.getByTestId("credits-cred_1")).toBeInTheDocument();
    // 导入入口收进对话框：按钮可见即代表可写（对话框内才有表单）
    const addButton = screen.getByTestId("open-add-dialog");
    expect(addButton).toBeInTheDocument();
    // 按钮挂在「凭证池」面板的标题栏右侧，不在页头
    const panel = screen.getByText("凭证池").closest("[data-slot='card']");
    expect(panel).toContainElement(addButton);
  });

  it("只读用户看不到凭证池标题栏的添加按钮", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential()], false, false) });
    renderPage(<CredentialsPage />, READER);
    await settle();
    expect(screen.queryByTestId("open-add-dialog")).not.toBeInTheDocument();
  });

  it("冷却中的凭证显示剩余时间", async () => {
    const future = Math.floor(Date.now() / 1000) + 7200;
    mockFetch({
      "/api/credentials": listBody([makeCredential({ cooling_until: future })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    const row = screen.getByTestId(/^row-/);
    expect(within(row).getByText("冷却中")).toBeInTheDocument();
    expect(within(row).getByText(/小时/)).toBeInTheDocument();
  });

  it("窗口内即将到期的积分单独成行；无到期信息的渠道不显示", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "cb", provider: "codebuddy", quota_expiring_credits: 100 }),
        makeCredential({ id: "tr", provider: "trae", quota_expiring_credits: null }),
        makeCredential({ id: "zero", provider: "codebuddy", quota_expiring_credits: 0 }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    const shown = screen.getAllByTestId("quota-expiring");
    expect(shown).toHaveLength(1);
    expect(shown[0]).toHaveTextContent("100 积分将在");
  });

  it("CodeArts 的到期额度按积分展示（每日池，0 点清零）", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "ca", provider: "codearts", health: 100,
          quota_expiring_credits: 999.5,
        }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    const shown = screen.getAllByTestId("quota-expiring");
    expect(shown).toHaveLength(1);
    expect(shown[0]).toHaveTextContent("999.5 积分将在");
  });

  it("主窗口为空时展示次窗口（7 天）到期积分，措辞与主窗口区分", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "week", provider: "codebuddy", health: 50,
          quota_expiring_credits: 0, quota_expiring_credits_secondary: 140,
        }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    expect(screen.queryByTestId("quota-expiring")).not.toBeInTheDocument();
    const secondary = screen.getAllByTestId("quota-expiring-secondary");
    expect(secondary).toHaveLength(1);
    expect(secondary[0]).toHaveTextContent("7.0 天内共 140 积分将过期");
  });

  it("主窗口有数字时不重复渲染次窗口（次窗口是主窗口的超集）", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "both", provider: "codebuddy", health: 50,
          quota_expiring_credits: 100, quota_expiring_credits_secondary: 140,
        }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    expect(screen.getAllByTestId("quota-expiring")).toHaveLength(1);
    expect(screen.queryByTestId("quota-expiring-secondary")).not.toBeInTheDocument();
  });

  it("次窗口关闭或无到期信息时不渲染第二行", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "trae2", provider: "trae", health: 50,
                         quota_expiring_credits: null,
                         quota_expiring_credits_secondary: null }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    expect(screen.queryByTestId("quota-expiring")).not.toBeInTheDocument();
    expect(screen.queryByTestId("quota-expiring-secondary")).not.toBeInTheDocument();
  });

  it("额度包明细：单元格只留「套餐 N 个」，悬浮弹出按到期升序的完整明细", async () => {
    const day = Math.floor(Date.now() / 1000) + 86400;
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "cb",
          provider: "codebuddy",
          // 故意逆序 + 一个无到期时间：有到期的按先后排，无到期的排最后
          quota_packages: [
            { name: "签到奖励", total: 150, used: 0, end: day + 200_000 },
            { name: "福利积分", total: 2000, used: 0, end: day },
            { name: "每月登录", total: 500, used: 76.564, end: day + 100_000 },
            { name: "无到期包", total: 100, used: 10, end: null },
          ],
        }),
        makeCredential({ id: "tr", provider: "trae", quota_packages: null }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    // 无明细 → 不渲染触发元素
    expect(screen.queryByTestId("packages-toggle-tr")).not.toBeInTheDocument();
    // 表格里只有一行触发元素，不把几十个包撑进单元格
    const toggle = screen.getByTestId("packages-toggle-cb");
    expect(toggle).toHaveTextContent("套餐 4 个");
    expect(screen.queryByTestId("package-cb")).not.toBeInTheDocument();
    // 「套餐 N 个」与首行「剩余 / 总量」同处一行的 flex 容器；
    // 数字那侧多包了一层 inline-flex（对齐积分记录图标按钮），所以要上溯一层
    const row = screen.getByTestId("row-cb");
    const quotaSpan = within(row).getByText("62 / 100");
    expect(quotaSpan.parentElement?.parentElement).toBe(toggle.parentElement);
    expect(toggle.parentElement).toHaveClass("flex-wrap");

    await userEvent.hover(toggle);
    const tip = await screen.findByRole("tooltip");
    const items = within(tip).getAllByTestId("package-cb");
    expect(items).toHaveLength(4);
    // 按到期升序：最近到期的「福利积分」排第一，无到期时间的「无到期包」排最后
    expect(items[0]).toHaveTextContent("福利积分");
    expect(items[0]).toHaveTextContent("2,000");
    expect(items[1]).toHaveTextContent("每月登录");
    expect(items[1]).toHaveTextContent("已用 76.56");
    expect(items[3]).toHaveTextContent("无到期包");
    expect(items[3]).toHaveTextContent("— 到期");
    // 明细行很长：弹层要放宽且不折行（默认 max-w-sm + 换行会把每行挤成两三行）
    expect(tip.className).toContain("max-w-[90vw]");
    expect(tip.className).toContain("whitespace-nowrap");
    await userEvent.unhover(toggle);
  });

  it("无额度包明细（未探测或 TRAE）时不显示套餐触发元素", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "empty", provider: "codebuddy", quota_packages: [] }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    expect(screen.queryByTestId("packages-toggle-empty")).not.toBeInTheDocument();
  });

  it("额度列不再显示「探测于」时间戳", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential({ id: "cb" })]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    const row = screen.getByTestId("row-cb");
    expect(within(row).queryByText(/探测于/)).not.toBeInTheDocument();
  });

  it("模型级冷却：表格里只留警告图标，hover 才展开明细", async () => {
    const now = Math.floor(Date.now() / 1000);
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "cb",
          provider: "codebuddy",
          // 未过期（限流）与已过期（不该上屏）
          model_cooldowns: [
            { model: "glm-5.2", cooling_until: now + 300, hits: 2, reason: "model" },
            { model: "stale", cooling_until: now - 300, hits: 1, reason: "model" },
          ],
        }),
        makeCredential({ id: "plain", provider: "trae", model_cooldowns: [] }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    // 明细不常驻表格：不 hover 时单元格里没有冷却文案，只有一个警告图标
    expect(screen.queryByTestId("model-cooldowns-cb")).not.toBeInTheDocument();
    const icon = screen.getByTestId("model-cooldown-icon-cb");
    const row = screen.getByTestId("row-cb");
    expect(within(row).queryByText(/模型 glm-5\.2/)).not.toBeInTheDocument();

    await userEvent.hover(icon);
    const block = await screen.findByTestId("model-cooldowns-cb");
    expect(block).toHaveTextContent("glm-5.2");
    expect(block).toHaveTextContent("其它模型不受影响");
    expect(block).toHaveTextContent("连续 2 次");
    expect(block).not.toHaveTextContent("stale");
    await userEvent.unhover(icon);

    // 无冷却条目的凭证不渲染图标
    expect(screen.queryByTestId("model-cooldown-icon-plain")).not.toBeInTheDocument();
  });

  it("暂停/取消暂停调用 toggle 接口", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/toggle")) return jsonResponse({ ok: true });
      return jsonResponse(listBody([makeCredential({ id: "cred_1", enabled: 1 })]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "暂停" }));

    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/credentials/cred_1/toggle",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("被硬禁用的凭证显示「恢复」并调用 revive 接口", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/revive")) return jsonResponse({ ok: true });
      return jsonResponse(listBody([
        makeCredential({ id: "cred_1", disabled: 1, disabled_reason: "session dead" }),
      ]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "恢复" }));

    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/credentials/cred_1/revive",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("健康凭证不显示「恢复」入口", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential({ id: "cred_1" })]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    expect(screen.queryByRole("menuitem", { name: "恢复" })).not.toBeInTheDocument();
  });

  it("指定优先使用调用 pin 接口", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/pin")) return jsonResponse({ ok: true });
      return jsonResponse(listBody([makeCredential({ id: "cred_1", pinned: 0 })]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "指定" }));

    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/credentials/pin",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("删除需要二次确认", async () => {
    const fetchSpy = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "DELETE") return jsonResponse({ ok: true });
      return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "删除" }));
    expect(screen.getByRole("button", { name: "确认删除" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/credentials/cred_1",
        expect.objectContaining({ method: "DELETE" }),
      ),
    );
  });

  it("导入凭证：非法 JSON 本地拦截，不发起请求", async () => {
    const fetchSpy = mockFetch({
      "/api/credentials": listBody([]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await openAddDialog();

    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    await userEvent.type(screen.getByTestId("import-payload"), "not-json-at-all");
    await userEvent.click(screen.getByTestId("import-submit"));

    expect(screen.getByText(/必须是合法 JSON/)).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledTimes(1); // 只有列表请求
  });

  it("导入凭证：合法 JSON 调用导入接口", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/credentials" && init?.method === "POST") {
        return jsonResponse({ id: "cred_new" });
      }
      return jsonResponse(listBody([]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await openAddDialog();
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    // 用 paste 写入 JSON，避开 userEvent.type 对花括号的转义语义
    const payload = screen.getByTestId("import-payload");
    await userEvent.click(payload);
    await userEvent.paste('{"token":"abc"}');
    await userEvent.click(screen.getByTestId("import-submit"));

    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/credentials",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("探测失败时提示「未探测」而非额度为 0，并给出可操作原因", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/probe")) {
          return jsonResponse({
            probed: false,
            reason: "upstream_response_invalid",
            detail: "quota response missing Accounts",
          });
        }
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("probe-cred_1"));

    const notice = await screen.findByTestId("credentials-notice");
    expect(notice).toHaveTextContent("探测失败");
    expect(notice).toHaveTextContent("未探测");
    // 面向用户的是可操作的中文说明，不是枚举或异常类名
    expect(notice).toHaveTextContent("渠道响应格式与预期不符");
    expect(notice).not.toHaveTextContent("upstream_response_invalid");
    // 上游原文不再回显给前端（M2/M4：可能含凭证/上游端点）
    expect(screen.queryByTestId("probe-detail")).toBeNull();
  });

  it("探测失败的未知原因有兜底文案", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/probe")) return jsonResponse({ probed: false });
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("probe-cred_1"));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("未知错误");
  });

  it("探测成功展示剩余与总量", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/probe")) {
          return jsonResponse({ probed: true, remaining: 30, total: 100 });
        }
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("probe-cred_1"));

    const notice = await screen.findByTestId("credentials-notice");
    expect(notice).toHaveTextContent("探测成功");
    expect(notice).toHaveTextContent("30");
  });

  it("签到成功展示获得积分", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/checkin")) {
          return jsonResponse({ ok: true, credit: 100, code: 0, message: "" });
        }
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("checkin-cred_1"));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("100");
  });

  it("已签到时显示渠道原文，不出现「获得 — 积分」", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/checkin")) {
          return jsonResponse({
            ok: true, credit: null, code: 10001,
            message: "今天已签到，请明天再来", already_checked_in: true,
          });
        }
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("checkin-cred_1"));
    const notice = await screen.findByTestId("credentials-notice");
    expect(notice).toHaveTextContent("今天已签到，请明天再来");
    expect(notice).not.toHaveTextContent("获得");
  });

  it("签到成功但无 credit 时显示「签到成功」", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/checkin")) {
          return jsonResponse({ ok: true, credit: null, code: 0,
                                message: "", already_checked_in: false });
        }
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("checkin-cred_1"));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("签到成功");
  });

  it("签到未成功展示 code，且 code 为 null 时也能渲染", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/checkin")) {
          return jsonResponse({ ok: false, credit: null, code: null, message: "已签到" });
        }
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("checkin-cred_1"));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("code=null");
  });

  it("空池提示", async () => {
    mockFetch({ "/api/credentials": listBody([]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    expect(screen.getByTestId("no-credentials")).toBeInTheDocument();
  });
});


