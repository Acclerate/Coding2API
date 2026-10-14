import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiKeysPage } from "../ApiKeysPage";
import { jsonResponse, mockFetch, renderPage, settle, userEvent } from "@/test-utils";

const KEY = {
  id: "key_1",
  username: "root",
  name: "laptop",
  preview: "sk-…Ab3d",
  created_at: 1_700_000_000,
  last_used_at: null,
  provider_binding: "",
  allowed_ips: "",
};

describe("OpenAI 客户端接入面板", () => {
  it("显示 Base URL 与端点，未创建 Key 时示例用占位符", async () => {
    mockFetch({ "/api/api-keys": { api_keys: [] } });
    renderPage(<ApiKeysPage />);
    await settle();
    await openOpenAIEntry();

    const baseUrl = screen.getByTestId("openai-base-url").textContent;
    expect(baseUrl).toBe(`${window.location.origin}/v1`);
    expect(screen.getByTestId("openai-entry")).toHaveTextContent("/chat/completions");
    expect(screen.getByTestId("openai-entry")).toHaveTextContent("/models");
    expect(screen.getByTestId("openai-entry")).toHaveTextContent("/user/balance");
    expect(screen.getByTestId("example-curl")).toHaveTextContent("sk-…");
    expect(screen.getByTestId("example-python")).toHaveTextContent(
      `base_url="${window.location.origin}/v1"`,
    );
    expect(screen.getByTestId("example-balance-curl")).toHaveTextContent(
      `curl ${window.location.origin}/v1/user/balance`,
    );
    expect(screen.getByTestId("example-balance-curl")).toHaveTextContent("sk-…");
    // Responses 端点在此列出，示例默认折叠
    expect(screen.getByTestId("example-details-responses")).toHaveTextContent("/responses");
    expect(screen.getByTestId("openai-entry")).toHaveTextContent("Codex CLI");
    // Anthropic 端点已拆到独立面板，OpenAI 面板不再包含
    expect(screen.queryByTestId("example-details-messages")).not.toBeInTheDocument();
  });
  it("创建 Key 后示例自动带入真实 Key", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/api-keys" && init?.method === "POST") {
        return jsonResponse({ ...KEY, api_key: "sk-real-key" });
      }
      if (url === "/api/api-keys") return jsonResponse({ api_keys: [] });
      throw new Error(`未 mock: ${url}`);
    });
    vi.stubGlobal("fetch", fetchSpy);
    renderPage(<ApiKeysPage />);
    await settle();
    await openOpenAIEntry();
    await openCreateDialog();
    await userEvent.type(screen.getByTestId("key-name"), "test");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => {
      expect(screen.getByTestId("example-curl")).toHaveTextContent("sk-real-key");
    });
    expect(screen.getByTestId("example-python")).toHaveTextContent('api_key="sk-real-key"');
    expect(screen.getByTestId("example-balance-curl")).toHaveTextContent("sk-real-key");
    expect(screen.getByTestId("example-responses")).toHaveTextContent(
      "export CODING2API_KEY=sk-real-key",
    );
  });});

describe("Anthropic 客户端接入面板", () => {
  it("Base URL 填到根（SDK 自拼 /v1/messages），Claude Code 示例默认折叠", async () => {
    mockFetch({ "/api/api-keys": { api_keys: [] } });
    renderPage(<ApiKeysPage />);
    await settle();
    await userEvent.click(screen.getByTestId("anthropic-entry-toggle"));

    expect(screen.getByTestId("anthropic-base-url")).toHaveTextContent(
      String(window.location.origin),
    );
    expect(screen.getByTestId("example-details-messages")).toHaveTextContent("/v1/messages");
    expect(screen.getByTestId("example-messages")).toHaveTextContent(
      `export ANTHROPIC_BASE_URL=${window.location.origin}`,
    );
    expect(screen.getByTestId("example-messages")).toHaveTextContent("ANTHROPIC_AUTH_TOKEN=sk-…");
  });

  it("创建 Key 后 Claude Code 示例自动带入真实 Key", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/api-keys" && init?.method === "POST") {
        return jsonResponse({ ...KEY, api_key: "sk-real-key" });
      }
      if (url === "/api/api-keys") return jsonResponse({ api_keys: [] });
      throw new Error(`未 mock: ${url}`);
    });
    vi.stubGlobal("fetch", fetchSpy);
    renderPage(<ApiKeysPage />);
    await settle();
    await userEvent.click(screen.getByTestId("anthropic-entry-toggle"));
    await openCreateDialog();
    await userEvent.type(screen.getByTestId("key-name"), "test");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => {
      expect(screen.getByTestId("example-messages")).toHaveTextContent(
        "export ANTHROPIC_AUTH_TOKEN=sk-real-key",
      );
    });
  });
});


