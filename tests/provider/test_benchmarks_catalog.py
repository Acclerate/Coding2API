"""OpenRouter 目录：模型目录/能力分构建与网络拉取。"""

from __future__ import annotations

import httpx
import pytest

from src.benchmarks import build_benchmark_table, build_model_catalog, fetch_openrouter_models
from tests.provider._benchmarks_support import OR_PAYLOAD as _OR_PAYLOAD

pytestmark = pytest.mark.real_upstream("openrouter")

# --------------------------------------------------------------- 模型目录构建

def test_build_model_catalog_captures_metadata():
    """同口径选条，保留 OpenRouter 的完整明细（名称 / 上下文 / 模态 / 能力 / 价格）。"""
    raw = {"data": [{
        "id": "z-ai/glm-5.3",
        "name": "Z.ai: GLM 5.3",
        "context_length": 1048576,
        "knowledge_cutoff": "2026-02-16",   # 上游给的就是日期字符串
        "architecture": {"input_modalities": ["text", "image", 7],
                         "output_modalities": ["text"]},
        "top_provider": {"max_completion_tokens": 131072},
        "supported_parameters": ["reasoning", "tools", "structured_outputs"],
        "pricing": {"prompt": "0.000001", "completion": "0.000005",
                    "input_cache_read": "0.0000001",
                    "input_cache_write": "0.00000125"},
    }]}
    catalog = build_model_catalog(raw)
    assert set(catalog) == {"z-ai/glm-5.3"}
    row = catalog["z-ai/glm-5.3"]
    assert row["id"] == "z-ai/glm-5.3"
    assert row["provider"] == "z-ai"          # id 的厂商前缀
    assert row["name"] == "Z.ai: GLM 5.3"
    assert row["knowledge"] == "2026-02-16"
    assert row["context"] == 1048576 and row["max_output"] == 131072
    assert row["input_modalities"] == ["text", "image"]     # 非串元素丢弃
    assert row["output_modalities"] == ["text"]
    assert row["attachment"] is True
    assert row["reasoning"] is True and row["tool_call"] is True
    assert row["structured_output"] is True
    assert row["input"] == pytest.approx(1.0) and row["output"] == pytest.approx(5.0)
    assert row["cache_read"] == pytest.approx(0.1)
    assert row["cache_write"] == pytest.approx(1.25)
    # 旧目录有、OpenRouter 没有的三列：给 None / False，不改前端契约
    assert row["family"] is None and row["release_date"] is None
    assert row["open_weights"] is False

def test_build_model_catalog_tolerates_missing_fields():
    """缺字段 / 类型不符一律收敛为 None / 空 / False；缓存读缺失按 input 原价。"""
    raw = {"data": [{
        "id": "m",
        "name": "",
        "context_length": True,              # 布尔 → None
        "architecture": None,                # 非 dict → 空模态
        "pricing": {"prompt": "0.000002"},   # 无 output / cache_read / cache_write
        "supported_parameters": "not-a-list",
    }]}
    row = build_model_catalog(raw)["m"]
    assert row["name"] is None
    assert row["context"] is None and row["max_output"] is None
    assert row["input_modalities"] == [] and row["output_modalities"] == []
    assert row["output"] == 0.0
    assert row["cache_read"] == pytest.approx(2.0)      # 缺失按 input 原价
    assert row["cache_write"] is None
    assert row["reasoning"] is False and row["structured_output"] is False
    assert row["family"] is None
    # top_provider / architecture 整体非 dict 的分支
    raw2 = {"data": [{"id": "m", "top_provider": "bad", "architecture": "bad",
                      "pricing": {"prompt": 1}}]}
    assert build_model_catalog(raw2)["m"]["max_output"] is None

