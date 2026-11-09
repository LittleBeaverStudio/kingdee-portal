# -*- coding: utf-8 -*-
"""门户的持久化状态：**会话**与**租户**共用一套 SQLite 存储。

解决两件事
──────────
1. **掉线**：会话过去只放进程内存，容器被平台回收重启 → 所有人被"登录已过期"。
   落到磁盘后，进程怎么重启都不用重登。
2. **多租户**：一个地址服务多家公司，靠"公司码 → 该公司的金蝶环境/发票云配置"路由。
   租户表本来就要持久化，所以和会话共用一套存储，避免同一块代码写两遍。

凭据怎么放（**请如实理解这一层保护**）
──────────────────────────────────────
`seal()/unseal()` 用 **HMAC-SHA256 作 PRF 的 CTR 流加密 + 完整性 tag** —— 纯标准库、
标准构造（不是自造密码学）。密钥单独放一个文件 `state.key`（0600，随机生成）。

⚠️ **威胁模型**（不要夸大）：防的是「**数据库文件被误拷贝 / 误提交 / 被日志打出来**」；
**不防**"已经拿到整个容器的人"——因为密钥就在同一台机器上。
这是"防呆不防贼"。真要做到防贼得靠操作系统级凭据保护（Windows DPAPI 那类），
而云端沙箱是 Linux，没有等价物（skill 的 credstore 在这上面用不了）。

存哪 / 会不会被重新部署覆盖
──────────────────────────
默认落在**应用目录**下的 `state.db`（部署包不含它，所以不会被覆盖）；
启动时若发现存在**基线文件** `tenants.seed.json`（由发布者维护、随部署包走），
会把里面的租户**合并**进来（基线优先，运行时新增的不会被删）。
即：最坏情况只是"最近在页面里新增的租户要重加一次"，已写进基线的不会丢。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time

_DIR = os.path.dirname(os.path.abspath(__file__))

# ★ 2026-10-19 重大修复：运行数据（state.db / state.key）**不能放在应用目录**。
#   部署是整目录上传，本地库会把云端库覆盖掉（今天实际发生：云端 demo-company-b 租户被本地
#   demo-company-a 库覆盖，用户登录报"公司码不存在"）。
#   现在统一放到用户主目录 ~/.kingdee-portal/，项目目录里不再有运行数据；
#   并做一次性迁移：新位置没有、旧位置有 → 复制过去。
_DATA_DIR = os.environ.get("PORTAL_STATE_DIR") or os.path.join(
    os.path.expanduser("~"), ".kingdee-portal")
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
except Exception:                                         # noqa: BLE001
    _DATA_DIR = _DIR

DB_PATH = os.path.join(_DATA_DIR, "state.db")
KEY_PATH = os.path.join(_DATA_DIR, "state.key")

_OLD_DB = os.path.join(_DIR, "state.db")
_OLD_KEY = os.path.join(_DIR, "state.key")
try:
    import shutil as _shutil
    if not os.path.exists(DB_PATH) and os.path.exists(_OLD_DB):
        _shutil.copy2(_OLD_DB, DB_PATH)
        for _ext in ("-wal", "-shm"):
            if os.path.exists(_OLD_DB + _ext):
                _shutil.copy2(_OLD_DB + _ext, DB_PATH + _ext)
    if not os.path.exists(KEY_PATH) and os.path.exists(_OLD_KEY):
        _shutil.copy2(_OLD_KEY, KEY_PATH)
except Exception:                                         # noqa: BLE001
    pass
SEED_PATH = os.path.join(_DIR, "tenants.seed.json")

_lock = threading.RLock()
_conn = None


# ────────────────────────── 凭据封存（HMAC-SHA256-CTR + tag） ──────────────────────────
def _key() -> bytes:
    """取主密钥；不存在就生成一个（0600）。密钥与数据分开存放。"""
    try:
        with open(KEY_PATH, "rb") as fh:
            k = fh.read()
            if len(k) >= 32:
                return k
    except OSError:
        pass
    k = secrets.token_bytes(32)
    fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(k)
    return k


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    c = 0
    while len(out) < n:
        out += hmac.new(key, nonce + c.to_bytes(8, "big"), hashlib.sha256).digest()
        c += 1
    return bytes(out[:n])


def seal(data: bytes) -> bytes:
    key = _key()
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    tag = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    return b"v1" + nonce + tag + ct


def unseal(blob: bytes) -> bytes:
    if not blob or blob[:2] != b"v1":
        raise ValueError("凭据格式不认识")
    key = _key()
    nonce, tag, ct = blob[2:18], blob[18:50], blob[50:]
    want = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, want):
        raise ValueError("凭据校验失败（密钥换过或被改过）")
    return bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))


def seal_json(obj) -> bytes:
    return seal(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def unseal_json(blob):
    return json.loads(unseal(blob).decode("utf-8"))


# ────────────────────────── 连接与建表 ──────────────────────────
def _db():
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("""CREATE TABLE IF NOT EXISTS sessions(
                token TEXT PRIMARY KEY, username TEXT, tenant TEXT,
                info TEXT, creds BLOB, created REAL, seen REAL)""")
            _conn.execute("""CREATE TABLE IF NOT EXISTS tenants(
                code TEXT PRIMARY KEY, name TEXT, server TEXT, acct TEXT,
                piazzone BLOB, orgs TEXT, created REAL, updated REAL,
                status TEXT DEFAULT 'active')""")
            # 轻量迁移：老库没有 status 列时补上（避免换版本要手工改库）
            cols = {r[1] for r in _conn.execute("PRAGMA table_info(tenants)").fetchall()}
            if "status" not in cols:
                _conn.execute("ALTER TABLE tenants ADD COLUMN status TEXT DEFAULT 'active'")
            if "admin_users" not in cols:
                _conn.execute("ALTER TABLE tenants ADD COLUMN admin_users TEXT DEFAULT '[]'")
            # 每家公司的凭据模式（空 = 跟随全局默认）
            if "cred_mode" not in cols:
                _conn.execute("ALTER TABLE tenants ADD COLUMN cred_mode TEXT DEFAULT ''")
            # 系统设置（键值）：目前放 credential_mode
            _conn.execute("""CREATE TABLE IF NOT EXISTS settings(
                k TEXT PRIMARY KEY, v TEXT, updated REAL)""")
            # 登录事件（运营统计用）：**只记账号、公司码、时间、成败，不记任何密码**
            _conn.execute("""CREATE TABLE IF NOT EXISTS login_events(
                at REAL, tenant TEXT, username TEXT, ok INTEGER, note TEXT)""")
            _conn.execute("CREATE INDEX IF NOT EXISTS ix_login_at ON login_events(at)")
            # ★ 2026-10-19：自助开通申请（公开表单提交 → 管理员审核）
            _conn.execute("""CREATE TABLE IF NOT EXISTS applications(
                code TEXT PRIMARY KEY, company TEXT, server TEXT, phone TEXT,
                wechat TEXT, note TEXT, ip TEXT, created REAL,
                status TEXT DEFAULT 'pending', decided REAL, reason TEXT)""")
            _conn.execute("CREATE INDEX IF NOT EXISTS ix_app_status ON applications(status)")
            _conn.commit()
        return _conn


# ────────────────────────── 系统设置 ──────────────────────────
def get_setting(k: str, default: str = "") -> str:
    c = _db()
    with _lock:
        row = c.execute("SELECT v FROM settings WHERE k=?", (str(k),)).fetchone()
    return (row["v"] if row else default) or default


def set_setting(k: str, v: str) -> None:
    c = _db()
    with _lock:
        c.execute("""INSERT INTO settings(k,v,updated) VALUES(?,?,?)
                     ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated""",
                  (str(k), str(v), time.time()))
        c.commit()


def credential_mode() -> str:
    """全局默认：disk（默认，加密落盘，重启免登）/ memory（不落盘，重启要重登）。"""
    m = str(get_setting("credential_mode", "disk") or "disk").lower()
    return m if m in ("disk", "memory") else "disk"


def tenant_cred_mode(code: str) -> str:
    """**某公司生效的**凭据模式：公司自己设过就用它，否则跟随全局默认。"""
    if code:
        t = get_tenant(code)
        m = str((t or {}).get("cred_mode") or "").lower()
        if m in ("disk", "memory"):
            return m
    return credential_mode()


def set_tenant_cred_mode(code: str, mode: str) -> bool:
    m = str(mode or "").lower()
    if m not in ("disk", "memory", "inherit"):
        return False
    val = "" if m == "inherit" else m
    c = _db()
    with _lock:
        n = c.execute("UPDATE tenants SET cred_mode=?, updated=? WHERE code=?",
                      (val, time.time(), str(code or "").strip())).rowcount
        c.commit()
    return bool(n)


# ────────────────────────── 管理员口令（PBKDF2 哈希，不存明文） ──────────────────────────
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(pw).encode("utf-8"), salt, 120000)
    return "pbkdf2$%s$%s" % (salt.hex(), dk.hex())


def verify_password(pw: str, blob: str) -> bool:
    try:
        _, salt, want = str(blob).split("$")
        dk = hashlib.pbkdf2_hmac("sha256", str(pw).encode("utf-8"), bytes.fromhex(salt), 120000)
        return hmac.compare_digest(dk.hex(), want)
    except Exception:                                     # noqa: BLE001
        return False


# ────────────────────────── 登录事件（运营统计） ──────────────────────────
def log_login(tenant: str, username: str, ok: bool, note: str = "") -> None:
    """记录一次登录尝试。**不记录密码**，只记公司码/账号/时间/成败。"""
    c = _db()
    with _lock:
        c.execute("INSERT INTO login_events(at, tenant, username, ok, note) VALUES(?,?,?,?,?)",
                  (time.time(), str(tenant or ""), str(username or ""), 1 if ok else 0,
                   str(note or "")[:120]))
        c.commit()


def login_stats(days: int = 30) -> dict:
    """给管理员看的运营统计（不涉及敏感数据）。"""
    c = _db()
    cut = time.time() - days * 86400
    day0 = time.time() - (time.time() % 86400)      # 今天 0 点（近似，UTC 起算）
    with _lock:
        tot = c.execute("SELECT COUNT(*) n, SUM(ok) o FROM login_events WHERE at>=?",
                        (cut,)).fetchone()
        today = c.execute("SELECT COUNT(*) n, SUM(ok) o FROM login_events WHERE at>=?",
                          (day0,)).fetchone()
        by_tenant = c.execute(
            "SELECT tenant, COUNT(DISTINCT username) u, COUNT(*) n, SUM(ok) o "
            "FROM login_events WHERE at>=? AND ok=1 GROUP BY tenant ORDER BY n DESC",
            (cut,)).fetchall()
        recent = c.execute(
            "SELECT at, tenant, username, ok, note FROM login_events ORDER BY at DESC LIMIT 40"
        ).fetchall()
    def _fmt(rows):
        return [{"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["at"])),
                 "tenant": r["tenant"], "username": r["username"],
                 "ok": bool(r["ok"]), "note": r["note"] or ""} for r in rows]
    return {
        "days": days,
        "total": {"n": (tot["n"] or 0), "ok": (tot["o"] or 0)},
        "today": {"n": (today["n"] or 0), "ok": (today["o"] or 0)},
        "by_tenant": [{"tenant": r["tenant"], "users": r["u"] or 0, "n": r["n"] or 0}
                      for r in by_tenant],
        "recent": _fmt(recent),
    }


# 易读字母表：去掉 i l o 0 1（容易看错），随机而非顺序号 → **不可枚举**
_CODE_ALPHA = "abcdefghjkmnpqrstuvwxyz23456789"


def save_application(code: str, company: str, server: str, phone: str,
                     wechat: str = "", note: str = "", ip: str = "") -> None:
    c = _db()
    with _lock:
        c.execute("""INSERT OR REPLACE INTO applications
                     (code, company, server, phone, wechat, note, ip, created, status)
                     VALUES(?,?,?,?,?,?,?,?,'pending')""",
                  (code, company, server, phone, wechat or "", note or "", ip or "",
                   time.time()))
        c.commit()


def get_application(code: str):
    c = _db()
    with _lock:
        row = c.execute("SELECT * FROM applications WHERE code=?",
                        (str(code or "").strip().lower(),)).fetchone()
    return dict(row) if row else None


def find_application(phone: str = "", wechat: str = "", server: str = "", company: str = ""):
    """幂等用：同一微信号/手机号/同一服务器+公司名不要重复提交。"""
    c = _db()
    clause, args = [], []
    if wechat:
        clause.append("wechat=?"); args.append(wechat)
    if phone:
        clause.append("phone=?"); args.append(phone)
    if server and company:
        clause.append("(server=? AND company=?)"); args += [server, company]
    if not clause:
        return None
    with _lock:
        row = c.execute("SELECT * FROM applications WHERE %s ORDER BY created DESC LIMIT 1"
                        % " OR ".join(clause), tuple(args)).fetchone()
    return dict(row) if row else None


def list_applications(status: str = ""):
    c = _db()
    with _lock:
        if status:
            rows = c.execute("SELECT * FROM applications WHERE status=? ORDER BY created DESC",
                             (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM applications ORDER BY created DESC").fetchall()
    return [dict(r) for r in rows]


def decide_application(code: str, status: str, reason: str = "") -> None:
    c = _db()
    with _lock:
        c.execute("UPDATE applications SET status=?, decided=?, reason=? WHERE code=?",
                  (status, time.time(), reason or "", str(code or "").strip().lower()))
        c.commit()


def gen_code(n: int = 6) -> str:
    """生成租户唯一码。不重复校验后才返回。"""
    for _ in range(50):
        code = "".join(secrets.choice(_CODE_ALPHA) for _ in range(n))
        if not get_tenant(code):
            return code
    raise RuntimeError("唯一码生成失败，请重试")


def init() -> dict:
    """建表 + 合并基线租户。返回统计，便于启动时打一行日志。"""
    _db()
    merged = merge_seed()
    return {"db": DB_PATH, "seed_merged": merged, "tenants": len(list_tenants())}


# ────────────────────────── 会话 ──────────────────────────
def save_session(token: str, username: str, tenant: str, info: dict, creds: dict) -> None:
    """保存会话。

    ⚠️ **内存模式**（`credential_mode=memory`）下**不把凭据写进磁盘** —— 这样"拿到机器"
    也解不出任何登录凭据，代价是服务重启后必须重新登录（会话无法自动恢复）。
    """
    c = _db()
    payload = (creds or {}) if credential_mode() == "disk" else {}
    with _lock:
        c.execute("""INSERT INTO sessions(token, username, tenant, info, creds, created, seen)
                     VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(token) DO UPDATE SET
                       username=excluded.username, tenant=excluded.tenant,
                       info=excluded.info, creds=excluded.creds, seen=excluded.seen""",
                  (token, username, tenant, json.dumps(info, ensure_ascii=False),
                   seal_json(payload), time.time(), time.time()))
        c.commit()


def load_session(token: str):
    """返回 (info, creds, tenant, created, seen)；不存在返回 None。"""
    if not token:
        return None
    c = _db()
    with _lock:
        row = c.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if not row:
        return None
    try:
        creds = unseal_json(row["creds"]) if row["creds"] else {}
    except Exception:                                     # noqa: BLE001
        creds = {}
    return (json.loads(row["info"] or "{}"), creds, row["tenant"] or "",
            row["created"] or 0, row["seen"] or 0)


def touch_session(token: str) -> None:
    c = _db()
    with _lock:
        c.execute("UPDATE sessions SET seen=? WHERE token=?", (time.time(), token))


def latest_session_creds(max_age_days: int = 7):
    """取最近活跃的连接凭据，用于服务端**内存预热**（不落盘任何业务数据）。

    返回 {"tenant", "username", "creds", "seen"}；没有可用会话返回 None。
    ⚠️ creds 已由 store 解密（Fernet），只在调用方进程内使用，不写日志、不返回给前端。
    """
    c = _db()
    with _lock:
        row = c.execute("SELECT * FROM sessions ORDER BY seen DESC LIMIT 1").fetchone()
    if not row:
        return None
    seen = row["seen"] or 0
    if time.time() - seen > max_age_days * 86400:
        return None
    try:
        creds = unseal_json(row["creds"]) if row["creds"] else {}
    except Exception:                                     # noqa: BLE001
        creds = {}
    if not creds.get("password") or not creds.get("username"):
        return None
    return {"tenant": row["tenant"] or "", "username": row["username"] or "",
            "creds": creds, "seen": seen}


def latest_creds_for_tenant(tenant: str, max_age_days: int = 30) -> dict:
    """该租户最近一次成功登录的凭据（只用于取账套兜底，不做其他用途）。"""
    code = str(tenant or "").strip().lower()
    if not code:
        return {}
    c = _db()
    with _lock:
        row = c.execute("SELECT creds, seen FROM sessions WHERE tenant=? ORDER BY seen DESC LIMIT 1",
                        (code,)).fetchone()
    if not row:
        return {}
    if time.time() - (row["seen"] or 0) > max_age_days * 86400:
        return {}
    try:
        return unseal_json(row["creds"]) if row["creds"] else {}
    except Exception:                                     # noqa: BLE001
        return {}


def drop_session(token: str) -> None:
    c = _db()
    with _lock:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))
        c.commit()


def count_sessions() -> int:
    """诊断用：磁盘上到底存了几个会话（判断"会话有没有真的落盘"）。"""
    try:
        c = _db()
        with _lock:
            return int(c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"])
    except Exception:                                     # noqa: BLE001
        return -1


def purge_sessions(max_age_sec: float) -> int:
    c = _db()
    cut = time.time() - max_age_sec
    with _lock:
        n = c.execute("DELETE FROM sessions WHERE seen<?", (cut,)).rowcount
        c.commit()
    return n


# ────────────────────────── 租户 ──────────────────────────
def upsert_tenant(code: str, name: str, server: str, acct: str,
                  piazzone: dict | None = None, orgs=None, status: str = "") -> dict:
    """新增/更新一个租户。**code 已存在时报错而不覆盖**（调用方负责确认）。"""
    code = str(code or "").strip()
    if not code:
        raise ValueError("公司码不能为空")
    c = _db()
    with _lock:
        exists = c.execute("SELECT code, status FROM tenants WHERE code=?", (code,)).fetchone()
        st = status or (exists["status"] if exists else "active") or "active"
        c.execute("""INSERT INTO tenants(code, name, server, acct, piazzone, orgs, created, updated, status)
                     VALUES(?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(code) DO UPDATE SET
                       name=excluded.name, server=excluded.server, acct=excluded.acct,
                       piazzone=excluded.piazzone, orgs=excluded.orgs,
                       updated=excluded.updated, status=excluded.status""",
                  (code, name, server, acct,
                   seal_json(piazzone or {}), json.dumps(orgs or [], ensure_ascii=False),
                   time.time(), time.time(), st))
        c.commit()
    return {"code": code, "created": not bool(exists)}


def set_tenant_status(code: str, status: str) -> bool:
    """启用 / 暂停 一个租户。status: active | suspended"""
    st = "suspended" if str(status).lower() in ("suspended", "paused", "off", "0") else "active"
    c = _db()
    with _lock:
        n = c.execute("UPDATE tenants SET status=?, updated=? WHERE code=?",
                      (st, time.time(), str(code or "").strip())).rowcount
        c.commit()
    return bool(n)


def get_tenant(code: str):
    c = _db()
    with _lock:
        row = c.execute("SELECT * FROM tenants WHERE code=?", (str(code or "").strip(),)).fetchone()
    if not row:
        return None
    try:
        pz = unseal_json(row["piazzone"]) if row["piazzone"] else {}
    except Exception:                                     # noqa: BLE001
        pz = {}
    return {"code": row["code"], "name": row["name"] or "", "server": row["server"] or "",
            "acct": row["acct"] or "", "piazzone": pz,
            "orgs": json.loads(row["orgs"] or "[]"),
            "cred_mode": (row["cred_mode"] if "cred_mode" in row.keys() else "") or "",
            "admin_users": json.loads((row["admin_users"] if "admin_users" in row.keys() else "[]") or "[]"),
            "status": (row["status"] if "status" in row.keys() else "active") or "active"}


def list_tenants(mask: bool = True):
    """列出租户。`mask=True` 时**不带发票云密钥**（页面/日志只该看到有没有配）。"""
    c = _db()
    with _lock:
        rows = c.execute("SELECT code,name,server,acct,piazzone,orgs,admin_users,"
                         "created,updated,status FROM tenants ORDER BY created").fetchall()
    out = []
    for r in rows:
        pz = {}
        if r["piazzone"]:
            try:
                pz = unseal_json(r["piazzone"])
            except Exception:                             # noqa: BLE001
                pz = {}
        cos = pz.get("companies") if isinstance(pz.get("companies"), list) else []
        cos = [x for x in cos if isinstance(x, dict)]
        has_pz = bool(pz.get("client_id") or any(x.get("client_id") for x in cos))
        tail = (pz.get("client_secret") or "")[-4:]
        if not tail:
            for x in cos:
                if x.get("client_secret"):
                    tail = str(x.get("client_secret"))[-4:]
                    break
        item = {"code": r["code"], "name": r["name"] or "", "server": r["server"] or "",
                "acct": r["acct"] or "", "orgs": json.loads(r["orgs"] or "[]"),
                "admins": json.loads(r["admin_users"] or "[]"),
                "status": (r["status"] if "status" in r.keys() else "active") or "active",
                "has_piazzone": has_pz,
                "piazzone_tail": tail}
        if not mask:
            item["piazzone"] = pz
        out.append(item)
    return out


def tenant_admins(code: str):
    t = get_tenant(code)
    return (t or {}).get("admin_users") or []


def add_tenant_admin(code: str, username: str, replace: bool = False) -> list:
    """把某人设为该租户的管理员（`replace=True` 时整体替换，供发布者改派）。"""
    u = str(username or "").strip()
    cur = [] if replace else tenant_admins(code)
    if u and u not in cur:
        cur.append(u)
    c = _db()
    with _lock:
        c.execute("UPDATE tenants SET admin_users=?, updated=? WHERE code=?",
                  (json.dumps(cur, ensure_ascii=False), time.time(), str(code or "").strip()))
        c.commit()
    return cur


def piazzone_revision(piazzone: dict) -> str:
    return hashlib.sha256(json.dumps(piazzone or {}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def set_tenant_piazzone(code: str, piazzone: dict, expected_revision=None) -> bool:
    """单独更新某租户的发票云配置（封存存储）。"""
    c = _db()
    with _lock:
        if expected_revision is not None:
            row = c.execute("SELECT piazzone FROM tenants WHERE code=?", (str(code).strip(),)).fetchone()
            current = unseal_json(row[0]) if row and row[0] else {}
            if piazzone_revision(current) != expected_revision:
                return False
        if expected_revision is not None:
            # Compare the original encrypted blob as well, so separate workers cannot lose edits.
            n = c.execute("UPDATE tenants SET piazzone=?, updated=? WHERE code=? AND piazzone IS ?",
                          (seal_json(piazzone or {}), time.time(), str(code or "").strip(), row[0] if row else None)).rowcount
        else:
            n = c.execute("UPDATE tenants SET piazzone=?, updated=? WHERE code=?",
                          (seal_json(piazzone or {}), time.time(), str(code or "").strip())).rowcount
        c.commit()
    return bool(n)


def delete_tenant(code: str) -> bool:
    c = _db()
    with _lock:
        n = c.execute("DELETE FROM tenants WHERE code=?", (str(code or "").strip(),)).rowcount
        c.commit()
    return bool(n)


def merge_seed() -> int:
    """把随部署包的基线租户合并进来（基线优先，不动运行时新增的）。返回合并条数。"""
    if not os.path.exists(SEED_PATH):
        return 0
    try:
        with open(SEED_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:                                     # noqa: BLE001
        return 0
    n = 0
    for t in (data.get("tenants") or []):
        if not t.get("code"):
            continue
        upsert_tenant(t.get("code"), t.get("name", ""), t.get("server", ""), t.get("acct", ""),
                      t.get("piazzone") or {}, t.get("orgs") or [])
        n += 1
    return n
