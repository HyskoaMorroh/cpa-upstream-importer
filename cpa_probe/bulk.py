"""既有上游的**批量管理**：按 (段, 网址) 成组地启停、改档、删除。

为什么这件事必须由本项目做（2026-09-11）
--------------------------------------
CPAMP 的「AI 提供商」页只有逐行操作：表格没有多选列（`ProviderTable` 的
props 全是单行回调），后端 `router.go` 的 provider 段只有 `GET/PUT/DELETE`
三个动词，**没有任何 bulk 路径**。它自己的「按结果应用」是 `for` 循环逐条
read-modify-write，每条都要 `GET /config` + `PUT` 整个数组 —— 158 条规模下
批量启停一次就是 158 轮往返，而且两个标签页同时操作会静默互相覆盖
（它的串行队列只在同一个 JS 进程内有效）。作者不会加这个功能，CPA 与 CPAMP
两个仓库我们都改不了，所以只能在本项目侧解决。

本模块的做法与 CPAMP 相反，也更安全：
  · **行级改写整份 config.yaml 文本**，不重新序列化 —— 注释、未知字段、
    手工排版全部原样保留。CPAMP 走分段 PUT 必须重新序列化，注释一律丢失。
  · 一次往返完成全部改动（写回仍走既有的
    `validate` → `write_local` 备份 → `push_to_cpa` → 读回校验链路）。
  · 定位键用**该段数组的下标**，而不是 CPAMP 的 `api-key + base-url` 查询
    —— 后者在「同 key 同 url 但 proxy/prefix/headers 不同」的合法重复条目上
    命中哪一条是不确定的（`providers.ts:699-704` 的注释自己承认了）。

「启用/停用」在两类段里是**两套字段**，必须分开写
------------------------------------------------
  · key 类段（gemini / codex / claude / xai / vertex / interactions）：
    往 `excluded-models` 里塞通配符 `"*"` 表示停用
    （CPAMP `components/providers/utils.ts:17` 的 DISABLE_ALL_MODELS_RULE）。
  · `openai-compatibility`：用真正的布尔字段 `disabled`
    （CPAMP `AiProvidersPage.tsx:450`）。
写错字段的后果是「界面显示已停用、CPA 照常轮询」，比不做还糟。

本模块只**生成新文本**，不落盘、不推送 —— 落盘与推送仍由 server 的
apply 链路负责，确认门槛一道不少。
"""

from __future__ import annotations

import re
import json
import hashlib

import yaml

from .parse import SECTIONS, host_of
from .writeback import (_detect_indent, _realign_priority, _section_span,
                        _split_comment, validate)

# 停用语义的**内置默认值**。真正生效的值由 `disable_semantics()` 决定 ——
# 它优先从 CPAMP 源码实时解析，解析不到才回落到这里。
# 用户 2026-09-11 的要求：所有改动都要能跟着 CPA / CPAMP 的更新自动同步，
# 不许把上游的常量抄死在本项目里。
DISABLE_ALL = "*"
DISABLED_FIELD = "disabled"


def disable_semantics() -> tuple[str, str, str]:
    """当前应当使用的停用写法。返回 (通配符, 布尔字段名, 来源说明)。

    优先级：CPAMP 源码解析 > 内置默认。
    解析走 `cpa_source_probe`，那里有 6 小时缓存与「失败也缓存」，
    所以这里可以每次调用都问，不会反复付网络代价。

    为什么必须跟着上游走：key 类段与 compat 段的停用是**两套完全不同的字段**
    （`excluded-models` 里的通配符 / 布尔 `disabled`）。CPAMP 哪天改了写法而
    本项目还按旧的写，后果是「界面显示已停用、CPA 照常轮询」—— 静默失效，
    比报错更难发现。
    """
    try:
        from . import cpa_source_probe as _csp
        ident = _csp.cached_identity()
        rule = (ident.cpamp_disable_all_rule or "").strip()
        fld = (ident.cpamp_disabled_field or "").strip()
        if rule and fld:
            return rule, fld, "CPAMP 源码"
        if rule or fld:
            return (rule or DISABLE_ALL, fld or DISABLED_FIELD,
                    "CPAMP 源码（部分）+ 内置默认")
    except Exception:
        pass
    return DISABLE_ALL, DISABLED_FIELD, "内置默认（未能解析 CPAMP 源码）"


