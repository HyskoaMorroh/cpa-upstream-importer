"""运行环境探测 —— 给并发数一个有依据的默认值。

为什么不能用 os.cpu_count()
--------------------------
容器里它返回**宿主机**的核数，不是分配给容器的配额。一台 64 核宿主上跑
`--cpus=4` 的容器，cpu_count() 仍然是 64 —— 按它算出的并发数会超配 16 倍。

正确的来源是 cgroup。两个版本的路径与格式都不同：
  cgroup v2   /sys/fs/cgroup/cpu.max          "400000 100000" 或 "max 100000"
              /sys/fs/cgroup/memory.max       字节数 或 "max"
  cgroup v1   /sys/fs/cgroup/cpu/cpu.cfs_quota_us   400000（-1 = 无限制）
              /sys/fs/cgroup/cpu/cpu.cfs_period_us  100000
              /sys/fs/cgroup/memory/memory.limit_in_bytes

读不到就回落到 os.cpu_count()，并在返回值里标明来源 —— 前端要显示依据，
「30 并发」这个数字不能凭空出现。

为什么并发数不能只看 CPU
----------------------
本工具是 I/O 密集型：一次探测的耗时几乎全在等上游响应（1-2 秒），CPU 占用
极低。所以并发上限的真实约束是三条，取最小：

  1. 站方限流 —— 最硬的一条，超了会被拉黑。节流按 (站, 段) 分桶已经保证
     同一端点的间隔，但站级并发太高仍会让同一批站集中受压
  2. 内存 —— 每个线程栈约 8MB 虚拟、实际驻留约 1MB；加上响应缓冲（读取
     上限 4MB/次），保守按每并发 12MB 估
  3. CPU —— 只在 JSON 解析与正则匹配时占用，一个核撑得住十几个并发

所以推荐值是「按内存算出的上限」与「按核数算出的上限」取小，再压到一个
经验安全上界内。
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field


# 每个并发探测线程的内存预算（MB）。依据：线程栈实际驻留约 1MB +
# 响应读取上限 4MB（client.py 的硬上限）+ JSON 解析峰值约 2 倍正文 +
# 余量。取 12 是保守值，宁可少开几个也不要 OOM —— 这个服务被 OOM kill
# 时可能正在写 config.yaml。
MB_PER_WORKER = 12

# 每核能撑的并发数。I/O 密集型任务下这个值可以很高（线程大多在
# socket.recv() 上阻塞、已释放 GIL），但不是无限：JSON 解析与正则是真
# CPU 活。取 12 是实测 4 核跑 40 并发时 CPU 未饱和的经验值。
WORKERS_PER_CORE = 12

# 经验安全上界。再高时瓶颈已经完全在站方限流上，多开只是空转线程 +
# 增加被判定为异常流量的风险。
HARD_CAP = 64

# 下界：至少 4，否则 175 个站要跑太久。
FLOOR = 4


@dataclass
class Resources:
    """探测到的运行环境。source 字段说明数字从哪来，供前端显示依据。"""

    cpus: float                     # 可用核数（可能是小数，如 --cpus=1.5）
    cpu_source: str
    memory_mb: int                  # 可用内存 MB；0 = 读不到
    memory_source: str
    in_container: bool
    recommended_workers: int
    reason: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "cpus": self.cpus,
            "cpu_source": self.cpu_source,
            "memory_mb": self.memory_mb,
            "memory_source": self.memory_source,
            "in_container": self.in_container,
            "recommended_workers": self.recommended_workers,
            "reason": self.reason,
            "notes": self.notes,
        }


def _read(path: str) -> str:
    try:
        with io.open(path, encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _in_container() -> bool:
    """是否在容器里。三个证据任一成立即算。"""
    if os.path.exists("/.dockerenv"):
        return True
    # cgroup v1 的挂载路径里带 docker/kubepods
    cg = _read("/proc/self/cgroup")
    if re.search(r"docker|kubepods|containerd|lxc", cg):
        return True
    # cgroup v2 下 /proc/self/cgroup 只有 "0::/"，改看这个
    return os.path.exists("/sys/fs/cgroup/cpu.max")


def detect_cpus() -> tuple[float, str]:
    """可用核数。优先 cgroup 配额，其次 CPU 亲和性，最后 cpu_count()。"""
    # cgroup v2
    v2 = _read("/sys/fs/cgroup/cpu.max")
    if v2:
        parts = v2.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota, period = int(parts[0]), int(parts[1])
                if quota > 0 and period > 0:
                    return round(quota / period, 2), "cgroup v2 cpu.max"
            except ValueError:
                pass

    # cgroup v1
    q = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    p = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if q and p:
        try:
            quota, period = int(q), int(p)
            if quota > 0 and period > 0:
                return round(quota / period, 2), "cgroup v1 cpu.cfs_quota_us"
        except ValueError:
            pass

    # 没有配额限制时用 CPU 亲和性（比 cpu_count 准：taskset/cpuset 会反映在这里）
    try:
        n = len(os.sched_getaffinity(0))       # 仅 Linux
        if n > 0:
            return float(n), "sched_getaffinity"
    except AttributeError:
        pass

    return float(os.cpu_count() or 1), "os.cpu_count"


def detect_memory_mb() -> tuple[int, str]:
    """可用内存 MB。优先 cgroup 限额，其次 /proc/meminfo 的 MemAvailable。"""
    # cgroup v2。注意 memory.max 是**硬上限**，容器实际能用的还要看
    # 宿主剩余；取两者较小者更稳，但读不到宿主时以限额为准。
    #
    # 必须挡住 <= 0 与超大值（2026-09-01 审计发现）：v1 那条路有 `0 < n` 的
    # 判断，v2 这条原来只判了 != "max"。于是 memory.max = "-1"（某些运行时
    # 表达「无限制」的方式）会被当成真实限额，算出 memory_mb = -1 —— 而
    # detect() 里的 `mem > 0` 判断会让 reason 里不带内存那一项，
    # 显示出来的依据与 memory_source 标的来源自相矛盾。
    v2 = _read("/sys/fs/cgroup/memory.max")
    if v2 and v2 != "max":
        try:
            n = int(v2)
            if 0 < n < 1024 ** 5:          # 上界同 v1：大于 1 PB 视为无限制
                return n // (1024 * 1024), "cgroup v2 memory.max"
        except ValueError:
            pass

    # cgroup v1。无限制时这个值是一个极大数（约 2^63），要挡掉。
    v1 = _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if v1:
        try:
            n = int(v1)
            # 大于 1 PB 视为「无限制」的哨兵值
            if 0 < n < 1024 ** 5:
                return n // (1024 * 1024), "cgroup v1 memory.limit_in_bytes"
        except ValueError:
            pass

    # 裸机：用 MemAvailable 而不是 MemTotal —— 前者才是现在真能拿到的
    mi = _read("/proc/meminfo")
    if mi:
        m = re.search(r"^MemAvailable:\s+(\d+)\s+kB", mi, re.M)
        if m:
            return int(m.group(1)) // 1024, "/proc/meminfo MemAvailable"
        m = re.search(r"^MemTotal:\s+(\d+)\s+kB", mi, re.M)
        if m:
            return int(m.group(1)) // 1024, "/proc/meminfo MemTotal"

    return 0, "读不到"


def detect(*, floor: int = FLOOR, cap: int = HARD_CAP) -> Resources:
    """探测环境并给出推荐并发数。

    推荐值 = min(内存能撑的, 核数能撑的, cap)，再抬到 floor 以上。
    每一步都写进 reason，前端要能显示「为什么是这个数」。
    """
    cpus, cpu_src = detect_cpus()
    mem, mem_src = detect_memory_mb()
    container = _in_container()
    notes: list[str] = []

    by_cpu = int(cpus * WORKERS_PER_CORE)

    if mem > 0:
        # 只拿一半内存做并发预算 —— 另一半留给 config.yaml 解析
        # （857KB 的文件解析峰值可达几十 MB）与响应缓冲。
        by_mem = int(mem * 0.5 / MB_PER_WORKER)
    else:
        by_mem = by_cpu
        notes.append("读不到内存限额，并发数只按核数估算")

    rec = max(floor, min(by_cpu, by_mem, cap))

    bits = [f"{cpus} 核 × {WORKERS_PER_CORE} = {by_cpu}"]
    if mem > 0:
        bits.append(f"{mem}MB 的一半 ÷ {MB_PER_WORKER}MB = {by_mem}")
    binding = "内存" if by_mem < by_cpu else "核数"
    if rec == cap:
        binding = f"安全上界 {cap}"
    elif rec == floor and min(by_cpu, by_mem) < floor:
        binding = f"下界 {floor}"
    reason = " · ".join(bits) + f" → 取 {rec}（受限于{binding}）"

    if not container:
        notes.append("未检测到容器环境，数字来自宿主机")
    if cpu_src == "os.cpu_count" and container:
        notes.append("在容器里但读不到 cgroup 配额，核数可能是宿主机的，"
                     "推荐值偏高，请手工确认")

    notes.append("并发数的真实瓶颈是站方限流，不是本机资源；"
                 "被限流时应降低这个值而不是提高")

    return Resources(
        cpus=cpus, cpu_source=cpu_src,
        memory_mb=mem, memory_source=mem_src,
        in_container=container,
        recommended_workers=rec,
        reason=reason,
        notes=notes,
    )


if __name__ == "__main__":
    r = detect()
    print(f"容器内       : {r.in_container}")
    print(f"可用核数     : {r.cpus}  （{r.cpu_source}）")
    print(f"可用内存     : {r.memory_mb} MB  （{r.memory_source}）")
    print(f"推荐并发     : {r.recommended_workers}")
    print(f"依据         : {r.reason}")
    for n in r.notes:
        print(f"  · {n}")
