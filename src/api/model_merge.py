"""模型列表的归一 / 命名规则（纯函数，不依赖 Services）。

本模块只管「一组上游模型表 → 对外条目 + 别名表」的纯计算，缓存 / TTL /
拉取编排在 `api.models`。拆开是因为命名规则本身就占了大半篇幅（六条渠道的
命名对齐、重名退回、对外 id 消歧），与服务层的缓存生命周期无关。

**命名三字段**（2026-10 起）：六条渠道（codebuddy / trae / zen / kilo /
qoder / codearts）的每个模型统一成三个字段，规则见 `provider.naming`：

1. **raw_id**：渠道请求时真正发的 key，**永不改动**。各渠道内部代号互不相同
   （Qoder `kmodel_latest` = TRAE `kimi-k3-1`），也可能带厂商命名空间前缀
   （kilo 的 `kilo-auto/free`、`nvidia/nemotron-3-ultra-550b-a55b:free`）。
2. **归一键**（`normalize_model_key`）：剥掉噪声标记（免费尾缀 `-free`/`_free`/
   `:free`、路径段 `free`、括号词 `(free)`；新版标记 `-new` / `(new)`）与厂商
   命名空间前缀，再 slug 化——`kilo-auto`、`longcat-2.5-preview`。
   **它是对外 id 的主体**（见下）。
3. **展示名**（`display_model_name`）：清洗后的可读文本 `LongCat 2.5
   Preview`。上游给了可读名就用它（Qoder `Qwen3.8-Max` → `Qwen3.8 Max`），
   没有就从 id 现派生（zen / kilo 多数如此）；品牌与缩写按表纠正大小写。

**每模型一条、不跨渠道合并（2026-10-16 起）**：同一模型在三条渠道都有时
列表里就是三条，**对外 id = `归一键-渠道`**（`kimi-k3-codebuddy` /
`kimi-k3-qoder` / `kimi-k3-trae`）——看到名字即知来源厂商，请求该 id
经别名表收窄后**钉死该渠道**（`kimi-k3-qoder` 永远走 qoder，转发时换回
`kmodel_latest`），行为可预测、故障一眼看出是哪家。原先「同名并成一条、
多渠道健康轮转」的合并口径取消；跨渠道自动选号改由显式的兼容组
（`MODEL_FALLBACK_GROUPS`）与各厂商自己的自动档模型（如 `org-auto`，
语义是「该厂商自动选模型」，照样带厂商后缀）承担。

**对外 id 的撞名退回**：同渠道内两条模型的归一键可能撞（CB 的
`hy4-preview` / `hy4-preview-x` 都叫「Hy4 Preview」、`foo` 与 `foo-free`
归一后同键），此时后来者依次退回「原代号的归一键 → 数字后缀」，保证
`归一键-渠道` 全局唯一。跨渠道永不撞：后缀把 id 钉在各自渠道上
（`{base}-{渠道}`，渠道 id 互不为对方的连字符后缀时天然唯一，已知渠道满足）。

**请求侧的三种旧写法仍可直连**：裸归一键（`kimi-k3`）、展示名（`Kimi K3`）、
原代号（`kmodel_latest`）都登记进别名表（见 `_register_aliases`）。裸名在
同名模型的各渠道各登记一份，故裸名请求仍按登记渠道扇出（旧口径的自动选号
语义）；要钉渠道就用对外 id（`kimi-k3-qoder`）或 `@` 语法（`kimi-k3@qoder`，
由 `model_resolver.resolve` 劈分）。

**哨兵名**（`auto` / `default`，见 `_SENTINEL_LABELS`）是各上游自己的
「自动路由 / 默认模型」占位，展示名语义只在本渠道内成立，对外 id 退回
**原代号的归一键**（kilo 的 `kilo-auto/free` → `kilo-auto-kilo`），并按
「展示名不入别名」处理。同渠道内展示名重名（CB 的 `hy4-preview` /
`hy4-preview-x`）同样退回原代号归一键、展示名不入别名——歧义的名字登记
成别名会把请求路由到错的模型。

**能力分（2026-10 起）**：每个条目可带可选 `benchmarks` 字段——Artificial
Analysis 的三项指数（智能 / 编程 / 智能体），经 OpenRouter 公开接口分发
（`src/benchmarks.py`）。匹配口径与价表共用 `model_match.lookup`：按对外 id、
归一键与展示名三路取候选键等值查表，**唯一命中才采用**，查不到或有歧义就不带
该字段（宁可不配也不错配）。渠道内部占位模型（`custom_model_*`、`*_subagent`）
本来就不该有分，匹配不到即无字段。数据是第三方成绩，不是本服务实测，故随条目
透出 `source` 与 `source_model`（上游原始 id）供溯源。

**黑名单过滤在每个出口现做**：`_block_names` / `_blocked` 按**归一后的对外
写法**匹配（原代号、原代号的归一键、展示名、展示名的归一键，以及各自拼
`-渠道` 的对外 id 形态六种写法任一命中即滤），用户照列表里看到的
`kimi-k3-qoder` / `Kimi K3` 写也能生效。
"""

