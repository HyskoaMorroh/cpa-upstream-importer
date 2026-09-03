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
import copy
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
from cpa_probe.batch import (  # noqa: E402
    BatchProber, existing_model_context, existing_model_extras,
    existing_prefixes, existing_provider_names, existing_proxies,
    existing_weights,
    extract_existing_entries,
)
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
        # 每个工作单元完成的时刻（相对 started 的秒数）。
        self.unit_done: list[float] = []
        # 在飞单元：{站名: 起始时刻}。用来回答「现在卡在谁身上」——
        # 这是操作员真正需要的信息，见 _progress 的说明。
        self.unit_flight: dict[str, float] = {}
        # 工作单元总数。普通探测=行数；全量重探=去重后的凭据数，由那条
        # 路径在算出来之后回填（它开始时还不知道会有多少个凭据）。
        self.unit_total: int = len(rows)
        # 实际并发度。**决定 ETA 给不给**，见 _ETA_MAX_WORKERS 的说明。
        # 两条路径的默认值差 7.5 倍（普通 4 / 全量重探 30），各自回填。
        self.workers: int = 1

    def emit(self, kind: str, data: dict) -> None:
        with self.lock:
            self.events.append({"t": round(time.time() - self.started, 1),
                                "kind": kind, **data})
            if kind == "attempt":
                self.calls += 1

    def mark_unit_start(self, name: str) -> None:
        """一个工作单元开始。name 必须是**脱敏**的站名，不能带 api_key。"""
        with self.lock:
            self.unit_flight[name] = time.time() - self.started

    def mark_unit_done(self, name: str | None = None, n: int = 1) -> None:
        """记 n 个工作单元完成。两条探测路径都要调，否则那条路没有进度。"""
        now = time.time() - self.started
        with self.lock:
            self.unit_done.extend([now] * n)
            if name is not None:
                self.unit_flight.pop(name, None)

    # ETA 的最小样本数。少于这个数不给任何秒数 —— 宁可显示「估算中」，
    # 也不给一个必然错的数字：先报 2 分钟后来变 8 分钟会让人做错决定。
    _ETA_MIN_SAMPLES = 5

    # 并发度上限。超过它就**不给 ETA**，只给吞吐率与在飞跟踪。
    #
    # 为什么必须按并发闸（2026-09-01 跨 12 种子 × 11 档并发回放）：
    #     并发   平均误差   区间命中
    #      1       67%       94%
    #      4       55%       74%     ← 普通探测默认，可用
    #      8       70%       51%
    #     30       92%        9%     ← 全量重探默认，完全不可用
    #
    # 高并发下剩余墙钟被「在飞最长的那个还需多久」主导（并发 30、进度
    # 40/79 时占比 100%），而那个值在它结束前无法从已完成的数据推出 ——
    # 不是算法不够好，是信息本身不在样本里。
    #
    # 取 4：命中率 74% 是「三次里对两次多」，勉强够用来安排下一件事；
    # 6 档降到 60%、8 档 51%，那种数字给了等于误导。
    _ETA_MAX_WORKERS = 4

    # 区间的分位。下界取 p25、上界取 p99。
    #
    # 为什么上界要到 p99 而不是 p90（2026-09-01 跨 12 组随机种子 × 3 档
    # 离散度回放验证）：单凭据代价是重尾分布（实测 p50=6 次请求、p90=42、
    # max=293，49 倍差）。区间命中率 p90 只有 31%，p95 是 68%，p99 才到
    # 91%-97%。一个 3 次里错 2 次的区间不如不给。
    _ETA_Q_LO = 0.25
    _ETA_Q_HI = 0.99

    def _progress(self, total: int) -> dict:
        """进度度量：ETA 区间、吞吐率、在飞站跟踪。

        点值用**全量累计均值**而不是最近窗口的吞吐（2026-09-01 回放验证）
        --------------------------------------------------------------
        直觉上「最近窗口」更能反映当下速度，实测反过来：

            估计器        离散度σ    平均误差   区间命中
            最近窗口       0.8/1.4/2.0   35/81/199%   81/65/51%
            累计均值+分位   0.8/1.4/2.0   30/67/157%   97/94/91%

        原因是各单元完成顺序与代价无关（随机顺序），此时累计均值是总体均值
        的无偏估计；而窗口平均会被恰好落在窗口里的一个慢站整体带偏，越到
        后期波动越大 —— 表现出来就是 ETA 在几次轮询之间大幅跳动。

        区间用经验分位而不是 ±标准差：代价是重尾分布（p50=6 次请求、
        p90=42、max=293），标准差被极值撑爆，反而给不出有效上界。
        """
        done = list(self.unit_done)
        flight = dict(self.unit_flight)
        now = time.time() - self.started
        out: dict = {"unit_done": len(done), "unit_total": total,
                     "in_flight": len(flight)}

        # 在飞最久的那个 —— 判断「是不是卡住了」只需要这一个数字。
        if flight:
            name, t0 = min(flight.items(), key=lambda kv: kv[1])
            out["slowest_host"] = name
            out["slowest_age"] = round(now - t0, 1)

        n = len(done)
        remain = total - n
        if n < self._ETA_MIN_SAMPLES or remain <= 0 or total <= 0:
            return out

        # 相邻完成时刻之差 = 每个单元占用的墙钟。第一个用绝对时刻（从
        # 任务起点算），否则会漏掉启动阶段。
        gaps = sorted([done[0]] + [done[i] - done[i - 1] for i in range(1, n)])
        if not gaps or gaps[-1] <= 0:
            return out

        def q(p: float) -> float:
            return gaps[min(int(len(gaps) * p), len(gaps) - 1)]

        mean = sum(gaps) / len(gaps)
        # 吞吐率与样本数任何并发下都给 —— 它们是实测量，不是外推。
        out["rate_per_min"] = round(60.0 / mean, 1) if mean > 0 else None
        out["samples"] = n

        # ETA 只在低并发下给。高并发时剩余时间被在飞的慢单元主导，
        # 外推出来的数字命中率不到 10%（见 _ETA_MAX_WORKERS）。
        if self.workers > self._ETA_MAX_WORKERS:
            out["eta_suppressed"] = f"并发 {self.workers} 过高，剩余时间无法可靠外推"
            return out

        out.update({
            "eta_sec": round(remain * mean, 1),
            "eta_lo": round(remain * q(self._ETA_Q_LO), 1),
            "eta_hi": round(remain * q(self._ETA_Q_HI), 1),
        })
        return out

    def snapshot(self, since: int = 0) -> dict:
        # _progress 自己要拿锁，所以先在锁外算好再合并 —— 在 with 里调它
        # 会死锁（threading.Lock 不可重入）。
        prog = self._progress(self.unit_total or len(self.rows))
        with self.lock:
            return {
                **prog,
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


class ApplyTask:
    """一次写回的后台收尾。落盘已完成，这里只跟踪重载与验证。

    为什么需要它（2026-09-02 解 Cloudflare 524）：重载 1-3 秒、验证单个最长
    45 秒，79 凭据那种规模累计破 100 秒，CF 直接切断连接返回 524 —— 而任务
    其实成功了。落盘同步做完给确定回执，剩下的丢后台，前端轮询进度。
    """

    def __init__(self, task_id: str, base_result: dict):
        self.id = task_id
        self.state = "running"          # running | done | error
        self.result = dict(base_result)  # 落盘阶段的结果，后续逐步补字段
        self.error = ""
        self.started = time.time()
        self.finished = 0.0
        # 阶段进度。写回没有「79 个单元」那种自然分片，能给准的是**阶段**
        # 与验证的 已完成/总数 —— 那两个都是实测量。
        self.stage = "写盘完成"
        self.verify_total = 0
        self.verify_done = 0
        self.lock = threading.Lock()

    def set_stage(self, stage: str) -> None:
        with self.lock:
            self.stage = stage

    def set_verify_total(self, n: int) -> None:
        with self.lock:
            self.verify_total = n

    def bump_verify(self) -> None:
        with self.lock:
            self.verify_done += 1

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "task_id": self.id,
                "state": self.state,
                "error": self.error,
                "stage": self.stage,
                "verify_total": self.verify_total,
                "verify_done": self.verify_done,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
                **self.result,
            }


