import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import {
  fetchModelAvailability,
  fetchModelCatalog,
  fetchModelProbes,
  probeModel,
  setModelProbeManual,
  withBlocklistPattern,
  type ModelCatalogParams,
} from "./modelExtras";
import { adminKey } from "./hooks";

/**
 * 模型面新增的查询 / 变更（批次 C）。独立成文件，`hooks.ts` 不动：
 * 那里是既有页面的共用入口，模型面的两个端点只服务「模型列表」页。
 */

/** 模型目录（带「疑似下线」筛选）。参数全部进 query key，切筛选即重新取数。 */
export function useModelCatalogPage(username?: string, params: ModelCatalogParams = {}) {
  return useQuery({
    queryKey: adminKey(username, "model-catalog-page", params),
    queryFn: () => fetchModelCatalog(params),
    placeholderData: keepPreviousData,
  });
}

/** 模型可用性视图（operator）：30 秒自动刷新——「谁刚坏了」是要盯的观测量。 */
export function useModelAvailability(username?: string, days?: number, limit?: number) {
  return useQuery({
    queryKey: adminKey(username, "model-availability", days, limit),
    queryFn: () => fetchModelAvailability(days, limit),
    refetchInterval: 30_000,
  });
}

/**
 * 一键把模型加入黑名单：读当前 `MODEL_BLOCKLIST` → 追加 → 写回。
 *
 * 走既有 `PUT /api/settings`（admin + CSRF + 审计），不新开写接口：黑名单是
 * 运行时配置，旁路写会绕过校验与留痕。写完失效目录、可用性与 Playground 列表
 * 三个缓存——它们都读黑名单（现算），不失效会继续显示已被滤掉的模型。
 */
export function useBlockModel(username?: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (pattern: string) => {
      const settings = await api.settings();
      const current = settings.settings.find((item) => item.key === "model_blocklist");
      const raw = typeof current?.value === "string" ? current.value : "";
      return api.updateSettings({ model_blocklist: withBlocklistPattern(raw, pattern) });
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminKey(username, "model-catalog") });
      void client.invalidateQueries({ queryKey: adminKey(username, "model-catalog-page") });
      void client.invalidateQueries({ queryKey: adminKey(username, "model-availability") });
      void client.invalidateQueries({ queryKey: adminKey(username, "settings") });
      void client.invalidateQueries({ queryKey: ["playground-models"] });
    },
  });
}

/**
 * 探活结论 + 当前负缓存。60 秒自动刷新：后台探活周期以小时计，但「人工点了
 * 探活之后结论什么时候回来」要能自己冒出来（一次探活最长 60 秒超时）。
 */
export function useModelProbes(username?: string, enabled = true) {
  return useQuery({
    queryKey: adminKey(username, "model-probes"),
    queryFn: fetchModelProbes,
    enabled,
    refetchInterval: 60_000,
  });
}

/**
 * 立即探活：成功即清负缓存。写完失效探活表、可用性视图、目录页与 Playground
 * 列表——负缓存一清，模型立刻回到池子，这几处的数据都会变。
 */
export function useProbeModel(username?: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ provider, model }: { provider: string; model: string }) =>
      probeModel(provider, model),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminKey(username, "model-probes") });
      void client.invalidateQueries({ queryKey: adminKey(username, "model-availability") });
      void client.invalidateQueries({ queryKey: adminKey(username, "model-catalog-page") });
      void client.invalidateQueries({ queryKey: ["playground-models"] });
    },
  });
}

/** 人工接管 / 交还自动探活（不改负缓存，只改「后台要不要继续探」）。 */
export function useSetProbeManual(username?: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ provider, model, on }: { provider: string; model: string; on: boolean }) =>
      setModelProbeManual(provider, model, on),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminKey(username, "model-probes") });
    },
  });
}
