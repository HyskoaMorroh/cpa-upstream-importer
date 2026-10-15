"""Fail-closed authorization for upstream attempts; this module never uses the network."""
from __future__ import annotations

from dataclasses import dataclass
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
from email.utils import parsedate_to_datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qsl, urlsplit
import uuid
import weakref

logger = logging.getLogger(__name__)

_LIVE_OWNERS = weakref.WeakValueDictionary()


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str
    reason: str


@dataclass(frozen=True)
class Permit:
    allowed: bool
    code: str
    reason: str
    reservation_id: str = ""


_REASONS = {
    "allowed": "规则允许；实际请求仍须原子预留额度。",
    "reserved": "已预留一次请求额度。",
    "passive": "默认被动模式：未配置有效的显式站点授权。",
    "invalid_policy": "策略格式或规则字段无效，禁止上游请求。",
    "unauthorized_site": "该站点未获显式授权，未进行实测。",
    "disabled_site": "该站点规则未启用，未进行实测。",
    "invalid_request": "请求地址或凭据格式无效，未进行实测。",
    "ledger_unavailable": "持久账本不可用，禁止上游请求。",
    "provider_changed": "站点所属服务商组与持久账本不一致，禁止刷新额度。",
    "provider_stopped": "服务商组限制记录阻止请求；明确拒绝不会自动解除，来源不明的旧记录仅允许新检测任务有界复核。",
    "provider_job_stopped": "本轮任务已收到限制响应或已用完旧记录复核机会，同组所有凭据停止；请在冷却后新建检测任务。",
    "provider_cooldown": "服务商限制响应或旧记录复核失败后处于持久冷却期，尚未发送请求（默认 15 分钟）。",
    "provider_budget": "服务商组请求额度已耗尽，未进行实测。",
    "credential_budget": "当前凭据请求额度已耗尽，未进行实测。",
    "minimum_interval": "尚未达到服务商组最小请求间隔，暂不能发送请求。",
    "orphaned_inflight": "存在所有权无法核实且无可信结算证据的历史请求；禁止自动重试，请核对原任务和账本。",
    "inflight": "服务商组存在未完成请求，暂不能发送请求。",
    "recovery_catalog_skipped": "旧记录复核中目录不可用；跳过重复目录请求，等待同凭据推理验证。",
    "recovery_inference_required": "旧记录复核仅剩同任务同凭据的一次推理验证机会；当前请求未发送。",
    "clock_rollback": "系统时间早于账本记录，禁止刷新额度。",
}
_SCHEMA = (
    "CREATE TABLE metadata (version INTEGER NOT NULL, secret BLOB NOT NULL)",
    "CREATE TABLE providers (id TEXT PRIMARY KEY, stopped TEXT NOT NULL, "
    "last_seen REAL NOT NULL, last_reserved REAL, window_seconds REAL NOT NULL, "
    "min_interval_seconds REAL NOT NULL, inflight TEXT NOT NULL)",
    "CREATE TABLE sites (host TEXT PRIMARY KEY, provider TEXT NOT NULL)",
    "CREATE TABLE reservations (id TEXT PRIMARY KEY, provider TEXT NOT NULL, "
    "credential TEXT NOT NULL, reserved_at REAL NOT NULL, outcome TEXT NOT NULL)",
    "CREATE INDEX reservation_usage ON reservations(provider, reserved_at, credential)",
)
_RESTRICTION = re.compile(
    r"\b(?:account|api[ _-]?key|credential|ip|access)\b.{0,48}\b(?:banned|suspended|blocked)\b"
    r"|\b(?:probing|probes?|scanning|automated (?:tests?|requests?))\b.{0,48}"
    r"\b(?:prohibited|forbidden|not (?:allowed|permitted)|disabled)\b"
    r"|\b(?:do not|must not|cannot) (?:probe|scan)\b"
    r"|\b(?:account_suspended|account_banned|ip_banned|ip_blocked|probing_not_allowed|anti_probe)\b"
    r"|(?:禁止|不允许|严禁).{0,24}(?:探测|测试|扫描|压测)"
    r"|(?:封禁|封号|账号已停用|账户已停用)",
    re.IGNORECASE,
)
_CLIENT_RESTRICTION = re.compile(
    r"\bonly\s+(?:official\s+)?(?:clients?|claude\s*code|codex)\b"
    r"|\brestricted\s+to\s+(?:claude\s*code|codex|(?:official\s+)?clients?)\b"
    r"|(?:仅限|只允许|只支持|必须使用).{0,24}(?:官方客户端|Claude\s*Code|Codex)",
    re.IGNORECASE,
)
_AUTH_HEADERS = {"authorization", "x-api-key", "api-key", "x-goog-api-key"}
_AUTH_QUERY = {"key", "api_key", "api-key", "access_token"}

_JOB_SCOPE = ContextVar("probe_policy_job", default=None)
_COOLDOWN_SECONDS = 900.0

# 限制证据的分类器版本（2026-10-13 从 1 提到 2）
# ------------------------------------------------
# `restriction_evidence.classifier_version` 这个列从引入起一直写死 1、
# 从未被任何查询读过。它存在的目的就是区分「这条证据是哪一版判定逻辑
# 得出的」，而现在真的需要了：`pipeline._stage0_catalog` 补上身份头之后，
# 「分段目录 GET 回 401 unauthorized client detected」这件事的含义从
# 「站方只认特定客户端」变成了「修复前我们自己没带身份头」。
#
# 版本号的唯一用途见 `_legacy_recovery`：低于当前版本的证据不再单独构成
# 永久封锁，可以走那条已有的有界复核路径；当前版本的证据语义不变。
# 以后再出现同类语义变更时继续 +1，并在这里写清变的是什么。
CLASSIFIER_VERSION = 2
_REQUEST_KIND = ContextVar("probe_policy_request_kind", default="unknown")


@contextmanager
def policy_request_kind(kind):
    """Semantic call-site evidence, never inferred from a user-controlled URL."""
    token = _REQUEST_KIND.set(kind if kind in ("model_catalog", "inference") else "unknown")
    try:
        yield
    finally:
        _REQUEST_KIND.reset(token)


def policy_request_scope(method):
    @wraps(method)
    def scoped(self, *args, request_kind="unknown", **kwargs):
        with policy_request_kind(request_kind):
            return method(self, *args, **kwargs)
    return scoped



def policy_job_scope(method):
    """Keep one job identity across the Prober's worker threads and transports."""
    @wraps(method)
    def scoped(self, *args, **kwargs):
        token = _JOB_SCOPE.set(self.session_id)
        try:
            return method(self, *args, **kwargs)
        finally:
            _JOB_SCOPE.reset(token)
    return scoped


