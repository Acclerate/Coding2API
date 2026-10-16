import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CredentialsPage } from "../CredentialsPage";
import {
  expandCredentialGroups, jsonResponse, makeCredential, mockFetch, renderPage, settle,
  userEvent,
} from "@/test-utils";

const ADMIN = { username: "root", is_admin: true } as const;
const READER = { username: "guest", is_admin: false } as const;

function listBody(credentials: unknown[], isAdmin = true) {
  return { credentials, expiry_window_seconds: 129600,
            expiry_secondary_window_seconds: 604800,
            token_expiry_warning_seconds: 3600,
            viewer: isAdmin ? "root" : "guest", is_admin: isAdmin,
            is_operator: isAdmin };
}

/** 找到批量端点那次调用的请求体（用 vi 的 spy 记录）。 */
function batchCall(spy: ReturnType<typeof mockFetch>) {
  const call = spy.mock.calls.find(
    ([url, init]) => String(url).endsWith("/api/credentials/batch")
      && (init as RequestInit | undefined)?.method === "POST",
  );
  if (!call) throw new Error("未发出批量请求");
  return JSON.parse(String((call[1] as RequestInit).body));
}

async function renderWithTwo() {
  const spy = mockFetch({
    "/api/credentials/batch": {
      action: "probe", total: 2, succeeded: 2, failed: 0,
      results: [{ id: "cb1", ok: true }, { id: "cb2", ok: true }],
    },
    "/api/credentials": listBody([
      makeCredential({ id: "cb1", provider: "codebuddy", nickname: "主号" }),
      makeCredential({ id: "cb2", provider: "codebuddy", nickname: "备用号" }),
    ]),
  });
  renderPage(<CredentialsPage />, ADMIN);
  await settle();
  await expandCredentialGroups();
  return spy;
}