class Store:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.plans: dict[str, dict] = {}
        self.applies: dict[str, ApplyTask] = {}
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

    def add_apply(self, task: "ApplyTask") -> None:
        with self.lock:
            self.applies[task.id] = task

    def get_apply(self, tid: str) -> "ApplyTask | None":
        with self.lock:
            return self.applies.get(tid)


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
        # 站方 /models 目录 —— 判死的段靠它给出可勾选的模型候选，
        # 没有它前端只能让操作员手打模型名（现场反馈的主要摩擦点）
        "catalog": list(getattr(v, "catalog", None) or []),
        "need_proxy": v.need_proxy,
        "min_headers": v.min_headers,
        # 请求指纹：通过时用的画像档名，以及该档是否需要请求体补丁。
        # 界面要显示它 —— 「需要 cc-std」比「需要 3 个头」对人有用得多，
        # 而 min_body_kind 非空意味着 headers 表达不了，claude 段得设
        # fingerprint-profile 让 CPA 自己补（其余三段配置层无解）。
        #
        # 2026-09-02 补：这两个字段一直在 SectionVerdict 上，但没进 JSON ——
        # 于是界面上「请求指纹」这一列永远是空的。
        "profile_name": v.profile_name,
        "min_body_kind": v.min_body_kind,
        "time_window": list(v.time_window) if v.time_window else None,
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
        # 候选身份。前端拿它当勾选键与 DOM 定位键 —— host 不唯一
        # （一个站常有 15 把 Key），用 host 会让同站多 Key 互相覆盖。
        "line_no": p.line_no,
        "key_masked": p.masked_key,
        "skipped": p.skipped,
        "any_writable": p.any_writable,
        "sections": {
            s: {
                "section": sp.section,
                "base_url": sp.base_url,
                "models": sp.models,
                # probed / catalog / manual —— 界面要标清模型是实测跑通的、
                # 站方目录报的，还是操作员手填的，三者可信度差一截
                "model_source": sp.model_source,
                # 站方目录整体落后市面最新一个世代以上（如目录只有 gpt-4 系
                # 而市面已到 5.6）。清单照旧列出，但默认不勾 —— 界面要说清
                # 为什么，否则「有模型却不建议勾」看着像 bug。
                "catalog_stale": sp.catalog_stale,
                "catalog_stale_why": sp.catalog_stale_why,
                "priority": sp.priority,
                "priority_reason": sp.priority_reason,
                "proxy_url": sp.proxy_url,
                # 前缀与 weight 也要给 —— 它们会落进 config.yaml，
                # 而界面上「本项目所有参数都应同时具备」（2026-09-02）。
                # prefix 决定 `ANT/claude-opus-5` 这类命名空间别名能不能
                # 命中；weight: 0 决定这个条目是否参与调度。
                "prefix": sp.prefix,
                "weight": sp.weight,
                "headers": sp.headers,
                "max_context_length": sp.max_context_length,
                "context_model": sp.context_model,
                "score": sp.score,
                "duplicate": sp.duplicate,
                "duplicate_note": sp.duplicate_note,
                "writable": sp.writable,
                # 这一段落盘时是「新增」还是「更新既有条目」。
                # 探测发现原上游在别的段也能用时，那一段是新增 —— 界面要标出来，
                # 因为它改变的是 config.yaml 的条目数，而不只是某个字段。
                "new_section": sp.new_section,
                # 非空 = 这一段不会写入，值就是原因。界面必须显示它 ——
                # 上一版这道闸只在写盘那层，界面显示「建议写入」并默认勾上，
                # 勾了写不进（2026-09-03 现场）。
                "write_blocked": sp.write_blocked,
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
        # ETA 的并发闸要知道真实并发度，不是配置值 —— 站数少于配置时
        # 实际并发就是站数。
        with job.lock:
            job.workers = cand_workers

        # 结果按输入行序回填，不用 append —— 并行下完成先后是乱的，
        # 而前端结果表与 build_diffs 的插入顺序都依赖原始行序。
        slots: list = [None] * len(job.rows)

        def one(i: int, row) -> None:
            job.mark_unit_start(row.host)
            slots[i] = prober.probe(row)
            with job.lock:
                # done_rows 是进度显示用的，只数已完成的，与顺序无关
                job.results = [x for x in slots if x is not None]
            # 锁外调 —— mark_unit_done 自己要拿同一把锁，在锁内调会死锁
            job.mark_unit_done(row.host)

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

# ── 漂移检测的服务端缓存 ──
#
# 为什么必须挪出请求路径（2026-09-02 现场）：`/api/context` 原来同步调
# check_profile_drift，而远程模式要拉两个 GitHub 文件。国内 VPS 直连
# raw.githubusercontent 不通，实测每次打开网页干等 15 秒 —— 而前端要等这个
# 响应回来才 `#app.hidden = false`，用户看到的是只有页头、正文全空的白屏。
#
# 现在：`/api/context` **只读缓存，永不阻塞**。缓存为空或过期时丢给后台线程，
# 本次先返回 pending，前端显示「正在核对」并稍后自动重取。
# 功能一项没少 —— 三条路径（本地源码 / 远程拉取 / config.yaml）全部保留，
# 只是换成异步刷新。
_DRIFT_CACHE: dict = {"at": 0.0, "value": None, "inflight": False}
_DRIFT_LOCK = threading.Lock()
# 成功结论 6 小时（与 cpa_source_probe 的远程缓存同量级），失败 10 分钟。
# 本地源码模式不走网络，但也走这套缓存 —— 它要读几个文件加 .git，
# 同样没必要每次打开网页重做。
_DRIFT_TTL_OK = 6 * 3600
_DRIFT_TTL_BAD = 600


def _drift_snapshot(*, runtime_commit_url: str = "", **kw) -> dict:
    """漂移检测结果：只读缓存，过期则后台刷新。绝不阻塞调用方。

    kw 原样转交 cp.check_profile_drift。

    runtime_commit_url 是 CPA 管理端点，在**后台线程里**才去打它的 /healthz
    取 X-CPA-COMMIT —— 那一步也是网络请求（超时 3 秒），在请求路径里算等于
    把这个接口的下限抬到 3 秒。它只是个增强信号（发现「源码更新了但 CPA 没
    重启」），不该决定页面能不能显示。
    """
    now = time.time()
    with _DRIFT_LOCK:
        cached = _DRIFT_CACHE["value"]
        ttl = _DRIFT_TTL_OK if (cached or {}).get("checked") else _DRIFT_TTL_BAD
        fresh = cached is not None and now - _DRIFT_CACHE["at"] < ttl
        need = not fresh and not _DRIFT_CACHE["inflight"]
        if need:
            _DRIFT_CACHE["inflight"] = True

    if need:
        def work() -> None:
            try:
                got = cp.check_profile_drift(
                    runtime_commit=_cpa_runtime_commit(runtime_commit_url),
                    **kw)
            except Exception as e:                       # noqa: BLE001
                # 后台线程里抛出去没人接，会静默丢失整个检查。转成一条
                # 「没能核对」的结论 —— 与三条路径都不成立时同一个形状。
                got = {"checked": False, "drifts": [],
                       "why": f"核对时出错：{type(e).__name__}: {e}"}
            with _DRIFT_LOCK:
                _DRIFT_CACHE.update(at=time.time(), value=got, inflight=False)
        threading.Thread(target=work, daemon=True,
                         name="drift-refresh").start()

    if cached is not None:
        out = dict(cached)
        # 过期但正在后台刷新 —— 让前端知道这份是旧的，不必等
        if not fresh:
            out["refreshing"] = True
        return out
    # 从来没算过：给一个明确的 pending，前端据此显示「正在核对」并稍后重取
    return {"checked": False, "pending": True, "drifts": [],
            "why": "正在核对画像基线（首次要拉 CPA 源码，不阻塞其他功能）"}


def _clean_override_models(section: str, raw_models: list) -> list[str]:
    """用户覆盖的模型清单也要过段规则。

    为什么必须在这里再过一遍（2026-09-02 自查发现）：`build_plan` 里的
    `force` 路径已经过滤了，但 `overrides["models"]` 是**另一条入口** ——
    它在 build_plan 之后直接赋值 `sp.models`，绕开全部规则。

    当前前端只用 `forced` 不用 `overrides.models`，所以这条路没被走到。
    但它是公开的 API 契约（`/api/plan` 收 overrides），curl 直接调就能塞进
    任意模型名，而写进 config.yaml 的模型必须与段协议匹配 —— 纵深防御，
    与 `build_plan` 里对 `v.catalog` 再过一遍闸同一个理由。

    全部不合规时**保留原样**并不清空：清空会让这一段 writable=False，
    用户的显式指定变成「什么都不写」，比拒绝更难排查。由调用方在
    warnings 里说明。
    """
    got = [str(m).strip() for m in raw_models if str(m).strip()]
    # 判据与 build_plan 的 forced 路径**必须同一个**（2026-09-03）：
    # `section_protocol_ok` 只挡协议层不可能成立的（段协议不匹配、非对话模型），
    # 不挡四族之外 —— 那是操作员的显式指定，而 compat 段确实能跑 grok / glm
    # （实测 runanytime 唯一验证过的模型就是 grok-4.6）。
    # 这里用 section_allows 会让「界面手填能写、curl 覆盖写不进」，两条入口
    # 对同一个名字给出不同结果。
    kept = cp.model_catalog.newest_generation_per_line(
        [m for m in got if cp.model_catalog.section_protocol_ok(section, m)])
    return kept or got


def _market_top_gen(cfg: dict) -> dict:
    """各段「当前市面最新」的最高世代，如 {"codex-api-key": [5, 6]}。

    前端拿它判断「站方目录是不是整体落后」—— 落后一个世代以上时不预勾
    （2026-09-02 现场：某站 codex 目录只有 gpt-4 系而市面已到 5.6，
    「取最高世代」把四个老款全留下还默认全勾）。

    后端在 build_plan 里判同一件事（catalog_is_stale），但结果表在勾选**之前**
    就渲染了，那时还没有 /api/plan 的响应 —— 所以两边都要能判。

    走 model_catalog 自己的缓存（成功 6 小时 / 失败 10 分钟），不会拖慢
    /api/context；拉不到时返回空 dict，前端退化成「不判落后、照常预勾」。
    """
    out: dict[str, list[int]] = {}
    try:
        remote, _why = cp.model_catalog.remote_names()
        for sec in cp.SECTIONS:
            names, _src = cp.model_catalog.latest_models(
                sec, cfg=cfg, remote=remote, limit=12)
            top = cp.model_catalog.top_generation(names)
            if top:
                out[sec] = [top[0], top[1]]
    except Exception:                                    # noqa: BLE001
        # 这只是个增强信号，绝不能让它影响 /api/context 的可用性 ——
        # 与漂移检测同一条原则（那次它把首屏卡成了白屏）。
        return {}
    return out


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
        with job.lock:
            job.workers = max(1, min(len(all_rows), max_workers))

        job.emit("info", {"msg": f"开始批量探测（{max_workers} 站并发）"})

        # 工作单元总数在这里才确定（去重后的凭据数），回填。
        # 普通路径的 unit_total 在 __init__ 里就是 len(rows)，这条路不是。
        with job.lock:
            job.unit_total = len(all_rows)

        # 进度回调 —— site 是站名（已脱敏，不含 api_key）
        def progress_cb(current, total, site, stats):
            # current=0 是占位调用，只记起始不记完成
            if current == 0:
                job.mark_unit_start(site)
                return
            # 先记完成再发事件 —— snapshot 读的是 unit_done，顺序反了
            # 会让本次事件对应的进度晚一轮才反映出来。
            job.mark_unit_done(site)
            job.emit("progress", {
                "msg": f"探测进度：{current}/{total}",
                "current": current,
                "total": total,
                "site": site,
                # success = 至少一段可用（操作员真正关心的「这个凭据能用吗」）
                # all_four = 四段全通，罕见，单独看
                "success": stats["success"],
                "all_four": stats.get("all_four", 0),
                "failure": stats["failure"],
            })

        # 批量探测
        results_dict = batch_prober.probe_batch(all_rows, progress_callback=progress_cb)

        # 转换为列表（按原顺序）。
        #
        # **必须滤掉 None**：BatchProber 对抛异常的站不会往 results_dict 里放
        # 条目，于是 .get() 返回 None。而下游 _api_job 的 snapshot 与
        # _api_plan 都直接取 res.row —— None 会让整个 /api/job 返回 500。
        # 触发条件低到「175 个站里有一个网络超时」。
        #
        # 滤掉之后要把数量说出来：静默少几个站，用户看到的是「探测完成」但
        # 方案里莫名少了几条，那比报错更难查。
        with job.lock:
            got = [results_dict.get((row.bare, row.api_key)) for row in all_rows]
            job.results = [r for r in got if r is not None]
        lost = len(got) - len(job.results)
        if lost:
            job.emit("error", {
                "msg": f"{lost} 个站探测时抛异常，未纳入结果"
                       f"（其余 {len(job.results)} 个正常）"
            })
            # 逐条报原因 —— BatchProber.errors 记了 (站, 异常)。
            # 只说「少了 3 个」而不说是哪 3 个、为什么，等于让人去猜。
            for host, why in batch_prober.errors[:20]:
                job.emit("error", {"msg": f"  {host}：{why}"})
            if len(batch_prober.errors) > 20:
                job.emit("error", {
                    "msg": f"  …另有 {len(batch_prober.errors) - 20} 条同类"})

        _st = batch_prober._stats
        job.emit("info", {"msg": (
            f"探测完成：{_st['success']} 个凭据至少一段可用"
            f"（其中 {_st.get('all_four', 0)} 个四段全通），"
            f"{_st['failure']} 个全灭")})

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
        elif route.startswith("/api/apply-status/"):
            self._api_apply_status(route[len("/api/apply-status/"):])
        elif route.startswith("/api/export/"):
            self._api_export(route[len("/api/export/"):])
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
        #
        # 走 _drift_snapshot 而不是直接调 —— 远程模式要拉 GitHub，拉不通时
        # 单次 8 秒起，而这个接口决定前端能不能显示页面。见那个函数的说明。
        drift = _drift_snapshot(
            source_root=type(self).cpa_source_root, cfg=cfg,
            runtime_commit_url=type(self).cpa_url,
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
            # 调度策略。界面上要说清 `weight: 0` 到底意味着什么 ——
            # 只有 weighted-round-robin 会把零权重凭据逐出调度池
            # （selector.go:650 → positiveWeightAuths）；默认的 round-robin
            # 与 fill-first 根本不读 weight，那时 `weight: 0` 的站照常轮询。
            # 见 cp.weight_zero_excludes。
            "routing_strategy": (
                str(((cfg or {}).get("routing") or {}).get("strategy") or "")
                if isinstance((cfg or {}).get("routing"), dict) else ""),
            "weight_zero_excludes": cp.weight_zero_excludes(cfg),
            # 各段「当前市面最新」的最高世代，形如 {"codex-api-key": [5, 6]}。
            #
            # 前端要它做首屏预勾判断：目录整体落后一个世代以上时不预勾
            # （如目录只有 gpt-4 系而市面已到 5.6）。后端在 build_plan 里
            # 也判同一件事，但那要等 /api/plan 回来 —— 而结果表在勾选之前
            # 就渲染了，两边都需要这个数。
            #
            # 走 model_catalog 自己的缓存（成功 6 小时 / 失败 10 分钟），
            # 所以不会因为它拖慢 /api/context。
            "market_top_gen": _market_top_gen(cfg),
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

        # ── 完整参数：走与全量检测**同一条链路** ──────────────────────
        #
        # 为什么必须（2026-09-02 用户指出）：这个端点原来只回答「要什么头」，
        # 而写进 config.yaml 需要的是全套 —— 代理、请求指纹、priority、前缀、
        # 模型清单、上下文上限、影响面。逐字段比对发现相对 verdict_json 缺 20
        # 个字段、相对 plan_json 缺 22 个。
        #
        # 根因不是「忘了加」，是它自己组装返回值 —— 四条途径各有一份字段清单
        # （verdict_json / plan_json / 这里的字典字面量 / CLI 的 print），
        # 加字段要改四处，漏一处就是缺失。
        #
        # 修法：诊断也跑 prober.probe() + build_plan()，然后复用同一套序列化。
        # 诊断与全量的差别只应在「探几个候选」与「要不要写回」，不该在
        # 「能得出什么结论」。
        full = bool(body.get("full", True))
        verdicts: dict = {}
        plan_out = None
        if full:
            # probe() 跑完整四阶段：目录 → 段归属 → 模型验证 → 换模 → 上限。
            # 上下文二分默认仍关（那是百万字符的大 body，诊断场景不值得），
            # 但换模采样打开 —— 它只要 3 次额外请求，而「照常计费却返回另一个
            # 模型」是必须让人看见的结论。
            fp = Prober(
                proxy=_resolve_proxy(str(body.get("proxy") or "")),
                gap=float(body.get("gap", 0.5)),
                timeout=int(body.get("timeout", 60)),
                probe_context=bool(body.get("probe_context", False)),
                swap_samples=int(body.get("swap_samples", 3)),
                workers=len(secs),
                cfg_snapshot=cfg,
            )
            result = fp.probe(row)
            verdicts = {sec: verdict_json(v)
                        for sec, v in result.sections.items()}
            # 定档与影响面：与写回路径同一个 build_plan，参数完全一致。
            # rebuild=False —— 诊断的语义是「这个站能不能导入」，撞已有条目
            # 时该如实报 duplicate，那正是操作员要知道的。
            pl = cp.build_plan(row, result, cfg, bands={},
                               seen=cp.existing_fingerprints(cfg),
                               probation=bool(body.get("probation", True)),
                               raw=raw)
            plan_out = plan_json(pl)

        self._json(200, {
            "host": row.host,
            "key_masked": row.masked(),
            "line_no": row.line_no,
            "sections": out,
            "total_calls": sum(v["calls"] for v in out.values()),
            # 与 /api/probe 完全同构 —— 前端可以复用同一套渲染
            "verdicts": verdicts,
            "plan": plan_out,
            "full": full,
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
        # **必须写进 opts**：_api_plan 从 job.opts 读这个标志决定走全量重建还是
        # 增量插入。只用它选执行函数是不够的 —— 那样 run_job_full_redetect 确实
        # 跑了，但随后 /api/plan 读到 False 就走增量分支，rebuild_config_full
        # 从不执行。表现是「等了 5 分钟重探，最后只追加了新站」，而且不报错：
        # diffs 为空、lines_before == lines_after，看起来像「没什么要改的」。
        opts["full_redetect"] = full_redetect
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

    def _api_export(self, jid: str) -> None:
        """整份探测日志导出成纯文本。给运维留档、也给排障时贴给别人看。

        为什么是 txt 而不是 JSON：这份东西的读者是人。JSON 要先格式化才能读，
        而排障现场经常是「把这段贴到聊天里问别人」，txt 直接可读。
        机器要的那份数据 /api/job 已经给了。

        **脱敏是硬要求**：日志里带 api-key，而导出文件会被贴到聊天、
        存到桌面、可能进网盘。全程只用 row.masked()，与 JSON 响应同一口径。
        """
        job = STORE.get_job(jid)
        if not job:
            self._json(404, {"error": f"没有这个任务：{jid}"})
            return

        L: list[str] = []
        w = L.append
        w("CPA 上游探测日志")
        w("=" * 66)
        w(f"任务 {job.id}")
        w(f"状态 {job.state}" + (f" · 错误 {job.error}" if job.error else ""))
        w(time.strftime("导出于 %Y-%m-%d %H:%M:%S", time.localtime()))
        w(time.strftime("开始于 %Y-%m-%d %H:%M:%S", time.localtime(job.started)))
        w(f"候选 {len(job.rows)} · 请求 {job.calls} 次 · "
          f"耗时 {round((job.finished or time.time()) - job.started, 1)}s")
        w("")
        w("注：api-key 一律只出末四位。上游 URL 与模型名原样保留。")
        w("")

        opts = job.opts or {}
        if opts:
            w("── 探测参数 " + "─" * 52)
            for k in sorted(opts):
                w(f"  {k} = {opts[k]!r}")
            w("")

        for r in job.results:
            row = r.row
            w("─" * 66)
            w(f"站 {row.bare}   key {row.masked()}   行 {row.line_no}")
            w(f"  可用段 {len(r.usable_sections)}/4 · 请求 {r.total_calls} 次")
            for sec, v in r.sections.items():
                tag = "可用" if v.usable else "不可用"
                w("")
                w(f"  [{sec}] {tag} · {v.category or '-'} · {v.action or '-'}")
                w(f"    base-url        {v.base_url or '-'}")
                if v.models:
                    w(f"    实测模型        {', '.join(v.models)}")
                cat = list(getattr(v, "catalog", None) or [])
                if cat:
                    w(f"    站方目录 {len(cat)} 个   {', '.join(cat)}")
                if v.profile_name:
                    w(f"    请求指纹        {v.profile_name}")
                if v.min_headers:
                    w("    最小门票头      "
                      + ", ".join(f"{k}: {x}" for k, x in v.min_headers.items()))
                if v.min_body_kind:
                    w(f"    需 body 补丁    {v.min_body_kind}")
                w(f"    需代理          {'是' if v.need_proxy else '否'}")
                if v.time_window:
                    w(f"    可调用时段      {v.time_window[0]}~{v.time_window[1]}")
                if v.max_context_length:
                    w(f"    上下文上限      {v.max_context_length}"
                      f"（实测于 {v.context_model or '?'}"
                      f"{'，截断反推不可信' if v.context_untrusted else ''}）")
                if v.swap:
                    w(f"    静默换模        {v.swap}")
                for a in v.attempts:
                    w(f"      · {a.status:>3} {a.model:<28} {a.combo:<18}"
                      f" {a.elapsed_ms:>6}ms"
                      + (f" 代理={a.proxy}" if a.proxy else "")
                      + (f" 回={a.resp_model}" if a.resp_model else ""))
                    if a.excerpt:
                        w(f"          {a.excerpt[:200]}")
            w("")

        w("─" * 66)
        w("事件流")
        for e in job.events:
            w(f"  [{e.get('t')}s] {e.get('kind')} "
              + " ".join(f"{k}={v}" for k, v in e.items()
                         if k not in ("t", "kind")))

        raw = ("\n".join(L) + "\n").encode("utf-8")
        name = time.strftime("cpa-probe-%Y%m%d-%H%M%S.txt", time.localtime())
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

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
        # 三个按候选索引的入参。键是**行号字符串**而不是 host ——
        # 一个站常有 15 把 Key（实测 gorouter 15、tabitoken 14），用 host
        # 做键会让同站多 Key 互相覆盖：勾选 Set 去重成一个、DOM 定位只命中
        # 第一行、priority 覆盖落到错误的条目上。2026-09-02 现场表现为
        # 「全勾选只勾中 26 项」。
        #
        # 兼容旧键：值仍接受 host（老前端缓存或外部脚本），查表时两种都试。
        overrides = body.get("overrides") or {}     # {line_no: {section: {...}}}
        selected = body.get("selected")             # [[line_no, section], ...] 或 None
        # 人工接管：{line_no: {section: [模型, ...]}}。探测判不可用但操作员确知
        # 可用的段，由他显式给模型清单。见 cp.build_plan 的 force 说明 ——
        # 只绕过 usable 判定，去重/定档/影响面/diff 确认一道都不少。
        forced = body.get("forced") or {}

        def _by_row(d: dict, row_or_plan) -> dict:
            """按候选取它那份配置。先试行号，再回落 host。

            回落是为了兼容旧前端的 {host: ...} 形态 —— 那种情况下同站多 Key
            会共用一份配置，与旧行为一致，不会更坏。
            """
            ln = str(getattr(row_or_plan, "line_no", "") or "")
            if ln and ln in d:
                return d[ln] or {}
            h = getattr(row_or_plan, "host", "")
            return d.get(h) or {}
        # 默认试用期：新站进最低可插档，不因探测满分就把已验证的站挡在其后
        probation = not bool(body.get("by_score"))

        # 检测是否全量重探模式
        is_full_redetect = job.opts.get("full_redetect", False)

        if is_full_redetect:
            # 全量重探模式：使用 rebuild_config_full
            bands: dict = {}
            seen = cp.existing_fingerprints(cfg)
            all_plans = {}  # {(base_url, api_key): ImportPlan}
            # 既有条目的 weight，按 (段, host, api_key) 查。`weight: 0` 是
            # 「把这个站逐出调度池」的唯一表达，而 CPA 缺这个字段时默认 1 ——
            # 不搬运它等于让手工封禁的站全部复活。
            #
            # 键含段（2026-09-03 对账发现，与 proxy-url 同一个成因）：实测
            # facai 的 3 把 Key 在 codex/claude 段是 weight:0（那两条路径静默
            # 换模，已封），compat 段**故意没写**；100xlabs 同理只封 claude。
            # 按 (host, key) 搬会把 0 灌进那些没封的段 —— 6 个 (凭据, 段)
            # 组合被无声逐出调度池，而 YAML 合法、写后验证也发现不了。
            weights = existing_weights(cfg)
            # proxy-url 同理。它只在探测当场判定「需要代理」时才有值，而重探时
            # 那个站可能这次直连就通 —— 方案里 proxy_url 为空，整段重写就把原有
            # 的 26 条 mihomo 代理全抹掉。见 existing_proxies 的说明。
            proxies = existing_proxies(cfg)
            # prefix 与 compat 的 provider name 同样必须搬原值。
            #
            # 2026-09-03 拿真实文件逐字段 deep-equal 才抓到（之前只比字段
            # **出现次数**，两处都数得对、值全错）：
            #   · prefix 121/121 被抹掉 —— dominant_prefix 只是给新条目猜的
            #     默认值，既有条目自己写的才是真的
            #   · compat 的 name 12/13 被改成 host —— 那是 CPA 的 provider
            #     身份（provider_key），改名作废冷却状态与能力缓存
            prefixes = existing_prefixes(cfg)
            pnames = existing_provider_names(cfg)
            # 每个模型自己的 max-context-length（在 models 块里，carry 搬不到）
            mctx = existing_model_context(cfg)
            # 模型级白名单外字段（当前配置为空，补闸）
            mextra = existing_model_extras(cfg)

            for res in job.results:
                fh = _by_row(forced, res.row)
                # rebuild=True 关掉去重判定 —— 全量重探的输入**就是** cfg 里的
                # 既有条目，而 seen 是从同一份 cfg 读出来的，每一条都必然撞上。
                #
                # 2026-09-02 实测：不传这个参数时 79 个凭据只有 26 项可勾选。
                # 14 个 host 里每个只有第一个 Key 逃过（它的 prefix/headers 与
                # 探测建议不同、五元组恰好没撞），其余 260 个段全判 duplicate
                # → writable=False → 「全勾选」跳过，勾选框点不动。
                p = cp.build_plan(res.row, res, cfg, bands=bands, seen=seen,
                                  probation=probation, rebuild=True, raw=raw,
                                  force={str(k): [str(m) for m in (v or [])]
                                         for k, v in fh.items()} if fh else None)
                for sec, sp in p.sections.items():
                    # weight 与 proxy-url 都按 (段, host, key) 搬 —— 同一个
                    # 凭据在不同段的这两个字段是**独立配置**，跨段共用会
                    # 静默改行为（见 existing_weights / existing_proxies）。
                    w = weights.get((sec, res.row.host, res.row.api_key))
                    if w is not None:
                        sp.weight = w
                    # 探测判定需要代理时它已有值，不覆盖 —— 那是本次实测
                    # 结论；只补「原来有、这次没探出来」的情形。
                    #
                    # 键含段（2026-09-02 修）：kktoken.cc 的 Key 在 compat 段
                    # 有代理、在 claude 段故意没有（那条路径直连可用）。
                    # 按 (host, key) 搬会把 compat 的代理灌进 claude ——
                    # 多一跳不会失败，所以 validate 与写后验证都发现不了。
                    if not sp.proxy_url:
                        got = proxies.get(
                            (sec, res.row.host, res.row.api_key))
                        if got:
                            sp.proxy_url = got
                    # prefix：既有条目自己写的优先于 dominant_prefix 猜的。
                    # `"" in prefixes` 与「键不存在」要分开 —— 前者是操作员
                    # 显式写了空串，也该照原样。
                    pk_ = (sec, res.row.host, res.row.api_key)
                    if pk_ in prefixes:
                        sp.prefix = prefixes[pk_]
                    # compat 的 provider name（按 host，组内共用）
                    if sec == "openai-compatibility":
                        sp.provider_name = pnames.get(res.row.host, "")
                    # 每个模型自己的 max-context-length。
                    #
                    # 它在 models 块里，carry 有意跳过那一块（清单由方案重新
                    # 生成），而方案只带本次实测的那**一个**。不搬的话本次没探
                    # 上下文时历史实测值全丢 —— 实测生产配置 8 处，客户端会按
                    # CPA 内置目录的偏大值定压缩点。
                    sp.prior_context = {
                        name: val
                        for (s2, h2, k2, name), val in mctx.items()
                        if s2 == sec and h2 == res.row.host
                        and k2 == res.row.api_key
                    }
                    # 模型级的白名单外字段（display-name / thinking / image /
                    # force-mapping / is-compat / *-modalities）—— 同一个空档，
                    # carry 跳过 models 块、render_entry 只写三个字段。
                    # 当前配置一个都没用到，这是补闸不是修事故。
                    sp.prior_model_extras = {
                        name: dict(val)
                        for (s2, h2, k2, name), val in mextra.items()
                        if s2 == sec and h2 == res.row.host
                        and k2 == res.row.api_key
                    }
                all_plans[(res.row.bare, res.row.api_key)] = p

            # 用户覆盖分**两批**应用，中间夹着新增段判定与批量定档。
            #
            # 为什么必须分开（2026-09-03）：
            #   · 模型清单覆盖要在 mark_new_sections **之前** —— 那道闸按
            #     model_source 判，手填的清单必须先落进方案，否则「手填了
            #     真实模型」仍会被当成工具猜测拦下。
            #   · priority 覆盖要在 assign_priorities **之后** —— 那个函数
            #     会给每个站重新定值，先覆盖就被它冲掉。
            # 上一版把两批放在一起、全放在定档之后，于是第一条不成立。
            for (base_url, api_key), p in all_plans.items():
                ov_host = _by_row(overrides, p)
                for sec, sp in list(p.sections.items()):
                    ov = ov_host.get(sec) or {}
                    if "proxy_url" in ov:
                        sp.proxy_url = str(ov["proxy_url"] or "")
                    if "headers" in ov and isinstance(ov["headers"], dict):
                        sp.headers = {str(k): str(v)
                                      for k, v in ov["headers"].items()}
                    if "models" in ov and isinstance(ov["models"], list):
                        sp.models = _clean_override_models(sec, ov["models"])
                        # 显式给了清单 = 操作员的手填意图，与 forced 同权。
                        sp.model_source = "manual"
                    if "max_context_length" in ov:
                        v = ov["max_context_length"]
                        sp.max_context_length = int(v) if v else None

            # 新增段的放行判定 —— **必须在定档之前**。
            #
            # 探测发现某个凭据在原本没配的段也能用时，那一段该作为新条目加进去
            # 并参与整体计算（用户 2026-09-03 的要求）。放行标准是「有没有实测
            # 依据」：probed / manual / catalog 放行，seed（工具猜测）不放行 ——
            # 后者正是 121 条目变 246 那次事故的成因。
            #
            # 为什么在定档之前：被拦下的段不会落盘，让它参与 assign_priorities
            # 会白占一个档位（taken 被污染，各站被挤得更低），影响面也会把不
            # 存在的条目算进遮挡关系。
            blocked_new = cp.mark_new_sections(cfg, list(all_plans.values()))

            # 批量定档：站与站之间不同值、同站所有 Key 同值。
            #
            # 必须在这里做而不是 build_plan 里（2026-09-02 现场）：
            # suggest_priority 每次只看「当前 config 有哪些空档」，79 个凭据
            # 串行调用它、每个都拿到同一个答案 —— 落盘后 claude 段 74 个条目
            # 全是 175，站与站之间毫无区分，而 priority 的唯一作用就是区分先后。
            #
            # raw 必传：定档的安全上限要读注释里的「实测不可用」结论，不传会把
            # 可用新站压到一堆死站后面（claude 段实测 500 → 175）。
            prio_warns = cp.assign_priorities(
                list(all_plans.values()), cfg, probation=probation, raw=raw)

            # 第二批覆盖：priority。放在定档之后，手工值不会被冲掉。
            for (base_url, api_key), p in all_plans.items():
                ov_host = _by_row(overrides, p)
                for sec, sp in list(p.sections.items()):
                    ov = ov_host.get(sec) or {}
                    if "priority" in ov:
                        sp.priority = int(ov["priority"])
                        sp.priority_reason = "用户手工指定"
                        # 影响面按新值重算 —— 旧值算出来的遮挡关系会误导。
                        # 增量路径一直这么做，这条路上一版漏了。
                        band = bands.get(sec) or cp.build_band(cfg, sec, raw=raw)
                        sp.impacts = cp.compute_impact(
                            band, sp.models, sp.priority)
                        sp.warnings = [w for w in sp.warnings
                                       if "抢走" not in w]
                        if sp.hijacked:
                            names = ", ".join(i.model for i in sp.hijacked[:4])
                            sp.warnings.append(
                                f"会抢走 {len(sp.hijacked)} 个模型的顶层"
                                f"（{names}）—— 你已手工确认")

            # 选择集过滤 —— 全量重探这条路原来**完全不读 selected**（2026-09-03）。
            #
            # 后果：操作员取消勾选的段照样被重写。而取消勾选的成因往往是
            # 「这一段我不想动」——「重探顺手把它改了」正好相反。
            #
            # 剪的是送进 rebuild_config_full 的副本，不是 all_plans 本身：
            # 界面要靠完整的 plans 渲染每一段的参数与勾选框（与增量路径同一条
            # 规则）。没进 write 集的既有条目由 keep_unplanned 原样保留，
            # 不会因为没勾就被删。
            if selected is None:
                # 首次拉取（前端为读 recommended）—— 取工具建议的集合，
                # 与增量路径同一个判据。绝不默认「全写」。
                want = {(str(p.line_no), sec)
                        for p in all_plans.values()
                        for sec, sp in p.sections.items() if sp.recommended}
            else:
                want = {(str(h), str(s)) for h, s in selected}

            write_plans: dict = {}
            for k, p in all_plans.items():
                keep = {sec: sp for sec, sp in p.sections.items()
                        if (str(p.line_no), sec) in want or (p.host, sec) in want}
                if not keep:
                    continue
                shallow = copy.copy(p)
                shallow.sections = keep
                write_plans[k] = shallow

            # 全量重建
            preview, warnings = cp.rebuild_config_full(
                cfg, write_plans, raw.splitlines(keepends=True))
            if blocked_new:
                warnings.append(
                    f"{blocked_new} 个 (凭据, 段) 组合原本不在 config.yaml 里，"
                    f"且模型清单只是工具猜测 —— 界面已标成「不写入」并说明原因。"
                    f"确知可用的话手填模型清单即可放行")
            # 覆盖之后再查同值：assign_priorities 保证站与站不同，但用户手工
            # 改 priority 是在它之后应用的 —— 改成邻站的值就同层了。同层按
            # weight 轮询是合法配置，但它取消的正是「不同网站不同优先级」，
            # 必须报出来而不是默默照写。
            #
            # 只查真会落盘的那批（write_plans）：没勾的段不写进去，报它们
            # 同层是无中生有的警告。
            prio_warns = list(prio_warns) + cp.priority_collisions(
                list(write_plans.values()))
            warnings = list(prio_warns) + list(warnings)

            # 生成完整 diff（整个文件）
            diffs = []
            ok, msg = validate(preview)

            pid = secrets.token_hex(8)
            # 存 write_plans 而不是 all_plans：写后验证按 entry["plans"] 挑目标，
            # 存全量会去验根本没写进去的段（与增量路径同一条规则）。
            STORE.add_plan(pid, {"plans": list(write_plans.values()), "diffs": diffs,
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
            fh = _by_row(forced, res.row)
            p = cp.build_plan(res.row, res, cfg, bands=bands, seen=seen,
                              probation=probation, raw=raw,
                              force={str(k): [str(m) for m in (v or [])]
                                     for k, v in fh.items()} if fh else None)
            plans.append(p)

        # 模型清单覆盖要在 mark_new_sections **之前** —— 那道闸按 model_source
        # 判，手填的清单必须先落进方案，否则「手填了真实模型」仍会被当成工具
        # 猜测拦下。与全量重探那条路同一个顺序。
        for p in plans:
            ov_host = _by_row(overrides, p)
            for sec, sp in list(p.sections.items()):
                ov = ov_host.get(sec) or {}
                if "models" in ov and isinstance(ov["models"], list):
                    # 过段规则（全量重探那条路早就过了，这条原来直接
                    # `str(m)` 塞进去，绕开全部规则），并把来源记成 manual。
                    sp.models = _clean_override_models(sec, ov["models"])
                    sp.model_source = "manual"

        # 新增段的放行判定，与全量重探同一套闸（2026-09-03）。
        #
        # 增量导入的多数行是全新凭据 —— `is_new_section` 对它们返回 False，
        # 这一步什么也不做。真正被它管住的是「粘贴的 Key 其实已经在
        # config.yaml 里配过某一段」：那时探测在别的段拿到 seed 猜测清单，
        # 写进去就是凭空多一个必失败的条目。两条路径判据必须一致，否则
        # 同一个凭据走增量与走重探得到不同结果。
        blocked_new = cp.mark_new_sections(cfg, plans)

        # 增量导入也批量定档 —— 同一个 bug 的同一个修法。
        #
        # 一次粘贴 15 个站时，build_plan 里的 suggest_priority 同样会给它们
        # 相同的值（bands 共享且不随本批新增更新）。批量分配保证站与站之间
        # 不同、同站多 Key 同值，与 config.yaml 既有的规律一致。
        prio_warns = cp.assign_priorities(plans, cfg, probation=probation, raw=raw)

        # 其余覆盖（优先级 / 代理 / 头 / 上下文上限）—— 定档之后应用，
        # 手工改的 priority 不会被 assign_priorities 冲掉。
        for p in plans:
            ov_host = _by_row(overrides, p)
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
                if "max_context_length" in ov:
                    v = ov["max_context_length"]
                    sp.max_context_length = int(v) if v else None

        # 选择集过滤。
        #
        # selected=None（前端首次拉取，为了读 recommended）不能等于「全写」：
        # 判死段现在也是 writable 了（有种子模型兜底、参数算全），而
        # build_diffs 按 writable 筛 —— 不设默认判据的话首次 /api/plan
        # 就会把 IP封 / 死路的段一起排进 diff。
        #
        # 默认判据取 recommended：那才是「工具建议写」的集合。判死段照旧
        # 出现在 plans 里（界面要显示它们的完整参数、勾选框要能勾），
        # 只是不进 diff。
        # 关键：剪的是**送进 build_diffs 的副本**，不是 plans 本身。
        # plans 要原样回给界面 —— 判死段的完整参数、勾选框都靠它渲染，
        # 从 plans 里删掉等于前端再也看不到那些段，「全勾」会退化成
        # 「只勾推荐项」（就是这一轮要修掉的症状）。
        if selected is None:
            want = {(str(p.line_no), sec)
                    for p in plans for sec, sp in p.sections.items()
                    if sp.recommended}
        else:
            want = {(str(h), str(s)) for h, s in selected}

        for_write = []
        for p in plans:
            # 行号优先，host 回落 —— 与 _by_row 同一套兼容策略
            keep = {sec: sp for sec, sp in p.sections.items()
                    if (str(p.line_no), sec) in want or (p.host, sec) in want}
            if not keep:
                continue
            shallow = copy.copy(p)
            shallow.sections = keep
            for_write.append(shallow)

        diffs = build_diffs(raw, for_write)
        preview = apply_diffs(raw, diffs)
        ok, msg = validate(preview)

        pid = secrets.token_hex(8)
        # 存 for_write 而不是 plans：apply 后的写后验证按 entry["plans"]
        # 挑目标，存全量就会去验根本没写进去的段（判死段现在也 writable）。
        STORE.add_plan(pid, {"plans": for_write, "diffs": diffs,
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
            # 定档提示：整批下移、越过现有档位、压到最低值，以及用户覆盖
            # 造成的同层。全都会影响站与站的先后，必须让人看到。
            # 只查真正写进去的那些段（for_write）—— 未勾选的段不落盘，
            # 报它们同值只是噪声。
            "warnings": list(prio_warns) + cp.priority_collisions(for_write)
            + ([f"{blocked_new} 个 (凭据, 段) 组合的模型清单只是工具猜测，"
                f"而这个凭据原本没配那一段 —— 界面已标成「不写入」并说明原因。"
                f"确知可用的话手填模型清单即可放行"] if blocked_new else []),
        })

    def _cpa_password_for(self, body: dict) -> str:
        """取 CPA 管理密码。请求里显式给的优先，否则复用登录凭据。

        只有用户是**用 CPA 管理密码登录**本服务时后者才成立 —— 用服务自己的
        token 登录的话我们手上没有管理密码。先排除「这就是本服务 token」，
        避免为它白跑一次 bcrypt（单次约 100ms，且必然不匹配）。
        """
        push = body.get("push") or {}
        mgmt = (push.get("mgmt_key") or "").strip()
        if mgmt:
            return mgmt
        cred = (body.get("_cred") or "").strip()
        if cred and not _same_secret(cred, type(self).token)                 and self._check_cpa_password(cred):
            return cred
        return ""

    def _api_apply_status(self, tid: str) -> None:
        """写回收尾的进度。前端轮询它，直到 state 不再是 running。

        落盘已经完成了 —— 这个端点只报「重载与验证进行到哪」。
        """
        task = STORE.get_apply(tid)
        if not task:
            self._json(404, {"error": f"没有这个写回任务：{tid}"})
            return
        self._json(200, task.snapshot())

    def _api_apply(self, body: dict) -> None:
        """真正落盘。必须带 plan_id + confirm=true。

        两段式（2026-09-02 改，为解 Cloudflare 524）
        ------------------------------------------
        落盘本身很快（一次 O_TRUNC 写），慢的是后面两步：
          · PUT 触发 CPA 重载        1-3 秒
          · 端到端验证 N 个段        单个最长 45 秒，并行但受上限 24 约束

        原来这三步在**同一个 HTTP 请求里同步做完**，于是 79 凭据那种规模会
        跑到 100 秒以上 —— Cloudflare 在 100 秒切断连接，返回 524，前端拿到
        的是 CF 的 HTML 拦截页而不是 JSON（现场截图里一堆 <!DOCTYPE html>）。
        任务其实已经写盘成功，但用户看到的是「写回失败」。

        并发度不是瓶颈：验证早就是并行的。瓶颈在「客户端必须一直等着」。
        所以改成：落盘同步做完（它是关键路径，必须给确定回执），重载与验证
        丢到后台线程，立刻返回 task_id，前端轮询 /api/apply-status/{id}。

        这样每个 HTTP 请求都在 1 秒内结束，CF 的 100 秒上限再也碰不到，
        而进度可见 —— 与步骤②的探测进度同一套显示。
        """
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

        # 落盘已完成，是不可逆的关键路径 —— 上面那段同步做完并给出确定回执。
        # 剩下的重载与验证丢到后台，立刻返回 task_id。见本方法 docstring。
        task = ApplyTask(secrets.token_hex(8), result)
        STORE.add_apply(task)
        cls = type(self)
        threading.Thread(
            target=_run_apply_tail,
            args=(task, entry, body, cls.cfg_path, cls.cpa_url,
                  self._cpa_password_for(body), self._cpa_client_key()),
            name=f"apply-tail-{task.id}", daemon=True).start()
        self._json(200, {**result, "task_id": task.id, "state": "running"})
        return




def _run_apply_tail(task: "ApplyTask", entry: dict, body: dict,
                    cfg_path: str, cfg_cpa_url: str,
                    mgmt: str, auto_client_key: str) -> None:
    """写回的后台收尾：触发 CPA 重载 + 端到端验证。

    落盘已在 HTTP 请求里同步完成 —— 这里只做「慢且非关键路径」的两步，
    进度写进 task 供 /api/apply-status 轮询。见 _api_apply 的 docstring：
    这两步同步做会让 79 凭据那种规模跑破 Cloudflare 的 100 秒上限。

    所有分支都必须落到 task.state —— 后台线程抛异常没人看得到，
    前端会永远停在「运行中」。
    """
    result = task.result
    try:

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
                    or (cfg_cpa_url or "").strip())
        # 管理密码由调用方算好传入（见 _cpa_password_for）

        if cpa_base and mgmt:
            task.set_stage("触发 CPA 重载")
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
            # 等 fsnotify 的 debounce 落地再验。
            #
            # 真正让新上游可被选中的是 reloadClients()，而它挂在 fsnotify
            # 那一路上，前面有 150ms 的 debounce
            # （internal/watcher/watcher.go:87 configReloadDebounce）。
            # PUT 返回 200 只说明 CPA 接受了这份 YAML 并更新了管理 handler
            # 的 h.cfg，凭据池此刻还没重建 —— 立刻打业务端点会打在旧池子上，
            # 于是刚写进去的站被报成「验证失败」，而它其实是好的。
            #
            # 1.2 秒 = 150ms debounce + LoadConfig 与 reloadClients 的余量。
            # 这一步的代价是固定 1.2 秒，而误报一个可用站的代价是用户把它删掉。
            time.sleep(1.2)
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
                client_key = auto_client_key
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

                task.set_stage("端到端验证")
                task.set_verify_total(len(todo))
                verified = [None] * len(todo)

                def _one(i: int, host: str, sec: str, model: str) -> None:
                    vok, vmsg = verify_upstream(
                        cpa_base, client_key, sec, model, timeout=45,
                    )
                    task.bump_verify()
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

        task.state = "done"
        task.set_stage("全部完成")
    except Exception:
        task.state = "error"
        task.error = traceback.format_exc(limit=4)
        task.set_stage("收尾出错")
    finally:
        task.finished = time.time()

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