def _cooldown_until(value, now):
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds > 0:
            return now + max(1.0, seconds)
    except (ValueError, TypeError, OverflowError):
        pass
    try:
        deadline = parsedate_to_datetime(str(value)).timestamp()
        if math.isfinite(deadline) and deadline > now:
            return deadline
    except (ValueError, TypeError, OverflowError):
        pass
    return now + _COOLDOWN_SECONDS


def _decision(code):
    return Decision(code == "allowed", code, _REASONS[code])


def _digest(kind, value):
    return hashlib.sha256((kind + "\0" + value).encode("utf-8")).hexdigest()


def _host(value):
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError("host")
    value = value.rstrip(".").lower()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    value = value.encode("idna").decode("ascii")
    if len(value) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in value.split(".")
    ):
        raise ValueError("host")
    return value


def _rules(policy):
    if not policy:
        return {}, "passive"
    try:
        if not isinstance(policy, dict) or type(policy.get("version")) is not int or policy["version"] != 1:
            raise ValueError("version")
        if not isinstance(policy.get("sites"), dict):
            raise ValueError("sites")
        rules, groups = {}, {}
        for host, entry in policy["sites"].items():
            host = _host(host)
            if host in rules or not isinstance(entry, dict):
                raise ValueError("site")
            if type(entry.get("enabled")) is not bool:
                raise ValueError("enabled")
            provider = entry.get("provider")
            if not isinstance(provider, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", provider):
                raise ValueError("provider")
            rule = {"enabled": entry["enabled"], "provider": _digest("provider", provider)}
            for field in ("max_requests", "max_requests_per_credential"):
                value = entry.get(field)
                if type(value) is not int or not 1 <= value <= 2 ** 63 - 1:
                    raise ValueError("count")
                rule[field] = value
            for field in ("window_seconds", "min_interval_seconds"):
                value = entry.get(field)
                if type(value) not in (float, int) or not 0 <= value <= 10 ** 12 or not math.isfinite(value):
                    raise ValueError("duration")
                if field == "window_seconds" and value == 0:
                    raise ValueError("window")
                rule[field] = float(value)
            limits = tuple(rule[k] for k in ("max_requests", "max_requests_per_credential",
                                            "window_seconds", "min_interval_seconds"))
            if provider in groups and groups[provider] != limits:
                raise ValueError("conflicting group limits")
            groups[provider] = limits
            rules[host] = rule
        return rules, "allowed" if any(r["enabled"] for r in rules.values()) else "passive"
    except (ValueError, TypeError, UnicodeError, OverflowError):
        return {}, "invalid_policy"


def _request(url, headers):
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError("url")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https", "ws", "wss") or parsed.username is not None or parsed.password is not None:
        raise ValueError("url")
    host = _host(parsed.hostname)
    if parsed.port is not None and parsed.port == 0:
        raise ValueError("port")
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        raise ValueError("headers")
    credentials = set()
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("headers")
        if name.lower() not in _AUTH_HEADERS:
            continue
        value = value.strip()
        if name.lower() == "authorization":
            parts = value.split(None, 1)
            if len(parts) != 2:
                raise ValueError("authorization")
            value = parts[1].strip() if parts[0].lower() == "bearer" else parts[0].lower() + ":" + parts[1].strip()
        if not value or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("credential")
        credentials.add(value)
    for name, value in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=256):
        if name.lower() in _AUTH_QUERY:
            if not value or any(c.isspace() or ord(c) < 32 for c in value):
                raise ValueError("credential")
            credentials.add(value)
    if len(credentials) > 1:
        raise ValueError("ambiguous credentials")
    credential = "credential\0" + next(iter(credentials)) if credentials else "anonymous\0"
    return host, credential.encode("utf-8")


