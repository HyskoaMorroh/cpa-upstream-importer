"""段前置闸：用一次目录 GET 判定该上游具备哪些协议，避免为必然不通的段跑完整画像梯。

背景（2026-10-09 本机实测，某聚合站）
------------------------------------------------
同一上游填进 cc-switch 能直连、填进 CPA 后 503 的那类站，真正的原因是站方
只认特定客户端 —— 而现行探测对每个候选**无条件**跑全部四段
（`pipeline.Prober.probe` 里 `for sec in SECTIONS`），于是：

  · claude 段 9 档画像梯、codex 段 7 档、compat 段 7 档、gemini 段 4 档，
    合计 27 档，且每档还可能带模型扫描；
  · 站方目录里根本没有 gemini 家族的站，仍要把 gemini 段那 4 档全部打完
    才判「死路」。

实测证据（裸 GET，不计费、不计入调用统计）：
  · `{base}/v1/models` 带裸 `x-api-key`          -> 401 unauthorized client detected
  · `{base}/v1/models` 带 claude-cli UA + x-app  -> 200，且响应里带
    `supported_endpoint_types:["anthropic","openai"]`
  · 同一主机 10 把不同 Key 返回**完全相同**的目录

所以「这个站有哪些协议」这件事，站方自己在一次 GET 里就说清楚了。用它做
前置闸，比逐个段去撞便宜一个数量级。

设计约束（与 fail-closed 的持久账本严格区分）
------------------------------------------------
本模块的所有判定都是 **fail-open**：

  · 目录读不到（非 200 / 超时 / 解析为空 / 策略拒绝）-> 返回 None，
    调用方照常探全部四段；
  · 判出的段集合为空 -> 同样返回 None，不作「无可用段」的结论。

绝不能因为一次预扫失败就把站判死 —— 那正是现行 `provider_stopped` 永久
封禁链造成的故障（`probe_policy._assess` 见到 restriction_evidence 就
永久返回 provider_stopped，`total_calls=0`，站再也没有机会被验证）。

手动优先
--------
操作员在界面上显式选定段时（`sections_hint`），本模块完全不跑 —— 手动
选择是零请求的，没有理由再用一次 GET 去推翻人的判断。
"""

from __future__ import annotations

from .parse import SECTIONS

# 站方 `supported_endpoint_types` 取值 -> 本项目段名。
#
# 为什么按端点数而不是按模型族：端点是站方**声明**的能力，族要从模型名
# 反推。两者都拿得到时取并集（保守侧：并集只会多探不会漏探）。
#
# `openai` 同时映射到 compat 与 codex 两段，这是刻意的**保守**映射：站方报
# "openai" 只说明它走 OpenAI 形态，并不能区分 `/chat/completions` 与
# `/responses`。两段共用一个 base（`/v1`），少探一段省不下多少，探错一段
# 却会让一个可用站缺一条通道 —— 这里偏保守。
_ENDPOINT_SECTIONS: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-api-key",),
    "claude": ("claude-api-key",),
    "openai": ("openai-compatibility", "codex-api-key"),
    "openai-compatible": ("openai-compatibility", "codex-api-key"),
    "responses": ("codex-api-key",),
    "gemini": ("gemini-api-key",),
    "google": ("gemini-api-key",),
}

# 模型名族 -> 段名。只用于「目录里出现该族」这一条正向证据。
_FAMILY_SECTIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("gemini-", "gemini_"), "gemini-api-key"),
    (("claude-", "claude_"), "claude-api-key"),
    (("gpt-", "gpt_", "o1-", "o3-", "o4-", "codex-"), "codex-api-key"),
)


def family_of(name: str) -> str:
    """从模型名反推族。给不出时返回段名对应的族键（''表示未知）。"""
    low = (name or "").strip().lower()
    if not low:
        return ""
    for prefixes, section in _FAMILY_SECTIONS:
        if low.startswith(prefixes):
            return section
    return ""