@pytest.mark.parametrize("cutoff,expected", [
    ("2026-02-16", "2026-02-16"),          # 上游真实形态：日期字符串
    ("  2026-02-16  ", "2026-02-16"),      # 两端空白照收
    (1750000000, "2025-06-15"),            # 数值（epoch 秒）仍兼容
    ("2026/02/16", None),                  # 别的日期格式不猜
    ("2026-2-16", None),                   # 不补零
    ("20260216", None),
    ("", None),
    (None, None),
    (-1, None),
    (True, None),
    (1e30, None),                          # 溢出的 epoch → None，不抛
])
def test_build_model_catalog_knowledge_cutoff_shapes(cutoff, expected):
    """知识截止只认 `YYYY-MM-DD`（上游真实形态）；别的写法宁可显示 — 也不给错日期。"""
    raw = {"data": [{"id": "m", "knowledge_cutoff": cutoff,
                     "pricing": {"prompt": "0.000001"}}]}
    assert build_model_catalog(raw)["m"]["knowledge"] == expected

def test_build_model_catalog_skips_unpriced():
    """没有刊例价的条目不进目录（页面「成本」列无意义）。"""
    raw = {"data": [{"id": "m", "name": "M"}]}     # 无 pricing
    assert build_model_catalog(raw) == {}

def test_build_model_catalog_empty_on_bad_raw():
    assert build_model_catalog(None) == {}
    assert build_model_catalog({"data": "x"}) == {}

def test_build_model_catalog_provider_without_slash():
    """id 无 '/' 时 provider 列给空串（不臆造厂商）。"""
    raw = {"data": [{"id": "bagelmix", "name": "Bagel", "pricing": {"prompt": 1}}]}
    assert build_model_catalog(raw)["bagelmix"]["provider"] == ""

def test_build_benchmark_table_indexes_by_match_keys():
    """三项指数入表；null 不带字段；完全无分数的模型不入表。"""
    table = build_benchmark_table(_OR_PAYLOAD)
    assert table["glm-5.3"] == {
        "source": "openrouter", "source_model": "z-ai/glm-5.3",
        "intelligence_index": 44.8, "coding_index": 74.8,
        "agentic_index": 53.1}
    # 展示名一路也能查（上游 id 带厂商前缀）
    assert table["kimi-k3"]["intelligence_index"] == 43.6
    assert "agentic_index" not in table["kimi-k3"]
    assert "grok-no-score" not in table

@pytest.mark.parametrize("raw", [
    None, [], "string", 42,
    {},
    {"data": "not-a-list"},
    {"data": [None, "x", 5]},
    {"data": [{"id": "a", "benchmarks": {"artificial_analysis": "bad"}}]},
    {"data": [{"id": "a", "benchmarks": "bad"}]},
    {"data": [{"benchmarks": {"artificial_analysis": {"intelligence_index": 1}}}]},
    {"data": [{"id": "a", "benchmarks": {"artificial_analysis": {
        "intelligence_index": True, "coding_index": -1, "agentic_index": "x"}}}]},
    {"data": [{"id": "a", "benchmarks": {"other_block": {"intelligence_index": 1}}}]},
])
def test_build_benchmark_table_tolerates_malformed(raw):
    """结构异常/非数值一律安静跳过，绝不让模型列表挂掉。"""
    assert build_benchmark_table(raw) == {}

def test_build_benchmark_table_rejects_nan_and_infinite():
    """NaN / inf 不是有效指数：不当字段收。"""
    table = build_benchmark_table({"data": [
        {"id": "a", "benchmarks": {"artificial_analysis": {
            "intelligence_index": float("nan"), "coding_index": float("inf"),
             "agentic_index": float("-inf")}}},
        {"id": "b", "benchmarks": {"artificial_analysis": {
            "intelligence_index": 0}}},
    ]})
    assert table == {"b": {"source": "openrouter", "source_model": "b",
                           "intelligence_index": 0}}

# ------------------------------------- 浮动别名取同族最新快照（2026-10-19）

def _snapshot_entry(model_id, *, canonical, score, name=None, created=None):
    """一条带快照声明的上游记录（只填能力分需要的字段）。"""
    return {"id": model_id,
            "canonical_slug": canonical,
            "name": name or model_id,
            "created": created,
            "benchmarks": {"artificial_analysis": {"intelligence_index": score}}}