class ProbePolicy:
    def __init__(self, policy: dict, ledger_path: str, *, managed: bool = False):
        self._rules, self._config_code = _rules(policy)
        self._managed = managed
        if managed:
            self._config_code = "allowed"
        self._ledger_path = os.fspath(ledger_path) if ledger_path else ""
        self._lock = threading.RLock()
        self._permits = {}
        self._completed_permits = weakref.WeakValueDictionary()
        self._permit_providers = {}
        self._deferred_settlements = {}
        self._pending_stops = {}
        self._permit_scopes = {}
        self._default_scope = uuid.uuid4().hex
        self._ledger_identity = None
        self._owner_epoch = uuid.uuid4().hex
        _LIVE_OWNERS[self._owner_epoch] = self

    def _connect(self):
        if not self._ledger_path or self._ledger_path == ":memory:":
            raise sqlite3.DatabaseError("persistent ledger required")
        created = False
        try:
            fd = os.open(self._ledger_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
            created = True
        db = sqlite3.connect(self._ledger_path, timeout=0.1, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            if created:
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute("INSERT INTO metadata VALUES (1, ?)", (os.urandom(32),))
                db.commit()
                db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("invalid ledger")
            rows = db.execute("SELECT version, secret FROM metadata").fetchall()
            if len(rows) != 1 or rows[0]["version"] != 1 or not isinstance(rows[0]["secret"], bytes) or len(rows[0]["secret"]) != 32:
                raise sqlite3.DatabaseError("invalid metadata")
            secret = rows[0]["secret"]
            if self._ledger_identity is not None and not hmac.compare_digest(self._ledger_identity, secret):
                raise sqlite3.DatabaseError("ledger replaced")
            self._ledger_identity = secret
            # A read-only connection must deny check as well as reserve.
            db.execute("UPDATE metadata SET version=version")
            # Additive migration: original reservations and stop reasons remain
            # intact. A bare legacy HTTP status is not proof of a permanent ban.
            db.execute("CREATE TABLE IF NOT EXISTS cooldowns (provider TEXT PRIMARY KEY, until REAL NOT NULL, reason TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS job_stops (provider TEXT NOT NULL, scope TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(provider, scope))")
            db.execute("CREATE TABLE IF NOT EXISTS restriction_evidence (reservation TEXT PRIMARY KEY, provider TEXT NOT NULL, classifier_version INTEGER NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, response_hash TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS recovery_attempts (reservation TEXT PRIMARY KEY, provider TEXT NOT NULL, scope TEXT NOT NULL, created_at REAL NOT NULL, outcome TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS recovery_provider ON recovery_attempts(provider, created_at)")
            db.execute("INSERT OR IGNORE INTO cooldowns SELECT p.id, COALESCE((SELECT MAX(r.reserved_at) FROM reservations r WHERE r.provider=p.id AND r.outcome=p.stopped), p.last_reserved, p.last_seen)+?, p.stopped FROM providers p WHERE p.stopped IN ('status_403','status_429')", (_COOLDOWN_SECONDS,))
            db.execute("CREATE TABLE IF NOT EXISTS reservation_owners (reservation TEXT PRIMARY KEY, owner_epoch TEXT NOT NULL, scope TEXT NOT NULL, phase TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS reservation_requests (reservation TEXT PRIMARY KEY, kind TEXT NOT NULL)")
            self._replay(db, secret)
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            return db, secret
        except Exception:
            db.close()
            raise


    def _is_live(self, db, reservation):
        row = db.execute("SELECT owner_epoch FROM reservation_owners WHERE reservation=?", (reservation,)).fetchone()
        owner = _LIVE_OWNERS.get(row["owner_epoch"]) if row else None
        return bool(owner and reservation in owner._permits
                    and os.path.realpath(owner._ledger_path) == os.path.realpath(self._ledger_path))

    def _settlement_store(self):
        # Adjacent to the ledger: included in the same persistent directory mount.
        path = self._ledger_path + ".settlements.sqlite3"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        side = sqlite3.connect(path, timeout=0.1, isolation_level=None)
        try:
            side.row_factory = sqlite3.Row
            side.execute("PRAGMA synchronous=FULL")
            side.execute("CREATE TABLE IF NOT EXISTS settlements (reservation TEXT PRIMARY KEY, payload TEXT NOT NULL, signature TEXT NOT NULL)")
            return side
        except Exception:
            side.close()
            raise

    def _persist_result(self, reservation, evidence):
        if self._ledger_identity is None:
            raise sqlite3.DatabaseError("missing ledger identity")
        payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        signature = hmac.new(self._ledger_identity, payload.encode(), hashlib.sha256).hexdigest()
        side = self._settlement_store()
        try:
            side.execute("INSERT OR IGNORE INTO settlements VALUES (?, ?, ?)", (reservation, payload, signature))
        finally:
            side.close()

    def _apply_result(self, db, reservation, evidence):
        strings = ("reservation", "provider", "owner_epoch", "scope", "stop", "status", "outcome", "response_hash")
        if (not isinstance(evidence, dict)
                or any(not isinstance(evidence.get(key), str) for key in strings)
                or type(evidence.get("recovered")) is not bool
                # 旧结算载荷没有这个键：缺省 False 即「维持原判」；有键必须是 bool。
                or type(evidence.get("classify_client_identity", False)) is not bool
                or any(type(evidence.get(key)) not in (int, float) for key in ("now", "cooldown_until"))
                or evidence["stop"] not in ("", "restriction", "client_restriction", "provider_restriction", "status_403", "status_429")
                or not evidence["outcome"] or evidence["reservation"] != reservation
                or not re.fullmatch(r"[0-9a-f]{64}", evidence["response_hash"])):
            raise sqlite3.DatabaseError("invalid settlement shape")
        row = db.execute("SELECT * FROM reservations WHERE id=?", (reservation,)).fetchone()
        owner = db.execute("SELECT * FROM reservation_owners WHERE reservation=?", (reservation,)).fetchone()
        if (row is None or owner is None or row["provider"] != evidence["provider"]
                or owner["owner_epoch"] != evidence["owner_epoch"] or owner["scope"] != evidence["scope"]):
            raise sqlite3.DatabaseError("settlement binding mismatch")
        if row["outcome"]:
            return
        stop, status, now = evidence["stop"], evidence["status"], evidence["now"]
        cooldown_until, outcome = evidence["cooldown_until"], evidence["outcome"]
        if not math.isfinite(now) or now < row["reserved_at"] or not math.isfinite(cooldown_until):
            raise sqlite3.DatabaseError("invalid settlement time")
        temporary = stop in ("status_403", "status_429")
        # 身份档（classify「客户端」）停止的作用域修正（2026-10-12 本机实测）
        # ----------------------------------------------------------------
        # 现场：同一主机 10 把不同 Key，第一把的**推理**请求回 401
        # 「unauthorized client detected」→ classify 归「客户端」→ finish 升级
        # 成 provider_restriction 的**全站**永久停止。于是账本里只剩 1 个凭据
        # 哈希，另外 9 把 Key 一次都没被预留（每个段 usable=false、
        # category=未实测、probe_policy_code=provider_stopped、attempts=0），
        # 界面 10 个候选 0 个可用段、0 个方案。一把 Key 的一次 401 把九把
        # 兄弟 Key 全部归零 —— 违反第 7 条「一个 KEY 不能拖垮全部」。
        #
        # 同主机反证（section_gate.py docstring，2026-10-09）：裸 x-api-key 回
        # 同一句 401，而 claude-cli UA + x-app 回 200。这句话说的是**请求形态**
        # 被拒，不是站方禁测；classify.py 自己的注释（2026-09-17）也写明处置
        # 是「换请求形态」而非「换 Key」。推理路径正是身份画像梯
        # （profiles.py；pipeline._stage1 → _try_profiles）逐档找形态的地方，
        # 全站永久停止把梯子在第一档掐死 —— 它永远拿不到后续档位，与它存在
        # 的目的直接矛盾。
        #
        # 修法：仅当这条预留**预留时**的请求类型是 inference，把该停止降级成
        # 预留级记录 —— outcome 仍如实写 provider_restriction（审计与预算消耗
        # 保留，_replay 按 outcome 判幂等不受影响），但不写 restriction_evidence、
        # 不写 providers.stopped。其余凭据与同一凭据的后续画像档位照常可预留；
        # 画像梯全败时由流水线按段/按 Key 记 error，整桶全挂再由上层降级到
        # 下一个域名 —— 这才是第 7 条要的形状。不加按凭据停止表：这次失败
        # 连凭据作用域都不是，按凭据停会把同一个 Key 的后续档位也掐掉。
        #
        # 作用域判定用 reservation_requests 里**持久化**的 kind，不用结算时刻
        # 的上下文：延迟结算（_attempt 的 _deferred_settlements 重放、重启后的
        # _replay）可能发生在别的请求类型之下，按上下文判会把目录请求的刻意
        # 停止误免、或把推理请求的身份档误停。老账本没有这一行、或旧结算载荷
        # 没有 classify_client_identity 标记时一律维持原判 —— 不确定就 fail-closed。
        #
        # 刻意**不**改的部分：目录请求（test_catalog_restriction_scope 的契约：
        # 显式拒绝赢过凭据作用域，哪怕正文带 unauthorized client detected）；
        # kind=unknown 的调用点（WS 握手、写回验证）；_RESTRICTION /
        # _CLIENT_RESTRICTION 命中的真·全站拒绝（account suspended、
        # only official clients…）以及 status_403/429 —— 这些仍按原语义停止。
        if stop == "provider_restriction" and evidence.get("classify_client_identity") is True:
            kind_row = db.execute("SELECT kind FROM reservation_requests WHERE reservation=?",
                                  (reservation,)).fetchone()
            if kind_row is not None and kind_row["kind"] == "inference":
                stop = ""
        if stop and not temporary:
            # Store provenance, never response bodies or credentials. New
            # evidenced restrictions supersede any old recovery overlay.
            # 版本号随 CLASSIFIER_VERSION 走，不再写死 1 —— `_legacy_recovery`
            # 按它区分「语义已变的旧结论」与「本轮分类器的结论」。
            db.execute("INSERT OR IGNORE INTO restriction_evidence VALUES (?, ?, ?, ?, ?, ?)",
                       (reservation, row["provider"], CLASSIFIER_VERSION,
                        status if status.isdigit() else "unknown", stop,
                        evidence['response_hash']))
        recovery = db.execute("SELECT 1 FROM recovery_attempts WHERE reservation=?", (reservation,)).fetchone()
        if recovery:
            recovered = evidence['recovered']
            request = db.execute("SELECT kind FROM reservation_requests WHERE reservation=?", (reservation,)).fetchone()
            catalog_missing = bool(request and request["kind"] == "model_catalog"
                                   and status == "404" and not stop
                                   and evidence.get("catalog_missing") is True)
            db.execute("UPDATE recovery_attempts SET outcome=? WHERE reservation=?",
                       ("recovered" if recovered else "catalog_unavailable" if catalog_missing else "unconfirmed", reservation))
            if not recovered and not catalog_missing:
                db.execute("INSERT OR IGNORE INTO job_stops VALUES (?, ?, 'legacy_recovery_unconfirmed')", (row["provider"], evidence['scope']))
                db.execute("INSERT INTO cooldowns VALUES (?, ?, 'legacy_recovery_unconfirmed') ON CONFLICT(provider) DO UPDATE SET until=MAX(until, excluded.until)", (row["provider"], now + _COOLDOWN_SECONDS))
        if temporary:
            db.execute("INSERT INTO cooldowns VALUES (?, ?, ?) ON CONFLICT(provider) DO UPDATE SET until=MAX(until, excluded.until), reason=excluded.reason", (row["provider"], cooldown_until, stop))
            db.execute("INSERT OR IGNORE INTO job_stops VALUES (?, ?, ?)", (row["provider"], evidence['scope'], stop))
        result = db.execute(
            "UPDATE providers SET stopped=CASE WHEN stopped='' OR stopped IN ('status_403','status_429') THEN ? ELSE stopped END, "
            "inflight='', last_seen=MAX(last_seen, ?) WHERE id=? AND inflight=?",
            ("" if temporary else stop, now, row["provider"], reservation),
        )
        if result.rowcount != 1:
            raise sqlite3.DatabaseError("reservation ownership conflict")
        db.execute("UPDATE reservations SET outcome=? WHERE id=?", (outcome, reservation))

        db.execute("UPDATE reservation_owners SET phase='settled' WHERE reservation=?", (reservation,))

    def _replay(self, db, secret):
        side = self._settlement_store()
        try:
            pending = db.execute("SELECT id FROM reservations WHERE outcome=''").fetchall()
            for pending_row in pending:
                reservation = pending_row["id"]
                result = side.execute("SELECT payload, signature FROM settlements WHERE reservation=?", (reservation,)).fetchone()
                if result is None:
                    continue
                signature = hmac.new(secret, result["payload"].encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, result["signature"]):
                    raise sqlite3.DatabaseError("invalid settlement signature")
                evidence = json.loads(result["payload"])
                if not isinstance(evidence, dict) or evidence.get("reservation") != reservation:
                    raise sqlite3.DatabaseError("settlement identity mismatch")
                self._apply_result(db, reservation, evidence)
        finally:
            side.close()

    def _legacy_recovery(self, db, state):
        """Overlay only ambiguous old classifiers; keep the original event intact.

        作用域从「没有 evidence」扩到「只有旧版分类器的 evidence」
        （2026-10-13，CLASSIFIER_VERSION 1 -> 2）
        -------------------------------------------------------------
        原来只救**完全没有** restriction_evidence 的旧停止；带 evidence 的
        一律永久封死。那在分类器语义不变时是对的 —— 证据就是证据。

        但 `pipeline._stage0_catalog` 的修复改变了「同一条 401 意味着什么」。
        修复前分段目录 GET 不带身份头（`models_endpoint` 只放凭据），站方回
        `401 unauthorized client detected`，classify 归「客户端」，于是整个
        服务商被永久停止。本机实测（同一把 Key、同一 URL、直连与过代理一致）：

            裸 Bearer / 裸 x-api-key / Bearer+curl UA      -> 401
            User-Agent: claude-cli/1.0.0 (external, cli)   -> 200 + 真实目录

        也就是说那条 401 记录的是**我们自己发错了请求形态**，不是站方拒绝。
        修复后同样的目录请求会带身份头，同一个站回 200。旧证据的前提已经
        不成立，而它仍在封死整站：实测 `restriction_evidence` 里 2 条
        （401 + 403）让 2 个 provider 永久 `provider_stopped`，新镜像起来后
        `prescan-miss status=blocked probe_policy_code=provider_stopped`，
        一个请求都发不出去 —— 光改代码救不回已经被判死的站。

        所以 `classifier_version` 终于派上用场（它从引入起一直写 1、从未被
        读过）：版本 < CLASSIFIER_VERSION 的证据视为「语义已变的旧结论」，
        允许走与无证据旧停止**完全相同**的那条有界复核路径（显式任务作用域
        + 同凭据 + 一次目录 404 后最多一次真实推理，见 `_assess`）。

        刻意**不**做的事：
          · 不删除、不改写任何旧证据行（`test_new_classifier_does_not_erase_
            existing_evidence` 的契约）—— 只是不再让它单独构成永久封锁；
          · 不放宽当前版本的证据。本轮分类器写下的 evidence 仍是永久停止，
            `test_catalog_restriction_scope` 与 `test_credential_scope_
            isolation` 的断言都用现跑引擎写入，不受影响；
          · 不自动放行。复核仍要显式任务作用域、仍受预算与冷却约束，
            失败即回到停止并记 `legacy_recovery_unconfirmed`。
        """
        if state is None or state["stopped"] != "provider_restriction":
            return False
        if db.execute("SELECT 1 FROM recovery_attempts WHERE provider=? AND outcome='recovered'",
                      (state["id"],)).fetchone():
            return False
        current = db.execute(
            "SELECT 1 FROM restriction_evidence WHERE provider=? AND classifier_version>=?",
            (state["id"], CLASSIFIER_VERSION)).fetchone()
        return not current

    def _assess(self, db, host, rule, credential, now, reserve=False):
        provider = rule["provider"]
        binding = db.execute("SELECT provider FROM sites WHERE host=?", (_digest("host", host),)).fetchone()
        if binding is not None and binding["provider"] != provider:
            return "provider_changed", None
        state = db.execute("SELECT * FROM providers WHERE id=?", (provider,)).fetchone()
        if state is None:
            return "allowed", None
        if state["stopped"] and state["stopped"] not in ("status_403", "status_429"):
            # 只有**当前版本**的证据才在这里直接判死。旧版分类器的证据交给
            # 下面的 `_legacy_recovery` 走有界复核 —— 否则这道闸会先返回
            # provider_stopped，复核路径永远到不了（2026-10-13，见
            # CLASSIFIER_VERSION 与 `_legacy_recovery` 的说明）。
            evidenced = db.execute(
                "SELECT 1 FROM restriction_evidence WHERE provider=? AND classifier_version>=?",
                (provider, CLASSIFIER_VERSION)).fetchone()
            recovered = db.execute("SELECT 1 FROM recovery_attempts WHERE provider=? AND outcome='recovered'", (provider,)).fetchone()
            if state["stopped"] != "provider_restriction" or evidenced or (not recovered and not _JOB_SCOPE.get()):
                return "provider_stopped", state
        if now < state["last_seen"]:
            return "clock_rollback", state
        scope = _JOB_SCOPE.get() or self._default_scope
        if db.execute("SELECT 1 FROM job_stops WHERE provider=? AND scope=?", (provider, scope)).fetchone():
            return "provider_job_stopped", state
        cooldown = db.execute("SELECT until FROM cooldowns WHERE provider=?", (provider,)).fetchone()
        if cooldown is not None and now < cooldown["until"]:
            return "provider_cooldown", state
        if state["inflight"]:
            return ("inflight" if self._is_live(db, state["inflight"]) else "orphaned_inflight"), state
        if self._legacy_recovery(db, state):
            attempts = db.execute(
                "SELECT a.outcome, r.credential FROM recovery_attempts a "
                "JOIN reservations r ON r.id=a.reservation WHERE a.provider=? AND a.scope=?",
                (provider, scope)).fetchall()
            if attempts:
                # Exactly one ordinary directory 404 may be followed by exactly
                # one real inference, with the original credential and job.
                if len(attempts) != 1 or attempts[0]["outcome"] != "catalog_unavailable":
                    return "provider_job_stopped", state
                if attempts[0]["credential"] != credential:
                    return "recovery_inference_required", state
                kind = _REQUEST_KIND.get()
                if kind == "model_catalog":
                    return "recovery_catalog_skipped", state
                if kind != "inference" and reserve:
                    return "recovery_inference_required", state
            else:
                latest = db.execute("SELECT MAX(created_at) FROM recovery_attempts WHERE provider=?", (provider,)).fetchone()[0]
                if now < max(latest or 0, state["last_reserved"] or 0) + _COOLDOWN_SECONDS:
                    return "provider_cooldown", state
        window = max(rule["window_seconds"], state["window_seconds"])
        counts = db.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(credential=?), 0) AS credential_total "
            "FROM reservations WHERE provider=? AND reserved_at>?",
            (credential, provider, now - window),
        ).fetchone()
        if counts["total"] >= rule["max_requests"]:
            return "provider_budget", state
        if counts["credential_total"] >= rule["max_requests_per_credential"]:
            return "credential_budget", state
        interval = max(rule["min_interval_seconds"], state["min_interval_seconds"])
        if state["last_reserved"] is not None and now - state["last_reserved"] < interval:
            return "minimum_interval", state
        return "allowed", state

    def admission_key(self, url, headers):
        """Share admission across policy instances and aliases of a provider."""
        try:
            host, _credential = _request(url, headers)
        except (ValueError, TypeError, UnicodeError):
            host = "invalid-request"
        with self._lock:
            rule = self._rules.get(host)
            provider = rule["provider"] if rule else _digest("provider", host)
        return (os.path.normcase(os.path.realpath(self._ledger_path)), provider)

    def _explain_decision(self, db, code, provider, now):
        """Describe a denial from ledger facts without exposing stored diagnostics.

        This is presentation only: it neither clears a stop nor extends cooldown.
        A completed recovery is not proof of successful inference or no wire call.
        """
        decision = _decision(code)
        if code not in ("provider_job_stopped", "provider_cooldown"):
            return decision
        reason = decision.reason
        if code == "provider_job_stopped":
            scope = _JOB_SCOPE.get() or self._default_scope
            stopped = db.execute(
                "SELECT reason FROM job_stops WHERE provider=? AND scope=?",
                (provider, scope),
            ).fetchone()
            safe_reasons = {
                "legacy_recovery_unconfirmed": "旧限制记录复核未确认成功，本轮同组后续探测已停止；受影响项目不代表分别发生了请求失败。",
                "status_403": "本轮同组请求收到 HTTP 403 限制响应，后续探测已停止。",
                "status_429": "本轮同组请求收到 HTTP 429 限制响应，后续探测已停止。",
            }
            if stopped:
                reason = safe_reasons.get(stopped["reason"], reason)
            reason += "原任务不会自动恢复；新任务仍须满足全部策略条件。"
        cooldown = db.execute("SELECT until FROM cooldowns WHERE provider=?", (provider,)).fetchone()
        if cooldown and math.isfinite(cooldown["until"]) and cooldown["until"] > now:
            remaining = math.ceil(cooldown["until"] - now)
            reason += f"账本冷却在本次判定时还剩 {remaining} 秒；冷却结束不保证允许请求。"
        return Decision(False, code, reason)

    def _attempt(self, url, headers, reserve):
        db = None
        with self._lock:
            if self._config_code != "allowed":
                return _decision(self._config_code), ""
            try:
                host, credential_value = _request(url, headers)
            except (ValueError, TypeError, UnicodeError):
                return _decision("invalid_request"), ""
            rule = self._rules.get(host)
            if rule is None and self._managed:
                # 只在实际请求到达时登记该目标；不会扫描配置或启动后台探测。
                # 同站不同凭据和代理共享预算与在途锁，不靠换 Key 规避限额。
                rule = {"enabled": True, "provider": _digest("provider", host),
                        "max_requests": 600, "max_requests_per_credential": 120,
                        "window_seconds": 3600.0, "min_interval_seconds": 1.0}
                self._rules[host] = rule
            if rule is None:
                return _decision("unauthorized_site"), ""
            if not rule["enabled"]:
                return _decision("disabled_site"), ""
            # A completed wire request may still own its reservation after a
            # transient ledger failure. Replay authenticated first-result evidence
            # without another wire request; never expire an unresolved request.
            for reservation, args in tuple(self._deferred_settlements.items()):
                if self._permit_providers.get(reservation) == rule["provider"]:
                    permit, status, body, error, retry_after = args
                    if not self.finish(permit, status, body, error, retry_after=retry_after):
                        return _decision("ledger_unavailable"), ""
            try:
                now = time.time()
                if not math.isfinite(now) or now < 0:
                    raise ValueError("clock")
                db, secret = self._connect()
                credential = hmac.new(secret, credential_value, hashlib.sha256).hexdigest()
                code, state = self._assess(db, host, rule, credential, now, reserve)
                decision = self._explain_decision(db, code, rule["provider"], now)
                if not reserve:
                    return decision, ""
                if state is not None:
                    db.execute(
                        "UPDATE providers SET last_seen=MAX(last_seen, ?), "
                        "window_seconds=MAX(window_seconds, ?), min_interval_seconds=MAX(min_interval_seconds, ?) WHERE id=?",
                        (now, rule["window_seconds"], rule["min_interval_seconds"], rule["provider"]),
                    )
                if code != "allowed":
                    db.commit()
                    return decision, ""
                reservation = uuid.uuid4().hex
                if state is None:
                    db.execute("INSERT INTO providers VALUES (?, '', ?, NULL, ?, ?, '')",
                               (rule["provider"], now, rule["window_seconds"], rule["min_interval_seconds"]))
                db.execute("INSERT OR IGNORE INTO sites VALUES (?, ?)", (_digest("host", host), rule["provider"]))
                db.execute("INSERT INTO reservations VALUES (?, ?, ?, ?, '')",
                           (reservation, rule["provider"], credential, now))
                db.execute("INSERT INTO reservation_owners VALUES (?, ?, ?, 'reserved')",
                           (reservation, self._owner_epoch, _JOB_SCOPE.get() or self._default_scope))
                db.execute("INSERT INTO reservation_requests VALUES (?, ?)", (reservation, _REQUEST_KIND.get()))
                if self._legacy_recovery(db, state):
                    db.execute("INSERT INTO recovery_attempts VALUES (?, ?, ?, ?, 'pending')",
                               (reservation, rule["provider"], _JOB_SCOPE.get(), now))
                db.execute("UPDATE providers SET last_reserved=?, inflight=? WHERE id=?",
                           (now, reservation, rule["provider"]))
                db.commit()
                return _decision("allowed"), reservation
            except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError) as exc:
                # 要求第 10 条：这是全系统**最要紧**的 fail-closed 判定，
                # 原来整个吞掉异常、一行日志都不记。
                #
                # 后果（2026-10-12 本机实测）：账本打不开时每个段都判
                # `ledger_unavailable` → 界面每个段显示「未实测」、定档 0 个
                # 方案、所有参数空白，而日志里**没有任何**线索说明为什么。
                # 现场只能看到「什么信息都没有」，无从下手 —— 这正是
                # tests/test_deploy_runtime_contracts.py 记的 2026-10-04 生产
                # 事故形态（账本无法创建 → 每个探测被拒）。
                #
                # 记异常类型与消息即可定位（权限、磁盘满、库被锁、路径不存在
                # 各有不同的 sqlite/OSError 文案）；不记凭据、不记请求体 ——
                # 异常对象本身不含它们，`exc_info` 也只带本模块的栈。
                logger.error("持久账本不可用，已拒绝本次上游请求：%s: %s"
                             " —— 账本路径见 IMPORTER_PROBE_LEDGER；"
                             "常见原因是目录不可写、磁盘满、或库被另一个进程锁住",
                             type(exc).__name__, exc, exc_info=True)
                return _decision("ledger_unavailable"), ""
            finally:
                if db is not None:
                    db.close()  # Also rolls back check's local writability test.

    def check(self, url: str, headers: dict | None = None) -> Decision:
        return self._attempt(url, headers, False)[0]

    def reserve(self, url: str, headers: dict | None = None) -> Permit:
        with self._lock:
            decision, reservation = self._attempt(url, headers, True)
            code = "reserved" if decision.allowed else decision.code
            permit = Permit(decision.allowed, code,
                            _REASONS[code] if decision.allowed else decision.reason, reservation)
            if permit.allowed:
                self._permits[reservation] = permit
                host, _ = _request(url, headers)
                self._permit_providers[reservation] = self._rules[host]["provider"]
                self._permit_scopes[reservation] = _JOB_SCOPE.get() or self._default_scope
            return permit

    def finish(self, permit: Permit, status: str, body: str = "", error: str = "", *, retry_after: str = "") -> bool:
        with self._lock:
            if not isinstance(permit, Permit) or not permit.allowed:
                return False
            if self._completed_permits.get(permit.reservation_id) is permit:
                return True
            if self._permits.get(permit.reservation_id) is not permit:
                return False
            # Retain the first result in memory and authenticated durable evidence.
            # A later caller cannot replace restriction evidence with a clean result.
            original = self._deferred_settlements.setdefault(
                permit.reservation_id, (permit, status, body, error, retry_after))
            permit, status, body, error, retry_after = original
            status = str(status).strip()
            stop = ""
            # 「客户端」类单独打标记（2026-10-12）：它与反测活/IP封/WAF 一样会在
            # 下面升级成 provider_restriction，但只有它的**作用域**需要按请求
            # 类型修正（推理请求上它是身份档被拒，不是全站拒绝）。判定放在
            # _apply_result —— 那里才有预留时持久化的 request kind；这里只把
            # 分类事实随结算载荷一起签名保存，重放与延迟结算都拿同一份事实。
            client_identity = False
            if not stop and any(isinstance(text, str) and _RESTRICTION.search(text) for text in (body, error)):
                stop = "restriction"
            # Client-only and WAF restrictions may arrive as 400/401/503, not
            # just 403. Do not respond by trying another fingerprint or proxy.
            error_response = status.isdigit() and int(status) >= 400
            if not error_response and isinstance(body, str):
                try:
                    payload = json.loads(body)
                    error_response = isinstance(payload, dict) and (
                        bool(payload.get("error")) or payload.get("success") is False)
                except (ValueError, TypeError):
                    pass
            if not stop and error_response and isinstance(body, str) and _CLIENT_RESTRICTION.search(body):
                stop = "client_restriction"
            if not stop and error_response:
                from .classify import classify
                category, _ = classify(status, body)
                if category in {"客户端", "反测活", "IP封", "WAF"}:
                    stop = "provider_restriction"
                    # 见 _apply_result 的作用域修正：推理请求上的「客户端」
                    # 身份档拒绝不构成全站结论，其余三类维持全站停止。
                    client_identity = category == "客户端"
            if not stop and status in ("403", "429"):
                stop = "status_" + status
            if stop:
                self._pending_stops.setdefault(permit.reservation_id, (stop, _cooldown_until(retry_after, time.time())))
            stop, cooldown_until = self._pending_stops.get(permit.reservation_id, ("", 0.0))
            outcome = stop or (status if re.fullmatch(r"[0-5][0-9]{2}", status) else "completed")
            db = None
            try:
                now = time.time()
                if not math.isfinite(now) or now < 0:
                    return False
                self._persist_result(permit.reservation_id, {
                    "reservation": permit.reservation_id, "owner_epoch": self._owner_epoch,
                    "provider": self._permit_providers[permit.reservation_id],
                    "scope": self._permit_scopes[permit.reservation_id],
                    "status": status if status.isdigit() else "unknown",
                    "stop": stop, "cooldown_until": cooldown_until, "outcome": outcome,
                    "now": now, "recovered": bool(not stop and not error and not error_response and status.isdigit() and 200 <= int(status) < 300),
                    "catalog_missing": bool(status == "404" and not stop and not error),
                    # 分类事实随载荷签名：_apply_result 据此（加上预留时持久化的
                    # request kind）决定「客户端」类停止是否只记到预留级。
                    "classify_client_identity": client_identity,
                    "response_hash": _digest("restriction", str(body) + "\0" + str(error)),
                })
                # _connect atomically replays authenticated results, including ours.
                db, _ = self._connect()
                row = db.execute("SELECT outcome FROM reservations WHERE id=?", (permit.reservation_id,)).fetchone()
                if row is None or not row["outcome"]:
                    return False
                db.commit()
                self._completed_permits[permit.reservation_id] = permit
                del self._permits[permit.reservation_id]
                self._permit_providers.pop(permit.reservation_id, None)
                self._deferred_settlements.pop(permit.reservation_id, None)
                self._pending_stops.pop(permit.reservation_id, None)
                self._permit_scopes.pop(permit.reservation_id, None)
                return True
            except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
                return False  # Keep in-flight state; report failed settlement to transport.
            finally:
                if db is not None:
                    db.close()

    def orphaned_reservations(self) -> list[dict]:
        """列出无法核实归属的在途预留（只读，不改任何状态）。

        为什么必须能列出来（2026-10-13 本机实测）
        --------------------------------------
        现象：探测 `calls=0`，日志里只有
        `probe_policy_code=orphaned_inflight`，界面提示「N 个未结算请求的
        持有者无法确认」。实测账本里 9 条预留卡在 `phase=reserved`，占死
        9 个 provider 的 `inflight`；这些 provider 的 `stopped` 是**空**
        （没被判死），但一个请求都发不出去。

        成因链（已逐跳查实）：容器重启 → 任务只在内存（server.py 的
        `JobStore.jobs` 是普通 dict）→ 在途预留无人结算 → 结算凭证库里
        这 9 条**一条都没有**（请求发出后、签名结算前进程就被杀）→
        `_replay` 无从恢复 → `_is_live` 靠进程内存字典 `_LIVE_OWNERS`
        判活，重启后必然为空 → 永久 `orphaned_inflight`。

        而部署方式是 `pull_policy: always`，每次 `docker compose up -d`
        都重建容器。正跑着探测时拉新镜像，账本就此半残 —— 这是「拉了新
        镜像反而更多站不通」的一个确切机制。

        为什么只列出、不自动放行
        ----------------------
        `test_policy_restart_recovery` 的
        `test_no_evidence_retired_owner_remains_unresolved_without_ttl`
        刻意钉死「没有 TTL 自愈」：时钟推到 10**12 仍然是
        `orphaned_inflight`。那个 fail-closed 是对的 —— 请求可能真的发
        出去并计了费，自动当作没发生会掩盖真实用量，也会让同一次请求被
        重复发送。所以这里只提供**事实**，由操作员核对后显式核销
        （见 `settle_orphan`）。
        """
        out: list[dict] = []
        with self._lock:
            db = None
            try:
                db, _ = self._connect()
                rows = db.execute(
                    "SELECT p.id AS provider, p.inflight AS reservation, "
                    "       p.last_reserved, r.credential, r.reserved_at, q.kind "
                    "FROM providers p "
                    "LEFT JOIN reservations r ON r.id = p.inflight "
                    "LEFT JOIN reservation_requests q ON q.reservation = p.inflight "
                    "WHERE p.inflight <> ''").fetchall()
                for row in rows:
                    if self._is_live(db, row["reservation"]):
                        continue
                    side = self._settlement_store()
                    try:
                        has_payload = bool(side.execute(
                            "SELECT 1 FROM settlements WHERE reservation=?",
                            (row["reservation"],)).fetchone())
                    finally:
                        side.close()
                    out.append({
                        # 只给前 12 位：足够人工比对日志，又不把完整内部 id
                        # 散播到界面与导出文件里。
                        "reservation": row["reservation"][:12],
                        "provider": row["provider"][:12],
                        "reserved_at": row["reserved_at"],
                        "request_kind": row["kind"] or "unknown",
                        # 有凭证的本该被 `_replay` 自动恢复；仍留在这里说明
                        # 凭证校验失败（签名不符 / 账本被换），那是另一类
                        # 问题，必须和「凭证缺失」区分开。
                        "has_settlement_payload": has_payload,
                    })
            except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
                return []
            finally:
                if db is not None:
                    db.close()
        return out

    def settle_orphan(self, reservation_prefix: str, *, outcome: str,
                      operator_note: str) -> tuple[bool, str]:
        """按操作员核对结果核销一条孤儿预留。返回 (是否成功, 说明)。

        这不是「清理」或「解锁」—— 它要求调用方**先去核对原请求到底有没有
        执行**，再把结论如实写进账本。两种合法结论：

          · `outcome="executed"`  —— 已核实请求确实发出（上游用量/日志里
            有记录）。按已执行记账：消耗预算、留审计，不产生限制证据。
          · `outcome="not-executed"` —— 已核实请求未曾到达上游。同样记账，
            但额外写一条 `job_stops` 留痕，避免「核对一次就等于永久免检」。

        两者都**不会**解除任何既有的 `provider_restriction` 或冷却；它们
        只释放被占死的 `inflight` 位。不提供「跳过核对」的第三种取值 ——
        那等于把 fail-closed 拆掉，而那道闸正是防止重复计费的。

        `operator_note` 必填且会落进 `job_stops.reason`：谁核的、依据什么，
        以后查账时要能追溯。
        """
        if outcome not in ("executed", "not-executed"):
            return False, "outcome 必须是 executed 或 not-executed"
        note = (operator_note or "").strip()
        if len(note) < 4:
            return False, "必须填写核对依据（至少 4 个字符）"
        if any(c in note for c in "\r\n\x00"):
            return False, "核对依据不能含换行或空字符"
        prefix = (reservation_prefix or "").strip()
        if not re.fullmatch(r"[0-9a-f]{8,64}", prefix):
            return False, "预留标识格式不对"
        with self._lock:
            db = None
            try:
                db, _ = self._connect()
                rows = db.execute(
                    "SELECT id, provider FROM reservations "
                    "WHERE outcome='' AND id LIKE ?", (prefix + "%",)).fetchall()
                if not rows:
                    return False, "找不到该未结算预留（可能已被结算）"
                if len(rows) > 1:
                    return False, "标识前缀匹配到多条，请提供更长的前缀"
                reservation, provider = rows[0]["id"], rows[0]["provider"]
                if self._is_live(db, reservation):
                    return False, "该预留仍由本进程持有，不能按孤儿核销"
                scope = _JOB_SCOPE.get() or self._default_scope
                # 如实记录人工核对的结论，不伪装成一次正常结算：outcome 用
                # 专门的取值，审计时一眼能分出「引擎自己结算的」与「人工
                # 核对后记账的」。
                db.execute("UPDATE reservations SET outcome=? WHERE id=?",
                           ("operator-" + outcome, reservation))
                db.execute("UPDATE reservation_owners SET phase='settled' "
                           "WHERE reservation=?", (reservation,))
                db.execute("UPDATE providers SET inflight='' "
                           "WHERE id=? AND inflight=?", (provider, reservation))
                if outcome == "not-executed":
                    db.execute(
                        "INSERT OR IGNORE INTO job_stops VALUES (?, ?, ?)",
                        (provider, scope,
                         "operator_cleared_unexecuted:" + note[:120]))
                db.commit()
                return True, ("已按「%s」记账并释放在途位；既有限制与冷却不变"
                              % outcome)
            except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError) as exc:
                logger.error("孤儿预留核销失败：%s", type(exc).__name__)
                return False, "账本写入失败，状态未改变"
            finally:
                if db is not None:
                    db.close()

    def summary(self) -> dict:
        with self._lock:
            count = sum(rule["enabled"] for rule in self._rules.values())
            # 账本不可用时授权数必须报 0（2026-10-04 本机实测）。
            # ------------------------------------------------------
            # 镜像把 /backups chown 给 APP_UID 并留 0755，而部署 compose 以
            # uid 0 运行且 cap_drop: [ALL] —— root 没有 CAP_DAC_OVERRIDE，
            # 建不了 probe-policy.sqlite3。本模块按 fail-closed 拒掉每一次
            # 上游请求（ledger_unavailable），但这里照旧报
            # 「已配置 N 个站点的主动探测许可」，前端就显示成一切正常，
            # 而八个账号四段全是「未实测」、零请求。界面必须与实际一致。
            blocked = ""
            inflight = {"inflight_count": 0, "orphaned_inflight_count": 0, "reservation_recovery_status": "clear"}
            if count or self._managed:
                db = None
                try:
                    db, _ = self._connect()
                    for row in db.execute("SELECT inflight FROM providers WHERE inflight<>''").fetchall():
                        field = "inflight_count" if self._is_live(db, row["inflight"]) else "orphaned_inflight_count"
                        inflight[field] += 1
                    inflight["reservation_recovery_status"] = ("unresolved" if inflight["orphaned_inflight_count"]
                                                              else "active" if inflight["inflight_count"] else "clear")
                except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
                    blocked = "ledger_unavailable"
                finally:
                    if db is not None:
                        db.close()
            if blocked:
                return {**inflight, "mode": "passive", "default_passive": True,
                        "authorized_sites": 0, "enabled_sites": 0,
                        "configured_sites": count, "ledger_blocked": True,
                        "reason": _REASONS[blocked] + "；已配置的站点授权当前一律无法生效，"
                                  "请检查备份目录与账本路径的写权限。"}
            if self._managed:
                return {**inflight, "mode": "managed", "default_passive": False,
                        "authorized_sites": count, "enabled_sites": count,
                        "configured_sites": 0, "ledger_blocked": False,
                        "limits": {"max_requests": 600, "max_requests_per_credential": 120,
                                   "window_seconds": 3600, "min_interval_seconds": 1},
                        "reason": "主动发起检测时实际请求目标站点；同站串行、共享预算和请求间隔，"
                                  "遇到明确禁测或封禁停止对应服务商，不自动绕过限制。"}
            return {**inflight, "mode": "restricted" if count else "passive", "default_passive": True,
                    "authorized_sites": count, "enabled_sites": count,
                    "configured_sites": count, "ledger_blocked": False,
                    "reason": "仅显式授权站点可按持久预算预留请求；不代表保证安全。" if count else _REASONS[self._config_code]}