describe("CC Switch 一键导入面板", () => {
  it("未创建 Key 时按钮禁用，不生成深链", async () => {
    mockFetch({ "/api/api-keys": { api_keys: [] } });
    renderPage(<ApiKeysPage />);
    await settle();
    await userEvent.click(screen.getByTestId("ccswitch-entry-toggle"));

    const claude = screen.getByTestId("ccswitch-claude");
    expect(claude.tagName).toBe("BUTTON");
    expect(claude).toBeDisabled();
  });

  it("创建 Key 后三个目标各自生成 ccswitch:// 深链", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/api-keys" && init?.method === "POST") {
        return jsonResponse({ ...KEY, api_key: "sk-real-key" });
      }
      if (url === "/api/api-keys") return jsonResponse({ api_keys: [] });
      throw new Error(`未 mock: ${url}`);
    });
    vi.stubGlobal("fetch", fetchSpy);
    renderPage(<ApiKeysPage />);
    await settle();
    await userEvent.click(screen.getByTestId("ccswitch-entry-toggle"));
    await openCreateDialog();
    await userEvent.type(screen.getByTestId("key-name"), "test");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => expect(screen.getByTestId("ccswitch-claude")).toHaveAttribute("href"));

    const href = (testid: string) => screen.getByTestId(testid).getAttribute("href") ?? "";
    const claude = new URLSearchParams(href("ccswitch-claude").split("?")[1]);
    expect(href("ccswitch-claude").startsWith("ccswitch://v1/import?")).toBe(true);
    expect(claude.get("app")).toBe("claude");
    expect(claude.get("apiFormat")).toBe("anthropic");
    expect(claude.get("endpoint")).toBe(window.location.origin);   // Anthropic 不带 /v1
    expect(claude.get("apiKey")).toBe("sk-real-key");

    const codex = new URLSearchParams(href("ccswitch-codex").split("?")[1]);
    expect(codex.get("apiFormat")).toBe("openai_responses");
    expect(codex.get("endpoint")).toBe(`${window.location.origin}/v1`);

    const opencode = new URLSearchParams(href("ccswitch-opencode").split("?")[1]);
    expect(opencode.get("apiFormat")).toBe("openai_chat");
  });

  it("填入默认模型后写进深链", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/api-keys" && init?.method === "POST") {
        return jsonResponse({ ...KEY, api_key: "sk-real-key" });
      }
      if (url === "/api/api-keys") return jsonResponse({ api_keys: [] });
      throw new Error(`未 mock: ${url}`);
    });
    vi.stubGlobal("fetch", fetchSpy);
    renderPage(<ApiKeysPage />);
    await settle();
    await userEvent.click(screen.getByTestId("ccswitch-entry-toggle"));
    await openCreateDialog();
    await userEvent.type(screen.getByTestId("key-name"), "test");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => expect(screen.getByTestId("ccswitch-claude")).toHaveAttribute("href"));

    await userEvent.type(screen.getByTestId("ccswitch-model"), "glm-5.2");
    const link = screen.getByTestId("ccswitch-claude").getAttribute("href") ?? "";
    expect(new URLSearchParams(link.split("?")[1]).get("model")).toBe("glm-5.2");
  });
});


it("调用示例默认折叠，点击端点行展开", async () => {
  mockFetch({ "/api/api-keys": { api_keys: [] } });
  renderPage(<ApiKeysPage />);
  await settle();
  await openOpenAIEntry();

  const trigger = screen.getByTestId("example-details-trigger");
  expect(trigger).toHaveAttribute("data-state", "closed");
  await userEvent.click(trigger);
  expect(trigger).toHaveAttribute("data-state", "open");
  expect(screen.getByTestId("example-curl")).toBeVisible();
});

it("余额端点示例同样默认折叠，展开后可复制 curl", async () => {
  const writeText = vi.fn(async () => undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  mockFetch({ "/api/api-keys": { api_keys: [] } });
  renderPage(<ApiKeysPage />);
  await settle();
  await openOpenAIEntry();

  const trigger = screen.getByTestId("example-details-balance-trigger");
  expect(trigger).toHaveAttribute("data-state", "closed");
  await userEvent.click(trigger);
  expect(trigger).toHaveAttribute("data-state", "open");

  const copy = screen.getByTestId("copy-balance-curl");
  await userEvent.click(copy);
  expect(writeText).toHaveBeenCalledWith(
    `curl ${window.location.origin}/v1/user/balance \\
  -H "Authorization: Bearer sk-…"`,
  );
});

it("Responses 端点示例默认折叠，展开后可复制 Codex CLI 配置", async () => {
  const writeText = vi.fn(async () => undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  mockFetch({ "/api/api-keys": { api_keys: [] } });
  renderPage(<ApiKeysPage />);
  await settle();
  await openOpenAIEntry();

  const trigger = screen.getByTestId("example-details-responses-trigger");
  expect(trigger).toHaveAttribute("data-state", "closed");
  // 折叠时子节点仍在 DOM 里（forceMount），靠 data-state 控制可见性
  expect(screen.getByTestId("example-responses").closest("[data-state]")).toHaveAttribute(
    "data-state",
    "closed",
  );

  await userEvent.click(trigger);
  expect(trigger).toHaveAttribute("data-state", "open");
  expect(screen.getByTestId("example-responses")).toBeVisible();
  expect(screen.getByTestId("example-responses")).toHaveTextContent("wire_api='responses'");
  expect(screen.getByTestId("example-responses")).toHaveTextContent(
    `base_url='${window.location.origin}/v1'`,
  );

  await userEvent.click(screen.getByTestId("copy-responses"));
  expect(writeText).toHaveBeenCalledWith(
    expect.stringContaining("export CODING2API_KEY=sk-…"),
  );
});


/** 展开「OpenAI 客户端接入」面板（默认收起，与 HelpBlock 一致）。 */
async function openOpenAIEntry() {
  await userEvent.click(screen.getByTestId("openai-entry-toggle"));
}

/** 打开「创建 API Key」对话框（由「我的 API Key」右上角按钮触发）。 */
async function openCreateDialog() {
  await userEvent.click(screen.getByTestId("open-create-key-dialog"));
  expect(await screen.findByTestId("create-key-dialog")).toBeInTheDocument();
}