def test_benchmark_table_floating_alias_takes_newest_snapshot():
    """浮动别名跟随上游漂移：取同族最新快照的分（复现 24.7 → 39.5 事故）。

    上游把浮动别名钉在旧快照上并换了评测变体（Non-reasoning 24.7），同族
    新快照是 Max 39.5。本项目转发默认开推理，故浮动别名应采用 39.5，且
    `source_model` 指向真正提供分数的 `-1017`。
    """
    table = build_benchmark_table({"data": [
        _snapshot_entry("deepseek/deepseek-v4.1-flash",
                        canonical="deepseek/deepseek-v4.1-flash-20260910",
                        score=24.7, name="DeepSeek: DeepSeek V4.1 Flash"),
        _snapshot_entry("deepseek/deepseek-v4.1-flash-1017",
                        canonical="deepseek/deepseek-v4.1-flash-20261017",
                        score=39.5, name="DeepSeek: DeepSeek V4.1 Flash 1017"),
    ]})
    floating = table["deepseek-v4.1-flash"]
    assert floating["intelligence_index"] == 39.5
    assert floating["source_model"] == "deepseek/deepseek-v4.1-flash-1017"
    # 显式快照 id 仍用自己的分（用户点名要的就是它）
    explicit = table["deepseek-v4.1-flash-1017"]
    assert explicit["intelligence_index"] == 39.5
    assert explicit["source_model"] == "deepseek/deepseek-v4.1-flash-1017"

def test_benchmark_table_floating_alias_keeps_newest_regardless_of_order():
    """最新快照先出现时结果不变（不依赖上游数组顺序）。"""
    newest = _snapshot_entry("deepseek/deepseek-v4-pro-0813",
                             canonical="deepseek/deepseek-v4-pro-20260813",
                             score=36.0)
    older = _snapshot_entry("deepseek/deepseek-v4-pro",
                            canonical="deepseek/deepseek-v4-pro-20260423",
                            score=30.4, name="DeepSeek: DeepSeek V4 Pro 0423")
    forward = build_benchmark_table({"data": [newest, older]})
    backward = build_benchmark_table({"data": [older, newest]})
    assert forward["deepseek-v4-pro"]["intelligence_index"] == 36.0
    assert backward["deepseek-v4-pro"]["intelligence_index"] == 36.0
    assert backward["deepseek-v4-pro"]["source_model"] == "deepseek/deepseek-v4-pro-0813"

def test_benchmark_table_floating_alias_without_newer_sibling_keeps_own():
    """没有同族更新快照时，浮动别名用自己的分（不跨家族借分）。"""
    table = build_benchmark_table({"data": [
        _snapshot_entry("deepseek/deepseek-r1",
                        canonical="deepseek/deepseek-r1", score=11.4),
    ]})
    assert table["deepseek-r1"]["intelligence_index"] == 11.4
    assert table["deepseek-r1"]["source_model"] == "deepseek/deepseek-r1"

def test_benchmark_table_does_not_merge_same_base_different_model():
    """只有带日期 canonical 的才算快照序列：无日期的不入家族，不互相借分。

    `mistralai/mistral-large`（Feb '24）与 `mistralai/mistral-large-2512`
    （Mistral Large 3）削完日期同基，却是两个模型——上游没给前者日期戳，
    就是在声明「它不是谁的快照」。
    """
    table = build_benchmark_table({"data": [
        _snapshot_entry("mistralai/mistral-large",
                        canonical="mistralai/mistral-large", score=5.8),
        _snapshot_entry("mistralai/mistral-large-2512",
                        canonical="mistralai/mistral-large-2512", score=9.3),
    ]})
    assert table["mistral-large"]["intelligence_index"] == 5.8
    assert table["mistral-large-2512"]["intelligence_index"] == 9.3

