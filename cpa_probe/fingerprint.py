"""响应指纹：判真实后端、判静默换模。

为什么 id 比 model 字段可靠
--------------------------
上游能随手改 model 字段的值，改不了 id 的生成方式。2026-08-28 实测：
betterclau 把 gpt-5.6-sol 换成 agnes-2.0-flash，relay-e 换成 grok-4.6，
但 id 形态暴露了真实后端。

model_matches 的容错原则
-----------------------
actual 为 None 时**返回 True**（无证据不判换模）。早期版本反过来，
造成 100% 假换模率 —— 真因是 read(20000) 截断，/v1/responses 把整个
Codex 系统提示放在 instructions 字段（40KB+），model 排其后被切掉。
"""

from __future__ import annotations

import json
import re

# 响应 id 形态 → 真实后端
_ID_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^msg_bdrk_"), "AWS Bedrock"),
    (re.compile(r"^msg_01[1-9A-HJ-NP-Za-km-z]{10,}$"), "Anthropic 官方"),
    (re.compile(r"^msg_[0-9a-f]{32}$"), "中转自造"),
    (re.compile(r"^chatcmpl-"), "OpenAI Chat 兼容（多为中转）"),
    (re.compile(r"^resp_[0-9a-f]{40,}$"), "OpenAI Responses 官方形态"),
    (re.compile(r"^resp_[0-9a-f]{16,32}$"), "中转自造"),
]

_ID_RE = re.compile(r'"id"\s*:\s*"((?:msg|resp|chatcmpl)[^"]{0,60})"')
_MODEL_RE = re.compile(r'"model"\s*:\s*"([^"]+)"')
_INPUT_TOKENS_RE = re.compile(r'"(?:input_tokens|prompt_tokens)"\s*:\s*(\d+)')


def resp_model(text: str) -> str | None:
    """按 model → response.model → modelVersion 顺序取，失败退化为正则。"""
    try:
        obj = json.loads(text)
    except Exception:
        m = _MODEL_RE.search(text)
        return m.group(1) if m else None

    if not isinstance(obj, dict):
        m = _MODEL_RE.search(text)
        return m.group(1) if m else None

    for path in (("model",), ("response", "model"), ("modelVersion",)):
        cur = obj
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                cur = None
                break
            cur = cur[k]
        if isinstance(cur, str) and cur:
            return cur

    m = _MODEL_RE.search(text)
    return m.group(1) if m else None


def resp_id(text: str) -> str | None:
    m = _ID_RE.search(text)
    return m.group(1) if m else None


def backend_of(rid: str | None) -> str:
    if not rid:
        return "?"
    for pat, name in _ID_RULES:
        if pat.search(rid):
            return name
    if rid.startswith(("msg_", "resp_")):
        return "其他形态"
    return "未知形态"


def input_tokens(text: str) -> int | None:
    m = _INPUT_TOKENS_RE.search(text)
    return int(m.group(1)) if m else None


def _strip_suffix(name: str) -> str:
    n = name.lower()
    for suf in ("-thinking", "-latest"):
        if n.endswith(suf):
            n = n[: -len(suf)]
    return n


# 日期版本后缀：-20260101（8 位）、-0827（4 位）、-2026-01-01（带连字符）。
# 只认纯数字与连字符的尾巴 —— 这是**唯一**能安全放行的后缀形态。
_DATE_TAIL = re.compile(r"^-\d[\d-]*$")


def model_matches(requested: str, actual: str | None) -> bool:
    """宽松匹配。actual 为 None 一律返回 True —— 无证据不判换模。

    为什么不能用裸 startswith（2026-08-31 自查发现的漏判）
    --------------------------------------------------
    原实现是 `act.startswith(req) or req.startswith(act)`，本意只为放行
    日期版本后缀。但同一个判断把**档位降级**也一并放行了 —— 而那正是本模块
    存在的理由。用真实种子模型（pipeline.SEED_MODELS）就能触发：

        gemini-2.5-flash  ->  gemini-2.5-flash-lite   判成同一模型
        gpt-5.6-sol       ->  gpt-5.6-sol-mini        判成同一模型
        claude-opus-5     ->  claude-opus-5-haiku     判成同一模型
        gpt-4             ->  gpt-40                  判成同一模型（不同族）

    站方拿 flash-lite 冒充 flash 照常计费，探测这边一路绿灯，条目被写进
    config.yaml。四个判定点全受影响：pipeline 的 _stage1/_stage2/形态复用，
    以及 writeback 的端到端验证。

    改法：后缀必须是纯数字日期形态才放行。`-mini`/`-lite`/`-haiku`/`-exp-0827`
    都含字母，一律判换模。`_strip_suffix` 处理的 `-thinking`/`-latest` 是另一
    条路 —— 那两个是同一模型的能力开关，不是档位。

    反向（req 比 act 长）也收窄成同一条规则：请求带日期、回复不带，是站方
    省略了版本号，仍算同一模型；请求 `gpt-40` 回 `gpt-4` 则不是。
    """
    if not actual:
        return True

    req = _strip_suffix(requested)
    act = _strip_suffix(actual)
    if req == act:
        return True

    for long_, short in ((act, req), (req, act)):
        if long_.startswith(short) and _DATE_TAIL.match(long_[len(short):]):
            return True

    return False


def truncated(sent_chars: int, text: str, *, ratio: float = 0.5) -> bool:
    """判上游是否截断了上下文 —— 200 不代表真吃下了那么多。

    重复字符实测约 1 char/token。input_tokens 低于发送量的 50% 即视为截断。
    relay-m.example 案例：发 105 万字符只回 132,696 tokens，且模型被换成
    codex-auto-review —— 那个 200 完全不可信。
    """
    tok = input_tokens(text)
    if tok is None or sent_chars <= 0:
        return False
    return tok < sent_chars * ratio


def swap_rate(samples: list[dict]) -> dict:
    """统计静默换模率。samples 每项需含 status / requested / actual。

    分母**不含** unknown —— 拿不到 model 字段不算换模。
    """
    same = swap = unknown = 0
    backends: dict[str, int] = {}
    tokens: list[int] = []

    for s in samples:
        if str(s.get("status")) != "200":
            continue
        actual = s.get("actual")
        if actual is None:
            unknown += 1
        elif model_matches(s.get("requested", ""), actual):
            same += 1
        else:
            swap += 1

        b = s.get("backend")
        if b:
            backends[b] = backends.get(b, 0) + 1
        t = s.get("input_tokens")
        if isinstance(t, int):
            tokens.append(t)

    denom = same + swap
    rate = (swap / denom * 100) if denom else 0.0

    # input_tokens 跨度异常 = 换模强信号（不同后端连系统提示都不同）
    span_anomaly = bool(tokens) and max(tokens) > min(tokens) * 3

    return {
        "same": same,
        "swap": swap,
        "unknown": unknown,
        "rate_pct": round(rate, 1),
        "backends": backends,
        "multi_backend": len(backends) > 1,
        "token_span_anomaly": span_anomaly,
    }