def _sections_from_endpoint_types(types: object) -> set[str]:
    found: set[str] = set()
    if not isinstance(types, (list, tuple, set)):
        return found
    for raw in types:
        if not isinstance(raw, str):
            continue
        for section in _ENDPOINT_SECTIONS.get(raw.strip().lower(), ()):
            found.add(section)
    return found


def _sections_from_models(models: object) -> set[str]:
    found: set[str] = set()
    if not isinstance(models, (list, tuple, set)):
        return found
    for raw in models:
        if not isinstance(raw, str):
            continue
        section = family_of(raw)
        if section:
            found.add(section)
    return found


def sections_for_catalog(models: object, endpoint_types: object = None) -> list[str] | None:
    """由目录结果判定该站应探哪些段。

    返回 None 表示**无法判定**，调用方必须回落全部四段（fail-open）。
    返回非空列表表示只探这些段。绝不会返回空列表 —— 空表示无法判定。

    两类证据**不等价**，这是本函数唯一的判据（2026-10-10 修）
    --------------------------------------------------------
    · `supported_endpoint_types` 是站方**自己声明**的协议集合。站方说它只有
      anthropic 与 openai，那 gemini 段就是真没有 —— 可以据此排除。
    · 模型族是从模型名**反推**的，而目录随时可能是不全的（聚合站按分组返回
      子集、按 Key 权限裁剪、缓存滞后）。拿「目录里没见到 claude-*」去排除
      claude 段，等于把「这次没看见」当成「不存在」。

    所以族证据只允许**加段**，绝不允许独立成为排除依据：没有
    `supported_endpoint_types` 时一律返回 None 回落四段。

    这不是保守过度 —— 它修的是一个实测过的产品缺陷：假上游 `compatonly`
    画像只在 `/v1/models` 返回 `{"data":[{"id":"gpt-5.6-sol"},…]}`（无
    endpoint_types），而 `gpt-` 在族表里只映射 codex 段，于是唯一真正可用的
    `openai-compatibility` 段被**零请求**判掉，站整体变成「无可用段」。
    用户现场反馈的「能用的站被判死」正是这一类。

    代价是会自报 endpoint_types 的站仍然省掉 gemini 段，
    而不自报的站回落旧行为 —— 只少省请求，不会少探段。
    """
    declared = _sections_from_endpoint_types(endpoint_types)
    if not declared:
        # 站方没声明协议集合 —— 无权排除任何段。
        return None
    found = declared | _sections_from_models(models)
    # 按 SECTIONS 原序返回：前端表格与 CLI 输出都依赖这个顺序。
    ordered = [sec for sec in SECTIONS if sec in found]
    return ordered or None


def catalog_hint_applies(endpoint_types: object, models: object) -> bool:
    """是否有可据以排除段的信号。

    口径必须与 `sections_for_catalog` 完全一致 —— 否则调用方会先判「有信号」
    再拿到 None，把一次成功的预扫记成 `prescan-miss`，日志与实际行为打架。
    `models` 保留在签名里只为调用点可读（族证据不参与排除判定）。
    """
    del models  # 族证据只加段，不参与「能否排除」的判定
    return bool(_sections_from_endpoint_types(endpoint_types))


def endpoint_types_from_body(text: str) -> list[str]:
    """从目录响应正文里抽出站方自报的 `supported_endpoint_types`。

    只读这一条：它是站方**声明**的协议能力，比从模型名反推族更硬。取不到时
    返回空列表，调用方靠模型族判定 —— 两条证据取并集，永远偏保守（只会多探
    不会漏探）。

    逐条校验元素类型：站方数据是外部输入，不对它做隐式假设。
    """
    import json

    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    # 形态一：{"data":[{... "supported_endpoint_types":[...]}]}
    # 形态二：{"supported_endpoint_types":[...]}  （整份响应的顶层）
    candidates: list[object] = [data.get("supported_endpoint_types")]
    items = data.get("data") or data.get("models") or []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                candidates.append(it.get("supported_endpoint_types"))
    found: list[str] = []
    for value in candidates:
        if not isinstance(value, list):
            continue
        for raw in value:
            if isinstance(raw, str) and raw.strip() and raw.strip().lower() not in found:
                found.append(raw.strip().lower())
    return found