def test_benchmark_table_route_suffix_is_not_a_snapshot_id():
    """`(...)` 分发口径（batch / free）不是快照日期：仍按浮动别名取最新。

    浮动的 `:batch` 与浮动的基名同属一个快照，取最新时同样受益——
    否则会出现「普通别名 39.5、batch 别名 24.7」的自相矛盾。
    """
    table = build_benchmark_table({"data": [
        _snapshot_entry("deepseek/deepseek-v4.1-flash",
                        canonical="deepseek/deepseek-v4.1-flash-20260910",
                        score=24.7),
        _snapshot_entry("deepseek/deepseek-v4.1-flash:batch",
                        canonical="deepseek/deepseek-v4.1-flash-20260910",
                        score=24.7),
        _snapshot_entry("deepseek/deepseek-v4.1-flash-1017",
                        canonical="deepseek/deepseek-v4.1-flash-20261017",
                        score=39.5),
    ]})
    assert table["deepseek-v4.1-flash"]["intelligence_index"] == 39.5
    assert table["deepseek-v4.1-flash-batch"]["intelligence_index"] == 39.5

def test_benchmark_table_floating_alias_keeps_own_when_it_is_newest():
    """浮动别名自己就是最新快照时用自己的分（上游已把别名切到新快照）。"""
    table = build_benchmark_table({"data": [
        _snapshot_entry("deepseek/deepseek-v4.1-flash",
                        canonical="deepseek/deepseek-v4.1-flash-20261017",
                        score=39.5),
    ]})
    assert table["deepseek-v4.1-flash"]["intelligence_index"] == 39.5
    assert table["deepseek-v4.1-flash"]["source_model"] == "deepseek/deepseek-v4.1-flash"

def test_benchmark_table_ignores_snapshot_entries_without_scores():
    """同族最新快照没分数时不参与「最新」判定（避免把空记录当最新）。"""
    table = build_benchmark_table({"data": [
        _snapshot_entry("m-0910", canonical="deepseek/m-20260910", score=24.7),
        {"id": "m-1017", "canonical_slug": "deepseek/m-20261017",
         "benchmarks": {"artificial_analysis": {"intelligence_index": None}}},
    ]})
    assert table["m-0910"]["intelligence_index"] == 24.7

@pytest.mark.parametrize("model_id,canonical,expected", [
    ("a/b", "a/b-20261017", ("a/b", "20261017", True)),         # 浮动别名
    ("a/b-1017", "a/b-20261017", ("a/b", "20261017", False)),   # 显式快照
    ("a/b_1017", "a/b-20261017", ("a/b", "20261017", False)),   # 下划线也认
    ("a/b", "a/b", (None, "", True)),                           # 无日期 canonical
    ("a/b", None, (None, "", True)),                            # 缺字段
    ("a/b-1017", "a/b-20261017:batch", (None, "", False)),      # 尾缀非日期 → 不入家族
    (None, "a/b-20261017", ("a/b", "20261017", True)),          # 缺 id
])
def test_snapshot_info_shapes(model_id, canonical, expected):
    """快照识别：只认 canonical 尾部 8 位日期；浮动与否只看上游 id。"""
    from src.benchmarks import _snapshot_info

    item = {"id": model_id, "canonical_slug": canonical}
    assert _snapshot_info(item) == expected

# --------------------------------------------------------------- 网络拉取

async def test_fetch_openrouter_models_parses_and_raises():
    """MockTransport 下成功返回 JSON；HTTP 错误向上抛（调用方降级）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_OR_PAYLOAD)

    raw = await fetch_openrouter_models(transport=httpx.MockTransport(handler))
    assert raw == _OR_PAYLOAD

    def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_openrouter_models(transport=httpx.MockTransport(failing))

async def test_fetch_openrouter_models_default_transport_branch(monkeypatch):
    """不注入 transport 时走真实 AsyncClient 构造（替身覆盖该分支）。"""
    import httpx

    seen: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url):
            return httpx.Response(200, json=_OR_PAYLOAD,
                                  request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    fetched = await fetch_openrouter_models("https://x")
    assert build_benchmark_table(fetched)["glm-5.3"]["coding_index"] == 74.8
    # 回归：默认必须 trust_env=False，否则宿主/系统代理会劫持目录抓取
    assert seen["trust_env"] is False
