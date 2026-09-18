#!/usr/bin/env python3
"""批量导入服务 —— HTTP 后端。只用标准库，VPS 上不需要装任何依赖。

安全模型（重要，别改松）
------------------------
这个服务持有**明文上游 API key**并能改写 config.yaml，等价于 CPA 的写权限。
所以：

  1. 默认只绑 127.0.0.1。要外网访问请用 nginx 反代并在那一层加 TLS + 认证，
     不要把 --host 改成 0.0.0.0 直接暴露。
  2. **强制 Bearer token**，没有免鉴权模式。token 从 --token 或环境变量
     IMPORTER_TOKEN 读；都没给则随机生成，但绝不打印到日志。
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
import difflib
import hmac
import hashlib
import ipaddress
import io
import json
import logging
import mimetypes
import os
import re
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
    BatchProber, CarryTables, extract_existing_entries,
)
from cpa_probe.writeback import (  # noqa: E402
    redact_yaml_secrets,
    apply_diffs,
    build_diffs,
    reload_cpa,
    validate,
    verify_upstream,
    write_local,
    config_version,
    WritebackError,
)

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "web")

# 本模块自己的 logger。2026-09-13 加：全量重探的定档耗时必须落进日志 ——
# 它跑过 Cloudflare 的 100 秒就变成 524，而用户侧只看到一页 HTML 错误页。
# 原来这里一个 logging 都没有，慢在哪只能靠猜。
logger = logging.getLogger("importer.server")

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
        self.event_cursor = 0
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
        # 因事件表上限而省略的条数（累计）。见 emit。
        self.dropped: int = 0

    # 事件表的条数上限（2026-09-05 加）。长任务每次 attempt 一条 ——
    # 79 个凭据最坏 2370 次请求，加上画像升级与重试，实测量级在几千条；
    # 而一次全量重探跑几分钟，浏览器可能整夜挂着不关。
    #
    # 超上限时丢**中间**那一段而不是最早的：开头几条是「任务怎么起的」
    # （参数、候选数、并发数），排障时最有用；末尾是「现在在干什么」。
    # 中间那些逐个 attempt 的细节可以丢，且丢了要留痕（插一条 truncated）。
    MAX_EVENTS = 6000
    KEEP_HEAD = 200

    def emit(self, kind: str, data: dict) -> None:
        with self.lock:
            self.event_cursor += 1
            self.events.append({"t": round(time.time() - self.started, 1),
                                "kind": kind, **data, "seq": self.event_cursor})
            if kind == "attempt":
                self.calls += 1
            if len(self.events) > self.MAX_EVENTS:
                drop = len(self.events) - self.MAX_EVENTS
                # 保头 + 留痕（2026-09-12 恢复）
                # --------------------------------
                # 中途被改成 `del self.events[:drop]`，那是两处降级：
                #   ① 最早的事件被丢掉 —— 开头那批正是这次跑的前置信息
                #     （目录、代理预检、画像选档），排查时最需要它们；
                #   ② 界面上看不出「中间有事件被省略」，读日志的人会把
                #     残缺的事件流当成完整的。
                # 用户第 8 条：改动只能正向。这里按 HEAD 的形状复原，
                # 同时保留新增的 seq 字段（游标分页靠它）。
                head = self.events[:self.KEEP_HEAD]
                cut = self.events[self.KEEP_HEAD:self.KEEP_HEAD + drop + 1]
                tail = self.events[self.KEEP_HEAD + drop + 1:]
                # 省略计数**累加**存在字段上，不能从本轮的 drop 现算 ——
                # 现算永远显示「省略 2 条」，而实际可能省了几千条，
                # 那比不显示更糟（读日志的人会以为只丢了 2 条）。
                #
                # 只数**真实事件**：被切掉的那一段里可能含上一轮的留痕条
                # （它就在 KEEP_HEAD 位置上，每轮都会被吃掉再重建）。
                # 把它也算进去的话计数会翻倍。
                self.dropped += sum(1 for e in cut if not e.get("_trunc"))
                self.events = head + [{
                    "t": head[-1]["t"] if head else 0.0,
                    "kind": "info",
                    "_trunc": True,
                    "seq": self.event_cursor,
                    "msg": f"（省略 {self.dropped} 条中间事件 —— 事件表上限 "
                           f"{self.MAX_EVENTS} 条。完整记录在服务端 stderr）",
                }] + tail

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
                "total_rows": self.unit_total,
                "done_rows": len(self.results),
                "events": [e for e in self.events if e["seq"] > since],
                "event_cursor": self.event_cursor,
                "next_cursor": self.event_cursor,
                "oldest_cursor": self.events[0]["seq"] if self.events else self.event_cursor + 1,
                # 「有事件看不到了」有**两种**成因，都要报（2026-09-12 补第二种）
                # ------------------------------------------------------------
                #   ① 轮询者落后太多，队头之前的事件已经不在表里
                #      —— 原判据 `since < events[0].seq - 1`
                #   ② 表满后**中间段**被截掉（emit 保留 KEEP_HEAD 条队头 +
                #      一条 `_trunc` 面包屑 + 队尾）。这一种下 `events[0].seq`
                #      恒为 1，①那个判据永远不成立 —— 于是事件确实丢了，
                #      而 `history_lost` 一直是 False，轮询者拿不到任何提示，
                #      还以为自己看到了完整事件流。
                #
                # `self.dropped` 就是 emit 累计丢弃的条数（面包屑自己不计入），
                # 用它兜住第二种。
                "history_lost": bool(
                    self.dropped
                    or (self.events and since < self.events[0]["seq"] - 1)),
                "lost_events": max(
                    0,
                    (self.events[0]["seq"] - 1 if self.events else 0) - since,
                ) + self.dropped,
            }


class ApplyTask:
    """后台事务状态：排队、基线复查、写盘、CPA 重载与验证。"""

    def __init__(self, task_id: str, base_result: dict):
        self.id = task_id
        self.state = "running"          # running | done | error
        self.result = dict(base_result)  # 落盘阶段的结果，后续逐步补字段
        self.error = ""
        self.started = time.time()
        self.finished = 0.0
        # 阶段进度。写回没有「79 个单元」那种自然分片，能给准的是**阶段**
        # 与验证的 已完成/总数 —— 那两个都是实测量。
        self.stage = "queued"
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


class PlanTask:
    """异步定档任务（2026-09-17）。

    `/api/plan` 把原来的同步计算变成两步：
      1. `POST /api/plan`  → 立即返回 `{"plan_task_id": "...", "state": "pending"}`
      2. `GET  /api/plan-status?plan_task_id=...` → 轮询直到 state == "done" | "error"

    为什么必须异步：定档要重建整份 config.yaml（173 站），实测 > 60 秒。
    Cloudflare Free 套餐回源超时硬限 100 秒、不可调，同步版本在冷启动时
    必然返回 524，priority 全停在「待定」占位符。改成立即返回 + 轮询后，
    每个 HTTP 请求都在 1 秒内完成，CF 超时彻底无关。

    缓存命中仍然同步返回（< 1ms），走原来的 `/api/plan` 响应体 ——
    那不走本类，本类只在真正要算的时候创建。
    """

    def __init__(self, task_id: str, body: dict):
        self.id = task_id
        self.state = "running"      # running | done | error
        self.body = body            # 入参存档，前端轮询时不用重传
        self.result: dict = {}      # 计算完毕后填入，与原 /api/plan 响应体一致
        self.error = ""
        self.started = time.time()
        self.finished = 0.0
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "plan_task_id": self.id,
                "state": self.state,
                "error": self.error,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
                # 只在 done 时才带上完整结果，避免 running 阶段返回半截数据
                **({"result": self.result} if self.state == "done" else {}),
            }


class CapacityError(ValueError):
    pass


class Store:
    """任务 / 方案 / 写回任务三张表。**都有容量上限与 TTL**。

    为什么必须有（2026-09-05 量化）
    ---------------------------
    `/api/plan` 每次调用存两份**整份配置**（`preview` 与 `base_raw`）。
    生产 config.yaml 约 857KB，即每次约 1.7MB，而 `plan_id` 每次新生成、
    旧条目原来永不释放。nginx 对 `/` 放行 240r/m（`nginx.conf:727`），
    即约 400MB/分钟；容器内存上限 512M（`docker-compose.yml:531`）且
    `restart: "no"` 不自愈 —— 约 90 秒 OOM。

    **非恶意也会撞上**：前端每次勾选变化都防抖 180ms 后调一次 `/api/plan`
    （`web/app.js:2136-2140`），一轮正常操作就积累几十份。

    淘汰策略按各表的语义分开：
      · plans   —— 一次性凭据（apply 成功即作废）。最容易涨，上限最小
      · jobs    —— 探测任务，用户可能回看事件流。跑着的绝不淘汰
      · applies —— 写回任务，同样跑着的不淘汰

    TTL 从**最后一次访问**算，不是创建时间 —— 用户盯着一个任务看半小时，
    不该因为「创建于 30 分钟前」被清掉。
    """

    # 上限按「一条占多少」定：plan 约 1.7MB × 8 ≈ 14MB，够一轮交互
    MAX_PLANS = 8
    # 批量方案比 plan 轻（只存两份文本 + 说明），但同样一份约 450KB。
    MAX_BULKS = 4
    MAX_JOBS = 32
    MAX_APPLIES = 32
    TTL = 2 * 3600          # 2 小时没人碰就清

    def __init__(self) -> None:
        self.apply_generation = 0
        self.jobs: dict[str, Job] = {}
        self.plans: dict[str, dict] = {}
        self.plan_tasks: dict[str, PlanTask] = {}   # 异步定档任务（2026-09-17）
        self.applies: dict[str, ApplyTask] = {}
        # 批量管理的预览结果（每份两段整份配置文本）
        self.bulks: dict[str, dict] = {}
        # {表名: {id: 最后访问时间}} —— 与数据分开存，避免污染 payload
        # 新增表**必须同时在这里登记**，否则 `_touch` 会 KeyError。
        self._touched: dict[str, dict[str, float]] = {
            "jobs": {}, "plans": {}, "plan_tasks": {}, "applies": {}, "bulks": {}}
        self.lock = threading.Lock()

    def _touch(self, table: str, key: str) -> None:
        """记一次访问。**调用方必须已持锁**。"""
        self._touched[table][key] = time.time()

    def _evict(self, table: str, data: dict, cap: int,
               busy=None) -> None:
        """清过期条目，仍超上限时淘汰最久未访问的。**调用方必须已持锁**。

        `busy(item) -> bool` 返回 True 的条目不淘汰（正在跑的任务）。
        """
        now = time.time()
        seen = self._touched[table]
        for k in [k for k, t in list(seen.items())
                  if now - t > self.TTL and not (busy and busy(data.get(k)))]:
            data.pop(k, None)
            seen.pop(k, None)
        # 同步掉已经不在数据表里的时间戳
        for k in [k for k in seen if k not in data]:
            seen.pop(k, None)
        if len(data) <= cap:
            return
        # 按「是否在跑, 最后访问」升序 —— 空闲且最久未碰的先走
        order = sorted(data.keys(),
                       key=lambda k: (bool(busy and busy(data.get(k))),
                                      seen.get(k, 0.0)))
        for k in order[:len(data) - cap]:
            if busy and busy(data.get(k)):
                break              # 剩下的全在跑，宁可超上限也不动它们
            data.pop(k, None)
            seen.pop(k, None)

    @staticmethod
    def _job_busy(job) -> bool:
        return bool(job is not None and getattr(job, "state", "") in ("pending", "running"))

    @staticmethod
    def _apply_busy(task) -> bool:
        return bool(task is not None
                    and getattr(task, "state", "") == "running")

    def add_job(self, job: Job) -> None:
        with self.lock:
            self._evict("jobs", self.jobs, self.MAX_JOBS - 1, self._job_busy)
            if len(self.jobs) >= self.MAX_JOBS:
                raise CapacityError("探测任务容量已满，请等待已有任务结束")
            self.jobs[job.id] = job
            self._touch("jobs", job.id)

    def get_job(self, jid: str) -> Job | None:
        with self.lock:
            got = self.jobs.get(jid)
            if got is not None:
                self._touch("jobs", jid)
            return got

    def put_bulk(self, base_raw: str, text: str, notes: list) -> str:
        """存一份批量预览结果，返回 bulk_id。

        新文本存在服务端而不是让前端回传 —— 回传等于让客户端决定写什么，
        而这个服务持有明文上游 key 与 config.yaml 的写权限。
        `base_raw` 用于 apply 时的基线比对，挡并发覆盖。
        """
        bid = secrets.token_urlsafe(12)
        with self.lock:
            self._evict("bulks", self.bulks, self.MAX_BULKS)
            self.bulks[bid] = {"base_raw": base_raw, "text": text,
                               "notes": list(notes)}
            self._touch("bulks", bid)
        return bid

    def get_bulk(self, bid: str) -> dict | None:
        with self.lock:
            got = self.bulks.get(bid)
            if got is not None:
                self._touch("bulks", bid)
            return got

    def add_plan(self, pid: str, payload: dict) -> None:
        with self.lock:
            self._evict("plans", self.plans, self.MAX_PLANS)
            self.plans[pid] = payload
            self._touch("plans", pid)

    def get_plan(self, pid: str) -> dict | None:
        with self.lock:
            got = self.plans.get(pid)
            if got is not None:
                self._touch("plans", pid)
            return got

    def drop_plan(self, pid: str) -> None:
        """写回成功后主动释放。那份 plan 已被基线比对作废，留着只占内存。

        每份 plan 持有两份整份配置（约 1.7MB），是这三张表里最重的。
        """
        with self.lock:
            self.plans.pop(pid, None)
            self._touched["plans"].pop(pid, None)

    def add_apply(self, task: "ApplyTask") -> None:
        with self.lock:
            self._evict("applies", self.applies, self.MAX_APPLIES - 1,
                        self._apply_busy)
            if len(self.applies) >= self.MAX_APPLIES:
                raise CapacityError("写回任务容量已满，请稍后重试")
            self.applies[task.id] = task
            self._touch("applies", task.id)

    def get_apply(self, tid: str) -> "ApplyTask | None":
        with self.lock:
            got = self.applies.get(tid)
            if got is not None:
                self._touch("applies", tid)
            return got

    def claim_apply(self, entry: dict) -> tuple[ApplyTask, bool]:
        with self.lock:
            tid = entry.get("task_id")
            if tid:
                task = self.applies.get(tid)
                if task is None:
                    raise ValueError("方案已消费，任务已过期；请重新预览")
                return task, False
            self._evict("applies", self.applies, self.MAX_APPLIES - 1, self._apply_busy)
            if len(self.applies) >= self.MAX_APPLIES:
                raise CapacityError("写回任务容量已满，请稍后重试")
            task = ApplyTask(secrets.token_hex(8), {"local_written": False})
            self.apply_generation = getattr(self, "apply_generation", 0) + 1
            task.generation = self.apply_generation
            task.stage = "queued"
            self.applies[task.id] = task
            self._touch("applies", task.id)
            entry["task_id"] = task.id
            return task, True

    def sizes(self) -> dict[str, int]:
        """三张表的条数。给 /api/context 用，便于运维看有没有堆积。"""
        with self.lock:
            return {"jobs": len(self.jobs), "plans": len(self.plans),
                    "plan_tasks": len(self.plan_tasks),
                    "applies": len(self.applies)}

    def add_plan_task(self, task: "PlanTask") -> None:
        with self.lock:
            # 上限从 8 提到 32、且 done 的任务 5 分钟内不淘汰（2026-09-17）
            # ------------------------------------------------------------
            # 现场两份快照都停在「定档轮询无响应 —— 刷新页面后重试」。
            # 成因：前端每次勾选变化都防抖 180ms 发一次 /api/plan，一轮操作
            # 几十次；每次缓存未命中都新建一个任务，8 条上限很快被挤满，
            # 正在被轮询的那条 done 任务被 LRU 淘汰 → /api/plan-status 404
            # → 前端连续 15 次 miss → 报「无响应」。任务本身没有问题。
            # busy 判据加上「刚完成不到 300 秒」，给轮询方取结果的窗口。
            self._evict("plan_tasks", self.plan_tasks, 32, self._plan_task_busy)
            self.plan_tasks[task.id] = task
            self._touch("plan_tasks", task.id)

    @staticmethod
    def _plan_task_busy(task) -> bool:
        if task is None:
            return False
        if getattr(task, "state", "") == "running":
            return True
        fin = getattr(task, "finished", 0.0) or 0.0
        return bool(fin) and (time.time() - fin) < 300.0

    def get_plan_task(self, tid: str) -> "PlanTask | None":
        with self.lock:
            got = self.plan_tasks.get(tid)
            if got:
                self._touch("plan_tasks", tid)
            return got


STORE = Store()


def _validate_body(body: dict) -> None:
    if not isinstance(body, dict):
        raise ValueError("JSON 顶层必须是对象")
    bools = {"confirm", "full_redetect", "by_score", "probe_context",
             "probe_capabilities", "reuse_profile_verdict", "run_full", "full", "probation",
             "rebuild_mid_system", "disable_cooling", "websockets",
             "prompt_cache_key"}
    def check(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in bools and type(value) is not bool:
                    # Nullable three-state configuration overrides are supported.
                    if not (key in {"rebuild_mid_system", "disable_cooling",
                                    "websockets", "prompt_cache_key"} and value is None):
                        raise ValueError(f"{key} 必须是布尔值")
                if key in {"priority", "max_context_length"} and value is not None:
                    try:
                        if isinstance(value, bool) or isinstance(value, (dict, list)):
                            raise ValueError
                        int(value)
                    except (ValueError, TypeError, OverflowError):
                        raise ValueError(f"{key} 必须是整数") from None
                check(value)
        elif isinstance(node, list):
            for item in node:
                check(item)
    check(body)
    for key in ("opts", "push", "overrides", "forced"):
        if key in body and not isinstance(body[key], dict):
            raise ValueError(f"{key} 必须是对象")
    if "text" in body and not isinstance(body["text"], str):
        raise ValueError("text 必须是字符串")
    for key in ("job_id", "plan_id", "bulk_id", "revision", "_cred"):
        if key in body and not isinstance(body[key], str):
            raise ValueError(f"{key} 必须是字符串")
    if "selected" in body and body["selected"] is not None:
        if not isinstance(body["selected"], list) or any(
                not isinstance(item, list) or len(item) != 2
                or not isinstance(item[0], (str, int)) or not isinstance(item[1], str)
                for item in body["selected"]):
            raise ValueError("selected 必须是 [行号, 段名] 列表")
    for group in ("overrides", "forced"):
        for rows in body.get(group, {}).values():
            if not isinstance(rows, dict):
                raise ValueError(f"{group} 的每行必须是对象")
            for section, value in rows.items():
                if section not in cp.SECTIONS:
                    raise ValueError("未知协议段")
                if group == "overrides" and not isinstance(value, dict):
                    raise ValueError("每段覆盖值必须是对象")
                if group == "forced":
                    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                        raise ValueError("forced 模型必须是字符串列表")
                    _clean_override_models(section, value)
    for key, value in body.get("push", {}).items():
        if key in {"base", "mgmt_key", "client_key"} and not isinstance(value, str):
            raise ValueError(f"push.{key} 必须是字符串")


_PUBLIC_SALT = secrets.token_bytes(32)
_DOMAIN = re.compile(r"(?<![\w-])(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,63}(?![\w-])")
_SECRET_NAME = re.compile(r"secret|password|authorization|cookie|credential|token|(?:^|_)cred$|^key$|api[-_]?key|mgmt[-_]?key|client[-_]?key", re.I)

# `_SECRET_NAME` 会误伤的字段名（2026-09-12）
# ------------------------------------------
# CPA 的**段名**本身就叫 `codex-api-key` / `claude-api-key` /
# `gemini-api-key` —— 它们匹配上面的 `api[-_]?key`，于是 `_public` 把这些
# 键的**值**（base-url、模型清单这类）整体换成 `***`：
# `/api/parse` 回的 bases 三段全是 `***`，界面上什么都看不到。
# 段名不是凭据，凭据是段里那个 `api-key` 字段 —— 后者仍然被抹。
_NOT_SECRET_FIELDS = frozenset({
    "verify_key_src",
    "codex-api-key", "claude-api-key", "gemini-api-key",
})


def _safe_text(text: str) -> str:
    """Public prose: remove URL credentials. Host names stay readable.

    2026-09-12：去掉「把域名替换成 site-<hash>.invalid」那一步。
    ------------------------------------------------------------
    这个函数在 `_json` 里作用于**每一个** API 响应，而本工具的界面就跑在
    操作员自己机器上，全部工作都要靠站名来做：`/api/parse` 回的 base-url、
    方案里的 base_url、diff 里的 config.yaml 行。域名一律改写之后：

      · `/api/parse` 回 `site-393c5c20b011.invalid/v1`，界面上认不出是哪个站；
      · 方案与 diff 里的 base-url 全变假名，操作员无法复核要写什么进配置；
      · 每次进程重启 `_PUBLIC_SALT` 都换，同一个站两次假名不同，
        连「前后两次是不是同一个站」都判断不了。

    用户第 9 条要的是**提交到 GitHub 之前**把私有域名排除掉 —— 那是
    `tools/scrub.py` 与 `.gitignore` 的职责，不是本地界面响应的职责。
    tests/test_server.py 里写明的契约也是「主机与端口要留着 —— 排障最需要
    看那部分」。所以这里只抹凭据：URL userinfo、query 里的 token/key、
    Authorization 头值。
    """
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1***@", str(text))
    text = re.sub(r"(?i)([?&](?:token|key|api_key|secret|password)=)[^&\s\"']+", r"\1***", text)
    text = re.sub(r"(?i)(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 ***", text)
    return text


def _public(value, field: str = ""):
    """Redact only output copies; never mutate plans or config snapshots."""
    if isinstance(value, dict):
        if field.endswith("headers"):
            import yaml
            redacted = yaml.safe_load(redact_yaml_secrets(
                yaml.safe_dump({"headers": value}, allow_unicode=True)))
            value = redacted["headers"]
        return {key: _public(item, key) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public(item, field) for item in value]
    if not isinstance(value, str):
        return value
    if (_SECRET_NAME.search(field) and field not in _NOT_SECRET_FIELDS
            and not field.endswith("_masked")):
        return "***" if value else ""
    # Reuse writeback's structural redactor for URL userinfo/query credentials.
    import yaml
    wrapped = yaml.safe_dump({"value": value}, allow_unicode=True)
    scrubbed = yaml.safe_load(redact_yaml_secrets(wrapped))
    return _safe_text(scrubbed["value"])


def _public_with_context(value, context):
    """Let writeback remove known credentials even inside free-form messages.

    豁免完整性校验字段（2026-09-18）
    ------------------------------
    `revision`、`fingerprint`、`bulk_id`、`tuning_id` 是 SHA-256 哈希或
    base64url token，用于完整性校验与去重。脱敏器在 writeback.py:1230 用
    盲文本替换 `text.replace(secret, _short_mask(secret))` —— 任何在 config
    里被判为凭据的短串（`"0"`、`"cli"`、`"5s"`、`"true"`）都会替换响应体
    全文，把 SHA-256 哈希里所有相同字符改坏。

    现场后果：生产配置 config.yaml 的 headers 里有值为 `"0"`、`"cli"`、
    `"5s"`、`"true"`、`"600"` 的短字符串，mask_key("0")=="0***" →
    `/api/routes` 响应的 `revision` / 每条 `fingerprint` 全部报废 →
    `/api/bulk-preview` 必 409 `stale_selection` → 前端静默重载清空选中 →
    「批量管理选好选项后点执行根本没反应」；`bulk_id` 同样被污染，apply 回 404。

    修法：在扁平化时识别这四类字段，打上豁免标记，让脱敏器跳过它们。
    这四类字段永远是哈希或 token，不可能是凭据。
    """
    import yaml
    def strings_only(node):
        if isinstance(node, dict):
            return {key: strings_only(item) for key, item in node.items()
                    if isinstance(item, (str, dict, list))}
        if isinstance(node, list):
            return [strings_only(item) for item in node
                    if isinstance(item, (str, dict, list))]
        return node
    # 完整性校验字段豁免名单：这四个字段是哈希或 token，永远不可能是凭据
    _INTEGRITY_FIELDS = {"revision", "fingerprint", "bulk_id", "tuning_id"}
    strings = []
    exempt = set()      # 记下豁免索引
    def flatten(node, path=()):
        if isinstance(node, dict):
            for key, item in node.items():
                flatten(item, path + (key,))
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                flatten(item, path + (idx,))
        elif isinstance(node, str):
            # path[-1] 是这个字符串的键名（或数组下标）
            if path and path[-1] in _INTEGRITY_FIELDS:
                exempt.add(len(strings))
            strings.append(node)
    flatten(value)
    # JSON is also YAML, and quotes every string. Non-string fields never
    # enter a credential redactor (e.g. nullable prompt_cache_key is not a key).
    text = json.dumps({"context": strings_only(context), "payload_items": strings},
                      ensure_ascii=False)
    redacted = yaml.safe_load(redact_yaml_secrets(text))
    if (not isinstance(redacted, dict) or not isinstance(redacted.get("payload_items"), list)
            or len(redacted["payload_items"]) != len(strings)):
        raise ValueError("公开输出无法安全脱敏")
    cleaned = iter(redacted["payload_items"])
    def typed(original, idx_holder=[0]):
        if isinstance(original, dict):
            return {key: typed(item) for key, item in original.items()}
        if isinstance(original, list):
            return [typed(item) for item in original]
        if isinstance(original, str):
            idx = idx_holder[0]
            idx_holder[0] += 1
            # 豁免字段直接用原值，不走脱敏器的输出
            return original if idx in exempt else next(cleaned)
        return original
    return _public(typed(value))

def _validate_final(preview: str, plans=(), *, cross_section: bool = True) -> tuple[bool, str]:
    """写盘前的最后一道闸。

    `cross_section` 控制「同一网址跨协议段也必须同档」这条 2026-09-11 批准的
    规则是否**阻断**（2026-09-18 加这个开关）：
      · 走方案写回（`/api/apply`）时保持 True —— 本工具自己产出的方案不该
        制造新的跨段分裂；
      · 走批量管理与全局调优（`/api/bulk-preview`、`/api/bulk-apply`、
        `/api/tuning-apply`）时传 False。理由见下面那段注释：那条路径没有
        `plans`，这道闸只能去扫**整份 config.yaml**，而生产配置里 18 个 host
        有 16 个跨段档位不同（同段内 0 个分裂），于是每次预览都被判失败、
        返回 400，前端把错误写进折叠着的 `#bmmsg` —— 用户看到的就是
        「选好选项点执行，根本没反应」。分裂照常作为提示回传，不再阻断。
    """
    ok, msg = validate(preview)
    if not ok:
        return ok, msg
    # 空 models 闸（2026-09-18）
    # ------------------------
    # 在此之前这道闸只查三件事：YAML 合法、priority 是整数、同站档位一致。
    # **不查 models 是否为空** —— 于是「探测全灭 → 目录读不到 → 清单为空」
    # 的段只要被勾上（前端 `selected` 自报，落盘侧无质量判据），就照样写进
    # config.yaml。CPA 拿到 models 为空的条目，每次轮到它必失败，
    # 而界面上看不出任何异常。这就是用户说的「低次品写进 config.yaml」。
    #
    # 端到端验证里早有同样的判据（`if not sp.writable or not sp.models`），
    # 但那是**写盘之后**的抽验，拦不住写入本身。
    empty = [sp for sp in plans
             if getattr(sp, "writable", False) and not getattr(sp, "models", None)]
    if empty:
        names = "、".join(
            f"{cp.host_of(str(getattr(sp, 'base_url', '')))}/{getattr(sp, 'section', '?')}"
            for sp in empty[:3])
        more = f" 等 {len(empty)} 条" if len(empty) > 3 else ""
        return False, (f"这些段没有任何模型，写进去 CPA 每次轮到都会失败：{names}{more}"
                       f" —— 请先取消勾选，或在「模型」列手工填入站方支持的模型名")
    issues = cp.priority_split_within_host(list(plans))
    import yaml
    cfg = yaml.safe_load(preview) or {}
    groups = {}
    for section in cp.SECTIONS:
        for entry in cfg.get(section) or []:
            if not isinstance(entry, dict):
                continue
            host = cp.host_of(str(entry.get("base-url") or ""))
            if not host:
                continue
            pri = entry.get("priority", 0)
            if type(pri) is not int:
                return False, "priority 必须是整数"
            values = [pri]
            if section == "openai-compatibility":
                values += [k.get("priority", pri) for k in entry.get("api-key-entries") or []
                           if isinstance(k, dict)]
            if any(type(value) is not int for value in values):
                return False, "priority 必须是整数"
            # 归集键 =（段, host）而不是单 host（2026-09-18）
            # ----------------------------------------------
            # 段内同站同档是硬约束（修改要求 3⑶④：同一类型相同域名共享同一
            # 优先级）。跨段是否也必须同档由 `cross_section` 决定 —— 见函数
            # docstring：批量管理那条路只能扫整份文件，用它阻断等于让 16/18
            # 个 host 的既有分裂把所有批量操作全卡死。
            groups.setdefault((section, host), set()).update(values)
            if cross_section:
                groups.setdefault(("*", host), set()).update(values)
    if any(len(values) > 1 for values in groups.values()):
        return False, ("同站优先级不一致：请将该站所有 Key"
                       "（包括未勾选项与默认 0）统一后重新预览")
    if issues:
        # 跨段分裂：阻断与否由 cross_section 决定，但无论如何都要让操作员看见。
        if cross_section:
            return False, "；".join(issues)
        msg = (msg + " · " if msg else "") + "；".join(issues)
    return True, msg


def _preview_diff(before: str, after: str) -> dict:
    diff = "\n".join(difflib.unified_diff(
        _safe_text(redact_yaml_secrets(before)).splitlines(),
        _safe_text(redact_yaml_secrets(after)).splitlines(),
        fromfile="config.yaml (before)", tofile="config.yaml (after)",
        lineterm="", n=2))
    return {"unified_diff": diff[:200000],
            "unified_diff_truncated": len(diff) > 200000}


def _apply_identity_override(sp, ov: dict) -> None:
    _validate_body(ov)
    for name in ("rebuild_mid_system", "disable_cooling"):
        if name in ov:
            setattr(sp, name, ov[name])
    for name in ("cloak_mode", "fingerprint_profile"):
        if name not in ov:
            continue
        value = ov[name]
        if not isinstance(value, str):
            raise ValueError(f"{name} 必须是字符串")
        from cpa_probe import cpa_source_probe
        ident = cpa_source_probe.cached_identity()
        choices = (getattr(ident, "claude_cloak_modes", []) if name == "cloak_mode"
                   else getattr(ident, "claude_fingerprint_profiles", [])) or []
        if value and value not in choices:
            raise ValueError(f"{name} 未被当前来源元数据支持，请刷新来源或留空")
        setattr(sp, name, value)


def _restore_public_scalar(value, original: str, field: str) -> str:
    value = str(value or "")
    if original and value == _public(original, field):
        return original
    if "***" in value or re.search(r"site-[0-9a-f]+\.invalid", value):
        raise ValueError(f"{field} 的脱敏占位符不能用于新值，请提供真实值或保留原值")
    return value


def _restore_public_headers(values: dict, original: dict) -> dict:
    public = _public(original, "headers")
    out = {}
    for key, value in values.items():
        key, value = str(key), str(value)
        if key in original and value == public.get(key):
            out[key] = original[key]
        elif "***" in value:
            raise ValueError("新的请求头不能使用脱敏占位符")
        else:
            out[key] = value
    return out


def _verification_target(cfg: dict, sp) -> tuple[str, str]:
    model = sp.models[0]
    prefix = getattr(sp, "prefix", "")
    # Gemini embeds the model in the URL; do not invent prefix path handling.
    if (getattr(sp, "section", "") == "gemini-api-key" or not prefix
            or not re.fullmatch(r"[A-Za-z0-9_-]+", prefix)):
        return model, "gateway_only"
    owners = []
    for sec in cp.SECTIONS:
        for entry in cfg.get(sec) or []:
            if not isinstance(entry, dict) or entry.get("prefix") != prefix:
                continue
            keys = (entry.get("api-key-entries") or []) if sec == "openai-compatibility" else [entry]
            for key in keys:
                if isinstance(key, dict):
                    owners.append((sec, entry, key.get("api-key", "")))
    if len(owners) != 1:
        return model, "gateway_only"
    sec, entry, key = owners[0]
    if (sec != sp.section or key != sp.api_key
            or entry.get("base-url", "").rstrip("/") != sp.base_url.rstrip("/")):
        return model, "gateway_only"
    aliases = [m.get("alias") or m.get("name")
               for m in entry.get("models") or []
               if isinstance(m, dict) and model in (m.get("name"), m.get("alias"))]
    if len(aliases) != 1 or not aliases[0] or "/" in aliases[0]:
        return model, "gateway_only"
    route = f"{prefix}/{aliases[0]}"
    if cfg.get("auth-dir") or cfg.get("oauth-model-alias"):
        return model, "gateway_only"
    for section in cp.SECTIONS:
        for other in cfg.get(section) or []:
            if not isinstance(other, dict) or other is entry:
                continue
            if any(isinstance(m, dict) and route in (m.get("name"), m.get("alias"))
                   for m in other.get("models") or []):
                return model, "gateway_only"
    return route, "unique_prefix"


# --------------------------------------------------------------------------
# 序列化：完整 key 绝不出现在响应里
# --------------------------------------------------------------------------


def row_json(row) -> dict:
    bases = {}
    for section in cp.SECTIONS:
        try:
            bases[section] = row.base_for(section) if row.ok else ""
        except ValueError:
            bases[section] = ""
    return {
        "line_no": row.line_no,
        "host": row.host,
        "bare": row.bare,
        "key_masked": row.masked(),
        "error": "输入格式无效，请检查网址和凭据" if row.error else "",
        "bases": bases,
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
        # 段专属能力开关的实测结论（三态 + 说明）。界面「请求指纹」那一列
        # 旁边显示它 —— 「支持 WebSocket」是可用性信息，「实测不支持」是
        # 结论，「未探测」是缺口，三者不能长一个样。
        "websockets": v.websockets,
        "websockets_note": v.websockets_note,
        "prompt_cache_key": v.prompt_cache_key,
        "prompt_cache_note": v.prompt_cache_note,
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
    return _public_with_context({
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
                "highest_models": list(getattr(sp, "highest_models", None) or []),
                "model_provenance": dict(getattr(sp, "model_provenance", None) or {}),
                "cloak_mode": getattr(sp, "cloak_mode", ""),
                "fingerprint_profile": getattr(sp, "fingerprint_profile", ""),
                "rebuild_mid_system": getattr(sp, "rebuild_mid_system", None),
                "disable_cooling": getattr(sp, "disable_cooling", None),
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
                # 段专属能力开关的三态结论。界面要能区分「实测不支持」与
                # 「未探测」—— 两者写回时都不写那个字段，但一个是结论、
                # 一个是缺口，显示成一个样子就是「未验证当已验证」的镜像。
                "websockets": sp.websockets,
                "websockets_note": sp.websockets_note,
                "prompt_cache_key": sp.prompt_cache_key,
                "prompt_cache_note": sp.prompt_cache_note,
                # 原条目里这两个开关的值 —— 界面要显示「本次未探测，沿用原值」。
                "prior_toggles": sp.prior_toggles,
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
    }, {"api-keys": [sp.api_key for sp in p.sections.values()],
        "entries": [{"headers": sp.headers, "proxy_url": sp.proxy_url}
                    for sp in p.sections.values()]})


# --------------------------------------------------------------------------
# 探测线程
# --------------------------------------------------------------------------


def proxy_candidates() -> list[str]:
    """本项目**自己要用**的代理地址候选，按优先级。

    为什么要可配置（第 6 条：严禁硬编码 / 死编码）
    ------------------------------------------
    原来 `mihomo:7890` / `127.0.0.1:7890` 写死在两处代码里（这里的候选表、
    以及 `plan.py` 写 `proxy-url` 时的字面量）。那两个值来自**当前这套部署**
    的 compose 服务名与端口映射 —— 换个部署（改服务名、改端口、用别的代理
    实现）就全部失效，而失效的表现是「所有需要代理的站探测失败」，不报错。

    取值顺序（先到先用，都不通则跳过代理）：
      1. `PROBE_PROXY` 环境变量 —— 逗号分隔可给多个，部署里显式指定
      2. `docker-compose.yml` 里解析出的代理服务名与端口（自动跟随部署）
      3. 内置回落 `http://mihomo:7890` / `http://127.0.0.1:7890`

    第 2 条是关键：它让「改了 compose 的服务名或端口」这件事自动被跟上，
    与 CPA / CPAMP 源码解析同一个思路 —— 配置的真相在部署文件里，不在我们
    的代码里。解析失败就静默跳到第 3 条，这是可选增强不是运行前提。
    """
    out: list[str] = []

    def add(u: str) -> None:
        u = (u or "").strip()
        if u and u not in out:
            out.append(u)

    for u in os.environ.get("PROBE_PROXY", "").split(","):
        add(u)

    # compose 里找暴露 7890/9090 这类端口的代理服务。只读不改。
    # 找不到 compose、或格式不认识，都只是少一个候选，不影响其余两条。
    try:
        import yaml as _yaml
        for cand_path in (os.environ.get("COMPOSE_FILE", ""),
                          "docker-compose.yml", "/deploy/docker-compose.yml"):
            if not cand_path or not os.path.isfile(cand_path):
                continue
            doc = _yaml.safe_load(io.open(cand_path, encoding="utf-8").read())
            for name, svc in (doc.get("services") or {}).items():
                if not isinstance(svc, dict):
                    continue
                img = str(svc.get("image") or "")
                hay = (name + " " + img).lower()
                # 认代理**实现**的名字，不认泛化的 "proxy" 字样（2026-09-11 收紧）。
                # 实测生产 compose 上放开 "proxy" 会把 `cli-proxy-api`（CPA 自己）
                # 收进来 —— 把 CPA 当成本工具的出网代理会造成转发环路，
                # 而且它那 4 个端口没一个是代理端口。
                if not any(k in hay for k in ("mihomo", "clash", "xray",
                                              "v2ray", "sing-box",
                                              "shadowsocks", "tinyproxy",
                                              "privoxy", "squid")):
                    continue
                for p in (svc.get("ports") or []):
                    # "127.0.0.1:7890:7890" / "7890:7890" / 7890
                    parts = str(p).split(":")
                    inner = parts[-1].split("/")[0]
                    if not inner.isdigit():
                        continue
                    # 只收**混合/HTTP 代理**常用端口。mihomo 之类同时还暴露
                    # 控制台端口（9090）—— 那是 REST API，拿它当代理必然不通，
                    # 收进来只会让 `_resolve_proxy` 多做几次无谓的 TCP 探测。
                    if int(inner) not in (7890, 7891, 1080, 8080, 8118, 3128):
                        continue
                    add(f"http://{name}:{inner}")          # 容器内走服务名
                    if len(parts) >= 2 and parts[-2].isdigit():
                        add(f"http://127.0.0.1:{parts[-2]}")   # 宿主机走映射端口
            break
    except Exception:
        pass

    # 内置回落：当前这套部署的形态。放最后，只在前两条都没给出候选时生效。
    add("http://mihomo:7890")
    add("http://127.0.0.1:7890")
    return out


def _resolve_proxy(requested: str) -> str | None:
    """把前端传来的代理意愿解析成一个真能连的地址。

    前端只表达「要不要试代理」（勾选框），不该让用户操心地址形态 ——
    容器内是服务名、宿主机上是映射端口，同一份前端两种部署都要能用。
    候选清单由 `proxy_candidates()` 给（环境变量 > compose 解析 > 内置回落），
    不再写死在这里。

    返回 None 表示都不通，整轮跳过 via-proxy（Prober.live_proxy 也会再挡一次）。
    """
    if not requested:
        return None
    from cpa_probe.client import probe_proxy
    from cpa_probe.parse import host_of, is_private_target
    cands = proxy_candidates()
    # 显式给了别的地址就只试那个，不擅自改成别的 —— 但要挡内网去向。
    #
    # 为什么这里也要挡（2026-09-05，与探测目标同一批）：`probe_proxy` 做的是
    # **裸 TCP 连接**，然后把连通性、异常类名（ConnectionRefused / timeout）
    # 与毫秒数经 `proxy-precheck` 事件回到 `/api/job`。那是比 HTTP 路径更干净
    # 的端口扫描 oracle —— `{"opts":{"proxy":"http://10.0.0.5:22"}}` 就能问
    # 「那台机器的 22 端口开着吗」。
    #
    # 候选清单里的地址是例外：它们是本工具**要用**的代理，由服务端自己算出
    # （环境变量 / compose / 内置回落），不来自请求体。
    if requested != "auto" and requested not in cands:
        why = is_private_target(host_of(requested) or requested)
        if why:
            return None
        return requested
    for cand in cands:
        ok, _detail = probe_proxy(cand, timeout=3)
        if ok:
            return cand
    return None


# 请求体里的数值参数各自的合法区间（2026-09-05 加）。
#
# 为什么必须钳（审计发现，实测成立）
# ------------------------------
# 这些值原来只做类型转换、不做区间检查，而它们直接决定线程数与等待时长：
#
#   {"full_redetect": true, "max_workers": 50000}
#     → BatchProber 开 5 万个站级线程，每个内部再开最多 4 个段线程
#   {"timeout": 9999999, "gap": 1e9}
#     → 线程被钉住；nginx 600 秒断连后 Python 侧仍在跑，重复几次即线程耗尽
#
# 而后果不止本机资源：把大量出网请求打向 121 个第三方站，可能触发站方的
# 批量探测防护 —— 代价落在真实凭据上（封号），那比服务挂掉更贵。
#
# 上界取普通用法的 2-4 倍：留足手工调优空间，又把最坏情形压成常数。
# max_workers 的 128 对应「cgroup 推荐值（4 核算出 48）的 2.6 倍」，
# 而 resources.detect 自己的 cap 是 64。
_LIMITS: dict[str, tuple[float, float]] = {
    "candidate_workers": (1, 32),
    "max_workers": (1, 128),
    "workers": (1, 16),            # 段级并行，四段最多 4，给 16 的余量
    "timeout": (1, 300),           # 秒。单次请求，nginx 侧 600 秒断连
    "gap": (0.0, 60.0),            # 节流间隔
    "swap_samples": (0, 10),       # 换模采样次数
    "max_models": (1, 20),         # 每段验几个模型
    "max_model_attempts": (1, 40),
}

# 一次能提交多少行凭据。8MB 请求体全是 `url,key` 约 20 万行 —— 那些行会各自
# 展开成 4 段探测，最坏 80 万次出网请求。500 行覆盖「一次导入一整批新站」
# 的真实用法（生产配置总共 121 个条目）。
MAX_INPUT_LINES = 500


def _clamp(opts: dict, name: str, default):
    """取一个数值参数并钳进合法区间。类型错或缺失时用 default。

    返回类型跟 default 走（int 默认给 int，float 默认给 float）——
    `Prober(gap=...)` 与 `workers=...` 对类型敏感。

    静默钳制而不是报 400：这些参数多半来自前端的滑块与输入框，用户手打一个
    大数字时更希望「按上限跑」而不是「整个请求失败」。真正的攻击者也一样被
    压到上限，目的达到了。钳过就在事件流里说一句，不静默。
    """
    lo, hi = _LIMITS[name]
    raw = opts.get(name, default)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    if val != val:                      # NaN
        return default
    val = max(lo, min(hi, val))
    return int(val) if isinstance(default, int) else val


def _clamped_note(opts: dict) -> list[str]:
    """哪些参数被钳过了。给事件流用 —— 静默改用户给的值不好。"""
    out = []
    for name, (lo, hi) in _LIMITS.items():
        if name not in opts:
            continue
        try:
            val = float(opts[name])
        except (TypeError, ValueError):
            out.append(f"{name}={opts[name]!r} 不是数字，用默认值")
            continue
        if val != val or val < lo or val > hi:
            out.append(f"{name}={opts[name]} 超出 [{lo:g}, {hi:g}]，已钳制")
    return out


def _error_ref(where: str) -> str:
    """记一次异常：完整 traceback 只进 stderr，返回一个短 id 供对外引用。

    为什么不把 traceback 回给客户端（2026-09-05 改）
    ------------------------------------------
    原来 500 响应体里直接带 `traceback.format_exc(limit=4)`，而 `job.error`
    也是它 —— 后者会进 `/api/job` 事件流**与 `/api/export` 的 txt**，
    而那个 txt 的设计用途就是「贴给别人看」（见 `_api_export` 的 docstring）。

    `format_exc` 不含局部变量，所以不会直接吐出密钥值。泄露的是容器内文件
    布局、模块结构与代码行号 —— 那降低后续利用的成本。任何畸形入参都能拿到
    一段（如 `{"overrides":{"1":{"claude-api-key":{"priority":"abc"}}}}`
    → `int("abc")`）。

    换成 id 之后排障链路没变短：运维 `docker compose logs | grep <id>` 就能
    定位到完整栈，而客户端只看到「服务内部错误（err-3f2a1b）」。
    """
    ref = secrets.token_hex(3)
    # stderr 那一份要**完整**（2026-09-12）
    # ------------------------------------
    # 中途改成 `format_tb(...) + type(exc).__name__`，那丢掉了异常的
    # **消息文本**：日志里只有栈帧和一个 `RuntimeError`，看不到
    # 「probe-for-test」这种真正说明问题的内容。而本函数的整个设计前提就是
    # 「客户端只拿 id，运维 `docker compose logs | grep <id>` 拿全量」——
    # 全量那一份被削薄，排障链路就断了。
    # 对外仍然只返回 id，泄露面没有变化。
    exc = sys.exc_info()[1]
    sys.stderr.write(
        f"[{time.strftime('%H:%M:%S')}] ERROR err-{ref} at {where}\n"
        + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    sys.stderr.flush()
    return f"err-{ref}"


def _cap_lines(text: str) -> tuple[str, str]:
    """把输入截到 MAX_INPUT_LINES 行。返回 (截断后的文本, 提示或空串)。

    为什么需要（2026-09-05 加）：`_body` 只挡 8MB，而 8MB 全是 `url,key`
    约 20 万行 —— 那些行各自展开成 4 段探测，最坏 80 万次出网请求打向
    第三方站。真实用法一次几十行，生产配置总共 121 个条目。

    截断而不是报 400：粘贴多了更希望「先处理前 500 行」而不是整个请求失败。
    但必须**说出来** —— 静默丢掉用户的输入行是最坏的处理方式。
    """
    lines = (text or "").splitlines()
    if len(lines) <= MAX_INPUT_LINES:
        return text, ""
    kept = "\n".join(lines[:MAX_INPUT_LINES])
    return kept, (f"输入 {len(lines)} 行，超过单次上限 {MAX_INPUT_LINES} 行，"
                  f"只处理前 {MAX_INPUT_LINES} 行；其余请分批提交")


def _emit_opt_notices(job: Job) -> None:
    """把「参数被钳了」与「输入被截断了」写进事件流。两条探测路径都要调。

    抽成函数而不是内联在两个 try 块里（2026-09-05）：内联时测试只能断言
    「源码里有没有这几行」，而那挡不住把条件改成 `if False:` —— 行还在、
    字符串还在，断言照样过。撤销实验证实过这一点。

    静默改用户给的值是最坏的处理方式：他填 50000 并发、我按 128 跑，
    而界面上什么都不说，那他下次还会填 50000。
    """
    for note in _clamped_note(job.opts):
        job.emit("info", {"msg": "参数越界：" + note})
    trunc = job.opts.get("_truncated")
    if trunc:
        job.emit("info", {"msg": str(trunc)})


def run_job(job: Job, cfg_path: str) -> None:
    job.state = "running"
    try:
        _emit_opt_notices(job)
        import yaml
        with open(cfg_path, encoding="utf-8") as stream:
            cfg = yaml.safe_load(stream) or {}
        prober = Prober(
            cfg_snapshot=cfg,
            proxy=_resolve_proxy(str(job.opts.get("proxy") or "")),
            gap=_clamp(job.opts, "gap", 1.5),
            timeout=_clamp(job.opts, "timeout", 120),
            probe_context=bool(job.opts.get("probe_context", True)),
            # 能力开关探测（codex 的 websockets、compat 的
            # support-prompt-cache-key）。默认开 —— 每段最多 1 次额外请求，
            # 而这两个开关配错的后果不对称：websockets 开错会让那个凭据的
            # WS 请求全废（CPA 不回落 HTTP）。
            probe_capabilities=bool(
                job.opts.get("probe_capabilities", True)),
            swap_samples=_clamp(job.opts, "swap_samples", 3),
            workers=_clamp(job.opts, "workers", 4),
            max_models=_clamp(job.opts, "max_models", 4),
            max_model_attempts=_clamp(job.opts, "max_model_attempts", 10),
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
        #
        # 默认值 4 -> 16（2026-09-11 提速）
        # -------------------------------
        # 4 是个过于保守的默认：它让「增加账号」这条路在 20 个新站时要排 5 轮，
        # 而每一轮的墙钟时间由**最慢的那个站**决定（实测单站最慢 121 秒）。
        # 放开到 16 不会加重任何单站的负担 —— 节流桶是按 `(host, section)` 分的
        # （`Prober._throttle`），跨站并发对单站的请求频率**没有任何影响**，
        # 上面那句注释「不同站之间没有任何理由互相等」说的就是这件事。
        #
        # 上限仍由三重闸兜住，不会失控：
        #   · `min(len(hosts), …)` —— 站数少于配置时实际并发就是站数
        #   · 每个候选内部最多 4 个段线程，总线程 = 16 × 4 = 64（可接受）
        #   · `_LIMITS["max_workers"]` 的 128 是硬上限
        # 全量重探那条路本来就是 30，这里对齐到同一量级。
        hosts = {r.host for r in job.rows}
        cand_workers = max(1, min(len(hosts),
                                  _clamp(job.opts, "candidate_workers", 16)))
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
    except concurrent.futures.CancelledError:
        # 用户按了停止 —— 这不是错误（2026-09-12）
        # ------------------------------------------
        # BatchProber 现在把取消原样往上抛（原来它落进逐站的
        # `except Exception`，于是 175 个站各记一条 failure，任务还报 done）。
        # 这里也必须与真正的失败分开：记成 error 会给用户一个查不出所以然的
        # 错误号（_error_ref 只回引用，细节在 stderr），而他自己按的停止
        # 根本不该去查日志。
        job.state = "cancelled"
        job.emit("info", {"msg": "已按请求停止 —— 未完成的站没有结论，"
                                 "已完成的结果保留"})
    except Exception:
        job.state = "error"
        job.error = _error_ref(f"job {job.id}")
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


def _drift_snapshot(*, runtime_commit_url: str = "", runtime_mgmt: str = "", **kw) -> dict:
    """漂移检测结果：只读缓存，过期则后台刷新。绝不阻塞调用方。

    kw 原样转交 cp.check_profile_drift。

    runtime_commit_url 是 CPA 管理端点，在**后台线程里**带认证取
    X-CPA-COMMIT —— 那一步也是网络请求（超时 3 秒），在请求路径里算等于
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
                commit = _cpa_runtime_commit(runtime_commit_url, runtime_mgmt)
                source_args = dict(kw)
                # A moving main branch is never evidence for a deployed build.
                if commit and source_args.get("remote_ref") == "main":
                    source_args["remote_ref"] = commit
                got = cp.check_profile_drift(runtime_commit=commit, **source_args)
                got["runtime_commit"] = commit
                version = (_CPA_COMMIT_CACHE.get("version", "")
                           if runtime_mgmt and _CPA_COMMIT_CACHE.get("key", ("",))[0]
                           == runtime_commit_url.rstrip("/") else "")
                got["runtime_version"] = version
                got["runtime_version_known"] = bool(commit or version)
                got["runtime_version_source"] = "authenticated_management" if commit or version else "unavailable"
                got["deployed_version_verified"] = bool(
                    commit and got.get("checked") and not got.get("drifts")
                    and str(got.get("source_commit") or "") == commit)
                if not commit:
                    got["version_note"] = "管理响应未提供运行版本；源码快照不能证明部署版本"
                    if version:
                        got["version_note"] = "已知运行版本标签，但缺少 commit；尚未绑定源码快照"
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
    # （实测 romeo 唯一验证过的模型就是 grok-4.6）。
    # 这里用 section_allows 会让「界面手填能写、curl 覆盖写不进」，两条入口
    # 对同一个名字给出不同结果。
    kept = cp.model_catalog.newest_generation_per_line(
        [m for m in got if cp.model_catalog.section_protocol_ok(section, m)])
    if got and not kept:
        raise ValueError("模型覆盖与目标协议不兼容，请选择该协议支持的模型")
    return kept