from __future__ import annotations

import logging
from fnmatch import fnmatch
from typing import Any

from ..benchmarks import BenchmarkTable
from ..model_match import lookup
from ..provider.base import Model
from ..provider.naming import display_model_name, normalize_model_key  # noqa: F401

logger = logging.getLogger(__name__)

# 响应透传的元数据字段（Model → OpenAI 额外字段）。`name` 放在最前：
# 上游人类可读名（Qoder 的 `Qwen3.8-Max`），前端优先展示它而非内部代号。
_META_FIELDS = (
    "name",
    "credit_rate",
    "max_input_tokens",
    "max_output_tokens",
    "supports_images",
    "supports_tool_call",
    "supports_reasoning",
    "default_effort",
)

# 展示排序权重：CodeBuddy / TRAE 优先，其余渠道（zen / kilo 等）在后。
_PROVIDER_RANK = {
    "codebuddy": 0,
    "codebuddy_intl": 1,
    "trae": 2,
    "qoder": 3,
    "codearts": 4,
}
_UNRANKED_PROVIDER = 5

# 哨兵名：各上游自己的「自动路由 / 默认模型」占位，语义只在本渠道内成立。
# kilo 的 `kilo-auto/free`（上游名 `Auto Free`）与 Qoder 的 `auto`
# 都归一成 `auto`，却是完全不同的东西。
_SENTINEL_LABELS = frozenset({"auto", "default"})


def _model_label(model: Model) -> str:
    """模型 → 展示名（清洗后）。

    上游给了可读名就用它（Qoder 的 `Qwen3.8-Max`、TRAE 的 `Kimi-K3`），没有就
    从 id 现派生（zen / kilo 多数如此）。两条路径都过 `display_model_name`，
    把 free 标记、新版标记、命名空间前缀与分隔符统一掉，得到 `Qwen3.8 Max` /
    `LongCat 2.5 Preview` 这类可直接展示的文本。清洗后为空（上游名就是 `free`
    之类）返回空串，调用方回退原始 id。
    """
    return display_model_name(model.name or model.id)


def _sort_key(entry: dict[str, Any]) -> tuple[int, str]:
    """定稿前的排序键：渠道优先级升序，同级按归一键字典序。

    归一键（`entry["key"]`）而非对外 id：`_sort_key` 要在 `_disambiguate`
    之前跑（对外 id 要等撞名退回后才能定），而 `_disambiguate` 需要一个确定的
    先后顺序来决定撞 id 时谁保留——按渠道优先级排，「靠前的渠道优先」与列表
    展示口径一致。
    """
    rank = min(
        _PROVIDER_RANK.get(pid, _UNRANKED_PROVIDER) for pid in entry["providers"]
    )
    return rank, entry["key"]


def _block_names(model: Model, provider_id: str) -> tuple[str, ...]:
    """一个模型可供黑名单匹配的全部写法。

    对外 id 改为 `归一键-渠道` 后，用户照列表里看到的**对外 id**（`kimi-k3-
    qoder`）写黑名单也必须命中；同时保留四种归一写法与拼 `-渠道` 的形态：

    * 原代号（`kmodel_latest`、`custom_model_claude`）；
    * 原代号的归一键（兜底/哨兵条目的归一主体，Kilo `kilo-auto/free` → `kilo-auto`）；
    * 展示名（`Kimi K3`）；
    * 展示名的归一键（正常条目的归一主体，`kimi-k3`）；
    * 以上归一写法各拼 `-{渠道}`（对外 id 形态，`kimi-k3-qoder`）。

    保留原代号是因为默认规则带下划线（`custom_model_*`），归一键把下划线
    换成连字符后并不命中，只有原代号能匹配。
    """
    label = _model_label(model)
    names = {model.id, normalize_model_key(model.id), label, normalize_model_key(label)}
    names.discard("")  # 清洗后退化的空名不参与匹配（否则 `*` 会命中空串）
    # 对外 id 形态：归一写法拼 `-{渠道}`（后缀只加给非空写法）
    names |= {f"{name}-{provider_id}" for name in tuple(names) if name}
    return tuple(names)