_cache_lock = threading.Lock()
_cached_policy = None
_cached_paths = None


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate policy field")
        result[name] = value
    return result


def get_policy() -> ProbePolicy:
    global _cached_policy, _cached_paths
    policy_path = os.environ.get("IMPORTER_PROBE_POLICY_FILE", "")
    mode = os.environ.get("IMPORTER_PROBE_MODE", "").strip().lower() or "managed"
    backup = os.environ.get("IMPORTER_BACKUP_DIR", "")
    ledger = os.environ.get("IMPORTER_PROBE_LEDGER", "") or (os.path.join(backup, "probe-policy.sqlite3") if backup else "")
    if not ledger:
        # CLI 没有 compose 的 /backups 时也使用稳定的持久目录，不能每次
        # 换临时账本刷新预算。显式配置了错误路径则报错，不另换账本。
        directory = os.path.join(os.path.expanduser("~"), ".cache", "upstream-importer")
        ledger = os.path.join(directory, "probe-policy.sqlite3")
        if mode != "passive":
            try:
                os.makedirs(directory, mode=0o700, exist_ok=True)
            except OSError:
                pass  # summary/check 报 ledger_unavailable，不放开请求。
    with _cache_lock:
        managed = False
        explicit = False
        if mode == "passive":
            policy = {}
        elif mode != "managed":
            policy = {"version": 0}  # 不认识的模式不能静默放行。
        else:
            try:
                with open(policy_path, encoding="utf-8") as source:
                    policy = json.load(source, object_pairs_hook=_unique_object)
                explicit = True
            except FileNotFoundError:
                # 策略文件是高级覆盖项，不是升级后的新增启动前提。
                # 已加载过的显式规则消失时不放宽权限，保留其恢复入口。
                known = (_cached_paths == (policy_path, ledger) and
                         getattr(_cached_policy, "_explicit_policy", False))
                policy, managed = ({"version": 0}, False) if known else ({}, True)
            except (OSError, ValueError, TypeError):
                policy, explicit = {"version": 0}, True
        paths = (policy_path, ledger)
        if _cached_policy is None or _cached_paths != paths:
            _cached_policy = ProbePolicy(policy, ledger, managed=managed)
            _cached_paths = paths
        else:
            # 重载配置不丢在途许可和持久预算；没有外部覆盖时保留已登记站点。
            with _cached_policy._lock:
                if not managed:
                    _cached_policy._rules, _cached_policy._config_code = _rules(policy)
                elif not _cached_policy._managed:
                    _cached_policy._rules = {}
                _cached_policy._managed = managed
                if managed:
                    _cached_policy._config_code = "allowed"
        _cached_policy._explicit_policy = explicit or getattr(_cached_policy, "_explicit_policy", False)
        return _cached_policy


def reset_policy_cache() -> None:
    """Forget configuration only; never clear persisted usage, stops or reservations."""
    global _cached_policy, _cached_paths
    with _cache_lock:
        _cached_policy = None
        _cached_paths = None
