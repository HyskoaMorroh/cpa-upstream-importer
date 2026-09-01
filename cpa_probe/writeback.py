"""写回 config.yaml。行级编辑保注释，写前必备份。

为什么行级编辑而不是 yaml.dump
------------------------------
config.yaml 里 198 条注释是两夜排障的全部记忆（「relay-f 403 User has
been banned，从 900 降权待解封」这类）。yaml.safe_load + dump 会把它们
全部丢掉。所以：读原文 → 按行插入 → 写原文。

为什么必须自己先备份
--------------------
`PUT /v0/management/config.yaml` 的落盘不是原子的：
  config_basic.go:101-116  WriteConfig 用 O_TRUNC 打开后直写
  校验链本身是稳的（temp 文件 + LoadConfigOptional 全量校验才放行），
  但通过校验后落盘中途崩溃会留下**被截断的 872KB 文件**，
  且 config_basic.go:163-167 在 LoadConfig 失败时只返回 500，**不回滚**。
write_local 自己那条路也一样：它必须就地 O_TRUNC 覆写（不能 tmp+rename，
见 write_local 的说明），同样不原子。所以备份是硬前置。

注释保留已验证
--------------
  GET  /v0/management/config.yaml  os.ReadFile 原样返回（config_basic.go:174-189）
  PUT  /v0/management/config.yaml  写请求体原文，只做 NormalizeCommentIndentation
所以「读原文 → 行级改 → PUT」这条链不会丢注释。

为什么不用 PATCH
----------------
PATCH /{section} 按 index/match 定位**已存在**条目，找不到返回 404
（config_lists.go:191-216）。它不能新增。四段只能整段 PUT 或走
PUT /config.yaml。整段 PUT 要重新序列化，会丢注释 —— 所以走 config.yaml。

写回后如何让 CPA 真正生效
------------------------
两条路刷的东西不同，缺一不可 —— 详见 reload_cpa 的说明：
  · fsnotify 那一路会 reloadClients()，真正重建凭据池（新上游能被选中）
  · PUT /config.yaml 只更新管理 handler 的 h.cfg，但它就地 O_TRUNC 落盘，
    **必然在容器内产生一次 Write 事件**，从而确定触发上面那一路
前提是 config.yaml 的 inode 从头到尾不变 —— 单文件 bind mount 在容器
启动时把 inode 定死了，换 inode 等于让容器永远读旧文件。
"""

from __future__ import annotations

import datetime
import io
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass

from .plan import ImportPlan, SectionPlan


@dataclass
class Diff:
    """一次写回的预览。用户确认前必须看到这个。"""

    section: str
    insert_at: int          # 1-based 行号，在此行**之后**插入
    lines: list[str]
    host: str
    # 非空表示这不是新建条目，而是把 Key 追加进已存在的 compat provider。
    # UI 与 CLI 要显式区分这两种 —— 「新增一个站」和「给已有站加 Key」
    # 对轮询池的影响完全不同。
    merged_into: str = ""

    def render(self) -> str:
        what = (f"追加进已有 provider {self.merged_into}"
                if self.merged_into else "新增条目")
        head = (f"# {self.section} ← {self.host}（第 {self.insert_at} 行后"
                f"{what}，{len(self.lines)} 行）")
        return head + "\n" + "\n".join(self.lines)


def backup(path: str, *, backup_dir: str | None = None) -> str:
    """写前备份。返回备份路径。

    这一步不能省 —— PUT 的落盘不原子且失败不回滚。

    backup_dir 用于容器场景：config.yaml 以单文件挂载时同目录不可写，
    备份要落到另一个卷。

    为什么时间戳之外还要防撞（2026-08-31 自查发现）
    ----------------------------------------------
    原来的名字只到**秒**，而 shutil.copy2 撞名直接静默覆盖。同一秒内写两次
    （并发 apply、或脚本连续跑）时，第二次的备份会把第一次的覆盖掉 ——
    留下来的是「第一次写完之后」的状态，真正的原始文件没了。

    而 write_local 是就地 O_TRUNC 覆写（见那边的说明，inode 必须不变），
    本身不原子、失败不回滚，**备份是唯一的回滚手段**。所以撞名不是"多留
    一份少留一份"的问题，是把唯一的退路弄丢。

    改法：撞上就往后加 -2、-3……直到找到没被占用的名字。用 O_CREAT|O_EXCL
    原子占位，避免两个进程同时算出同一个空位。
    """
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = (os.path.join(backup_dir, os.path.basename(path))
            if backup_dir else path)
    if backup_dir:
        os.makedirs(backup_dir, exist_ok=True)

    dst = f"{base}.bak-{stamp}"
    n = 1
    while True:
        try:
            # O_EXCL：文件已存在就抛 FileExistsError，由内核保证互斥。
            fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            n += 1
            dst = f"{base}.bak-{stamp}-{n}"
            if n > 999:             # 同一秒 999 次写回，说明有别的问题
                raise
            continue
        os.close(fd)
        break
    # copy2 会覆盖我们刚占的空文件，同时保留原文件的 mtime/权限
    shutil.copy2(path, dst)
    return dst


