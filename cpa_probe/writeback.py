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

import copy
import datetime
import hashlib
import json
import os
import re
import shutil
import textwrap
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import betas
from .plan import ImportPlan, SectionPlan


def _load_yaml(text: str):
    """Reject explicit duplicate keys before YAML merge expansion."""
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        _merge_token = object()

        def flatten_mapping(self, node):
            # Check the original mapping, not the flattened inherited pairs.
            if not getattr(node, "_keys_checked", False):
                node._keys_checked = True
                seen = set()
                for key, _value in node.value:
                    token = self._merge_token if key.tag == "tag:yaml.org,2002:merge" else (
                        self.construct_object(key, deep=True),)
                    if token in seen:
                        raise ValueError("duplicate YAML key")
                    seen.add(token)
            super().flatten_mapping(node)

    try:
        return yaml.load(text, Loader=UniqueLoader)
    except Exception:
        # Parser exceptions include source snippets, which may contain credentials.
        raise ValueError("YAML invalid or duplicate mapping key") from None


def _dump_fields(values: dict, indent: str) -> list[str]:
    import yaml
    if not values:
        return []
    return [indent + line for line in yaml.safe_dump(
        values, allow_unicode=True, sort_keys=False).rstrip("\n").splitlines()]


def _field_fragments(lines: list[str], omitted: set[str], indent: str) -> list[str]:
    """Keep untouched field text and its nested comments, using YAML boundaries."""
    import yaml
    text = textwrap.dedent("\n".join(x.rstrip("\r\n") for x in lines))
    node = yaml.compose(text)
    source = text.splitlines()
    if isinstance(node, yaml.SequenceNode):
        node = node.value[0]
    if not isinstance(node, yaml.MappingNode):
        return []
    out = []
    for index, (key, value) in enumerate(node.value):
        if key.value in omitted:
            continue
        start = key.start_mark.line
        end = (node.value[index + 1][0].start_mark.line
               if index + 1 < len(node.value) else len(source))
        # Flow members share lines; emit their semantic value instead.
        if node.flow_style:
            return _dump_fields({k: v for k, v in
                                 (_load_yaml(text)[0] if text.lstrip().startswith("-")
                                  else _load_yaml(text)).items()
                                 if k not in omitted}, indent)
        width = key.start_mark.column
        for offset, line in enumerate(source[start:end]):
            if offset == 0:
                line = line[width:]
            else:
                line = line[min(width, len(line) - len(line.lstrip())):]
            out.append(indent + line)
    return out


def _semantic_equal(left, right) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return (len(left) == len(right) and
                all(any(_semantic_equal(k, rk) and _semantic_equal(v, rv)
                        for rk, rv in right.items()) for k, v in left.items()))
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _semantic_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, float) and left != left:
        return right != right
    return left == right


def _source_records(lines: list[str], section: str) -> list[tuple[dict, list[str]]]:
    """Use YAML nodes for record boundaries, including flow and zero-indent lists."""
    import yaml
    text = "\n".join(line.rstrip("\r\n") for line in lines)
    cfg = _load_yaml(text) or {}
    root = yaml.compose(text)
    if not isinstance(root, yaml.MappingNode):
        return []
    seq = next((v for k, v in root.value if k.value == section), None)
    if not isinstance(seq, yaml.SequenceNode):
        return []
    rows = cfg.get(section) or []
    result = []
    for row, node in zip(rows, seq.value):
        if not isinstance(row, dict):
            raise ValueError("YAML provider record must be a mapping")
        start, end = node.start_mark.line, node.end_mark.line
        if (node.end_mark.column and end < len(lines) and
                lines[end][:node.end_mark.column].strip()):
            end += 1
        if (seq.flow_style or node.flow_style or
                not re.match(r"^\s*-\s", lines[start]) or
                "&" in lines[start] or "*" in lines[start]):
            block = ["  " + ln for ln in yaml.safe_dump(
                [row], allow_unicode=True, sort_keys=False).rstrip().splitlines()]
        else:
            block = [ln.rstrip("\r\n") for ln in lines[start:end]]
        result.append((row, block))
    return result


def _source_identity(base: str) -> str:
    """上游身份：只折叠「同一个上游的不同写法」，其余一律当作不同来源。

    折叠哪些、保留哪些
    ----------------
    折叠：主机名大小写、尾斜杠、**尾部单个 `/v1`**。
    保留：scheme、userinfo、路径大小写、查询串、fragment —— 那几项换了就
    是另一个渠道，折叠会把两个上游的配置串到一起。

    为什么必须折叠尾部 `/v1`（2026-09-12 实测抓到）
    -------------------------------------------
    `parse.base_for_section` 对 codex / compat 段**一律补上** `/v1`
    （见那个函数里 `_NEEDS_V1` 的说明），所以方案侧的 base 永远带 `/v1`；
    而 config.yaml 里手写的条目常常不带（CPA 自己拼 `/chat/completions`
    时对两种写法都能用）。两侧不折叠这一段，身份就对不上：

      · `_original_entry` 查不到原条目 → prefix / headers / proxy-url /
        weight / 模型级字段**全部搬不回来**
      · `rebuild_config_full` 认为这是个新 provider → 同一个站在
        openai-compatibility 段里渲染出**两条** provider，同一把 Key 在
        CPA 的轮询池里占两个位，而冷却与模型能力按 `name` 索引、两条同站
        provider 的 name 一个是原短名一个是现编的 host，两套状态各走各的

    实测形态：config 里 `base-url: "https://o.example.com"` + 方案侧
    `https://o.example.com/v1` → 落盘 2 条 provider，prefix `CH`、
    per-key `proxy-url` 全丢。

    `/v1beta` 之类不受影响（判据是整段 `/v1`），claude / gemini 段也安全：
    `base_for_section` 把它们的 `/v1` 剥掉，两侧同样落在剥掉那一侧。
    """
    from urllib.parse import urlsplit, urlunsplit
    raw = str(base or "").strip()
    parsed = urlsplit(raw if "://" in raw else "//" + raw)
    userinfo, marker, authority = parsed.netloc.rpartition("@")
    netloc = userinfo + marker + authority.lower() if marker else parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc,
                       path, parsed.query, parsed.fragment))


def _original_entry(cfg: dict, sp: SectionPlan) -> dict:
    matches = []
    for row in cfg.get(sp.section) or []:
        if _source_identity(row.get("base-url", "")) != _source_identity(sp.base_url):
            continue
        keys = ([k.get("api-key") for k in row.get("api-key-entries") or []]
                if sp.section == "openai-compatibility" else [row.get("api-key")])
        if sp.api_key in keys:
            if sp.provider_name and row.get("name") != sp.provider_name:
                continue
            matches.append(row)
    if len(matches) > 1:
        raise ValueError("Ambiguous source identity; select a unique provider")
    return copy.deepcopy(matches[0]) if matches else {}


def _prepare_source_plan(cfg: dict, sp: SectionPlan,
                         source_block: list[str] | None = None) -> SectionPlan:
    """Rebuild prior data from exact source; never trust host-only carry tables."""
    old = _original_entry(cfg, sp)
    sp = copy.deepcopy(sp)
    sp.carry_lines = _dump_fields(
        {k: v for k, v in old.items() if k not in _RENDERED_KEYS}, "    ")
    if source_block:
        try:
            kept = _field_fragments(source_block, _RENDERED_KEYS, "    ")
            if _semantic_equal(_load_yaml(textwrap.dedent("\n".join(kept))) or {},
                               {k: v for k, v in old.items() if k not in _RENDERED_KEYS}):
                sp.carry_lines = kept
        except Exception:
            pass  # External YAML anchors are already resolved in old.
    sp.prior_context = {}
    sp.prior_model_extras = {}
    sp.prior_toggles = {k: old[k] for k in ("websockets", "support-prompt-cache-key")
                       if k in old}
    for model in old.get("models") or []:
        name = model.get("name")
        if name:
            sp.prior_model_extras[name] = {k: v for k, v in model.items()
                                          if k not in ("name", "max-context-length")}
            if "max-context-length" in model:
                sp.prior_context[name] = model["max-context-length"]
    sp.headers = merge_entry_headers(old.get("headers"), sp.headers)
    if sp.weight is None:
        sp.weight = old.get("weight")
    if not sp.proxy_url:
        sp.proxy_url = old.get("proxy-url", "")
    if not sp.prefix:
        sp.prefix = old.get("prefix", "")
    if sp.section == "openai-compatibility":
        sp.provider_name = old.get("name", sp.provider_name)
    return sp


def _compat_capability(sp: SectionPlan, old: dict | None = None) -> str:
    row = _load_yaml("\n".join(render_entry(
        sp, "", "  ", "", original_entry=old)))[0]
    for key in ("name", "api-key-entries", "priority", "base-url"):
        row.pop(key, None)
    return json.dumps(row, sort_keys=True, ensure_ascii=True, default=str)