# `excluded-models` 的键名是 **CPA config.yaml 的 schema**（config_types.go），
# 不是 CPAMP 的 UI 约定，所以它写死在这里是对的 —— 变的话 CPA 自己会不认。
# 而停用**用什么值**表达（通配符 / 布尔字段名）是 CPAMP 的约定，
# 由 `disable_semantics()` 动态取，见上。
_EX_LINE = re.compile(r"^(\s*)excluded-models\s*:\s*(.*)$")


class BulkError(ValueError):
    """批量操作无法安全执行。带上具体原因，绝不静默跳过。"""


def config_revision(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def entry_fingerprint(entry: dict) -> str:
    return config_revision(json.dumps(entry, sort_keys=True, ensure_ascii=True))


def _set_priority(block: list[str], section: str, value: int,
                  field: str) -> tuple[list[str], bool, str]:
    text = "".join(block)
    try:
        root = yaml.compose(text)
        entry = root.value[0]
        edits = []
        found = False
        for key, node in entry.value:
            if key.value == "priority":
                edits.append((node.start_mark.index, node.end_mark.index, str(value)))
                found = True
            if section == "openai-compatibility" and key.value == "api-key-entries":
                for child in node.value:
                    for nested_key, nested in child.value:
                        if nested_key.value == "priority":
                            edits.append((nested.start_mark.index, nested.end_mark.index, str(value)))
        for start, end, replacement in sorted(set(edits), reverse=True):
            text = text[:start] + replacement + text[end:]
        out = text.splitlines(keepends=True)
        if not found and value != 0:
            if not out[0].endswith("\n"):
                out[0] += "\n"
            out.insert(1, f"{field}priority: {value}\n")
        return out, "".join(out) != "".join(block), ""
    except (yaml.YAMLError, AttributeError, TypeError, IndexError):
        return block, False, "priority 结构无法安全改写"


def _entry_blocks(lines: list[str], section: str) -> list[tuple[int, int]]:
    """把一个段切成 [(起, 止), ...] 的条目块（行号，左闭右开）。

    条目起点判据与 `_orphan_entry_lines` 同口径：段内**该段 dash 缩进**的
    `- ` 行。不用 YAML 解析 —— 解析完再序列化就丢注释了，而保注释是本项目
    写回路径的硬要求（见 writeback 模块 docstring）。
    """
    span = _section_span(lines, section)
    if span is None:
        return []
    start, end = span
    dash, _field = _detect_indent(lines, start, end)
    marks: list[int] = []
    for i in range(start, end):
        s = lines[i]
        if s[len(s) - len(s.lstrip()):].startswith("- ") and \
                (len(s) - len(s.lstrip())) == len(dash):
            marks.append(i)
    if not marks:
        return []
    out = []
    for j, m in enumerate(marks):
        stop = marks[j + 1] if j + 1 < len(marks) else end
        out.append((m, stop))
    return out


def _set_enabled(block: list[str], section: str, enabled: bool,
                 field_indent: str, *, rule: str = "",
                 disabled_field: str = "") -> tuple[list[str], bool, str]:
    """把一个条目块改成启用/停用。返回 (新块, 改没改, 跳过原因)。

    `rule` / `disabled_field` 由调用方从 `disable_semantics()` 取，
    默认空时回落到内置常量 —— 这样上游改了写法本项目能跟着变。
    """
    rule = rule or DISABLE_ALL
    disabled_field = disabled_field or DISABLED_FIELD
    if block and re.match(r"^\s*-\s+(?:excluded-models|" + re.escape(disabled_field) + r")\s*:", block[0]):
        # Normalize a first-field target temporarily; restore the dash after
        # editing so the ordinary field-level path preserves the other fields.
        dash = field_indent[:-2]
        first = re.sub(r"^\s*-\s+", field_indent, block[0])
        normalized = [dash + "- __bulk_anchor__: null\n", first] + block[1:]
        edited, changed, why = _set_enabled(normalized, section, enabled,
                                           field_indent, rule=rule,
                                           disabled_field=disabled_field)
        edited = edited[1:]
        for i, line in enumerate(edited):
            if line.strip() and not line.lstrip().startswith("#"):
                edited[i] = dash + "- " + line[len(field_indent):]
                break
        return edited, changed, why
    if section == "openai-compatibility":
        # 布尔字段 disabled
        want = "false" if enabled else "true"
        for i, line in enumerate(block):
            m = re.match(rf"^(\s*){re.escape(disabled_field)}\s*:\s*(.*)$",
                         line.rstrip("\n"))
            if m and len(m.group(1)) == len(field_indent):
                cur = m.group(2).split("#")[0].strip().lower()
                if cur == want:
                    return block, False, ""
                nb = list(block)
                if enabled:
                    # 启用就**删掉整行**而不是写 `disabled: false`。
                    # 「没有这一行」与 `disabled: false` 对 CPA 完全等价，
                    # 而删行让「停用→启用」逐字节回到原文 —— 多数 provider
                    # 本来就没有这一行，留个 false 会产生一处永久 diff。
                    del nb[i]
                    return nb, True, ""
                nl = "\n" if line.endswith("\n") else ""
                nb[i] = f"{m.group(1)}{disabled_field}: {want}{nl}"
                return nb, True, ""
        if enabled:
            return block, False, ""          # 本来就没有 disabled 行 = 启用中
        nb = list(block)
        nb.insert(1, f"{field_indent}{disabled_field}: true\n")
        return nb, True, ""

    # key 类段：excluded-models 里的通配符
    for i, line in enumerate(block):
        m = _EX_LINE.match(line.rstrip("\n"))
        if not m or len(m.group(1)) != len(field_indent):
            continue
        tail = m.group(2).strip()
        end = i + 1
        while end < len(block):
            ln = block[end]
            if ln.strip() and not ln.lstrip().startswith("#"):
                indent = len(ln) - len(ln.lstrip())
                if indent < len(field_indent) or (
                        indent == len(field_indent) and not ln.lstrip().startswith("- ")):
                    break
            end += 1
        try:
            fragment = "".join(block[i:end])
            items = yaml.safe_load(fragment)["excluded-models"] or []
            if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
                raise ValueError
        except (ValueError, TypeError, yaml.YAMLError):
            return block, False, "excluded-models 必须是字符串列表"
        has = rule in items
        if enabled == (not has):
            return block, False, ""          # 已经是目标状态
        if enabled:
            items = [x for x in items if x != rule]
        else:
            items = items + [rule]
        nb = list(block)
        comments = []
        for ln in block[i:end]:
            if not ln.strip():
                comments.append(ln)
                continue
            _, comment = _split_comment(ln.rstrip("\r\n"))
            if comment:
                comments.append(field_indent + comment.lstrip() + "\n")
        if enabled and not items:
            # 清空就**删掉整行**，而不是留一个 `excluded-models: []`。
            # 理由是往返可逆：原条目多数根本没有这一行，停用时我们插进去，
            # 启用时要能回到逐字节相同的原文 —— 留个空列表会让
            # 「停用→启用」产生一处永久 diff，写回时凭空多出噪声。
            # 语义上 `[]` 与「没有这一行」对 CPA 完全等价
            # （`NormalizeExcludedModels` 对空值与缺失同样处理）。
            nb[i:end] = comments
            return nb, True, ""
        body = ", ".join(json.dumps(x, ensure_ascii=False) for x in items)
        nl = "\n" if block[i].endswith("\n") else ""
        nb[i:end] = [f'{m.group(1)}excluded-models: [{body}]{nl}'] + comments
        return nb, True, ""

    if enabled:
        return block, False, ""              # 没有 excluded-models = 启用中
    nb = list(block)
    nb.insert(1, f'{field_indent}excluded-models: ["{rule}"]\n')
    return nb, True, ""


def apply_bulk(raw: str, ops: list[dict], *, stamp: str = "") -> tuple[str, list[str], list[str]]:
    """对整份 config.yaml 文本施加一批操作。

    ops 每项：
        {"section": "codex-api-key", "index": 3, "action": "disable"}
        {"section": "codex-api-key", "index": 3, "action": "enable"}
        {"section": "codex-api-key", "index": 3,
         "action": "priority", "value": 349}

        {"section": "codex-api-key", "index": 3, "action": "delete",
         "expect": "https://api.example.com/v1"}

    Returns:
        (新文本, 每条操作的结果说明, 未能执行的原因清单)

    删除的两道额外保护（它是这里唯一不可逆的操作）
    --------------------------------------------
      1. **内容指纹校验**：`expect` 必须与该条目块里的 `base-url` 实际值
         逐字相符，不符就拒绝并报出来。下标是位置，位置会因为别人并发改动
         而指向另一个条目 —— 删错一条上游是静默的数据丢失，光靠下标不够。
      2. **降序执行**：删除会让后面所有条目的行号前移。下面按下标降序处理，
         所以每次删除只影响**已经处理过**的那些，不会让未处理的下标失效。
         同理，`enable/disable` 的插入行也只影响已处理过的部分。

    调用方（server）还有第三道：`confirm=true` + 基线比对。
    """
    lines = raw.splitlines(keepends=True)
    # Validate and deduplicate against the ORIGINAL index space, before edits.
    unique = {}
    actions = {}
    for op in ops:
        if (not isinstance(op, dict) or type(op.get("index")) is not int
                or not isinstance(op.get("section"), str)
                or not isinstance(op.get("action"), str)):
            return raw, [], ["每条操作必须是对象，index 必须是整数"]
        identity = (op.get("section"), op["index"])
        action = op["action"]
        signature = (*identity, action)
        prior = actions.setdefault(identity, set())
        if (signature in unique and unique[signature] != op
                or action == "delete" and prior - {"delete"}
                or action != "delete" and "delete" in prior
                or action == "enable" and "disable" in prior
                or action == "disable" and "enable" in prior):
            return raw, [], ["同一条目存在冲突操作，请拆分并重新预览"]
        unique[signature] = op
        prior.add(action)
    ops = list(unique.values())
    # 停用写法跟着 CPAMP 源码走，不写死（用户 2026-09-11 的要求）
    rule, disabled_field, sem_src = disable_semantics()
    notes: list[str] = []
    problems: list[str] = []

    # 按段分组，段内**下标降序**处理 —— 插入行会让后面的行号偏移，
    # 从后往前改就不用重算。同一条目被多条操作命中时按给定顺序叠加。
    by_section: dict[str, list[dict]] = {}
    for op in ops:
        sec = str(op.get("section") or "")
        if sec not in SECTIONS:
            problems.append(f"未知段 {sec!r}")
            continue
        by_section.setdefault(sec, []).append(op)

    for section, sec_ops in by_section.items():
        blocks = _entry_blocks(lines, section)
        if not blocks:
            problems.append(f"段 {section} 里找不到任何条目")
            continue
        span = _section_span(lines, section)
        _dash, field = _detect_indent(lines, span[0], span[1])
        # 下标降序，避免行号偏移
        last_idx = None
        for op in sorted(sec_ops, key=lambda o: -int(o.get("index", -1))):
            idx = int(op.get("index", -1))
            if idx == last_idx:
                # Independent field edits can change this block's length.
                # Deletes cannot share an index because preflight rejects them.
                blocks = _entry_blocks(lines, section)
            last_idx = idx
            if not (0 <= idx < len(blocks)):
                problems.append(f"段 {section} 下标 {idx} 越界"
                                f"（共 {len(blocks)} 条）")
                continue
            lo, hi = blocks[idx]
            block = lines[lo:hi]
            action = str(op.get("action") or "")
            if action in ("enable", "disable"):
                nb, changed, why = _set_enabled(
                    block, section, action == "enable", field,
                    rule=rule, disabled_field=disabled_field)
                if why:
                    problems.append(f"段 {section} 第 {idx} 条：{why}")
                    continue
                if changed:
                    lines[lo:hi] = nb
                    notes.append(f"{section}[{idx}] → "
                                 f"{'启用' if action == 'enable' else '停用'}")
            elif action == "priority":
                try:
                    val = int(op.get("value"))
                except (TypeError, ValueError):
                    problems.append(f"段 {section} 第 {idx} 条："
                                    f"priority 值不是整数")
                    continue
                note = f"{stamp} 批量改档" if stamp else "批量改档"
                nb, changed, why = _set_priority(block, section, val, field)
                if why:
                    problems.append(f"段 {section} 第 {idx} 条：{why}")
                    continue
                if changed:
                    nb.insert(1, f"{field}# {note}\n")
                    lines[lo:hi] = nb
                    notes.append(f"{section}[{idx}] → priority {val}")
            elif action == "delete":
                expect = str(op.get("expect") or "").strip()
                if not expect:
                    problems.append(f"段 {section} 第 {idx} 条：删除必须带 "
                                    f"expect（该条目的 base-url），"
                                    f"防止下标错位删错条目")
                    continue
                got = ""
                for line in block:
                    m = re.match(r"^\s*-?\s*base-url\s*:\s*(.*)$",
                                 line.rstrip("\n"))
                    if m:
                        got = m.group(1).split("#")[0].strip().strip("\"'")
                        break
                if got != expect:
                    problems.append(
                        f"段 {section} 第 {idx} 条：内容指纹不符，拒绝删除。"
                        f"期望 base-url={expect!r}，实际={got!r} —— "
                        f"config.yaml 可能已被改动，请重新读取")
                    continue
                del lines[lo:hi]
                notes.append(f"{section}[{idx}] → 删除（{got}）")
            else:
                problems.append(f"未知操作 {action!r}")

    out = "".join(lines)
    ok, msg = validate(out)
    if not ok:
        raise BulkError(f"批量改动后 YAML 校验失败，已放弃：{msg}")

    # 段被删空的守卫（2026-09-11）
    # -------------------------
    # 删光一个段的所有条目后，那个键会变成 `gemini-api-key:` 后面什么都没有，
    # YAML 解析出来是 **null** 而不是空列表。CPA 侧多处是
    # `for _, e := range cfg.GeminiKey` 这种写法，null 与空列表在 Go 里都是
    # 零值切片、不会崩；但配置文件里留一个 null 段会让下一次
    # `extract_existing_entries` / `build_band` 拿到 None 而不是 []，
    # 本项目自己的路径反而更脆。
    #
    # 判据用**解析后的值**而不是「删了几条」—— 原本就是空段的情况不该报。
    try:
        import yaml as _yaml
        before = _yaml.safe_load(raw) or {}
        after = _yaml.safe_load(out) or {}
        for sec in SECTIONS:
            if (isinstance(before.get(sec), list) and before.get(sec)
                    and after.get(sec) in (None, [])):
                problems.append(
                    f"段 {sec} 的条目已被全部删除 —— 该段会变成空值。"
                    f"若确实要清空，建议保留至少一条或手工删掉整个段键")
    except Exception:
        pass                      # 校验已过，这里只是补充提示，失败不影响主流程

    return out, notes, problems


def unify_priority_ops(cfg: dict, section: str, host: str,
                       value: int | None = None) -> list[dict]:
    """把某个 (段, 网址) 组内所有条目的 priority 统一成同一个值。

    这是用户那条硬要求的直接实现：**同一网址的上游，Key 不同、优先级也必须
    相同**。不给 value 时取组内**最高**的那个档 —— 往高对齐而不是往低，
    因为低档那几条本来就是被高档遮住、实质不参与轮询的冷备；往低对齐会
    把整组一起降级。

    实测证据（2026-09-10 对账两份生产配置）：本项目**注入前** 40 组 0 组分裂，
    **注入后** 3 组分裂 —— 说明分裂是注入过程写进去的，不是历史遗留。
    """
    hits = []
    for sec in SECTIONS:
        for idx, e in enumerate(cfg.get(sec) or []):
            if not isinstance(e, dict):
                continue
            if host_of(str(e.get("base-url") or "")) != host:
                continue
            pri = e.get("priority", 0)
            values = [pri]
            if sec == "openai-compatibility":
                values += [key.get("priority", pri)
                           for key in e.get("api-key-entries") or []
                           if isinstance(key, dict)]
            if not all(type(p) is int for p in values):
                raise BulkError("同站 priority 必须是整数")
            hits.append((sec, idx, values))
    if not hits:
        return []
    target = value if value is not None else max(p for _s, _i, values in hits for p in values)
    return [{"section": sec, "index": i, "action": "priority",
             "value": target} for sec, i, values in hits if any(p != target for p in values)]


# ── 站间档位不得相同：批量设档后的冲突消解 ──────────────────────────
#
# 用户第 3⑶ / 第 7 条原话：「同一个类型不同域名的优先级一定要不同，哪怕算出来
# 相同，也要适当做点微调给出点偏差」。
#
# 为什么必须在这里做，而不是让它撞上
# ----------------------------------
# 「批量设为 350」是**跨组**动作，一次能命中几十个组。若它们全落 350，
# CPA 的 selector 会把这一层当**同一个桶**按 weight 轮询 —— 站与站的先后
# 次序被推平，而 priority 的**唯一**作用就是区分先后（selector.go:527-553
# 只取最高那一桶）。
#
# `priority_collisions`（plan.py:3589）会**报告**这种撞值，但它只在
# `build_plan` 那条路径上被调用 —— 批量管理这条路直接改既有条目，
# 不经过 build_plan，所以那边报了也没人看。实测：批量设档从不触发它。
#
# 消解方式（不是阻断）
# -------------------
# 按 `(-目标值, host)` 排序：字典序最小的一组如愿拿到原值（0 次让位），
# 其余各组各自往下挪 `step` 的整数倍，直到落在一个没被占用的格子上。
#
# 为什么定序键里有 host，而不是「谁先被勾选」：选中集是个 Set，遍历顺序
# 不稳定 —— 同样的输入两次预览必须给出同样的结果，否则 diff 无法复核。
#
# 为什么让位是「各挪到第一个空格」而不是「依次减 1、2、3」：后者只在下界
# 之上且步长为 1 时才碰巧等价。`step=5` 时三组 100 想给的是 100/95/90，
# 「依次减」会给出 100/99/98 —— 步长这个口子就白留了。
#
# 为什么不往高加：往高加会把整批一起推上去，可能盖过本来更高的在用站
# （那正是 `assign_priorities` 的注释里记着的「抢顶层」事故）。往低走只会
# 进入本来就被遮住的区间，影响面小一档。
#
# `step` 给调用方留出「微调幅度」的口子（默认 1，即紧凑连号）。

def resolve_priority_collisions(
    wanted: dict[str, int],
    *,
    step: int = 1,
    taken: set[int] | None = None,
    floor: int = 1,
) -> tuple[dict[str, int], list[str]]:
    """把「多个组想拿同一个 priority」消解成互不相同的值。

    Args:
        wanted: {host: 目标 priority}。host 是站的身份（同站多 Key 同一个）。
        step:   相邻两组之间的差值。1 = 紧凑连号；5 = 留出插队余地。
        taken:  本段**已存在**的档位集合 —— 消解出来的值不许撞上它们，
                否则等于与那个在用站同层轮询。
        floor:  下界。CPA 不校验 priority 下界，但 0 与负数语义未定义
                （plan.py:1361 记着这条），所以不许降到 0 以下。

    Returns:
        (host -> 最终 priority, 说明清单)。被挪动过的组各有一条说明，
        说明里带原来想拿的值与最终值 —— 前端直接显示，不用自己拼。
    """
    if step < 1:
        raise BulkError("微调步长必须 >= 1")
    if floor < 1:
        raise BulkError("档位下界必须 >= 1")

    out: dict[str, int] = {}
    notes: list[str] = []
    used: set[int] = set(taken or ())

    # 挪位的次数上限。**必须有**：`while value in used or value < floor` 在
    # 「目标值贴近下界且下方全被占」时会一直往下走 —— 而 `used` 每轮只增不减，
    # 条件永远为真，于是死循环。2026-09-13 实测：`{x:1, y:1}` 这一步直接挂住
    # 测试进程（不是慢，是永不返回）。
    #
    # 上界取 `(组数 + 既有档位数) * step` —— 合法情形下每个组最多需要绕开
    # 「已占的格子」，而格子总数就是这两个数之和；再乘 step 是因为每挪一格
    # 值减 step。越界即说明档位谱已经密到放不下这么多组。
    budget = (len(wanted) + len(used)) * max(1, step)

    # 按目标值降序（高的先占位），同目标值内按 host 字典序 —— 全程确定性。
    for host in sorted(wanted, key=lambda h: (-wanted[h], h)):
        want = wanted[host]
        if type(want) is not int:
            raise BulkError(f"{host} 的 priority 必须是整数")
        value = want
        # 撞上「同批里已分配的值」或「本段既有档位」就往下挪
        moved = 0
        while value in used or value < floor:
            value -= step
            moved += 1
            if moved > budget:
                raise BulkError(
                    f"{host} 想拿 {want}，但下方 {budget} 个档位都被占用 —— "
                    f"该段档位太密（下界 {floor}），请改一个更高或更稀疏的目标值，"
                    f"或先批量删除多余条目")
        used.add(value)
        out[host] = value
        if value != want:
            notes.append(f"{host}：{want} → {value}（与其它站撞档，下移微调）")
    return out, notes
