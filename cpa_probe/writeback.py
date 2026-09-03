"""写回 config.yaml。行级编辑保注释，写前必备份。

为什么行级编辑而不是 yaml.dump
------------------------------
config.yaml 里 198 条注释是两夜排障的全部记忆（「foxtrot 403 User has
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
    # 空段头改写：`claude-api-key: []` 这种自带空数组字面量的段头，插入块
    # 序列前必须先把 `[]` 摘掉，否则产出
    #     claude-api-key: []
    #       - api-key: "..."
    # 是非法 YAML（已是流式空序列，不能再挂块序列）。
    # 值为 (1-based 行号, 新行内容)，由 apply_diffs 就地替换。
    rewrite: tuple[int, str] | None = None
    # 段头补建：这条 diff 的 lines 追加到**文件末尾**，不占 insert_at 坐标。
    #
    # 为什么单开一路而不复用 insert_at：build_diffs 是在「补过段头的
    # lines」上算条目行号的，apply_diffs 却从原文起插。两套坐标系只有在
    # 段头不参与行号排序时才对齐 —— 让段头永远追加到尾部，条目行号就与
    # build_diffs 看到的完全一致。
    #
    # 曾经让段头也走 insert_at：四个段头挤在同一行号，条目串段（codex 的
    # 条目插进 openai-compatibility），后来改成逐个独立行号，又因段头块里
    # 的空行让排序不稳、条目重复落地（4 条变 8 条）。两次都是坐标系混用。
    append_only: bool = False

    def render(self) -> str:
        what = (f"追加进已有 provider {self.merged_into}"
                if self.merged_into else "新增条目")
        head = (f"# {self.section} ← {self.host}（第 {self.insert_at} 行后"
                f"{what}，{len(self.lines)} 行）")
        return head + "\n" + "\n".join(self.lines)


# 四段的 YAML 顶层键。与 parse.SECTIONS 同源，但这里显式列出而不是 import ——
# 顺序在这个模块里有语义（渲染与 span 定位都按它走），而 parse 那边只是集合。
#
# 注意 compat 段的键名是 `openai-compatibility` 而**不是** `openai-api-key`。
# 2026-09-01 审计发现的一个缺陷正是把段头正则写成 `openai-api-key` ——
# 那个键不存在，于是真正的 compat 段匹配不到、被当成全局配置复制一遍，
# 再生成一次，产出两个同名顶层键（yaml.safe_load 静默取后者，原 provider 消失）。
_SECTION_KEYS = (
    "gemini-api-key",
    "codex-api-key",
    "claude-api-key",
    "openai-compatibility",
)

# model_source 的人话标签。只用于 warnings 文案 —— 操作员要能一眼看出
# 「新增这一段的依据有多硬」，而 `probed` / `catalog` 这类内部值看不出来。
_SRC_LABEL = {
    "probed": "本次实测通过",
    "catalog": "站方目录声称有",
    "manual": "你手填的清单",
    "seed": "工具猜测",
}


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


def _empty_literal_rewrite(lines: list[str], start: int,
                           section: str) -> tuple[int, str] | None:
    """段头自带空字面量时，返回把它摘成裸键的改写；否则 None。

    形态：`claude-api-key: []`、`claude-api-key: {}`（含行尾注释）。
    这种段头在全新或被清空的 config.yaml 里很常见 —— CPA 自己生成的
    模板就是这样，而 `[]` 后面直接挂 `- api-key:` 是非法 YAML。

    2026-09-01 实测触发：假门禁站端到端脚本用 `claude-api-key: []` 造
    空段，写回产出的文件 yaml.safe_load 直接报
    `expected <block end>, but found '<block sequence start>'`。
    原来的用例都基于「段里已有条目」的真实 config.yaml，从没覆盖到空段。

    行尾注释保留 —— 那也是人写的。
    """
    m = re.match(rf"^({re.escape(section)}\s*:)\s*(\[\s*\]|\{{\s*\}})\s*(#.*)?$",
                 lines[start])
    if not m:
        return None
    tail = f"  {m.group(3)}" if m.group(3) else ""
    return start + 1, f"{m.group(1)}{tail}"


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
                 extra_keys: list[str] | None = None,
                 key_lines: dict[str, list[str]] | None = None,
                 key_plans: dict[str, SectionPlan] | None = None) -> list[str]:
    """生成一个条目的 YAML 行。

    字段顺序与现有文件一致（api-key, base-url, prefix, priority, models…），
    这样 diff 看起来跟手写的一样，便于人工核对。
    compat 段结构不同 —— provider 级 + api-key-entries。

    max-context-length 只写在**实测过的那个模型**上（sp.context_model）。
    同站不同模型窗口能差一个数量级；把 opus 的实测值抄给 haiku，客户端
    会按错的窗口定压缩点 —— 那正是第 08 章那条 400 的成因。未实测的模型
    留空，CPA 回落内置目录值（service_models.go 各段的 fallback）。

    per-key 字段（compat 段，2026-09-03）：`proxy-url` 与 `weight` 挂在
    `api-key-entries` 的**每一项**上（CPA 的 OpenAICompatibilityAPIKey 只有这
    三个字段：api-key / weight / proxy-url，config_types.go:691-701），所以逐把
    Key 各自决定，绝不跨 Key 套用：

      key_lines  {api_key: [该 Key 原有的续行]} —— 操作员的显式配置，最高优先
      key_plans  {api_key: 该 Key 自己的 SectionPlan} —— 本次探测对它的结论
      两者都没有就不写 —— CPA 侧 proxy 回落全局、weight 默认 1

    实测 kktoken.cc 5 把、ai.hybgzs.com 3 把带 per-key `proxy-url`，而同站
    claude 段那几把故意不带。拿 head 那把的值套给全组会多一跳（不会失败，所以
    validate 与写后验证都发现不了）；weight 更糟，0 会把那把 Key 整个逐出调度池。
    见 compat_key_blocks。
    """
    out: list[str] = []
    note = f"# {stamp} 批量导入 · 得分 {sp.score} · {sp.priority_reason}"

    def model_lines(indent: str) -> list[str]:
        rows: list[str] = []
        for m in sp.models:
            rows.append(f"{indent}- name: {_yaml_str(m)}")
            # alias 写空串，与现有 459 个条目一致（生产 config.yaml 里
            # 100% 是 `alias: ""`）。
            #
            # 为什么不写成与 name 相同（2026-09-02 核对 CPA 源码）：
            # buildConfiguredModelInfo（service_models.go:678-682）在
            # alias 为空时自动回落成 name，所以 `alias: ""` 与
            # `alias: <name>` 的**运行时效果完全相同**；而写死值会让
            # 全量重建把 459 行原本是 `""` 的行全改掉 —— diff 里 459 处
            # 无意义改动，掩盖真正要看的 priority 变化。
            rows.append(f'{indent}  alias: ""')
            if sp.max_context_length and m == sp.context_model:
                rows.append(f"{indent}  max-context-length: {sp.max_context_length}"
                            f"   # 实测值")
        return rows

    if sp.section == "openai-compatibility":
        # compat 段的结构与其余三段**不同**：一个 provider 条目 = 一个上游站，
        # 该站的多个 Key 全部挂在它的 api-key-entries 下。
        #
        # 实测现有 12 个 provider 全部遵循这个约定，且每站名字唯一
        # （foxtrot 15 个 Key、relay-l 15 个，都在同一个 provider 里）。
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
            # per-key 字段（proxy-url / weight）逐把 Key 决定，**绝不跨 Key 套用**。
            #
            # 三个来源，优先级递减：
            #   ① 原文里这把 Key 自己的续行（key_lines）—— 操作员的显式配置
            #   ② 这把 Key 自己的方案（key_plans）—— 本次探测对它的结论
            #   ③ 什么都没有就不写 —— CPA 侧 proxy 回落全局、weight 默认 1
            #
            # 为什么必须逐把（2026-09-03，同一个缺陷改两次）：第一版只搬原文、
            # 原文非空就 `elif` 掉新值；第二版改成合并，但补的是 `sp.proxy_url`
            # —— 那是 **head 那把**的代理，于是组内所有没有原文行的 Key 都被灌上
            # head 的出口。实测 kktoken.cc 5 把带 per-key 代理、claude 段那几把
            # 故意不带，跨 Key 套用会多一跳；weight 更糟，0 会把那把 Key 整个
            # 逐出调度池。多一跳不会失败，所以 validate 与写后验证都发现不了。
            own = list((key_lines or {}).get(key) or [])
            out.extend(ln.rstrip("\n") for ln in own)
            # 这把 Key 自己的方案。head 就是 sp 本身；其余成员由调用方给
            # （build_diffs / rebuild_config_full 都按段归组，拿得到每把的方案）。
            mine = (key_plans or {}).get(key) or (sp if key == sp.api_key else None)
            if not any(re.match(r"^\s*proxy-url\s*:", ln) for ln in own):
                pu = mine.proxy_url if mine is not None else ""
                if pu:
                    out.append(f"{field}    proxy-url: {_yaml_str(pu)}")
            if not any(re.match(r"^\s*weight\s*:", ln) for ln in own):
                w = mine.weight if mine is not None else None
                if w is not None:
                    why = ("   # 原值搬运（weight: 0 = 已逐出调度池）"
                           if w == 0 else "")
                    out.append(f"{field}    weight: {w}{why}")
        if sp.headers:
            out.append(f"{field}headers:")
            for k, v in sp.headers.items():
                out.append(f"{field}  {k}: {_yaml_str(v)}")
        for ln in sp.carry_lines:
            out.append(ln.rstrip("\n"))
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
    # weight 只在全量重建搬运原值时非 None。**不能丢** —— `weight: 0` 是
    # 「把这个站逐出调度池」的唯一表达，而 CPA 缺这个字段时默认 1，
    # 丢掉等于让手工封禁的站全部复活（2026-09-01 审计发现）。
    if sp.weight is not None:
        why = "   # 原值搬运（weight: 0 = 已逐出调度池）" if sp.weight == 0 else ""
        out.append(f"{field}weight: {sp.weight}{why}")
    if sp.proxy_url:
        out.append(f"{field}proxy-url: {_yaml_str(sp.proxy_url)}")
    if sp.headers:
        out.append(f"{field}headers:")
        for k, v in sp.headers.items():
            out.append(f"{field}  {k}: {_yaml_str(v)}")
    # 原条目里 render_entry 不认识的字段，按原文行搬运。
    # 放在 models 之前 —— YAML 映射无序，但放这里让 diff 与原文的键序一致。
    # 已经 rstrip 过换行，写出时由调用方统一补。
    for ln in sp.carry_lines:
        out.append(ln.rstrip("\n"))
    out.append(f"{field}models:")
    out.extend(model_lines(f"{field}  "))
    return out


def _ensure_sections(lines: list[str], plans: list[ImportPlan],
                     stamp: str) -> tuple[list[str], list[Diff]]:
    """为「有可写方案但 config.yaml 里没段头」的段补出段头。

    返回 (补过段头的 lines, 段头 diff 列表)。段头插在文件末尾 ——
    YAML 顶层键无序，位置不影响语义，而插在尾部不动任何现有行、
    也不打乱已有注释的归属。

    只补真正要写入的段：某段一个可写方案都没有就不补，免得给
    config.yaml 添四个空段。
    """
    need: list[str] = []
    for section in _SECTION_KEYS:
        if any(sp is not None and sp.writable
               for plan in plans
               for sec, sp in plan.sections.items() if sec == section):
            if _section_span(lines, section) is None:
                need.append(section)
    if not need:
        return lines, []

    out = list(lines)
    # 末尾没有空行就补一个，别和最后一个条目粘在一起
    while out and not out[-1].strip():
        out.pop()

    # 每个段头**各占一条 diff、各占独立行号**。
    #
    # 曾经的写法是四个段头共用一条 diff 插在同一个 insert_at —— 结果
    # 各段的条目 diff 目标行号全落进同一块，codex 的条目插进了
    # openai-compatibility 段里（yaml.safe_load 不报错，段归属却全错）。
    # 段头必须逐个落地，中间留出空行，条目 span 才各归各段。
    head_diffs: list[Diff] = []
    for i, section in enumerate(need):
        block = [""]
        if i == 0:
            block.append(f"# {stamp} 批量导入：以下段原本不存在，自动补出")
        block.append(f"{section}:")
        out.extend(block)
        head_diffs.append(
            Diff(section=section, insert_at=0, lines=block,
                 host="(段头)", merged_into="", append_only=True)
        )
    return out, head_diffs


def build_diffs(raw: str, plans: list[ImportPlan]) -> list[Diff]:
    """算出每个段要插入什么。**不修改任何现有行** —— 只追加。

    compat 段按主机归并：该段一个条目 = 一个上游站，多个 Key 挂在它的
    api-key-entries 下。其余三段每个 Key 各占一条（现有 config.yaml
    两种约定都实测确认过：foxtrot 在 claude 段 15 条、在 compat 段 1 条）。

    不归并会生成 N 个重名 provider。CPA 的 compat 段不去重
    （SanitizeOpenAICompatibility 只丢缺 base-url 的），于是同一个站被注册
    成 N 个独立 provider，模型清单重复 N 遍 —— 5 个 Key 的站就是 5 份。
    """
    lines = raw.split("\n")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d")

    # 段头缺失就先补出来。config.yaml 常常只有你实际用过的段 —— 缺 codex
    # 或 compat 段头是常态，不是异常。
    #
    # 2026-09-01 修：原来 _section_span 返回 None 就 `continue`，于是勾选
    # 过、参数齐全的段被**静默丢弃** —— 界面显示「已勾选 N 项写入」，
    # 落盘却少几段，而且不报错不警告。这是这一轮最严重的一处：用户以为
    # 写进去了。
    # 补出的段头本身也是一条 diff（插在文件尾），这样 apply_diffs 只认
    # diff 就够，不需要知道 build_diffs 内部改过 lines。
    lines, head_diffs = _ensure_sections(lines, plans, stamp)

    diffs: list[Diff] = list(head_diffs)

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
                    rewrite=_empty_literal_rewrite(lines, start, section),
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
                by_key = {g.api_key: g for g in group}
                add_lines = []
                for k in fresh:
                    add_lines.append(f"{ki}  - api-key: {_yaml_str(k)}")
                    # per-key 的 proxy-url 取**这把 Key 自己**的探测结论，
                    # 不是 head 的（2026-09-03）。组内出口不一致是常态：
                    # 实测同站有的 Key 走 mihomo、有的直连可用，套用 head
                    # 会给不需要代理的 Key 多加一跳 —— 不失败，所以 validate
                    # 与写后验证都发现不了。
                    mine = by_key.get(k)
                    if mine is not None and mine.proxy_url:
                        add_lines.append(
                            f"{ki}    proxy-url: {_yaml_str(mine.proxy_url)}")
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
                                       extra_keys=keys[1:],
                                       key_plans={g.api_key: g
                                                  for g in group}),
                    host=host,
                    rewrite=_empty_literal_rewrite(
                        lines, start, "openai-compatibility"),
                )
            )
    return diffs


def apply_diffs(raw: str, diffs: list[Diff]) -> str:
    """把 diff 应用到原文。从后往前插，避免行号偏移。

    空段头改写（rewrite）先做：它只改一行、不动行数，所以和插入的行号
    互不影响。同一段有多个 diff 时改写内容相同，重复执行是幂等的。
    """
    lines = raw.split("\n")

    # ① 补建的段头先追加到尾部 —— build_diffs 算条目行号时看到的就是这个
    #    形态，两边坐标系必须一致。
    heads = [d for d in diffs if d.append_only]
    if heads:
        while lines and not lines[-1].strip():
            lines.pop()
        for d in heads:
            lines.extend(d.lines)

    # ② 空段头改写：只改一行、不动行数，与插入的行号互不影响。
    #    同段多个 diff 携带相同改写，重复执行是幂等的。
    for d in diffs:
        if d.rewrite:
            ln, text = d.rewrite
            lines[ln - 1] = text

    # ③ 条目从后往前插，避免行号偏移
    for d in sorted((x for x in diffs if not x.append_only),
                    key=lambda x: -x.insert_at):
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


def _orphan_entry_lines(lines: list[str], span: tuple[int, int],
                        section: str,
                        planned: set[tuple[str, str]]) -> list[str]:
    """段内**未被本次方案覆盖**的条目原文行（含它们自己的注释）。

    整段重写只写 planned 里的凭据，这个函数把其余条目原样捞出来接在后面。
    见 render_section 里 keep_unplanned 那一段的说明。

    识别条目边界靠「该段条目级 dash 的缩进」—— 嵌套结构里的
    `- status: 403` 也是 dash 行，按「有没有 dash」切会把一个条目切成好几段。
    与 extract_carry_lines 同一套判据。

    compat 段不走这里：它的结构是 provider 级 + api-key-entries，一个条目
    含多个 Key，「未覆盖」要在 Key 粒度判断，语义与前三段不同。
    """
    from .parse import host_of

    start, end = span
    item_indent: int | None = None
    blocks: list[tuple[list[str], str, str]] = []   # (行, host, api_key)
    cur: list[str] = []
    cur_host = cur_key = ""
    pending: list[str] = []                        # 归属下一个条目的注释

    def flush() -> None:
        nonlocal cur, cur_host, cur_key
        if cur:
            blocks.append((cur, cur_host, cur_key))
        cur, cur_host, cur_key = [], "", ""

    for i in range(start + 1, end):
        line = lines[i]
        st = line.strip()
        if not st:
            (cur if cur else pending).append(line)
            continue
        if st.startswith("#"):
            (cur if cur else pending).append(line)
            continue
        ind = len(line) - len(line.lstrip())
        is_dash = bool(re.match(r"^\s*-\s+\S", line))
        if is_dash and item_indent is None:
            item_indent = ind
        if is_dash and ind == item_indent:
            flush()
            if pending:
                cur.extend(pending)
                pending = []
        cur.append(line)
        m = re.match(r"^\s*(?:-\s+)?(api-key|base-url)\s*:(.*)$", line)
        if m:
            val = _scalar_value(m.group(2))
            if m.group(1) == "api-key":
                cur_key = val
            else:
                cur_host = host_of(val)
    flush()

    out: list[str] = []
    for blk, h, k in blocks:
        if not h or not k:
            continue                    # 认不出身份的块不搬 —— 宁可漏不可错
        if (h, k) in planned:
            continue
        out.extend(blk)
    return out


def _orphan_provider_lines(lines: list[str], span: tuple[int, int],
                           touched_hosts: set[str]) -> list[str]:
    """compat 段里**本次方案没碰到**的 provider 的原文行。

    `touched_hosts` 里放的是 `compat_provider_key()` 的结果（归一化 base-url，
    含路径），不是主机名 —— 同一台主机可以按路径挂多个互不相干的上游，按
    host 判会把没碰到的那个也当成已覆盖然后丢掉。

    与 _orphan_entry_lines 分开写，因为 compat 的结构不同：一个 provider
    条目含多个 api-key-entries，「未覆盖」只能按 provider 判，不能按
    单个 Key 判 —— 按 Key 判会把同一个 provider 撕成两半。
    """
    start, end = span
    item_indent: int | None = None
    blocks: list[tuple[list[str], str]] = []
    cur: list[str] = []
    cur_host = ""
    pending: list[str] = []

    def flush() -> None:
        nonlocal cur, cur_host
        if cur:
            blocks.append((cur, cur_host))
        cur, cur_host = [], ""

    for i in range(start + 1, end):
        line = lines[i]
        st = line.strip()
        if not st or st.startswith("#"):
            (cur if cur else pending).append(line)
            continue
        ind = len(line) - len(line.lstrip())
        is_dash = bool(re.match(r"^\s*-\s+\S", line))
        if is_dash and item_indent is None:
            item_indent = ind
        if is_dash and ind == item_indent:
            flush()
            if pending:
                cur.extend(pending)
                pending = []
        cur.append(line)
        # provider 级 base-url 的缩进 == item_indent + 2；api-key-entries
        # 底下的行更深，不会误取。
        m = re.match(r"^\s*(?:-\s+)?base-url\s*:(.*)$", line)
        if m and item_indent is not None and ind <= item_indent + 2:
            cur_host = compat_provider_key(_scalar_value(m.group(1)))
    flush()

    out: list[str] = []
    for blk, h in blocks:
        if not h or h in touched_hosts:
            continue
        out.extend(blk)
    return out


def compat_provider_key(base_url: str) -> str:
    """compat provider 的归并身份：归一化后的 base-url（**含路径**）。

    为什么不能用 host（2026-09-03 端到端演练抓到）：同一台主机上可以挂多个
    互不相干的上游，靠路径区分 —— `tools/e2e_redetect.py` 的假上游正是
    `127.0.0.1:PORT/good` 与 `.../gate` 两个 provider。按 host 归并会把它们
    合成一条，一个站的 Key 被灌进另一个站。

    为什么不能用原始 base_url：同一个 provider 的写法可能有多种（尾斜杠、
    带不带 `/v1`、scheme 大小写）。按原文归并会分成两组、渲染出两个同站
    provider —— 而 CPA 按 `name` 索引冷却、模型能力与执行路由，两个同名
    provider 会让这三处对同一个 Key 命中两套配置，同一把 Key 还会在轮询池
    里占两个位。

    归一化：剥 scheme、转小写、去尾斜杠、去尾 `/v1`。剥 scheme 是因为
    `http://` 与 `https://` 同路径在实践中是同一个上游的两种写法，分开写
    两条 provider 属于配置错误而不是意图。
    """
    s = (base_url or "").strip().lower()
    s = re.sub(r"^https?://", "", s).rstrip("/")
    if s.endswith("/v1"):
        s = s[:-3].rstrip("/")
    return s


def compat_key_blocks(lines: list[str]) -> dict[str, dict[str, list[str]]]:
    """compat 段每个 provider 下**每把 Key 自己的**续行，按 (provider, api-key) 索引。

    provider 键是 `compat_provider_key(base-url)` —— 含路径，见那个函数的说明。

    返回 {provider_key: {api_key: [该 Key 的续行原文, ...]}}。续行**不含**
    `- api-key:` 那一行本身（调用方要重新渲染它），也就是 per-key 的
    `proxy-url` / `weight` 之类。

    为什么必须有（2026-09-03，两处缺陷共用一个成因）
    ---------------------------------------------
    render_entry 渲染 compat 条目时，`api-key-entries` 下只写 `- api-key: X`
    加上一个**全组共用**的 proxy-url。而 compat 段的结构是「一个 provider
    条目 = 一个站，多把 Key 挂在它下面」，per-key 字段是真实存在的：

      ① 组内没进方案的 Key 会消失。`_orphan_provider_lines` 只保留「整个
         provider 都没被碰到」的条目，被碰到的 provider 整条重写 —— 组内
         少一把 Key 就少一把。实测生产配置 gorouter.app 15 把、
         tabitoken.com 14 把，只要一把探测抛异常（BatchProber 会把它整个
         凭据从 results 里去掉）就丢一把。前三段有 `_orphan_entry_lines`
         兜这个，compat 段没有。
      ② per-key 的 proxy-url 被统一。实测 kktoken.cc 5 把、ai.hybgzs.com
         3 把带 per-key `proxy-url: http://mihomo:7890`，组内不一致时
         全组按 head 那把写 —— 多一跳不会失败，所以 validate 与写后验证
         都发现不了，又是一处静默改行为。

    与 extract_carry_lines 的分工：那个函数抓的是**条目级**（provider 级）
    的未知字段，它对 api-key-entries 整块是跳过的 —— 这里补的正是那一块。
    """
    out: dict[str, dict[str, list[str]]] = {}
    span = _section_span(lines, "openai-compatibility")
    if not span:
        return out
    start, end = span

    item_indent: int | None = None
    cur_host = ""
    # api-key-entries 块内的状态
    in_keys = False
    keys_dash_indent: int | None = None
    cur_key = ""
    cur_lines: list[str] = []

    def flush_key() -> None:
        nonlocal cur_key, cur_lines
        if cur_host and cur_key:
            out.setdefault(cur_host, {})[cur_key] = list(cur_lines)
        cur_key, cur_lines = "", []

    for i in range(start + 1, end):
        line = lines[i]
        st = line.strip()
        if not st or st.startswith("#"):
            # 注释与空行归属当前 Key（如果正在收集一把 Key）
            if in_keys and cur_key:
                cur_lines.append(line)
            continue
        ind = len(line) - len(line.lstrip())
        is_dash = bool(re.match(r"^\s*-\s+\S", line))

        if is_dash and item_indent is None:
            item_indent = ind
        # 新的 provider 条目
        if is_dash and item_indent is not None and ind == item_indent:
            flush_key()
            in_keys = False
            keys_dash_indent = None
            cur_host = ""

        # provider 级 base-url（缩进 <= item_indent + 2）
        m = re.match(r"^\s*(?:-\s+)?base-url\s*:(.*)$", line)
        if m and item_indent is not None and ind <= item_indent + 2:
            cur_host = compat_provider_key(_scalar_value(m.group(1)))

        # 进入 / 离开 api-key-entries 块
        if re.match(r"^\s*api-key-entries\s*:", line):
            flush_key()
            in_keys = True
            keys_dash_indent = None
            continue
        if in_keys:
            # 缩进退回到 api-key-entries 同级或更浅 = 这一块结束
            if not is_dash and keys_dash_indent is not None and ind < keys_dash_indent:
                flush_key()
                in_keys = False
                keys_dash_indent = None
                # 这一行属于 provider 级，继续走下面的通用逻辑
            elif is_dash and (keys_dash_indent is None or ind == keys_dash_indent):
                if keys_dash_indent is None:
                    keys_dash_indent = ind
                flush_key()
                km = re.match(r"^\s*-\s+api-key\s*:(.*)$", line)
                cur_key = _scalar_value(km.group(1)) if km else ""
                continue
            elif cur_key:
                # 当前 Key 的续行（per-key proxy-url / weight …）
                cur_lines.append(line)
                continue
    flush_key()
    return out


# 新增段（凭据原本没配这一段）放不放行，**只看模型清单有没有依据**。
# probed / manual / catalog 放行，seed 不放行 —— 见 rebuild_config_full 第 2 步
# 的完整说明。
#
# 单独抽出来是因为有**三个调用方**必须给出同一个答案（2026-09-03）：
#   · rebuild_config_full —— 真正决定写不写
#   · server 的 /api/plan —— 决定界面上这一段显示成「建议写入」还是「不写入」
#   · 前端                —— 决定默认勾不勾
# 上一版只有第一个有闸，另两个按「没有闸」渲染，于是界面显示建议写入并默认
# 勾上，勾了写不进，只在 warnings 里留一句话。
_NEW_SECTION_SOURCES = frozenset({"probed", "manual", "catalog"})


def new_section_admitted(model_source: str) -> bool:
    """这个 model_source 够不够格新增一个原本不存在的 (凭据, 段) 条目。"""
    return model_source in _NEW_SECTION_SOURCES


def is_new_section(cfg: dict, sp: SectionPlan,
                   owned: dict[tuple[str, str], set[str]] | None = None) -> bool:
    """这个方案落盘时是「新增段」还是「更新既有条目」。

    owned 可复用（一次重探要问几百次，别每次重扫 cfg）。
    """
    from .parse import host_of

    table = owned if owned is not None else owned_sections(cfg)
    have = table.get((host_of(sp.base_url), sp.api_key))
    # have 为 None = 整个凭据都是新的（增量导入），那是「新凭据」而不是
    # 「已有凭据新增一段」—— 走原有的新增路径，不受这道闸约束。
    return have is not None and sp.section not in have


def mark_new_sections(cfg: dict, plans: list[ImportPlan]) -> int:
    """给每个方案打上 new_section / write_blocked，返回被拦下的段数。

    **必须在 assign_priorities 之前调用。** 定档与影响面都按「有哪些段要写」
    算，而被拦下的段不会落盘 —— 让它们参与定档会白占档位（`taken` 被污染，
    各站被挤得更低），影响面也会把不存在的条目算进遮挡关系。

    为什么单独一步而不是在 rebuild_config_full 里顺手做（2026-09-03）：
    那个函数是**整条链的最后一步**，在它内部判就意味着界面拿到的
    writable / recommended 与落盘结果不一致 —— 上一版正是如此，界面显示
    「建议写入」、默认勾上，勾了写不进。
    """
    owned = owned_sections(cfg)
    blocked = 0
    for plan in plans:
        for sp in plan.sections.values():
            # 每次 /api/plan 都重建方案对象，理论上无残留；显式清零是为了
            # 让复用同一批对象的调用方（测试、脚本）也拿到干净结果。
            sp.new_section = is_new_section(cfg, sp, owned)
            sp.write_blocked = ""
            if sp.new_section and not new_section_admitted(sp.model_source):
                sp.write_blocked = (
                    f"原本没配这一段，而本次模型清单是"
                    f"{_SRC_LABEL.get(sp.model_source, sp.model_source)}"
                    f"（没有实测依据）—— 不新增。确知该站这一段可用的话，"
                    f"在模型格里手填真实清单，它就会作为新条目写入")
                blocked += 1
    return blocked


def owned_sections(cfg: dict) -> dict[tuple[str, str], set[str]]:
    """每个凭据 (host, api_key) **原本占了哪几个段**。

    为什么必须有（2026-09-02 生产事故）：全量重探为每个凭据的四段都生成方案，
    整段重写时全部写进去 —— 121 个条目变 246 个。而真实情况是每个凭据只配了
    自己那几段：实测 79 个凭据里跨四段的只有 9 个，跨两段 49 个、单段 10 个，
    合计 177 个 (凭据, 段) 组合。

    凭空多出来的条目不是「多配一点没坏处」：那个凭据在那一段**没有依据可用**，
    写进去只会让 CPA 每次轮到它吃一次失败，耗掉 request-retry ×
    max-retry-credentials 的预算（实测配置：1 轮额外重试 × 12 个凭据）。

    但这张表**不是**「不许新增段」的意思（2026-09-03 修正）：原来的用法是
    「不在这张表里就跳过」，把「探测发现原来没配的段也能用」一起挡掉了 ——
    那正是最该新增的条目。中转站常先只卖 claude，后来加开 codex，而配置里
    没人回头补。现在这张表只回答「这是更新还是新增」，新增放不放行由
    `model_source` 定（见 rebuild_config_full 第 2 步）。

    键与 existing_weights / existing_proxies 同一套 —— base-url 在不同段
    形态不同（codex/compat 带 /v1），只有 host 稳定。
    """
    from .parse import host_of

    out: dict[tuple[str, str], set[str]] = {}
    for section in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        for e in cfg.get(section) or []:
            if not isinstance(e, dict):
                continue
            h = host_of(str(e.get("base-url") or ""))
            k = str(e.get("api-key") or "")
            if h and k:
                out.setdefault((h, k), set()).add(section)

    for prov in cfg.get("openai-compatibility") or []:
        if not isinstance(prov, dict):
            continue
        h = host_of(str(prov.get("base-url") or ""))
        for ke in prov.get("api-key-entries") or []:
            if not isinstance(ke, dict):
                continue
            k = str(ke.get("api-key") or "")
            if h and k:
                out.setdefault((h, k), set()).add("openai-compatibility")
    return out


def rebuild_config_full(
    cfg: dict,
    all_plans: dict[tuple[str, str], ImportPlan],
    original_lines: list[str],
    *,
    only_owned: bool = True,
    keep_unplanned: bool = True,
) -> tuple[str, list[str]]:
    """全量重建四段，**原文件的其余部分逐字保留**。

    为什么是「替换四段」而不是「重新拼装文件」（2026-09-01 审计发现三个数据
    销毁缺陷后重写）
    -----------------------------------------------------------------
    第一版的做法是：取「第一个段之前的行」当全局配置，然后从 all_plans 生成四段
    拼在后面。三个后果，且 validate() 全部报成功：

      · **排在第一个段之后的全局键全部消失**。实测 `api-keys`（客户端认证凭据）、
        `remote-management`（含管理密钥）、`quota-exceeded`、`logging-to-file`
        一起丢 —— 丢 api-keys 的后果是所有客户端立刻断连。
      · **某段没有可写方案时该段整段消失**，哪怕原文件里有条目。触发条件低到
        「这一段的站这次全部判重复」。
      · 段头正则写的是 `openai-api-key`（不存在的键），真正的
        `openai-compatibility` 匹配不到 —— 它会被当成全局配置复制一遍，
        然后再生成一次，产出两个同名顶层键。

    现在的做法：拿原文件逐行走，只在四段的 span 内替换内容，其余原样输出。
    没有可写方案的段**不动它**（保留原条目），而不是删掉。

    Args:
        cfg: 原始 config.yaml 解析结果（用于 compat 段的既有 provider 信息）
        all_plans: {(base_url, api_key): ImportPlan, ...}
        original_lines: 原始文件的行列表（带换行符）

    Returns:
        (new_content, warnings)
    """
    warnings: list[str] = []
    from .parse import host_of as _host_of

    # 1. 注释索引（按 (段, 键)）
    comments_map = _extract_entry_comments(original_lines)

    # 1b. 原条目里 render_entry 不认识的字段（request-scoped-errors /
    #     excluded-models / websockets / fingerprint-profile / disabled…）。
    #     整段重写会把它们抹掉，所以按原文行搬回去 —— 见 extract_carry_lines。
    carry_map = extract_carry_lines(original_lines)

    # 1c. compat 段 per-key 的续行（proxy-url / weight …），按 (host, Key) 索引。
    #     extract_carry_lines 抓的是 provider 级字段，对 api-key-entries 整块
    #     是跳过的 —— 这一份补那一块。见 compat_key_blocks 的两处成因说明。
    key_blocks = compat_key_blocks(original_lines)

    def attach_carry(sp: SectionPlan) -> None:
        """给方案补上该条目原有的 carry 行。已经有了就不动（用户覆盖优先）。"""
        if sp.carry_lines:
            return
        d = carry_map.get(sp.section) or {}
        exact = carry_key(_host_of(sp.base_url), sp.api_key)
        if exact in d:
            # 原文件里有这个凭据的条目 —— 用它自己的，哪怕是空的。
            # 退到兜底键会把同站另一条的字段染过来（实测 zulu 的
            # fingerprint-profile 从 1 个条目扩散到 3 个）。
            sp.carry_lines = list(d[exact])
            return
        # 新导入的 Key：原文件没有它的条目，拿同站的规则当默认
        h = _host_of(sp.base_url)
        got = d.get(h) or d.get(sp.base_url)
        if got:
            sp.carry_lines = list(got)

    # 2. 按段归集可写方案。section 用的是**完整 YAML 段名** ——
    #    pipeline 与 build_plan 一路如此，不做短名映射。
    #
    # only_owned 的判据（2026-09-03 重写）：**这一段这次有没有实测依据**，
    # 而不是「原来配过没有」。
    #
    # 原来的判据是后者，起因是 2026-09-02 的事故：探测给每个凭据的四段都生成
    # 方案，整段重写全写进去，121 条目变 246。那时四段无条件都算可写。
    #
    # 但「原来配过没有」把用户要的能力一起挡掉了：探测发现某个凭据在原来没配
    # 的段也能用时，那正是最该新增的条目 —— 中转站常常先只卖 claude，后来加开
    # codex，而配置里没人回头补。实测生产配置：79 个凭据 × 4 段 = 316，实占
    # 177，139 个空位全被这道闸挡住。
    #
    # 更糟的是界面按「没有这道闸」渲染：build_plan 给那些段的 writable 与
    # recommended 都是 True，界面显示「建议写入」并默认勾上 —— 勾了写不进，
    # 只在 warnings 里留一句话。
    #
    # 新判据按证据强弱分档（model_source，见 SectionPlan 的说明）：
    #   · 已占有的段  —— 照写。条目本来就在，这是「更新」而不是「新增」，
    #                    它存在本身就是先前的依据。
    #   · probed      —— 本次实测跑通了推理。新增它有实测依据，写。
    #   · manual      —— 操作员显式手填了模型清单。显式意图优先于工具推测，写。
    #   · catalog     —— 只有站方目录声称有，推理没通过。recommended=False，
    #                    默认不勾；操作员勾了就是显式意图，写。
    #   · seed        —— 工具写死的猜测，没有任何依据。**不写** —— 那正是
    #                    121 → 246 那次事故的成因。
    #
    # catalog 与 seed 的差别不在「可信度高一档」这么模糊的地方：catalog 的名字
    # 是这个站自己报的，seed 的名字是本工具猜的，后者与这个站没有任何关系。
    owned = owned_sections(cfg) if only_owned else {}
    skipped_unowned = 0
    added_unowned: list[str] = []

    sections_data: dict[str, list[SectionPlan]] = {s: [] for s in _SECTION_KEYS}
    for (base_url, api_key), plan in all_plans.items():
        for sp in plan.sections.values():
            if not sp.writable:
                warnings.append(f"{plan.host} 段 {sp.section} 不可写，跳过")
                continue
            if sp.section not in sections_data:
                warnings.append(f"{plan.host} 段 {sp.section} 不是已知段名，跳过")
                continue
            if only_owned:
                have = owned.get((_host_of(sp.base_url), sp.api_key))
                # have 为 None = 这是个新凭据（增量导入混进重探），照写。
                # have 非空但不含本段 = 原来没配这一段 —— 按证据强弱决定。
                if have is not None and sp.section not in have:
                    if not new_section_admitted(sp.model_source):
                        skipped_unowned += 1
                        continue
                    # 身份用 base_url 而不是 plan.host —— 同一台主机可以按
                    # 路径挂多个上游（假上游的 /good 与 /gate），只报 host
                    # 会出现「同一行文本重复几遍」而看不出是哪一个。
                    added_unowned.append(
                        f"{sp.base_url} · {sp.section}"
                        f"（{_SRC_LABEL.get(sp.model_source, sp.model_source)}）")
            sections_data[sp.section].append(sp)

    if skipped_unowned:
        warnings.append(
            f"{skipped_unowned} 个 (凭据, 段) 组合原本不在 config.yaml 里，"
            f"且本次探测没有实测依据（模型清单只是工具猜测）—— 已跳过。"
            f"确知可用的话在结果表里手填模型清单，就会作为新条目写入")
    if added_unowned:
        shown = "、".join(added_unowned[:8])
        warnings.append(
            f"新增 {len(added_unowned)} 个原本不在 config.yaml 里的 (凭据, 段)："
            f"{shown}{'…' if len(added_unowned) > 8 else ''} —— "
            f"这些段本次探测通过或由你手填，已按新档位一并计入定档与影响面")

    # 3. compat 段按**归一化后的 base-url**（含路径）归并 —— 一个条目 =
    #    一个上游站，多个 Key 挂在 api-key-entries 下。
    #
    #    键的选择见 compat_provider_key：不能用 host（同一主机可用路径挂多个
    #    互不相干的上游，实测假上游 `.../good` 与 `.../gate`），也不能用原始
    #    base_url（同一个 provider 的写法可能有尾斜杠 / `/v1` / scheme 之差，
    #    分组会裂成两条，渲染出两个同站 provider —— CPA 按 name 索引冷却、
    #    模型能力与执行路由，重名会让这三处对同一个 Key 命中两套配置，同一把
    #    Key 还在轮询池里占两个位）。
    compat_groups: dict[str, list[SectionPlan]] = {}
    for sp in sections_data["openai-compatibility"]:
        compat_groups.setdefault(compat_provider_key(sp.base_url), []).append(sp)
    for pkey, group in compat_groups.items():
        spellings = {sp.base_url for sp in group}
        if len(spellings) > 1:
            # 取 priority 最高那个的写法（下面 head 用的就是它），并说出来 ——
            # 静默选一个会让另一种拼写的段悄悄换了 base-url。
            keep = max(group, key=lambda x: x.priority).base_url
            warnings.append(
                f"段 openai-compatibility · {pkey}：本次方案里有 "
                f"{len(spellings)} 种 base-url 写法（{'、'.join(sorted(spellings))}）"
                f"—— 已合并成一个 provider 并采用 {keep}，"
                f"否则会写出两个同站条目、同一把 Key 占两个轮询位")

    # 4. 排序：前三段按 priority 降序；compat 按组内最高的 priority
    #    （用 head 的会让「head 恰好是低档那个」的组被错误定位）
    for s in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        sections_data[s].sort(key=lambda sp: sp.priority, reverse=True)
    compat_ordered = sorted(
        compat_groups.items(),
        key=lambda kv: max(x.priority for x in kv[1]), reverse=True)

    stamp = datetime.datetime.now().strftime("%Y-%m-%d")

    def render_section(section: str) -> list[str] | None:
        """生成该段的条目行。返回 None 表示「这一段不要动」。"""
        if section == "openai-compatibility":
            if not compat_ordered:
                return None
            span = _section_span(original_lines, section)
            dash, field = (_detect_indent(original_lines, span[0], span[1])
                           if span else ("  ", "    "))
            out: list[str] = []
            used: set[str] = set()
            for pkey, group in compat_ordered:
                # head 取组内 priority 最高的那个，不是插入顺序的第一个 ——
                # 组的其余成员只贡献 api-key，所以 head 的选择决定了整组用
                # 哪一套 headers/priority/models。
                head = max(group, key=lambda x: x.priority)
                extra = [g.api_key for g in group if g is not head]
                # 组内**没进方案**的 Key 也要保留（2026-09-03）。
                #
                # 前三段有 _orphan_entry_lines 兜这件事，compat 段没有 ——
                # _orphan_provider_lines 只保留「整个 provider 都没被碰到」
                # 的条目，被碰到的 provider 整条重写，组内少一把 Key 就少
                # 一把。而「没进方案」有无害成因：探测抛异常（BatchProber
                # 会把那个凭据整个从 results 去掉）、用户没勾、该段判不可写。
                # 实测生产配置 gorouter.app 15 把、tabitoken.com 14 把。
                own_keys = key_blocks.get(pkey) or {}
                planned_keys = {sp.api_key for sp in group}
                orphan_keys = ([k for k in own_keys if k not in planned_keys]
                               if keep_unplanned else [])
                if orphan_keys:
                    extra = extra + orphan_keys
                    warnings.append(
                        f"段 {section} · {pkey}：{len(orphan_keys)} 把 Key 不在本次"
                        f"方案内（探测异常 / 未勾选 / 判不可写）—— 已原样保留在"
                        f"这个 provider 下，不会因整条重写而丢失")
                attach_carry(head)
                for c in _comments_for(comments_map, section, head, used, _host_of):
                    out.append(c if c.endswith("\n") else c + "\n")
                # 每把 Key 自己的方案 —— per-key 的 proxy-url / weight 逐把取，
                # 不拿 head 的值套给全组（见 render_entry 的 per-key 一节）。
                for line in render_entry(head, dash, field, stamp,
                                         extra_keys=extra,
                                         key_lines=own_keys,
                                         key_plans={g.api_key: g
                                                    for g in group}):
                    out.append(line + "\n")

            # compat 段的「未覆盖」按 **provider 身份**判 —— 它的结构是
            # provider 级 + api-key-entries，一个条目含多个 Key。本次方案
            # 没碰到的 provider 整条原样保留，否则「只勾了 1 个站」会把另外
            # 12 个 provider 全删掉（2026-09-02 实测 13 → 1）。
            if keep_unplanned and span:
                touched = {p for p, _g in compat_ordered}
                kept = _orphan_provider_lines(original_lines, span, touched)
                if kept:
                    out.extend(kept)
                    warnings.append(
                        f"段 {section}：{len(touched)} 个 provider 按新方案重写，"
                        f"其余 provider 已原样保留")
            return out

        entries = sections_data.get(section) or []
        if not entries:
            return None
        span = _section_span(original_lines, section)
        dash, field = (_detect_indent(original_lines, span[0], span[1])
                       if span else ("  ", "    "))
        out = []
        used = set()
        for sp in entries:
            attach_carry(sp)
            for c in _comments_for(comments_map, section, sp, used, _host_of):
                out.append(c if c.endswith("\n") else c + "\n")
            for line in render_entry(sp, dash, field, stamp):
                out.append(line + "\n")

        # keep_unplanned：本段有方案的凭据只是一部分，其余原条目**原样保留**。
        #
        # 为什么必须有（2026-09-02 生产事故）：整段重写会把「没进方案」的条目
        # 一并抹掉。而「没进方案」有三种完全无害的原因 ——
        #   · 用户只勾了推荐项，其余段没勾
        #   · 那个段判不可写（models 为空）
        #   · 探测时抛异常，那个凭据整个不在结果里
        # 三种都不该导致删除。删除只应由用户显式操作，不该是「没勾」的副作用。
        if keep_unplanned and span:
            planned = {(_host_of(x.base_url), x.api_key) for x in entries}
            kept = _orphan_entry_lines(original_lines, span, section, planned)
            if kept:
                out.extend(kept)
                warnings.append(
                    f"段 {section}：{len(entries)} 条按新方案重写，"
                    f"另有条目不在本次方案内 —— 已原样保留")
        return out

    # 5. 逐行走原文件，只替换四段的 span 内容，其余逐字保留。
    #    span 是 (start, end)：start 是段头那一行，end 是段内最后一个实质行之后。
    spans: dict[str, tuple[int, int]] = {}
    for section in _SECTION_KEYS:
        sp_ = _section_span(original_lines, section)
        if sp_:
            spans[section] = sp_

    # 段的出现顺序按原文件，不按我们的偏好 —— 重排顶层键会让 diff 变成整文件改动
    ordered = sorted(spans.items(), key=lambda kv: kv[1][0])

    out_lines: list[str] = []
    cursor = 0
    replaced: list[str] = []
    for section, (start, end) in ordered:
        # 段之前的内容（含其他全局键、注释、空行）原样输出
        out_lines.extend(original_lines[cursor:start])
        body = render_section(section)
        if body is None:
            # 没有可写方案 —— **保留原条目**，不是删掉。
            # 删掉的后果是「这一段的站这次全部判重复」就把整段清空。
            out_lines.extend(original_lines[start:end])
        else:
            out_lines.append(original_lines[start])      # 段头原样
            out_lines.extend(body)
            replaced.append(section)
        cursor = end

    # 最后一个段之后的所有内容 —— 这里正是第一版丢掉 api-keys 与
    # remote-management 的地方
    out_lines.extend(original_lines[cursor:])

    # 原文件里没有的段：在末尾补一个。
    #
    # 「保留既有内容」与「新段有处可去」两件都要 —— 只做前者会让「原文件没有
    # codex 段但这次探到了 codex 可用」的方案静默无处安放，只留一条警告。
    for section in _SECTION_KEYS:
        if section in spans:
            continue
        body = render_section(section)
        if body is None:
            continue
        if out_lines and out_lines[-1].strip():
            out_lines.append("\n")
        out_lines.append(f"{section}:\n")
        out_lines.extend(body)
        warnings.append(f"原文件没有 {section} 段，已在末尾新建")

    untouched = [s for s in spans if s not in replaced]
    if untouched:
        warnings.append(
            "以下段本次没有可写方案，原条目已原样保留："
            + "、".join(untouched))

    return "".join(out_lines), warnings


def _comments_for(comments_map: dict, section: str, sp: SectionPlan,
                  used: set, host_of_fn) -> list[str]:
    """按多个候选键查该站的人工注释，同一份只挂一次。

    多候选是因为 sp.base_url 是 base_for_section() 的产物（codex/compat 补了
    /v1），与原文件里写的未必一致。去重是因为前三段每个 Key 各占一条，
    按 host 匹配会让原文件里只出现一次的注释在重建后出现 N 次。
    """
    sec = comments_map.get(section, {})
    for cand in (sp.base_url, host_of_fn(sp.base_url)):
        if not cand or cand not in sec:
            continue
        if cand in used:
            return []
        used.add(cand)
        return sec[cand]
    return []


# render_entry 自己会写的字段。搬运原字段时要跳过它们 —— 否则同一个键会
# 出现两次（YAML 里后者覆盖前者，值可能是旧的）。
_RENDERED_KEYS = frozenset({
    "api-key", "base-url", "prefix", "priority", "weight", "proxy-url",
    "headers", "models", "name", "api-key-entries",
})


def carry_key(host: str, api_key: str) -> str:
    """carry 行的精确索引键。用 NUL 分隔 —— host 与 key 都可能含任何可打印字符。"""
    return f"{host}\x00{api_key}"


def _scalar_value(tail: str) -> str:
    """取 `键: 值 # 注释` 里的值部分，剥掉行尾注释。

    为什么必须剥（2026-09-02 实测）：生产 config.yaml 里有
    `base-url: "https://api.example.com" # 注意不带 /v1` 这种写法。
    不剥注释时值带着 ` # 注意不带 /v1`，host_of 解析不出主机名，
    整条目的 carry 索引键就少一个 —— 28 个条目因此搬不到。

    只在引号闭合之后才认 `#`：值本身可以含井号（`prefix: "a#b"`）。
    """
    t = tail.strip()
    if not t:
        return ""
    if t[0] in "\"'":
        q = t[0]
        i = 1
        while i < len(t):
            if t[i] == "\\":
                i += 2
                continue
            if t[i] == q:
                return t[1:i]
            i += 1
        return t[1:]                       # 引号没闭合，原样给回
    # 裸标量：第一个 ` #` 之前
    cut = t.find(" #")
    return (t[:cut] if cut >= 0 else t).strip()


def extract_carry_lines(lines: list[str]) -> dict[str, dict[str, list[str]]]:
    """提取每个条目里 render_entry **不认识**的字段，按 (段, 站名) 索引原文行。

    为什么必须有（2026-09-02 拿生产 config.yaml 核对发现）
    -------------------------------------------------
    render_entry 是白名单式渲染，只写它知道的 10 个字段；而全量重探用它
    **整段重写**。生产配置 108 个条目里 106 条带白名单外的字段，重写后
    全部静默消失 —— validate() 报成功，YAML 也合法，只是行为变了：

        request-scoped-errors  105 条   冷却规则，丢了坏站不再被剔除
        excluded-models         39 条   `["*"]` = 只用显式列的模型
        websockets               2 条   codex 的 WebSocket 开关
        fingerprint-profile      1 条   让 CPA 自己补设备指纹
        disabled                 1 条   手工停用的 provider 会复活

    存**原文行**而不是解析后的值：这些字段结构任意深（request-scoped-errors
    是对象数组），重新序列化要处理缩进、引号风格、键序；而原文行拿来就能用、
    逐字保真、diff 也干净。

    索引键有两级（2026-09-02 实测同 host 多条目会互相覆盖后改）：
      · 精确键 `host\\x00api-key` —— 前三段是「一个 Key 一条」，同一个站在
        codex 段能有 3 条，只按 host 索引时后一条会覆盖前一条的 carry 行。
        实测 alfa.example 的 `websockets: true` 只在第一个 Key 上，被后两个
        无该字段的条目覆盖掉。
      · 兜底键 `host` 与 base-url 原文 —— compat 段是「一个站一条、多 Key 挂
        api-key-entries」，没有 per-key 的 carry；另外新导入的 Key 也没有精确
        匹配的原条目，用 host 拿同站的规则是合理的默认。

    值都用 `base_for_section` 之前的原文 host：sp.base_url 经过加工
    （codex/compat 补 /v1），与原文里的写法对不上，只有 host 稳定。
    """
    from .parse import host_of

    out: dict[str, dict[str, list[str]]] = {}
    section = None
    # 当前条目：收集到的 carry 行 + 它的候选键
    buf: list[str] = []
    keys: list[str] = []
    # 当前条目的 host 与 api-key —— 用来建精确键 host\x00api-key
    cur_host: str = ""
    cur_key: str = ""
    # 正在跳过某个多行字段（如 models: 下面的整块）时的缩进阈值
    skip_indent: int | None = None
    # 本段条目级 dash 的缩进。**必须按它判断新条目**，不能只看「有没有 dash」——
    # request-scoped-errors 底下的 `- status: 403` 也是 dash 行，按后者判断会在
    # 每个嵌套列表项上误触发 flush()，把刚收集的 carry 行连同索引键一起清空。
    # 2026-09-02 实测：前三段 106 个待搬条目只搬出 39 个，就是这个原因。
    item_indent: int | None = None

    def flush() -> None:
        nonlocal buf, keys, cur_host, cur_key
        if section and (cur_host or keys):
            d = out.setdefault(section, {})
            # 精确键**总是**写，哪怕 buf 是空的。
            #
            # 空列表是有意义的信号：「这个凭据在原文件里确实没有额外字段」。
            # 没有它时 attach_carry 会退到兜底键，把同站另一个条目的字段
            # 染给它 —— 实测 zulu 一个 Key 有 fingerprint-profile，
            # 同站另两个没有，重建后三个都有了（1 → 3）。
            if cur_host and cur_key:
                d[carry_key(cur_host, cur_key)] = list(buf)
            # 兜底键只在有内容且尚未写过时写 —— 它服务的是「新导入的 Key，
            # 原文件里没有对应条目」，那时拿同站规则是合理的默认。
            if buf:
                for k in keys:
                    d.setdefault(k, list(buf))
        buf, keys = [], []
        cur_host, cur_key = "", ""

    def indent_of(ln: str) -> int:
        return len(ln) - len(ln.lstrip())

    for line in lines:
        m = re.match(r"^(gemini-api-key|codex-api-key|claude-api-key"
                     r"|openai-compatibility)\s*:", line)
        if m:
            flush()
            section = m.group(1)
            skip_indent = None
            item_indent = None
            continue
        if section is None:
            continue

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # 离开本段（顶层键）
        if re.match(r"^[a-zA-Z_][a-zA-Z0-9_-]*\s*:", line):
            flush()
            section = None
            skip_indent = None
            item_indent = None
            continue

        ind = indent_of(line)

        # 正在跳过某个白名单字段的子块（models: / headers: / api-key-entries:）
        if skip_indent is not None:
            if ind > skip_indent:
                continue
            skip_indent = None

        # 新条目起点：**该段条目级缩进**上的 dash 行。第一个 dash 定基准。
        # 用缩进而不是「是不是 dash」—— 见 item_indent 的说明。
        dash_here = bool(re.match(r"^\s*-\s+\S", line))
        if dash_here and item_indent is None:
            item_indent = ind
        is_item = dash_here and ind == item_indent
        if is_item:
            flush()
            # 条目首行的字段与后续字段行对齐（dash 缩进 + "- " 宽度）
            ind += 2

        # 取字段名。条目首行形如 `  - api-key: x`，字段名在 `- ` 之后
        fm = re.match(r"^\s*(?:-\s+)?([a-zA-Z_][a-zA-Z0-9_-]*)\s*:", line)
        if not fm:
            # 不是 `键:` 形态（列表项的值行等），归入当前 carry 块
            if buf:
                buf.append(line)
            continue
        field_name = fm.group(1)
        inline_value = _scalar_value(line.split(":", 1)[1])

        if field_name in _RENDERED_KEYS:
            # 记键：name 与 host_of(base-url) 都要
            val = inline_value
            if field_name == "name" and val:
                keys.append(val)
            elif field_name == "base-url" and val:
                h = host_of(val)
                if h:
                    keys.append(h)
                    cur_host = h
                keys.append(val)
            elif field_name == "api-key" and val:
                cur_key = val
            # 只有「值在后续行」的字段才需要整块跳过（models: / headers: /
            # api-key-entries:）。行内就有值的（api-key: "x"）不能设 —— 那会
            # 把同一条目里紧随其后的字段全部吞掉。
            if not inline_value:
                skip_indent = ind
            continue

        # 白名单外的字段 —— 连同它的子块一起搬
        buf.append(line)

    flush()
    return out


def _extract_entry_comments(lines: list[str]) -> dict[str, dict[str, list[str]]]:
    """提取每个站条目的人工注释，按 (段, 键) 索引。

    键的选择：`name`（compat 段有）与 `host_of(base-url)` 两个都建，
    渲染时按同样的两个候选去查。

    为什么不能只用 base-url 原文（端到端验证抓到）
    -------------------------------------------
    写回时 `sp.base_url` 是 `base_for_section()` 的产物 —— codex 与 compat
    段会补上 `/v1`，gemini 与 claude 不补。而注释是从原文件读的，那里的
    base-url 是站方当初怎么写就怎么存。两边字符串对不上，注释就静默丢失。

    取 host 做键绕开这个问题：协议、路径、尾部 /v1 都不参与比较，
    而同一个站在同一段里只会有一个条目（多 Key 走 compat 的
    api-key-entries，不是多条目）。
    """
    from .parse import host_of

    comments: dict[str, dict[str, list[str]]] = {}
    current_section = None
    pending: list[str] = []

    for line in lines:
        # 段头：顶层键
        m = re.match(r"^(gemini-api-key|codex-api-key|claude-api-key"
                     r"|openai-compatibility)\s*:", line)
        if m:
            current_section = m.group(1)
            comments.setdefault(current_section, {})
            pending = []
            continue

        if current_section is None:
            continue

        stripped = line.strip()

        # 注释行：攒着，等下一个条目认领
        if stripped.startswith("#"):
            pending.append(line)
            continue

        # 空行不清空 pending —— 注释与条目之间可以隔空行
        if not stripped:
            continue

        # 顶层键（非本段）：离开该段
        if re.match(r"^[a-zA-Z_][a-zA-Z0-9_-]*\s*:", line):
            current_section = None
            pending = []
            continue

        m_name = re.match(r"^\s*-?\s*name:\s*(.+)$", line)
        m_base = re.match(r"^\s*-?\s*base-url:\s*(.+)$", line)

        if m_name and pending:
            key = m_name.group(1).strip().strip("\"'")
            comments[current_section][key] = pending
            # 不清空 —— 同一条目的 base-url 可能在下一行，两个键都要能查到
        elif m_base and pending:
            raw_base = m_base.group(1).strip().strip("\"'")
            h = host_of(raw_base)
            if h:
                comments[current_section][h] = pending
            comments[current_section][raw_base] = pending
            pending = []

    return comments
