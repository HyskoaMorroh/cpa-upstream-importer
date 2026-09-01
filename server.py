#!/usr/bin/env python3
"""批量导入服务 —— HTTP 后端。只用标准库，VPS 上不需要装任何依赖。

安全模型（重要，别改松）
------------------------
这个服务持有**明文上游 API key**并能改写 config.yaml，等价于 CPA 的写权限。
所以：

  1. 默认只绑 127.0.0.1。要外网访问请用 nginx 反代并在那一层加 TLS + 认证，
     不要把 --host 改成 0.0.0.0 直接暴露。
  2. **强制 Bearer token**，没有免鉴权模式。token 从 --token 或环境变量
     IMPORTER_TOKEN 读；都没给则启动时随机生成并打印到 stdout。
  3. 写回必须两步：先 /api/plan 拿到 plan_id，再 /api/apply 带同一个
     plan_id + confirm=true。单次请求改不了文件。
  4. 完整 key 只在内存里，不落日志、不进 JSON 响应（一律 masked）。

用法
----
    # VPS /opt/deploy/upstream-importer 下
    IMPORTER_TOKEN=$(openssl rand -hex 16) python3 server.py \
        --config /opt/deploy/config.yaml --port 8765

    # 浏览器开 http://127.0.0.1:8765/?token=<那串>
    # 或用 SSH 端口转发：ssh -L 8765:127.0.0.1:8765 root@vps
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hmac
import io
import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cpa_probe as cp  # noqa: E402
from cpa_probe.pipeline import Prober, SEED_MODELS  # noqa: E402
from cpa_probe.batch import BatchProber, extract_existing_entries  # noqa: E402
from cpa_probe.writeback import (  # noqa: E402
    apply_diffs,
    build_diffs,
    push_to_cpa,
    reload_cpa,
    validate,
    verify_upstream,
    write_local,
)

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "web")


# --------------------------------------------------------------------------
# 任务状态
# --------------------------------------------------------------------------


class Job:
    """一次探测任务。事件流供前端轮询 —— 不用 SSE/WebSocket，与 CPAMP 口径一致。"""

    def __init__(self, job_id: str, rows: list, opts: dict):
        self.id = job_id
        self.rows = rows
        self.opts = opts
        self.events: list[dict] = []
        self.results: list = []
        self.state = "pending"      # pending | running | done | error
        self.error = ""
        self.started = time.time()
        self.finished = 0.0
        self.calls = 0
        self.lock = threading.Lock()

    def emit(self, kind: str, data: dict) -> None:
        with self.lock:
            self.events.append({"t": round(time.time() - self.started, 1),
                                "kind": kind, **data})
            if kind == "attempt":
                self.calls += 1

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "state": self.state,
                "error": self.error,
                "calls": self.calls,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
                "total_rows": len(self.rows),
                "done_rows": len(self.results),
                "events": self.events[since:],
                "event_cursor": len(self.events),
            }


class Store:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.plans: dict[str, dict] = {}
        self.lock = threading.Lock()

    def add_job(self, job: Job) -> None:
        with self.lock:
            self.jobs[job.id] = job

    def get_job(self, jid: str) -> Job | None:
        with self.lock:
            return self.jobs.get(jid)

    def add_plan(self, pid: str, payload: dict) -> None:
        with self.lock:
            self.plans[pid] = payload

    def get_plan(self, pid: str) -> dict | None:
        with self.lock:
            return self.plans.get(pid)


STORE = Store()


# --------------------------------------------------------------------------
# 序列化：完整 key 绝不出现在响应里
# --------------------------------------------------------------------------


def row_json(row) -> dict:
    return {
        "line_no": row.line_no,
        "host": row.host,
        "bare": row.bare,
        "key_masked": row.masked(),
        "error": row.error,
        "bases": {s: row.base_for(s) for s in cp.SECTIONS},
    }


def verdict_json(v) -> dict:
    return {
        "section": v.section,
        "usable": v.usable,
        "base_url": v.base_url,
        "models": v.models,
        "need_proxy": v.need_proxy,
        "min_headers": v.min_headers,
        "swap": v.swap,
        "swap_detected": v.swap_detected,
        "max_context_length": v.max_context_length,
        "context_untrusted": v.context_untrusted,
        "context_model": v.context_model,
        "category": v.category,
        "action": v.action,
        "summary": v.summary(),
        "attempts": [
            {
                "model": a.model,
                "combo": a.combo,
                "status": a.status,
                "category": a.category,
                "action": a.action,
                "elapsed_ms": a.elapsed_ms,
                "proxy": a.proxy,
                "resp_model": a.resp_model,
                "backend": a.backend,
                "input_tokens": a.input_tokens,
                "sent_chars": a.sent_chars,
                "excerpt": a.excerpt,
            }
            for a in v.attempts
        ],
    }


def plan_json(p) -> dict:
    return {
        "host": p.host,
        "key_masked": p.masked_key,
        "skipped": p.skipped,
        "any_writable": p.any_writable,
        "sections": {
            s: {
                "section": sp.section,
                "base_url": sp.base_url,
                "models": sp.models,
                "priority": sp.priority,
                "priority_reason": sp.priority_reason,
                "proxy_url": sp.proxy_url,
                "headers": sp.headers,
                "max_context_length": sp.max_context_length,
                "context_model": sp.context_model,
                "score": sp.score,
                "duplicate": sp.duplicate,
                "duplicate_note": sp.duplicate_note,
                "writable": sp.writable,
                # 「能写」与「建议写」分开：recommended 决定 UI 默认勾选，
                # writable 决定用户手工勾上后能不能真写。换模/抢顶层/截断反推
                # 三类仍可写，但默认不勾 —— 见 SectionPlan.recommended。
                "recommended": sp.recommended,
                "recommend_reason": sp.recommend_reason,
                "warnings": sp.warnings,
                "impacts": [
                    {"model": i.model, "current_top": i.current_top,
                     "new_priority": i.new_priority, "hijacks": i.hijacks,
                     "shares": i.shares,
                     # 被挡在其后的站 —— 没劫持顶层时最容易被忽略的影响面
                     "shadowed_hosts": i.shadowed_hosts}
                    for i in sp.impacts
                ],
            }
            for s, sp in p.sections.items()
        },
    }


# --------------------------------------------------------------------------
# 探测线程
# --------------------------------------------------------------------------


def _resolve_proxy(requested: str) -> str | None:
    """把前端传来的代理意愿解析成一个真能连的地址。

    前端只表达「要不要试代理」（勾选框），不该让用户操心地址形态 ——
    容器内是服务名 `mihomo:7890`（同 default 网络），宿主机上得用映射端口
    `127.0.0.1:7890`。同一份前端两种部署都要能用，所以这里依次探测。

    返回 None 表示都不通，整轮跳过 via-proxy（Prober.live_proxy 也会再挡一次）。
    """
    if not requested:
        return None
    from cpa_probe.client import probe_proxy
    # 显式给了别的地址就只试那个，不擅自改成别的
    if requested not in ("http://mihomo:7890", "auto"):
        return requested
    for cand in ("http://mihomo:7890", "http://127.0.0.1:7890"):
        ok, _detail = probe_proxy(cand, timeout=3)
        if ok:
            return cand
    return None


def run_job(job: Job, cfg_path: str) -> None:
    job.state = "running"
    try:
        prober = Prober(
            proxy=_resolve_proxy(str(job.opts.get("proxy") or "")),
            gap=float(job.opts.get("gap", 3.0)),
            timeout=int(job.opts.get("timeout", 120)),
            probe_context=bool(job.opts.get("probe_context", True)),
            swap_samples=int(job.opts.get("swap_samples", 3)),
            workers=int(job.opts.get("workers", 4)),
            max_models=int(job.opts.get("max_models", 4)),
            max_model_attempts=int(job.opts.get("max_model_attempts", 10)),
            reuse_profile_verdict=bool(
                job.opts.get("reuse_profile_verdict", True)),
            on_event=job.emit,
        )

        # 候选并行度。不同站之间完全独立（gap 桶按 host 分、形态缓存按
        # (host, section) 分），所以可以放开跑。
        #
        # 为什么不无限并行：
        #   · 同一主机的多行会被 single-flight 归并成一次形态学习，
        #     真正的并行度上限是**不同主机数**，再高只是空转线程
        #   · 每个候选内部还会开最多 4 个段线程，总线程数是乘出来的
        # 所以取「不同主机数」与配置上限的较小值。
        hosts = {r.host for r in job.rows}
        cand_workers = max(1, min(len(hosts),
                                  int(job.opts.get("candidate_workers", 4))))

        # 结果按输入行序回填，不用 append —— 并行下完成先后是乱的，
        # 而前端结果表与 build_diffs 的插入顺序都依赖原始行序。
        slots: list = [None] * len(job.rows)

        def one(i: int, row) -> None:
            slots[i] = prober.probe(row)
            with job.lock:
                # done_rows 是进度显示用的，只数已完成的，与顺序无关
                job.results = [x for x in slots if x is not None]

        if cand_workers > 1 and len(job.rows) > 1:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=cand_workers,
                    thread_name_prefix="probe-cand") as ex:
                futs = [ex.submit(one, i, r) for i, r in enumerate(job.rows)]
                for f in concurrent.futures.as_completed(futs):
                    f.result()      # 让异常冒出去，走下面的 error 分支
        else:
            for i, row in enumerate(job.rows):
                one(i, row)

        with job.lock:
            job.results = [x for x in slots if x is not None]
        job.state = "done"
    except Exception:
        job.state = "error"
        job.error = traceback.format_exc(limit=4)
    finally:
        job.finished = time.time()


_CPA_COMMIT_CACHE: dict = {"at": 0.0, "commit": ""}
_CPA_COMMIT_TTL = 300.0


def _cpa_runtime_commit(base: str) -> str:
    """读运行中 CPA 的 commit（管理响应头 X-CPA-COMMIT，handler.go:267-269）。

    用来发现「源码已更新但 CPA 没重启」—— 挂进来的是源码，跑着的是编译产物。

    打的是 /healthz：它不需要管理密钥，而 X-CPA-COMMIT 由管理路由的中间件写
    在响应头上。拿不到就返回空串，调用方降级为「不比对版本」，不报错 ——
    这只是个增强信号，不能让它影响 /api/context 的可用性。

    缓存 5 分钟：每次打开网页都发一次外网请求不值得，而 CPA 版本不会秒级变。
    """
    if not base:
        return ""
    now = time.time()
    if now - _CPA_COMMIT_CACHE["at"] < _CPA_COMMIT_TTL:
        return _CPA_COMMIT_CACHE["commit"]
    commit = ""
    try:
        req = urllib.request.Request(base.rstrip("/") + "/healthz",
                                     method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            commit = (resp.headers.get("X-CPA-COMMIT") or "").strip()
    except Exception:                                   # noqa: BLE001
        commit = ""
    _CPA_COMMIT_CACHE.update(at=now, commit=commit)
    return commit


def run_job_full_redetect(job: Job, cfg_path: str) -> None:
    """全量重探模式：重新探测所有既有站 + 新站

    与 run_job 的区别：
    - 从 config.yaml 提取所有既有站
    - 使用 BatchProber 进行站级并发
    - 最后不返回 job.results，而是写回完整 config
    """
    job.state = "running"
    try:
        # 加载 config
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = f.read()
        import yaml
        cfg = yaml.safe_load(raw) or {}

        # 提取既有站
        existing_entries = extract_existing_entries(cfg)
        job.emit("info", {"msg": f"提取到 {len(existing_entries)} 个既有条目"})

        # 按凭据去重 —— **这一步省掉的量比并发还多**。
        #
        # config.yaml 的条目是「(凭据, 段)」的组合：同一个 url+key 常被写进
        # 2-4 个段（gemini / codex / claude / compat），因为很多中转站用同一把
        # Key 提供多种协议。实测这份配置 175 个条目其实只有 77 个不同凭据，
        # 9 个跨全四段、11 个跨三段、49 个跨两段。
        #
        # 而 Prober.probe() 的语义本来就是「拿一个凭据把四段各打一遍」——
        # 按条目喂它等于同一个凭据重复探 2-4 次，那份配置会白打 98 次全流程。
        #
        # 去重键取 (host, api_key) 而不是 (base_url, api_key)：同一个站在
        # 不同段的 base-url 形态不同（codex/compat 带 /v1，另两段不带），
        # 用 base_url 会把同一个凭据判成两个。host_of 已经小写化并剥掉协议。
        seen_cred: set[tuple[str, str]] = set()
        lines: list[str] = []
        dup = 0
        for _sec, base_url, api_key, _orig in existing_entries:
            ck = (cp.host_of(base_url), api_key)
            if ck in seen_cred:
                dup += 1
                continue
            seen_cred.add(ck)
            lines.append(f"{base_url},{api_key}")

        # 新站：原始文本，同样参与去重（用户可能粘贴了已在配置里的站）
        for row in job.rows:
            ck = (cp.host_of(row.bare), row.api_key)
            if ck in seen_cred:
                dup += 1
                continue
            seen_cred.add(ck)
            lines.append(row.raw)

        if dup:
            job.emit("info", {
                "msg": f"按 (站, Key) 去重：{len(existing_entries) + len(job.rows)} "
                       f"个条目 → {len(lines)} 个凭据，省掉 {dup} 次重复探测"
            })

        # 统一走 parse_lines，保证 ParsedRow 的 bare 按段规范化过
        parsed = cp.parse_lines("\n".join(lines))
        all_rows = list(parsed.valid)
        if parsed.invalid:
            job.emit("info", {"msg": f"{len(parsed.invalid)} 行解析失败，已跳过"})

        job.emit("info", {
            "msg": f"待探测 {len(all_rows)} 个凭据"
                   f"（每个凭据四段各探一遍，段级并发）"
        })

        # 创建 Prober
        prober = Prober(
            proxy=_resolve_proxy(str(job.opts.get("proxy") or "")),
            gap=float(job.opts.get("gap", 3.0)),
            timeout=int(job.opts.get("timeout", 120)),
            probe_context=bool(job.opts.get("probe_context", True)),
            swap_samples=int(job.opts.get("swap_samples", 3)),
            workers=int(job.opts.get("workers", 4)),
            max_models=int(job.opts.get("max_models", 4)),
            max_model_attempts=int(job.opts.get("max_model_attempts", 10)),
            reuse_profile_verdict=bool(
                job.opts.get("reuse_profile_verdict", True)),
            on_event=job.emit,
        )

        # 使用 BatchProber（站级并发）
        max_workers = int(job.opts.get("max_workers", 30))
        batch_prober = BatchProber(prober, max_workers=max_workers)

        job.emit("info", {"msg": f"开始批量探测（{max_workers} 站并发）"})

        # 进度回调
        def progress_cb(current, total, site, stats):
            job.emit("progress", {
                "msg": f"探测进度：{current}/{total}",
                "current": current,
                "total": total,
                "site": site,
                "success": stats["success"],
                "partial": stats["partial"],
                "failure": stats["failure"],
            })

        # 批量探测
        results_dict = batch_prober.probe_batch(all_rows, progress_callback=progress_cb)

        # 转换为列表（按原顺序）
        with job.lock:
            job.results = [results_dict.get(row.bare) for row in all_rows]

        job.emit("info", {"msg": f"探测完成：{batch_prober._stats['success']} 成功，{batch_prober._stats['partial']} 部分通，{batch_prober._stats['failure']} 失败"})

        job.state = "done"
    except Exception:
        job.state = "error"
        job.error = traceback.format_exc(limit=4)
        job.emit("error", {"msg": job.error})
    finally:
        job.finished = time.time()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _same_secret(a: str, b: str) -> bool:
    """常量时间比较两个密文。非 ASCII 安全。

    为什么不直接用 hmac.compare_digest(a, b)：传 str 时它要求两边都是
    ASCII，否则抛 TypeError ——
        TypeError: comparing strings with non-ASCII characters is not supported
    而 CPA 的管理密码完全可能含中文或其他非 ASCII 字符。抛异常会变成 500，
    看起来像服务坏了，而不是「密码不对」。

    先各自 encode 成 bytes 再比 —— bytes 路径没有这个限制。
    """
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    server_version = "cpa-upstream-importer/1.0"
    cfg_path = ""
    # CLIProxyAPI 源码根。给了就能精确核对画像梯是否随 CPA 升级而过期
    # （见 cpa_source_probe）。容器里默认没有 —— 只挂了 config.yaml。
    cpa_source_root = ""
    # 允许从 GitHub 直接拉那两个 Go 文件做漂移检测（只读、约 110KB、缓存 6h）。
    # 适合「VPS 上只有 compose + config + env + nginx 四个文件」的部署 ——
    # 不需要源码目录、不需要 git。默认开：它只出网读公开源码，不传任何本地数据。
    cpa_source_remote = True
    cpa_source_ref = "main"
    # 拉 GitHub 用的代理。国内 VPS 直连 raw.githubusercontent 常不通，
    # 而这台机器上通常已经有 mihomo —— 复用它。
    drift_proxy = ""
    token = ""
    # 容器里 config.yaml 是单文件挂载，同目录不可写 —— 备份要落到另一个卷
    backup_dir = ""
    # 是否允许用 CPA 的 remote-management.secret-key 当凭据登录。
    # 开启后「能进 CPA 后台的人」就能进投喂台，不必另记一个 token ——
    # 这两把钥匙的权限本来就等价（都能改写 config.yaml），分开记只是负担。
    accept_cpa_key = True
    # CPA 管理端点地址。容器内用服务名（compose 里 CPA_UPSTREAM_URL 已设
    # http://cli-proxy-api:8317），宿主机跑用 http://127.0.0.1:8317。
    # 写回后要主动 PUT 到这里让 CPA 重载 —— 它的 fsnotify 收不到
    # 单文件 bind mount 的外部写入（见 writeback.reload_cpa 的说明）。
    cpa_url = ""

    # 失败封锁：与 CPA 自己的口径一致（handler.go:301-302，5 次 / 30 分钟）。
    # 投喂台的凭据等价于 CPA 写权限，不能给在线暴破留缺口。
    MAX_FAILURES = 5
    BAN_SECONDS = 30 * 60
    _failures: dict[str, dict] = {}
    _fail_lock = threading.Lock()

    # ---- 基础设施 ----

    def log_message(self, fmt: str, *args) -> None:
        # 不记 query string —— token 可能在里面
        path = self.path.split("?")[0]
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.command} {path} "
                         f"{fmt % args if args else ''}\n")

    @classmethod
    def _cpa_mgmt_hash(cls) -> str:
        """从 config.yaml 读 CPA 管理密钥的 bcrypt 哈希。每次读盘 —— 它可能被热改。

        CPA 首次加载 config.yaml 时会把明文密钥 bcrypt 化并写回
        （`config_load.go:104-113`），所以磁盘上正常是 `$2a$...` 形态。
        用户输入的是**原始密码**，这里做 bcrypt 比对，不是字符串相等。

        只读这一个字段、不缓存：管理密钥换了之后旧密码应立刻失效。
        读失败一律返回空串（等于这条登录路径关闭），绝不因为读不到就放行。
        """
        if not cls.accept_cpa_key or not cls.cfg_path:
            return ""
        try:
            import yaml
            cfg = yaml.safe_load(io.open(cls.cfg_path, encoding="utf-8").read())
            rm = (cfg or {}).get("remote-management") or {}
            h = str(rm.get("secret-key") or "")
            return h if h.startswith(("$2a$", "$2b$", "$2y$")) else ""
        except Exception:
            return ""

    @classmethod
    def _cpa_client_key(cls) -> str:
        """从 config.yaml 读一个 CPA **客户端入口** Key（api-keys 之一）。

        用途只有一个：写回并重载之后，替用户打一次 CPA 自己的业务端点，
        确认新上游经 CPA 真的能出活。

        为什么要自动读，而不是让用户填
        ------------------------------
        「重载成功」只证明 CPA 收下了这份 YAML，证明不了客户端能用 ——
        直连 200 而经 CPA 换模是实测存在的情形（atlas 第 12 章）。这一层
        验证是**唯一**能发现那种分叉的手段，可它却挂在一个需要用户去
        config.yaml 里翻 api-keys 的输入框上，于是默认永远被跳过。
        本服务已经在读同一个文件（上游 Key、管理哈希都在里面），
        没有理由让用户手工搬运其中一个字段。

        安全边界：这个值**只在服务端使用**，绝不放进任何 JSON 响应，
        也绝不回填到前端输入框 —— 那等于把 CPA 的入口凭据递给浏览器。

        读失败返回空串（等于跳过验证），绝不因为读不到就假装验证过。
        """
        if not cls.cfg_path:
            return ""
        try:
            import yaml
            cfg = yaml.safe_load(io.open(cls.cfg_path, encoding="utf-8").read())
            keys = (cfg or {}).get("api-keys") or []
            for k in keys:
                s = str(k or "").strip()
                if s:
                    return s
        except Exception:
            return ""
        return ""

    @classmethod
    def _check_cpa_password(cls, provided: str) -> bool:
        """bcrypt 比对。没有 bcrypt 库时这条路径直接关闭，不退化成明文比较。"""
        h = cls._cpa_mgmt_hash()
        if not h or not provided:
            return False
        try:
            import bcrypt
        except ImportError:
            return False
        try:
            return bcrypt.checkpw(provided.encode("utf-8"), h.encode("utf-8"))
        except Exception:
            return False

    def _client_ip(self) -> str:
        return (self.client_address or ("?",))[0]

    @classmethod
    def _locked_out(cls, ip: str) -> float:
        """返回该 IP 还需等待的秒数；0 表示未封。

        与 CPA 自己的口径一致（handler.go:301-302）：5 次失败封 30 分钟。
        投喂台的凭据等价于 CPA 写权限，不能给在线暴破留缺口。
        """
        with cls._fail_lock:
            info = cls._failures.get(ip)
            if not info:
                return 0.0
            until = info.get("until", 0.0)
            if not until:
                # 尚未封锁。这里**绝不能**碰 count —— _authed 每次都调本方法，
                # 顺手清零会让失败计数永远回到 0，封锁永不触发（实测踩过）。
                return 0.0
            left = until - time.time()
            if left <= 0:
                # 封锁期已过：解封并重新计数
                info["until"] = 0.0
                info["count"] = 0
                return 0.0
            return left

    @classmethod
    def _note_failure(cls, ip: str) -> None:
        with cls._fail_lock:
            info = cls._failures.setdefault(ip, {"count": 0, "until": 0.0})
            info["count"] += 1
            if info["count"] >= cls.MAX_FAILURES:
                info["until"] = time.time() + cls.BAN_SECONDS
                info["count"] = 0

    @classmethod
    def _note_success(cls, ip: str) -> None:
        with cls._fail_lock:
            cls._failures.pop(ip, None)

    def _authed(self) -> bool:
        got = ""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            got = auth[7:].strip()
        if not got:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got = (q.get("token") or [""])[0]
        if not got:
            return False

        ip = self._client_ip()
        if self._locked_out(ip) > 0:
            return False

        own = type(self).token
        # 两条路径：服务自己的 token（等长常量时间比较），或 CPA 管理密码（bcrypt）。
        ok = _same_secret(got, own)
        if not ok:
            ok = self._check_cpa_password(got)

        if ok:
            self._note_success(ip)
        else:
            self._note_failure(ip)
        return ok

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 8 * 1024 * 1024:
            raise ValueError("请求体过大（上限 8MB）")
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"JSON 解析失败：{e}") from e

    def _static(self, rel: str) -> None:
        # 防目录穿越。
        #
        # 为什么不能用 startswith 比前缀（2026-08-31 自查发现的真实穿越）：
        # 原实现是 `abspath(full).startswith(abspath(STATIC))`，那是**字符串**
        # 前缀比较，不是路径边界比较。normpath 不会消掉开头的 `..`（前面没东西
        # 可抵消），lstrip 只去掉开头的分隔符字符、不去掉 `..` 这个路径段，
        # 于是 `../web.bak/leak.txt` 原样留下，而 `/app/web.bak` 这个字符串
        # 确实以 `/app/web` 开头 —— 检查通过，文件被读出去。
        # 兄弟目录只要以 web 开头就中：web.bak / web-old / web2 / webhooks。
        # 这个路由是**免鉴权**的，等于任意人可读那些目录里的文件。
        # Linux（正斜杠）才触发；Windows 上 `\` 也算分隔符，被 lstrip 削掉了。
        #
        # 改用 commonpath：它按**路径段**比较，`/app/web.bak` 与 `/app/web`
        # 的公共前缀是 `/app`，不等于 STATIC，直接拒。
        root = os.path.abspath(STATIC)
        full = os.path.abspath(os.path.join(root, os.path.normpath(rel).lstrip("\\/")))
        try:
            inside = os.path.commonpath([full, root]) == root
        except ValueError:
            inside = False          # 跨盘符（Windows）时 commonpath 会抛
        if not inside:
            self._json(403, {"error": "路径越界"})
            return
        if not os.path.isfile(full):
            self._json(404, {"error": f"找不到 {rel}"})
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        data = open(full, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "javascript" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        p = urllib.parse.urlparse(self.path)
        route = p.path.rstrip("/") or "/"

        # 首页不校验 token —— 页面本身没有秘密，API 才校验。
        # 这样用户可以先打开页面再粘 token。
        if route == "/":
            self._static("index.html")
            return
        if route.startswith("/static/"):
            self._static(route[len("/static/"):])
            return

        if not self._authed():
            self._json(401, {"error": "缺少或错误的 token"})
            return

        if route == "/api/context":
            self._api_context()
        elif route.startswith("/api/job/"):
            jid = route[len("/api/job/"):]
            since = int((urllib.parse.parse_qs(p.query).get("since") or ["0"])[0])
            self._api_job(jid, since)
        else:
            self._json(404, {"error": f"未知路由 {route}"})

    def do_POST(self) -> None:  # noqa: N802
        route = urllib.parse.urlparse(self.path).path.rstrip("/")
        if not self._authed():
            self._json(401, {"error": "缺少或错误的 token"})
            return
        try:
            body = self._body()
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return

        try:
            if route == "/api/parse":
                self._api_parse(body)
            elif route == "/api/diag":
                self._api_diag(body)
            elif route == "/api/probe":
                self._api_probe(body)
            elif route == "/api/plan":
                self._api_plan(body)
            elif route == "/api/apply":
                self._api_apply(body)
            else:
                self._json(404, {"error": f"未知路由 {route}"})
        except Exception:
            self._json(500, {"error": "服务内部错误",
                             "trace": traceback.format_exc(limit=4)})

    # ---- 端点实现 ----

    # config.yaml 的解析缓存。
    #
    # 为什么值得缓存：实测 yaml.safe_load 这份 857 KB / 14900 行的文件要
    # **352 毫秒**，是整个服务里最贵的一次 CPU 操作。
    #
    # 缓存键是**内容哈希**，不是 (mtime_ns, size)。自查（2026-08-30）发现
    # 只看元数据有两个漏洞：
    #
    #   ① 同秒同大小的修改会漏判。虽然 mtime_ns 是纳秒精度，但某些文件系统
    #      （NFS、部分容器 overlay）的实际粒度粗得多；而这个文件恰恰会被
    #      别处改（CPA 自己的 PUT、用户手工编辑、另一个投喂台实例）。
    #      漏判的后果是写回建立在过期基线上 —— 比慢 352 毫秒严重得多。
    #
    #   ② 更要紧的：write_local 是**就地 O_TRUNC 覆写**（inode 不能换，
    #      见 writeback.write_local 的说明），写入过程中文件先被截为 0
    #      再增长。此时并发读会拿到**半截内容**，而 os.stat 已经报出新的
    #      mtime —— 元数据键会把这个中间态当成「新版本」缓存下来。
    #      Windows 上有文件锁掩盖，Linux（VPS 实际环境）没有。
    #
    # 内容哈希把两个漏洞一起解决：读到什么就哈希什么，不完整的内容
    # 哈希也不同，不会被误认为某个已知版本；而且**语法校验兜底** ——
    # 半截 YAML 几乎必然解析失败，那时直接重读而不是缓存一份坏数据。
    _cfg_cache: dict | None = None       # {"sig", "raw", "cfg"}
    _cfg_cache_lock = threading.Lock()

    # 写回串行锁。**不能用 _cfg_cache_lock 顶替** —— 那把锁在 _load_cfg 里
    # 也拿，而写回流程内部会读配置，同一把非重入锁会自锁死。
    #
    # 为什么必须有（2026-08-31 自查发现的 TOCTOU）：服务跑在
    # ThreadingHTTPServer 上，_api_apply 的「读文件 → 比基线 → 校验 → 写盘」
    # 四步没有任何互斥。两个 apply 并发进来时，两边的读+比基线都在任一次写盘
    # **之前**完成，于是双方都看到 raw_now == base_raw、都判定基线有效，
    # 然后依次写盘 —— 后写的那次覆盖掉前一次的全部改动，且给前一个客户端
    # 回的仍是 200 + written。实测复现：A 插的条目在最终文件里彻底消失。
    #
    # 顺序执行不会有这个问题（第二次的基线比对会正确地 409），所以这**只**
    # 是并发缺陷，不是逻辑缺陷 —— 也正因如此，顺序跑的测试抓不到它。
    _apply_lock = threading.Lock()

    def _load_cfg(self) -> tuple[str, dict]:
        """读并解析 config.yaml。返回 (原文, 解析结果)。

        返回的 cfg 是**缓存里的同一个对象**，调用方绝不能原地改它 ——
        改了会污染其他并发请求看到的配置。当前所有调用方（build_band /
        build_plan / existing_fingerprints）都只读，已核查。
        """
        import hashlib
        import yaml

        path = type(self).cfg_path

        # 先看元数据能不能快速否掉缓存 —— 变了就一定要重读，
        # 没变也仍要读一次内容确认（成本是一次 read，比 yaml 解析便宜两个数量级）。
        raw = io.open(path, encoding="utf-8").read()
        sig = hashlib.sha256(raw.encode("utf-8")).hexdigest()

        with Handler._cfg_cache_lock:
            c = Handler._cfg_cache
            if c is not None and c["sig"] == sig:
                return c["raw"], c["cfg"]

        cfg = yaml.safe_load(raw)
        # 半截文件通常在这里就炸了；万一它恰好是合法 YAML 但结构不对，
        # 顶层非映射也说明读到的不是完整配置 —— 两种都不缓存。
        if not isinstance(cfg, dict):
            raise ValueError(
                f"{path} 顶层不是映射（可能读到了写入中的半截文件）")

        with Handler._cfg_cache_lock:
            Handler._cfg_cache = {"sig": sig, "raw": raw, "cfg": cfg}
        return raw, cfg

    def _api_context(self) -> None:
        """当前 config.yaml 的档位谱与规模 —— 前端据此显示插档基准。"""
        raw, cfg = self._load_cfg()
        bands = {}
        for s in cp.SECTIONS:
            # 传 raw：定档要读注释里的「实测不可用」结论。不传的话会把死站
            # 当活站保护，把可用新站压到最低档（2026-08-30 实测到的缺陷）。
            b = cp.build_band(cfg, s, raw=raw)
            dead = sorted(b.dead_hosts)
            unhealthy = sorted(
                h for h in b.hosts_at.get(b.top, []) + [
                    x for v in b.hosts_at.values() for x in v]
                if h.lower() not in b.dead_hosts
                and b.unhealthy_hosts
                and cp.host_matches_note(h.lower(), b.unhealthy_hosts, b.alias))
            bands[s] = {
                "tiers": b.tiers,
                "top": b.top,
                "hosts_at": {str(k): v for k, v in b.hosts_at.items()},
                "gaps": b.gaps(),
                "entries": len(cfg.get(s) or []),
                # 让前端能标出「这一档全是死站」—— 用户看到 465 挡了 9 个站
                # 会紧张，看到那 9 个全是实测不可用的就不会。
                "dead_hosts": dead,
                "unhealthy_hosts": sorted(set(unhealthy)),
                # 注释里提到但匹配不上任何现有站的短名。**静默漏判的可见化** ——
                # 别名表只能从 compat 段的 name 字段建，另三段没有 name 字段。
                # 非空说明那几个站的「实测不可用」结论没作用到定档上。
                "unmatched_notes": b.unmatched_notes,
            }

        # 计算既有站总数（用于全量重探提示）
        existing_entries = cp.extract_existing_entries(cfg)
        existing_count = len(existing_entries)

        # 运行环境与推荐并发数。前端要显示「为什么是这个数」，所以连
        # 依据（cpus/memory/来源/reason）一起给，不只给一个数字。
        # 容器里 os.cpu_count() 是宿主机核数，必须读 cgroup —— 见 resources 模块。
        res = cp.detect_resources()

        # 画像基线漂移：CPA 升级换了默认头而画像梯没跟上时，探测发的形态就与
        # CPA 实际转发的不一致 —— 那会让「探测通了但 CPA 不通」或反之。
        # 优先读 CPA 源码（能区分有条件/无条件 beta），读不到退回 config.yaml
        # 的 header-defaults。两条都不成立时明确报「无法核对」，不假装检查过。
        drift = cp.check_profile_drift(
            source_root=type(self).cpa_source_root, cfg=cfg,
            runtime_commit=_cpa_runtime_commit(type(self).cpa_url),
            allow_remote=type(self).cpa_source_remote,
            remote_ref=type(self).cpa_source_ref,
            proxy=type(self).drift_proxy or None)

        self._json(200, {
            "config_path": type(self).cfg_path,
            "lines": raw.count("\n") + 1,
            "bytes": len(raw.encode("utf-8")),
            "sections": bands,
            "section_order": list(cp.SECTIONS),
            "existing_count": existing_count,
            "resources": res.as_dict(),
            "profile_drift": drift,
        })

    def _api_parse(self, body: dict) -> None:
        res = cp.parse_lines(body.get("text") or "")
        self._json(200, {
            "valid": [row_json(r) for r in res.valid],
            "invalid": [row_json(r) for r in res.invalid],
        })

    def _api_diag(self, body: dict) -> None:
        """单站诊断：只跑画像梯，回答「这个站要什么头」。

        与 /api/probe 的区别是**意图不同**，不是参数不同：
          · /api/probe  为导入服务 —— 探完要生成方案、要写回
          · /api/diag   为排障服务 —— 只回答一个问题，不产生任何可写状态

        所以它不建 Job、不进 STORE、不能被 /api/plan 引用。想导入的话，前端把
        结果预填回步骤①走正常流水线 —— 诊断与写回之间必须有人工确认这一跳。

        同步返回（不走轮询）：单段 3-8 次请求、几秒内完成，为它引入一套任务
        状态不值得。四段全查才 25 次，也在可接受范围。
        """
        res = cp.parse_lines(f"{body.get('url') or ''},{body.get('key') or ''}")
        if not res.valid:
            why = res.invalid[0].error if res.invalid else "url 或 key 为空"
            self._json(400, {"error": f"解析不了：{why}"})
            return
        row = res.valid[0]

        want = str(body.get("section") or "").strip()
        secs = [want] if want in cp.SECTIONS else list(cp.SECTIONS)

        raw, cfg = self._load_cfg()
        prober = Prober(
            proxy=_resolve_proxy(str(body.get("proxy") or "")),
            gap=float(body.get("gap", 0.5)),
            timeout=int(body.get("timeout", 60)),
            probe_context=False,       # 诊断不探上下文 —— 那是百万字符的大 body
            swap_samples=0,            # 也不采样换模，那要 3 次额外请求
            workers=len(secs),
            cfg_snapshot=cfg,
        )

        out: dict[str, dict] = {}
        for section in secs:
            base = cp.base_for_section(row.bare, section)
            model = SEED_MODELS[section][0]
            rungs: list[dict] = []
            hit: dict | None = None

            for prof in cp.profiles.ladder(section, cfg):
                hdrs, patch = cp.profiles.materialize(prof, row.api_key)
                att = prober._call(section, base, row.api_key, model,
                                   combo=prof.name,
                                   extra_headers=hdrs or None,
                                   body_patch=patch or None)
                rungs.append({
                    "profile": prof.name, "tier": prof.tier,
                    "family": prof.family, "alt": prof.alt,
                    "why": prof.why,
                    "status": att.status, "category": att.category,
                    "elapsed_ms": att.elapsed_ms,
                    "excerpt": att.excerpt,
                    "resp_model": att.resp_model,
                    "headers": hdrs, "body_patch": bool(patch),
                    "ok": att.ok and not att.error_envelope,
                })
                if att.ok and not att.error_envelope:
                    hit = rungs[-1]
                    break

            out[section] = {
                "base_url": base,
                "model": model,
                "rungs": rungs,
                "hit": hit,
                # 写进 config.yaml 的形态 —— baseline 通过时不需要任何 headers
                "needed_headers": (hit["headers"] if hit and hit["profile"] != "baseline"
                                   else {}),
                "needs_body": bool(hit and hit["body_patch"]),
                "calls": len(rungs),
            }

        self._json(200, {
            "host": row.host,
            "key_masked": row.masked(),
            "sections": out,
            "total_calls": sum(v["calls"] for v in out.values()),
        })

    def _api_probe(self, body: dict) -> None:
        full_redetect = body.get("full_redetect", False)
        max_workers = body.get("max_workers")

        res = cp.parse_lines(body.get("text") or "")
        if not res.valid and not full_redetect:
            self._json(400, {"error": "没有可用行",
                             "invalid": [row_json(r) for r in res.invalid]})
            return

        opts = body.get("opts") or {}
        if full_redetect and max_workers is not None:
            opts["max_workers"] = max_workers

        jid = secrets.token_hex(8)
        job = Job(jid, res.valid, opts)
        STORE.add_job(job)

        # 选择执行函数
        target_fn = run_job_full_redetect if full_redetect else run_job
        threading.Thread(target=target_fn, args=(job, type(self).cfg_path),
                         daemon=True).start()

        self._json(202, {"job_id": jid, "rows": len(res.valid),
                         "invalid": [row_json(r) for r in res.invalid],
                         "full_redetect": full_redetect})

    def _api_job(self, jid: str, since: int) -> None:
        job = STORE.get_job(jid)
        if not job:
            self._json(404, {"error": f"没有这个任务：{jid}"})
            return
        snap = job.snapshot(since)
        if job.state in ("done", "error"):
            snap["results"] = [
                {"row": row_json(r.row),
                 "usable_sections": r.usable_sections,
                 "total_calls": r.total_calls,
                 "sections": {s: verdict_json(v) for s, v in r.sections.items()}}
                for r in job.results
            ]
        self._json(200, snap)

    def _api_plan(self, body: dict) -> None:
        """把探测结果变成写入方案 + diff 预览。不落盘。"""
        job = STORE.get_job(body.get("job_id") or "")
        if not job:
            self._json(404, {"error": "任务不存在"})
            return
        if job.state != "done":
            self._json(409, {"error": f"任务状态 {job.state}，还不能定方案"})
            return

        raw, cfg = self._load_cfg()
        overrides = body.get("overrides") or {}     # {host: {section: {...}}}
        selected = body.get("selected")             # [[host, section], ...] 或 None=全选
        # 人工接管：{host: {section: [模型, ...]}}。探测判不可用但操作员确知
        # 可用的段，由他显式给模型清单。见 cp.build_plan 的 force 说明 ——
        # 只绕过 usable 判定，去重/定档/影响面/diff 确认一道都不少。
        forced = body.get("forced") or {}
        # 默认试用期：新站进最低可插档，不因探测满分就把已验证的站挡在其后
        probation = not bool(body.get("by_score"))

        # 检测是否全量重探模式
        is_full_redetect = job.opts.get("full_redetect", False)

        if is_full_redetect:
            # 全量重探模式：使用 rebuild_config_full
            bands: dict = {}
            seen = cp.existing_fingerprints(cfg)
            all_plans = {}  # {(base_url, api_key): ImportPlan}

            for res in job.results:
                fh = forced.get(res.row.host) or {}
                p = cp.build_plan(res.row, res, cfg, bands=bands, seen=seen,
                                  probation=probation,
                                  force={str(k): [str(m) for m in (v or [])]
                                         for k, v in fh.items()} if fh else None)
                all_plans[(res.row.bare, res.row.api_key)] = p

            # 应用用户覆盖
            for (base_url, api_key), p in all_plans.items():
                ov_host = overrides.get(p.host) or {}
                for sec, sp in list(p.sections.items()):
                    ov = ov_host.get(sec) or {}
                    if "priority" in ov:
                        sp.priority = int(ov["priority"])
                        sp.priority_reason = "用户手工指定"
                    if "proxy_url" in ov:
                        sp.proxy_url = str(ov["proxy_url"] or "")
                    if "headers" in ov and isinstance(ov["headers"], dict):
                        sp.headers = {str(k): str(v) for k, v in ov["headers"].items()}
                    if "models" in ov and isinstance(ov["models"], list):
                        sp.models = [str(m) for m in ov["models"]]
                    if "max_context_length" in ov:
                        v = ov["max_context_length"]
                        sp.max_context_length = int(v) if v else None

            # 全量重建
            preview, warnings = cp.rebuild_config_full(cfg, all_plans, raw.splitlines(keepends=True))

            # 生成完整 diff（整个文件）
            diffs = []
            ok, msg = validate(preview)

            pid = secrets.token_hex(8)
            STORE.add_plan(pid, {"plans": list(all_plans.values()), "diffs": diffs,
                                 "preview": preview, "base_raw": raw,
                                 "created": time.time(),
                                 "full_redetect": True})

            self._json(200, {
                "plan_id": pid,
                "plans": [plan_json(p) for p in all_plans.values()],
                "diffs": [{
                    "section": "全量重建",
                    "host": f"{len(all_plans)} 个站",
                    "insert_at": 0,
                    "lines": preview.splitlines(keepends=True),
                    "text": f"全量重建整个 config.yaml\n警告：{len(warnings)} 个\n" + "\n".join(warnings) if warnings else "全量重建整个 config.yaml"
                }],
                "valid": ok,
                "validate_msg": msg,
                "lines_before": raw.count("\n") + 1,
                "lines_after": preview.count("\n") + 1,
                "warnings": warnings,
                "full_redetect": True
            })
            return

        # 原有逻辑：增量模式
        bands: dict = {}
        seen = cp.existing_fingerprints(cfg)
        plans = []
        for res in job.results:
            fh = forced.get(res.row.host) or {}
            p = cp.build_plan(res.row, res, cfg, bands=bands, seen=seen,
                              probation=probation,
                              force={str(k): [str(m) for m in (v or [])]
                                     for k, v in fh.items()} if fh else None)
            plans.append(p)

        # 应用用户覆盖（优先级 / 代理 / 头 / 模型 / 是否写入）
        for p in plans:
            ov_host = overrides.get(p.host) or {}
            for sec, sp in list(p.sections.items()):
                ov = ov_host.get(sec) or {}
                if "priority" in ov:
                    sp.priority = int(ov["priority"])
                    sp.priority_reason = "用户手工指定"
                    band = bands.get(sec) or cp.build_band(cfg, sec, raw=raw)
                    sp.impacts = cp.compute_impact(band, sp.models, sp.priority)
                    sp.warnings = [w for w in sp.warnings if "抢走" not in w]
                    if sp.hijacked:
                        names = ", ".join(i.model for i in sp.hijacked[:4])
                        sp.warnings.append(
                            f"会抢走 {len(sp.hijacked)} 个模型的顶层（{names}）—— 你已手工确认")
                if "proxy_url" in ov:
                    sp.proxy_url = str(ov["proxy_url"] or "")
                if "headers" in ov and isinstance(ov["headers"], dict):
                    sp.headers = {str(k): str(v) for k, v in ov["headers"].items()}
                if "models" in ov and isinstance(ov["models"], list):
                    sp.models = [str(m) for m in ov["models"]]
                if "max_context_length" in ov:
                    v = ov["max_context_length"]
                    sp.max_context_length = int(v) if v else None

        # 选择集过滤
        if selected is not None:
            want = {(str(h), str(s)) for h, s in selected}
            for p in plans:
                for sec in list(p.sections):
                    if (p.host, sec) not in want:
                        p.skipped[sec] = "用户未选择"
                        del p.sections[sec]

        diffs = build_diffs(raw, plans)
        preview = apply_diffs(raw, diffs)
        ok, msg = validate(preview)

        pid = secrets.token_hex(8)
        STORE.add_plan(pid, {"plans": plans, "diffs": diffs,
                             "preview": preview, "base_raw": raw,
                             "created": time.time()})

        self._json(200, {
            "plan_id": pid,
            "plans": [plan_json(p) for p in plans],
            "diffs": [{"section": d.section, "host": d.host,
                       "insert_at": d.insert_at, "lines": d.lines,
                       "text": d.render()} for d in diffs],
            "valid": ok,
            "validate_msg": msg,
            "lines_before": raw.count("\n") + 1,
            "lines_after": preview.count("\n") + 1,
        })

    def _api_apply(self, body: dict) -> None:
        """真正落盘。必须带 plan_id + confirm=true。"""
        pid = body.get("plan_id") or ""
        entry = STORE.get_plan(pid)
        if not entry:
            self._json(404, {"error": "方案不存在或已过期，请重新生成"})
            return
        if not body.get("confirm"):
            self._json(400, {"error": "未确认。写回需要 confirm=true"})
            return

        # 并发保护：文件在生成方案后被改过就拒绝。
        # 读→比基线→校验→写盘必须在**同一把锁内**完成，否则两个并发 apply
        # 会各自比对到同一份未改动的基线、双双通过，然后后写的覆盖先写的
        # （见 _apply_lock 处的说明）。锁只圈到写盘为止 —— 之后的 CPA 重载与
        # 端到端验证要发外网请求、可能几十秒，圈进来会让第二个请求干等。
        with Handler._apply_lock:
            raw_now = io.open(type(self).cfg_path, encoding="utf-8").read()
            if raw_now != entry["base_raw"]:
                self._json(409, {"error": "config.yaml 在此期间已被修改，"
                                          "方案基线失效。请重新生成方案"})
                return

            ok, msg = validate(entry["preview"])
            if not ok:
                self._json(400, {"error": f"预览内容校验不通过，拒绝写入：{msg}"})
                return

            # write_local 内部已备份，别再单独调 backup —— 否则每次写回两个 .bak
            bak = write_local(type(self).cfg_path, entry["preview"],
                              backup_dir=type(self).backup_dir or None)
            # 同一个 plan_id 不能被重放写第二次：基线已经不匹配了，但把它显式
            # 作废更直接 —— 重放会拿旧 base_raw 去比新文件，只是恰好也被 409 挡住。
            entry["base_raw"] = entry["preview"]
            # 显式清缓存。write_local 会改 mtime，(mtime_ns, size) 已经能自动
            # 失效 —— 但依赖那个隐式行为不值得：若将来有人写入同样长度的内容
            # 且文件系统 mtime 精度不够，就会读到旧基线去生成下一个方案。
            with Handler._cfg_cache_lock:
                Handler._cfg_cache = None

        result = {"backup": bak, "written": type(self).cfg_path,
                  "validate_msg": msg,
                  "diffs": len(entry["diffs"])}

        # ── 自动让 CPA 立即生效 ────────────────────────────────────────
        # write_local 就地 O_TRUNC 覆写，inode 不变，所以 cli-proxy-api 容器
        # 能看到新字节，CPA 的 fsnotify 也覆盖这种写入。但那条链没有保证：
        # inotify 事件可能丢，而 CPA **没有轮询兜底**（internal/watcher/
        # 只有 debounce 定时器，没有 Ticker），事件一丢就永远不重载、不自愈。
        #
        # 所以主动推一次 PUT /v0/management/config.yaml：CPA 自己校验、自己
        # 就地落盘，必然产生一次容器内 Write 事件，把「可能丢」换成「必然有」，
        # 并且给出可判断的 HTTP 回执 + 读回校验（见 writeback.reload_cpa）。
        #
        # 密码来源：优先用请求里显式给的；否则复用用户登录本服务时输的那个。
        # 只有当用户是**用 CPA 管理密码登录**时这条才成立 —— 用服务自己的
        # token 登录的话，我们手上没有管理密码，只能走下面的告警路径。
        push = body.get("push") or {}
        # 地址取值：**服务端配置优先**，请求里给的只作为显式覆盖。
        #
        # 为什么不能反过来（实测踩过）：前端那个输入框曾硬编码
        # https://cpa.example.com，于是 PUT 走公网 → Cloudflare 拦成
        # 403 error code 1010（CF 的码，不是 CPA 拒绝配置），
        # 而容器内配好的 cli-proxy-api:8317 永远用不上。
        #
        # 顺序反过来后：留空走服务名直连（既绕开 CF、也不出公网），
        # 只有用户明确填了别的地址才用他填的。
        cpa_base = ((push.get("base") or "").strip()
                    or (type(self).cpa_url or "").strip())
        mgmt = (push.get("mgmt_key") or "").strip()
        if not mgmt:
            cred = (body.get("_cred") or "").strip()
            # 先排除「这就是本服务的 token」，避免为 token 白跑一次 bcrypt
            # （bcrypt 单次约 100ms，且必然不匹配）。
            is_own_token = _same_secret(cred, type(self).token)
            if cred and not is_own_token and self._check_cpa_password(cred):
                mgmt = cred            # 登录用的就是 CPA 管理密码，直接复用

        if cpa_base and mgmt:
            rok, rmsg = reload_cpa(cpa_base, mgmt, entry["preview"])
            result["reload_ok"] = rok
            result["reload_msg"] = rmsg
            result["push_ok"] = rok       # 兼容前端既有字段
            result["push_msg"] = rmsg
        elif cpa_base:
            result["reload_ok"] = False
            result["reload_msg"] = (
                "已写盘，但**未触发 CPA 重载** —— 没有可用的管理密码。\n"
                "CPA 不会自己发现这次改动（单文件挂载 + 无轮询兜底）。\n"
                "两条路：① 用 CPA 后台管理密码重新登录本页，再写回一次；"
                "② 在 VPS 上执行 docker restart cli-proxy-api")
            result["push_ok"] = False
            result["push_msg"] = result["reload_msg"]
        else:
            result["reload_ok"] = False
            result["reload_msg"] = ("已写盘。未配置 CPA 地址（CPA_UPSTREAM_URL），"
                                    "无法自动重载 —— 请 docker restart cli-proxy-api")

        if result.get("reload_ok"):
            # 第二级验证：热重载**之后**打 CPA 自己的业务端点。
            # 重载成功只证明 CPA 接受了这份 YAML，证明不了新上游真能出活 ——
            # 直连 200 而经 CPA 换模是实测存在的情形（atlas 第 12 章）。
            #
            # Key 来源：用户填的优先；没填就自动从 config.yaml 的 api-keys 取。
            # 自动取是默认路径 —— 否则这层验证会因为「要用户去翻配置文件」
            # 而永远被跳过，而它恰恰是唯一能发现「经 CPA 换模」的手段。
            client_key = (push.get("client_key") or "").strip()
            key_src = "用户填写"
            if not client_key:
                client_key = self._cpa_client_key()
                key_src = "自动取自 config.yaml 的 api-keys"
            if client_key:
                # 待验证清单先摊平，再并行打 —— 串行会让这个 HTTP 请求超时。
                #
                # 自查（2026-08-30）：原来是双重 for 串行调用，每次
                # verify_upstream 默认 timeout=120 秒。20 个可写段最坏
                # 20 × 120 = 2400 秒 —— 客户端、nginx、浏览器全都会先断，
                # 而服务端仍在傻跑完整个循环。
                #
                # 三道保护：
                #   · 并行（打的是 CPA 自己的入口，不是上游站 —— 没有
                #     站方限频问题；CPA 内部自会按凭据轮询与冷却）
                #   · 单次 timeout 收到 45 秒（业务请求正常 2-4 秒，
                #     45 秒还不回就是有问题，没必要等满 120）
                #   · 条数上限 24 —— 超出的部分明确报「未验证」，
                #     而不是悄悄少验或把请求拖死
                todo = []
                for plan in entry["plans"]:
                    for sec, sp in plan.sections.items():
                        if not sp.writable or not sp.models:
                            continue
                        todo.append((plan.host, sec, sp.models[0]))

                MAX_VERIFY = 24
                skipped_over = todo[MAX_VERIFY:]
                todo = todo[:MAX_VERIFY]

                verified = [None] * len(todo)

                def _one(i: int, host: str, sec: str, model: str) -> None:
                    vok, vmsg = verify_upstream(
                        cpa_base, client_key, sec, model, timeout=45,
                    )
                    verified[i] = {"host": host, "section": sec,
                                   "model": model, "ok": vok, "msg": vmsg}

                if len(todo) > 1:
                    with concurrent.futures.ThreadPoolExecutor(
                            max_workers=min(6, len(todo)),
                            thread_name_prefix="verify") as ex:
                        futs = [ex.submit(_one, i, h, sc, mo)
                                for i, (h, sc, mo) in enumerate(todo)]
                        for f in futs:
                            try:
                                f.result()
                            except Exception as e:      # noqa: BLE001
                                pass                    # 下面统一补空位
                elif todo:
                    try:
                        _one(0, *todo[0])
                    except Exception:                   # noqa: BLE001
                        pass

                # 抛异常的位置补成明确的失败项，不留 None
                for i, (h, sc, mo) in enumerate(todo):
                    if verified[i] is None:
                        verified[i] = {"host": h, "section": sc, "model": mo,
                                       "ok": False, "msg": "验证请求本身失败（超时或连接错误）"}
                if skipped_over:
                    result["verify_over_limit"] = (
                        f"另有 {len(skipped_over)} 个条目未验证 —— "
                        f"单次写回最多验 {MAX_VERIFY} 个，避免请求超时。"
                        f"它们已写入 config.yaml，可稍后单独验证")
                result["verified"] = verified
                result["verify_failed"] = [v for v in verified if not v["ok"]]
                # 只报来源，绝不报值 —— 这是 CPA 的入口凭据
                result["verify_key_src"] = key_src
                if not verified:
                    result["verify_skipped"] = (
                        "没有可验证的条目 —— 本次写入的段都没有可用模型")
            else:
                result["verify_skipped"] = (
                    "CPA 已重载成功，但跳过了端到端验证：config.yaml 的 "
                    "api-keys 为空，且未手工填写客户端 Key。\n"
                    "缺这一层意味着：现在只知道 CPA 收下了配置，"
                    "不知道客户端打过来时新上游会不会被换模或拒绝。"
                )
        self._json(200, result)


def main() -> None:
    ap = argparse.ArgumentParser(prog="upstream-importer-server")
    ap.add_argument("--config", default="config.yaml", help="config.yaml 路径")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址。默认只本机；改 0.0.0.0 前请先加 nginx + TLS + 认证")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default=os.environ.get("IMPORTER_TOKEN", ""),
                    help="Bearer token。不给则随机生成并打印")
    ap.add_argument("--backup-dir", default=os.environ.get("IMPORTER_BACKUP_DIR", ""),
                    help="备份目录。容器里 config.yaml 是单文件挂载、同目录不可写，"
                         "必须指到另一个卷（compose 里已设 /backups）")
    ap.add_argument("--cpa-url",
                    default=os.environ.get("CPA_UPSTREAM_URL",
                                           "http://127.0.0.1:8317"),
                    help="CPA 管理端点。写回后自动 PUT 到这里触发重载 —— "
                         "CPA 的 fsnotify 收不到单文件挂载的外部写入。"
                         "容器内默认取 CPA_UPSTREAM_URL（compose 已设服务名）")
    ap.add_argument("--cpa-source",
                    default=os.environ.get("CPA_SOURCE_ROOT", ""),
                    help="CLIProxyAPI 源码根目录。给了才能精确核对画像梯是否"
                         "随 CPA 升级过期；不给则退回读 config.yaml 的 "
                         "claude-header-defaults（覆盖面小）")
    ap.add_argument("--no-drift-remote", action="store_true",
                    help="禁止从 GitHub 拉 CPA 源码做漂移检测。默认允许 —— "
                         "只读公开源码、不传任何本地数据、缓存 6 小时")
    ap.add_argument("--drift-ref", default=os.environ.get("CPA_SOURCE_REF", "main"),
                    help="拉哪个 ref 的源码。你运行的 CPA 不是最新版时，"
                         "指到对应 tag（如 v7.2.0）才能得到有意义的比对")
    ap.add_argument("--drift-proxy", default=os.environ.get("DRIFT_PROXY", ""),
                    help="拉 GitHub 用的代理（如 http://mihomo:7890）。"
                         "国内 VPS 直连 raw.githubusercontent 常不通")
    ap.add_argument("--no-cpa-key", action="store_true",
                    help="不接受 CPA 管理密钥登录，只认本服务的 token")
    args = ap.parse_args()

    cfg = os.path.abspath(args.config)
    if not os.path.isfile(cfg):
        sys.exit(f"找不到 config.yaml：{cfg}")
    if not os.path.isdir(STATIC):
        sys.exit(f"找不到前端目录：{STATIC}")

    token = args.token or secrets.token_hex(16)
    Handler.cfg_path = cfg
    Handler.token = token
    Handler.backup_dir = args.backup_dir
    Handler.accept_cpa_key = not args.no_cpa_key
    Handler.cpa_url = args.cpa_url
    Handler.cpa_source_root = args.cpa_source
    Handler.cpa_source_remote = not args.no_drift_remote
    Handler.cpa_source_ref = args.drift_ref
    Handler.drift_proxy = args.drift_proxy

    cpa_hash = Handler._cpa_mgmt_hash()
    try:
        import bcrypt as _bcrypt   # noqa: F401
        has_bcrypt = True
    except ImportError:
        has_bcrypt = False

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 68)
    print("CPA 上游批量导入服务 · 投喂台")
    print("=" * 68)
    print(f"  config.yaml : {cfg}")
    print(f"  监听        : http://{args.host}:{args.port}")
    print(f"  token       : {token}")
    print(f"  打开        : http://{args.host}:{args.port}/?token={token}")
    if args.backup_dir:
        print(f"  备份目录    : {args.backup_dir}")
    if Handler.accept_cpa_key:
        if cpa_hash and has_bcrypt:
            print("  也可用 CPA 后台的管理密码登录（输原始密码，不是 config.yaml")
            print("    里那串 $2a$ 哈希）—— 两把钥匙权限等价，不必另记")
        elif cpa_hash and not has_bcrypt:
            print("  ! 未安装 bcrypt，CPA 密码登录已关闭。要启用：")
            print("      dnf install -y python3-bcrypt")
        else:
            print("  ! config.yaml 里 remote-management.secret-key 不是 bcrypt 形态，")
            print("    只能用上面这个 token 登录")
    else:
        print("  已禁用 CPA 密码登录（--no-cpa-key）")
    print(f"  失败封锁    : {Handler.MAX_FAILURES} 次 / "
          f"{Handler.BAN_SECONDS // 60} 分钟（按来源 IP）")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print()
        print("  ⚠ 非本机监听。这个服务持有明文上游 Key 且能改写 config.yaml，")
        print("    请确保前面有 nginx（TLS + 访问控制），不要直接暴露到公网。")
    print("=" * 68)
    print("  Ctrl-C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