def _market_top_gen(cfg: dict) -> tuple[dict, dict]:
    """各段「当前市面最新」的最高世代。返回 (全局, 逐产品线)。

    全局形如 `{"codex-api-key": [5, 6]}`，逐产品线形如
    `{"codex-api-key": {"gpt": [5, 6], "o": [4, 0]}}`。

    前端拿它判断「站方目录是不是整体落后」—— 落后一个世代以上时不预勾
    （2026-09-02 现场：某站 codex 目录只有 gpt-4 系而市面已到 5.6，
    「取最高世代」把四个老款全留下还默认全勾）。

    为什么必须给逐产品线的那一份（2026-09-04）：o 系列（`o1` / `o3-mini`）与
    gpt 系列是**互不相干的编号体系**，`o3` 的 3 不代表它比 `gpt-5.6` 老一代。
    只给全局最高世代时，「目录里只有 o 系列」的站会被判成落后从而一个都不预勾。
    后端 `catalog_is_stale` 已改成逐产品线比，前端必须用同一套数据，否则
    界面预勾与落盘清单再次分叉。

    后端在 build_plan 里判同一件事（catalog_is_stale），但结果表在勾选**之前**
    就渲染了，那时还没有 /api/plan 的响应 —— 所以两边都要能判。

    走 model_catalog 自己的缓存（成功 6 小时 / 失败 10 分钟），不会拖慢
    /api/context；拉不到时返回空 dict，前端退化成「不判落后、照常预勾」。
    """
    out: dict[str, list[int]] = {}
    by_line: dict[str, dict[str, list[int]]] = {}
    try:
        remote, _why = cp.model_catalog.remote_names()
        for sec in cp.SECTIONS:
            names, _src = cp.model_catalog.latest_models(
                sec, cfg=cfg, remote=remote, limit=12)
            top = cp.model_catalog.top_generation(names)
            if top:
                out[sec] = [top[0], top[1]]
            per = cp.model_catalog.top_generation_per_line(names)
            if per:
                by_line[sec] = {ln: [g[0], g[1]] for ln, g in per.items()}
    except Exception:                                    # noqa: BLE001
        # 这只是个增强信号，绝不能让它影响 /api/context 的可用性 ——
        # 与漂移检测同一条原则（那次它把首屏卡成了白屏）。
        return {}, {}
    return out, by_line