def _blocked(model: Model, patterns: tuple[str, ...], provider_id: str) -> bool:
    """该模型是否命中黑名单：任一种对外写法命中任一 glob 即滤。

    两边都忽略大小写：用户照展示名写（`Kimi K3`）与照归一键写（`kimi-k3`）
    应等价命中，不能只把被匹配方转小写、模式原样比对（`Kimi-K3` 会漏）。
    """
    return any(
        fnmatch(name.lower(), pattern.lower())
        for name in _block_names(model, provider_id)
        for pattern in patterns
    )


def _entry_base(model: Model, duplicate_labels: set[str]) -> tuple[str, bool]:
    """一个模型的对外 id 主体（撞名退回前）与「展示名是否可入别名」。

    返回 `(归一键主体, 是否按名入键)`：第二个值必须由本函数判定，不能事后拿
    `主体 == 归一展示名` 反推——重名退回时两者可能碰巧相等（CodeBuddy 的
    `hy4-preview` 与其展示名「Hy4 Preview」归一后同键），反推会把它误当成
    「按名入键」，进而把歧义的名字登记成别名。

    有**唯一**展示名时主体取展示名的归一键（`Kimi-K3` → `kimi-k3`），
    展示名可作别名；以下情况退回**原代号的归一键**、展示名不入别名：

    * 展示名清洗后为空（上游名就是 `free` 之类）；
    * 展示名是**哨兵名**（`auto` / `default` 等，见 `_SENTINEL_LABELS`）：
      这些是各上游自己的「自动路由 / 默认模型」占位，语义只在本渠道内成立；
    * 展示名在**本渠道内重名**（CodeBuddy 的 `hy4-preview` / `hy4-preview-x`
      都叫「Hy4 preview」）：重名的几个模型靠不同的原代号归一值自然分开
      （`hy4-preview` / `hy4-preview-x`），其中 id 恰好自洽的那个主体不变。

    除上述外一律不退回原 id 当主体：原 id 是各渠道内部代号，展示名才是跨渠道
    能对上的身份（`kmodel_latest` 的主体是 `kimi-k3` 而非 `kmodel-latest`）。
    """
    label_key = normalize_model_key(_model_label(model))
    if not label_key or label_key in _SENTINEL_LABELS or label_key in duplicate_labels:
        return normalize_model_key(model.id), False
    return label_key, True


def _collect_models(
    grouped: dict[str, dict[str, Any]],
    provider_id: str,
    models_by_lower: dict[str, Model],
) -> None:
    """把单个上游的 {小写 id: Model} 逐模型收进 grouped（**每模型一条**）。

    同一模型在别的渠道还有条目时各归各（不合并）：对外 id 带渠道后缀，
    看到名字即知来源厂商，请求即钉死该渠道。元数据不再跨渠道补缺——
    每条就是该渠道自己的元数据（`by_provider.{渠道}` 与顶层同源）。
    合并键 / 主体见 `_entry_base`，别名表在定稿阶段统一登记（见
    `_register_aliases`）。
    """
    # 先数本渠道内的重名（按清洗后的展示名归一键），用于把冲突项退回原代号键。
    label_keys = [
        normalize_model_key(_model_label(model)) for model in models_by_lower.values()
    ]
    label_counts: dict[str, int] = {}
    for label_key in label_keys:
        label_counts[label_key] = label_counts.get(label_key, 0) + 1
    duplicate_labels = {key for key, count in label_counts.items() if count > 1}
    for model in models_by_lower.values():
        base, name_keyed = _entry_base(model, duplicate_labels)
        label = _model_label(model)
        meta = {field: getattr(model, field) for field in _META_FIELDS}
        meta["name"] = label  # 展示名清洗后覆盖上游原文（对外 name）
        # 每模型一条：组键带渠道与原代号，永不与别条合并。
        grouped[f"{provider_id}\x00{model.id.lower()}"] = {
            "key": base,
            "base": base,
            "providers": {provider_id},
            "raw_ids": {provider_id: model.id},
            "meta": meta,
            "label": label,
            "name_keyed": name_keyed,
        }


def _disambiguate(entries: list[dict[str, Any]]) -> None:
    """保证对外 id 全局唯一：同渠道撞名时后者退回，再拼 `{归一键}-{渠道}`。

    对外 id 都取 `归一键-渠道`，多数情况天然唯一（渠道后缀把 id 钉在各自
    渠道上，跨渠道永不撞）；仍可能撞的只有**同渠道内**归一键相同**：兜底/
    哨兵条目与正常条目的归一主体可能相等（kilo 一个模型的原代号归一后恰好
    等于另一个模型的展示名归一键）。撞车时两条 entry 共用一个 id 会让前端
    选择器的 `key` 与该渠道的别名表互相覆盖。

    保留先定稿的那个（同 `_sort_key` 序，靠前的渠道优先），把后来的换成
    「原代号的归一键」（用户最熟悉的 key）；再不行加数字后缀，保证一定唯一。
    """
    taken: dict[str, set[str]] = {}
    for entry in entries:
        provider_id = sorted(entry["providers"])[0]
        used = taken.setdefault(provider_id, set())
        for candidate in (
            entry["base"],
            normalize_model_key(entry["raw_ids"][provider_id]),
        ):
            if candidate and candidate.lower() not in used:
                entry["base"] = candidate
                break
        else:
            entry["base"] = _unique_suffix(entry["base"], used)
        used.add(entry["base"].lower())
        entry["id"] = f"{entry['base']}-{provider_id}"