def _unselected_records(lines: list[str], section: str,
                        selected: set[tuple[str, str]], field: str,
                        host_tier: dict[str, tuple[int, str]] | None = None,
                        realigned: list[str] | None = None,
                        skipped: list[str] | None = None) -> list[str]:
    """没进本次方案的条目原文。给了 host_tier 就把 priority 对齐到同站新档。

    为什么必须对齐（2026-09-12 接回来）
    --------------------------------
    原样搬回留守条目是对的（删除只该由用户显式操作），但**原样**包含
    priority：同站另外几把 Key 拿到新值时，落盘结果里这一个站就有了两个
    priority。CPA 的层级隔离只取最高可用桶，两层意味着低档那批只在高档
    全部不可用时才轮到 —— 用户第 4 条要的「同一网址上游 Key 即使不同、
    优先级也要相同」被破坏，多 Key 并行轮询退化成主备切换。

    这件事本来由 `_orphan_entry_lines` 做，但调用方改走了本函数，
    对齐能力就一起掉了（`_orphan_entry_lines` 成了死代码）。这里把它接回来：
    只改条目级那一行**裸整数**写法的 priority，其余字段与注释逐字保留；
    改不动的（引号、锚点、行内表等）记进 skipped 交给调用方报出来。
    """
    import re
    import yaml
    from .parse import host_of as _host_of
    out = []
    for old, block in _source_records(lines, section):
        base = _source_identity(old.get("base-url", ""))
        if section == "openai-compatibility":
            keys = old.get("api-key-entries") or []
            remaining = [k for k in keys if (base, k.get("api-key")) not in selected]
            if keys and not remaining:
                continue
            if len(keys) != len(remaining):
                old = dict(old, **{"api-key-entries": remaining})
                block = [field[:-2] + ln for ln in yaml.safe_dump(
                    [old], allow_unicode=True, sort_keys=False).rstrip().splitlines()]
        elif (base, old.get("api-key")) in selected:
            continue
        host = _host_of(str(old.get("base-url") or ""))
        if host_tier and host in host_tier:
            new_pri, note = host_tier[host]
            if int(old.get("priority") or 0) != new_pri:
                hit = False
                # 条目级那一行的缩进 = 条目首行 `- ` 的缩进 + 2。
                #
                # 不能用「第一条 priority」当判据（2026-09-12）：字段顺序
                # 不保证，原文件里就有
                #     - api-key: …
                #       request-scoped-errors:
                #         configs:
                #           example:
                #             priority: 1     ← 嵌套的无关键，缩进更深
                #       priority: 900         ← 真正要改的那一行
                # 这种形状。按「第一条」会把嵌套里那个无关键改成档位值，
                # 条目级反而没对齐 —— 三重错误，且 YAML 仍然合法。
                lead = re.match(r"^(\s*)-\s", block[0]) if block else None
                want_indent = (lead.group(1) + "  ") if lead else None
                for i, ln in enumerate(block):
                    m = re.match(r"^(\s*)priority:\s*(\d+)\s*(#.*)?$",
                                 ln.rstrip("\n"))
                    if not m:
                        continue
                    if want_indent is not None and m.group(1) != want_indent:
                        continue        # 嵌套结构里的同名键，跳过
                    nl = "\n" if ln.endswith("\n") else ""
                    block[i] = f"{m.group(1)}priority: {new_pri}  # {note}{nl}"
                    hit = True
                    break
                if hit:
                    if realigned is not None and host not in realigned:
                        realigned.append(host)
                elif skipped is not None and host not in skipped:
                    skipped.append(host)
        out.extend(block)
    return out


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
    replace_all: bool = False

    def render(self) -> str:
        what = (f"追加进已有 provider {self.merged_into}"
                if self.merged_into else "新增条目")
        if self.replace_all:
            what = "重建配置（保留未选来源）"
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
    "prior": "沿用原 config.yaml 的清单",
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
    with open(dst, "r+b") as saved:
        os.fsync(saved.fileno())
    if os.name != "nt":
        directory_fd = os.open(os.path.dirname(os.path.abspath(dst)), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return dst


# 顶层键那一行：零缩进、不是列表项、不是注释，且这一行里有个冒号。
#
# 不要求键名是标识符 —— YAML 允许 `a.b:` 与 `"my key":`，而把它们判成
# 「不是顶层键」会让上一段的 span 吞掉它们（见 _section_span 的说明）。
#
# 要求「有冒号」而不是「以冒号结尾」：顶层键可以带值（`host: "127.0.0.1"`）。
# 排除 `-` 开头是因为零缩进的列表项属于上一个键的值。
_TOP_LEVEL_KEY = re.compile(r"^(?![-#\s])[^\s:][^:]*:(\s|$)")


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
        # 顶层键 = 零缩进、不是列表项、不是注释。
        #
        # 判据从「标识符正则」放宽到「零缩进且不以 - 或 # 开头」
        # （2026-09-04 修）：原来的 `^[a-zA-Z_][\w-]*\s*:` 认不出含点号的键
        # （`a.b: 42`）与引号键（`"my key": 42`）—— 那时段尾判在更后面，
        # 那个顶层键被划进 span、进而被 extract_carry_lines 当成条目的 carry 行
        # 收走，重建后它从顶层**消失**。
        #
        # CPA 的 46 个顶层 yaml tag 全是合法标识符，两份生产文件的 42 个顶层键
        # 也全合法，所以这是补闸不是修事故。但「零缩进且不是列表项」本来就是
        # YAML 顶层键的完整判据，正则那一版只是它的一个子集。
        if _TOP_LEVEL_KEY.match(l):
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


# 非空 flow 序列的段头：`claude-api-key: [{api-key: "k1", ...}]`。
#
# 只匹配到 `[` 为止 —— 内容可能在下一行（`claude-api-key: [` 换行再列条目），
# 那时首行 `[` 之后什么都没有。闭合位置由 `_flow_section_span` 按括号计数找。
# 排除 `[]`（空 flow 由 _empty_literal_rewrite 处理，两者不能都命中）。
_FLOW_SECTION_HEAD = re.compile(
    r"^([A-Za-z0-9_.\-\"']+\s*:)\s*\[\s*(?!\]\s*(?:#.*)?$)")


def _flow_section_span(lines: list[str], start: int) -> int | None:
    """段头是**非空** flow 序列时，返回它占到第几行（不含）；否则 None。

    为什么需要（2026-09-05 修）
    ----------------------
    `_empty_literal_rewrite` 只认 `[]` 与 `{}`。而 `claude-api-key:
    [{api-key: "k1", ...}]` 是合法 YAML、CPA 读得出来，全量重建却会把块序列
    挂在它后面：

        claude-api-key: [{api-key: "k1", ...}]
          - api-key: "k1"                       ← 非法

    `validate()` 挡住了（不会写坏文件），但**全量重建对这种文件整个不可用**，
    而报错是 `while parsing a block mapping` —— 看不出根因是段头形态。

    按括号计数找闭合，不用正则 —— flow 里可以嵌任意深度且能跨行。
    引号内的括号不计数（`base-url: "https://x/[a]"` 这种）。

    当前使用 YAML 节点结束位置，替代旧计数器，以正确处理转义引号。
    """
    if not _FLOW_SECTION_HEAD.match(lines[start]):
        return None
    import yaml
    try:
        root = yaml.compose("\n".join(x.rstrip("\r\n") for x in lines))
        if isinstance(root, yaml.MappingNode):
            for key, value in root.value:
                if key.start_mark.line == start and isinstance(value, yaml.SequenceNode):
                    return value.end_mark.line + bool(value.end_mark.column)
    except Exception:
        pass
    return None                     # 没闭合 —— 文件本身有问题，交给 validate


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


# YAML plain 标量能安全承载的头名：HTTP token 字符集的常见子集。
# 只有它才免引号 —— 其余一律走 _yaml_str。
#
# 为什么不无条件加引号（2026-09-04）：生产 config.yaml 里 277 个头名全是这种
# 形态、全部不带引号，无条件加引号会让 diff 里多出 277 处纯风格改动，把真正
# 要看的 priority 变化埋掉。而不加引号的风险只存在于**非** token 名字上
# （`a #c` / `null` / `1` / 含制表符），那些走 _yaml_str。
_SAFE_HEADER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9!#$%&'*+.^_`|~-]*$")

# YAML 1.1 的字面量关键字。它们**符合** token 形态却会被解析成非字符串：
# `null` → None、`true`/`yes`/`on` → True。PyYAML 与 Go 的 yaml.v3 都这样读，
# 于是 CPA 侧 `map[string]string` 拿到的键不是那个头名 —— 头静默消失。
# 大小写不敏感（YAML 认 `Null` / `TRUE` / `Yes`）。
_YAML_KEYWORDS = frozenset({
    "null", "~", "true", "false", "yes", "no", "on", "off",
})


def _yaml_header_name(name: str) -> str:
    """头名的 YAML 字面量。常规 HTTP token 原样，其余加引号转义。

    名字来自 `overrides.headers` 与前端编辑器，两处都只 trim 不校验形态
    （server.py 的 `str(k)`、web/app.js 的 `.trim()`）。实测未转义时：
      · `a #c` 单头 → 本工具 validate **放过**，CPA 报
        `cannot unmarshal !!str into map[string]string`（工具放过、CPA 拒收）
      · `null` → 双侧「合法」，但 YAML 把键读成 None，CPA 读出的 Headers
        为空 —— 那个头静默消失
      · `1` / `true` → 键类型变 int / bool
    """
    n = str(name)
    if n.lower() in _YAML_KEYWORDS:
        return _yaml_str(n)
    return n if _SAFE_HEADER_NAME.match(n) else _yaml_str(n)


def _weight_note(w: int) -> str:
    """weight 行尾该加什么说明。按 CPA 的**实际**语义判，不只判 `== 0`。

    核对 internal/credentialweight/weight.go:21-28 与 selector.go:380-396：
      · `<= 0`        Normalize 返回 0 → positiveWeightAuths 把它整个剔除
      · `> 1000000`   Normalize 返回**错误** → authWeight 走 `errParse != nil`
                      那一支也返回 0 → 同样被剔除
      · 其余          正常权重，不加说明

    只给 `0` 加说明会让 `-1` 与超上限的值看着像正常权重 —— 而它们的效果
    与 0 完全相同。这两种值都不是本工具生成的（只在原值搬运时出现），
    但既然搬回来就得把语义说对。

    注意这一切只在 `routing.strategy = weighted-round-robin` 下成立：
    round-robin（默认）与 fill-first 根本不读 weight。界面那条警告按
    `weight_zero_excludes(cfg)` 分岔措辞，这里的行尾注释是给读文件的人看的，
    所以写「已逐出调度池」时不重复策略前提 —— 上方的段级注释里有。
    """
    if w <= 0:
        return "   # 原值搬运（<= 0：加权轮询下已逐出调度池）"
    if w > 1_000_000:
        return ("   # 原值搬运（超过上限 1000000，CPA 解析失败按 0 处理"
                "：加权轮询下已逐出调度池）")
    return ""


def _yaml_field(indent: str, key: str, val) -> list[str]:
    """把一个已解析的 YAML 值渲染回行。只支持模型条目里会出现的形状。

    用途是搬运 render_entry 白名单外的**模型级**字段（display-name /
    thinking / image / force-mapping / is-compat / input-modalities /
    output-modalities）—— 见 existing_model_extras。

    形状有限是有意的：这些字段在 CPA 的结构体里只有标量、字符串数组与
    一层浅映射（thinking 的 `levels: [...]`）。遇到认不出的形状**整个跳过**
    而不是硬塞 —— 写出半个结构比丢掉更糟，那会让 YAML 变成合法但语义错误的
    东西，而 validate() 只看语法。跳过至少是可见的（diff 里那几行不在了）。
    """
    # 键也要能安全写出（2026-09-04 修）：子键来自原文件的解析结果，
    # 而 YAML 允许 `a: b` 这种含冒号的键。原来直接 `str(k2)` 插值，
    # 于是 `{'a: b': 1}` 写成 `a: b: 1` —— 非法 YAML，整份配置读不出来。
    k = _yaml_header_name(str(key))
    if isinstance(val, bool):
        return [f"{indent}{k}: {'true' if val else 'false'}"]
    if isinstance(val, int):
        return [f"{indent}{k}: {val}"]
    if isinstance(val, float):
        # 非有限值（inf / nan）与科学计数法在 YAML 里读回是**字符串**
        # （`1e+20` → `'1e+20'`），那是语义漂移而不是保真。按「认不出的形状
        # 整个跳过」处理 —— 与下面 list/dict 分支同一条原则。
        # CPA 的模型级字段里没有 float（全是 int / bool / []string），
        # 所以这一支只在原文件手工写了浮点时才会走到。
        import math
        if not math.isfinite(val):
            return []
        txt = repr(val)
        if "e" in txt or "E" in txt:
            return []
        return [f"{indent}{k}: {txt}"]
    if isinstance(val, str):
        return [f"{indent}{k}: {_yaml_str(val)}"]
    if isinstance(val, (list, tuple)):
        if not val:
            return [f"{indent}{k}: []"]
        if all(isinstance(x, (str, int, float, bool)) for x in val):
            # 列表元素里的 float 同样要过有限性检查
            if any(isinstance(x, float)
                   and (x != x or x in (float("inf"), float("-inf"))
                        or "e" in repr(x) or "E" in repr(x))
                   for x in val):
                return []
            rows = [f"{indent}{k}:"]
            for x in val:
                rows.append(f"{indent}  - " + (
                    ("true" if x else "false") if isinstance(x, bool)
                    else (str(x) if isinstance(x, (int, float))
                          else _yaml_str(str(x)))))
            return rows
        return []
    if isinstance(val, dict):
        if not val:
            return [f"{indent}{k}: {{}}"]
        rows = [f"{indent}{k}:"]
        for k2 in sorted(val):
            sub = _yaml_field(f"{indent}  ", str(k2), val[k2])
            if not sub:
                return []       # 子结构认不出 —— 整个字段跳过，别写半截
            rows.extend(sub)
        return rows
    return []


def find_compat_provider(lines: list[str], base_url: str) -> dict | None:
    """在 compat 段里按 base-url 找已存在的 provider，返回它的行位置与 name。

    为什么必须先找：`name` 就是 CPA 的 provider 身份 ——
    `util.OpenAICompatibleProviderKey(name)` 的结果被写进 Auth 的
    `provider_key`，而冷却（conductor_cooldown.go:73）、模型能力
    （api_key_model_capabilities.go:186）、执行路由（conductor_execution.go:1605-1609）
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
            # 值一律走 _scalar_value：它会剥行尾注释（2026-09-04 修）。
            #
            # 原来只 strip 引号。生产 config.yaml 里前三段有 86 行
            # `base-url: "https://x" # 注意不带 /v1` 这种写法，同一种写法迁到
            # compat 段就让 base_here 带着注释文本 —— 与 want 比不相等，
            # 于是「同站再来新 Key」会新建一个同 base-url 的 provider，
            # 而 CPA 的 SanitizeOpenAICompatibility 不去重：同一把 Key 在
            # 轮询池里占两个位，冷却/能力/路由三处各命中一套配置。
            #
            # 同文件的 compat_key_blocks 与 _scalar_value 早就正确剥注释了，
            # 只有这一处漏了。
            m = re.match(r"^\s*-?\s*base-url:\s*(.+?)\s*$", l)
            if m and not base_here:
                base_here = _scalar_value(m.group(1)).rstrip("/")
            m = re.match(r"^\s*-?\s*name:\s*(.+?)\s*$", l)
            if m and not name_here and not in_keys:
                name_here = _scalar_value(m.group(1))
            m = re.match(r"^(\s*)api-key-entries:\s*$", l)
            if m:
                keys_line = st + j
                keys_indent = m.group(1)
                in_keys = True
                continue
            if in_keys:
                mk = re.match(r"^\s*-\s*api-key:\s*(.+?)\s*$", l)
                if mk:
                    # api-key 同样可能带行尾注释（生产配置里有
                    # `api-key: "sk-x" # 2026-08-20 新增` 这种写法）
                    existing.append(_scalar_value(mk.group(1)))
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


# 值需要脱敏的字段。键名匹配不区分大小写，也认带 `- ` 前缀的列表项。
#
# 为什么按**字段名**而不是按值形态（如 `sk-` 前缀）：中转站的 Key 形态五花八门
# （`sk-`、`AIza`、纯 hex、UUID、base64），按形态匹配必然漏。字段名是有限集，
# 且它就是 CPA 的 schema —— 漏一个字段比漏一种形态容易发现。
_SECRET_FIELDS = frozenset({
    "api-key",              # 四段的上游凭据
    "secret-key",           # remote-management 的管理密钥（bcrypt 哈希）
    "password",
    "token",
    "access-token",
    "refresh-token",
    "client-secret",
})

# `api-keys:` 那种下面挂裸标量列表的顶层键（CPA 的客户端入口 Key）。
# 它的值不在 `键: 值` 形态里，而是独立的 `  - sk-xxx` 行，所以要按「当前在
# 哪个块里」判断。
_SECRET_LIST_KEYS = frozenset({"api-keys"})

# 值里**可能内嵌**凭据的字段：`scheme://user:pass@host`。生产配置里这种写法
# 0 处（两份都查过），但 mihomo 之外的代理常要用户名密码，所以留一道。
# 与 _SECRET_FIELDS 分开处理：这里只抹 userinfo 段，主机与端口要留着 ——
# 那是排障时最需要看的部分。
_URL_FIELDS = frozenset({"proxy-url", "base-url"})
_URL_USERINFO = re.compile(r"(://)([^/@\s\"']+):([^/@\s\"']+)@")

_KV_LINE = re.compile(r"^(\s*(?:-\s+)?)([A-Za-z0-9_.\-\"']+)(\s*:\s*)(.*)$")
_LIST_ITEM = re.compile(r"^(\s*)-\s+(.+?)\s*$")


def _short_mask(value: str) -> str:
    """凭据的脱敏形态：前 6 后 4，与 parse.mask_key 同一口径。

    2026-09-12 抽出来：结构化脱敏与行级脱敏都要用它，两处口径必须一致，
    否则同一份 diff 里会出现两种形态，看不出哪种代表「被改动了」。
    """
    from .parse import mask_key
    return mask_key(value)


def _scrub_structural_secrets(text: str) -> str:
    import yaml
    from urllib.parse import parse_qsl, quote, unquote, urlsplit

    secrets: set[str] = set()
    edits = []
    seen = set()
    safe_headers = {"user-agent", "content-type", "accept", "anthropic-version",
                    "anthropic-beta", "x-channel", "originator"}
    sensitive = re.compile(r"key|token|secret|password|authorization|cookie|credential|signature",
                           re.I)

    def collect(node, secret=False, headers=False):
        identity = (id(node), secret, headers)
        if identity in seen:
            return
        seen.add(identity)
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                name = key.value.lower().replace("_", "-")
                marked = (secret or name in _SECRET_FIELDS or
                          name in _SECRET_LIST_KEYS or
                          (headers and name not in safe_headers) or
                          bool(sensitive.search(name)) and name not in
                          {"support-prompt-cache-key", "api-key-entries", *_SECTION_KEYS})
                collect(value, marked, name == "headers")
        elif isinstance(node, yaml.SequenceNode):
            for value in node.value:
                collect(value, secret, headers)
        elif isinstance(node, yaml.ScalarNode):
            value = node.value
            # 只脱敏**字符串**标量（2026-09-12）
            # ------------------------------------
            # `sensitive` 按键名**含子串**判，于是 `max-retry-credentials: 12`、
            # `credential-concurrency.default: 4`、`credential-in-flight.max: 2`
            # 这三类纯整数配置项全被当成凭据 —— 生产配置里三处都命中，diff 里
            # 显示成 `12******`：操作员看不出真实值，而这几项正是全局调优要改的
            # 那几个键，看不见当前值就无法复核建议。
            #
            # 判据用 PyYAML 解析出来的 tag，而不是再往 `sensitive` 上贴例外：
            # 凭据一定是字符串，裸 int / bool / float / null 不可能是凭据。
            # 带引号的 `"12"` 仍然是 str tag，照样脱敏 —— 那种写法有可能
            # 真是个数字形态的 Key，不能因为「看着像数字」就放过。
            if secret and value and node.tag == "tag:yaml.org,2002:str":
                secrets.add(value)
                if value.lower().startswith(("bearer ", "basic ")):
                    secrets.add(value.split(" ", 1)[1])
                # 用「前 6 后 4」而不是整段 `***`（2026-09-12）
                # ------------------------------------------------
                # 结构化这一遍是好事（它能抓到行级匹配漏掉的嵌套字段），
                # 但把值整体换成 `***` 是降级：diff 的价值在于「写回前看清
                # 这个条目会变成什么样」，而 177 个条目全是 `***` 时，
                # 操作员分不清哪条是哪把 Key，也看不出某条有没有被改动。
                # 本模块 docstring 与 parse.mask_key 的口径都是
                # `sk-abc...wxyz` —— 既认得出是哪一把，又不泄露完整值。
                #
                # 保留原有的引号风格
                # ------------------------------------
                # 本模块的硬要求是「只动值，不动结构」。原来无条件写成带引号的
                # 形态，于是 `api-key: sk-bare-1234`（裸值）在 diff 里变成
                # `api-key: "sk-bar...7890"` —— 引号是凭空多出来的差异，
                # 让人分不清「这行真的变了」还是「只是被脱敏了」。
                # node.style 是 PyYAML 记下来的原始风格：None 表示裸标量。
                masked = _short_mask(value)
                edits.append((node.start_mark.index, node.end_mark.index,
                              masked if node.style is None
                              else _yaml_str(masked)))
            if "://" in value:
                try:
                    url = urlsplit(value)
                    # 只收 password，不收 username（2026-09-12）
                    # ----------------------------------------------
                    # 用户名不是凭据，而排障要靠它认出这是哪一条代理链路。
                    # tests/test_server.py 写明的契约就是
                    # `http://user:***@mihomo:7890` —— 主机、端口、用户名
                    # 都留着，只抹密码段。
                    if url.password:
                        secrets.update((url.password, unquote(url.password)))
                    for key, item in parse_qsl(url.query, keep_blank_values=True):
                        if item and sensitive.search(key):
                            secrets.update((item, quote(item, safe=""), quote(item, safe="").replace("%20", "+")))
                except ValueError:
                    pass

    try:
        root = yaml.compose(text)
        if root is not None:
            collect(root)
    except Exception:
        # An invalid fragment cannot be proved safe for a preview.
        return "[YAML preview withheld: invalid structure]"
    for start, end, replacement in sorted(set(edits), reverse=True):
        text = text[:start] + replacement + text[end:]
    variants = set(secrets)
    for secret in secrets:
        variants.update((_yaml_str(secret)[1:-1], secret.replace("'", "''"),
                         json.dumps(secret, ensure_ascii=True)[1:-1]))
    for secret in sorted(variants, key=len, reverse=True):
        # 残留清理：上面按节点改过的位置已经是脱敏形态，这一遍兜住
        # 「同一个凭据还出现在别处」（URL 里、自由文本里）的情形。
        # 替换成同一种脱敏形态而不是 `***`，理由见 _short_mask。
        if secret and secret != "***" and secret != _short_mask(secret):
            text = text.replace(secret, _short_mask(secret))
    return text


def redact_yaml_secrets(text: str) -> str:
    """把 YAML 文本里的凭据值换成脱敏形态。**只动值，不动结构**。

    为什么需要（2026-09-05 修的 P1）
    ----------------------------
    全量重探那条路的 `/api/plan` 响应里，diff 就是**重建后的整个文件** ——
    不是增量片段。于是 121 个条目的 `api-key:` 明文、`remote-management`
    的 `secret-key`、以及全部注释一起进了浏览器：

        生产 config.yaml：177 行 api-key 明文 + 1 行 secret-key，共 349KB

    而 `server.py` 开头第 15 行自述「完整 key 只在内存里，不落日志、
    **不进 JSON 响应**（一律 masked）」—— 那条纪律在这条路径上没有兑现。

    后果：凭据落进浏览器内存与 DOM；界面的「复制」按钮把整份配置连 177 个
    Key 一起写进系统剪贴板；任何一处 XSS 或恶意浏览器扩展就是一次全量泄露。

    为什么按行脱敏而不是「不给全文」
    -----------------------------
    全量重探的价值就在于「写回前看清整个文件会变成什么样」。给一段掐头去尾的
    片段等于把这个功能废掉。按行脱敏保留了行号、缩进、注释、以及其余全部字段，
    diff 仍然完全可读，只有凭据值变成 `sk-abc...wxyz` 这种形态。

    落盘走的是服务端内存里的原文（`entry["preview"]`），不受本函数影响 ——
    脱敏只作用于**发给客户端的那一份**。

    保留原有的引号风格与行尾注释：改动那些会在 diff 里产生无意义的差异，
    让人分不清「这行真的变了」还是「只是被脱敏了」。
    """
    text = _scrub_structural_secrets(text)
    out: list[str] = []
    in_secret_list = False
    list_indent = -1

    for line in text.splitlines(keepends=True):
        nl = ""
        body = line
        if body.endswith("\r\n"):
            nl, body = "\r\n", body[:-2]
        elif body.endswith("\n"):
            nl, body = "\n", body[:-1]

        stripped = body.strip()

        # 注释行与空行原样保留 —— 注释里不该有凭据（真有的话那是配置本身的
        # 问题，且注释是人写的，脱敏它会破坏排障记录）
        if not stripped or stripped.startswith("#"):
            out.append(body + nl)
            continue

        # 正在 `api-keys:` 这种裸标量列表里
        if in_secret_list:
            m = _LIST_ITEM.match(body)
            if m and len(m.group(1)) > list_indent:
                val, comment = _split_comment(m.group(2))
                out.append(f"{m.group(1)}- {_mask_scalar(val)}{comment}" + nl)
                continue
            # 缩进回到列表键那一级或更外 —— 列表结束
            cur = len(body) - len(body.lstrip())
            if cur <= list_indent:
                in_secret_list = False

        m = _KV_LINE.match(body)
        if not m:
            out.append(body + nl)
            continue

        prefix, key, sep, rest = m.groups()
        bare = key.strip().strip('"').strip("'").lower()

        if bare in _SECRET_LIST_KEYS and not rest.strip():
            in_secret_list = True
            list_indent = len(prefix) - len(prefix.rstrip())
            list_indent = len(prefix.replace("- ", "  ")) if "-" in prefix \
                else len(prefix)
            out.append(body + nl)
            continue

        if bare in _SECRET_FIELDS:
            val, comment = _split_comment(rest)
            out.append(f"{prefix}{key}{sep}{_mask_scalar(val)}{comment}" + nl)
            continue

        if bare in _URL_FIELDS and "@" in rest:
            val, comment = _split_comment(rest)
            new_val = _URL_USERINFO.sub(
                lambda m: f"{m.group(1)}{m.group(2)}:***@", val)
            out.append(f"{prefix}{key}{sep}{new_val}{comment}" + nl)
            continue

        out.append(body + nl)

    return "".join(out)


def _split_comment(rest: str) -> tuple[str, str]:
    """把 `"值" # 注释` 拆成 (值, " # 注释")。引号内的 # 不算注释起点。"""
    q = ""
    for i, ch in enumerate(rest):
        if q:
            if ch == q:
                q = ""
            continue
        if ch in "\"'":
            q = ch
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t"):
            return rest[:i].rstrip(), " " + rest[i:]
    return rest.rstrip(), ""


def _mask_scalar(val: str) -> str:
    """脱敏一个标量值，保留它原来的引号风格。

    与 `parse.mask_key` 同一口径（前 6 后 4），但这里要处理引号 ——
    改动引号风格会在 diff 里制造无意义差异。
    """
    from .parse import mask_key

    raw = val.strip()
    if not raw:
        return val
    quote = ""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        quote, raw = raw[0], raw[1:-1]
    if not raw:
        return val
    masked = mask_key(raw)
    return f"{quote}{masked}{quote}" if quote else masked


def merge_entry_headers(old: dict[str, str] | None,
                        probed: dict[str, str] | None) -> dict[str, str]:
    """把探测得出的 headers 合并进原条目的 headers。原值为底，探测值覆盖同名键。

    为什么是**合并**而不是「探测有值就整份用探测的」（2026-09-04）
    -------------------------------------------------------
    画像梯能产出的 `anthropic-beta` 是写死的常量清单，里面**永远没有**两项：

      · `oauth-2025-04-20`      —— profiles.py 有意去掉：api-key 探测不该
                                   声称走 oauth
      · `context-1m-2025-08-07` —— 只由 betas.py 在站方正文点名时才补

    整份替换会把原条目里手工配的能力 beta 静默抹掉。实测 Desktop 版那份配置：
    含这两个 beta 的条目 33 个，其中 alfa.example 的 claude 条目 headers
    **只有** `anthropic-beta: context-1m-2025-08-07`、没有 UA —— baseline 一通过
    `need_ua=False`，`sp.headers` 是空 dict，那个站的 1m 上下文直接被关掉。

    合并方向：原值在下、探测值在上。探测值是本次实测出来的最省必需集，同名键
    以它为准；原值里探测没提到的键（能力 beta、站方特供头）保留。

    `anthropic-beta` 特殊 —— 它是逗号分隔的**集合**，同名覆盖会丢项，所以走
    `betas.merge` 保序去重合并。头名大小写不敏感（HTTP 语义），所以匹配时
    统一小写比较，但**保留原条目的大小写写法**：改大小写会在 diff 里多出
    无意义改动。

    抽成独立函数而不是内联在 server 的循环里（2026-09-04）：内联时只能靠源码
    结构断言「这一行在不在」，而那挡不住「赋的是空」—— 撤销实验里把
    `old = hdrs.get(...)` 改成 `old = None`，1198 项测试全绿。
    """
    if not old:
        return dict(probed or {})
    merged = dict(old)
    if not probed:
        return merged
    lower = {k.lower(): k for k in merged}
    for hk, hv in probed.items():
        exist = lower.get(hk.lower())
        if exist is not None and hk.lower() == "anthropic-beta" and merged[exist]:
            merged[exist] = betas.merge(
                merged[exist],
                [x.strip() for x in str(hv).split(",") if x.strip()])
        elif exist is not None:
            merged[exist] = hv
        else:
            merged[hk] = hv
    return merged


def _toggle_lines(sp: SectionPlan, field: str) -> list[str]:
    """段专属能力开关要写的行。三态：只有 True 才写。

    值的优先级（与 max-context-length 同一套）：
      ① 本次实测（sp.websockets / sp.prompt_cache_key）
      ② 原条目的值（sp.prior_toggles）—— 关掉能力探测或该段本次未跑时兜住
      ③ 都没有就不写 —— CPA 的零值就是关闭（`websockets bool` 无指针，
         config_types.go:486），所以「不写」与「写 false」运行时等价

    为什么 False 也不写：写 `websockets: false` 与不写效果相同，但会在 diff 里
    多出一行、且让「这个站被测过且不支持」与「用户显式关掉」混在一起。行尾
    注释里说明实测结论，字段本身省掉。

    为什么 ① 覆盖 ②：实测是本轮的新证据。这里有一个方向性的取舍 ——
    实测 False 会**关掉**原来开着的开关。那是对的：`websockets: true` 而站方
    不支持时 CPA 不会回落 HTTP（codex_websockets_executor.go:71-77），
    那个凭据的 WS 请求全废；把它关掉是修复而不是破坏。
    但 None（未探测）绝不覆盖原值 —— 那才是「没有证据就不动」。
    """
    out: list[str] = []
    for name, probed, note in (
        ("websockets", sp.websockets, sp.websockets_note),
        ("support-prompt-cache-key", sp.prompt_cache_key, sp.prompt_cache_note),
    ):
        if probed is True:
            tail = f"   # {note}" if note else ""
            out.append(f"{field}{name}: true{tail}")
        elif probed is None and sp.prior_toggles.get(name):
            out.append(f"{field}{name}: true   # 原值搬运（本次未探测此开关）")
        # probed is False：实测不支持 —— 不写。原值即使为 true 也不搬，
        # 那正是这次探测要修掉的错配置。
    return out


def render_entry(sp: SectionPlan, dash: str, field: str, stamp: str,
                 extra_keys: list[str] | None = None,
                 key_lines: dict[str, list[str]] | None = None,
                 key_plans: dict[str, SectionPlan] | None = None,
                 original_entry: dict | None = None) -> list[str]:
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

    实测 kilo.example 5 把、hotel.example 3 把带 per-key `proxy-url`，而同站
    claude 段那几把故意不带。拿 head 那把的值套给全组会多一跳（不会失败，所以
    validate 与写后验证都发现不了）；weight 更糟，0 会把那把 Key 整个逐出调度池。
    见 compat_key_blocks。
    """
    sp = copy.deepcopy(sp)
    identity_keys = {"cloak", "fingerprint-profile",
                     "rebuild-mid-system-message", "disable-cooling"}
    carry = _load_yaml(textwrap.dedent("\n".join(sp.carry_lines))) or {}
    identity = {k: carry.pop(k) for k in identity_keys if k in carry}
    sp.carry_lines = _field_fragments(sp.carry_lines, identity_keys, field)
    if sp.cloak_mode:
        cloak = dict(identity.get("cloak") or {})
        cloak["mode"] = sp.cloak_mode
        identity["cloak"] = cloak
    if sp.fingerprint_profile:
        identity["fingerprint-profile"] = sp.fingerprint_profile
    if sp.rebuild_mid_system is not None:
        identity["rebuild-mid-system-message"] = sp.rebuild_mid_system
    if sp.disable_cooling is not None:
        identity["disable-cooling"] = sp.disable_cooling
    out: list[str] = []
    note = f"# {stamp} 批量导入 · 得分 {sp.score} · {sp.priority_reason}"

    def model_lines(indent: str) -> list[str]:
        # P0 修复：空模型验证与应急处理（2026-09-12）
        # ---------------------------------------------------
        # 背景：95% 检测失败 → 空段 → sp.models=[] → 生成空 models 块
        #
        # 三层检查：
        # 1. sp.models 非空：正常渲染
        # 2. sp.highest_models 非空：应急回退（元数据字段，但好过空白）
        # 3. 两者都空：记录严重错误，返回空列表（由调用方决定是否写入）
        if not sp.models:
            logger.error(
                f"CRITICAL: 空模型渲染被阻止 - "
                f"section={sp.section}, base_url={sp.base_url}, "
                f"model_source={sp.model_source}, score={sp.score}")

            # 尝试使用 highest_models 作为应急回退
            if sp.highest_models:
                logger.warning(
                    f"  → 使用 highest_models 作为应急回退："
                    f"{len(sp.highest_models)} 个模型")
                models_to_render = sp.highest_models
            else:
                # 最后防线：使用硬编码回退
                from .model_catalog import FALLBACK_MODELS
                emergency_fallback = FALLBACK_MODELS.get(sp.section, [])

                if emergency_fallback:
                    logger.error(
                        f"  → highest_models 也空，使用硬编码回退："
                        f"{len(emergency_fallback)} 个模型")
                    models_to_render = list(emergency_fallback)
                else:
                    # 无任何回退可用，记录严重错误并返回空
                    logger.critical(
                        f"  → 所有回退均失败！将生成空 models 块。"
                        f"该条目可能无法在 CPA 中正常工作。")
                    return []  # 返回空列表，让调用方决定如何处理
        else:
            models_to_render = sp.models

        rows: list[str] = []
        for m in models_to_render:
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
            # alias：原值优先，没有原值才写空串。
            #
            # 写空串对生产配置里那 459 个条目是对的（它们 100% 就是 `""`），
            # 而 CPA 在 alias 为空时自动回落成 name
            # （buildConfiguredModelInfo，service_models.go:678-682），
            # 所以 `alias: ""` 与 `alias: <name>` 运行时等价。
            #
            # 但**非空且与 name 不同**的 alias 不等价 —— 那是段级兼容名
            # （README 明确推荐这个做法：保留原名轮询、用 alias 补兼容名）。
            # 原来这里写死空串、`existing_model_extras` 又把 alias 排除在外、
            # carry 跳过整个 models 块，于是它掉进第三个空档：整段重写后
            # `alias: "claude-opus-5"` 变成 `alias: ""`（2026-09-04 修）。
            #
            # 仍然**不**写成与 name 相同：那会让 459 行原本是 `""` 的行全被
            # 改掉，diff 里多出 459 处无意义改动，掩盖真正要看的 priority 变化。
            _al = (sp.prior_model_extras.get(m) or {}).get("alias")
            if _al is not None and str(_al) != "":
                rows.append(f'{indent}  alias: {_yaml_str(str(_al))}')
            else:
                rows.append(f'{indent}  alias: ""')
            # 每个模型自己的窗口值。三档，优先级递减：
            #   ① 本次实测的那一个（context_model）—— 最新的实测依据
            #   ② 原条目里这个模型自己的值（prior_context）—— 历史实测依据
            #   ③ 都没有就不写，CPA 回落内置目录值
            #
            # 为什么必须有 ②（2026-09-03 逐字段对账）：这个值在 models 块里，
            # extract_carry_lines 有意跳过那一块，而方案只带一个实测值 ——
            # 本次没探上下文时历史值全丢。实测生产配置 8 处，客户端会按 CPA
            # 内置目录的偏大值定压缩点，塞满才发现被上游截断。
            if sp.max_context_length and m == sp.context_model:
                # 行尾注明单位与来路：这个数是**折算成 token** 的实测容量
                # （探测发字符、CPA 读 token，见 pipeline._bisect 的单位一节）。
                # 原来只写「实测值」，而文件里同时存在旧的字符数值 ——
                # 读文件的人无从分辨哪一个是哪一种。
                rows.append(f"{indent}  max-context-length: {sp.max_context_length}"
                            f"   # 实测容量（token）")
            elif sp.prior_context.get(m):
                rows.append(f"{indent}  max-context-length: "
                            f"{sp.prior_context[m]}   # 原值搬运")
            # 白名单外的模型级字段（display-name / thinking / image /
            # force-mapping / is-compat / *-modalities）—— 原样搬回。
            # carry 跳过整个 models 块，这七个字段没有别人接。
            #
            # alias 已经在上面单独处理过（它在 extras 表里只为了被搬运，
            # 渲染位置必须紧跟 name，与现有文件的键序一致），这里跳过它，
            # 否则一个模型会写出两行 alias。
            for k, v in sorted((sp.prior_model_extras.get(m) or {}).items()):
                if k == "alias":
                    continue
                rows.extend(_yaml_field(f"{indent}  ", k, v))
        if original_entry and rows:
            rendered = _load_yaml(textwrap.dedent("\n".join(rows)))
            restored = []
            for model in rendered:
                aliases = [old for old in original_entry.get("models") or []
                           if old.get("name") == model["name"]]
                if not aliases:
                    restored.append(model)
                    continue
                for old in aliases:
                    merged = copy.deepcopy(old)
                    merged.update({k: v for k, v in model.items()
                                   if k not in sp.prior_model_extras.get(model["name"], {})})
                    if "alias" in old:
                        merged["alias"] = old["alias"]
                    restored.append(merged)
            import yaml
            rows = [indent + line for line in yaml.safe_dump(
                restored, allow_unicode=True, sort_keys=False).rstrip().splitlines()]
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
        # provider `name` 就是 CPA 的身份（provider_key 由它算），必须搬回
        # 原值 —— 用 host 现编会给 12 个 provider 全部改名，作废它们的冷却
        # 状态与能力缓存，并让本项目自己的短名→域名别名表失效。
        # 见 SectionPlan.provider_name。
        pname = sp.provider_name or sp.base_url.split("//")[-1].split("/")[0]
        out.append(f"{dash}- name: {_yaml_str(pname)}")
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
            # head 的出口。实测 kilo.example 5 把带 per-key 代理、claude 段那几把
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
                    out.append(f"{field}    weight: {w}{_weight_note(w)}")
        if sp.headers:
            out.append(f"{field}headers:")
            for k, v in sp.headers.items():
                # 头**名**也要过 _yaml_str（2026-09-04 自查）：名字来自
                # overrides.headers 与前端编辑器，两处都只 trim 不校验形态。
                # 实测 `a #c` 单头时本工具 validate 放过而 CPA 报
                # `cannot unmarshal !!str into map[string]string`；`null` 双侧
                # 「合法」但 CPA 读出的 Headers 为空 —— 那个头静默消失。
                out.append(f"{field}  {_yaml_header_name(k)}: {_yaml_str(v)}")
        out.extend(_toggle_lines(sp, field))
        out.extend(_dump_fields(identity, field))
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
        out.append(f"{field}weight: {sp.weight}{_weight_note(sp.weight)}")
    if sp.proxy_url:
        out.append(f"{field}proxy-url: {_yaml_str(sp.proxy_url)}")
    if sp.headers:
        out.append(f"{field}headers:")
        for k, v in sp.headers.items():
            # 键也走 _yaml_str —— 见 compat 分支同一处的说明。
            out.append(f"{field}  {_yaml_header_name(k)}: {_yaml_str(v)}")
    # 段专属能力开关（codex 的 websockets）。放在 headers 之后、carry 之前 ——
    # 与生产 config.yaml 的键序一致（实测两条 `websockets: true` 分别紧跟
    # base-url 与 headers）。
    out.extend(_toggle_lines(sp, field))
    # claude 段的请求体级身份（2026-09-11）
    # ------------------------------------
    # 画像梯的 cc-body-* 三档，门票在**请求体**里（metadata.user_id、
    # Claude Code 的 system 块），`headers:` 表达不了。CPA 侧对应的能力是
    # `cloak`（config_types.go:348）与 `fingerprint-profile`（:433）——
    # 让 CPA 自己去补那段身份，而不是我们把请求体塞进条目（条目不支持）。
    #
    # 只在实测确实需要时才写（值由 plan.build_plan 判定，取值从 CPA 源码
    # 解析、不写死）。不需要的站写上等于凭空改写它们的请求体。
    out.extend(_dump_fields({k: v for k, v in identity.items()
                             if k in ("cloak", "fingerprint-profile")}, field))
    # claude 段：对话中途的 system 消息要不要让 CPA 挪到顶层。
    # 只在**实测需要**时写 true（`_probe_mid_system` 的结论）——
    # False 表示「上游自己就收」，那时保持字段缺席即可，写一个 false 只是噪声。
    # Explicit False is measured evidence, not absence.
    if "rebuild-mid-system-message" in identity:
        out.extend(_dump_fields({"rebuild-mid-system-message":
                                 identity["rebuild-mid-system-message"]}, field))
    # 冷却策略。None = 跟随全局（CPAMP 界面默认，也是绝大多数条目的正确状态），
    # 此时**不写这一行** —— 无条件写值等于把全局可调的策略钉死在每条上。
    # 只有实测判为限流/限频的站才写 false（强制启用冷却），见 _cooling_override。
    if "disable-cooling" in identity:
        out.extend(_dump_fields({"disable-cooling": identity["disable-cooling"]}, field))
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
    cfg = _load_yaml(raw) or {}
    selected = [sp for p in plans for sp in p.sections.values() if sp.writable]
    requires_rebuild = any(_original_entry(cfg, sp) for sp in selected)
    requires_rebuild |= any(
        (span := _section_span(lines, sp.section)) is not None and
        _flow_section_span(lines, span[0]) is not None for sp in selected)
    if requires_rebuild:
        grouped = {}
        for p in plans:
            for sp in p.sections.values():
                key = (sp.base_url, sp.api_key)
                grouped.setdefault(key, ImportPlan(p.host, p.masked_key)).sections[sp.section] = sp
        text, _ = rebuild_config_full(cfg, grouped, lines, only_owned=False)
        return [Diff("config.yaml", 0, text.split("\n"), "", replace_all=True)]

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
    compat_groups: dict[tuple[str, str, str], list] = {}
    for plan in plans:
        sp = plan.sections.get("openai-compatibility")
        if sp is not None and sp.writable:
            compat_groups.setdefault((plan.host, sp.base_url, _compat_capability(sp)), []).append(sp)

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
        for (host, base, capability), group in compat_groups.items():
            head = copy.deepcopy(group[0])
            head.priority = max(g.priority for (h, b, c), members in compat_groups.items()
                                if h == host for g in members)
            keys = [g.api_key for g in group]

            # 该 base-url 已有 provider？追加 Key 进它的 api-key-entries，
            # 不新建条目 —— name 就是 CPA 的 provider 身份，重名会让冷却、
            # 模型能力、执行路由三处对同一个 Key 命中两套配置。
            found = find_compat_provider(lines, base)
            cfg = _load_yaml(raw) or {}
            providers = [r for r in cfg.get("openai-compatibility") or []
                         if _source_identity(r.get("base-url", "")) == _source_identity(base)]
            # 同一个 base-url 就并进去（2026-09-12）
            # ------------------------------------------
            # 中途加过一道「形态必须逐字段相同、priority 也必须相同」的闸，
            # 不满足就把 found 丢掉、另起一个带哈希后缀的 provider 名。
            # 那与本函数开头写明的规则相反，后果也正是那里警告的：
            # `name` 就是 CPA 的 provider 身份，同一个 base-url 出现两个
            # provider，会让冷却、模型能力、执行路由三处对同一把 Key 命中
            # 两套配置。
            #
            # 而 priority 本来就**不该**参与这个判断：用户第 4 条要求
            # 「所有相同网址上游 key 即使不同，优先级也要保持相同」——
            # 新算出来的档位与原值不同时，正确做法是把这一组对齐到同一档
            # （`head.priority` 已经取了同 host 的最大值），不是因为不同就
            # 另起一个 provider 把同一个站拆成两半。
            #
            # 仍然保留「能力不同就分开」：`capability` 已经是 compat_groups
            # 的分组键之一（不同能力的 Key 本来就落在不同 group），
            # 下面 `not found` 时的哈希后缀只用于**确实没有**现成 provider
            # 可并、且同 host 有多个能力分组的情形。
            if found:
                head.priority = max(
                    head.priority,
                    *(int(r.get("priority") or 0) for r in providers
                      if r.get("name") == found["name"]),
                )
            # New incompatible credentials get their own provider identity.
            if not found and (providers or sum(1 for h, b, c in compat_groups
                                               if h == host) > 1):
                name = head.provider_name or host
                head.provider_name = name + "-" + hashlib.sha256(
                    (base + "\0" + capability).encode()).hexdigest()[:10]
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
                    if mine is not None and mine.weight is not None:
                        add_lines.append(f"{ki}    weight: {mine.weight}")
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


def _tuning_key_line(lines: list[str], path: tuple[str, ...]) -> int:
    """找到 `path` 指向的那一行，返回 0-based 行号；找不到返回 -1。

    只支持一层与两层键（`debug`、`codex.stream-bootstrap-buffering`）——
    全局调优项都在这个深度，再深的路径不在本函数职责内，返回 -1 交给调用方
    报「改不动」而不是猜。

    为什么按行找而不是改 YAML 对象再 dump（与本模块其余部分同一条原则）：
    dump 会重排键序、丢掉全部注释。而这个文件的注释是**决策记录**——
    `max-retry-credentials` 那一项的注释里记着四次调值的实测依据与行号引用，
    丢掉它等于把「为什么是这个数」永久删除。
    """
    if not path or len(path) > 2:
        return -1
    if len(path) == 1:
        want = path[0]
        for i, line in enumerate(lines):
            m = re.match(r"^([A-Za-z0-9_.\-]+):(\s|$)", line)
            if m and m.group(1) == want:
                return i
        return -1
    parent, child = path
    depth = None
    for i, line in enumerate(lines):
        if depth is None:
            m = re.match(r"^([A-Za-z0-9_.\-]+):(\s|$)", line)
            if m and m.group(1) == parent:
                depth = 0
            continue
        # 进了父块：顶层键出现就说明父块结束
        if re.match(r"^[A-Za-z0-9_.\-]+:(\s|$)", line):
            return -1
        m = re.match(r"^(\s+)([A-Za-z0-9_.\-]+):(\s|$)", line)
        if m and m.group(2) == child:
            return i
    return -1


def _tuning_render(want) -> str:
    """把建议值渲染成 YAML 标量。布尔要小写，与 CPA 的 yaml.v3 一致。"""
    if want is True:
        return "true"
    if want is False:
        return "false"
    if isinstance(want, int):
        return str(want)
    return _yaml_str(str(want))


def _tuning_edit(lines: list[str], path: tuple[str, ...], want
                 ) -> tuple[bool, str]:
    """就地把 `path` 那一行的值改成 `want`。返回 (改成了吗, 说明)。

    行尾注释逐字保留 —— 见 `_tuning_key_line` 的说明。
    """
    idx = _tuning_key_line(lines, path)
    label = ".".join(path)
    if idx < 0:
        return False, f"{label}：配置里找不到这个键（或它嵌得比两层更深）"
    line = lines[idx]
    m = re.match(r"^(\s*)([A-Za-z0-9_.\-]+):(\s*)(.*)$", line)
    if not m:
        return False, f"{label}：这一行的写法认不出来，没有改"
    indent, key, gap, rest = m.groups()
    value, comment = _split_comment(rest)
    if not value.strip():
        # 键在、值是个块（下面还有缩进的子键）—— 不能当标量改
        return False, f"{label}：这一项不是标量（值在下面的块里），没有改"
    lines[idx] = f"{indent}{key}:{gap or ' '}{_tuning_render(want)}{comment}"
    return True, ""


def global_tuning_diffs(raw: str, advices) -> tuple[list[Diff], list[str]]:
    """把全局调优建议做成一条整文件 diff。返回 (diffs, 改不动的说明)。

    为什么是 `replace_all` 而不是逐行插入：这些键散在文件各处（`debug` 在
    第 74 行、`codex.stream-bootstrap-buffering` 在 486），而 `Diff.insert_at`
    的语义是「在此行后**插入**」—— 改现有值不是插入。整文件替换让
    `apply_diffs` 走已有的那条路，写盘、备份、写后回读、CPA 重载全部复用
    既有实现，不新增一条写盘路径（那是最容易出事的地方）。

    只改**值**：键序、注释、空行、缩进全部逐字保留。
    """
    lines = raw.split("\n")
    problems: list[str] = []
    touched = 0
    for adv in advices:
        if not getattr(adv, "changed", False):
            continue
        ok, why = _tuning_edit(lines, tuple(adv.path), adv.want)
        if ok:
            touched += 1
        else:
            problems.append(why)
    if not touched:
        return [], problems
    text = "\n".join(lines)
    ok, msg = validate(text)
    if not ok:
        return [], problems + [f"改完后 YAML 校验不通过，已放弃：{msg}"]
    return [Diff("全局调优", 0, text.split("\n"), "", replace_all=True)], problems


def apply_diffs(raw: str, diffs: list[Diff]) -> str:
    """把 diff 应用到原文。从后往前插，避免行号偏移。

    空段头改写（rewrite）先做：它只改一行、不动行数，所以和插入的行号
    互不影响。同一段有多个 diff 时改写内容相同，重复执行是幂等的。
    """
    replacements = [d for d in diffs if d.replace_all]
    if replacements:
        if len(diffs) != 1:
            raise ValueError("Full replacement cannot be mixed with line insertions")
        return "\n".join(replacements[0].lines)
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
        return False, "未安装 PyYAML，不能验证配置"
    try:
        cfg = _load_yaml(text)
    except Exception:
        return False, "YAML 语法错误或存在重复键（已隐藏原始内容）"
    if not isinstance(cfg, dict):
        return False, "顶层不是映射，config.yaml 结构异常"
    if any(cfg.get(s) is not None and not isinstance(cfg[s], list)
           for s in _SECTION_KEYS):
        return False, "提供商段必须是列表"
    n = sum(len(cfg.get(s) or []) for s in
            ("gemini-api-key", "codex-api-key", "claude-api-key", "openai-compatibility"))
    return True, f"YAML OK · {len(cfg)} 个顶层键 · 四段共 {n} 条目"


_LOCAL_WRITE_LOCK = threading.RLock()


def config_version(path: str) -> str:
    """Opaque version for caller snapshots; contains no configuration values."""
    with open(path, "rb") as stream:
        data = stream.read()
        stat = os.fstat(stream.fileno())
    return hashlib.sha256(
        f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_ctime_ns}:".encode()
        + data).hexdigest()


class WritebackError(OSError):
    def __init__(self, message: str, *, backup_path=None, expected_version=None,
                 current_version=None, restored=False):
        super().__init__(message)
        self.backup_path = backup_path
        self.expected_version = expected_version
        self.current_version = current_version
        self.restored = restored


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        count = os.write(fd, data[offset:])
        if count <= 0:
            raise OSError("Short write")
        offset += count


def write_local(path: str, text: str, *, backup_dir: str | None = None,
                expected_version: str | None = None) -> str:
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

    expected_version 应取自预览对应的 config_version 快照；冲突时不写。
    失败通过 WritebackError.backup_path/restored 报告恢复上下文。
    本进程锁不协调外部写入；调用方仍须串行化整个本地与远端事务。
    """
    # This lock coordinates only this Python process. Callers must serialize
    # the entire local/remote transaction and exclude external CPA/CPAMP writes.
    # Version checks detect observed conflicts, not an atomic cross-process CAS.
    with _LOCAL_WRITE_LOCK:
        before = config_version(path)
        if expected_version is not None and before != expected_version:
            raise WritebackError("Config version conflict", expected_version=expected_version,
                                 current_version=before)
        bak = backup(path, backup_dir=backup_dir)
        with open(bak, "rb") as saved:
            original = saved.read()
        if config_version(path) != before:
            raise WritebackError("Config changed during backup", backup_path=bak,
                                 expected_version=before, current_version=config_version(path))

        # 就地覆写。inode 不变是硬要求，不是优化偏好。
        fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        touched = False
        data = text.encode("utf-8")
        try:
            if (os.fstat(fd).st_ino != os.stat(path).st_ino or
                    config_version(path) != before):
                raise WritebackError("Config changed before write")
            os.ftruncate(fd, 0)
            touched = True
            _write_all(fd, data)
            os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            if os.read(fd, len(data) + 1) != data:
                raise OSError("Config readback mismatch")
        except Exception:
            restored = False
            failed_version = None
            try:
                failed_version = config_version(path)
                os.lseek(fd, 0, os.SEEK_SET)
                partial = os.read(fd, max(len(data), len(original)) + 1)
                same_inode = os.fstat(fd).st_ino == os.stat(path).st_ino
                # Only our expected prefix is eligible, and recheck the version
                # immediately before restoration. Never restore over other content.
                if (touched and same_inode and data.startswith(partial) and
                        config_version(path) == failed_version):
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.ftruncate(fd, 0)
                    _write_all(fd, original)
                    os.fsync(fd)
                    os.lseek(fd, 0, os.SEEK_SET)
                    restored = os.read(fd, len(original) + 1) == original
            except Exception:
                pass
            raise WritebackError(
                "Config write failed; durable backup available; "
                + ("original restored" if restored else "manual recovery required"),
                backup_path=bak, expected_version=before,
                current_version=failed_version, restored=restored) from None
        finally:
            os.close(fd)
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
    校验用 bcrypt.CompareHashAndPassword（handler.go:389）。
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
            raw_body = e.read().decode("utf-8", "replace")
            # Classify internally, never echo server-provided configuration/key snippets.
            body = ("cloudflare" if "cloudflare" in raw_body.lower()
                    or "cf-ray" in raw_body.lower() else "[response body withheld]")
        except Exception:
            pass
        # 422 才是语义校验失败的真实状态码（config_basic.go:153 用的是
        # StatusUnprocessableEntity）；400 只用于 body 读不出来（:121）与
        # YAML 语法错（:126）。这三个码都属「CPA 未落盘」的安全失败。
        #
        # **500 不在这一类**（2026-09-05 订正）：原来这里把 `:133` 也算进 400，
        # 而那一行是 `StatusInternalServerError`（write_failed，创建 temp 文件
        # 失败）。代码分支本身没错（下面只判 400/422），但那句话会误导下一轮
        # 判断「500 是不是也安全」—— 而 `:158` 之后的 500 是**落盘之后**
        # LoadConfig 失败，且不回滚。那时磁盘上已经是新内容了。
        if e.code in (400, 422):
            return False, (f"{e.code} 校验未通过，CPA 未落盘（这是安全失败）：{body}")
        if e.code == 401:
            return False, ("401 鉴权失败。PUT 端点用 bcrypt 比对**原始密码**"
                           "（handler.go:389），传 config.yaml 里那串 $2a$ 哈希"
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
    except Exception:
        return False, (
            "连接失败（已隐藏异常内容）。若请求已发出，请核对 VPS 上 config.yaml 完整性"
        )

    # 读回校验。200 只说明 CPA 接受并落盘了，不说明它内存里那份是新的 ——
    # GET /config.yaml 走 os.ReadFile 直读磁盘（config_basic.go:174-189），
    # 所以这一步验证的是「CPA 容器看到的文件内容」==「我们要写的内容」。
    # 这恰好能抓住 inode 分叉：如果挂载点还指着旧 inode，读回的就是旧内容。
    ok_rb, msg_rb = _readback_check(base, mgmt_key, text, timeout=timeout)
    if not ok_rb:
        return False, f"PUT {status} 成功，但读回校验失败：{msg_rb}"
    return True, f"PUT {status} + 读回一致（{msg_rb}）；运行时路由仍需独立验证"


def _readback_check(base: str, mgmt_key: str, want: str, *,
                    timeout: int = 30) -> tuple[bool, str]:
    """GET /v0/management/config.yaml 并与期望内容比对行数与四段条目数。

    不做逐字节比对 —— CPA 在 PUT 时会跑 NormalizeCommentIndentation
    （config_basic.go:102），注释缩进可能被规整，字节流本就允许不同。
    比对「行数 + 四段条目数」足以确认是同一份内容，且能抓住
    「读回的是旧文件」这个我们真正担心的情形。

    上述计数结论已修正：当前按类型敏感的 YAML 语义比较，数量只作诊断；
    文件一致不代表 watcher 已完成运行时路由注册。
    """
    url = base.rstrip("/") + "/v0/management/config.yaml"
    req = urllib.request.Request(
        url, method="GET", headers={"Authorization": f"Bearer {mgmt_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            got = resp.read().decode("utf-8", "replace")
    except Exception:
        return False, "GET 失败（已隐藏异常内容）"

    try:
        expected, actual = _load_yaml(want), _load_yaml(got)
    except Exception:
        return False, "读回 YAML 无效或存在重复键（已隐藏内容）"
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return False, "读回配置顶层必须是映射"
    same = _semantic_equal(expected, actual)

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
    if not same and want_n != got_n:
        detail = ", ".join(f"{k}: 期望 {want_n[k]} 实得 {got_n[k]}"
                           for k in want_n if want_n[k] != got_n[k])
        return False, (f"CPA 读回的条目数不符（{detail}）。"
                       "最可能的原因是 config.yaml 的 inode 被换过 —— "
                       "单文件 bind mount 在容器启动时把 inode 定死了，"
                       "容器仍在读旧文件。需要 docker restart cli-proxy-api")
    if not same:
        return False, "CPA 读回配置语义不符；请核对配置版本与挂载状态"
    total = sum(len(expected.get(section) or []) for section in _SECTION_KEYS)
    return True, f"配置语义一致；四段共 {total} 条目；非运行时路由证明"


def _valid_verification_body(body: str, section: str = "") -> bool:
    """Shared error/HTML semantics plus a completed inference-result requirement."""
    from .classify import has_error_envelope, looks_like_html
    if not body.strip() or looks_like_html(body) or has_error_envelope(body):
        return False

    def output(obj):
        if not isinstance(obj, dict) or has_error_envelope(json.dumps(obj)):
            return False
        if obj.get("status") in ("failed", "incomplete", "cancelled"):
            return False
        def parts(items):
            if not isinstance(items, list):
                return False
            return any(isinstance(p, dict) and
                       (isinstance(p.get("text"), str) and bool(p["text"]) or
                        p.get("type") in ("tool_use", "function_call") and bool(p.get("name")) or
                        isinstance(p.get("functionCall"), dict) and bool(p["functionCall"].get("name")) or
                        parts(p.get("content"))) for p in items)
        return bool(parts(obj.get("content")) or parts(obj.get("output")) or obj.get("output_text")
                    or any(c.get("message", {}).get("content") or
                           c.get("message", {}).get("tool_calls")
                           for c in obj.get("choices", []) if isinstance(c, dict))
                    or any(parts(c.get("content", {}).get("parts"))
                           for c in obj.get("candidates", []) if isinstance(c, dict)))

    try:
        return output(json.loads(body))
    except (ValueError, TypeError, AttributeError):
        pass
    if not re.search(r"^data:", body, re.M):
        return False
    terminal = False
    content = False
    response_completed = False
    for frame in re.split(r"\r?\n\r?\n", body):
        event = ""
        parts = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                parts.append(line[5:].lstrip())
        if event in ("error", "response.failed", "response.incomplete"):
            return False
        if not parts:
            continue
        data = "\n".join(parts)
        if data == "[DONE]":
            terminal = True
            continue
        try:
            obj = json.loads(data)
        except ValueError:
            return False
        if not isinstance(obj, dict) or has_error_envelope(data):
            return False
        kind = obj.get("type", event)
        if kind in ("error", "response.failed", "response.incomplete"):
            return False
        if kind == "response.completed":
            response = obj.get("response", {})
            if not output(response):
                return False
            terminal = content = True
            response_completed = True
        elif kind == "message_stop":
            terminal = True
        elif kind in ("content_block_delta", "response.output_text.delta"):
            delta = obj.get("delta")
            content |= bool(delta if isinstance(delta, str) else
                            isinstance(delta, dict) and (delta.get("text") or delta.get("partial_json")))
        for choice in obj.get("choices", []):
            if isinstance(choice, dict):
                delta = choice.get("delta") or {}
                content |= bool(delta.get("content") or delta.get("tool_calls"))
    return terminal and content and (section != "codex-api-key" or response_completed)


def verify_upstream(
    cpa_base: str,
    client_key: str,
    section: str,
    model: str,
    *,
    timeout: int = 120,
    proxy: str | None = None,
    scope: dict | None = None,
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

    返回仍为 (bool, str)。可选 scope 字典报告 verification_scope=gateway、
    target_verified=False；本函数没有站点或目标凭据绑定能力。
    """
    from .fingerprint import backend_of, model_matches, resp_id, resp_model
    from .request import probe_text_for
    if scope is not None:
        scope.update(verification_scope="gateway", target_verified=False)

    # 端到端验证的请求最终仍会落到**上游站**（CPA 只是转发），所以同样
    # 受站方反测活的影响。按 (客户端 Key, 模型) 派生而不是用全局唯一那
    # 一句：一次写回要验多个段多个模型，全用同一句话会在站方日志里形成
    # 一串完全相同的请求。派生保持可复现 —— 同一次验证重跑结果一致。
    PROBE_TEXT = probe_text_for(f"{client_key}|{model}")

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

    try:
        resp = _client.send(
            url,
            headers=headers,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            proxy=proxy,
            timeout=timeout,
        )
    except Exception:
        return False, "gateway · 请求失败（已隐藏异常内容）"

    if resp.status != "200":
        from .classify import classify

        cat, _why = classify(resp.status, resp.body)
        return False, f"gateway · 请求失败 · {cat}（已隐藏响应内容）"

    if not _valid_verification_body(resp.body, section):
        return False, "gateway · HTTP 200 但不是完整有效的推理结果"

    actual = resp_model(resp.body)
    rid = resp_id(resp.body)
    backend = backend_of(rid)

    if not model_matches(model, actual):
        # 经 CPA 换模 —— 直连可能是好的，但客户端拿不到要的模型
        #
        # 2026-09-12：措辞退回「换模」并带上两个模型名。中途改成
        # 「响应模型不匹配（已隐藏响应内容）」有两处不好：
        #   · 「换模」是本项目上下文里的固定术语，界面、注释、分类器
        #     （classify 的静默换模一类）全用它，换掉就对不上；
        #   · 模型名不是敏感信息 —— 它本来就要被写进 config.yaml、
        #     显示在界面上。隐掉它让这条结论没法排查：用户只知道
        #     「不匹配」，不知道站方到底给了什么。
        # 隐藏的仍然是响应正文，两个模型名照常说。
        return False, (
            f"gateway · 200 但换模：请求 {model}，返回 {actual}"
            f"（后端形态 {backend}，响应正文已隐藏）。"
            "直连正常不代表接进 CPA 正常"
        )

    return True, f"gateway · 200 · 后端 {backend}；未证明目标站点或凭据"


def _orphan_entry_lines(lines: list[str], span: tuple[int, int],
                        section: str,
                        planned: set[tuple[str, str]],
                        host_tier: dict[str, tuple[int, str]] | None = None,
                        realigned: list[str] | None = None,
                        field_indent: int | None = None,
                        skipped: list[str] | None = None) -> list[str]:
    """段内**未被本次方案覆盖**的条目原文行（含它们自己的注释）。

    整段重写只写 planned 里的凭据，这个函数把其余条目原样捞出来接在后面。
    见 render_section 里 keep_unplanned 那一段的说明。

    识别条目边界靠「该段条目级 dash 的缩进」—— 嵌套结构里的
    `- status: 403` 也是 dash 行，按「有没有 dash」切会把一个条目切成好几段。
    与 extract_carry_lines 同一套判据。

    compat 段不走这里：它的结构是 provider 级 + api-key-entries，一个条目
    含多个 Key，「未覆盖」要在 Key 粒度判断，语义与前三段不同。

    host_tier：{host: (本次该站的新 priority, 行尾注释)}。给了它就把留守条目的
    `priority:` 行对齐到同站的新值 —— 见 _realign_priority 的说明。不给则原样搬。
    field_indent 是该段条目的字段缩进（`_detect_indent` 的第二个返回值），用来
    区分条目级 `priority:` 与嵌套结构里的同名键。skipped 收「找到条目但没能改」
    的原因（值不是裸整数等）—— 静默跳过等于让用户以为拆档修好了。
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
        if host_tier and h in host_tier:
            new_pri, note = host_tier[h]
            blk, changed, odd = _realign_priority(blk, new_pri, note,
                                                  indent=field_indent)
            if changed and realigned is not None and h not in realigned:
                realigned.append(h)
            if odd and skipped is not None:
                skipped.append(f"{h}（{odd}）")
        out.extend(blk)
    return out


# 条目级 `priority:` 行。值必须是裸整数，后面可带行尾注释。
#
# 缩进要**由调用方限定**（`_realign_priority` 的 indent 参数）—— 不限定就会
# 命中嵌套结构里的同名键。真实 config.yaml 里 `plugins.configs.example.priority`
# 缩进 6、条目级是 4；而字段顺序不保证（手工编辑过的条目可能把嵌套块写在
# 条目级 priority 之前），「命中第一条就停」那时会改错那一行。
_PRI_LINE = re.compile(r"^(\s*)priority\s*:\s*(-?\d+)\s*(#.*)?$")

# 值不是裸整数的写法。这些形态本工具**不改**，但必须报出来 —— 静默跳过等于
# 「用户以为拆档修好了，其实没修」。CPA 侧照样能解析它们
# （yaml.Unmarshal 认 `"900"` 与 `!!int 900`），所以它们是合法的既有写法。
_PRI_ODD = re.compile(r"^\s*priority\s*:\s*(?!-?\d+\s*(?:#|$))\S")


def _realign_priority(block: list[str], new_pri: int, note: str,
                      indent: int | None = None) -> tuple[list[str], bool, str]:
    """把留守条目的 `priority:` 对齐到同站本次的新值。

    为什么必须做（2026-09-04 现场截图）
    ------------------------------
    `assign_priorities` 保证「同站所有 Key 同档」，但那只作用于**进了本批方案**
    的段。留守条目（用户没勾、判不可写、探测异常）由 `_orphan_entry_lines`
    原样搬回来 —— 于是同一个站的 5 把 Key 拿新值、另外 9 把留在旧值上：

        kilo.example  claude  3 把 → 164   +  2 把留在 372
        tango   claude  9 把 → 167   +  5 把留在 371

    这与 CPA 的调度语义直接冲突：`priority` 决定「哪一层先被尝试」，
    层级隔离下只取最高那一桶（selector.go:527-553 的
    availableAuthsFromPriorityBuckets 只收 bestPriority；
    scheduler.go:1229-1231 的 priorityOrder 降序）。同站多 Key 指向同一个上游、
    能力完全相同，被拆成两层就把「多 Key 并行轮询」变成了「主备切换」——
    留在高档的那几把先被打光配额，低档那批只在它们全部不可用时才轮到。

    为什么对齐而不是「让留守条目决定新值」：新值来自本轮定档的完整安全推导
    （suggest_priority 的三条硬约束 + 全段单调递减），旧值是上一轮的产物；
    把新值抬回旧值会越过本轮算出的上限，可能劫持顶层。

    只改**条目级**的那一行，靠 `indent` 限定（2026-09-04 自查修正）
    ------------------------------------------------------------
    第一版只认「第一条 `priority:` 行」，不看缩进。真实 config.yaml 里
    `plugins.configs.example.priority: 1` 缩进 6、条目级是 4；而**字段顺序不保证**
    —— 手工编辑过的条目完全可能把 `request-scoped-errors:` 这类嵌套块写在条目级
    `priority:` 之前。那时「第一条」命中的是嵌套里那个，于是：条目级 priority
    没被对齐（拆档没修上），嵌套结构里一个无关的键被改成了档位值，而
    `realigned` 报的是「已对齐」—— 三重错误叠在一起，且 YAML 仍然合法。

    给了 `indent` 就只认该缩进的行。调用方传的是该段条目的字段缩进
    （`_detect_indent` 从原文件读出来的，四段未必一致）。

    返回 (新行, 改没改, 跳过原因)。第三项非空表示「找到了这个条目但没能改」——
    值不是裸整数（`"900"` / `!!int 900` / 锚点 / flow 风格）或整个条目找不到
    条目级 priority。这类必须报出来：静默跳过等于让用户以为拆档修好了。
    """
    out: list[str] = []
    changed = False
    done = False
    odd = ""
    for line in block:
        raw = line.rstrip("\n")
        m = None if done else _PRI_LINE.match(raw)
        if m and indent is not None and len(m.group(1)) != indent:
            m = None                    # 嵌套里的同名键，不是条目级那一行
        if m:
            done = True
            if int(m.group(2)) != new_pri:
                nl = "\n" if line.endswith("\n") else ""
                out.append(f"{m.group(1)}priority: {new_pri}        "
                           f"# {note}{nl}")
                changed = True
                continue
        elif not done and not odd and _PRI_ODD.match(raw):
            # 值不是裸整数。缩进也要对得上才算条目级 —— 否则嵌套里的
            # `priority: *anchor` 会被误报成本条目的问题。
            lead = len(raw) - len(raw.lstrip())
            if indent is None or lead == indent:
                odd = raw.strip()
        out.append(line)
    if not changed and not odd and not done:
        odd = "该条目没有条目级 priority 行"
    return out, changed, odd


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

    当前契约修正：以上是旧策略。现在只折叠主机大小写和尾斜杠，保留
    scheme、userinfo、路径大小写和查询参数，避免不同渠道串源。
    """
    # Scheme, channel path and query are source identity; only host case folds.
    return _source_identity(base_url)


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
         少一把 Key 就少一把。实测生产配置 gorou.example 15 把、
         tango.example 14 把，只要一把探测抛异常（BatchProber 会把它整个
         凭据从 results 里去掉）就丢一把。前三段有 `_orphan_entry_lines`
         兜这个，compat 段没有。
      ② per-key 的 proxy-url 被统一。实测 kilo.example 5 把、hotel.example
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
    # Reject malformed originals before any flow-style normalization.
    parsed_cfg = _load_yaml("\n".join(x.rstrip("\r\n") for x in original_lines))
    if parsed_cfg != cfg:
        raise ValueError("Source config differs from its YAML snapshot")
    source_records = {s: _source_records(original_lines, s) for s in _SECTION_KEYS}

    # 1. 注释索引（按 (段, 键)）
    comments_map = _extract_entry_comments(original_lines)

    # 1b. 原条目里 render_entry 不认识的字段（request-scoped-errors /
    #     excluded-models / websockets / fingerprint-profile / disabled…）。
    #     整段重写会把它们抹掉，所以按原文行搬回去 —— 见 extract_carry_lines。
    carry_map = {}
    for section, records in source_records.items():
        exact_rows = carry_map.setdefault(section, {})
        for row, _block in records:
            keys = ([k.get("api-key") for k in row.get("api-key-entries") or []]
                    if section == "openai-compatibility" else [row.get("api-key")])
            for key in keys:
                exact_rows[carry_key(_source_identity(row.get("base-url", "")), key)] = (
                    _dump_fields({k: v for k, v in row.items()
                                  if k not in _RENDERED_KEYS}, "    "))

    # 1c. compat 段 per-key 的续行（proxy-url / weight …），按 (host, Key) 索引。
    #     extract_carry_lines 抓的是 provider 级字段，对 api-key-entries 整块
    #     是跳过的 —— 这一份补那一块。见 compat_key_blocks 的两处成因说明。
    # Per-key fields now come directly from the exact original provider below;
    # a URL-only key-block table cannot distinguish two providers at one URL.

    def attach_carry(sp: SectionPlan) -> None:
        """给方案补上该条目原有的 carry 行。已经有了就不动（用户覆盖优先）。"""
        if sp.carry_lines:
            return
        d = carry_map.get(sp.section) or {}
        exact = carry_key(_source_identity(sp.base_url), sp.api_key)
        if exact in d:
            # 原文件里有这个凭据的条目 —— 用它自己的，哪怕是空的。
            # 退到兜底键会把同站另一条的字段染过来（实测 zulu 的
            # fingerprint-profile 从 1 个条目扩散到 3 个）。
            sp.carry_lines = list(d[exact])
            return
        # 新导入的 Key：原文件没有它的条目，拿同站的规则当默认
        # 旧 host 默认策略已停用；只接受完整来源身份。
        h = _source_identity(sp.base_url)
        got = d.get(carry_key(h, sp.api_key))
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
            old = _original_entry(cfg, sp)
            block = next((b for row, b in source_records[sp.section] if row == old), None)
            sections_data[sp.section].append(_prepare_source_plan(cfg, sp, block))

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
    compat_groups: dict[tuple[str, str, str], list[SectionPlan]] = {}
    for sp in sections_data["openai-compatibility"]:
        old = _original_entry(cfg, sp)
        group_key = (compat_provider_key(sp.base_url), _compat_capability(sp, old),
                     old.get("name", sp.provider_name))
        compat_groups.setdefault(group_key, []).append(sp)
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
            # 被并回重写后的 provider 的留守 Key。它们的行已经写出去了，
            # 所以下面 keep_unplanned 那一步不能再把同一条 provider 记录
            # 原样贴一遍 —— 否则同一个站出现两条 provider、同一把 Key 在
            # CPA 的轮询池里占两个位（2026-09-12 实测形态）。
            absorbed: set[tuple[str, str]] = set()
            for (pkey, capability, source_name), group in compat_ordered:
                # head 取组内 priority 最高的那个，不是插入顺序的第一个 ——
                # 组的其余成员只贡献 api-key，所以 head 的选择决定了整组用
                # 哪一套 headers/priority/models。
                head = copy.deepcopy(max(group, key=lambda x: x.priority))
                # Capability splits stay at the same site tier.
                head.priority = max(x.priority for x in sections_data[section]
                                    if _host_of(x.base_url) == _host_of(head.base_url))
                old = _original_entry(cfg, head)
                own_keys = {k["api-key"]: _dump_fields(
                    {f: v for f, v in k.items() if f != "api-key"}, field + "    ")
                    for k in old.get("api-key-entries") or []}
                planned_keys = {sp.api_key for sp in group}
                # 组内**没进方案**的 Key：原样留在这个 provider 下。
                #
                # 2026-09-12 接回来。这一份必须在 own_keys 被按 group 过滤
                # **之前**算出来（下面那一行过滤），否则永远是空集 ——
                # 上一版正是那个顺序，`orphan_keys` 成了死代码，于是
                # 「只勾了组里一把 Key」时另外几把全丢。
                orphan_keys = ([k for k in own_keys if k not in planned_keys]
                               if keep_unplanned else [])
                # 能力分裂的判据：这个 provider 里有**方案说要用另一套参数**
                # 的 Key。
                #
                # 不能拿「own_keys 有 group 之外的 Key」当判据（2026-09-12
                # 修）：那一批里绝大多数是上面 orphan_keys 那种「本次没重探」
                # 的 Key，它们会被原样并回同一条 provider，根本没有参数冲突。
                # 按那个判据一分裂，provider 就被改名成
                # `p.example.com-<hash>`，而 CPA 按 `name` 索引冷却
                # （conductor_cooldown.go:73）、模型能力
                # （api_key_model_capabilities.go:186）与执行路由 ——
                # 改名等于把这三处的状态全部作废，实测形态是「勾了组里一把
                # Key，落盘多出一条同站 provider」。
                #
                # 真正需要分裂的只有两种：
                #   · 同一个 pkey 在本次方案里出现了**多种能力组合**
                #     （compat_groups 的键第二维不同），那时同名会让两套
                #     参数互相覆盖
                #   · 全新 provider（old 为空）而同 host 下已经有别的组
                other_capabilities = sum(
                    1 for p, c, n in compat_groups if p == pkey)
                split = (other_capabilities > 1 or
                         not old and sum(1 for p, c, n in compat_groups
                                         if _host_of(p) == _host_of(pkey)) > 1)
                if split:
                    name = head.provider_name or _host_of(head.base_url)
                    tag = hashlib.sha256(
                        (pkey + "\0" + capability).encode()).hexdigest()[:10]
                    # 已经带着**同一个**后缀时不再追加（2026-09-12）
                    # ------------------------------------------------
                    # 重建必须语义幂等：同一份方案跑两遍要得到同一份配置。
                    # 上一版无条件追加，于是第二遍把
                    # `fixture-provider-753bdf4f14` 变成
                    # `fixture-provider-753bdf4f14-753bdf4f14` —— provider
                    # 每重建一次改一次名，而 CPA 按 `name` 索引冷却
                    # （conductor_cooldown.go:73）与模型能力
                    # （api_key_model_capabilities.go:186），等于每次重建
                    # 都把这两处状态清零。
                    #
                    # 只剥「与本次算出的完全相同」的后缀，不按形态剥 ——
                    # 某个 provider 的原名恰好以 10 位十六进制结尾时，
                    # 按形态剥会把操作员写的名字改掉。
                    if name.endswith("-" + tag):
                        head.provider_name = name
                    else:
                        head.provider_name = name + "-" + tag
                extra = [g.api_key for g in group if g.api_key != head.api_key]
                # 组内**没进方案**的 Key 也要保留（2026-09-03）。
                #
                # 前三段有 _orphan_entry_lines 兜这件事，compat 段没有 ——
                # _orphan_provider_lines 只保留「整个 provider 都没被碰到」
                # 的条目，被碰到的 provider 整条重写，组内少一把 Key 就少
                # 一把。而「没进方案」有无害成因：探测抛异常（BatchProber
                # 会把那个凭据整个从 results 去掉）、用户没勾、该段判不可写。
                # 实测生产配置 gorou.example 15 把、tango.example 14 把。
                # 能力分裂时才按 group 过滤 per-key 续行：分裂出来的那条
                # provider 只该带自己那几把 Key。不分裂时留守的 Key 也要
                # 带上它们自己的续行（proxy-url / weight），否则并回来的
                # Key 会丢掉那些字段 —— `weight: 0` 丢了那把 Key 就复活。
                if split:
                    own_keys = {k: v for k, v in own_keys.items()
                                if k in planned_keys}
                    orphan_keys = []
                if orphan_keys:
                    extra = extra + orphan_keys
                    absorbed.update((_source_identity(head.base_url), k)
                                    for k in orphan_keys)
                    warnings.append(
                        f"段 {section} · {pkey}：{len(orphan_keys)} 把 Key 不在本次"
                        f"方案内（探测异常 / 未勾选 / 判不可写）—— 已原样保留在"
                        f"这个 provider 下，不会因整条重写而丢失")
                attach_carry(head)
                # 模型级字段（窗口值、白名单外字段）要取**组内并集**，不只读
                # head 的（2026-09-04 逐字段对账发现）。
                #
                # compat 段的 `models:` 块在 **provider 级**，一份清单为组内所有
                # Key 共用；而 `prior_context` / `prior_model_extras` 是按
                # (段, host, api_key) 查出来的 per-Key 数据。只读 head 的话，
                # head 一旦是「本次新粘的 Key」（它在原文件里没有任何记录），
                # 整组的模型级字段就查不到 —— `models` 块被重写成裸
                # `name` / `alias`。
                #
                # 实测生产配置 kilo.example 的 compat 段：新 Key 排在方案前面时
                # `claude-opus-5: 987500` 变成空；排在后面才不丢。触发条件低到
                # 「同 priority 时谁先进 all_plans」，而那个顺序取决于输入行序。
                #
                # 并集不会引入重复：同一个模型名在组内各 Key 那里是同一个值
                # （它们读的是同一份 provider 级 models 块），dict 更新即可。
                # 万一真有分歧，以 head 的为准 —— 最后写它。
                merged_ctx: dict[str, int] = {}
                merged_extra: dict[str, dict] = {}
                for g in group:
                    if g is head:
                        continue
                    merged_ctx.update(g.prior_context or {})
                    for mname, fields in (g.prior_model_extras or {}).items():
                        merged_extra.setdefault(mname, {}).update(fields)
                merged_ctx.update(head.prior_context or {})
                for mname, fields in (head.prior_model_extras or {}).items():
                    merged_extra.setdefault(mname, {}).update(fields)
                if merged_ctx != (head.prior_context or {}):
                    head = copy.copy(head)
                    head.prior_context = merged_ctx
                    head.prior_model_extras = merged_extra
                elif merged_extra != (head.prior_model_extras or {}):
                    head = copy.copy(head)
                    head.prior_model_extras = merged_extra
                for c in _comments_for(comments_map, section, head, used, _host_of):
                    out.append(c.rstrip("\n"))
                # 每把 Key 自己的方案 —— per-key 的 proxy-url / weight 逐把取，
                # 不拿 head 的值套给全组（见 render_entry 的 per-key 一节）。
                for line in render_entry(head, dash, field, stamp,
                                         extra_keys=extra,
                                         key_lines=own_keys,
                                         key_plans={g.api_key: g
                                                    for g in group},
                                         original_entry=old):
                    out.append(line)

            # compat 段的「未覆盖」按 **provider 身份**判 —— 它的结构是
            # provider 级 + api-key-entries，一个条目含多个 Key。本次方案
            # 没碰到的 provider 整条原样保留，否则「只勾了 1 个站」会把另外
            # 12 个 provider 全删掉（2026-09-02 实测 13 → 1）。
            if keep_unplanned and span:
                touched = {(_source_identity(g.base_url), g.api_key)
                           for g in sections_data[section]} | absorbed
                kept = _unselected_records(original_lines, section, touched, field)
                if kept:
                    for line in kept:
                        out.append(line if line.endswith("\n") else line + "\n")
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
                out.append(c.rstrip("\n"))
            for line in render_entry(sp, dash, field, stamp,
                                     original_entry=_original_entry(cfg, sp)):
                out.append(line)

        # keep_unplanned：本段有方案的凭据只是一部分，其余原条目**原样保留**。
        #
        # 为什么必须有（2026-09-02 生产事故）：整段重写会把「没进方案」的条目
        # 一并抹掉。而「没进方案」有三种完全无害的原因 ——
        #   · 用户只勾了推荐项，其余段没勾
        #   · 那个段判不可写（models 为空）
        #   · 探测时抛异常，那个凭据整个不在结果里
        # 三种都不该导致删除。删除只应由用户显式操作，不该是「没勾」的副作用。
        if keep_unplanned and span:
            planned = {(_source_identity(x.base_url), x.api_key) for x in entries}
            # 留守条目的 priority 对齐到同站本次的新值 —— 同站同档必须在
            # **落盘结果**上成立，不只在方案对象里成立。见 _realign_priority。
            #
            # 同一批 entries 里同 host 出现多个 priority 时**整站放弃对齐**
            # （2026-09-04 自查）：那意味着操作员手工把同站的几把 Key 改成了
            # 不同值（覆盖在定档之后应用，`assign_priorities` 拦不住）。
            # 此时「同站的新档」不存在唯一答案，随便挑一个去改留守条目等于
            # 用工具的猜测覆盖操作员的显式意图。改为报出来让人自己决定。
            per_host: dict[str, set[int]] = {}
            for x in entries:
                h = _host_of(x.base_url)
                if h:
                    per_host.setdefault(h, set()).add(x.priority)
            host_tier: dict[str, tuple[int, str]] = {}
            ambiguous = sorted(h for h, v in per_host.items() if len(v) > 1)
            for h, vals in per_host.items():
                if len(vals) > 1:
                    continue
                pri = next(iter(vals))
                host_tier[h] = (
                    pri,
                    f"{stamp} 对齐同站档位 · 本次未重探此 Key，"
                    f"随同站其他 Key 一并置为 {pri}"
                    f"（同站同档，否则被拆成两层轮询）")
            if ambiguous:
                warnings.append(
                    f"段 {section}：{len(ambiguous)} 个站的 Key 在本次方案里拿到了"
                    f"**不同**的 priority（{'、'.join(ambiguous)}）—— 通常是手工改过。"
                    f"该站没进方案的 Key 因此保持原值不动：同站的「新档」不唯一，"
                    f"工具不替你挑。要同站同层请把它们改成同一个值")
            realigned: list[str] = []
            skipped: list[str] = []
            kept = _unselected_records(original_lines, section, planned, field,
                                       host_tier=host_tier, realigned=realigned,
                                       skipped=skipped)
            if kept:
                for line in kept:
                    out.append(line.rstrip("\n"))
                warnings.append(
                    f"段 {section}：{len(entries)} 条按新方案重写，"
                    f"另有条目不在本次方案内 —— 已原样保留")
            if realigned:
                warnings.append(
                    f"段 {section}：{len(realigned)} 个站有 Key 不在本次方案内，"
                    f"其 priority 已对齐到同站新档"
                    f"（{'、'.join(sorted(realigned))}）—— 同站多 Key 必须同层，"
                    f"分成两层会退化成主备切换而不是并行轮询")
            if skipped:
                warnings.append(
                    f"段 {section}：{len(skipped)} 个留守条目的 priority **没能对齐**"
                    f"（{'、'.join(skipped[:4])}{'…' if len(skipped) > 4 else ''}）"
                    f"—— 本工具只改裸整数写法的那一行。这些条目会与同站其他 Key "
                    f"分处两层（主备切换而非并行轮询），请手工核对")
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

    # 行尾换行必须先剥掉（2026-09-11 实跑对账发现的生产缺陷）
    # ------------------------------------------------------
    # 本函数末尾是 `"\n".join(out_lines)`，所以 out_lines 的每个元素**必须是
    # 不带换行的裸行**。而下面三处 `out_lines.extend(original_lines[...])`
    # 直接搬运入参，入参又是所有调用方（含生产写回 server.py:2452）传的
    # `raw.splitlines(keepends=True)` —— 每行自带 `\n`，再被 join 加一个，
    # 于是**每一行后面都多出一个空行**。
    #
    # 实测：拿生产 config.yaml 走一次「一条都没勾选」的全量重建，
    # 6081 行变 12161 行、空行从 9 个变 6089 个。内容与注释都不丢
    # （所以逐字段对账看不出来，此前一直没被发现），但文件每重建一次翻一倍。
    #
    # 在入口统一归一化而不是改那三处 extend：`render_section` 产出的 body
    # 本来就是裸行，两种来源在这里对齐，后面的逻辑不用再关心换行形态。
    original_lines = [x.rstrip("\r\n") for x in original_lines]

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
            # 段头原样输出，**除非**它自带空字面量（`claude-api-key: []` /
            # `{}`）—— 那种段头后面直接挂 `- api-key:` 是非法 YAML
            # （2026-09-04 自查：增量路径早就用 `_empty_literal_rewrite` 处理
            # 这件事，全量重建这一支漏了，于是同一份输入走两条路一条合法
            # 一条不合法。落盘被 validate 挡住，所以症状是「全量重探对这类
            # 文件整个不可用」而不是写坏文件）。
            #
            # 这类段头在全新或被清空的 config.yaml 里很常见，本项目自己的
            # tools/e2e_dead_pick.py 造场景时就用 `claude-api-key: []`。
            head_line = original_lines[start]
            rw = _empty_literal_rewrite(original_lines, start, section)
            flow_end = None
            if rw is not None:
                head_line = rw[1]
            else:
                # **非空** flow 序列（`claude-api-key: [{api-key: "k1", ...}]`）。
                #
                # 合法 YAML、CPA 读得出来，但块序列不能挂在它后面。
                # 段头改成裸键，flow 那几行整个丢掉 —— 里面的条目已经由
                # render_section 从方案重新生成了（方案本身就是从 cfg 读的，
                # 而 cfg 是 PyYAML 解析这份 flow 得来的，所以不丢数据）。
                #
                # 2026-09-05 加。此前 validate 会挡住产出（不会写坏文件），
                # 但报错是 `while parsing a block mapping` —— 看不出根因是
                # 段头形态，症状表现为「全量重探对这类文件整个不可用」。
                flow_end = _flow_section_span(original_lines, start)
                if flow_end is not None:
                    key = head_line.split(":", 1)[0]
                    head_line = f"{key}:"
            out_lines.append(head_line)
            out_lines.extend(body)
            replaced.append(section)
        # flow 段头跨了几行就跳过那几行 —— 否则它们会被当成「段之后的内容」
        # 原样输出，产出重复条目
        cursor = max(end, flow_end) if body is not None and flow_end else end

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
            out_lines.append("")
        out_lines.append(f"{section}:")
        out_lines.extend(body)
        warnings.append(f"原文件没有 {section} 段，已在末尾新建")

    untouched = [s for s in spans if s not in replaced]
    if untouched:
        warnings.append(
            "以下段本次没有可写方案，原条目已原样保留："
            + "、".join(untouched))

    return "\n".join(out_lines), warnings


def _comments_for(comments_map: dict, section: str, sp: SectionPlan,
                  used: set, host_of_fn) -> list[str]:
    """按多个候选键查该站的人工注释，同一份只挂一次。

    多候选是因为 sp.base_url 是 base_for_section() 的产物（codex/compat 补了
    /v1），与原文件里写的未必一致。去重是因为前三段每个 Key 各占一条，
    按 host 匹配会让原文件里只出现一次的注释在重建后出现 N 次。

    两个候选要**合并**，不是「第一个命中就返回」（2026-09-03）
    ------------------------------------------------------
    `_extract_entry_comments` 给同一个条目建两个键（base-url 原文与 host），
    但两者的内容会分叉 —— 段尾那块未被认领的注释只挂在 host 键上（那时
    base-url 原文键早已用过），实测 kilo.example 的 host 键 26 行、
    base-url 键 20 行，差的 6 行正是它提档到 550 的唯一依据。
    上一版先试 base-url、命中就 return，那 6 行永远出不来。
    """
    sec = comments_map.get(section, {})
    out: list[str] = []
    seen: set[str] = set()
    mine: set[str] = set()      # 本次调用已消费的候选
    hit = False
    for cand in (sp.base_url, host_of_fn(sp.base_url)):
        if not cand or cand not in sec:
            continue
        if cand in mine:
            # 两个候选算出同一个键（base-url 本来就是裸主机名时）——
            # 已经收过了，跳过而不是放弃。上一版在这里 `return []`，于是
            # 那种条目的注释全丢（test_rebuild_config_preserves_comments 抓到）。
            continue
        if cand in used:
            # 这一份已经挂给同站的另一个 Key 了 —— 整条跳过，
            # 不能只跳这个候选（那会让同一批行输出两次）。
            return []
        used.add(cand)
        mine.add(cand)
        hit = True
        for line in sec[cand]:
            k = line.strip()
            if k in seen:
                continue
            seen.add(k)
            out.append(line)
    return out if hit else []


# render_entry 自己会写的字段。搬运原字段时要跳过它们 —— 否则同一个键会
# 出现两次（YAML 里后者覆盖前者，值可能是旧的）。
_RENDERED_KEYS = frozenset({
    "api-key", "base-url", "prefix", "priority", "weight", "proxy-url",
    "headers", "models", "name", "api-key-entries",
    # 段专属能力开关（2026-09-04）：从 carry 移到 render_entry 自己写。
    #
    # 为什么要移：它们现在**由实测决定**（_stage5_capabilities），而 carry 是
    # 原文行搬运 —— 两者同时生效会写出两行同名键。PyYAML 取后一个、
    # 而 Go 的 yaml.v3 直接报 `mapping key already defined` 拒绝加载整份配置。
    # 也就是说重复键不是「值取谁」的小问题，是 CPA 起不来。
    #
    # 移过来之后原值由 `existing_toggles` 查表搬（见 _toggle_lines 的三层
    # 优先级），语义与 headers / proxy-url 一致。
    "websockets", "support-prompt-cache-key",
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
    # 旧手工扫描器的两个分支现由结构化标量解析替代：
    # 引号没闭合，原样给回
    # 裸标量：第一个 ` #` 之前
    # 无效引号现在明确拒绝，不再返回可能已经变质的凭据。
    import yaml
    try:
        # BaseLoader decodes quoting without coercing credential scalars to bool/int.
        value = yaml.load(tail, Loader=yaml.BaseLoader)
    except Exception:
        raise ValueError("Invalid YAML scalar") from None
    return value if isinstance(value, str) else ""


def extract_carry_lines(lines: list[str]) -> dict[str, dict[str, list[str]]]:
    """提取每个条目里 render_entry **不认识**的字段，按 (段, 站名) 索引原文行。

    为什么必须有（2026-09-02 拿生产 config.yaml 核对发现）
    -------------------------------------------------
    render_entry 是白名单式渲染，只写它知道的 12 个字段；而全量重探用它
    **整段重写**。生产配置 121 个条目里 117 条带白名单外的字段，重写后
    全部静默消失 —— validate() 报成功，YAML 也合法，只是行为变了：

        request-scoped-errors  116 条   冷却规则，丢了坏站不再被剔除
        fingerprint-profile      1 条   让 CPA 自己补设备指纹

    2026-09-04 重新点过：以前这张表里的 `excluded-models 39 条` 与
    `disabled 1 条` **都是 0** —— 那两个数是 `grep -c` 数出来的，把注释里的
    `# excluded-models 可选…` 也数进去了。这类计数从此按解析后的 YAML 数。
    `websockets` 也从这张表里移走了：它现在由实测决定，见 _toggle_lines。

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

        # 离开本段（顶层键）。判据与 _section_span 同一个 —— 三处都要认
        # 含点号与引号的顶层键，否则那个键会被当成条目的 carry 行收走。
        if _TOP_LEVEL_KEY.match(line):
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

    两条修正（2026-09-03，拿注释最全的那份 config.yaml 对账才暴露）
    ----------------------------------------------------------
    ① `models:` 底下的 `- name: <模型名>` 也匹配 `m_name`，于是**模型名被
       当成条目键**。实测那份文件里 `claude-opus-5` 这个「键」被覆盖 57 次、
       `claude-opus-4-8` 48 次 —— 每次覆盖都把上一块注释整个丢掉，合计 13271
       行注释被反复顶掉，其中 118 行是**任何键都不再指向**的孤儿（包括
       「hotel：实测 403 WAF 按 IP 拦截」「weight: 0（为压 Cloudflare 524
       加的）」这类唯一的排障结论）。
       修法：只认**条目级**的 name —— 缩进不深于 base-url 那一层，且
       不在 `models:` 块内。

    ② 同一个键第二次出现时直接 `=` 覆盖。前三段每个 Key 各占一条目、
       同站多条目是常态（gorou 15 条），后一条的注释会顶掉前一条的。
       改成**累加**（去重后 extend）：`_comments_for` 那边按 host 查、
       同一份只挂一次，多挂几行不会重复输出，而丢掉就再也找不回来。
    """
    from .parse import host_of

    comments: dict[str, dict[str, list[str]]] = {}
    current_section = None
    pending: list[str] = []
    # models: 块的缩进。进入后 `- name:` 是模型名而不是条目名。
    models_indent: int | None = None

    def add(section: str, key: str, block: list[str]) -> None:
        """把一块注释挂到键上。已有内容就累加，绝不覆盖。"""
        if not key or not block:
            return
        cur = comments[section].setdefault(key, [])
        have = {x.strip() for x in cur}
        cur.extend(x for x in block if x.strip() not in have)

    # 当前条目的键，以及「是否正处在一个条目内部」。
    # 用于接住**夹在条目中间**的注释块 —— 见循环末尾那一支。
    last_key = ""
    entry_open = False
    # 前置注释块是否已经为「某个新条目」跨越过一次 `-` 边界。
    # 只允许跨一个 —— 见循环末尾 is_dash 那一支的说明。
    pending_carried = False

    for line in lines:
        # 段头：顶层键
        m = re.match(r"^(gemini-api-key|codex-api-key|claude-api-key"
                     r"|openai-compatibility)\s*:", line)
        if m:
            current_section = m.group(1)
            comments.setdefault(current_section, {})
            pending = []
            pending_carried = False
            models_indent = None
            last_key = ""
            entry_open = False
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

        # 顶层键（非本段）：离开该段。判据与 _section_span 同一个。
        if _TOP_LEVEL_KEY.match(line):
            current_section = None
            pending = []
            pending_carried = False
            models_indent = None
            last_key = ""
            entry_open = False
            continue

        indent = len(line) - len(line.lstrip())
        # 进入 / 离开 models: 块。块内的 `- name:` 是模型名，不是条目名。
        if re.match(r"^\s*models\s*:", line):
            models_indent = indent
            continue
        if models_indent is not None and indent <= models_indent:
            models_indent = None

        m_name = re.match(r"^\s*-?\s*name:\s*(.+)$", line)
        m_base = re.match(r"^\s*-?\s*base-url:\s*(.+)$", line)
        is_dash = line.lstrip().startswith("-")

        if m_name and pending and models_indent is None:
            add(current_section, _scalar_value(m_name.group(1)), pending)
            # 不清空 —— 同一条目的 base-url 可能在下一行，两个键都要能查到
        elif m_base and pending:
            # 必须用 _scalar_value 剥行尾注释（2026-09-03，与 2026-09-02 的
            # carry 索引同一个成因，同一个 bug 修在两处）。生产文件里有
            # `base-url: "https://nova.example" # 注意不带 /v1`，
            # `.strip().strip("\"'")` 只剥得掉前引号 —— 剩下
            # `https://nova.example" # 注意不带 /v1`，host_of 解析不出主机名，
            # 于是整条目的注释挂在一个**永远查不到**的垃圾键上。
            # 实测那份文件 45 种注释因此丢失，含「hotel：实测 403 WAF 按 IP
            # 拦截」这类唯一的排障结论。
            raw_base = _scalar_value(m_base.group(1))
            h = host_of(raw_base)
            if h:
                add(current_section, h, pending)
            add(current_section, raw_base, pending)
            pending = []
            pending_carried = False
        elif (pending and entry_open and last_key
                and not is_dash and models_indent is None):
            # 注释块**夹在条目中间** —— 不在 name / base-url 之前，后面也不会
            # 再有它们来认领。
            #
            # 实测那份 config.yaml 的 compat 段最后一个 provider 就是这个形状：
            #     - name: "kilo.example"
            #       base-url: "https://kilo.example/v1"
            #       # priority 25 -> 530（2026-08-30 深夜，实测可用后提档）
            #       # …5 行依据…
            #       priority: 550
            # base-url 已经把 pending 清空，这一块攒在它之后，到下一个条目时
            # 被 `pending = []` 丢掉。那 6 行是它提档到 550 的唯一依据。
            #
            # 四道闸缺一不可（2026-09-07 加第四道）：
            #   · `entry_open` —— 必须真的在某个条目内部（本段见过 base-url
            #     之后、下一个 `-` 之前）。段头到第一个条目之间那些「字段说明」
            #     注释不属于任何条目，挂上去会让它们跟着那个站被复制 N 遍
            #     （实测放开后多出 107 行重复）。
            #   · 不是 `-` 开头 —— 那是下一个条目的起点，它的前置注释归它。
            #   · `models_indent is None` —— 不在 models 块内。块内的注释是
            #     **模型级**的（`# 这一款静默换模` 之类），提到条目级会让它
            #     跟着整个站走。自测抓到：`models:` 底下的注释被挂到条目上。
            #   · pending 里的注释缩进 > 0 且 ≤ 4（条目字段级）—— 顶格注释
            #     （indent=0）不算字段间注释。它们在条目内部时不应被挂到 last_key，
            #     否则会被 _comments_for 插到段头第一个条目前（原本在条目中间），
            #     第二次运行时丢失（非幂等）。request-scoped-errors 的 match 列表
            #     里那些缩进 10 的注释是**字段内部结构**的，也不是字段之间的。
            #     代价：条目内的顶格注释会丢失（实测 5 条），但保留 1648 行字段间
            #     注释更重要。
            # 检查 pending 首行缩进（已知全是注释行，统一缩进）
            first_comment_indent = len(pending[0]) - len(pending[0].lstrip()) if pending else 999
            if 0 < first_comment_indent <= 4:
                add(current_section, last_key, pending)
            pending = []
            pending_carried = False

        # 条目边界：见到 base-url 就认为进入了一个条目（四段都有这个字段），
        # 见到下一个 `-` 起头的行就认为上一个条目已经结束。
        if m_base and models_indent is None:
            _rb = _scalar_value(m_base.group(1))
            last_key = host_of(_rb) or _rb or last_key
            entry_open = True
        elif is_dash and models_indent is None and not m_name:
            entry_open = False
            # 这一行是**新条目的起点**，紧挨它上面的注释块属于**这个新条目**，
            # 不能在这里清空（2026-09-10 修，`注释索引的六条边界` ① ③ 因此长期红）。
            #
            # 原来无条件 `pending = []`，理由写的是「条目结束，清空未认领的注释」。
            # 但条目的第一行常常不是 `name:` / `base-url:` 而是 `- api-key:` ——
            # 四段里 gemini / codex / claude 三段都是这个写法。于是：
            #     # A 的结论：实测 200        <- pending 攒下
            #     - api-key: "kA"            <- m_name/m_base 都不匹配，
            #                                   落到这里被清空
            #       base-url: "https://a…"   <- 轮到它认领时 pending 已空
            # 结果**所有以 `- api-key:` 开头的条目，其前置注释全部静默丢失**。
            # 只有像 C 那样注释写在 base-url 之后的才能靠「夹在条目中间」那支活下来。
            #
            # 但也不能无条件保留：gemini 段的 `base-url` 是可选的
            # （config_types.go:607 允许为空），一个既没有 name 也没有 base-url 的
            # 条目不会有人来认领 pending，放任下去会让它**串到下一个条目**上。
            # 所以只允许跨越**一个**条目边界：`pending_carried` 记住"这块注释
            # 已经为某个新条目保留过一次了"，再遇到下一个 `-` 仍未被认领就丢掉。
            if pending_carried:
                pending = []
                pending_carried = False
            elif pending:
                pending_carried = True

    return comments
