#!/usr/bin/env python3
"""mihomo 出口健康检查 + 自动降级（Python 版，总期限 30 秒）。

这个脚本**两个项目共用**（cpa2sub2api 与 cpa-upstream-importer），
`cpa2sub2api` 的 README 第一行就把「可切换的代理出口」写成核心用途
（用于绕开按 IP 拦截的上游站点），所以降级能力不能去掉。
配套的 shell 版见同目录 healthcheck.sh —— 两份服务的容器不同：

  · healthcheck.sh —— 给 mihomo 容器用（metacubex/mihomo 是 busybox 基座：
    有 wget/nc/sed/awk，但没有 curl、也没有 python3）
  · healthcheck.py —— 给**本镜像**用（python:3.12-slim 派生，有 python3
    无 curl），也可给其它 Python 环境用

为什么不能只判活、不降级
----------------------
docker 的 healthcheck 只能把容器标成 unhealthy 或触发重启，**改不了 mihomo
的选路**。机场节点全挂时进程本身活得好好的（端口在听、API 有响应），重启也
救不回来，而 config.yaml 里配了代理的上游都指向本容器，于是请求走进一个没有
可用出口的代理里排队直到超时 —— 表面现象是上游 502/524，真因在这里。

退出码：0 = 健康（含「已降级到 DIRECT 且直连可用」），1 = 连直连都不通。
降级本身不算失败：报 unhealthy 会让 depends_on 的服务连不上，反而放大故障。

2026-10-13 并回说明
------------------
本文件一度被改写成「只读、不改选路」的 66 行版本，同时 README 声称
MIHOMO_STATE_FILE / MIHOMO_GROUP「不再使用」。那与共用契约矛盾（共享
compose 只有一个 mihomo 容器、执行同一份脚本）。现按两项目共用的基线恢复
降级，并保留只读版引入的三项真改进：
  · 子进程 + 总期限 30 秒，覆盖 DNS 阻塞与持续缓慢响应
  · 经代理探测时清空 no_proxy / NO_PROXY，避免绕过规则让直连成功掩盖代理故障
  · 严格 2xx 判定 + 禁止重定向，重定向到门户页不再算「链路通」
顺带修掉基线里两处弱点：切换的组名改读 MIHOMO_GROUP（原来写死 PROXY），
以及 probe_* 原本接受 <400 和 <500。

环境变量：
  MIHOMO_API        默认 http://127.0.0.1:9090
  MIHOMO_PROXY      默认 http://127.0.0.1:7890
  MIHOMO_SECRET     API 鉴权密钥，留空则不带 Authorization
  MIHOMO_PROBE_URL  默认 https://www.gstatic.com/generate_204
  MIHOMO_STATE_FILE 默认 /tmp/mihomo-egress-state
  MIHOMO_GROUP      默认 PROXY（要切换的组名）
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

API = os.environ.get("MIHOMO_API", "http://127.0.0.1:9090").rstrip("/")
PROXY = os.environ.get("MIHOMO_PROXY", "http://127.0.0.1:7890")
SECRET = os.environ.get("MIHOMO_SECRET", "")
PROBE_URL = os.environ.get("MIHOMO_PROBE_URL",
                           "https://www.gstatic.com/generate_204")
STATE = os.environ.get("MIHOMO_STATE_FILE", "/tmp/mihomo-egress-state")
GROUP = os.environ.get("MIHOMO_GROUP", "PROXY")
TOTAL_TIMEOUT = 30


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止跟随重定向。

    探测端点正常回 204。被劫持到门户页时是 30x + 200，跟随之后看起来像
    「链路通」—— 那正是要挡的误判。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def log(msg):
    print("[healthcheck] %s" % msg, flush=True)


def check(url, *, proxy=None, secret="", timeout=12):
    """只有 2xx 算通；显式指定的代理不会被 NO_PROXY 绕过。

    这是只读版引入的判定函数，测试（tests/test_mihomo_healthcheck_readonly.py）
    直接按这个签名调用，保留不动。
    """
    try:
        request = urllib.request.Request(url, method="GET")
        if secret:
            request.add_header("Authorization", "Bearer " + secret)
        if proxy:
            parsed = urlsplit(proxy)
            if parsed.scheme != "http" or not parsed.netloc:
                return False
            request.set_proxy(parsed.netloc, "http")
        # ProxyHandler({}) 清掉环境里的代理设置：显式 set_proxy 之后再让
        # 环境变量插手，会出现「以为走代理、其实直连」或访问本机 API 自环。
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as error:
        error.close()
        return False
    except Exception:
        return False


def api_call(path, method="GET", payload=None, timeout=5):
    """调 mihomo 的 RESTful API。返回解析后的 JSON，失败返回 None。

    与 `check` 分开：这里要读响应体（节点清单）并支持 PUT，而 `check`
    只回布尔。两者都用 ProxyHandler({}) —— 访问本机 API 绝不能走代理。
    """
    url = API + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if SECRET:
        headers["Authorization"] = "Bearer " + SECRET
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            if not body:
                return {}
            try:
                return json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                return {}
    except Exception:
        return None


def probe_via_proxy(timeout=12):
    """经代理实际出一次网。与 CPA 使用的路径完全一致。"""
    return check(PROBE_URL, proxy=PROXY, timeout=timeout)


def probe_direct(timeout=12):
    """不经代理直连。决定容器降级后还有没有价值。"""
    return check(PROBE_URL, timeout=timeout)


def read_state():
    try:
        with open(STATE, "r") as f:
            return f.read().strip() or "AUTO"
    except Exception:
        return "AUTO"


def write_state(v):
    try:
        with open(STATE, "w") as f:
            f.write(v)
    except Exception:
        # 状态文件写不进去只影响"恢复后切回"的判断，不该让健康检查失败
        pass


def switch(target):
    """把 MIHOMO_GROUP 指定的组切到 target。成功返回 True。"""
    return api_call("/proxies/" + GROUP, method="PUT",
                    payload={"name": target}) is not None


def run_checks():
    """判活 + 按需降级。返回进程退出码。

    拆成独立函数是为了让测试能直接调（只读版引入，保留）：`main` 负责
    子进程与总期限，这里只做判定本身。
    """
    # ---- 1. API 是否响应。不响应说明进程真的坏了，让 docker 重启它 ----
    if api_call("/version") is None:
        log("API 无响应（%s），判定进程异常" % API)
        return 1

    # ---- 2. AUTO 组里有多少可用节点 ----
    auto = api_call("/proxies/AUTO") or {}
    nodes = auto.get("all") or []
    node_count = len(nodes)

    # ---- 3. 真正试一次出网。节点数 > 0 不等于能用 ----
    egress_ok = probe_via_proxy()
    prev = read_state()

    if egress_ok and node_count > 0:
        # 出网正常。如果上一轮降级过，现在切回 AUTO。
        if prev == "DIRECT":
            if switch("AUTO"):
                log("出口已恢复（AUTO 组 %d 个节点），切回 AUTO" % node_count)
                write_state("AUTO")
            else:
                log("! 恢复切回 AUTO 失败，仍停在 DIRECT")
        return 0

    # ---- 4. 代理出不去：降级 DIRECT ----
    log("代理出口不可用（AUTO 节点 %d 个，探测 %s 失败），降级 DIRECT"
        % (node_count, PROBE_URL))
    if switch("DIRECT"):
        write_state("DIRECT")
    else:
        log("! 切换 DIRECT 失败（%s 组可能不是 select 类型，或 secret 不对）"
            % GROUP)

    if probe_direct():
        log("直连可用，容器保持健康（流量已走 DIRECT）")
        return 0

    log("直连也不通，判定不健康")
    return 1


def main():
    # 独立子进程给整轮检查设硬上限：单步的 timeout 挡不住 DNS 解析阻塞与
    # 持续缓慢的响应，而步数不定（判活 + 读组 + 探代理 + 切换 + 探直连）
    # 串起来可能超过 compose 的 healthcheck timeout。超时后连带回收子进程。
    try:
        return subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--bounded-child"],
            timeout=TOTAL_TIMEOUT, check=False,
        ).returncode
    except subprocess.TimeoutExpired:
        log("总期限 %d 秒已到，终止本轮检查" % TOTAL_TIMEOUT)
        return 1


if __name__ == "__main__":
    sys.exit(run_checks() if sys.argv[1:] == ["--bounded-child"] else main())