def _unique_suffix(base: str, taken: set[str]) -> str:
    """`base` 已被占用时追加数字后缀直到唯一（最后一道兜底）。"""
    index = 2
    while f"{base}-{index}".lower() in taken:
        index += 1
    return f"{base}-{index}"


def _register_aliases(
    entries: list[dict[str, Any]], aliases: dict[str, dict[str, str]]
) -> None:
    """把「请求名 → 渠道原 id」写进别名表（按渠道各记一份）。

    `executor.upstream_model_name` 在真正转发时据此换回各渠道能识别的原 id，
    **raw key 到此为止不再改动**。每渠道登记的键：

    * **对外 id**（`kimi-k3-qoder`）：列表里看到的值；请求它时候选收窄只剩
      登记它的这条渠道，等于钉死渠道；
    * **裸归一键**（`kimi-k3`）：同名模型在各渠道各登记一份，裸名请求仍按
      登记渠道扇出（旧口径的自动选号语义）；
    * **原代号**（`kmodel_latest`）：手写直连；
    * **展示名**（`Kimi K3`，仅「按展示名入键」的模型）：人类可读写法直连；
      同渠道重名项**不**登记（CB 的 `hy4-preview` / `hy4-preview-x` 都叫
      「Hy4 Preview」，登记任一个都会指向错误模型）。
    """
    for entry in entries:
        provider_id = sorted(entry["providers"])[0]
        provider_aliases = aliases.setdefault(provider_id, {})
        raw_id = entry["raw_ids"][provider_id]
        provider_aliases[entry["id"].lower()] = raw_id
        provider_aliases[entry["base"].lower()] = raw_id
        provider_aliases[raw_id.lower()] = raw_id
        display = entry["label"]
        if entry["name_keyed"] and display:
            provider_aliases[str(display).lower()] = raw_id


def _build_aliases(entries: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """定稿全部条目（算出对外 id）并产出别名表。"""
    _disambiguate(entries)
    aliases: dict[str, dict[str, str]] = {}
    _register_aliases(entries, aliases)
    return aliases


def _entry_response(
    entry: dict[str, Any], benchmarks: BenchmarkTable | None = None
) -> dict[str, Any]:
    """定稿后的条目 → OpenAI 兼容响应条目。

    对外 id 是 `归一键-渠道`（`kimi-k3-qoder`）——看到名字即知来源厂商，
    请求即钉死该渠道。`by_provider.{pid}` 给出该渠道的 `raw_id`（转发时真正
    发的 key）与倍率：`raw_id` 一并透出，让用户能直接看到「选这个模型实际会
    发什么 key」（kilo 的 `kilo-auto/free`、Qoder 的 `kmodel_latest`），排障
    不必再翻服务端日志。

    `benchmarks` 非空时按对外 id、归一键与展示名三路查能力分（唯一命中才带），
    查不到就不带该字段——调用方不传（默认 None）时行为与加该字段前完全一致。
    """
    provider_id = sorted(entry["providers"])[0]
    result: dict[str, Any] = {
        "id": entry["id"],
        "object": "model",
        "owned_by": "Coding2API",
        "providers": [provider_id],
        # 空串按「无该字段」处理：`name`/`default_effort` 未提供时是 ""，
        # 透传空串会让前端显示空模型名或多余的 `default_effort: ""`。
        **{
            field: value
            for field, value in entry["meta"].items()
            if value is not None and value != ""
        },
    }
    detail: dict[str, Any] = {"raw_id": entry["raw_ids"][provider_id]}
    if entry["meta"]["credit_rate"] is not None:
        detail["credit_rate"] = entry["meta"]["credit_rate"]
    result["by_provider"] = {provider_id: detail}
    score = lookup(
        benchmarks or {}, entry["id"], entry["base"], entry["meta"].get("name")
    )
    if score is not None:
        result["benchmarks"] = score
    return result


def _visible(
    models_by_lower: dict[str, Model], patterns: tuple[str, ...], provider_id: str
) -> dict[str, Model]:
    """按当前黑名单过滤一个上游的模型表（缓存里存的是未过滤的原始表）。"""
    return {
        lower: model
        for lower, model in models_by_lower.items()
        if not _blocked(model, patterns, provider_id)
    }