describe("CredentialsPage 批量运维", () => {
  it("无选中项时不显示批量操作条", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential({ id: "cb1" })]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    expect(screen.queryByTestId("credentials-batch-bar")).not.toBeInTheDocument();
  });

  it("只读用户没有选择列与批量操作条", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential({ id: "cb1" })], false) });
    renderPage(<CredentialsPage />, READER);
    await settle();
    await expandCredentialGroups();
    expect(screen.queryByTestId("select-cb1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("group-select-codebuddy")).not.toBeInTheDocument();
    expect(screen.queryByTestId("credentials-batch-bar")).not.toBeInTheDocument();
  });

  it("勾选后出现操作条，提交批量探测带上选中 id 并汇报结果", async () => {
    const spy = await renderWithTwo();
    await userEvent.click(screen.getByTestId("select-cb1"));
    await userEvent.click(screen.getByTestId("select-cb2"));

    const bar = screen.getByTestId("credentials-batch-bar");
    expect(within(bar).getByTestId("batch-selected-count")).toHaveTextContent("已选 2 个");

    await userEvent.click(within(bar).getByTestId("batch-probe"));
    await waitFor(() => expect(screen.getByTestId("credentials-batch-result")).toBeInTheDocument());
    expect(batchCall(spy)).toEqual({ action: "probe", ids: ["cb1", "cb2"] });
    expect(screen.getByTestId("credentials-batch-result"))
      .toHaveTextContent("探测完成：成功 2 / 2");
    // 成功后清空选择，操作条随之消失
    await waitFor(() => expect(screen.queryByTestId("credentials-batch-bar")).not.toBeInTheDocument());
  });

  it("批量暂停显式传 enabled=false（不做各自取反）", async () => {
    const spy = mockFetch({
      "/api/credentials/batch": {
        action: "toggle", total: 2, succeeded: 2, failed: 0,
        results: [{ id: "cb1", ok: true }, { id: "cb2", ok: true }],
      },
      "/api/credentials": listBody([
        makeCredential({ id: "cb1", enabled: 1 }),
        makeCredential({ id: "cb2", enabled: 0 }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("select-cb1"));
    await userEvent.click(screen.getByTestId("select-cb2"));
    await userEvent.click(screen.getByTestId("batch-toggle"));
    await waitFor(() => expect(screen.getByTestId("credentials-batch-result")).toBeInTheDocument());
    expect(batchCall(spy)).toEqual({ action: "toggle", ids: ["cb1", "cb2"], enabled: false });
  });

  it("组头全选纳入本渠道全部账号，再次点击取消", async () => {
    const spy = await renderWithTwo();
    await userEvent.click(screen.getByTestId("group-select-codebuddy"));
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 2 个");
    // 取消全选：操作条消失
    await userEvent.click(screen.getByTestId("group-select-codebuddy"));
    expect(screen.queryByTestId("credentials-batch-bar")).not.toBeInTheDocument();
    // 全选态下提交仍带全部 id
    await userEvent.click(screen.getByTestId("group-select-codebuddy"));
    await userEvent.click(screen.getByTestId("batch-checkin"));
    await waitFor(() => expect(batchCall(spy).action).toBe("checkin"));
  });

  it("取消选择按钮清空选中集合", async () => {
    await renderWithTwo();
    await userEvent.click(screen.getByTestId("select-cb1"));
    await userEvent.click(screen.getByTestId("batch-clear"));
    expect(screen.queryByTestId("credentials-batch-bar")).not.toBeInTheDocument();
  });

  it("全部签到：对池内全部可签到凭证提交批量，免费层与国际站剔除", async () => {
    const spy = mockFetch({
      "/api/credentials/batch": {
        action: "checkin", total: 2, succeeded: 2, failed: 0,
        results: [{ id: "trae1", ok: true }, { id: "cb1", ok: true }],
      },
      "/api/credentials": listBody([
        makeCredential({ id: "trae1", nickname: "TRAE 号" }),
        makeCredential({ id: "cb1", provider: "codebuddy", nickname: "CB 号" }),
        // zen 免费层无签到能力、CodeBuddy 国际站 checkin=None：行内没有
        // 签到按钮，也不该混进一键签到
        makeCredential({ id: "zen1", provider: "zen", nickname: "免费层" }),
        makeCredential({ id: "intl1", provider: "codebuddy_intl", nickname: "国际站" }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    // 按钮在面板标题栏，分组折叠时也可见、可点
    await userEvent.click(screen.getByTestId("checkin-all"));
    await waitFor(() => expect(screen.getByTestId("credentials-batch-result")).toBeInTheDocument());
    expect(batchCall(spy)).toEqual({ action: "checkin", ids: ["trae1", "cb1"] });
    expect(screen.getByTestId("credentials-batch-result"))
      .toHaveTextContent("签到完成：成功 2 / 2");
  });

  it("全部签到不动多选集合：签完选中项仍保留", async () => {
    const spy = mockFetch({
      "/api/credentials/batch": {
        action: "checkin", total: 2, succeeded: 2, failed: 0,
        results: [{ id: "cb1", ok: true }, { id: "cb2", ok: true }],
      },
      "/api/credentials": listBody([
        makeCredential({ id: "cb1", provider: "codebuddy" }),
        makeCredential({ id: "cb2", provider: "codebuddy" }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("select-cb1"));
    await userEvent.click(screen.getByTestId("checkin-all"));
    await waitFor(() => expect(screen.getByTestId("credentials-batch-result")).toBeInTheDocument());
    // 提交的是全池（与勾选无关），勾选也不被顺带清空——它不是「全选 + 签到」
    expect(batchCall(spy).ids).toEqual(["cb1", "cb2"]);
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 1 个");
  });

  it("池内没有可签到凭证时「全部签到」禁用而非消失", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "zen1", provider: "zen" }),
        makeCredential({ id: "intl1", provider: "codebuddy_intl" }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    expect(screen.getByTestId("checkin-all")).toBeDisabled();
  });

  it("只读用户看不到「全部签到」", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential({ id: "cb1" })], false) });
    renderPage(<CredentialsPage />, READER);
    await settle();
    expect(screen.queryByTestId("checkin-all")).not.toBeInTheDocument();
  });

  it("部分失败：浮层列出失败原因与昵称，色调为 warn", async () => {
    mockFetch({
      "/api/credentials/batch": {
        action: "probe", total: 2, succeeded: 1, failed: 1,
        results: [
          { id: "cb1", ok: true },
          { id: "cb2", ok: false, detail: { probed: false, reason: "credential_rejected" } },
        ],
      },
      "/api/credentials": listBody([
        makeCredential({ id: "cb1", provider: "codebuddy", nickname: "主号" }),
        makeCredential({ id: "cb2", provider: "codebuddy", nickname: "备用号" }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("group-select-codebuddy"));
    await userEvent.click(screen.getByTestId("batch-probe"));

    const toast = await screen.findByTestId("credentials-batch-result");
    expect(toast).toHaveTextContent("探测完成：成功 1 / 2，失败 1");
    const failure = within(toast).getByTestId("batch-failure");
    // 失败行用昵称而不是裸 id，并翻译成可操作的中文
    expect(failure).toHaveTextContent("备用号：凭证被渠道拒绝，需要重新登录该账号");
  });

  it("执行前就失败的原因（凭证不存在 / 渠道不支持）翻译成人话", async () => {
    mockFetch({
      "/api/credentials/batch": {
        action: "checkin", total: 6, succeeded: 1, failed: 5,
        results: [
          { id: "cb1", ok: true },
          { id: "cb2", ok: false, error: "credential not found" },
          { id: "cb3", ok: false, error: "credential does not support checkin" },
          { id: "cb4", ok: false, error: "action_failed" },
          { id: "cb5", ok: false, error: "something_else" },
          { id: "cb6", ok: false },
        ],
      },
      "/api/credentials": listBody([
        makeCredential({ id: "cb1", provider: "codebuddy", nickname: "主号" }),
        makeCredential({ id: "cb2", provider: "codebuddy", nickname: "备用号" }),
        makeCredential({ id: "cb3", provider: "codebuddy", nickname: "三号" }),
        makeCredential({ id: "cb4", provider: "codebuddy", nickname: "四号" }),
        makeCredential({ id: "cb5", provider: "codebuddy", nickname: "五号" }),
        makeCredential({ id: "cb6", provider: "codebuddy", nickname: "六号" }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("group-select-codebuddy"));
    await userEvent.click(screen.getByTestId("batch-checkin"));

    const toast = await screen.findByTestId("credentials-batch-result");
    const failures = within(toast).getAllByTestId("batch-failure");
    const text = failures.map((node) => node.textContent).join("\n");
    expect(text).toContain("凭证不存在（可能已被删除）");
    expect(text).toContain("该渠道不支持此操作");
    expect(text).toContain("执行失败，详见服务端日志");
    // 未知 error 原样透传（后端契约里的稳定枚举，不该被前端吞掉）
    expect(text).toContain("something_else");
    // 无原因可查的兜底为「失败」
    expect(failures[4]).toHaveTextContent("失败");
  });

  it("签到失败时回落到 detail.message；失败超过 5 条折成计数", async () => {
    const rows = Array.from({ length: 7 }, (_, index) => ({
      id: `cb${index + 1}`,
      ok: false,
      detail: { ok: false, message: "今天已签过" },
    }));
    mockFetch({
      "/api/credentials/batch": {
        action: "checkin", total: 7, succeeded: 0, failed: 7, results: rows,
      },
      "/api/credentials": listBody(
        rows.map((row, index) =>
          makeCredential({ id: row.id, provider: "codebuddy", nickname: `号${index + 1}` })),
      ),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("group-select-codebuddy"));
    await userEvent.click(screen.getByTestId("batch-checkin"));

    const toast = await screen.findByTestId("credentials-batch-result");
    expect(toast).toHaveTextContent("签到完成：成功 0 / 7，失败 7");
    expect(within(toast).getAllByTestId("batch-failure")).toHaveLength(5);
    expect(within(toast).getAllByTestId("batch-failure")[0]).toHaveTextContent("今天已签过");
    expect(toast).toHaveTextContent("另有 2 条失败");
  });

  it("结果里的 id 已不在列表中时回落显示 id 前缀", async () => {
    mockFetch({
      "/api/credentials/batch": {
        action: "revive", total: 1, succeeded: 0, failed: 1,
        results: [{ id: "cred_deleted_after_select", ok: false, error: "credential not found" }],
      },
      "/api/credentials": listBody([makeCredential({ id: "cb1", provider: "codebuddy" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("select-cb1"));
    // 后端按提交时的 ids 执行，结果里可能出现已被删除的凭证
    await userEvent.click(screen.getByTestId("batch-revive"));
    const toast = await screen.findByTestId("credentials-batch-result");
    expect(within(toast).getByTestId("batch-failure"))
      .toHaveTextContent("cred_deleted：凭证不存在（可能已被删除）");
  });

  it("请求整体失败时走 error 浮层", async () => {
    mockFetch({
      "/api/credentials/batch": () =>
        jsonResponse({ error: { code: "forbidden", message: "operator only" } }, 403),
      "/api/credentials": listBody([makeCredential({ id: "cb1", provider: "codebuddy" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("select-cb1"));
    await userEvent.click(screen.getByTestId("batch-probe"));
    const error = await screen.findByTestId("credentials-error");
    expect(error).toHaveTextContent("operator only");
  });

  it("筛选隐藏的行不进入批量动作（选中集合与筛选结果求交）", async () => {
    const spy = await renderWithTwo();
    await userEvent.click(screen.getByTestId("group-select-codebuddy"));
    // 筛选到只剩「主号」，批量动作不应带上被隐藏的 cb2
    await userEvent.type(screen.getByTestId("credential-search"), "主号");
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 1 个");
    await userEvent.click(screen.getByTestId("batch-probe"));
    await waitFor(() => expect(screen.getByTestId("credentials-batch-result")).toBeInTheDocument());
    expect(batchCall(spy).ids).toEqual(["cb1"]);
  });
});
