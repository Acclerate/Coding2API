/** StatsPage 测试共享的样例数据。 */
export const ADMIN = { username: "root", is_admin: true } as const;
export const READER = { username: "alice", is_admin: false } as const;

export const OVERVIEW = {
  requests: 120,
  ok_count: 114,
  success_rate: 0.95,
  input_tokens: 1000,
  output_tokens: 2000,
  reasoning_tokens: 300,
  cached_tokens: 400,
  credit: 12.5,
  credit_estimated: false,
  cost_usd: 1.84,
  cost_cny: 12.34,
  avg_latency_ms: 850,
  avg_ttfb_ms: 220,
};

export const PROVIDERS = {
  providers: [
    { provider: "trae", requests: 70, ok_count: 68, input_tokens: 600, output_tokens: 1200, cached_tokens: 250, credit: 5.25, credit_estimated: true, cost_usd: 0.5, cost_cny: 3.35 },
    { provider: "codebuddy", requests: 50, ok_count: 46, input_tokens: 400, output_tokens: 800, cached_tokens: 150, credit: 12.5, credit_estimated: false, cost_usd: 1.34, cost_cny: 8.99 },
  ],
};

export const MODELS = {
  models: [
    { model: "glm-5.2", requests: 40, ok_count: 38, input_tokens: 300, output_tokens: 900, cached_tokens: 100, credit: 8.0, credit_estimated: false, cost_usd: 0.8, cost_cny: 5.36 },
    { model: "kimi-k3", requests: 30, ok_count: 28, input_tokens: 200, output_tokens: 600, credit: 4.5, credit_estimated: true, cost_usd: 0.4, cost_cny: 2.68 },
  ],
};

export const USERS = {
  users: [
    { username: "alice", requests: 80, ok_count: 76, input_tokens: 700, output_tokens: 1500, cached_tokens: 300, credit: 9.75, credit_estimated: true, cost_usd: 1.2, cost_cny: 8.04 },
    { username: "bob", requests: 40, ok_count: 38, input_tokens: 300, output_tokens: 500, cached_tokens: 100, credit: 2.75, credit_estimated: false, cost_usd: 0.64, cost_cny: 4.28 },
  ],
};

export const CREDENTIALS = {
  credentials: [
    { credential_id: "cred_abc123", credential_name: "主账号", provider: "trae",
      requests: 70, ok_count: 68, input_tokens: 600, output_tokens: 1200, cached_tokens: 250, credit: 5.25, credit_estimated: true, cost_usd: 0.5, cost_cny: 3.35 },
    // 无昵称（老库/已删除）：回落 ID 前 12 位
    { credential_id: "cred_zzz999888777", credential_name: null, provider: "codebuddy",
      requests: 50, ok_count: 46, input_tokens: 400, output_tokens: 800, cached_tokens: 150, credit: 12.5, credit_estimated: false, cost_usd: 1.34, cost_cny: 8.99 },
  ],
};

export const MODEL_TIMELINE = {
  models: ["glm-5.2", "kimi-k3"],
  points: [
    { hour: 1700000000, "glm-5.2": 4, "kimi-k3": 1 },
    { hour: 1700003600, "glm-5.2": 0, "kimi-k3": 2 },
  ],
};

export const EVENTS_EMPTY = { events: [], next_before: null };
export const BASE_TS = 1_700_000_000;

export const EVENTS_PAGE1 = {
  events: [
    { rowid: 3, ts: BASE_TS + 200, username: "root", provider: "codebuddy", model: "glm-5.2",
      credential_id: "cred_abc123", credential_name: "主账号",
      ok: 1, error_type: null, input_tokens: 120, output_tokens: 480, cached_tokens: 90,
      credit: 0.35, credit_estimated: 0, cost_usd: 0.0012, cost_cny: 0.008,
      latency_ms: 8200, ttfb_ms: 2400 },
    { rowid: 2, ts: BASE_TS + 100, username: "root", provider: "trae", model: "deepseek-v4",
      credential_id: null, credential_name: null,
      ok: 0, error_type: "rate_limit", input_tokens: null, output_tokens: null, cached_tokens: null,
      credit: null, credit_estimated: 0, cost_usd: null, cost_cny: null,
      latency_ms: 460, ttfb_ms: 120 },
  ],
  next_before: 2,
};

export const EVENTS_PAGE2 = {
  events: [
    { rowid: 1, ts: BASE_TS, username: "root", provider: "trae", model: "kimi-k3",
      ok: 1, error_type: null, input_tokens: 5, output_tokens: 9, cached_tokens: null,
      credit: null, credit_estimated: 0, cost_usd: null, cost_cny: null,
      latency_ms: 300, ttfb_ms: 150 },
  ],
  next_before: null,
};