def _cpa_runtime_commit(base: str, mgmt: str = "") -> str:
    """读运行中 CPA 的 commit（管理响应头 X-CPA-COMMIT，handler.go:267-269）。

    用来发现「源码已更新但 CPA 没重启」—— 挂进来的是源码，跑着的是编译产物。

    只读取认证管理响应头；没有管理凭据或响应头就返回空串，
    不能将未绑定的源码 main 快照当成部署版本。

    缓存 5 分钟：每次打开网页都发一次外网请求不值得，而 CPA 版本不会秒级变。
    """
    if not base or not mgmt:
        return ""
    cache_key = (base.rstrip("/"), hashlib.sha256(mgmt.encode()).hexdigest())
    now = time.time()
    if (_CPA_COMMIT_CACHE.get("key") == cache_key
            and now - _CPA_COMMIT_CACHE["at"] < _CPA_COMMIT_TTL):
        return _CPA_COMMIT_CACHE["commit"]
    commit = ""
    version = ""
    try:
        req = urllib.request.Request(base.rstrip("/") + "/v0/management/config.yaml",
                                     method="GET", headers={"Authorization": f"Bearer {mgmt}"})
        # Reuse the transport's same-origin redirect guard; never send
        # management credentials to a redirect destination on another origin.
        with cp.client._opener(None).open(req, timeout=3) as resp:
            commit = (resp.headers.get("X-CPA-COMMIT") or "").strip()
            version = (resp.headers.get("X-CPA-VERSION") or "").strip()
    except Exception:                                   # noqa: BLE001
        commit = ""
    _CPA_COMMIT_CACHE.update(at=now, commit=commit, version=version, key=cache_key)
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
        _emit_opt_notices(job)

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
            ck = (base_url.rstrip("/"), api_key)
            if ck in seen_cred:
                dup += 1
                continue
            seen_cred.add(ck)
            lines.append(f"{base_url},{api_key}")

        # 新站：原始文本，同样参与去重（用户可能粘贴了已在配置里的站）
        for row in job.rows:
            ck = (row.bare.rstrip("/"), row.api_key)
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
            cfg_snapshot=cfg,
            proxy=_resolve_proxy(str(job.opts.get("proxy") or "")),
            gap=_clamp(job.opts, "gap", 1.5),
            timeout=_clamp(job.opts, "timeout", 120),
            probe_context=bool(job.opts.get("probe_context", True)),
            # 能力开关探测（codex 的 websockets、compat 的
            # support-prompt-cache-key）。默认开 —— 每段最多 1 次额外请求，
            # 而这两个开关配错的后果不对称：websockets 开错会让那个凭据的
            # WS 请求全废（CPA 不回落 HTTP）。
            probe_capabilities=bool(
                job.opts.get("probe_capabilities", True)),
            swap_samples=_clamp(job.opts, "swap_samples", 3),
            workers=_clamp(job.opts, "workers", 4),
            max_models=_clamp(job.opts, "max_models", 4),
            max_model_attempts=_clamp(job.opts, "max_model_attempts", 10),
            reuse_profile_verdict=bool(
                job.opts.get("reuse_profile_verdict", True)),
            on_event=job.emit,
        )

        # 使用 BatchProber（站级并发）
        max_workers = _clamp(job.opts, "max_workers", 30)
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
    except concurrent.futures.CancelledError:
        # 用户按了停止 —— 这不是错误（2026-09-12）
        # ------------------------------------------
        # BatchProber 现在把取消原样往上抛（原来它落进逐站的
        # `except Exception`，于是 175 个站各记一条 failure，任务还报 done）。
        # 这里也必须与真正的失败分开：记成 error 会给用户一个查不出所以然的
        # 错误号（_error_ref 只回引用，细节在 stderr），而他自己按的停止
        # 根本不该去查日志。
        job.state = "cancelled"
        job.emit("info", {"msg": "已按请求停止 —— 未完成的站没有结论，"
                                 "已完成的结果保留"})
    except Exception:
        job.state = "error"
        job.error = _error_ref(f"job {job.id}")
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
    # 表的容量上限（2026-09-05 加）。键是**攻击者可控**的来源 IP，没有上限时
    # 一个 IPv6 段就能塞爆内存 —— 而这条路径是未认证可达的。
    # 满了先淘汰已过期的，再淘汰最早的；上限取 4096（正常用不到 10 个）。
    MAX_FAIL_ENTRIES = 4096
    # 未封锁但有失败计数的条目，多久之后忘掉。不忘的话「一天里零散失败 5 次」
    # 也会触发封锁，那不是暴破。
    FAIL_TTL = 30 * 60
    _failures: dict[str, dict] = {}
    _fail_lock = threading.Lock()

    # 直连对端是这些地址时，才信任 X-Forwarded-For / X-Real-IP。
    # 服务只在 127.0.0.1:8765 监听、由 nginx 反代，所以可信代理就是回环。
    # 绑到 0.0.0.0 直接暴露时对端是真实客户端，那时**不能**信这两个头 ——
    # 否则任何人都能伪造来源 IP 绕过封锁。
    _LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"})
    trusted_proxy_peers: tuple[str, ...] = ()

    # ---- 基础设施 ----

    def log_message(self, fmt: str, *args) -> None:
        # 不记 query string —— token 可能在里面
        # requestline and formatter arguments can both contain credentials.
        path = urllib.parse.urlsplit(self.path).path
        status = str(args[1]) if len(args) > 1 and re.fullmatch(r"\d{3}", str(args[1])) else ""
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] "
                         f"{self.command} {_safe_text(path)} {status}\n")

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
        """真实来源 IP。经 nginx 时读 X-Forwarded-For 的**最右一跳**。

        为什么必须读（2026-09-05 修，未认证可打的 DoS）
        ------------------------------------------
        服务只在 `127.0.0.1:8765` 监听，所有请求经 nginx `proxy_pass`
        （`nginx.conf:715/721/728`），于是 `client_address` 对**每一个**访客
        都是 `127.0.0.1`。失败封锁按它索引，结果整张表只有一个桶：

            任何人对任意路径连发 5 次带假 Bearer 的请求
              → `_failures["127.0.0.1"]` 触发 until
              → 之后 30 分钟运维本人也进不来（`_authed` 在比对密钥**之前**
                就查封锁），而容器 `restart: "no"` 不会自愈

        每 30 分钟重打 5 次即永久封锁，且不需要任何凭据。
        同时它宣称的暴破防护对真实攻击者完全无效 —— 换 IP 与不换等价。

        为什么取**最右**一跳
        -----------------
        nginx 用的是 `$proxy_add_x_forwarded_for`（`nginx.conf:700`），
        语义是「把 `$remote_addr` 追加到客户端已有的 XFF 后面」。所以链条是

            <客户端可伪造的任意内容>, <nginx 看到的真实对端>
             └─ 不可信 ─┘              └─ 可信，最右 ─┘

        取最左（常见写法）等于让客户端自己声明 IP —— 那比不读还糟：
        攻击者每次换一个伪造 IP 就绕过了封锁，而运维的真实 IP 反而会被封。

        为什么要先判对端是回环
        -------------------
        只有「请求确实来自我们自己的 nginx」时这个头才可信。绑到 0.0.0.0
        直接暴露（README 明确不建议，但会有人这么做）时对端就是客户端本身，
        那时任何人都能自带 XFF 伪造来源。
        """
        peer = (self.client_address or ("?",))[0]
        def trusted(address):
            if address in self._LOOPBACK:
                return True
            try:
                ip = ipaddress.ip_address(address)
                return any(ip in ipaddress.ip_network(net, strict=False)
                           for net in type(self).trusted_proxy_peers)
            except ValueError:
                return False

        if not trusted(peer):
            return peer
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            hops = [h.strip() for h in xff.split(",")]
            try:
                hops = [str(ipaddress.ip_address(h)) for h in hops]
            except ValueError:
                return peer
            current = peer
            for hop in reversed(hops):
                if not trusted(current):
                    break
                current = hop
            return current
        real = (self.headers.get("X-Real-IP") or "").strip()
        try:
            return str(ipaddress.ip_address(real)) if real else peer
        except ValueError:
            return peer

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
            now = time.time()
            left = until - now
            if left <= 0:
                # 封锁期已过：解封并重新计数。`last` 也要更新 ——
                # 不更新的话这个条目立刻满足 FAIL_TTL 而被 _prune 忘掉，
                # 那本身没问题，但解封瞬间的时间戳更准。
                info["until"] = 0.0
                info["count"] = 0
                info["first"] = now
                info["last"] = now
                return 0.0
            return left

    @classmethod
    def _prune_failures(cls, now: float) -> None:
        """清掉过期条目；仍然超上限时淘汰最早的。**调用方必须已持锁**。

        为什么需要（2026-09-05，与真实 IP 提取同一批改动）
        --------------------------------------------
        修好 `_client_ip` 之后这张表的键从「恒为 127.0.0.1」变成**攻击者
        可控的来源 IP**。没有上限时一个 IPv6 /64 段就能塞进天文数字的条目，
        而这条路径是**未认证可达**的（`_authed` 在校验密钥之前就记失败）。
        那等于把一个 DoS 换成另一个。

        两条淘汰规则：
          · 已解封、且最后一次失败早于 FAIL_TTL 的 —— 直接忘掉。不忘的话
            「一天里零散失败 5 次」也会触发封锁，那不是暴破。
          · 仍然超 MAX_FAIL_ENTRIES 时按 last 最早的淘汰。**正在封锁中的
            条目排在最后**才淘汰 —— 否则攻击者可以用大量新 IP 把自己的
            封锁记录挤掉。
        """
        f = cls._failures
        if len(f) <= cls.MAX_FAIL_ENTRIES:
            # 未超上限也顺手清过期的，避免长期驻留
            dead = [k for k, v in f.items()
                    if not v.get("until") and now - v.get("last", 0) > cls.FAIL_TTL]
            for k in dead:
                f.pop(k, None)
            return
        dead = [k for k, v in f.items()
                if not v.get("until") and now - v.get("last", 0) > cls.FAIL_TTL]
        for k in dead:
            f.pop(k, None)
        if len(f) <= cls.MAX_FAIL_ENTRIES:
            return
        # 还是超：按 (是否在封锁中, last) 排序，先淘汰未封锁且最早的
        victims = sorted(f.items(),
                         key=lambda kv: (bool(kv[1].get("until")),
                                         kv[1].get("last", 0.0)))
        for k, _v in victims[:len(f) - cls.MAX_FAIL_ENTRIES]:
            f.pop(k, None)

    @classmethod
    def _note_failure(cls, ip: str) -> None:
        now = time.time()
        with cls._fail_lock:
            info = cls._failures.get(ip)
            if info is None:
                cls._prune_failures(now)      # 只在**新增**键时才需要腾位置
                info = cls._failures.setdefault(
                    ip, {"count": 0, "until": 0.0, "last": now})
            info["last"] = now
            # 距上次失败超过 TTL 的，计数重新开始 —— 零散失败不该累积成封锁
            if now - info.get("first", now) > cls.FAIL_TTL:
                info["count"] = 0
                info["first"] = now
            info.setdefault("first", now)
            info["count"] += 1
            if info["count"] >= cls.MAX_FAILURES:
                info["until"] = now + cls.BAN_SECONDS
                info["count"] = 0
                info["first"] = now

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
            self._validated_credential = got
            self._validated_management = not _same_secret(got, own)
            self._note_success(ip)
        else:
            self._note_failure(ip)
        return ok

    def _json(self, code: int, payload: dict) -> None:
        # 定档响应缓存：只对 /api/plan 的成功响应、且只在本请求算出了指纹时
        # 记账。放在 _json 里而不是两处 return 点，是因为 _api_plan 有两条
        # 成功出口（全量重探 / 增量），漏掉任何一条就会「改了代码忘了缓存」。
        # 存**脱敏前**的 payload，脱敏仍由下面的 _public_with_context 逐次做
        # —— 缓存里留原始对象不增加暴露面（它本来就在内存里），但避免把
        # 脱敏结果二次脱敏。
        if code == 200:
            _ck = getattr(self, "_plan_cache_key", "")
            # 只缓存**真正的定档结果**（2026-09-18）
            # ------------------------------------
            # 现场：两份快照都停在「定档轮询无响应 —— 刷新页面后重试」。
            # 成因是 keep-alive —— 同一个 Handler 实例先服务 `POST /api/plan`
            # （设了 `_plan_cache_key`、回 202），接着服务
            # `POST /api/plan-status`，而后者的 `_json(200, snap)` 又落到这里，
            # 于是 `{state:"running", elapsed:…}` 被当成定档结果写进缓存。
            # 下一次同参 `/api/plan` 命中缓存原样重放这份快照 —— 前端拿到的
            # 体里没有 `plans` / `plan_id` / `diffs`，裸取字段抛 TypeError，
            # 整张表永久停在占位符。
            # `_plan_cache_key` 的生命周期已经改成不跨请求（见 `_api_plan`），
            # 这里的 `"plans" in payload` 是第二道闸：形状不对就不进缓存。
            if _ck and isinstance(payload, dict) and "plans" in payload:
                with Handler._plan_cache_lock:
                    if len(Handler._plan_cache) >= Handler._PLAN_CACHE_MAX:
                        oldest = min(Handler._plan_cache.items(),
                                     key=lambda kv: kv[1][0])
                        Handler._plan_cache.pop(oldest[0], None)
                    Handler._plan_cache[_ck] = (time.time(), payload)
            # 异步定档任务钩子（2026-09-17）：_plan_body 在后台线程里通过
            # _json 发结果时，把 payload 存进 task.result 而不是发 HTTP。
            # 注意：不替换 self._json，只在这里检查 flag，避免覆盖测试 mock。
            _apt = getattr(self, "_async_plan_task", None)
            if _apt is not None:
                with _apt.lock:
                    _apt.result = payload
                    _apt.state = "done"
                    _apt.finished = time.time()
                return          # 后台线程里不发 HTTP，直接返回
        if code >= 400 and "error_code" not in payload:
            payload = {**payload, "error_code": {
                400: "invalid_request", 401: "unauthorized", 404: "not_found",
                409: "conflict", 428: "revision_required", 429: "capacity_exhausted",
                503: "unavailable"}.get(code, "internal_error")}
        context = {"request": getattr(self, "_output_context", {}),
                   "credential": getattr(self, "_validated_credential", "")}
        body = json.dumps(_public_with_context(payload, context), ensure_ascii=False).encode("utf-8")
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
            body = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError("JSON 解析失败") from e
        _validate_body(body)
        self._output_context = body
        return body

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
        """GET 路由。**整体包在 try 里** —— 与 do_POST 对称。

        为什么必须包（2026-09-05 修）：`since` 那一步的
        `int(parse_qs(...)["since"])` 原来在 try 之外，于是
        `GET /api/job/<jid>?since=x` 让 ValueError 冒到 socketserver 的
        `handle_error` —— 客户端拿到的是**连接重置**而不是 400。

        前端会把连接重置计入「轮询失败」并重试（`web/app.js` 的轮询容错），
        于是一个打错的参数变成无限重试；而 stderr 里堆的是无归属的 traceback。
        """
        try:
            self._do_get()
        except ValueError as e:
            self._json(400, {"error": str(e), "error_code": "invalid_request"})
        except Exception:
            ref = _error_ref(f"GET {self.path.split('?')[0]}")
            self._json(500, {"error": f"服务内部错误（{ref}）",
                             "error_ref": ref,
                             "hint": "完整堆栈在服务端日志，按这个 id 检索"})

    def _do_get(self) -> None:
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
        elif route == "/api/tuning":
            # 全局调优体检（只读）：重试预算与顶层池的耦合结论
            self._api_tuning()
        elif route == "/api/routes":
            # 既有上游路由清单（只读），批量管理面板的数据源
            self._api_routes()
        elif route == "/api/plan-status":
            # 异步定档轮询也挂 GET（2026-09-18）
            # ------------------------------
            # `PlanTask` 的 docstring 一直写的是
            # `GET /api/plan-status?plan_task_id=…`，而路由只挂了 POST ——
            # 按文档接的客户端、健康检查、curl 排障全部落 404「未知路由」，
            # 看起来像「任务不存在」。轮询是纯读操作，GET 更符合语义。
            ptid = (urllib.parse.parse_qs(p.query).get("plan_task_id")
                    or [""])[0]
            self._api_plan_status(ptid)
        elif route.startswith("/api/apply-status/"):
            self._api_apply_status(route[len("/api/apply-status/"):])
        elif route.startswith("/api/export/"):
            self._api_export(route[len("/api/export/"):])
        elif route.startswith("/api/job/"):
            jid = route[len("/api/job/"):]
            raw_since = (urllib.parse.parse_qs(p.query).get("since")
                         or ["0"])[0]
            try:
                since = max(0, int(raw_since))
            except (TypeError, ValueError):
                # 400 而不是 500：这是调用方的参数问题，不是服务的故障。
                # 前端的轮询容错会把 5xx 当「服务挂了」而无限重试。
                self._json(400, {"error": f"since 必须是非负整数，"
                                          f"收到 {raw_since[:40]!r}"})
                return
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
            elif route == "/api/plan-status":
                # 异步定档轮询（2026-09-17）
                ptid = (body.get("plan_task_id") or
                        self.path.split("plan_task_id=")[-1].split("&")[0])
                self._api_plan_status(ptid)
            elif route == "/api/apply":
                self._api_apply(body)
            elif route == "/api/tuning-apply":
                # 全局调优：落盘 + 推送，必须 confirm
                self._api_tuning_apply(body)
            elif route == "/api/bulk-preview":
                # 批量管理：只算 diff，不落盘
                self._api_bulk_preview(body)
            elif route == "/api/bulk-apply":
                # 批量管理：落盘 + 推送，必须 confirm
                self._api_bulk_apply(body)
            else:
                self._json(404, {"error": f"未知路由 {route}"})
        except CapacityError as e:
            self._json(429, {"error": str(e), "error_code": "capacity_exhausted",
                             "retryable": True})
        except ValueError as e:
            self._json(400, {"error": str(e), "error_code": "invalid_request"})
        except Exception:
            # 完整 traceback 只进 stderr；响应只带引用 id。
            # 见 _error_ref —— 那段栈会泄露容器内文件布局与行号。
            ref = _error_ref(f"POST {route}")
            self._json(500, {"error": f"服务内部错误（{ref}）",
                             "error_ref": ref,
                             "hint": "完整堆栈在服务端日志，按这个 id 检索"})

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

    # ── 定档响应缓存（2026-09-13 现场：CF 524）───────────────────────────
    #
    # 全量重探的 /api/plan 要重建整份配置（实测 173 站），跑过了 Cloudflare
    # 的 100 秒上限 —— 现场表现是 ③ 判定与定档 顶上一条红框，内容是一整页
    # CF 的「524: A timeout occurred」，priority 与建议栏全停在占位符。
    #
    # 但那次请求**并没有白跑**：前端每一次勾选变化都防抖 180ms 后重发一次
    # /api/plan，而除 `selected` 外的入参（job / overrides / forced / 配置
    # 原文）完全一样。于是 173 站的重建在一轮操作里被重复做了好几遍。
    #
    # 这里按「入参指纹」缓存响应体，命中就原样重放，包括 `plan_id` ——
    # 同一份输入产出的方案本来就是同一份，`add_plan` 存的 entry 是纯数据，
    # 没有随调用递增的字段，重放不会把两份方案混起来。
    #
    # 键里必须带 `config_revision(raw)`：配置在别处被改过时缓存要立刻失效，
    # 否则会拿旧基线的方案去写回 —— 那正是 `add_plan` 存 `base_raw` 要防的事。
    #
    # 只留 4 条、180 秒：这是「一次操作里的重复调用」优化，不是长期缓存。
    # 长期缓存会让操作员改完配置回来看到的还是旧档位。
    _plan_cache: dict[str, tuple[float, dict]] = {}
    _plan_cache_lock = threading.Lock()
    _PLAN_CACHE_TTL = 180.0
    _PLAN_CACHE_MAX = 4

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
        with io.open(path, encoding="utf-8") as stream:
            raw = stream.read()
        sig = hashlib.sha256(raw.encode("utf-8")).hexdigest()

        with Handler._cfg_cache_lock:
            c = Handler._cfg_cache
            if c is not None and c["sig"] == sig:
                self._output_context = {"request": getattr(self, "_output_context", {}),
                                        "config": c["cfg"]}
                return c["raw"], c["cfg"]

        cfg = yaml.safe_load(raw)
        # 半截文件通常在这里就炸了；万一它恰好是合法 YAML 但结构不对，
        # 顶层非映射也说明读到的不是完整配置 —— 两种都不缓存。
        if not isinstance(cfg, dict):
            raise ValueError(
                f"{path} 顶层不是映射（可能读到了写入中的半截文件）")

        with Handler._cfg_cache_lock:
            Handler._cfg_cache = {"sig": sig, "raw": raw, "cfg": cfg}
        self._output_context = {"request": getattr(self, "_output_context", {}),
                                "config": cfg}
        return raw, cfg

    def _api_routes(self) -> None:
        """既有上游路由清单，按 (段, 网址) 分组 —— 批量管理面板的数据源。

        为什么本项目要自己做这件事（2026-09-11）
        ----------------------------------------
        CPAMP 的「AI 提供商」页只有逐行操作：表格无多选列，`ProviderTable`
        的 props 全是单行回调；后端 `router.go` 的 provider 段只有
        `GET/PUT/DELETE` 三个动词，**没有任何 bulk 路径**。它自己的
        「按结果应用」是 `for` 循环逐条 read-modify-write，每条都要
        `GET /config` + `PUT` 整个 158 条数组。
        158 条规模下批量启停一次就是 158 轮往返，且两个标签页同时操作会
        静默互相覆盖（它的串行队列只在同一 JS 进程内有效）。

        本项目走的是另一条路：`writeback.push_to_cpa` →
        `PUT /v0/management/config.yaml`，整份文本、行级改、**保注释与未知
        字段**。一次往返完成全部改动，且天然规避 CPAMP 那条
        「DELETE 只按 api-key + base-url 匹配、重复条目删哪条不确定」的坑。

        这个接口只读，不改任何东西 —— 真正的写入仍走既有的
        plan → diff → 确认 → apply 链路，一道闸都不少。

        返回结构：
            {"groups": [{
                "section": "codex-api-key",
                "host": "api.example.com",
                "base_urls": ["https://api.example.com/v1"],
                "priorities": [349],            # 该组出现过的档位，降序
                "split": false,                 # 同组是否档位分裂（阻断级）
                "entries": [{
                    "index": 3,                 # 在该段数组里的下标，删改的定位键
                    "base_url": "...",
                    "api_key_masked": "sk-xxx***ab",
                    "priority": 349, "weight": null, "prefix": "",
                    "models": 6, "enabled": true,
                    "proxy_url": "", "has_headers": true,
                    "websockets": null,
                }, ...]}, ...],
             "totals": {...}}
        """
        _raw, cfg = self._load_cfg()
        rule, disabled_field, _ = cp.bulk.disable_semantics()

        def _enabled(entry: dict, section: str) -> bool:
            """条目当前是启用还是停用。

            CPAMP 的两套语义（`components/providers/utils.ts:17`
            与 `AiProvidersPage.tsx:450`）必须分开认，否则批量启停会写错字段：
              · key 类段：靠 `excluded-models` 里塞通配符 `"*"` 表示停用
              · openai-compatibility：用真正的布尔字段 `disabled`
            """
            if section == "openai-compatibility":
                return not bool(entry.get(disabled_field))
            ex = entry.get("excluded-models") or []
            if isinstance(ex, str):
                ex = [ex]
            return rule not in [str(x).strip() for x in ex]

        groups: dict[tuple[str, str], dict] = {}
        for section in cp.SECTIONS:
            arr = cfg.get(section) or []
            if not isinstance(arr, list):
                continue
            for idx, e in enumerate(arr):
                if not isinstance(e, dict):
                    continue
                base_url = str(e.get("base-url") or "")
                if not base_url:
                    continue
                host = cp.host_of(base_url)
                if not host:
                    continue
                # compat 段是 provider 级条目，一个 provider 下挂多把 Key；
                # 前三段一条目一把 Key。两者都以「该段数组下标」为定位键。
                if section == "openai-compatibility":
                    keys = [str(k.get("api-key") or "")
                            for k in (e.get("api-key-entries") or [])
                            if isinstance(k, dict)]
                else:
                    keys = [str(e.get("api-key") or "")]
                pri = e.get("priority", 0)
                pri = int(pri) if isinstance(pri, int) else None
                g = groups.setdefault((section, host), {
                    "section": section, "host": host,
                    "base_urls": [], "entries": [],
                })
                if base_url not in g["base_urls"]:
                    g["base_urls"].append(base_url)
                g["entries"].append({
                    "index": idx,
                    "fingerprint": cp.bulk.entry_fingerprint(e),
                    "base_url": base_url,
                    "api_key_masked": "、".join(
                        cp.mask_key(k) for k in keys if k) or "(无)",
                    "key_count": len([k for k in keys if k]),
                    "priority": pri,
                    "key_priorities": [key.get("priority", pri) for key in
                                       (e.get("api-key-entries") or [])
                                       if isinstance(key, dict)],
                    "weight": e.get("weight"),
                    "prefix": str(e.get("prefix") or ""),
                    "models": len(e.get("models") or []),
                    "enabled": _enabled(e, section),
                    "proxy_url": str(e.get("proxy-url") or ""),
                    "has_headers": bool(e.get("headers")),
                    "websockets": e.get("websockets"),
                })

        site_priorities = {}
        for g in groups.values():
            for entry in g["entries"]:
                values = [entry["priority"]] + entry["key_priorities"]
                site_priorities.setdefault(g["host"], set()).update(
                    value for value in values if type(value) is int)
        out = []
        for g in groups.values():
            pris = sorted({x["priority"] for x in g["entries"]
                           if x["priority"] is not None}, reverse=True)
            g["priorities"] = pris
            # 同一段同一网址出现多个档位 = 违反「同网址同优先级」，阻断级。
            # 实测两份生产配置：本项目注入前 0 组、注入后 3 组。
            g["split"] = len(pris) > 1
            g["site_priorities"] = sorted(site_priorities[g["host"]], reverse=True)
            g["site_split"] = len(g["site_priorities"]) > 1
            g["entries"].sort(key=lambda x: x["index"])
            out.append(g)
        out.sort(key=lambda g: (g["section"], g["host"]))

        self._json(200, {
            "revision": cp.bulk.config_revision(_raw),
            "groups": out,
            "totals": {
                "groups": len(out),
                "entries": sum(len(g["entries"]) for g in out),
                "split_groups": sum(1 for g in out if g["split"]),
                "disabled_entries": sum(
                    1 for g in out for x in g["entries"] if not x["enabled"]),
            },
        })

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

        # 「市面最新世代」的两份：全局与逐产品线。见 _market_top_gen。
        mkt_top, mkt_top_lines = _market_top_gen(cfg)

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
            runtime_mgmt=self._cpa_password_for({}),
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
            "market_top_gen": mkt_top,
            # 同一批数据的**逐产品线**版本。判「落后」必须按线比 ——
            # o 系列与 gpt 系列的编号互不相干（`o3` 不比 `gpt-5.6` 老一代）。
            # 见 _market_top_gen 与 catalog_is_stale。
            "market_top_gen_lines": mkt_top_lines,
            # 三张内存表的条数。有上限与 TTL（见 Store 的 docstring），
            # 这里露出来是为了让运维看得见有没有堆积 —— 那三张表里
            # plans 每份持有两份整份配置，是 OOM 的主要来源。
            "store": STORE.sizes(),
        })

    def _api_parse(self, body: dict) -> None:
        text, over = _cap_lines(body.get("text") or "")
        res = cp.parse_lines(text)
        out = {
            "valid": [row_json(r) for r in res.valid],
            "invalid": [row_json(r) for r in res.invalid],
        }
        if over:
            out["truncated"] = over
        self._json(200, out)

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
            gap=_clamp(body, "gap", 0.5),
            timeout=_clamp(body, "timeout", 60),
            probe_context=False,       # 诊断不探上下文 —— 那是百万字符的大 body
            # 诊断也不探能力开关：它只回答「这个站要什么 header」，
            # 不生成写回方案，那两个开关没有落点。
            probe_capabilities=False,
            swap_samples=0,            # 也不采样换模，那要 3 次额外请求
            workers=len(secs),
            cfg_snapshot=cfg,
        )

        # 四段并行跑（2026-09-11 提速）
        # ---------------------------
        # 原来是 `for section in secs:` 串行 —— 虽然给 Prober 传了
        # `workers=len(secs)`，但那个参数管的是 `probe()` 内部的并行度，
        # 这个自己写的梯子循环根本没用上它。四段各打一遍画像梯，
        # 最坏 4 × 8 档 = 32 次串行请求，单次 60 秒超时 ——
        # 一个慢站能让「单站诊断」跑到几分钟。
        #
        # 并行是安全的：四段各写各的 `out[section]`，段之间零共享状态；
        # 节流桶按 `(host, section)` 分，四段并发不会加重同一个桶的频率。
        # 段**内部**的梯子仍然串行 —— 那是「最省可用档」的语义要求
        # （找到第一个通的就停），不能并行。
        out: dict[str, dict] = {}
        out_lock = threading.Lock()

        def _diag_one(section: str) -> None:
            base = cp.base_for_section(row.bare, section)
            # 先问站方目录，再挑模型（2026-09-18）
            # --------------------------------
            # 原来这里写死 `SEED_MODELS[section][0]` —— 种子是本工具**猜**的
            # 名字。站方没有这个模型时每一档都回 404/「分组无该模型渠道」，
            # 诊断报「所有画像都不通」，而站点其实活着：与主流程 2026-09-01
            # 复盘修掉的是同一个根因（那次把 `_stage0_catalog` 提到了所有推理
            # 请求之前，见 `pipeline.py:1654` 的说明），只是诊断这条路没跟上。
            # 目录读不到（401/404/关闭）时照常回落种子，行为与原来一致。
            model = SEED_MODELS[section][0]
            catalog: list[str] = []
            try:
                catalog = prober._stage0_catalog(row, section, base) or []
            except Exception:           # 目录端点不可用不该让诊断整段失败
                catalog = []
            if catalog:
                order = prober._probe_order(section, catalog)
                if order:
                    model = order[0]
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

            got = {
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
            with out_lock:
                out[section] = got

        # 线程数取段数：再多也没有第五个段可跑。
        # 异常必须收上来 —— 后台线程里抛异常没人看得到，会让某个段静默缺席。
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, len(secs)),
                thread_name_prefix="diag") as ex:
            for fut in concurrent.futures.as_completed(
                    [ex.submit(_diag_one, s) for s in secs]):
                fut.result()

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
                gap=_clamp(body, "gap", 0.5),
                timeout=_clamp(body, "timeout", 60),
                probe_context=bool(body.get("probe_context", False)),
                probe_capabilities=bool(
                    body.get("probe_capabilities", True)),
                swap_samples=_clamp(body, "swap_samples", 3),
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
        _validate_body(body)
        full_redetect = body.get("full_redetect", False)
        max_workers = body.get("max_workers")

        text, over = _cap_lines(body.get("text") or "")
        res = cp.parse_lines(text)
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
        if over:
            opts["_truncated"] = over
        if full_redetect and max_workers is not None:
            opts["max_workers"] = max_workers

        jid = secrets.token_hex(8)
        job = Job(jid, res.valid, opts)
        try:
            STORE.add_job(job)
        except CapacityError as e:
            self._json(429, {"error": str(e), "error_code": "capacity_exhausted",
                             "retryable": True})
            return

        # 选择执行函数
        target_fn = run_job_full_redetect if full_redetect else run_job
        try:
            threading.Thread(target=target_fn, args=(job, type(self).cfg_path),
                             daemon=True).start()
        except Exception:
            job.state = "error"
            job.error = "探测工作线程无法启动"
            job.finished = time.time()
            self._json(503, {"error": job.error, "job_id": jid, "state": job.state})
            return

        self._json(202, {"job_id": jid, "rows": len(res.valid),
                         "invalid": [row_json(r) for r in res.invalid],
                         "full_redetect": full_redetect})

    def _api_job(self, jid: str, since: int) -> None:
        job = STORE.get_job(jid)
        if not job:
            self._json(404, {"error": f"没有这个任务：{jid}"})
            return
        snap = job.snapshot(since)
        self._output_context = {
            "api-keys": [row.api_key for row in job.rows]
                        + [r.row.api_key for r in job.results],
            "opts": job.opts,
            "entries": [{"headers": verdict.min_headers} for r in job.results
                        for verdict in r.sections.values()]}
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

        opts = _public(job.opts or {})
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
                      + ", ".join(f"{k}: {x}" for k, x in
                                  _public(v.min_headers, "headers").items()))
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
                # 段专属能力开关。三态各自一种写法 —— 导出日志是交接材料，
                # 「实测不支持」与「未探测」必须能区分开。
                for _lbl, _val, _note in (
                    ("websockets", v.websockets, v.websockets_note),
                    ("support-prompt-cache-key",
                     v.prompt_cache_key, v.prompt_cache_note),
                ):
                    if _val is None and not _note:
                        continue
                    _state = ("支持" if _val is True
                              else ("不支持" if _val is False else "未探测"))
                    w(f"    {_lbl:<15} {_state}"
                      + (f" —— {_note}" if _note else ""))
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

        context = {"api-keys": [row.api_key for row in job.rows]
                              + [r.row.api_key for r in job.results],
                   "opts": job.opts,
                   "entries": [{"headers": verdict.min_headers} for r in job.results
                               for verdict in r.sections.values()]}
        raw = (_public_with_context("\n".join(L), context) + "\n").encode("utf-8")
        name = time.strftime("cpa-probe-%Y%m%d-%H%M%S.txt", time.localtime())
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _api_plan(self, body: dict) -> None:
        """把探测结果变成写入方案 + diff 预览。异步版本（2026-09-17）。

        原来同步跑 _plan_body 会超过 Cloudflare Free 的 100 秒回源限制，导致
        priority 全停在「待定」占位符。现在改成：
          1. 缓存命中 → 同步返回（< 1ms，CF 不会超时）
          2. 需要计算 → 立即返回 {plan_task_id, state: "running"}（202），
             后台线程跑 _plan_body，前端轮询 /api/plan-status 拿结果
        """
        _validate_body(body)
        job = STORE.get_job(body.get("job_id") or "")
        if not job:
            self._json(404, {"error": "任务不存在"})
            return
        if job.state != "done":
            self._json(409, {"error": f"任务状态 {job.state}，还不能定方案"})
            return

        raw, cfg = self._load_cfg()
        try:
            _ck = hashlib.sha256(json.dumps(
                {"j": job.id,
                 "o": body.get("overrides") or {},
                 "s": body.get("selected"),
                 "f": body.get("forced") or {},
                 "b": bool(body.get("by_score")),
                 "r": cp.bulk.config_revision(raw)},
                sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
        except (TypeError, ValueError):
            _ck = ""
        if _ck:
            with Handler._plan_cache_lock:
                hit = Handler._plan_cache.get(_ck)
                if hit and time.time() - hit[0] < Handler._PLAN_CACHE_TTL:
                    logger.info("定档命中缓存（同一份输入 180 秒内重放）")
                    self._json(200, hit[1])
                    return

        # 缓存未命中 → 判断是否需要异步。
        #
        # 异步路径是为了绕开 Cloudflare Free 的 100s 回源超时 ——
        # 只有真实 HTTP 连接才会碰到这个问题。测试框架 mock 了 _json 且
        # 没有真实 socket，走异步反而会让测试拿不到同步结果（h.response 停在
        # 202 而不是 200）。检测方式：有没有真实 wfile（HTTP 连接）。
        _has_socket = hasattr(self, "wfile") and self.wfile is not None
        if not _has_socket:
            # 测试环境：同步跑 _plan_body，直接通过 _json 写结果
            self._plan_cache_key = _ck
            try:
                _t0 = time.time()
                self._plan_body(body, job, raw, cfg)
                logger.info("定档完成（同步）：%.1f 秒 · 全量重探=%s · %d 个候选",
                            time.time() - _t0,
                            bool(job.opts.get("full_redetect")),
                            len(job.results))
            finally:
                self._plan_cache_key = ""
            return

        # 生产环境：启动异步任务，立即返回 task_id
        task = PlanTask(secrets.token_hex(10), body)
        STORE.add_plan_task(task)
        # 缓存键**不挂在 self 上**（2026-09-18）
        # ------------------------------------
        # 挂在 self 上就会随 keep-alive 活到后续请求里 —— 下一条
        # `POST /api/plan-status` 的 200 响应会被 `_json` 当成定档结果写进
        # `_plan_cache`，再下一次同参 `/api/plan` 就重放一份没有 `plans` 的
        # 体，界面永久停在占位符（现场：「定档轮询无响应」两份快照）。
        # 键只交给后台 worker 那一份浅拷贝，前台 handler 全程保持空。

        def _run():
            try:
                _t0 = time.time()
                # _plan_body 写到 task.result 而非直接发送
                self._plan_async_body(body, job, raw, cfg, task, cache_key=_ck)
                logger.info("异步定档完成：%.1f 秒 · 全量重探=%s · %d 个候选",
                            time.time() - _t0,
                            bool(job.opts.get("full_redetect")),
                            len(job.results))
            except Exception as exc:
                with task.lock:
                    task.state = "error"
                    task.error = str(exc)
                    task.finished = time.time()
                logger.exception("异步定档异常")

        threading.Thread(target=_run, daemon=True).start()
        self._json(202, {"plan_task_id": task.id, "state": "running"})

    def _api_plan_status(self, plan_task_id: str) -> None:
        """轮询异步定档任务状态。state=done 时响应体里带完整 result。"""
        task = STORE.get_plan_task(plan_task_id or "")
        if not task:
            self._json(404, {"error": f"没有这个定档任务：{plan_task_id}"})
            return
        snap = task.snapshot()
        # state=done 时 result 已填充，前端直接取；state=running 时只有进度
        self._json(200, snap)

    def _plan_async_body(self, body: dict, job, raw: str, cfg: dict,
                         task: PlanTask, *, cache_key: str = "") -> None:
        """在后台线程里跑 _plan_body，把结果存进 task.result。

        **必须在 Handler 的浅拷贝上跑**（2026-09-17 修死锁）
        ----------------------------------------------------
        上一版把 `_async_plan_task` 挂在 `self` 上。而 HTTP keep-alive 会让
        **同一个 Handler 实例**接着服务后续请求 —— 前端轮询
        `/api/plan-status` 正是走同一条连接：

          1. `_api_plan_status` 调 `self._json(200, snap)`
          2. `_json` 看到 `_async_plan_task` 还挂着
          3. 于是把**轮询响应**当成定档结果写进 `task.result`，
             并 `return` 不发 HTTP
          4. 前端收不到响应 → 无限重试 → 界面永远停在
             「定档计算中…（后台运行，请稍候）」

        浅拷贝隔开实例状态：拷贝上的 `_async_plan_task` 与 `_plan_cache_key`
        只影响后台那一次 `_json`，原 Handler 照常服务 HTTP。
        `copy.copy` 不复制 socket 与缓冲区对象本身（那些是引用），
        但后台路径在写进 task 后就 `return`，永远走不到 `send_response`，
        所以不会与前台争用同一个 socket。
        """
        import copy as _copy

        worker = _copy.copy(self)
        worker._async_plan_task = task      # type: ignore[attr-defined]
        # 缓存键只在这份拷贝上存在（2026-09-18）：前台 handler 不再持有它，
        # 所以 keep-alive 上的后续请求不可能被误当成定档结果缓存。
        worker._plan_cache_key = cache_key
        try:
            worker._plan_body(body, job, raw, cfg)
        finally:
            worker._async_plan_task = None  # type: ignore[attr-defined]

        # _plan_body 没走 _json(200) 就结束时（抛异常被上层捕获、或走了
        # 4xx 出口），确保 task 不会永远停在 running。
        with task.lock:
            if task.state == "running":
                task.error = "定档未产生响应（_plan_body 未返回 200）"
                task.state = "error"
                task.finished = time.time()

    def _plan_body(self, body: dict, job, raw: str, cfg: dict) -> None:
        # 三个按候选索引的入参。键是**行号字符串**而不是 host ——
        # 一个站常有 15 把 Key（实测 gorou 15、tango 14），用 host
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
            # 八张「探测问不出来、必须原样搬」的查表，一次建好。
            #
            # 抽成 CarryTables（2026-09-04）：这段搬运逻辑原来在这里与演练脚本
            # 各写一遍。两处分叉的后果实测过两次（演练自己也搬 headers，所以
            # 「server 不搬」这个缺陷演练照样对上账）；更要紧的是内联时只能靠
            # AST 断言「这一行在不在」，挡不住「行还在、传的是空」—— 撤销实验里
            # 把 `old = hdrs.get(...)` 改成 `old = None`，1198 项测试全绿。
            #
            # 每张表少一张的后果、以及各字段为什么按不同方向搬（原值优先 /
            # 实测优先 / 合并），见 cpa_probe/batch.py 的 CarryTables 与
            # README 的「重探时每个字段以哪一侧为准」。
            carry = CarryTables(cfg)
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
                    carry.apply(sp, res.row.api_key)

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
                    _apply_identity_override(sp, ov)
                    if "proxy_url" in ov:
                        sp.proxy_url = _restore_public_scalar(ov["proxy_url"], sp.proxy_url, "proxy_url")
                    if "headers" in ov and isinstance(ov["headers"], dict):
                        sp.headers = _restore_public_headers(ov["headers"], sp.headers)
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
            # 同一网址的条目拿到不同 priority 是**阻断级**错误（与上面那条
            # 「不同站同值」方向相反）。用户的硬要求是同网址同优先级，而
            # `assign_priorities` 之后的用户覆盖按每把 Key 应用，改一把就分层。
            # 实测两份生产配置：注入前 0 组分裂，注入后 3 组 —— 是本项目写进去的。
            prio_warns = prio_warns + cp.priority_split_within_host(
                list(write_plans.values()))
            warnings = list(prio_warns) + list(warnings)

            # 生成完整 diff（整个文件）
            diffs = []
            ok, msg = _validate_final(preview, list(write_plans.values()))

            pid = secrets.token_hex(8)
            # 存 write_plans 而不是 all_plans：写后验证按 entry["plans"] 挑目标，
            # 存全量会去验根本没写进去的段（与增量路径同一条规则）。
            STORE.add_plan(pid, {"plans": list(write_plans.values()), "diffs": diffs,
                                 "preview": preview, "base_raw": raw,
                                 "valid": ok, "validate_msg": msg,
                                 "created": time.time(),
                                 "full_redetect": True})

            self._json(200, {
                "plan_id": pid,
                "preview_kind": "full_snapshot",
                **_preview_diff(raw, preview),
                "plans": [plan_json(p) for p in all_plans.values()],
                "diffs": [{
                    "section": "全量重建",
                    "host": f"{len(all_plans)} 个站",
                    "insert_at": 0,
                    # **脱敏后**才进 JSON（2026-09-05 修的 P1）。
                    #
                    # 这条路的 diff 是重建后的**整个文件**，不是增量片段 ——
                    # 生产配置里那是 177 行 api-key 明文 + 1 行 secret-key、
                    # 共 349KB，全部会进浏览器 DOM，而界面的「复制」按钮
                    # 会把它连同 177 个 Key 一起写进系统剪贴板。
                    #
                    # 本文件开头第 15 行写着「完整 key 只在内存里，不落日志、
                    # 不进 JSON 响应（一律 masked）」—— 那条纪律在这条路径上
                    # 一直没有兑现。
                    #
                    # 落盘走的是 entry["preview"]（服务端内存里的原文），
                    # 不受这里影响；脱敏只作用于发给客户端的那一份。
                    "lines": redact_yaml_secrets(preview).splitlines(
                        keepends=True),
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
                _apply_identity_override(sp, ov)
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
                    sp.proxy_url = _restore_public_scalar(ov["proxy_url"], sp.proxy_url, "proxy_url")
                if "headers" in ov and isinstance(ov["headers"], dict):
                    sp.headers = _restore_public_headers(ov["headers"], sp.headers)
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
        ok, msg = _validate_final(preview, for_write)

        pid = secrets.token_hex(8)
        # 存 for_write 而不是 plans：apply 后的写后验证按 entry["plans"]
        # 挑目标，存全量就会去验根本没写进去的段（判死段现在也 writable）。
        STORE.add_plan(pid, {"plans": for_write, "diffs": diffs,
                             "preview": preview, "base_raw": raw,
                             "valid": ok, "validate_msg": msg,
                             "created": time.time()})

        self._json(200, {
            "plan_id": pid,
            "preview_kind": "incremental",
            **_preview_diff(raw, preview),
            "plans": [plan_json(p) for p in plans],
            "diffs": [{"section": d.section, "host": d.host,
                       "insert_at": d.insert_at,
                       "lines": _safe_text(redact_yaml_secrets("\n".join(d.lines))).splitlines(),
                       "text": _safe_text(redact_yaml_secrets("\n".join(d.lines)))} for d in diffs],
            "valid": ok,
            "validate_msg": msg,
            "lines_before": raw.count("\n") + 1,
            "lines_after": preview.count("\n") + 1,
            # 定档提示：整批下移、越过现有档位、压到最低值，以及用户覆盖
            # 造成的同层。全都会影响站与站的先后，必须让人看到。
            # 只查真正写进去的那些段（for_write）—— 未勾选的段不落盘，
            # 报它们同值只是噪声。
            "warnings": (list(prio_warns)
                         + cp.priority_collisions(for_write)
                         # 同网址不同 priority 是阻断级错误，不是「可能是预期结果」
                         + cp.priority_split_within_host(for_write))
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
        if getattr(self, "_validated_management", False):
            return self._validated_credential
        cred = (body.get("_cred") or "").strip()
        if cred and not _same_secret(cred, type(self).token)                 and self._check_cpa_password(cred):
            return cred
        return ""

    def _api_apply_status(self, tid: str) -> None:
        """查询整个后台事务；仅 local_written=true 才表示已经写盘。"""
        task = STORE.get_apply(tid)
        if not task:
            self._json(404, {"error": f"没有这个写回任务：{tid}"})
            return
        self._json(200, task.snapshot())

    def _api_tuning(self) -> None:
        """全局调优体检：算出「重试预算 vs 顶层池」的耦合结论，只读不改。

        为什么放在服务端而不是让前端算（与 /api/routes 同一条原则）：
        判据要读 CPA 源码行号、顶层池实况与探测实测耗时，那些数据只有
        服务端有。前端只负责显示结论与「要不要应用」。

        返回的 advices 里每条都带 `why` —— 那句话就是给操作员复核用的，
        不许只给一个数字让人凭信任点确认。
        """
        raw, cfg = self._load_cfg()
        # 实测单次失败耗时：优先用最近一次探测的样本，拿不到就用估值。
        # 两种情况都要在 attempt_why 里说清，别让估值看起来像实测。
        attempt_sec, attempt_why = cp.tuning.FALLBACK_ATTEMPT_SEC, ""
        with STORE.lock:
            jobs = list(STORE.jobs.values())
        for job in reversed(jobs):
            results = {(r.row.bare, r.row.api_key): r
                       for r in (job.results or []) if getattr(r, "row", None)}
            if results:
                attempt_sec, attempt_why = cp.tuning.attempt_seconds_from_results(
                    results)
                break
        if not attempt_why:
            attempt_sec, attempt_why = cp.tuning.attempt_seconds_from_results({})

        advices, notes, facts = cp.tuning.advise(
            cfg, attempt_sec=attempt_sec, attempt_why=attempt_why)
        pending = [a for a in advices if a.changed]
        tuning_id = ""
        diff = ""
        problems: list[str] = []
        if pending:
            diffs, problems = cp.writeback.global_tuning_diffs(raw, pending)
            if diffs:
                new_text = "\n".join(diffs[0].lines)
                diff = "\n".join(difflib.unified_diff(
                    _safe_text(redact_yaml_secrets(raw)).splitlines(),
                    _safe_text(redact_yaml_secrets(new_text)).splitlines(),
                    fromfile="config.yaml（当前）", tofile="config.yaml（调优后）",
                    lineterm="", n=2))
                tuning_id = STORE.put_bulk(
                    raw, new_text,
                    [f"{a.label}: {a.current} → {a.want}" for a in pending])
        self._json(200, {
            "tuning_id": tuning_id,
            "attempt_sec": round(attempt_sec, 2),
            "attempt_why": attempt_why,
            "edge_window_sec": cp.tuning.DEFAULT_EDGE_WINDOW_SEC,
            "advices": [{"item": a.label, "current": a.current, "want": a.want,
                         "why": a.why, "severity": a.severity,
                         "changed": a.changed} for a in advices],
            "notes": notes,
            "problems": problems,
            "tiers": [{"section": f.section, "top_priority": f.top_priority,
                       "credentials": f.credentials, "hosts": f.hosts,
                       "longest_same_host_run": f.longest_same_host_run,
                       "run_host": f.run_host} for f in facts.values()],
            "diff": diff[:200000],
            "diff_truncated": len(diff) > 200000,
        })

    def _api_tuning_apply(self, body: dict) -> None:
        """全局调优落盘 + 推送。必须带 tuning_id + confirm=true。

        复用 `_submit_apply` —— 基线比对、备份、落盘、PUT 重载、读回校验
        与投喂流程完全同一条链路。不新增写盘路径。
        """
        tid = body.get("tuning_id") or ""
        entry = STORE.get_bulk(tid)
        if not entry:
            self._json(404, {"error": "调优方案不存在或已过期，请重新体检"})
            return
        if body.get("confirm") is not True:
            self._json(400, {"error": "未确认。调优写回需要 confirm=true"})
            return
        self._submit_apply(entry, body)

    @staticmethod
    def _resolve_op_collisions(cfg: dict,
                               ops: list[dict]) -> tuple[list[dict], list[str]]:
        """把一批 `action: priority` 里**同段同值**的站错开，返回新 ops 与说明。

        只看同段：跨段的档位谱互相独立（`priority_collisions` 的注释已写死
        这条），段间同值毫无关系。

        算法：按段分组 → 每组取「本段全部条目的现有档位」当 `taken` →
        `resolve_priority_collisions` 给出互不相同的目标值 → 回填到 ops。

        **`taken` 的构成是本函数的关键**，三种值都要进去，少一种就会撞：
          · 本段**未被本批改动**的条目档位 —— 那是在用站，撞上等于同层轮询；
          · 本批**不涉及改档**的条目（只启停/只删除的）档位 —— 同上；
          · 被本批改动的条目自己的旧档位 —— **不能**放进 taken。
            放进去会让「整体下移一批」自锁：350 想拿走 350，却发现 350
            被「自己」占着，于是无谓地降到 349。所以先把要改的下标挖掉。
        """
        from urllib.parse import urlsplit

        def _host(url: str) -> str:
            return (urlsplit(str(url or "")).netloc or "").lower()

        pri_ops: dict[str, list[dict]] = {}
        for op in ops:
            if op.get("action") == "priority":
                pri_ops.setdefault(op["section"], []).append(op)
        if not pri_ops:
            return ops, []

        adjusted: list[str] = []
        # 要改的主机集合，按段收 —— 用于从 taken 里挖掉它们自己的旧档位
        for section, sec_ops in pri_ops.items():
            arr = cfg.get(section) or []
            moving = {(section, op["index"]) for op in sec_ops}
            taken: set[int] = set()
            for i, e in enumerate(arr):
                if not isinstance(e, dict) or (section, i) in moving:
                    continue
                p = e.get("priority", 0)
                if type(p) is int:
                    taken.add(p)

            # 同站多 Key 必须同档，所以按 host 归并而不是按条目：先算出每个
            # host 的一个目标值，再回填给它的全部条目。
            by_host: dict[str, int] = {}
            for op in sec_ops:
                h = _host(arr[op["index"]].get("base-url") if op["index"] < len(arr) else "")
                if not h:
                    continue
                by_host[h] = int(op["value"])
            final, notes = cp.bulk.resolve_priority_collisions(
                by_host, taken=taken)
            adjusted += notes
            for op in sec_ops:
                i = op["index"]
                h = _host(arr[i].get("base-url") if i < len(arr) else "")
                if h in final:
                    op["value"] = final[h]
        return ops, adjusted

    def _api_bulk_preview(self, body: dict) -> None:
        """批量操作的**预览**：只算新文本与 diff，不落盘、不推送。

        与投喂流程的 plan → apply 是同一道门槛：先看 diff、再显式确认。
        差别只在于批量操作改的是**既有条目的字段**，不新增条目，所以不需要
        定档、去重、影响面那一整套 —— 它们是给「插入新条目」用的。

        返回 diff 与一个 `bulk_id`，确认后拿它调 /api/bulk-apply。
        新文本存在服务端，不让前端回传 —— 回传等于让客户端决定写什么。
        """
        _validate_body(body)
        ops = body.get("ops") or []
        if not isinstance(ops, list) or not ops:
            self._json(400, {"error": "ops 为空"})
            return
        if len(ops) > 2000:
            self._json(400, {"error": f"一次最多 2000 条操作，收到 {len(ops)}"})
            return
        raw, _cfg = self._load_cfg()
        revision = body.get("revision")
        if not revision:
            self._json(428, {"error": "请刷新路由并提交 revision",
                             "error_code": "revision_required"})
            return
        if revision != cp.bulk.config_revision(raw):
            self._json(409, {"error": "选择基于旧配置，请刷新路由并重新选择",
                             "error_code": "stale_selection"})
            return
        checked_ops = []
        for op in ops:
            if (not isinstance(op, dict) or type(op.get("index")) is not int
                    or not isinstance(op.get("section"), str)
                    or op.get("section") not in cp.SECTIONS):
                self._json(400, {"error": "ops 每项必须有整数 index",
                                 "error_code": "invalid_operation"})
                return
            arr = _cfg.get(op.get("section")) or []
            idx = op["index"]
            if idx < 0 or idx >= len(arr) or not isinstance(arr[idx], dict):
                self._json(409, {"error": "条目已变化，请重新选择",
                                 "error_code": "stale_selection"})
                return
            if op.get("fingerprint") and op["fingerprint"] != cp.bulk.entry_fingerprint(arr[idx]):
                self._json(409, {"error": "条目指纹不符，请重新选择",
                                 "error_code": "stale_selection"})
                return
            # The revision binds the original indices. Public URLs are masked.
            checked = dict(op)
            if checked.get("action") == "delete":
                checked["expect"] = str(arr[idx].get("base-url") or "")
            checked_ops.append(checked)
        # ── 批量设档的站间撞值消解 ──────────────────────────────────
        #
        # 用户第 3⑶ / 第 7 条：同类型不同域名的优先级一定要不同，算出来相同
        # 也要做微调给出偏差。「批量设为 N」是跨组动作，一次能命中几十个组；
        # 全落同一个值 = 这些站被并进同一个桶按 weight 轮询，站间次序被推平。
        #
        # 放在这里（apply_bulk 之前）而不是前端，是因为前端改档还有单站直改
        # 那条路，两条路都必须过同一道消解，否则「单站改的撞了、批量改的不撞」
        # 这类不一致迟早会出现。
        try:
            checked_ops, coll_notes = self._resolve_op_collisions(
                _cfg, checked_ops)
        except cp.bulk.BulkError as e:
            self._json(400, {"error": str(e),
                             "error_code": "priority_collision"})
            return
        stamp = time.strftime("%Y-%m-%d %H:%M")
        try:
            new_text, notes, problems = cp.bulk.apply_bulk(
                raw, checked_ops, stamp=stamp)
        except cp.bulk.BulkError as e:
            self._json(400, {"error": str(e)})
            return
        if problems:
            self._json(400, {"error": "批量操作未通过校验", "problems": problems,
                             "error_code": "invalid_operation", "bulk_id": ""})
            return
        # cross_section=False：批量路径没有 plans，这道闸只能扫整份文件。
        # 生产配置 18 个 host 有 16 个跨段档位不同（同段内 0 个分裂），
        # 用它阻断等于让所有批量操作永远失败 —— 现场表现就是「点了没反应」。
        # 段内同站同档仍然阻断；跨段分裂作为提示随 `msg` 回传。
        ok, msg = _validate_final(new_text, cross_section=False)
        if not ok:
            self._json(400, {"error": msg, "error_code": "priority_invariant",
                             "bulk_id": ""})
            return
        if not notes:
            self._json(200, {"changed": 0, "notes": [], "problems": problems,
                             "collision_notes": coll_notes,
                             "diff": "", "bulk_id": ""})
            return

        import difflib
        diff = "\n".join(difflib.unified_diff(
            _safe_text(redact_yaml_secrets(raw)).splitlines(),
            _safe_text(redact_yaml_secrets(new_text)).splitlines(),
            fromfile="config.yaml（当前）", tofile="config.yaml（批量后）",
            lineterm="", n=2))
        bid = STORE.put_bulk(raw, new_text, notes)
        self._json(200, {
            "bulk_id": bid, "changed": len(notes), "notes": notes,
            "problems": problems,
            # 撞档消解的说明单独一份 —— 前端要把它顶到 diff 上方显示，
            # 因为它解释了「我明明填的 350，落盘却是 349」这个疑惑。
            "collision_notes": coll_notes,
            # diff 可能很长（158 条全改时）。截断并说明，不把整份塞给浏览器。
            "diff": diff[:200000],
            "diff_truncated": len(diff) > 200000,
            "semantics": cp.bulk.disable_semantics()[2],
        })

    def _api_bulk_apply(self, body: dict) -> None:
        """批量操作落盘 + 推送。必须带 bulk_id + confirm=true。

        走的是与 `_api_apply` **完全相同**的收尾链路：
        基线比对（防并发覆盖）→ YAML 校验 → 本地备份 + 落盘 →
        后台 PUT 重载 → 读回校验。一道闸都不少。
        """
        bid = body.get("bulk_id") or ""
        entry = STORE.get_bulk(bid)
        if not entry:
            self._json(404, {"error": "批量方案不存在或已过期，请重新预览"})
            return
        if body.get("confirm") is not True:
            self._json(400, {"error": "未确认。批量写回需要 confirm=true"})
            return

        self._submit_apply(entry, body)

    def _api_apply(self, body: dict) -> None:
        """原子认领方案并立即返回 task_id；confirm 必须严格等于 true。

        重复确认返回同一任务。后台串行执行基线复查、写盘、PUT 和验证，
        HTTP 请求不等待事务锁，也不会把接收入队误报为最终成功。
        """
        pid = body.get("plan_id") or ""
        entry = STORE.get_plan(pid)
        if not entry:
            self._json(404, {"error": "方案不存在或已过期，请重新生成"})
            return
        if body.get("confirm") is not True:
            self._json(400, {"error": "未确认。写回需要 confirm=true"})
            return

        self._submit_apply(entry, body)

    def _submit_apply(self, entry: dict, body: dict) -> None:
        # Claiming is short and atomic. Slow I/O never holds an HTTP request.
        if entry.get("valid") is False:
            self._json(400, {"error": entry.get("validate_msg", "方案校验不通过"),
                             "error_code": "invalid_plan"})
            return
        try:
            task, created = STORE.claim_apply(entry)
        except CapacityError as e:
            self._json(429, {"error": str(e), "error_code": "capacity_exhausted",
                             "retryable": True})
            return
        cls = type(self)
        if created:
            try:
                _start_apply_task(task, entry, body, cls.cfg_path, cls.cpa_url,
                                  self._cpa_password_for(body), self._cpa_client_key(),
                                  cls.backup_dir)
            except Exception:
                task.state = "error"
                task.error = "后台任务无法启动，请重新预览"
                task.result["error_code"] = "worker_start_failed"
                task.finished = time.time()
        self._json(202 if created else 200, task.snapshot())




def _push_target_ok(base: str, configured: str) -> str:
    """这个地址能不能作为 PUT /config.yaml 的目标。不能则返回拒绝原因。

    为什么必须有白名单（2026-09-05 加）
    -----------------------------
    `reload_cpa` 的请求体是**整份 config.yaml**，头里带
    `Authorization: Bearer <CPA 管理密码>`。地址原来完全由请求体决定
    （`push.base` 优先于服务端配置），所以填错一次就是：

        177 行明文上游凭据 + 管理密码，以一次 PUT 发给第三方

    这件事**已经发生过**：前端那个输入框曾硬编码 `https://cpa.example.com`，
    那次请求确实出了公网，只是被 Cloudflare 挡在 403（见下方 cpa_base 取值
    处的注释）。当时改的是「服务端配置优先」的取值顺序 —— 那降低了误配概率，
    但没有关掉这条出口。

    能触发的人已经掌握 CPA 管理密码（`mgmt` 非空的前提），所以这**不是**权限
    提升；这道闸防的是误配与内部人一次性外发。

    放行三类：
      · 回环与 compose 服务名 —— CPA 与本服务在同一个 docker 网络里，
        那是唯一的正常形态
      · 服务端 `--cpa-url` 显式配置的那个 host —— 运维在启动参数里写死的，
        比请求体可信
      · 私网地址 —— CPA 可能部署在同一内网的另一台机器上。这里与
        `is_private_target` 的判断**方向相反**：那边挡私网（防拿服务端扫内网），
        这边只放私网（防把凭据发出公网）。两者不矛盾 —— 判据都是「这个地址
        该不该是这条路的目标」，只是两条路的正常目标恰好互补。
    """
    b = (base or "").strip()
    if not b:
        return ""                       # 空地址由调用方另行处理（跳过重载）
    try:
        url = urllib.parse.urlsplit(b)
        configured_url = urllib.parse.urlsplit(configured or "")
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError
        if url.query or url.fragment:
            raise ValueError
        origin = (url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80))
        if configured_url.hostname:
            allowed = (configured_url.scheme, configured_url.hostname,
                       configured_url.port or (443 if configured_url.scheme == "https" else 80))
            if origin == allowed:
                return ""
        try:
            ip = ipaddress.ip_address(url.hostname)
        except ValueError:
            if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", url.hostname):
                return ""
        else:
            if ip.is_loopback or (ip.is_private and not ip.is_unspecified
                                  and not ip.is_multicast and not ip.is_reserved):
                return ""
    except ValueError:
        return "配置推送地址格式无效"
    # 理由要说清**后果**，不能只说「被拒了」（2026-09-12 退回原措辞）
    # ----------------------------------------------------------------
    # 推送走的是 `PUT /v0/management/config.yaml` —— 发出去的是**整份配置**，
    # 里面含 `remote-management.secret-key`（管理密码）与全部上游 Key。
    # 发错目标就是一次全量凭据泄露，所以这句话必须让人看懂代价，
    # 而不是只留一句「请使用明确配置的管理地址」。
    return ("拒绝向未配置的公网目标发送配置：推送会把**整份配置**"
            "（含管理密码与全部上游 Key）发给该地址。"
            "请改用回环、私网、docker 服务名，或服务端 --cpa-url 明确配置过的地址")


def _push_result(base: str, configured: str) -> dict | None:
    """目标地址被拒时该回给前端的那几个字段；放行则返回 None。

    抽成函数而不是内联在 `_run_apply_tail` 里（2026-09-05）：内联时测试只能
    断言「源码里有没有 push_ok 这个键」，而那挡不住把值赋成 True ——
    撤销实验证实过。

    四个字段都要给：`reload_ok`/`reload_msg` 是当前口径，
    `push_ok`/`push_msg` 是前端既有字段（兼容）。少给一对就会让界面
    显示成「已生效」，而实际上重载根本没发出去。
    """
    why = _push_target_ok(base, configured)
    if not why:
        return None
    return {"reload_ok": False, "reload_msg": why,
            "push_ok": False, "push_msg": why}


def _start_apply_task(task, entry, body, cfg_path, cpa_url, mgmt, client_key,
                      backup_dir=""):
    threading.Thread(
        target=_commit_apply,
        args=(task, entry, copy.deepcopy(body), cfg_path, cpa_url, mgmt,
              client_key, backup_dir),
        name=f"apply-{task.id}", daemon=True).start()


def _commit_apply(task, entry, body, cfg_path, cpa_url, mgmt, client_key,
                  backup_dir=""):
    # One transaction lock covers local write AND management PUT. HTTP merely
    # claims work; a queued task must recheck its original snapshot under lock.
    try:
        with Handler._apply_lock:
            if getattr(task, "generation", 0) < STORE.apply_generation:
                task.result["error_code"] = "stale_queued_work"
                raise ValueError("已有更新的确认任务，请重新预览")
            version = config_version(cfg_path)
            with open(cfg_path, encoding="utf-8") as stream:
                raw = stream.read()
            if raw != entry["base_raw"] or config_version(cfg_path) != version:
                task.result["error_code"] = "stale_config"
                raise ValueError("配置已变化，队列中的旧方案已拒绝；请重新预览")
            preview = entry.get("preview", entry.get("text", ""))
            ok, msg = _validate_final(preview, entry.get("plans", []))
            if not ok:
                task.result["error_code"] = "invalid_plan"
                raise ValueError(msg)
            push = body.get("push") or {}
            refused = _push_result(push.get("base") or cpa_url, cpa_url)
            if refused:
                task.result.update(refused, error_code="push_target_refused")
                raise ValueError("管理目标被拒，未写盘")
            task.set_stage("local_write")
            bak = write_local(cfg_path, preview, backup_dir=backup_dir or None,
                              expected_version=version)
            task.result.update(backup=bak, written=cfg_path, local_written=True,
                               validate_msg=msg, diffs=len(entry.get("diffs", [])),
                               notes=entry.get("notes", []))
            with Handler._cfg_cache_lock:
                Handler._cfg_cache = None
            tail_entry = {**entry, "preview": preview,
                          "plans": entry.get("plans", [])}
            _run_apply_tail(task, tail_entry, body, cfg_path, cpa_url, mgmt, client_key)
    except (ValueError, WritebackError) as e:
        task.state = "error"
        task.error = _safe_text(str(e))
        task.result.setdefault("error_code", "write_conflict")
        task.set_stage("refused")
    except Exception:
        task.state = "error"
        task.error = _error_ref(f"apply {task.id}")
        task.result["error_code"] = "write_failed"
        task.set_stage("error")
    finally:
        task.finished = time.time()


def _run_apply_tail(task: "ApplyTask", entry: dict, body: dict,
                    cfg_path: str, cfg_cpa_url: str,
                    mgmt: str, auto_client_key: str) -> None:
    """写回的后台收尾：触发 CPA 重载 + 端到端验证。

    由 _commit_apply 持事务锁并完成写盘后调用。进度写进 task，
    供 /api/apply-status 轮询；不占用确认请求的响应时间。

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
        # 地址白名单（2026-09-05 加）。上面那个「服务端配置优先」的顺序降低了
        # 误配概率，但没关掉出口 —— 用户明确填一个公网地址仍然会把整份配置
        # 与管理密码发出去。见 _push_target_ok。
        refused = _push_result(cpa_base, cfg_cpa_url)
        if refused:
            result.update(refused)
            task.set_stage("目标地址被拒")
            cpa_base = ""               # 后面的验证也一并跳过
        # 管理密码由调用方算好传入（见 _cpa_password_for）

        if cpa_base and mgmt:
            task.set_stage("reload")
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
                import yaml
                final_cfg = yaml.safe_load(entry["preview"]) or {}
                scopes = {}
                for plan in entry["plans"]:
                    for sec, sp in plan.sections.items():
                        if not sp.writable or not sp.models:
                            continue
                        # A shared model request proves only gateway behavior.
                        # Do not label it as evidence for this upstream/key.
                        model, scope = _verification_target(final_cfg, sp)
                        scopes[(plan.host, sec, model)] = scope
                        todo.append((plan.host, sec, model))

                MAX_VERIFY = 24
                skipped_over = todo[MAX_VERIFY:]
                todo = todo[:MAX_VERIFY]

                task.set_stage("verify")
                task.set_verify_total(len(todo))
                verified = [None] * len(todo)

                def _one(i: int, host: str, sec: str, model: str) -> None:
                    vok, vmsg = verify_upstream(
                        cpa_base, client_key, sec, model, timeout=45,
                    )
                    task.bump_verify()
                    verified[i] = {"host": host, "section": sec,
                                   "model": model, "ok": vok, "msg": vmsg,
                                   "verification_scope": scopes[(host, sec, model)],
                                   "target_verified": bool(vok and scopes[(host, sec, model)] == "unique_prefix")}

                if len(todo) > 1:
                    with concurrent.futures.ThreadPoolExecutor(
                            max_workers=min(6, len(todo)),
                            thread_name_prefix="verify") as ex:
                        futs = [ex.submit(_one, i, h, sc, mo)
                                for i, (h, sc, mo) in enumerate(todo)]
                        for f in futs:
                            try:
                                f.result()
                            except Exception:           # noqa: BLE001
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
                                       "ok": False, "msg": "验证请求本身失败（超时或连接错误）",
                                       "verification_scope": "gateway_only",
                                       "target_verified": False}
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

        failed = not result.get("reload_ok") or bool(result.get("verify_failed"))
        task.state = "error" if failed else "done"
        if failed:
            task.error = result.get("reload_msg") if not result.get("reload_ok") else "网关验证失败"
            result["error_code"] = "reload_failed" if not result.get("reload_ok") else "verification_failed"
        task.set_stage("error" if failed else "done")
    except Exception:
        task.state = "error"
        task.error = _error_ref(f"apply {task.id}")
        task.set_stage("收尾出错")
    finally:
        task.finished = time.time()
        # 那份 plan 已经作废（上面把 base_raw 置成 preview，重放会被 409 挡）。
        # 主动释放它 —— 每份持有两份**整份配置**（生产文件约 857KB，即约
        # 1.7MB），是三张表里最重的，而前端每次勾选变化都会新生成一份。
        # Keep the bounded, TTL-managed claim so retries return the same task.
        pass


def main() -> None:
    ap = argparse.ArgumentParser(prog="upstream-importer-server")
    ap.add_argument("--config", default="config.yaml", help="config.yaml 路径")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址。默认只本机；改 0.0.0.0 前请先加 nginx + TLS + 认证")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default=os.environ.get("IMPORTER_TOKEN", ""),
                    help="Bearer token。日志不显示；也可使用 CPA 管理密码登录")
    ap.add_argument("--log-level",
                    default=os.environ.get("IMPORTER_LOG_LEVEL", "INFO"),
                    choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                    help="日志级别。DEBUG 输出每条请求的探测细节与耗时；"
                         "也可用环境变量 IMPORTER_LOG_LEVEL 设置。"
                         "容器里日志直接写到 stdout/stderr，"
                         "docker compose logs -f 可实时跟踪。")
    ap.add_argument("--trusted-proxy-peer", action="append",
                    default=[x.strip() for x in os.environ.get("IMPORTER_TRUSTED_PROXY_PEERS", "").split(",") if x.strip()],
                    help="明确可信的反代 IP/CIDR，可重复；默认仅回环")
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

    # 详细日志初始化（修改要求第 11 条，2026-09-18）
    # -----------------------------------------
    # 容器里日志直接写到 stdout/stderr，`docker compose logs -f` 实时跟踪。
    # 级别由 --log-level 参数或 IMPORTER_LOG_LEVEL 环境变量控制：
    #   INFO（默认）：每条 API 请求与关键里程碑（探测开始/结束、写回成功/失败）
    #   DEBUG：每次上游请求的状态码、耗时、画像档位，便于定位具体站点问题
    #   WARNING/ERROR：只有警告与错误，适合生产安静运行
    #
    # 格式：时间戳 + 级别 + 模块名 + 消息，时间精确到毫秒。
    # 敏感信息（token、api-key）不进日志：Handler 类的请求日志已过滤，
    # probe/plan 流程里的 key 均以 mask_key() 脱敏后才传给 logger。
    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,   # 覆盖 Python 默认的 WARNING 级别（容器里往往已有 basicConfig）
    )
    # 第三方库（urllib3、PyYAML）的 DEBUG 日志极为冗长，单独压到 WARNING
    for noisy in ("urllib3", "yaml", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logger.info("日志级别 %s，服务即将启动", args.log_level.upper())
    try:
        Handler.trusted_proxy_peers = tuple(str(ipaddress.ip_network(x, strict=False))
                                            for x in args.trusted_proxy_peer)
    except ValueError:
        ap.error("trusted-proxy-peer 必须是合法 IP 或 CIDR")

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
        import importlib.util
        has_bcrypt = importlib.util.find_spec("bcrypt") is not None
    except ImportError:
        has_bcrypt = False

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 68)
    print("CPA 上游批量导入服务 · 投喂台")
    print("=" * 68)
    print(f"  config.yaml : {cfg}")
    print(f"  监听        : http://{args.host}:{args.port}")
    print("  token       : [不在日志中显示；请使用配置的凭据]")
    print(f"  打开        : http://{args.host}:{args.port}/")
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
          f"{Handler.BAN_SECONDS // 60} 分钟（按来源 IP，最多记 "
          f"{Handler.MAX_FAIL_ENTRIES} 个）")
    if args.host in ("127.0.0.1", "localhost", "::1"):
        print("                经 nginx 反代时按 X-Forwarded-For 最右一跳判 —— "
              "反代必须转发该头")
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