def _section_span(lines: list[str], section: str) -> tuple[int, int] | None:
    """找出某段在文件里的行范围（0-based, [start, end)）。

    段头是顶层键（零缩进）。段尾是下一个顶层键之前的最后一个**实质**行 ——
    注释与空行都不算实质。

    为什么注释也要剥掉（2026-08-31 自查发现）
    ---------------------------------------
    原来只回退空行，注释留在 span 里。而这份 config.yaml 的写法是
    「下一段的说明注释写在下一段段头之前」，于是：

        gemini 段末尾挂着 32 行属于 codex 的说明
        codex  段末尾挂着 89 行属于 claude 的说明

    span 的 end 落在那些注释**之后**，新条目就插到了它们后面 ——
    结果 codex 的整段文档注释跑到 gemini 的新条目上面去了。YAML 仍然合法
    （段头还没出现，条目确实还在上一段里），所以不报错、不失败，
    只是把「198 条注释是两夜排障的全部记忆」这件事悄悄搞乱。

    剥掉注释后 end 落在最后一个真实条目行之后，新条目紧跟着它 ——
    与手写的样子一致，也不动任何既有注释的归属。
    """
    start = None
    for i, l in enumerate(lines):
        if re.match(rf"^{re.escape(section)}\s*:", l):
            start = i
            break
    if start is None:
        return None

    end = len(lines)
    for i in range(start + 1, len(lines)):
        l = lines[i]
        if not l.strip():
            continue
        # 顶层键：零缩进且不是列表项、不是注释
        if re.match(r"^[a-zA-Z_][a-zA-Z0-9_-]*\s*:", l):
            end = i
            break

    # 回退到最后一个实质行之后（空行与注释都不是实质行）
    while end > start + 1 and (not lines[end - 1].strip()
                               or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    return start, end


def _detect_indent(lines: list[str], start: int, end: int) -> tuple[str, str]:
    """探出该段列表项的缩进风格。返回 (dash_indent, field_indent)。

    不猜 —— 从现有条目读。config.yaml 里四段的缩进未必一致。
    """
    for i in range(start + 1, end):
        m = re.match(r"^(\s*)-\s+(\S)", lines[i])
        if m:
            dash = m.group(1)
            # 字段缩进 = dash 缩进 + "  "（"- " 的宽度）
            return dash, dash + "  "
    return "  ", "    "


# 双引号风格里必须转义的字符。YAML 的双引号标量支持 C 风格转义，
# 所以真换行能写成两字符的 \n，值不变形、又不占第二个物理行。
_YAML_ESCAPES = {
    "\\": "\\\\",       # 必须排第一：先替反斜杠，否则会把后面加的反斜杠再替一遍
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\x00": "\\0",
}

# YAML 不允许在标量里裸出现的字符：C0 控制字符、DEL、C1 控制区。
# DEL(0x7f) 容易漏 —— 它不小于空格，按 `c < " "` 判会放过去，
# 而 PyYAML 读回时抛 ReaderError（special characters are not allowed）。
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _yaml_str(v: str) -> str:
    """标量转 YAML 字面量。key 与 url 一律加引号，避免特殊字符踩坑。

    为什么必须转义反斜杠与控制字符（2026-08-31 自查发现的两个缺陷）
    ------------------------------------------------------------
    原实现只处理 `"`，于是：

      · 值以单个反斜杠结尾（`abc\\`）→ 写出 `"abc\\"`，闭合引号被转义掉，
        整个文件变成非法 YAML，validate() 报 ScannerError。写盘前能挡住，
        但那次写回直接失败。
      · 值含反斜杠 + n（`sk-abc\\ndef`）→ 写出 `"sk-abc\\ndef"`，YAML 把它
        读成真换行，Key 静默变质（回读为 `sk-abc def`，长度都变了）。
      · 值含**真换行**（overrides 传进来的 headers 可以带）→ 写出的行断成两截，
        YAML 仍解析成功（流式标量折行），但 `_section_span` 是逐行正则扫描，
        会把折出来的那半行当成真段头。实测能凭空多出一个
        `openai-compatibility:` 段，下一次写入插进错误位置。

    所以控制字符不能靠「上游别传」来保证 —— 在这里一次性转义掉，
    值完整保留，且保证一个标量只占一个物理行。

    单引号风格只在「有 `"` 且不含反斜杠与控制字符」时用：单引号标量里
    反斜杠是字面量、换行无法转义，用它兜不住上面那几种输入。
    """
    s = str(v)
    risky = any(c in s for c in _YAML_ESCAPES if c != '"')
    if '"' in s and not risky and not _CTRL_RE.search(s):
        return "'" + s.replace("'", "''") + "'"
    for raw, rep in _YAML_ESCAPES.items():
        s = s.replace(raw, rep)
    # 其余 YAML 不允许裸出现的字符统一按 \xNN 转义：C0（除已处理的）、
    # DEL(0x7f) 与 C1(0x80-0x9f)。漏掉 DEL 会让 yaml 读回时抛 ReaderError。
    s = _CTRL_RE.sub(lambda m: f"\\x{ord(m.group()):02x}", s)
    return '"' + s + '"'


def find_compat_provider(lines: list[str], base_url: str) -> dict | None:
    """在 compat 段里按 base-url 找已存在的 provider，返回它的行位置与 name。

    为什么必须先找：`name` 就是 CPA 的 provider 身份 ——
    `util.OpenAICompatibleProviderKey(name)` 的结果被写进 Auth 的
    `provider_key`，而冷却（conductor_cooldown.go:73）、模型能力
    （api_key_model_capabilities.go:186）、执行路由（conductor_execution.go:1473）
    三处都按它索引。两个同名 provider 会让这三处对同一个 key 命中两套配置。

    所以同一个站再来新 Key 时，正确做法是**把 Key 追加进现有 provider 的
    api-key-entries**，而不是新建一条同名（或同 base-url）的 provider。

    返回 {"name", "start", "end", "keys_line", "keys_indent", "existing_keys"}；
    找不到返回 None。start/end 是该 provider 条目的行范围（0-based, [start,end)）。
    """
    span = _section_span(lines, "openai-compatibility")
    if span is None:
        return None
    sec_start, sec_end = span
    dash, _field = _detect_indent(lines, sec_start, sec_end)
    want = base_url.strip().rstrip("/")

    # 逐个 provider 条目扫（以 dash 缩进的 "- " 开头）
    starts = [i for i in range(sec_start + 1, sec_end)
              if lines[i].startswith(dash + "- ")]
    for idx, st in enumerate(starts):
        en = starts[idx + 1] if idx + 1 < len(starts) else sec_end
        block = lines[st:en]
        base_here = ""
        name_here = ""
        keys_line = -1
        keys_indent = ""
        existing: list[str] = []
        in_keys = False
        for j, l in enumerate(block):
            m = re.match(r"^\s*-?\s*base-url:\s*(.+?)\s*$", l)
            if m and not base_here:
                base_here = m.group(1).strip().strip('"').strip("'").rstrip("/")
            m = re.match(r"^\s*-?\s*name:\s*(.+?)\s*$", l)
            if m and not name_here and not in_keys:
                name_here = m.group(1).strip().strip('"').strip("'")
            m = re.match(r"^(\s*)api-key-entries:\s*$", l)
            if m:
                keys_line = st + j
                keys_indent = m.group(1)
                in_keys = True
                continue
            if in_keys:
                mk = re.match(r"^\s*-\s*api-key:\s*(.+?)\s*$", l)
                if mk:
                    existing.append(mk.group(1).strip().strip('"').strip("'"))
                elif re.match(r"^\s*[a-zA-Z_-]+:\s*", l) and not l.strip().startswith("-"):
                    # 遇到同级的下一个字段（models: / headers: …），entries 段结束
                    cur_indent = len(l) - len(l.lstrip())
                    if cur_indent <= len(keys_indent):
                        in_keys = False
        if base_here == want:
            # entries 的最后一行：keys_line 之后、属于该列表的最后一行
            last = keys_line
            if keys_line >= 0:
                for j in range(keys_line + 1, en):
                    l = lines[j]
                    if not l.strip():
                        continue
                    cur = len(l) - len(l.lstrip())
                    if cur > len(keys_indent):
                        last = j
                    else:
                        break
            return {"name": name_here, "start": st, "end": en,
                    "keys_line": keys_line, "keys_indent": keys_indent,
                    "existing_keys": existing, "last_key_line": last}
    return None


def render_entry(sp: SectionPlan, dash: str, field: str, stamp: str,
                 extra_keys: list[str] | None = None) -> list[str]:
    """生成一个条目的 YAML 行。

    字段顺序与现有文件一致（api-key, base-url, prefix, priority, models…），
    这样 diff 看起来跟手写的一样，便于人工核对。
    compat 段结构不同 —— provider 级 + api-key-entries。

    max-context-length 只写在**实测过的那个模型**上（sp.context_model）。
    同站不同模型窗口能差一个数量级；把 opus 的实测值抄给 haiku，客户端
    会按错的窗口定压缩点 —— 那正是第 08 章那条 400 的成因。未实测的模型
    留空，CPA 回落内置目录值（service_models.go 各段的 fallback）。
    """
    out: list[str] = []
    note = f"# {stamp} 批量导入 · 得分 {sp.score} · {sp.priority_reason}"

    def model_lines(indent: str) -> list[str]:
        rows: list[str] = []
        for m in sp.models:
            rows.append(f"{indent}- name: {_yaml_str(m)}")
            rows.append(f"{indent}  alias: {_yaml_str(m)}")
            if sp.max_context_length and m == sp.context_model:
                rows.append(f"{indent}  max-context-length: {sp.max_context_length}"
                            f"   # 实测值")
        return rows

    if sp.section == "openai-compatibility":
        # compat 段的结构与其余三段**不同**：一个 provider 条目 = 一个上游站，
        # 该站的多个 Key 全部挂在它的 api-key-entries 下。
        #
        # 实测现有 12 个 provider 全部遵循这个约定，且每站名字唯一
        # （relay-f 15 个 Key、relay-l 15 个，都在同一个 provider 里）。
        # 每个 Key 生成一个同名 provider 会造成重名条目 —— CPA 的 compat 段
        # 不去重（SanitizeOpenAICompatibility 只丢缺 base-url 的），
        # 于是同一个站被注册成 N 个独立 provider，模型清单重复 N 遍。
        #
        # 所以这里接收的是**同主机同段的一组 key**（extra_keys），
        # 由 build_diffs 先按 (host, section) 归并后传入。
        out.append(f"{dash}- name: {_yaml_str(sp.base_url.split('//')[-1].split('/')[0])}")
        out.append(f"{field}base-url: {_yaml_str(sp.base_url)}")
        if sp.prefix:
            out.append(f"{field}prefix: {_yaml_str(sp.prefix)}")
        out.append(f"{field}priority: {sp.priority}        {note}")
        out.append(f"{field}api-key-entries:")
        for key in [sp.api_key] + list(extra_keys or []):
            out.append(f"{field}  - api-key: {_yaml_str(key)}")
            if sp.proxy_url:
                out.append(f"{field}    proxy-url: {_yaml_str(sp.proxy_url)}")
        if sp.headers:
            out.append(f"{field}headers:")
            for k, v in sp.headers.items():
                out.append(f"{field}  {k}: {_yaml_str(v)}")
        out.append(f"{field}models:")
        out.extend(model_lines(f"{field}  "))
        return out

    # 字段顺序与现有条目一致：api-key → base-url → prefix → priority → …
    # （实测 config.yaml 里 claude 段就是这个顺序，diff 看起来才像手写的）
    out.append(f"{dash}- api-key: {_yaml_str(sp.api_key)}")
    out.append(f"{field}base-url: {_yaml_str(sp.base_url)}")
    if sp.prefix:
        out.append(f"{field}prefix: {_yaml_str(sp.prefix)}")
    out.append(f"{field}priority: {sp.priority}        {note}")
    if sp.proxy_url:
        out.append(f"{field}proxy-url: {_yaml_str(sp.proxy_url)}")
    if sp.headers:
        out.append(f"{field}headers:")
        for k, v in sp.headers.items():
            out.append(f"{field}  {k}: {_yaml_str(v)}")
    out.append(f"{field}models:")
    out.extend(model_lines(f"{field}  "))
    return out


def build_diffs(raw: str, plans: list[ImportPlan]) -> list[Diff]:
    """算出每个段要插入什么。**不修改任何现有行** —— 只追加。

    compat 段按主机归并：该段一个条目 = 一个上游站，多个 Key 挂在它的
    api-key-entries 下。其余三段每个 Key 各占一条（现有 config.yaml
    两种约定都实测确认过：relay-f 在 claude 段 15 条、在 compat 段 1 条）。

    不归并会生成 N 个重名 provider。CPA 的 compat 段不去重
    （SanitizeOpenAICompatibility 只丢缺 base-url 的），于是同一个站被注册
    成 N 个独立 provider，模型清单重复 N 遍 —— 5 个 Key 的站就是 5 份。
    """
    lines = raw.split("\n")
    diffs: list[Diff] = []
    stamp = datetime.datetime.now().strftime("%Y-%m-%d")

    # compat 段先按 (host, base_url) 归并同站的多个 Key
    compat_groups: dict[tuple[str, str], list] = {}
    for plan in plans:
        sp = plan.sections.get("openai-compatibility")
        if sp is not None and sp.writable:
            compat_groups.setdefault((plan.host, sp.base_url), []).append(sp)

    for plan in plans:
        for section, sp in plan.sections.items():
            if not sp.writable:
                continue
            if section == "openai-compatibility":
                continue        # 下面统一处理
            span = _section_span(lines, section)
            if span is None:
                continue
            start, end = span
            dash, field = _detect_indent(lines, start, end)
            diffs.append(
                Diff(
                    section=section,
                    insert_at=end,
                    lines=render_entry(sp, dash, field, stamp),
                    host=plan.host,
                )
            )

    span = _section_span(lines, "openai-compatibility")
    if span is not None and compat_groups:
        start, end = span
        dash, field = _detect_indent(lines, start, end)
        for (host, base), group in compat_groups.items():
            head = group[0]
            keys = [g.api_key for g in group]

            # 该 base-url 已有 provider？追加 Key 进它的 api-key-entries，
            # 不新建条目 —— name 就是 CPA 的 provider 身份，重名会让冷却、
            # 模型能力、执行路由三处对同一个 Key 命中两套配置。
            found = find_compat_provider(lines, base)
            if found and found["keys_line"] >= 0:
                fresh = [k for k in keys if k not in set(found["existing_keys"])]
                if not fresh:
                    continue        # 全都已存在，无事可做
                ki = found["keys_indent"]
                add_lines = []
                for k in fresh:
                    add_lines.append(f"{ki}  - api-key: {_yaml_str(k)}")
                    if head.proxy_url:
                        add_lines.append(f"{ki}    proxy-url: {_yaml_str(head.proxy_url)}")
                add_lines.append(
                    f"{ki}  # {stamp} 批量导入追加 {len(fresh)} 个 Key"
                    f"（provider {found['name']} 已存在，"
                    f"原有 {len(found['existing_keys'])} 个）"
                )
                diffs.append(
                    Diff(
                        section="openai-compatibility",
                        insert_at=found["last_key_line"] + 1,
                        lines=add_lines,
                        host=host,
                        merged_into=found["name"],
                    )
                )
                continue

            diffs.append(
                Diff(
                    section="openai-compatibility",
                    insert_at=end,
                    lines=render_entry(head, dash, field, stamp,
                                       extra_keys=keys[1:]),
                    host=host,
                )
            )
    return diffs


def apply_diffs(raw: str, diffs: list[Diff]) -> str:
    """把 diff 应用到原文。从后往前插，避免行号偏移。"""
    lines = raw.split("\n")
    for d in sorted(diffs, key=lambda x: -x.insert_at):
        lines[d.insert_at:d.insert_at] = d.lines
    return "\n".join(lines)


def validate(text: str) -> tuple[bool, str]:
    """本地先校验，别让 CPA 的非原子写去踩坑。

    yaml.safe_load 只查语法。CPA 侧还会跑 LoadConfigOptional 做 schema
    校验，那一层过不了它会返回 400 且**不落盘**，是安全的。危险的是
    过了校验之后的落盘阶段 —— 所以本地先挡掉语法错。
    """
    try:
        import yaml
    except ImportError:
        return True, "未安装 PyYAML，跳过本地校验（CPA 侧仍会校验）"
    try:
        cfg = yaml.safe_load(text)
    except Exception as e:
        return False, f"YAML 语法错误：{e}"
    if not isinstance(cfg, dict):
        return False, "顶层不是映射，config.yaml 结构异常"
    n = sum(len(cfg.get(s) or []) for s in
            ("gemini-api-key", "codex-api-key", "claude-api-key", "openai-compatibility"))
    return True, f"YAML OK · {len(cfg)} 个顶层键 · 四段共 {n} 条目"


def write_local(path: str, text: str, *, backup_dir: str | None = None) -> str:
    """本地落盘（先备份）。返回备份路径。

    **一律就地覆写，绝不 tmp + os.replace。**

    为什么不用原子替换 —— 这是踩过的坑，别"优化"回去
    -----------------------------------------------
    config.yaml 被 bind mount 进多个容器：
        docker-compose.yml:364  ./config.yaml:/CLIProxyAPI/config.yaml:Z
        docker-compose.yml:547  ./config.yaml:/data/config.yaml:Z

    **单文件 bind mount 在容器启动时就把宿主 inode 解析定死了。**
    `os.replace` 换的是目录项指向的 inode —— 宿主机看到新内容，而
    cli-proxy-api 容器里的挂载点仍然指着**旧 inode**，那个文件还在
    （被挂载引用着，没被回收），内容永远是旧的。

    实测症状（2026-08-30）：宿主机 wc -l 14851 行、四段 212 条目，
    而 CPA 与 CPAMP 面板都停在 206，重启容器才对上 —— 因为重启才重新
    解析挂载。这不是"通知没送到"，是**CPA 读的根本是另一个文件**。

    就地覆写（O_TRUNC + write）保持 inode 不变，所有挂了这个文件的容器
    立刻看到新字节，CPA 的 fsnotify 也能收到 Write 事件
    （internal/watcher/events.go:69 的 configOps 含 fsnotify.Write）。

    代价：就地覆写不原子，写一半崩溃会留下截断文件。所以 bak 先算出来 ——
    备份成功是执行写入的前置条件，最坏情况可回滚。
    """
    bak = backup(path, backup_dir=backup_dir)

    # 就地覆写。inode 不变是硬要求，不是优化偏好。
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    return bak


def reload_cpa(base: str, mgmt_password: str, text: str, *,
               timeout: int = 60) -> tuple[bool, str]:
    """让 CPA 立刻用上新的 config.yaml。返回 (成功, 说明)。

    为什么写完盘还要这一步（实测 + 读源码确认，2026-08-30）
    ----------------------------------------------------
    write_local 已经保证 inode 不变，容器里的挂载点能看到新字节，CPA 的
    fsnotify 也确实覆盖这种情形（events.go:69 的 configOps 含
    fsnotify.Write，Rename 也在内；config_reload.go:51 会做 SHA256 比对
    后重载）。所以理论上磁盘写完就该生效。

    但这条链有两个薄弱点，都会让「已经改了却没生效」静默发生：

      1. inotify 事件本身可能丢 —— 队列溢出、容器与宿主的挂载传播模式、
         以及 write_local 之前的历史版本用过 os.replace（换 inode，
         容器永远读旧文件）。CPA **没有轮询兜底**：internal/watcher/
         全目录只有 debounce 定时器（configReloadDebounce），没有 Ticker。
         事件一丢就永远不重载，不会自愈。
      2. 事件到了但重载失败（LoadConfig 报错）只写日志，调用方无从得知。

    主动推一次 `PUT /v0/management/config.yaml` 把这两点都消掉：CPA 自己
    校验（LoadConfigOptional 全量语义检查）、自己落盘、然后
    **在同一个请求里同步重载** `h.cfg`（config_basic.go:162-168），
    HTTP 状态码直接告诉我们成没成。这是唯一一条「不重启且有确定回执」的路。

    注意（读源码确认，很重要）：两条路刷的东西**不一样**，缺一不可。

      · fsnotify 那一路（config_reload.go:51-143）走完整流程：
        SHA256 比对 → LoadConfig → 更新 w.config → **reloadClients()**，
        即真正重建凭据池（新上游要能被选中，只有这一路能做到）。
      · PUT /config.yaml 那一路只更新管理 handler 的 h.cfg
        （config_basic.go:162-168），**不调** configReloadHook ——
        那个 hook 只挂在 persistLocked（handler.go:408-421）上，
        也就是 PUT /claude-api-key 这类分段端点走的路。

    所以 PUT 的作用不是"代替 fsnotify"，而是**保证 fsnotify 一定被触发**：
    WriteConfig 用 O_TRUNC 就地写（config_basic.go:101-116，inode 不变），
    在容器内部产生一次确定的 Write 事件，把"事件可能丢"换成"事件必然有"。
    真正让新上游可用的仍是随后那次 reloadClients。

    mgmt_password 必须是**原始密码**，不是 config.yaml 里那串 `$2a$` 哈希 ——
    校验用 bcrypt.CompareHashAndPassword（handler.go:387）。
    """
    if not mgmt_password:
        return False, "缺少管理密码，无法触发重载"
    if mgmt_password.startswith(("$2a$", "$2b$", "$2y$")):
        return False, ("收到的是 bcrypt 哈希而非原始密码 —— PUT 端点用 "
                       "bcrypt.CompareHashAndPassword 校验，哈希必然 401")
    return push_to_cpa(base, mgmt_password, text, timeout=timeout)


def push_to_cpa(base: str, mgmt_key: str, text: str, *, timeout: int = 120) -> tuple[bool, str]:
    """PUT /v0/management/config.yaml —— 写请求体原始字节，注释保留。

    CPA 侧行为（config_basic.go:118-170）：
      读 body → yaml.Unmarshal 语法检查 → 写同目录 temp 文件
      → LoadConfigOptional 全量语义校验 → 通过才 WriteConfig 落盘
      → 进程内重载 h.cfg；fsnotify 另外独立触发一次（150ms 去抖 + SHA256 比对）

    所以不需要 docker restart。但落盘用 O_TRUNC 且失败不回滚 ——
    调用方必须已经备份过。

    返回 (成功, 说明)。400 是安全失败（校验未过、文件未动）；
    其他失败必须立刻人工核对文件完整性。
    """
    if not mgmt_key:
        return False, "缺少 management key"

    url = base.rstrip("/") + "/v0/management/config.yaml"
    req = urllib.request.Request(
        url,
        data=text.encode("utf-8"),
        method="PUT",
        headers={
            "Authorization": f"Bearer {mgmt_key}",
            "Content-Type": "application/yaml",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        # 422 才是语义校验失败的真实状态码（config_basic.go:152 用的是
        # StatusUnprocessableEntity）；400 只用于 body 读不出来/YAML 语法错
        # （:121, :133）。两者都属"CPA 未落盘"的安全失败。
        if e.code in (400, 422):
            return False, (f"{e.code} 校验未通过，CPA 未落盘（这是安全失败）：{body}")
        if e.code == 401:
            return False, ("401 鉴权失败。PUT 端点用 bcrypt 比对**原始密码**"
                           "（handler.go:387），传 config.yaml 里那串 $2a$ 哈希"
                           "必然 401。连续 5 次失败会封该 IP 30 分钟")
        if e.code == 403:
            # 403 有两种来源，处置完全相反，必须分开说 ——
            # 混在一起会把人引向「核对文件完整性」，而文件根本没被碰过。
            low = body.lower()
            cf = ("error code: 1010" in low or "cloudflare" in low
                  or "cf-ray" in low or "attention required" in low)
            if cf:
                return False, (
                    f"403 被 Cloudflare 拦下（{body[:120]}）—— **请求根本没到 CPA**，"
                    "config.yaml 一个字节都没动。\n"
                    "原因：管理端点走了公网域名，而公网入口在 CF 后面。\n"
                    "修法：CPA 地址留空，走容器内服务名直连 "
                    "http://cli-proxy-api:8317 —— 既绕开 CF 也不出公网。")
            return False, (
                f"403 被拒（{body[:160]}）。请求可能没到 CPA（前置网关拦下），"
                "也可能是 CPA 的 remote-management 拒绝了这个来源 —— "
                "检查 config.yaml 的 remote-management.allow-remote，"
                "以及你打的地址是不是绕了公网网关")
        return False, (
            f"{e.code} 失败：{body}。"
            "注意：PUT 落盘用 O_TRUNC 且失败不回滚，请立刻核对 VPS 上 config.yaml 完整性"
        )
    except Exception as e:
        return False, (
            f"连接失败：{e}。若请求已发出，请核对 VPS 上 config.yaml 完整性"
        )

    # 读回校验。200 只说明 CPA 接受并落盘了，不说明它内存里那份是新的 ——
    # GET /config.yaml 走 os.ReadFile 直读磁盘（config_basic.go:174-189），
    # 所以这一步验证的是「CPA 容器看到的文件内容」==「我们要写的内容」。
    # 这恰好能抓住 inode 分叉：如果挂载点还指着旧 inode，读回的就是旧内容。
    ok_rb, msg_rb = _readback_check(base, mgmt_key, text, timeout=timeout)
    if not ok_rb:
        return False, f"PUT {status} 成功，但读回校验失败：{msg_rb}"
    return True, f"PUT {status} + 读回一致（{msg_rb}）—— CPA 已用上新配置"


def _readback_check(base: str, mgmt_key: str, want: str, *,
                    timeout: int = 30) -> tuple[bool, str]:
    """GET /v0/management/config.yaml 并与期望内容比对行数与四段条目数。

    不做逐字节比对 —— CPA 在 PUT 时会跑 NormalizeCommentIndentation
    （config_basic.go:102），注释缩进可能被规整，字节流本就允许不同。
    比对「行数 + 四段条目数」足以确认是同一份内容，且能抓住
    「读回的是旧文件」这个我们真正担心的情形。
    """
    url = base.rstrip("/") + "/v0/management/config.yaml"
    req = urllib.request.Request(
        url, method="GET", headers={"Authorization": f"Bearer {mgmt_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            got = resp.read().decode("utf-8", "replace")
    except Exception as e:
        return False, f"GET 失败：{e}"

    def count_entries(txt: str) -> dict[str, int]:
        out: dict[str, int] = {}
        lines = txt.splitlines()
        for sec in ("gemini-api-key", "codex-api-key", "claude-api-key",
                    "openai-compatibility"):
            span = _section_span(lines, sec)
            if span is None:
                out[sec] = 0
                continue
            st, en = span
            dash, _f = _detect_indent(lines, st, en)
            out[sec] = sum(1 for i in range(st + 1, en)
                           if lines[i].startswith(dash + "- "))
        return out

    want_n, got_n = count_entries(want), count_entries(got)
    if want_n != got_n:
        detail = ", ".join(f"{k}: 期望 {want_n[k]} 实得 {got_n[k]}"
                           for k in want_n if want_n[k] != got_n[k])
        return False, (f"CPA 读回的条目数不符（{detail}）。"
                       "最可能的原因是 config.yaml 的 inode 被换过 —— "
                       "单文件 bind mount 在容器启动时把 inode 定死了，"
                       "容器仍在读旧文件。需要 docker restart cli-proxy-api")
    total = sum(want_n.values())
    return True, f"四段共 {total} 条目"


def verify_upstream(
    cpa_base: str,
    client_key: str,
    section: str,
    model: str,
    *,
    timeout: int = 120,
    proxy: str | None = None,
) -> tuple[bool, str]:
    """写入并热重载后，打 CPA **自己的**业务端点做端到端确认。

    为什么这一步不能省 —— `POST /v0/management/api-call` 是裸 HTTP 转发
    （api_tools.go:99-215）：不走 translator、不做 cloak/fingerprint 改写，
    原样回 {status_code, header, body}。它只能证明「凭据本身有效」，
    证明不了「接进 CPA 后客户端能用」。

    两者会分叉的真实情形（第 12 章）：直连 200，经 CPA 却换模 —— 因为
    CPA 加了自己的头、走了自己的 translator，上游据此换了后端。

    走的是 CPA 的**客户端入口**（默认 8317 + api-keys 里的 key），
    不是 management 端口。段决定路径：
        claude / compat  → /v1/messages
        codex            → /v1/responses
        gemini           → /v1beta/models/{model}:generateContent
    """
    from .fingerprint import backend_of, model_matches, resp_id, resp_model
    from .request import PROBE_TEXT

    base = cpa_base.rstrip("/")
    headers = {"Content-Type": "application/json"}

    if section == "gemini-api-key":
        url = f"{base}/v1beta/models/{model}:generateContent?key={client_key}"
        payload = {"contents": [{"role": "user",
                                 "parts": [{"text": PROBE_TEXT}]}]}
    elif section == "codex-api-key":
        url = f"{base}/v1/responses"
        headers["Authorization"] = f"Bearer {client_key}"
        payload = {"model": model, "stream": False, "input": PROBE_TEXT}
    else:
        # claude 段与 compat 段都从 CPA 的 /v1/messages 进 —— CPA 自己按
        # 模型名路由到哪一段，这正是要验证的部分
        url = f"{base}/v1/messages"
        headers["Authorization"] = f"Bearer {client_key}"
        headers["x-api-key"] = client_key
        headers["anthropic-version"] = "2023-06-01"
        payload = {"model": model, "max_tokens": 64,
                   "messages": [{"role": "user", "content": PROBE_TEXT}]}

    from . import client as _client

    resp = _client.send(
        url,
        headers=headers,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        proxy=proxy,
        timeout=timeout,
    )

    if resp.status != "200":
        from .classify import body_excerpt, classify

        cat, _why = classify(resp.status, resp.body)
        return False, (f"{resp.status} {cat} · {body_excerpt(resp.body, 200)}")

    actual = resp_model(resp.body)
    rid = resp_id(resp.body)
    backend = backend_of(rid)

    if not model_matches(model, actual):
        # 经 CPA 换模 —— 直连可能是好的，但客户端拿不到要的模型
        return False, (
            f"200 但换模：请求 {model}，返回 {actual}（后端形态 {backend}）。"
            "直连正常不代表接进 CPA 正常"
        )

    return True, f"200 · {actual or model} · 后端 {backend}"


def rebuild_config_full(
    cfg: dict,
    all_plans: dict[tuple[str, str], ImportPlan],
    original_lines: list[str]
) -> tuple[str, list[str]]:
    """全量重建 config.yaml（用于全量重探模式）

    保留：
    - 全局配置（host/port/tls/remote-management/...）
    - 四段的段头位置
    - 各站的人工注释（通过 name 匹配原始条目）

    重建：
    - 每个站的完整配置块（headers/priority/proxy/models/prefix）
    - 按 priority 从高到低排序

    Args:
        cfg: 原始 config.yaml 解析结果
        all_plans: {(base_url, api_key): ImportPlan, ...}
        original_lines: 原始文件的行列表（带 \\n）

    Returns:
        (new_content, warnings)
        new_content: 重建后的完整文件内容
        warnings: 警告列表（如：注释匹配失败）
    """
    warnings = []

    # 1. 提取全局配置（到第一个 *-api-key: 之前）
    global_lines = []
    first_section_idx = None
    for i, line in enumerate(original_lines):
        if re.match(r'^(gemini|codex|claude|openai)-api-key\s*:', line):
            first_section_idx = i
            break
        global_lines.append(line)

    if first_section_idx is None:
        # 没有任何段，异常情况
        warnings.append("原文件未找到任何 *-api-key 段，全局配置可能不完整")
        first_section_idx = len(original_lines)

    # 2. 提取每个站的人工注释（按 name 索引）
    comments_map = _extract_entry_comments(original_lines)

    # 3. 按段组织所有方案
    sections_data = {
        "gemini-api-key": [],
        "codex-api-key": [],
        "claude-api-key": [],
        "openai-compatibility": []
    }

    section_map = {
        "gemini": "gemini-api-key",
        "codex": "codex-api-key",
        "claude": "claude-api-key",
        "compat": "openai-compatibility"
    }

    for (base_url, api_key), plan in all_plans.items():
        # 遍历该站的所有段
        for sp in plan.sections.values():
            if not sp.writable:
                warnings.append(f"站 {base_url} 段 {sp.section} 不可写，跳过")
                continue

            section_full = section_map.get(sp.section)
            if not section_full:
                warnings.append(f"站 {base_url} 段 {sp.section} 映射失败，跳过")
                continue

            sections_data[section_full].append(sp)

    # 4. 按 priority 排序（每段内从高到低）
    for section_full in sections_data:
        sections_data[section_full].sort(
            key=lambda sp: sp.priority,
            reverse=True
        )

    # 5. 渲染四段
    output_lines = global_lines.copy()

    for section_full in ["gemini-api-key", "codex-api-key", "claude-api-key", "openai-compatibility"]:
        entries = sections_data[section_full]
        if not entries:
            # 该段为空，跳过
            continue

        # 段头
        output_lines.append(f"{section_full}:\n")

        # 检测原段的缩进（从原文件读）
        span = _section_span(original_lines, section_full)
        if span:
            dash_indent, field_indent = _detect_indent(original_lines, span[0], span[1])
        else:
            dash_indent, field_indent = "  ", "    "

        # 渲染每个条目
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        for sp in entries:
            # 查找该站的人工注释
            entry_comments = comments_map.get(section_full, {}).get(sp.base_url, [])

            # 生成条目
            entry_lines = _render_entry_full(
                sp,
                dash_indent,
                field_indent,
                stamp,
                entry_comments
            )
            output_lines.extend(entry_lines)

        output_lines.append("\n")

    return "".join(output_lines), warnings


def _extract_entry_comments(lines: list[str]) -> dict[str, dict[str, list[str]]]:
    """提取每个站条目的人工注释

    Returns:
        {section_full: {name_or_base_url: [comment_lines], ...}, ...}
    """
    comments = {}
    current_section = None
    current_entry_name = None
    current_comments = []

    for i, line in enumerate(lines):
        # 检测段头
        m = re.match(r'^(gemini|codex|claude|openai)-api-key\s*:', line)
        if m:
            current_section = m.group(0).rstrip(":")
            current_entry_name = None
            current_comments = []
            if current_section not in comments:
                comments[current_section] = {}
            continue

        # 检测注释行（在条目之前）
        if line.lstrip().startswith("#"):
            current_comments.append(line)
            continue

        # 检测条目名或 base-url
        if current_section:
            # 检测 name: 或 base-url:
            m_name = re.match(r'^\s+name:\s*(.+)', line)
            m_base = re.match(r'^\s+base-url:\s*(.+)', line)

            if m_name:
                current_entry_name = m_name.group(1).strip().strip('"\'')
                if current_comments:
                    comments[current_section][current_entry_name] = current_comments
                    current_comments = []
            elif m_base and not current_entry_name:
                # 如果没有 name，用 base-url 作为 key
                base_url = m_base.group(1).strip().strip('"\'')
                if current_comments:
                    comments[current_section][base_url] = current_comments
                    current_comments = []

    return comments


def _render_entry_full(
    sp: SectionPlan,
    dash_indent: str,
    field_indent: str,
    stamp: str,
    comments: list[str]
) -> list[str]:
    """渲染一个站的完整配置块

    Args:
        sp: SectionPlan（包含 priority/headers/models/...）
        dash_indent: 列表项缩进（如 "  "）
        field_indent: 字段缩进（如 "    "）
        stamp: 时间戳（用于注释）
        comments: 该站的人工注释（原样保留）

    Returns:
        行列表（每行带 \\n）
    """
    lines = []

    # 保留人工注释
    if comments:
        lines.extend(comments)

    # 条目开始
    lines.append(f"{dash_indent}- name: {_quote(sp.base_url)}\n")
    lines.append(f"{field_indent}base-url: {_quote(sp.base_url)}\n")
    lines.append(f"{field_indent}api-key: {_quote(sp.api_key)}\n")

    # models
    if sp.models:
        models_str = "[" + ", ".join(sp.models) + "]"
        lines.append(f"{field_indent}model: {models_str}\n")

    # priority
    lines.append(f"{field_indent}priority: {sp.priority}\n")

    # prefix
    if sp.prefix:
        lines.append(f"{field_indent}prefix: {sp.prefix}\n")

    # proxy-url
    if sp.proxy_url:
        lines.append(f"{field_indent}proxy-url: {_quote(sp.proxy_url)}\n")

    # headers
    if sp.headers:
        lines.append(f"{field_indent}headers:\n")
        for k, v in sorted(sp.headers.items()):
            lines.append(f"{field_indent}  {k}: {_quote(v)}\n")

    # 添加重建时间戳注释
    lines.append(f"{field_indent}# 重建于 {stamp}\n")

    return lines


def _quote(s: str) -> str:
    """YAML 字符串引用（简化版）"""
    if not s:
        return '""'

    # 需要引号的情况
    needs_quote = (
        s[0] in " \t" or
        s[-1] in " \t" or
        ":" in s or
        "#" in s or
        any(c in s for c in ["\n", "\r", "\t", "\\", '"'])
    )

    if not needs_quote:
        return s

    # 双引号 + 转义
    escaped = s
    for old, new in _YAML_ESCAPES.items():
        escaped = escaped.replace(old, new)

    return f'"{escaped}"'

