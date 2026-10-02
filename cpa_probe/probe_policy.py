"""Fail-closed authorization for upstream attempts; this module never uses the network."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qsl, urlsplit
import uuid


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
    "provider_stopped": "服务商组已因限制响应永久停止；不会自动解除。",
    "provider_budget": "服务商组请求额度已耗尽，未进行实测。",
    "credential_budget": "当前凭据请求额度已耗尽，未进行实测。",
    "minimum_interval": "尚未达到服务商组最小请求间隔，跳过本次请求。",
    "inflight": "服务商组存在未完成请求，跳过本次请求。",
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
    def __init__(self, policy: dict, ledger_path: str):
        self._rules, self._config_code = _rules(policy)
        self._ledger_path = os.fspath(ledger_path) if ledger_path else ""
        self._lock = threading.RLock()
        self._permits = {}
        self._pending_stops = {}
        self._ledger_identity = None

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
            return db, secret
        except Exception:
            db.close()
            raise

    def _assess(self, db, host, rule, credential, now):
        provider = rule["provider"]
        binding = db.execute("SELECT provider FROM sites WHERE host=?", (_digest("host", host),)).fetchone()
        if binding is not None and binding["provider"] != provider:
            return "provider_changed", None
        state = db.execute("SELECT * FROM providers WHERE id=?", (provider,)).fetchone()
        if state is None:
            return "allowed", None
        if state["stopped"]:
            return "provider_stopped", state
        if now < state["last_seen"]:
            return "clock_rollback", state
        if state["inflight"]:
            return "inflight", state
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
            if rule is None:
                return _decision("unauthorized_site"), ""
            if not rule["enabled"]:
                return _decision("disabled_site"), ""
            try:
                now = time.time()
                if not math.isfinite(now) or now < 0:
                    raise ValueError("clock")
                db, secret = self._connect()
                credential = hmac.new(secret, credential_value, hashlib.sha256).hexdigest()
                code, state = self._assess(db, host, rule, credential, now)
                if not reserve:
                    return _decision(code), ""
                if state is not None:
                    db.execute(
                        "UPDATE providers SET last_seen=MAX(last_seen, ?), "
                        "window_seconds=MAX(window_seconds, ?), min_interval_seconds=MAX(min_interval_seconds, ?) WHERE id=?",
                        (now, rule["window_seconds"], rule["min_interval_seconds"], rule["provider"]),
                    )
                if code != "allowed":
                    db.commit()
                    return _decision(code), ""
                reservation = uuid.uuid4().hex
                if state is None:
                    db.execute("INSERT INTO providers VALUES (?, '', ?, NULL, ?, ?, '')",
                               (rule["provider"], now, rule["window_seconds"], rule["min_interval_seconds"]))
                db.execute("INSERT OR IGNORE INTO sites VALUES (?, ?)", (_digest("host", host), rule["provider"]))
                db.execute("INSERT INTO reservations VALUES (?, ?, ?, ?, '')",
                           (reservation, rule["provider"], credential, now))
                db.execute("UPDATE providers SET last_reserved=?, inflight=? WHERE id=?",
                           (now, reservation, rule["provider"]))
                db.commit()
                return _decision("allowed"), reservation
            except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
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
            permit = Permit(decision.allowed, code, _REASONS[code], reservation)
            if permit.allowed:
                self._permits[reservation] = permit
            return permit

    def finish(self, permit: Permit, status: str, body: str = "", error: str = "") -> bool:
        with self._lock:
            if not isinstance(permit, Permit) or not permit.allowed or self._permits.get(permit.reservation_id) is not permit:
                return False
            status = str(status).strip()
            stop = "status_" + status if status in ("403", "429") else ""
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
                if category in {"客户端", "门禁", "反测活", "限频", "IP封", "WAF", "边缘", "时段"}:
                    stop = "provider_restriction"
            if stop:
                self._pending_stops.setdefault(permit.reservation_id, stop)
            stop = self._pending_stops.get(permit.reservation_id, "")
            outcome = stop or (status if re.fullmatch(r"[0-5][0-9]{2}", status) else "completed")
            db = None
            try:
                db, _ = self._connect()
                row = db.execute("SELECT provider FROM reservations WHERE id=? AND outcome=''",
                                 (permit.reservation_id,)).fetchone()
                if row is None:
                    return False
                now = time.time()
                if not math.isfinite(now) or now < 0:
                    return False
                result = db.execute(
                    "UPDATE providers SET stopped=CASE WHEN stopped='' THEN ? ELSE stopped END, "
                    "inflight='', last_seen=MAX(last_seen, ?) WHERE id=? AND inflight=?",
                    (stop, now, row["provider"], permit.reservation_id),
                )
                if result.rowcount != 1:
                    return False
                db.execute("UPDATE reservations SET outcome=? WHERE id=?", (outcome, permit.reservation_id))
                db.commit()
                del self._permits[permit.reservation_id]
                self._pending_stops.pop(permit.reservation_id, None)
                return True
            except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
                return False  # Keep in-flight state; report failed settlement to transport.
            finally:
                if db is not None:
                    db.close()

    def summary(self) -> dict:
        with self._lock:
            count = sum(rule["enabled"] for rule in self._rules.values())
            return {"mode": "restricted" if count else "passive", "default_passive": True,
                    "authorized_sites": count, "enabled_sites": count,
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
    backup = os.environ.get("IMPORTER_BACKUP_DIR", "")
    ledger = os.environ.get("IMPORTER_PROBE_LEDGER", "") or (os.path.join(backup, "probe-policy.sqlite3") if backup else "")
    with _cache_lock:
        try:
            with open(policy_path, encoding="utf-8") as source:
                policy = json.load(source, object_pairs_hook=_unique_object)
        except (OSError, ValueError, TypeError):
            policy = {}
        paths = (policy_path, ledger)
        if _cached_policy is None or _cached_paths != paths:
            _cached_policy = ProbePolicy(policy, ledger)
            _cached_paths = paths
        else:
            # Reload deleted/edited rules, but retain ownership of outstanding permits.
            with _cached_policy._lock:
                _cached_policy._rules, _cached_policy._config_code = _rules(policy)
        return _cached_policy


def reset_policy_cache() -> None:
    """Forget configuration only; never clear persisted usage, stops or reservations."""
    global _cached_policy, _cached_paths
    with _cache_lock:
        _cached_policy = None
        _cached_paths = None
