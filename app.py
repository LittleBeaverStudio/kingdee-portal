# -*- coding: utf-8 -*-
"""金蝶经营门户 · 本地试验版（Flask）

设计原则（与安全评估一一对应）
------------------------------
1. 不使用任何内置金蝶凭据：账号密码由登录者在页面输入，只用于建立会话，用完即弃。
   服务端内存里只有 requests.Session（金蝶给的会话 cookie），没有密码。
2. 门户自身不做权限判断：每个请求都用「登录者本人」的会话去金蝶取数，
   能取到什么完全由金蝶权限决定；取不到就如实显示"无权限/无数据"，不兜底、不缓存成假数据。
3. 组织名不内置：登录后自动发现，并把每一级的成功/失败原因摆到界面上。
4. 默认只监听 127.0.0.1：这是本地试验，不是公网服务。
"""
from __future__ import annotations

import ipaddress
import hashlib
import json
import os
import re
import secrets
import socket
import sys
import tempfile
import urllib.request
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

from flask import (Flask, Response, g, jsonify, make_response, render_template, request,
                   send_from_directory)

import metrics_cashflow
import metrics_expense
import metrics_mgmt
from invoice_config import merge_org_rows, org_codes
import metrics_mgmt_full
import metrics_sales
import portal_actions
import store
from kd_client import KdError, KingdeeClient, normalize_base_url

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "portal_config.json")
EXAMPLE_CONFIG_PATH = os.path.join(BASE_DIR, "portal_config.example.json")

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8765,
    "server_url": "",
    "acct_name": "",
    "scheme_id": metrics_mgmt.DEFAULT_SCHEME_ID,
    # 会话时长：放宽到"一天内回来都还在"，配合 _relogin() 基本见不到"会话失效"
    "session_idle_minutes": 720,
    "session_max_hours": 168,
    "cache_ttl_minutes": 30,
    "warmup_auto": True,             # 后台内存预热（线程），只在内存、不落盘
    "warmup_token": "",              # ⚠️ 已废弃：预热口令只认环境变量 PORTAL_WARMUP_TOKEN
    "max_bills_for_detail": 0,
    "api_calls_per_minute": 420,
    "max_books": 12,
    "allow_http": False,
    "block_private_network": True,
    "login_rate_per_minute": 5,
    "debug": False,
    "version": "1.5.5",
    # ── 写操作（报销办理）─────────────────────────────────────────────
    # ⚠️ 这是门户里唯一的写入口，与只读看板的权限模型不同：
    #    只读看板用「登录者本人的会话」，写操作走 skill 配置的账号（通常需收票池权限）。
    #    因此默认关闭，需要你显式打开再使用。
    "enable_write_ops": False,
    "expense_skill_dir": "",          # 留空则自动探测 kingdee-expense-flow
    "audit_log": "portal_audit.log",  # 写操作审计日志（JSONL）
    # ── DSH 插件内嵌（iframe 嵌入）──────────────────────────────────
    # 本门户只监听 127.0.0.1，不是公网服务。设为 True 后去掉
    # X-Frame-Options: DENY 与 frame-ancestors 'none'，允许本地任意页面
    # （如 DSH 的 Web GUI 动态端口）用 iframe 嵌入完整门户界面。
    # 公网发布的副本保持默认 False，安全基线不变。
    "allow_local_embedding": False,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    # Public checkouts ship only the example config. Fall back to it so a fresh
    # clone runs out of the box; a local portal_config.json still wins.
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else (
        EXAMPLE_CONFIG_PATH if os.path.exists(EXAMPLE_CONFIG_PATH) else "")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                cfg.update(json.load(fh) or {})
        except Exception as exc:  # noqa: BLE001
            print("[warn] 配置文件读取失败，使用默认配置：", exc)
    return cfg


CFG = load_config()
# DSH 本地版与原公网租户模式分离：仅插件启动时启用。
LOCAL_MODE = os.environ.get("PORTAL_LOCAL_MODE") == "1"
# 发布到云端沙箱时，平台会注入 PORT，并要求服务绑定 0.0.0.0（而不是只监听本机）。
# 本地跑（没有 PORT）时行为完全不变：只监听 127.0.0.1。
if os.environ.get("PORT") and not LOCAL_MODE:
    CFG["port"] = int(os.environ["PORT"])
    CFG["host"] = "0.0.0.0"
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_AS_ASCII"] = False
# 本地试验版：模板改动即时生效，避免服务端缓存旧页面导致前后端不一致
app.config["TEMPLATES_AUTO_RELOAD"] = True

# ---------------------------------------------------------------- 会话与任务
_sessions = {}
_jobs = {}
_BOOT_AT = time.time()          # 进程启动时刻（诊断用：判断容器是否被平台重启过）
_lock = threading.Lock()
# 发票云客户端依赖进程级环境变量和模块状态，配置、测试和上传必须串行。
_piazzone_request_lock = threading.RLock()


def _now():
    return time.time()


def _cleanup():
    idle = CFG["session_idle_minutes"] * 60
    maxlife = CFG["session_max_hours"] * 3600
    with _lock:
        for token, s in list(_sessions.items()):
            if _now() - s["seen"] > idle or _now() - s["created"] > maxlife:
                _sessions.pop(token, None)
        for jid, j in list(_jobs.items()):
            if _now() - j.created > 3600:
                _jobs.pop(jid, None)
    # 磁盘上的会话也要清（否则会无限长），用更宽松的窗口保留
    try:
        store.purge_sessions(max(idle, maxlife))
    except Exception:                                     # noqa: BLE001
        pass


def _relogin(s) -> bool:
    """会话过期后，用**内存里留着的凭据自动重新登录** —— 让用户完全无感。

    这是"取数失败（会话）"的正解：不是把文案改好看，而是根本不让它发生。
    凭据只来自这次登录，不落盘；重登失败才回落成"请重新登录"。
    ⚠️ 网络调用放在锁外，否则会占着 `_lock` 把整个站点卡住。
    """
    c = dict(s.get("creds") or {})
    if not c:
        return False
    try:
        base = _validate_base_url(c.pop("base_url", ""))
        cli = KingdeeClient(base, per_minute=int(CFG.get("api_calls_per_minute", 420)), **c)
        cli.login()
    except Exception as exc:                              # noqa: BLE001
        # 失败原因落库（不含密码），便于事后查"为什么恢复会话失败"
        try:
            store.set_setting("last_relogin_error",
                              "%s | %s" % (time.strftime("%m-%d %H:%M"), repr(exc)[:180]))
        except Exception:                                 # noqa: BLE001
            pass
        return False
    with _lock:
        s["client"] = cli
        s["seen"] = _now()
        s["creds"] = {**c, "base_url": base}
        s.pop("act_kd", None)          # 技能侧客户端也作废，下次重建
        s.pop("act_at", None)
    return True


def _get_session(create=False):
    # ★ 内嵌模式：会话 token 优先走请求头 X-Portal-Sid（前端把它存在门户自己
    #   域下的 localStorage 里，跨站 iframe 里第三方 cookie 被浏览器丢弃时仍能
    #   保持登录态）；没有头就回落 cookie —— 两种模式完全兼容。
    token = (request.headers.get("X-Portal-Sid") or "").strip()
    if not LOCAL_MODE and not token:
        token = request.cookies.get("portal_sid", "")
    if LOCAL_MODE:
        with _lock:
            s = _sessions.get(token) if token else None
            if s and (_now() - s["created"] > CFG["session_max_hours"] * 3600 or
                      _now() - s["seen"] > CFG["session_idle_minutes"] * 60):
                _sessions.pop(token, None)
                s = None
            if s:
                s["seen"] = _now()
                return token, s
            if create:
                token = secrets.token_urlsafe(32)
                s = {"client": None, "created": _now(), "seen": _now(),
                     "orgs": None, "org_trace": None, "notes": [],
                     "cache": {}, "info": {}}
                _sessions[token] = s
                return token, s
        return token, None
    s = None
    need_relogin = False
    restored = False
    with _lock:
        s = _sessions.get(token) if token else None
        if s:
            age_ok = _now() - s["created"] <= CFG["session_max_hours"] * 3600
            idle_ok = _now() - s["seen"] <= CFG["session_idle_minutes"] * 60
            if age_ok and idle_ok:
                s["seen"] = _now()
                return token, s
            need_relogin = bool(s.get("creds")) and age_ok
        if create:
            token = secrets.token_urlsafe(32)
            _sessions[token] = {"client": None, "created": _now(), "seen": _now(),
                                "orgs": None, "org_trace": None, "notes": [],
                                "cache": {}, "info": {}}
            return token, _sessions[token]
    # ① 内存里没有 → 从磁盘恢复。
    #    **容器被平台回收重启后就是走这条路**，恢复出来自带凭据，所以用户不用重登。
    #    这就是"掉线"的根治：会话不再只活在进程内存里。
    if token:
        try:
            rec = store.load_session(token)
        except Exception:                                 # noqa: BLE001
            rec = None
        if rec:
            info, creds, tenant, created, seen = rec
            age_ok = (_now() - (created or 0)) <= CFG["session_max_hours"] * 3600
            idle_ok = (_now() - (seen or 0)) <= CFG["session_idle_minutes"] * 60
            if age_ok and idle_ok:
                s = {"client": None, "created": created or _now(), "seen": _now(),
                     "orgs": None, "org_trace": None, "notes": [], "cache": {},
                     "info": info or {}, "creds": creds or {}, "tenant": tenant or ""}
                with _lock:
                    _sessions[token] = s
                restored = True
                need_relogin = bool(creds)
    # ② 需要（重新）建立金蝶连接 —— 必须在锁外，网络调用不能占着锁
    if s is not None and (restored or need_relogin) and not _relogin(s):
        return token, None            # 凭据已失效（改过密码/环境变了）→ 只能重登
    return token, s


def _require_session():
    _token, s = _get_session()
    if not s or not s.get("client"):
        raise KdError("登录已过期", "会话", "页面上重新登录一次即可（之后会一直保持）")
    return s


class Job:
    """把耗时取数放到后台线程，前端轮询进度（避免请求超时）"""

    def __init__(self, name, owner=None):
        self.id = uuid.uuid4().hex
        self.owner = owner
        self.name = name
        self.status = "running"
        self.step = "准备中"
        self.pct = 0
        self.result = None
        self.error = None
        self.created = _now()
        with _lock:
            _jobs[self.id] = self

    def progress(self, text, pct=None):
        with _lock:
            self.step = text
            if pct is not None:
                self.pct = pct

    def done(self, result):
        with _lock:
            self.status = "done"
            self.result = result
            self.pct = 100
            self.step = "完成"

    def fail(self, err):
        with _lock:
            self.status = "error"
            self.error = err
            self.step = "失败"

    def snapshot(self):
        with _lock:
            return {"id": self.id, "name": self.name, "status": self.status, "step": self.step,
                    "pct": self.pct, "result": self.result, "error": self.error}

    def run(self, fn):
        def _wrap():
            try:
                fn(self)
            except KdError as exc:
                self.fail(exc.to_dict())
            except Exception as exc:  # noqa: BLE001
                self.fail({"stage": "内部", "message": f"{type(exc).__name__}: {exc}"})
        threading.Thread(target=_wrap, daemon=True).start()
        return self


# ---------------------------------------------------------------- 安全辅助
def _add_security_headers(resp: Response) -> Response:
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    # ★ DSH 插件内嵌：默认安全基线（拒绝任何 iframe 嵌入）；
    #   portal_config.json 设 "allow_local_embedding": true 后，
    #   本服务只监听 127.0.0.1，放开 frame-ancestors 允许本地页面（含 DSH Web GUI
    #   的任意动态端口）嵌入完整门户界面。
    if LOCAL_MODE and CFG.get("allow_local_embedding"):
        # Desktop is served from the dedicated dsh-app scheme. Web's exact
        # listener origin is supplied by the Host, never from request headers.
        ancestors = ["dsh-app:"]
        web_origin = os.environ.get("PORTAL_DSH_WEB_ORIGIN", "")
        if re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}", web_origin):
            port = int(web_origin.rsplit(':', 1)[1])
            if 1 <= port <= 65535:
                ancestors.append(web_origin)
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; "
            "frame-ancestors " + " ".join(ancestors) + "; base-uri 'none'")
    else:
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
    return resp


def _check_csrf():
    cookie = request.cookies.get("portal_csrf", "")
    header = request.headers.get("X-CSRF-Token", "")
    if LOCAL_MODE:
        token, active = _get_session()
        with _lock:
            ref = active.get("csrf", "") if active and _sessions.get(token) is active else ""
        if header and ref and secrets.compare_digest(header, ref):
            return
        raise KdError("请求校验失败（CSRF）", "安全", "刷新页面后重试")
    # cookie 可用时走原逻辑（同源访问完全不变）
    if cookie and header:
        if cookie != header:
            raise KdError("请求校验失败（CSRF）", "安全", "刷新页面后重试")
        return
    # ★ 内嵌模式（iframe）：第三方 cookie 被浏览器丢弃时，cookie 为空。
    #   此时用会话内登记的 csrf（/api/bootstrap 写入）+ 请求头校验：
    #   攻击者拿不到 X-Portal-Sid（存于门户域的 localStorage，跨站不可达），
    #   也拿不到该会话的 csrf，校验强度与原 cookie 方案一致。
    if header:
        token = (request.headers.get("X-Portal-Sid") or "").strip() or request.cookies.get("portal_sid", "")
        s = _sessions.get(token)
        ref = (s or {}).get("csrf") or request.cookies.get("portal_csrf", "")
        if token and s and ref and header == ref:
            return
    raise KdError("请求校验失败（CSRF）", "安全", "刷新页面后重试")


def _validate_base_url(raw: str) -> str:
    """SSRF 加固：只允许 http(s)，默认拒绝内网/回环地址（可用配置放开）"""
    url = normalize_base_url(raw)
    parts = urlparse(url)
    if parts.scheme not in ("https", "http"):
        raise KdError("服务器地址协议不支持", "配置", "只支持 http/https")
    if parts.scheme == "http" and not CFG.get("allow_http"):
        raise KdError("暂不允许 http 明文地址", "配置",
                      "金蝶云星空一般是 https；若确为内网 http，请在 portal_config.json 里把 allow_http 设为 true")
    host = parts.hostname or ""
    if CFG.get("block_private_network", True):
        try:
            infos = socket.getaddrinfo(host, None)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    raise KdError("该地址解析到内网/回环 IP，已被策略拒绝", "配置",
                                  "如确需访问内网金蝶，请在 portal_config.json 里把 "
                                  "block_private_network 设为 false（请自行评估风险）")
        except socket.gaierror:
            raise KdError("域名无法解析", "配置", "检查服务器地址是否写对、当前网络是否可达") from None
    return url


_login_hits = {}


def _login_rate_ok(ip: str) -> bool:
    limit = int(CFG.get("login_rate_per_minute", 5))
    now = _now()
    with _lock:
        hits = [t for t in _login_hits.get(ip, []) if now - t < 60]
        if len(hits) >= limit:
            _login_hits[ip] = hits
            return False
        hits.append(now)
        _login_hits[ip] = hits
    return True


# ---------------------------------------------------------------- 页面
@app.before_request
def _local_mode_boundary():
    if not LOCAL_MODE:
        return None
    # 本地模式不开放公网平台的管理或申请端点。
    if request.path.startswith(("/api/admin/", "/api/apply", "/api/warmup")) or request.path in (
        "/api/reimburse/piazzone-transfer", "/api/reimburse/piazzone-mode",
        "/api/net/probe"
    ):
        return jsonify({"ok": False, "error": {"stage": "模式", "message": "本地版不提供平台管理功能"}}), 404
    host = (request.host or "").split(":", 1)[0].lower()
    if host not in ("127.0.0.1", "localhost"):
        return jsonify({"ok": False, "error": {"stage": "安全", "message": "只允许本机访问"}}), 403
    return None


@app.after_request
def _after(resp):
    return _add_security_headers(resp)


@app.get("/api/admin/need-setup")
def api_admin_need_setup():
    """控制台是否还没设置口令（用于前端显示「首次设置」表单）。"""
    return jsonify({"ok": True, "need_setup": not bool(_admin_password())}), 200


@app.post("/api/admin/setup")
def api_admin_setup():
    """首次设置管理员口令：**只在当前没有口令时**允许；设过之后自动关闭。

    ⚠️ 部署后到首次设置之间有一个窗口期（任何人打开根地址都可能抢先设置），
    所以部署完请立刻设置。口令存 state.db（不进配置文件、不进发布包）。
    """
    if _admin_password():
        return jsonify({"ok": False, "error": {"message": "口令已设置，请直接登录"}}), 403
    body = request.get_json(silent=True) or {}
    pw = str(body.get("password") or "")
    if len(pw) < 8:
        return jsonify({"ok": False, "error": {"message": "口令至少 8 位"}}), 200
    try:
        store.set_setting("admin_password", pw)
    except Exception as exc:                              # noqa: BLE001
        return jsonify({"ok": False, "error": {"message": "保存失败：%s" % str(exc)[:80]}}), 200
    _audit("管理-设置口令", {"via": "setup"}, "setup")
    return jsonify({"ok": True}), 200


# ---------------------------------------------------------------- 自助开通申请
_apply_hits = {}                 # ip -> [ts, ...]（每 IP 每小时 3 次）


def _apply_rate_ok(ip: str) -> bool:
    now = _now()
    with _lock:
        hits = [t for t in _apply_hits.get(ip, []) if now - t < 3600]
        if len(hits) >= 3:
            _apply_hits[ip] = hits
            return False
        hits.append(now)
        _apply_hits[ip] = hits
        return True


def _apply_webhook() -> str:
    try:
        return str(store.get_setting("apply_webhook", "") or "").strip()
    except Exception:                                     # noqa: BLE001
        return ""


def _notify_apply(row: dict, base_url: str = "") -> None:
    """新申请推到企业微信群机器人。尽力而为：失败只写日志，不影响申请人。"""
    url = _apply_webhook()
    lines = ["**新的开通申请**",
             "> 公司：%s" % (row.get("company") or ""),
             "> 服务器：%s" % (row.get("server") or ""),
             "> 手机号：%s" % (row.get("phone") or "")]
    if row.get("wechat"):
        lines.append("> 微信：%s" % row["wechat"])
    if row.get("note"):
        lines.append("> 备注：%s" % row["note"])
    lines.append("> 公司码：`%s`" % (row.get("code") or ""))
    if base_url:
        lines.append("> 审核：[打开控制台](%s/)" % base_url.rstrip("/"))
    payload = {"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}}

    def work():
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as exc:                          # noqa: BLE001
            sys.stderr.write("[apply-notify] %r\n" % (exc,))
            # ★ 2026-10-20：原来失败只写 stderr，用户完全看不到（"昨天好用今天坏了"无从判断）。
            #   现在写审计，控制台「运营统计」能看到「推送失败 + 原因」。
            try:
                _audit("推送-企业微信", {"company": row.get("company") or "",
                                        "err": repr(exc)[:160]}, "系统")
            except Exception:                             # noqa: BLE001
                pass

    if not url:
        try:
            _audit("推送-企业微信", {"company": row.get("company") or "",
                                    "err": "未配置 webhook 地址（控制台「安全与凭据」里填）"}, "系统")
        except Exception:                                 # noqa: BLE001
            pass
        return

    threading.Thread(target=work, daemon=True).start()


@app.get("/api/admin/apply-webhook")
def api_admin_webhook_get():
    if not _admin_ok():
        return _err(_ADMIN_FAIL, "权限")
    w = _apply_webhook()
    masked = ""
    if w:
        masked = w[:38] + "…" + w[-6:] if len(w) > 50 else w
    return jsonify({"ok": True, "configured": bool(w), "webhook": masked}), 200


@app.post("/api/admin/apply-webhook")
def api_admin_webhook_set():
    _check_csrf()
    if not _admin_ok():
        return _err(_ADMIN_FAIL, "权限")
    body = request.get_json(silent=True) or {}
    url = str(body.get("webhook") or "").strip()
    if url and not url.startswith("https://"):
        return _err("通知设置", "webhook 必须是 https:// 地址")
    store.set_setting("apply_webhook", url)
    _audit("管理-申请通知", {"set": bool(url)}, "admin")
    return jsonify({"ok": True, "configured": bool(url)}), 200


@app.post("/api/admin/apply-webhook/test")
def api_admin_webhook_test():
    _check_csrf()
    if not _admin_ok():
        return _err(_ADMIN_FAIL, "权限")
    if not _apply_webhook():
        return _err("通知设置", "还没填 webhook")
    try:
        _notify_apply({"company": "（测试消息）", "server": "https://example.ik3cloud.com/",
                       "phone": "13800000000", "code": "test01"}, request.url_root.rstrip("/"))
    except Exception as exc:                              # noqa: BLE001
        return _err("通知设置", repr(exc)[:120])
    return jsonify({"ok": True, "note": "已发送（若群内没收到，检查机器人是否被移出群）"}), 200


@app.post("/api/apply")
def api_apply():
    """公开申请开通：公司名 / 服务器 / 手机号 / 微信号。

    设计（2026-10-19）：**提交即生成专属链接，但租户是 pending**——管理员在控制台
    点一下才真正可用。这样体验上"填完就有链接"，又不至于让公开表单变成扫站入口。
    """
    _check_csrf()
    ip = request.remote_addr or "-"
    body = request.get_json(silent=True) or {}
    company = str(body.get("company") or "").strip()
    server = str(body.get("server") or "").strip()
    phone = str(body.get("phone") or "").strip()
    wechat = str(body.get("wechat") or "").strip()
    note = str(body.get("note") or "").strip()[:200]
    if not company or len(company) < 2:
        return _err("开通申请", "请填写公司名称")
    if not wechat:
        return _err("开通申请", "请填微信号（开通通知发到这里）")
    if len(wechat) < 2 or len(wechat) > 60:
        return _err("开通申请", "微信号长度不对（2–60 个字符）")
    if phone and not re.fullmatch(r"1[3-9]\d{9}", phone):
        return _err("开通申请", "手机号格式不对（不填也可以）")
    if not _apply_rate_ok(ip):
        return jsonify({"ok": False, "error": {
            "stage": "频率", "message": "提交太频繁，请稍后再试",
            "hint": "同一网络每小时最多提交 3 次，避免被滥用。"}}), 200
    try:
        base = _validate_base_url(server)
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200

    # 幂等：同手机号，或同「服务器 + 公司名」已经申请过 → 直接返回原来的
    try:
        dup = store.find_application(phone=phone, wechat=wechat, server=base, company=company)
    except Exception:                                     # noqa: BLE001
        dup = None
    if dup:
        return jsonify({"ok": True, "code": dup["code"], "status": dup["status"],
                        "server": dup["server"], "company": dup["company"],
                        "dup": True}), 200

    # 探活：确认是金蝶 WebAPI 端点（地址不通就别建，免得对方白等）
    probe = _probe_server(base)
    if not probe.get("reachable"):
        return jsonify({"ok": False, "error": {
            "stage": "服务器验证", "message": "这个地址没有连上金蝶",
            "hint": "%s。请填形如 https://xxx.ik3cloud.com/ 的地址。" % probe.get("note", "")}}), 200

    code = store.gen_code()
    try:
        store.upsert_tenant(code, company, base, "", None, [], "pending")
        store.save_application(code, company, base, phone, wechat, note, ip)
    except Exception as exc:                              # noqa: BLE001
        return _err("开通申请", "保存失败：%s" % str(exc)[:100])
    _audit("申请-提交", {"code": code, "company": company, "ip": ip}, "apply")
    try:
        _notify_apply({"code": code, "company": company, "server": base, "phone": phone,
                       "wechat": wechat, "note": note}, request.url_root.rstrip("/"))
    except Exception:                                     # noqa: BLE001
        pass
    # ⚠️ 不把公司码返回给申请人（2026-10-19 用户要求）：专属链接由发布者
    #    在审核通过后通过微信发给他，系统不直接吐出链接。
    return jsonify({"ok": True, "status": "pending",
                    "server": base, "company": company}), 200


@app.get("/api/apply/status")
def api_apply_status():
    """查申请状态（凭公司码或手机号）。不返回任何账号/密码。"""
    code = str(request.args.get("code") or "").strip().lower()
    phone = str(request.args.get("phone") or "").strip()
    wechat = str(request.args.get("wechat") or "").strip()
    app_row = store.get_application(code) if code else None
    if not app_row and wechat:
        app_row = store.find_application(wechat=wechat)
    if not app_row and phone:
        app_row = store.find_application(phone=phone)
    if not app_row:
        return jsonify({"ok": False, "error": {"message": "没有找到这个申请"}}), 200
    t = store.get_tenant(app_row["code"]) or {}
    return jsonify({"ok": True, "company": app_row["company"],
                    "status": app_row["status"],
                    "tenant_status": t.get("status") or "",
                    "reason": app_row.get("reason") or "",
                    "created": app_row.get("created")}), 200


@app.get("/api/admin/applications")
def api_admin_applications():
    if not _admin_ok():
        return _err(_ADMIN_FAIL, "权限", "在「租户管理」页顶部输入管理员口令")
    st = str(request.args.get("status") or "pending")
    return jsonify({"ok": True, "apps": store.list_applications(st if st != "all" else "")}), 200


@app.post("/api/admin/applications/decide")
def api_admin_app_decide():
    """通过 / 拒绝一个开通申请。通过 = 租户 active；拒绝 = 租户 suspended。"""
    _check_csrf()
    if not _admin_ok():
        return _err(_ADMIN_FAIL, "权限")
    body = request.get_json(silent=True) or {}
    code = str(body.get("code") or "").strip().lower()
    action = str(body.get("action") or "").strip()
    reason = str(body.get("reason") or "").strip()[:200]
    app_row = store.get_application(code)
    if not app_row:
        return _err("审核", "没有这个申请")
    if action == "approve":
        store.set_tenant_status(code, "active")
        store.decide_application(code, "approved")
        _audit("申请-通过", {"code": code, "company": app_row.get("company")}, "admin")
        return jsonify({"ok": True, "status": "approved"}), 200
    if action == "reject":
        store.set_tenant_status(code, "suspended")
        store.decide_application(code, "rejected", reason)
        _audit("申请-拒绝", {"code": code, "reason": reason}, "admin")
        return jsonify({"ok": True, "status": "rejected"}), 200
    return _err("审核", "action 只能是 approve / reject")


@app.get("/api/tenant-info")
def api_tenant_info():
    """按公司码返回**登录页需要的最小信息**（公司名 / 服务器 / 账套），不含任何密钥。

    2026-10-19：登录页的服务器与账套不再从 portal_config.json 预置（那会把真实账套
    渲染进公网 HTML），改为按 `?c=公司码` 从租户记录取——知道链接的员工自动带出，
    陌生人打开是空的。
    """
    code = str(request.args.get("c") or "").strip().lower()
    if not code:
        return jsonify({"ok": True, "tenant": None}), 200
    try:
        t = store.get_tenant(code)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("[tenant-info] %r\n" % (exc,))
        return jsonify({"ok": True, "tenant": None}), 200
    if not t or str(t.get("status") or "active") != "active":
        return jsonify({"ok": True, "tenant": None}), 200
    return jsonify({"ok": True, "tenant": {
        "code": t["code"], "name": t["name"],
        "server": t["server"], "acct": t["acct"]}}), 200


@app.route("/")
def index():
    # 本地模式会话仅由 bootstrap 创建；页面导航不应泄漏无人使用的匿名会话。
    token, s = _get_session(create=not LOCAL_MODE)
    csrf = secrets.token_urlsafe(24)
    html = render_template("portal.html",
                           logged_in=bool(s and s.get("client")),
                           default_server=CFG.get("server_url", ""),
                           default_acct=CFG.get("acct_name", ""),
                           max_bills=CFG.get("max_bills_for_detail", 400),
                           version=str(CFG.get("version") or DEFAULT_CONFIG["version"]),
                           login_name=(s or {}).get("info", {}).get("username", ""),
                           local_mode=LOCAL_MODE)
    resp = make_response(html)
    if not LOCAL_MODE:
        secure = request.is_secure
        resp.set_cookie("portal_sid", token, httponly=True, samesite="Lax", secure=secure,
                        max_age=CFG["session_max_hours"] * 3600, path="/")
        resp.set_cookie("portal_csrf", csrf, httponly=False, samesite="Lax", secure=secure,
                        max_age=CFG["session_max_hours"] * 3600, path="/")
    return resp


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


# ---------------------------------------------------------------- 接口
@app.post("/api/datacenters")
def api_datacenters():
    _check_csrf()
    body = request.get_json(silent=True) or {}
    try:
        base = _validate_base_url(body.get("server_url", ""))
        centers = KingdeeClient.list_datacenters(base)
        return jsonify({"ok": True, "datacenters": centers})
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200


@app.post("/api/login")
def api_login():
    _check_csrf()
    ip = request.remote_addr or "-"
    if not _login_rate_ok(ip):
        return jsonify({"ok": False, "error": {
            "stage": "安全", "message": "登录尝试过于频繁",
            "hint": "金蝶账号连续错密码会锁号，已限流。请稍后再试并仔细核对密码。"}}), 200
    body = request.get_json(silent=True) or {}
    server = (body.get("server_url") or CFG.get("server_url") or "").strip()
    acct = (body.get("acct") or CFG.get("acct_name") or "").strip()
    user = (body.get("username") or "").strip()
    pwd = body.get("password") or ""
    company = str(body.get("company") or "").strip()
    if LOCAL_MODE and len(company) < 2:
        return _err("表单", "请填写公司名称（至少 2 个字）")
    # ★ 租户：员工从"唯一链接"（?c=xxx）进来时带公司码。
    #   服务器地址**以租户配置为准**，员工不用填、也不该能改。
    code = "" if LOCAL_MODE else str(body.get("code") or "").strip().lower()
    tenant = None
    if code:
        try:
            tenant = store.get_tenant(code)
        except Exception:                                 # noqa: BLE001
            tenant = None
        # ⚠️ 防枚举：公司码不存在时**不能说"公司码不存在"** —— 统一成和密码错一样的话，
        #    否则外面可以一个个试出"哪些公司开通了"。
        if not tenant:
            try:
                store.log_login(code, user, False, "公司码不存在")
            except Exception:                             # noqa: BLE001
                pass
            return jsonify({"ok": False, "error": {
                "stage": "登录", "message": "公司码或账号密码不正确"}}), 200
        if (tenant.get("status") or "active") != "active":
            st = str(tenant.get("status") or "").lower()
            if st == "pending":
                return jsonify({"ok": False, "error": {
                    "stage": "公司", "message": "资料已收到，正在开通审核",
                    "hint": "审核通过后即可登录，通常几小时内。着急可以联系发布者。"}}), 200
            return jsonify({"ok": False, "error": {
                "stage": "公司", "message": "该公司已暂停使用，请联系管理员"}}), 200
        if tenant.get("server"):
            server = tenant["server"]
    # ★ 2026-10-19：配置文件不再预置账套（防真实账套渲染进公网 HTML）。
    #   多账套环境下租户记录若没写账套，先用**该租户上次成功登录的账套**兜底，
    #   避免清空配置后员工直接登不进去；再不行才走自动解析。
    if code and not acct:
        try:
            last = store.latest_creds_for_tenant(code)
            if last:
                acct = str(last.get("acctid") or "").strip()
        except Exception:                                 # noqa: BLE001
            pass
    # ★ 租户模式：账套由系统解析，员工不用填（连接设置整块从界面上删掉了）
    if (code or LOCAL_MODE) and not acct and server:
        try:
            acct = KingdeeClient(_validate_base_url(server), per_minute=60).resolve_acctid() or ""
        except KdError as exc:
            return jsonify({"ok": False, "error": {
                "stage": "账套", "message": "无法自动确定账套：%s" % (exc.message or ""),
                "hint": "该环境可能有多个账套，请在连接设置中选择或填写账套" if LOCAL_MODE else "该环境可能有多个账套，请联系发布者在本公司配置里指定"}}), 200
        if not acct:
            return jsonify({"ok": False, "error": {
                "stage": "账套", "message": "该服务器返回了多个账套，无法自动选择",
                "hint": "请在连接设置中选择或填写账套" if LOCAL_MODE else "请联系发布者在本公司配置里指定账套"}}), 200
    if not server or not user or not pwd or not acct:
        return jsonify({"ok": False, "error": {
            "stage": "表单",
            "message": "请填写金蝶服务器地址、账套、账号、密码",
            "hint": "服务器地址与账套因单位而异（例如 https://某单位.ik3cloud.com/ 与账套名称）；"
                    "账号密码是你在该金蝶环境里的登录凭据。"}}), 200
    try:
        base = _validate_base_url(server)
        client = KingdeeClient(base, acctid=acct if acct.isdigit() else "",
                               acct_name="" if acct.isdigit() else acct,
                               username=user, password=pwd,
                               per_minute=int(CFG.get("api_calls_per_minute", 420)))
        client.login()
    except KdError as exc:
        try:
            if not LOCAL_MODE:
                store.log_login(code, user, False, exc.message or "登录失败")
        except Exception:                                 # noqa: BLE001
            pass
        return jsonify({"ok": False, "error": exc.to_dict()}), 200

    if LOCAL_MODE:
        # 本地公司记录只在用户成功登录后建立；同一服务器和账套共用本地配置。
        identity = (base.rstrip('/').lower() + '\n' + acct).encode('utf-8')
        code = 'local-' + hashlib.sha256(identity).hexdigest()[:24]
        try:
            existing = store.get_tenant(code)
            if not existing:
                store.upsert_tenant(code, company, base, acct, status='active')
        except Exception:
            return _err('本地配置', '无法保存公司连接信息，请检查本地数据目录权限')
    token, s = _get_session(create=True)
    if LOCAL_MODE:
        # 登录成功时轮换匿名会话 ID，防止会话固定；CSRF 保留供本页面使用。
        with _lock:
            old_token = token
            token = secrets.token_urlsafe(32)
            _sessions.pop(old_token, None)
            _sessions[token] = s
    s["client"] = client
    # ⚠️ 必须写进**内存会话**：数据库里存了不够 —— `_tenant_for_session()` 读的是 s["tenant"]，
    #    漏了这一行就会出现"已用专属链接登录，却说没有绑定公司"。
    s["tenant"] = code
    s["info"] = {"username": user, "company": company if LOCAL_MODE else "", "server": base, "acct_display": acct,
                 "login_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    # 留着凭据（**加密落盘 + 内存各一份**），用于会话过期/容器重启后自动重登 —— 见 _relogin()
    s["creds"] = {"base_url": base,
                  "acctid": acct if acct.isdigit() else "",
                  "acct_name": "" if acct.isdigit() else acct,
                  "username": user, "password": pwd}
    try:
        if not LOCAL_MODE:
            store.save_session(token, user, code, s["info"], s["creds"])
        if not LOCAL_MODE:
            store.log_login(code, user, True)      # 公网运营统计；本地版不做使用统计
    except Exception as exc:                              # noqa: BLE001
        sys.stderr.write("[store] 会话落盘失败（不影响本次登录）：%r\n" % (exc,))
    s["cache"] = {}
    s["orgs"] = None
    # 立刻开始自动发现组织（不阻塞登录返回）
    job = Job("发现组织", s).run(lambda j: _job_discover_orgs(s, j))
    result = {"ok": True, "login": s["info"], "org_job": job.id}
    if LOCAL_MODE:
        result["sid"] = token
    return jsonify(result)


@app.route("/api/bootstrap")
def api_bootstrap():
    """★ 内嵌模式（iframe）的会话接力：跨站 iframe 里第三方 cookie（Lax）被
    浏览器丢弃，页面 JS 读不到 portal_csrf。该端点让前端随时重新拿回
    当前 sid + 一个新的 csrf，存进门户自己域的 localStorage，之后每个
    请求经 X-Portal-Sid / X-CSRF-Token 头带上。同源直接访问时前端优先
    走 cookie，该端点只是多一条可靠通路，无副作用。"""
    token, s = _get_session()
    if s is None:
        # 无会话（如直接访问）：按"新建会话"处理，与页面 / 的 create=True 一致
        token, s = _get_session(create=True)
        if s is None:
            return jsonify({"ok": False, "error": {"stage": "会话", "message": "会话无效"}}), 200
    if LOCAL_MODE:
        with _lock:
            csrf = s.get("csrf") or secrets.token_urlsafe(24)
            s["csrf"] = csrf
    else:
        csrf = (request.cookies.get("portal_csrf") or "").strip()
        if not csrf:
            csrf = secrets.token_urlsafe(24)
            with _lock:
                s["csrf"] = csrf
    return jsonify({"ok": True, "sid": token, "csrf": csrf})


@app.post("/api/logout")
def api_logout():
    _check_csrf()
    token = (request.headers.get("X-Portal-Sid") or "").strip() or request.cookies.get("portal_sid", "")
    with _lock:
        session = _sessions.pop(token, None)
        if session is not None:
            for jid, job in list(_jobs.items()):
                if job.owner is session:
                    _jobs.pop(jid, None)
    if not LOCAL_MODE and token:
        store.drop_session(token)
    resp = jsonify({"ok": True})
    resp.delete_cookie("portal_sid", path="/")
    return resp


@app.get("/api/me")
def api_me():
    try:
        s = _require_session()
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    return jsonify({"ok": True, "login": s["info"], "orgs": s.get("orgs"),
                    "org_trace": s.get("org_trace"), "notes": s.get("notes", [])})


@app.get("/api/job/<jid>")
def api_job(jid):
    with _lock:
        job = _jobs.get(jid)
    try:
        current = _require_session()
    except KdError:
        current = None
    if current is None or job is None or job.owner is not current:
        return jsonify({"ok": False, "error": {"stage": "任务", "message": "任务不存在或已过期"}}), 200
    _cleanup()
    return jsonify({"ok": True, "job": job.snapshot()})


@app.post("/api/orgs/rediscover")
def api_orgs_rediscover():
    _check_csrf()
    try:
        s = _require_session()
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    job = Job("重新发现组织", s).run(lambda j: _job_discover_orgs(s, j, force=True))
    return jsonify({"ok": True, "job": job.id})


@app.post("/api/dashboard/expense")
def api_expense():
    _check_csrf()
    try:
        s = _require_session()
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    body = request.get_json(silent=True) or {}
    orgs = [str(x) for x in (body.get("orgs") or []) if str(x).strip()]
    force = bool(body.get("force"))
    key = "expense:" + ",".join(sorted(orgs))
    cached = _cache_get(s, key, force)
    if cached:
        return jsonify({"ok": True, "job": {"id": "cache", "status": "done",
                                            "step": "来自缓存", "pct": 100,
                                            "result": cached, "cached": True}})
    job = Job("费用报销看板", s).run(lambda j: _job_expense(s, j, orgs, key))
    return jsonify({"ok": True, "job": job.snapshot()})


@app.post("/api/dashboard/mgmt")
def api_mgmt():
    _check_csrf()
    try:
        s = _require_session()
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    body = request.get_json(silent=True) or {}
    orgs = [str(x) for x in (body.get("orgs") or []) if str(x).strip()]
    year = body.get("year") or datetime.now().year
    month = body.get("month") or datetime.now().month
    force = bool(body.get("force"))
    key = f"mgmt:{','.join(sorted(orgs))}:{year}-{month}"
    cached = _cache_get(s, key, force)
    if cached:
        return jsonify({"ok": True, "job": {"id": "cache", "status": "done",
                                            "step": "来自缓存", "pct": 100,
                                            "result": cached, "cached": True}})
    job = Job("管理会计看板", s).run(lambda j: _job_mgmt(s, j, orgs, year, month, key))
    return jsonify({"ok": True, "job": job.snapshot()})


@app.post("/api/dashboard/mgmt-full")
def api_mgmt_full():
    """管理会计全貌看板：主页指标卡片 + 明细分页（12 源，独立降级）。"""
    _check_csrf()
    try:
        s = _require_session()
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    body = request.get_json(silent=True) or {}
    orgs = [str(x) for x in (body.get("orgs") or []) if str(x).strip()]
    year = body.get("year") or datetime.now().year
    month = body.get("month") or datetime.now().month
    force = bool(body.get("force"))
    phase = str(body.get("phase") or "all")
    if phase not in ("all", "overview", "capital", "detail"):
        return _err("无效的加载阶段", "参数")
    key = f"mgmtfull:{','.join(sorted(orgs))}:{year}-{month}:{phase}"
    cached = _cache_get(s, key, force)
    if cached:
        return jsonify({"ok": True, "job": {"id": "cache", "status": "done",
                                            "step": "来自缓存", "pct": 100,
                                            "result": cached, "cached": True}})
    job = Job("管理会计全貌看板", s).run(lambda j: _job_mgmt_full(s, j, orgs, year, month, key, phase))
    return jsonify({"ok": True, "job": job.snapshot()})


@app.post("/api/dashboard/sales")
def api_sales():
    """销售出库看板：开票跟踪（出库/应收/开票/结算四联动）。"""
    _check_csrf()
    try:
        s = _require_session()
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    body = request.get_json(silent=True) or {}
    orgs = [str(x) for x in (body.get("orgs") or []) if str(x).strip()]
    start = str(body.get("start") or "").strip()
    end = str(body.get("end") or "").strip()
    force = bool(body.get("force"))
    key = f"sales:{','.join(sorted(orgs))}:{start}:{end}"
    cached = _cache_get(s, key, force)
    if cached:
        return jsonify({"ok": True, "job": {"id": "cache", "status": "done",
                                            "step": "来自缓存", "pct": 100,
                                            "result": cached, "cached": True}})
    job = Job("销售出库看板", s).run(lambda j: _job_sales(s, j, orgs, start, end, key))
    return jsonify({"ok": True, "job": job.snapshot()})


@app.post("/api/dashboard/cash")
def api_cash():
    """现金流预测看板：实际头寸 + 12 周前瞻 + 穿底 + 风险榜。"""
    _check_csrf()
    try:
        s = _require_session()
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    body = request.get_json(silent=True) or {}
    orgs = [str(x) for x in (body.get("orgs") or []) if str(x).strip()]
    force = bool(body.get("force"))
    key = "cash:" + ",".join(sorted(orgs))
    cached = _cache_get(s, key, force)
    if cached:
        return jsonify({"ok": True, "job": {"id": "cache", "status": "done",
                                            "step": "来自缓存", "pct": 100,
                                            "result": cached, "cached": True}})
    job = Job("现金流预测看板", s).run(lambda j: _job_cash(s, j, orgs, key))
    return jsonify({"ok": True, "job": job.snapshot()})


def _warmup_token():
    """预热接口的口令：只认环境变量 PORTAL_WARMUP_TOKEN（不进配置文件、不进发布包）。

    后台线程不依赖它（内部直接调用 _run_warmup）；它是给运维手动触发用的，
    没配就等于禁用外部触发。
    """
    return str(os.environ.get("PORTAL_WARMUP_TOKEN") or "").strip()


def _run_warmup(sess, views):
    """用会话凭据预热指定看板，写入进程内存缓存（**不写任何文件**）。

    返回 (out, error)：out = {view: {"ok","seconds"[,"error"]}}。
    """
    creds = sess["creds"]
    client = KingdeeClient(creds["base_url"], acctid=creds.get("acctid") or "",
                           acct_name=creds.get("acct_name") or "",
                           username=creds["username"], password=creds["password"],
                           per_minute=int(CFG.get("api_calls_per_minute", 420)))
    client.login()
    orgs = []
    try:
        orgs = client.discover_orgs().get("orgs") or []
    except Exception:  # noqa: BLE001
        pass
    org_nums = [str(o.get("number")) for o in orgs if str(o.get("number") or "").strip()]
    fake = {"tenant": sess["tenant"]}
    out = {}
    for v in views:
        t0 = time.time()
        try:
            if v == "sales":
                data = metrics_sales.build(client, org_numbers=org_nums)
                key = "sales:" + ",".join(sorted(org_nums)) + "::"
            elif v == "expense":
                data = metrics_expense.build(
                    client, org_numbers=org_nums,
                    max_bills=int(CFG.get("max_bills_for_detail", 400)))
                key = "expense:" + ",".join(sorted(org_nums))
            elif v == "cash":
                data = metrics_cashflow.build(client, orgs=orgs, org_numbers=org_nums)
                key = "cash:" + ",".join(sorted(org_nums))
            else:  # mgmt
                now = datetime.now()
                data = metrics_mgmt_full.build(client, orgs=orgs, org_numbers=org_nums,
                                               year=now.year, period=now.month,
                                               scheme_id=CFG.get("scheme_id"))
                key = "mgmtfull:%s:%s-%s" % (",".join(sorted(org_nums)), now.year, now.month)
            data["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _cache_put(fake, key, data)
            out[v] = {"ok": True, "seconds": round(time.time() - t0, 1)}
        except Exception as exc:  # noqa: BLE001
            out[v] = {"ok": False, "seconds": round(time.time() - t0, 1),
                      "error": str(getattr(exc, "message", exc))[:140]}
    return out, ""


@app.get("/api/warmup")
@app.post("/api/warmup")
def api_warmup():
    """手动触发内存预热（定时任务/运维用）。鉴权：portal_config.json 的 warmup_token。"""
    body = request.get_json(silent=True) or {}
    token = (request.args.get("token") or body.get("token")
             or request.headers.get("X-Warmup-Token") or "").strip()
    expect = _warmup_token()
    if not expect or token != expect:
        return jsonify({"ok": False, "error": "预热未开启或 token 不正确"}), 403
    want = str(request.args.get("views") or body.get("views") or "sales,expense")
    views = [v.strip() for v in want.split(",")
             if v.strip() in ("sales", "expense", "cash", "mgmt")]
    sess = store.latest_session_creds()
    if not sess:
        return jsonify({"ok": False,
                        "error": "没有可复用的登录会话（需要先有一次员工登录）"}), 200
    try:
        out, err = _run_warmup(sess, views)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": "预热登录失败：%s"
                        % str(getattr(exc, "message", exc))[:120]}), 200
    return jsonify({"ok": True, "tenant": sess["tenant"], "views": out,
                    "cache_size": len(_GLOBAL_CACHE)})


@app.get("/api/warmup/status")
def api_warmup_status():
    """只看不改：当前进程内存缓存有哪些看板、多久前算的。"""
    body = request.get_json(silent=True) or {}
    token = (request.args.get("token") or body.get("token")
             or request.headers.get("X-Warmup-Token") or "").strip()
    expect = _warmup_token()
    if not expect or token != expect:
        return jsonify({"ok": False, "error": "未开启或 token 不正确"}), 403
    now = _now()
    items = []
    with _GLOBAL_LOCK:
        for k, v in sorted(_GLOBAL_CACHE.items(), key=lambda kv: -kv[1]["at"]):
            items.append({"key": k, "age_seconds": round(now - v["at"])})
    return jsonify({"ok": True, "cache_size": len(items),
                    "ttl_minutes": CFG.get("cache_ttl_minutes"), "items": items})


def _warmup_loop():
    """后台预热线程：每 N 分钟把常用看板算好放内存（容器活着时用户随时秒开）。

    ⚠️ 只写进程内存，不落盘；容器重启后需要一次员工登录才能恢复（凭据在会话里）。
    """
    time.sleep(30)                       # 等服务先起来
    views = [v.strip() for v in str(CFG.get("warmup_views") or "sales,expense").split(",")
             if v.strip() in ("sales", "expense", "cash", "mgmt")]
    interval = max(5, int(CFG.get("warmup_interval_minutes", 20))) * 60
    done = 0
    while True:
        try:
            sess = store.latest_session_creds()
            if sess:
                _run_warmup(sess, views)
                done += 1
                print("[warmup] 已预热 %s（内存，不落盘·第 %d 次）" % (",".join(views), done),
                      flush=True)
            elif done == 0:
                # 启动阶段还没有员工登录过：60 秒后重试（前 3 轮密集重试，之后按间隔）
                print("[warmup] 暂无可用登录会话，等首次员工登录后再预热", flush=True)
        except Exception as exc:  # noqa: BLE001
            print("[warmup] 跳过：%s" % str(exc)[:100], flush=True)
        time.sleep(60 if done < 3 else interval)


# ---------------------------------------------------------------- 缓存
# 仅使用服务器/当前页面内存缓存，按租户、账号和会话隔离；不在浏览器持久保存。
_GLOBAL_CACHE = {}
_GLOBAL_LOCK = threading.Lock()
_GLOBAL_CACHE_MAX = 32


def _ck(s, key):
    """Same-tenant employees and separate sessions never share dashboard results."""
    return f"v156::{s.get('tenant') or '-'}::{(s.get('info') or {}).get('username', '')}::{s.get('cache_identity', id(s))}::{key}"


def _cache_get(s, key, force=False):
    if force:
        return None
    ttl = CFG["cache_ttl_minutes"] * 60
    ck = _ck(s, key)
    with _GLOBAL_LOCK:
        g = _GLOBAL_CACHE.get(ck)
        if g and _now() - g["at"] <= ttl:
            return g["data"]
        if g:
            _GLOBAL_CACHE.pop(ck, None)
    item = (s.get("cache") or {}).get(key)
    if not item:
        return None
    if _now() - item["at"] > ttl:
        return None
    return item["data"]


def _cache_put(s, key, data):
    ck = _ck(s, key)
    with _GLOBAL_LOCK:
        _GLOBAL_CACHE[ck] = {"at": _now(), "data": data}
        if len(_GLOBAL_CACHE) > _GLOBAL_CACHE_MAX:
            oldest = min(_GLOBAL_CACHE, key=lambda k: _GLOBAL_CACHE[k]["at"])
            _GLOBAL_CACHE.pop(oldest, None)
    s.setdefault("cache", {})[key] = {"at": _now(), "data": data}


# ---------------------------------------------------------------- 后台任务实现
def _job_discover_orgs(s, job, force=False):
    client = s["client"]
    job.progress("正在读取组织基础资料…")
    if s.get("orgs") and not force:
        job.done({"orgs": s["orgs"], "trace": s.get("org_trace"),
                  "notes": s.get("notes", []), "cached": True})
        return
    res = client.discover_orgs()
    s["orgs"] = res["orgs"]
    s["org_trace"] = res["trace"]
    s["notes"] = res.get("notes", [])
    job.done(res)


def _job_expense(s, job, orgs, key):
    client = s["client"]
    job.progress("开始取数…", 2)

    def progress(text, pct=None):
        job.progress(text, pct)

    data = metrics_expense.build(client, org_numbers=orgs,
                                max_bills=int(CFG.get("max_bills_for_detail", 400)),
                                progress=progress)
    data["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["scope"] = {"orgs": orgs or ["（全部有权组织）"],
                     "operator": s["info"].get("username", ""),
                     "max_bills": int(CFG.get("max_bills_for_detail", 800))}
    data["api_usage"] = {"calls": client.api_calls, "rate_retries": client.rate_retries,
                         "per_minute_limit": client.limiter.per_minute,
                         "waited_seconds": round(client.limiter.waited, 1)}
    if orgs:
        resolved = {o.get("number"): o.get("name") for o in (s.get("orgs") or [])}
        data["scope"]["org_names"] = [f"{n} {resolved.get(n, '')}".strip() for n in orgs]
    _cache_put(s, key, data)
    job.done(data)


def _job_mgmt(s, job, orgs, year, month, key):
    client = s["client"]
    job.progress("开始取数…", 2)

    def progress(text, pct=None):
        job.progress(text, pct)

    if not s.get("orgs"):
        progress("组织尚未发现，先自动发现组织")
        try:
            res = client.discover_orgs()
            s["orgs"] = res["orgs"]
            s["org_trace"] = res["trace"]
            s["notes"] = res.get("notes", [])
        except Exception:  # noqa: BLE001
            pass
    data = metrics_mgmt.build(client, orgs=s.get("orgs") or [], year=year, period=month,
                              scheme_id=CFG.get("scheme_id"),
                              max_books=int(CFG.get("max_books", 12)), progress=progress,
                              org_filter=orgs)
    data["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["scope"] = {"orgs": orgs or ["（全部有权组织）"],
                     "operator": s["info"].get("username", "")}
    data["api_usage"] = {"calls": client.api_calls, "rate_retries": client.rate_retries,
                         "per_minute_limit": client.limiter.per_minute,
                         "waited_seconds": round(client.limiter.waited, 1)}
    _cache_put(s, key, data)
    job.done(data)


def _ensure_orgs(s, progress):
    if not s.get("orgs"):
        progress("组织尚未发现，先自动发现组织")
        try:
            res = s["client"].discover_orgs()
            s["orgs"] = res["orgs"]
            s["org_trace"] = res["trace"]
            s["notes"] = res.get("notes", [])
        except Exception:  # noqa: BLE001
            pass
    return s.get("orgs") or []


def _finish(s, job, key, data, orgs, name):
    data["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["scope"] = {"orgs": orgs or ["（全部有权组织）"],
                     "operator": s["info"].get("username", "")}
    c = s["client"]
    data["api_usage"] = {"calls": c.api_calls, "rate_retries": c.rate_retries,
                         "per_minute_limit": c.limiter.per_minute,
                         "waited_seconds": round(c.limiter.waited, 1)}
    _cache_put(s, key, data)
    job.done(data)


def _job_mgmt_full(s, job, orgs, year, month, key, phase="all"):
    client = s["client"]
    job.progress("开始取数…", 2)

    def progress(text, pct=None):
        job.progress(text, pct)

    orgs_all = _ensure_orgs(s, progress)
    data = metrics_mgmt_full.build(client, orgs=orgs_all, org_numbers=orgs,
                                   year=year, period=month,
                                   scheme_id=CFG.get("scheme_id"), progress=progress, phase=phase)
    _finish(s, job, key, data, orgs, "财务健康与风险")


def _job_sales(s, job, orgs, start, end, key):
    client = s["client"]
    job.progress("开始取数…", 2)

    def progress(text, pct=None):
        job.progress(text, pct)

    orgs_all = _ensure_orgs(s, progress)
    data = metrics_sales.build(client, org_numbers=orgs or None,
                               start_date=start or None, end_date=end or None,
                               progress=progress)
    _finish(s, job, key, data, orgs, "销售出库看板")


def _job_cash(s, job, orgs, key):
    client = s["client"]
    job.progress("开始取数…", 2)

    def progress(text, pct=None):
        job.progress(text, pct)

    orgs_all = _ensure_orgs(s, progress)
    data = metrics_cashflow.build(client, orgs=orgs_all, org_numbers=orgs,
                                  progress=progress)
    _finish(s, job, key, data, orgs, "现金流预测看板")


# 统一的 KdError → JSON。
# 必要性：各 POST 端点的 `_check_csrf()` 写在 try 之外，CSRF 失败会冒到 Flask 变成 **500 HTML**，
# 前端解析不了只能显示"连接失败"。挂上这个 handler 后一律回标准错误结构。
@app.errorhandler(KdError)
def _kd_error_handler(exc):
    code = 200
    return jsonify({"ok": False, "error": exc.to_dict()}), code


# ================================================================ 写操作：报销办理
# ⚠️ 这一区是门户里**唯一的写入口**，权限模型与只读看板不同：
#    · 只读看板：用「登录者本人的金蝶会话」取数，取不到就显示空（不换账号兜底）
#    · 写操作：按《金蝶Skill架构约定》复用 kingdee-expense-flow 配置的账号
#      （写收票信息通常需要收票池权限，员工个人账号往往没有）
#    因此默认关闭，且每个动作都落审计日志。打开方式：portal_config.json → enable_write_ops

_PORTAL_DIR = os.path.dirname(os.path.abspath(__file__))


def _bind_actor(s) -> str:
    """把「本次请求的操作者」绑定为**登录者本人**。

    写操作（下推/建收票单/改金额/提交）因此跑在这位员工自己的金蝶身份上：
      · 他只能动自己有权动的单据 —— 权限由金蝶裁决，门户不额外放权；
      · 服务端**不持有任何人的账号**，凭据取自这次登录的内存会话；
      · 与"发票云 API Key"是两回事，后者是公司级接口密钥，单独在配置页里管。

    返回操作者账号，供审计日志用。
    """
    cli = s.get("client")
    creds = None
    if cli is not None:
        creds = {
            "base_url": getattr(cli, "base_url", "") or "",
            "acctid": getattr(cli, "acctid", "") or "",
            "acct_name": getattr(cli, "acct_name", "") or "",
            "username": getattr(cli, "_username", "") or "",
            "password": getattr(cli, "_password", "") or "",
            "lcid": 2052,
        }
    if LOCAL_MODE and (not creds or not creds.get("username") or not creds.get("password")):
        raise KdError("当前登录身份不可用于办理业务", "会话", "请重新登录；不会使用其他账号代办")
    portal_actions.use_credentials(creds, cache=s)
    return (creds or {}).get("username", "") or str((s.get("info") or {}).get("username") or "")


@app.before_request
def _reset_actor():
    """每个请求先清空"操作者身份"。

    ⚠️ 必须做：Flask 的线程是复用的，不清空的话上一个请求的身份会被下一个请求继承（串号）。
    """
    if request.path.startswith("/api/reimburse"):
        portal_actions.use_credentials(None)
    if request.method == "POST" and request.path in (
        "/api/reimburse/upload", "/api/reimburse/piazzone-config", "/api/reimburse/piazzone-test"
    ):
        _piazzone_request_lock.acquire()
        g.piazzone_lock_held = True


@app.teardown_request
def _clear_request_actor(_error):
    # Werkzeug worker threads may be reused for another user's request.
    portal_actions.use_credentials(None)
    if getattr(g, "piazzone_lock_held", False):
        g.piazzone_lock_held = False
        _piazzone_request_lock.release()


def _write_gate() -> str:
    """写操作门禁。

    ⚠️ 与最初版本的关键区别：**不再要求环境变量**，也不再依赖 skill 的"服务账号"。
    写操作现在以「登录者本人」的身份执行（见 `_bind_actor`），所以把开关交给配置即可：
    谁能登录、谁就能用，但他只能用他自己在金蝶里本来就有的权限。
    """
    if not LOCAL_MODE and not bool(CFG.get("enable_write_ops")):
        raise KdError("写操作未开启", "权限",
                      "在 portal_config.json 里把 \"enable_write_ops\" 设为 true，重启后可用")
    skill = portal_actions.find_skill_dir(str(CFG.get("expense_skill_dir") or ""))
    if not skill:
        raise KdError("缺少报销模块", "依赖",
                      "需要随门户发布的 vendor/helpers（部署包已带），"
                      "或本机安装的 kingdee-expense-flow")
    return skill


def _audit(action: str, detail: dict, actor: str = "") -> None:
    portal_actions.audit(os.path.join(_PORTAL_DIR, str(CFG.get("audit_log") or "portal_audit.log")),
                         action, detail, actor)


def _err(stage: str, message: str, hint: str = ""):
    return jsonify({"ok": False, "error": {"stage": stage, "message": message, "hint": hint}}), 200


@app.get("/api/reimburse/status")
def api_reimburse_status():
    if LOCAL_MODE:
        try:
            _require_session()
        except KdError as exc:
            return jsonify({"ok": False, "error": exc.to_dict()}), 200
    skill = portal_actions.find_skill_dir(str(CFG.get("expense_skill_dir") or ""))
    # 诊断字段：用来判断"会话丢失"到底是因为我重新部署，还是**沙箱自己重启了**。
    # uptime_sec 在没部署的情况下变小/归零 → 说明容器被平台回收重启，会话（存在内存里）随之清空。
    return jsonify({"ok": True, "enabled": LOCAL_MODE or bool(CFG.get("enable_write_ops")),
                    "skill_dir": skill, "audit_log": str(CFG.get("audit_log") or ""),
                    "pid": os.getpid(),
                    "uptime_sec": int(time.time() - _BOOT_AT),
                    "sessions": len(_sessions),
                    "cred_mode": "memory" if LOCAL_MODE else store.credential_mode(),
                    "store_sessions": 0 if LOCAL_MODE else store.count_sessions(),
                    "last_relogin_error": "" if LOCAL_MODE else store.get_setting("last_relogin_error", "")}), 200


@app.get("/api/reimburse/requests")
def api_reimburse_requests():
    """可下推的申请单（只读，但仍属写操作模块 → 同样受开关约束）。"""
    try:
        _write_gate()
        s = _require_session()
        _bind_actor(s)
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    try:
        orgs = [str(x) for x in (request.args.getlist("org") or []) if str(x).strip()]
        rows = portal_actions.list_pushable(str(CFG.get("expense_skill_dir") or ""),
                                            orgs=orgs or None)
        return jsonify({"ok": True, "rows": rows,
                        "operator": s["info"].get("username", "")}), 200
    except Exception as exc:                              # noqa: BLE001
        return _err("读取申请单", repr(exc)[:300])


@app.get("/api/reimburse/bills")
def api_reimburse_bills():
    """报销单列表 + 状态（提交后看进度用）。"""
    try:
        _write_gate()
        _bind_actor(_require_session())
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    try:
        orgs = [str(x) for x in (request.args.getlist("org") or []) if str(x).strip()]
        rows = portal_actions.list_bills(
            str(CFG.get("expense_skill_dir") or ""), orgs=orgs or None,
            proposer=(request.args.get("proposer") or "").strip(),
            only_pending=request.args.get("pending") in ("1", "true", "True"))
        return jsonify({"ok": True, "rows": rows}), 200
    except Exception as exc:                              # noqa: BLE001
        return _err("读取报销单列表", repr(exc)[:300])


@app.get("/api/reimburse/bill/<fid>")
def api_reimburse_bill(fid):
    try:
        _write_gate()
        _bind_actor(_require_session())
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    try:
        travel = request.args.get("travel") in ("1", "true", "True")
        return jsonify({"ok": True,
                        "bill": portal_actions.get_bill(str(CFG.get("expense_skill_dir") or ""),
                                                        fid, travel)}), 200
    except Exception as exc:                              # noqa: BLE001
        return _err("读取报销单", repr(exc)[:300])


@app.get("/api/reimburse/request/<fid>")
def api_reimburse_request_detail(fid):
    """查看费用/出差申请单内容（只读；申请单不提供编辑，审批依据要稳定）。"""
    try:
        _write_gate()
        _bind_actor(_require_session())
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    try:
        travel = request.args.get("travel") in ("1", "true", "True")
        return jsonify({"ok": True,
                        "request": portal_actions.request_detail(
                            str(CFG.get("expense_skill_dir") or ""), fid, travel)}), 200
    except Exception as exc:                              # noqa: BLE001
        return _err("读取申请单", repr(exc)[:300])


@app.post("/api/reimburse/delete")
def api_reimburse_delete():
    """删除报销单（仅 Z/A/D 状态）。重复下推的草稿单用它清掉。"""
    _check_csrf()
    actor = ""
    try:
        _write_gate()
        actor = _bind_actor(_require_session())
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    body = request.get_json(silent=True) or {}
    fid = str(body.get("bill_fid") or "").strip()
    if not fid:
        return _err("删除报销单", "缺少 bill_fid")
    travel = bool(body.get("is_travel"))
    try:
        res = portal_actions.delete_bill(str(CFG.get("expense_skill_dir") or ""), fid, travel)
        if res.get("ok"):
            _audit("报销-删除", res.get("audit") or {"fid": fid}, actor)
            return jsonify({"ok": True, "result": res}), 200
        return jsonify({"ok": False, "error": {
            "stage": res.get("stage") or "删除",
            "message": res.get("message") or "删除失败",
            "hint": res.get("hint") or "",
            "status": res.get("status") or ""}}), 200
    except Exception as exc:                              # noqa: BLE001
        _audit("报销-删除", {"fid": fid, "error": repr(exc)[:200]}, actor)
        return _err("删除报销单", repr(exc)[:300])


@app.post("/api/reimburse/push")
def api_reimburse_push():
    _check_csrf()
    actor = ""
    try:
        _write_gate()
        s = _require_session()
        _bind_actor(s)
        actor = s["info"].get("username", "")
        body = request.get_json(silent=True) or {}
        bill_no = str(body.get("bill_no") or body.get("fid") or "").strip()
        travel = bool(body.get("is_travel"))
        if not bill_no:
            return _err("参数", "缺少申请单单号")
        res = portal_actions.push(str(CFG.get("expense_skill_dir") or ""), bill_no, travel)
        _audit("报销-下推", {"src": bill_no, "new_fid": res.get("fid")}, actor)
        return jsonify({"ok": True, "result": res}), 200
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    except Exception as exc:                              # noqa: BLE001
        _audit("报销-下推", {"error": repr(exc)[:200]}, actor)
        return _err("下推", repr(exc)[:300])


@app.post("/api/reimburse/upload")
def api_reimburse_upload():
    """上传发票 → 建收票单（手填真流水号）→ 挂到报销单。一步到位。"""
    _check_csrf()
    actor = ""
    tmp_path = ""
    try:
        _write_gate()
        s = _require_session()
        _bind_actor(s)
        actor = s["info"].get("username", "")
        bill_fid = (request.form.get("bill_fid") or "").strip()
        # 组织不再让用户填：直接用报销单自带的组织（申请单已经带了组织）
        org_no = (request.form.get("org_no") or "").strip()
        item_name = (request.form.get("item_name") or "").strip()
        bill_no = (request.form.get("bill_no") or "").strip()
        travel = (request.form.get("is_travel") or "") in ("1", "true", "True")
        _apply_tenant_piazzone(_tenant_for_session(s))   # 上传要用【本公司】的发票云密钥
        f = request.files.get("file")
        if not bill_fid:
            return _err("参数", "缺少报销单 FID")
        if not f:
            return _err("参数", "没有收到发票文件", "请选择发票（XML/PDF 优于拍照件）后重试")
        skill = str(CFG.get("expense_skill_dir") or "")
        if not org_no:                      # 回落到报销单的组织
            try:
                org_no = str((portal_actions.get_bill(skill, bill_fid, travel) or {}).get("org_no") or "")
            except Exception:               # noqa: BLE001
                org_no = ""
        if not org_no:
            return _err("参数", "无法确定组织（报销单未带出组织且未显式提供）")
        tmpdir = os.path.join(tempfile.gettempdir(), "kingdee_portal_upl")
        os.makedirs(tmpdir, exist_ok=True)
        # ★ 2026-10-19：**判定要用原始文件名**。
        #   临时文件名为了安全会做 ASCII 化（中文全变下划线），拿它去判"是不是发票"
        #   会永远匹配不上（用户传「行程报销单」仍走发票云 → ValueError）。
        orig_name = os.path.basename(f.filename or "invoice.bin")
        safe = re.sub(r"[^0-9A-Za-z._-]", "_", orig_name)
        tmp_path = os.path.join(tmpdir, "%s_%s" % (uuid.uuid4().hex[:8], safe))
        f.save(tmp_path)
        res = portal_actions.upload_or_attach(
            str(CFG.get("expense_skill_dir") or ""), bill_fid, tmp_path, orig_name,
            org_no, item_name, bill_no, travel)
        _audit("报销-上传发票", {"bill_fid": bill_fid, "file": orig_name, "org": org_no,
                                 "route": res.get("route"), "ok": res.get("ok"),
                                 "recvin": res.get("recvin_bill_no"),
                                 "file_id": res.get("file_id"),
                                 "invoice_no": res.get("invoice_no")}, actor)
        if not res.get("ok"):
            return _err(res.get("stage") or "上传发票",
                        res.get("message") or "上传失败", res.get("hint") or "")
        return jsonify({"ok": True, "result": res}), 200
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    except Exception as exc:                              # noqa: BLE001
        _audit("报销-上传发票", {"error": repr(exc)[:200]}, actor)
        return _err("上传发票", repr(exc)[:300])
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@app.post("/api/reimburse/amount")
def api_reimburse_amount():
    _check_csrf()
    actor = ""
    try:
        _write_gate()
        s = _require_session()
        _bind_actor(s)
        actor = s["info"].get("username", "")
        body = request.get_json(silent=True) or {}
        fid = str(body.get("fid") or "").strip()
        amount = float(body.get("amount") or 0)
        travel = bool(body.get("is_travel"))
        if not fid:
            return _err("参数", "缺少报销单 FID")
        res = portal_actions.set_amount(str(CFG.get("expense_skill_dir") or ""), fid, amount, travel)
        _audit("报销-改金额", {"fid": fid, "before": res.get("before"), "after": res.get("amount")}, actor)
        return jsonify({"ok": True, "result": res}), 200
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    except Exception as exc:                              # noqa: BLE001
        _audit("报销-改金额", {"error": repr(exc)[:200]}, actor)
        return _err("调整金额", repr(exc)[:300])


@app.get("/api/reimburse/precheck/<fid>")
def api_reimburse_precheck(fid):
    try:
        _write_gate()
        _bind_actor(_require_session())
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    try:
        travel = request.args.get("travel") in ("1", "true", "True")
        return jsonify({"ok": True,
                        "check": portal_actions.precheck(str(CFG.get("expense_skill_dir") or ""),
                                                         fid, travel)}), 200
    except Exception as exc:                              # noqa: BLE001
        return _err("提交前体检", repr(exc)[:300])


@app.post("/api/reimburse/submit")
def api_reimburse_submit():
    """提交单据（进审批流）。**必须显式二次确认**：body.confirm == "SUBMIT"。"""
    _check_csrf()
    actor = ""
    try:
        _write_gate()
        s = _require_session()
        _bind_actor(s)
        actor = s["info"].get("username", "")
        body = request.get_json(silent=True) or {}
        fid = str(body.get("fid") or "").strip()
        travel = bool(body.get("is_travel"))
        if not fid:
            return _err("参数", "缺少报销单 FID")
        if str(body.get("confirm") or "") != "SUBMIT":
            return _err("二次确认", "提交会让单据进入审批流，请再次确认",
                        "前端弹确认框后再提交")
        skill = str(CFG.get("expense_skill_dir") or "")
        chk = portal_actions.precheck(skill, fid, travel)
        if not chk.get("ok"):
            _audit("报销-提交被闸门拦下", {"fid": fid, "issues": chk.get("issues")}, actor)
            return _err("提交前体检未通过",
                        "；".join(chk.get("issues") or []) or chk.get("gate_message", ""))
        res = portal_actions.submit(skill, fid, travel)
        _audit("报销-提交", {"fid": fid, "ok": res.get("ok"),
                             "before": res.get("before_status"), "after": res.get("after_status")}, actor)
        if not res.get("ok"):
            return _err("提交", res.get("message") or "提交失败")
        return jsonify({"ok": True, "result": res}), 200
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    except Exception as exc:                              # noqa: BLE001
        _audit("报销-提交", {"error": repr(exc)[:200]}, actor)
        return _err("提交", repr(exc)[:300])


def _pz_companies(t) -> list:
    """租户的发票云**多家开票主体**列表（兼容旧版单份配置）。

    存储形态：`piazzone = {"base_url": ..., "companies": [{id, ghf_mc, tax_no,
    client_id, client_secret, kingdee_orgs}], "default_id": ...}`。

    ⚠️ 旧版（2026-10-14 之前）只有单份平铺字段，读到时**自动迁移成一家公司**，
    下次保存即写回新结构（平铺字段同时清掉，避免两处口径）。
    """
    pz = (t or {}).get("piazzone") or {}
    cos = pz.get("companies")
    cos = [c for c in cos if isinstance(c, dict)] if isinstance(cos, list) else []
    if not cos and (pz.get("client_id") or pz.get("ghf_mc")):
        cos = [{"id": "c-legacy", "ghf_mc": pz.get("ghf_mc", ""),
                "tax_no": pz.get("tax_no", ""), "client_id": pz.get("client_id", ""),
                "client_secret": pz.get("client_secret", ""),
                "kingdee_orgs": ",".join((t or {}).get("orgs") or [])}]
    for c in cos:
        if not str(c.get("id") or "").strip():
            c["id"] = "c-" + secrets.token_hex(3)
        c.setdefault("kingdee_orgs", "")
    return cos


def _pz_co_from_body(body: dict, t) -> dict:
    """从请求体取一家开票主体的字段（secret 留空 = 不修改，由调用方判断）。"""
    orgs = str(body.get("kingdee_orgs") or "").strip()
    if not orgs:
        orgs = ",".join((t or {}).get("orgs") or [])   # 留空 = 本公司所有组织
    return {"ghf_mc": str(body.get("ghf_mc") or "").strip(),
            "tax_no": str(body.get("tax_no") or "").strip(),
            "client_id": str(body.get("client_id") or "").strip(),
            "client_secret": str(body.get("client_secret") or "").strip(),
            "kingdee_orgs": orgs,
            "eid": str(body.get("eid") or "").strip()}


def _orgs_from_req(sess) -> list:
    """组织清单优先顺序：请求体/查询参数（前端已发现的那份）→ 会话 → 空。"""
    raw = request.args.get("orgs")
    if not raw:
        try:
            raw = (request.get_json(silent=True) or {}).get("orgs")
        except Exception:                                 # noqa: BLE001
            raw = None
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:                                 # noqa: BLE001
            raw = None
    if isinstance(raw, list) and raw:
        return [x for x in raw if isinstance(x, dict)]
    return (sess or {}).get("orgs") or []


def _pz_payload(tenant, me: str, sess_orgs=None) -> dict:
    """给前端看的**本租户**发票云配置（不含密钥原文，只给尾 4 位）。

    ⚠️ **不再回显 base_url**：它就是发票云接口地址，几乎所有客户都用同一个默认值，
    由发布者维护在代码默认里即可，不该让公司重复填（用户 2026-10-14 拍板）。
    ⚠️ 一家租户可配**多家开票主体**（集团多公司场景）；上传时按单据组织自动选对应授权。
    """
    t = tenant or {}
    cos = _pz_companies(t)
    default_id = str((t.get("piazzone") or {}).get("default_id") or "")
    if default_id not in [c.get("id") for c in cos]:
        default_id = cos[0]["id"] if cos else ""
    admins = t.get("admin_users") or []
    mode = store.tenant_cred_mode(t.get("code", ""))
    companies = []
    for c in cos:
        companies.append({
            "id": c.get("id", ""), "ghf_mc": c.get("ghf_mc", ""),
            "tax_no": c.get("tax_no", ""), "client_id": c.get("client_id", ""),
            "kingdee_orgs": c.get("kingdee_orgs", ""),
            "has_secret": bool(c.get("client_secret")),
            "secret_tail": (c.get("client_secret") or "")[-4:],
            "is_default": c.get("id") == default_id,
        })
    # ★ 2026-10-19：配置页改成「以组织为主键」的表格，需要组织清单（自动发现的那份）
    src_orgs = sess_orgs if sess_orgs else (t.get("orgs") or [])
    orgs = []
    for o in src_orgs:
        if not isinstance(o, dict):
            continue
        no = str(o.get("number") or o.get("no") or "").strip()
        if no:
            orgs.append({"no": no, "name": str(o.get("name") or "").strip()})

    # Persisted authorizations remain visible when discovery only returns a subset.
    known = {o["no"] for o in orgs}
    for co in cos:
        for no in org_codes(co.get("kingdee_orgs")):
            if no not in known:
                orgs.append({"no": no, "name": "已保存组织"})
                known.add(no)

    # 兼容旧前端/回归脚本的单份字段（取默认那家）
    d0 = ([c for c in companies if c["is_default"]] or companies[:1] or [{}])[0]
    return {
        "tenant": t.get("code", ""), "tenant_name": t.get("name", ""),
        "orgs": orgs,
        "companies": companies,
        "revision": store.piazzone_revision(t.get("piazzone") or {}),
        "ghf_mc": d0.get("ghf_mc", ""), "tax_no": d0.get("tax_no", ""),
        "client_id": d0.get("client_id", ""),
        "has_secret": d0.get("has_secret", False),
        "secret_tail": d0.get("secret_tail", ""),
        "has_base_url": True,
        "admins": admins, "me": me,
        "can_edit": bool(me) and (LOCAL_MODE or not admins or me in admins),
        "claimable": bool(me) and not LOCAL_MODE and not admins,
        "cred_mode": "memory" if LOCAL_MODE else mode,
        "cred_mode_own": "memory" if LOCAL_MODE else (t.get("cred_mode") or "") or "inherit",
        "storage": "发票云授权保存在本机公司记录，不会自动同步到其他电脑" if LOCAL_MODE else "加密存在本公司的公司记录里（全公司共用）",
        "note": "可配多家开票主体；上传时按单据组织自动选对应授权，只配一家时直接用。",
    }


def _tenant_for_session(s):
    code = str(s.get("tenant") or "")
    if not code:
        return None
    try:
        return store.get_tenant(code)
    except Exception:                                     # noqa: BLE001
        return None


def _apply_tenant_piazzone(t) -> None:
    """把某租户的发票云配置注入到本进程（上传发票要用它）。

    支持**多家开票主体**；具体用哪家由 `pick_company_for_org(组织编码)` 在上传时决定。
    """
    # 即使公司没有授权也要清空上一次请求的主体，不能沿用其他公司的凭据。
    t = t or {}
    cos = [c for c in _pz_companies(t) if c.get("client_id")]
    pz = t.get("piazzone") or {}
    portal_actions.set_piazzone_companies(pz.get("base_url") or "https://api.piaozone.com", cos)


@app.get("/api/reimburse/piazzone-config")
def api_pz_config_get():
    """本租户的发票云配置（按公司隔离；密钥不回显）。"""
    try:
        _write_gate()
        s = _require_session()
        _bind_actor(s)
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    me = str((s.get("info") or {}).get("username") or "")
    # 组织清单：优先用前端带来的（它已经跑过发现），其次会话，再次租户记录
    return jsonify({"ok": True, "config": _pz_payload(
        _tenant_for_session(s), me, _orgs_from_req(s))}), 200


@app.post("/api/reimburse/piazzone-config")
def api_pz_config_set():
    """保存**本租户**的发票云配置。

    权限规则（用户 2026-10-13 定）：
      · 该租户**还没有管理员** → **第一个来配的人自动成为管理员**（首登认领，无需额外界面）
      · 已有管理员 → 只有管理员能改（其他人只读）
      · 发布者可在「租户管理」页改派管理员（兜底）
    密钥**留空 = 不修改**，永不回显。

    action（用户 2026-10-14 定，一家租户下可配多家开票主体）：
      · add    新增一家开票主体（自动成为默认）
      · save   保存指定 id 那家（缺省第一家；一家都没有时等价于新增）
      · delete 删除一家；默认被删则顺延到第一家
      · use    把某家设为默认
    """
    _check_csrf()
    try:
        _write_gate()
        s = _require_session()
        _bind_actor(s)
        t = _tenant_for_session(s)
        if not t:
            return _err("没有绑定公司", "公司",
                        "请用本公司专属链接（带 ?c= 的那条）登录后再配置发票云")
        me = str((s.get("info") or {}).get("username") or "")
        admins = t.get("admin_users") or []
        if not LOCAL_MODE and admins and me not in admins:
            return _err("只有本公司管理员能改发票云配置", "权限",
                        "当前管理员：%s。需要变更请联系发布者" % "、".join(admins))
        claimed = False
        body = request.get_json(silent=True) or {}
        if body.get("revision") != store.piazzone_revision(t.get("piazzone") or {}):
            return _err("配置已被更新，请重新打开此页后再保存", "配置冲突")
        action = str(body.get("action") or "save").strip().lower()
        cos = _pz_companies(t)
        pz = dict(t.get("piazzone") or {})
        changed = False
        if action == "add":
            # 增加一家开票主体（用户 2026-10-14：集团多公司，一家租户下要能加多家）
            co = _pz_co_from_body(body, t)
            co["id"] = "c-" + secrets.token_hex(3)
            if not co["client_id"]:
                return _err("请至少填 client_id", "表单",
                            "新增开票主体时，购方名称/税号/client_id 都要填")
            cos.append(co)
            pz["default_id"] = co["id"]            # 新加的这家先当默认
            changed = True
        elif action == "save":
            cid = str(body.get("id") or "").strip()
            co = None
            for x in cos:
                if x.get("id") == cid:
                    co = x
                    break
            if co is None:
                if cid and not body.get("new"):
                    return _err("要保存的主体不存在", "参数", "请刷新页面后重试")
                if not cid and not body.get("new") and cos:
                    co = cos[0]          # 旧版调用（不带 id）：存到第一家
                else:
                    # 页面上先加的空卡（new=true）→ 落库时才算真正新建
                    co = {"id": "c-" + secrets.token_hex(3)}
                    cos.append(co)
            upd = _pz_co_from_body(body, t)
            for k in ("ghf_mc", "tax_no", "client_id", "kingdee_orgs"):
                if upd[k]:
                    co[k] = upd[k]
            if upd["client_secret"]:
                co["client_secret"] = upd["client_secret"]   # 留空 = 不修改
            if not co.get("client_id"):
                return _err("请至少填 client_id", "表单")
            pz.setdefault("default_id", co["id"])
            changed = True
        elif action == "save_orgs":
            rows = body.get("rows") or []
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                return _err("配置行格式无效", "表单")
            if not any(str(row.get("client_id") or "").strip() for row in rows):
                return _err("至少填一行的 client_id", "表单")
            cos = merge_org_rows(cos, rows)
            keep = str(pz.get("default_id") or "")
            pz["default_id"] = keep if keep in [c["id"] for c in cos] else cos[0]["id"]
            changed = True
        elif action == "delete":
            cid = str(body.get("id") or "").strip()
            cos = [x for x in cos if x.get("id") != cid]
            if pz.get("default_id") == cid or not cos:
                pz["default_id"] = cos[0]["id"] if cos else ""
            changed = True
        elif action == "use":
            cid = str(body.get("id") or "").strip()
            if cid not in [x.get("id") for x in cos]:
                return _err("要设为默认的主体不存在", "参数")
            pz["default_id"] = cid
            changed = True
        else:
            return _err("不支持的操作", "参数")
        if changed:
            # 归一化写回：只留 companies/default_id/base_url，清掉旧版平铺字段
            pz["companies"] = cos
            for k in ("ghf_mc", "tax_no", "client_id", "client_secret"):
                pz.pop(k, None)
            revision = body.get("revision")
            if not revision or not store.set_tenant_piazzone(t["code"], pz, expected_revision=revision):
                return _err("配置已被更新，请重新打开此页后再保存", "配置冲突")
            if not LOCAL_MODE and not admins:
                store.add_tenant_admin(t["code"], me)
                claimed = True
        _audit("报销-发票云配置", {"tenant": t["code"], "claimed_admin": claimed,
                                   "action": action, "n": len(cos),
                                   "has_secret": any(x.get("client_secret") for x in cos)}, me)
        t2 = store.get_tenant(t["code"])
        _apply_tenant_piazzone(t2)
        try:
            _no_secret = [c.get("ghf_mc") or c.get("client_id") or "某主体"
                          for c in _pz_companies(t2) if not c.get("client_secret")]
        except Exception:                                 # noqa: BLE001
            _no_secret = []
        return jsonify({"ok": True, "config": _pz_payload(t2, me, _orgs_from_req(s)),
                        "claimed": claimed,
                        "warn": ("这些主体没填 client_secret，签名会通不过：" + "、".join(_no_secret))
                                if _no_secret else ""}), 200
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    except Exception as exc:                              # noqa: BLE001
        return _err("保存发票云配置", repr(exc)[:300])


def _pz_explain(r) -> str:
    """把发票云的返回翻成人话（403 最常见：IP 白名单 / 密钥不匹配）。"""
    if not isinstance(r, dict):
        return str(r)[:300]
    code = str(r.get("errcode") or r.get("code") or "")
    desc = str(r.get("description") or r.get("message") or r.get("errmsg") or "")
    blob = (code + " " + desc).lower()
    if code == "NETWORK" or "timeout" in blob or "ssl" in blob or "timed out" in blob:
        return ("请求超时。**最常见的原因是这张发票的购方税号/抬头与本公司不一致**——"
                "发票云对不上号的发票会做额外校验，经常拖到超时；"
                "正常发票一般几秒就回。\n"
                "请先核对：① 发票抬头/税号是不是本公司；② 单据组织与发票主体是否对应。\n"
                "原始返回：%s" % desc[:160])
    if "forbidden" in blob or code == "403":
        return ("**403 Forbidden = 本服务器的出口 IP 不在发票云白名单**（不是凭据问题）。\n"
                "本地电脑能通、云端不通，就是这个原因——发票云按 IP 放行，你办公网 IP 加过白，"
                "容器的没有。\n"
                "解决：把本服务器的出口 IP 报给发票云客服加入白名单（IP 可在「设置 → 网络」页查看）。\n"
                "原始返回：%s" % (desc[:200] or code))
    if any(k in desc for k in ("购方", "抬头", "税号", "购买方")) and \
            any(k in desc for k in ("不一致", "不符", "不匹配", "错误")):
        return ("发票信息与本公司不一致：**%s**\n"
                "请确认三件事：① 这张发票的购方是不是本公司（抬头/税号）；"
                "② 单据所属组织与发票主体是否对应（101 的发票只能传 101 的单据）；"
                "③ 若确认无误，把这张发票号发我，我去查发票云侧的授权绑定。" % desc[:160])
    if "sign" in blob:
        return "签名不对：请核对 client_secret（签名 = MD5(client_id + client_secret + 时间戳)）。原始返回：%s" % desc[:200]
    if "基础授权" in desc or "授权信息" in desc:
        return ("发票云说「基础授权信息有误」——这是**发票云侧**的校验，不是门户的问题。\n"
                "请拿下面这三项去发票云后台「企业授权」逐项核对，必须完全一致：\n"
                "① 购方企业全称（一个字都不能差，要跟营业执照/授权登记一致）\n"
                "② 购方税号\n"
                "③ client_id 是否属于这家企业的授权（别把两家的混用）\n"
                "如果三项都对，把本条的「原始返回」发给发票云实施/客服，让他们查这个 client_id 的授权状态。")
    return (desc or code or "未知错误")[:300]


@app.errorhandler(Exception)
def _handle_unexpected(exc):
    """任何未捕获异常都返回 JSON。

    2026-10-19：上传发票时后端抛异常 → 返回 500 HTML，前端 `r.json()` 解析失败 →
    用户只看到"请求超时/网络繁忙"，真实原因完全丢失。这里统一收敛成结构化错误。
    """
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    try:
        sys.stderr.write("[unhandled] %r\n" % (exc,))
    except Exception:                                     # noqa: BLE001
        pass
    return jsonify({"ok": False, "error": {
        "stage": "服务内部",
        "message": "服务内部错误：%s" % type(exc).__name__,
        "hint": str(exc)[:300] or "请把这条信息发我"}}), 200


@app.get("/api/net/probe")
def api_net_probe():
    """从服务器直连发票云，把 HTTP 状态码与响应头原样返回 —— 用于判断 403 是谁返回的。"""
    url = str(request.args.get("url") or "https://api.piaozone.com/m4/fpzs/getLinkKey")
    if "piaozone.com" not in url:
        return jsonify({"ok": False, "error": "只允许探测 piaozone.com 域名"}), 200
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(url, data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=12) as r:
            body = r.read()[:300].decode("utf-8", "replace")
            return jsonify({"ok": True, "status": r.status,
                            "headers": dict(r.headers), "body": body}), 200
    except urllib.error.HTTPError as e:
        try:
            body = e.read()[:300].decode("utf-8", "replace")
        except Exception:                                 # noqa: BLE001
            body = ""
        return jsonify({"ok": False, "status": e.code,
                        "headers": dict(e.headers or {}), "body": body}), 200
    except Exception as exc:                              # noqa: BLE001
        return jsonify({"ok": False, "error": repr(exc)[:200]}), 200


@app.get("/api/net/egress-ip")
def api_egress_ip():
    """本服务器访问外网时的出口 IP —— 发票云白名单要加的就是这个。"""
    ip, err = "", ""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            ip = urllib.request.urlopen(req, timeout=8).read().decode().strip()
            if ip:
                break
        except Exception as exc:                          # noqa: BLE001
            err = repr(exc)[:120]
    return jsonify({"ok": bool(ip), "ip": ip, "error": err}), 200


@app.post("/api/reimburse/piazzone-test")
def api_pz_test():
    """用当前配置的主体测一次发票云连通性（不发任何发票、不写金蝶）。

    目的：把「上传到一半才知道 403」提前成「点一下就看到原因」。
    """
    _check_csrf()
    try:
        _write_gate()
        s = _require_session()
        _bind_actor(s)
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    t = _tenant_for_session(s)
    if not t:
        return _err("没有绑定公司", "公司", "请用本公司专属链接登录后再测")
    cos = _pz_companies(t)
    if not cos:
        return _err("还没配开票主体", "发票云", "先在上面表格里填一行并保存")
    default_id = str((t.get("piazzone") or {}).get("default_id") or "")
    co = next((c for c in cos if c.get("id") == default_id), cos[0])
    steps = []
    try:
        portal_actions.apply_company_credentials(co)
        rl, _ee, skill = portal_actions._load(CFG.get("expense_skill_dir", ""))
        import importlib
        import sys as _sys
        _sys.modules.pop("piazzone_client", None)
        pz = importlib.import_module("piazzone_client")
        importlib.reload(pz)
        def _ok(r):
            """发票云成功时 errcode 常见为 "0" / 0 / 空，甚至只给"操作成功"文案。"""
            if not isinstance(r, dict):
                return False
            ec = str(r.get("errcode") if r.get("errcode") is not None else "").strip().lower()
            desc = str(r.get("description") or r.get("message") or "")
            if "操作成功" in desc or "success" in desc.lower():
                return True
            return ec in ("", "0", "200", "success", "true")
        r1 = pz.get_link_key()
        ok1 = _ok(r1)
        steps.append({"step": "getLinkKey（连通性，仅供参考）",
                      "ok": ok1, "optional": True,
                      "detail": ("操作成功" if ok1 else
                                 "403 属正常：这个接口我们发的是空 body（不带签名），发票云现在要求签名。"
                                 "它只用于探活，**不影响上传发票**——真正的判定看下面 access_token / getUserKey。"),
                      "raw": str(r1)[:400]})
        # ★ 2026-10-19：官方文档说「基础授权信息有误」多是**环境不匹配**
        #   （生产地址配了测试授权，或反之）。生产/测试各打一次，谁能过就说明授权属于哪个环境。
        # ★ v1.3.1 精简：排查阶段已完成，探测只保留 **3 次**调用。
        #   之前 8 次连续探测会把短时配额打满，紧接着上传就收到 403 Forbidden（用户实际踩到）。
        try:
            os.environ["PIAZZONE_BASE_URL"] = "https://api.piaozone.com"
            importlib.reload(pz)
            r2 = pz.get_user_key(bxd_key="PORTAL-TEST", ghf_mc=co.get("ghf_mc") or pz.GHF_MC)
            steps.append({"step": "getUserKey（生产环境 · 鉴权 + 录入单据）", "ok": _ok(r2),
                          "detail": _pz_explain(r2), "raw": str(r2)[:400]})
            r5 = pz.get_token()
            steps.append({"step": "access_token（client_id + secret 签名）", "ok": _ok(r5),
                          "detail": ("凭据有效。" if _ok(r5) else _pz_explain(r5)),
                          "raw": str(r5)[:400]})
        except Exception as exc:                          # noqa: BLE001
            steps.append({"step": "调用发票云", "ok": False,
                          "detail": "%r" % (exc,), "raw": ""})
    except Exception as exc:                              # noqa: BLE001
        steps.append({"step": "加载客户端", "ok": False,
                      "detail": "%s: %s" % (type(exc).__name__, str(exc)[:200]), "raw": ""})
    ok = all(x["ok"] for x in steps if not x.get("optional"))
    _audit("报销-发票云连通测试", {"tenant": t.get("code"), "ok": ok,
                                   "主体": co.get("ghf_mc") or co.get("client_id")}, "")
    return jsonify({"ok": True, "passed": ok, "steps": steps,
                    "company": {"ghf_mc": co.get("ghf_mc"), "tax_no": co.get("tax_no"),
                                "client_id": (co.get("client_id") or "")[:10] + "…",
                                "cid_full": co.get("client_id") or "",
                                "cid_len": len((co.get("client_id") or "").strip()),
                                "sec_len": len((co.get("client_secret") or "").strip())}}), 200


@app.post("/api/reimburse/piazzone-transfer")
def api_pz_transfer():
    """**公司内自己移交管理员**（不需要发布者介入）。"""
    _check_csrf()
    try:
        _write_gate()
        s = _require_session()
        _bind_actor(s)
        t = _tenant_for_session(s)
        if not t:
            return _err("没有绑定公司", "公司", "请用本公司专属链接登录")
        me = str((s.get("info") or {}).get("username") or "")
        admins = t.get("admin_users") or []
        if admins and me not in admins:
            return _err("只有本公司管理员能移交", "权限")
        newu = str((request.get_json(silent=True) or {}).get("new_admin") or "").strip()
        if not newu:
            return _err("请填写接手人的账号", "参数")
        store.add_tenant_admin(t["code"], newu, replace=True)
        _audit("报销-移交管理员", {"tenant": t["code"], "from": me, "to": newu}, me)
        return jsonify({"ok": True,
                        "config": _pz_payload(store.get_tenant(t["code"]), me)}), 200
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    except Exception as exc:                              # noqa: BLE001
        return _err("移交管理员", repr(exc)[:300])


@app.post("/api/reimburse/piazzone-mode")
def api_pz_mode():
    """本公司切换**凭据模式**（disk / memory / inherit 跟随全局）。

    只有本公司管理员能改；这是"这家公司愿不愿意把登录凭据落在磁盘上"的选择。
    """
    _check_csrf()
    try:
        _write_gate()
        s = _require_session()
        _bind_actor(s)
        t = _tenant_for_session(s)
        if not t:
            return _err("没有绑定公司", "公司", "请用本公司专属链接登录")
        me = str((s.get("info") or {}).get("username") or "")
        admins = t.get("admin_users") or []
        if admins and me not in admins:
            return _err("只有本公司管理员能改", "权限", "当前管理员：%s" % "、".join(admins))
        mode = str((request.get_json(silent=True) or {}).get("credential_mode") or "").lower()
        if not store.set_tenant_cred_mode(t["code"], mode):
            return _err("模式只能是 disk / memory / inherit", "参数")
        _audit("报销-凭据模式", {"tenant": t["code"], "mode": mode}, me)
        return jsonify({"ok": True, "config": _pz_payload(store.get_tenant(t["code"]), me)}), 200
    except KdError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 200
    except Exception as exc:                              # noqa: BLE001
        return _err("切换凭据模式", repr(exc)[:300])


# ================================================================ 租户管理（管理员）
# 定位：**发布者/管理员**用来"开通租户"的地方 —— 公司码 + 金蝶环境 + 组织映射。
# 发票云密钥是公司级密钥，也在这里按租户落库（封存），普通员工看不到这个页面的入口。
# 口令来源：portal_config.json 的 admin_password（环境变量 PORTAL_ADMIN_PASSWORD 优先）。
_ADMIN_FAIL = "口令不对"
# ★ 管理口令**防爆破**：员工地址就是根地址的下一层，他们一定会来试。
# ⚠️ 踩过的坑：**不能只按 IP 计数** —— 平台网关转发时 X-Forwarded-For 每次可能不同，
#    按 IP 计数永远攒不到阈值，线上实测 7 次错误都没锁上。所以这里改成**全局计数**。
#    代价：攻击者可以故意打错来"锁住"管理员（10 分钟）。这是可接受的取舍 ——
#    宁可短暂自锁，也不允许无限次试口令。IP 仍然记录进审计日志，便于追溯。
_adm_fails = []            # [时间戳, ...] 全局
_ADMIN_MAX_FAILS = 6
_ADMIN_FAIL_WINDOW = 600   # 10 分钟窗口内失败 6 次
_ADMIN_LOCK = 600          # 锁定 10 分钟


def _admin_ip() -> str:
    fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return fwd or (request.remote_addr or "?")


def _admin_locked() -> int:
    """返回还需锁定的秒数（0 = 未锁）。"""
    global _adm_fails
    now = time.time()
    _adm_fails = [t for t in _adm_fails if now - t < _ADMIN_FAIL_WINDOW]
    if len(_adm_fails) >= _ADMIN_MAX_FAILS:
        return max(0, int(_ADMIN_LOCK - (now - _adm_fails[-1])))
    return 0


def _admin_record_fail() -> None:
    _adm_fails.append(time.time())
    try:
        _audit("管理-口令错误", {"ip": _admin_ip(), "fails": len(_adm_fails)}, "?")
    except Exception:                                     # noqa: BLE001
        pass


def _admin_password() -> str:
    env = str(os.environ.get("PORTAL_ADMIN_PASSWORD") or "").strip()
    if env:
        return env
    cfg_pw = str(CFG.get("admin_password") or "").strip()
    if cfg_pw:
        return cfg_pw
    # 2026-10-19：口令不再进配置文件/发布包 —— 首次由控制台页面设置，存 state.db。
    try:
        return str(store.get_setting("admin_password", "") or "").strip()
    except Exception:                                     # noqa: BLE001
        return ""


def _admin_ok() -> bool:
    """校验管理口令。

    口令来源优先级：**库里的 PBKDF2 哈希** > 配置/环境变量里的初始口令。
    一旦在页面上改过口令，就以库里的哈希为准（部署包里的初始口令随即失效）。
    """
    if _admin_locked():
        raise KdError("尝试次数过多，已暂时锁定", "安全",
                      "请等 10 分钟后再试；如需立即恢复请联系发布者")
    got = str(request.headers.get("X-Admin-Token") or "")
    if not got:
        got = str((request.get_json(silent=True) or {}).get("admin_password") or "")
    h = ""
    try:
        h = store.get_setting("admin_pw_hash", "")
    except Exception:                                     # noqa: BLE001
        h = ""
    if h:
        ok = bool(got) and store.verify_password(got, h)
    else:
        pw = _admin_password()
        ok = bool(pw) and bool(got) and secrets.compare_digest(got, pw)
    if not ok:
        _admin_record_fail()
        return False
    return True


@app.post("/api/admin/password")
def api_admin_password():
    """修改管理员口令（存 PBKDF2 哈希，不存明文）。"""
    _check_csrf()
    if not _admin_ok():
        return _err("管理员口令不对", "权限")
    body = request.get_json(silent=True) or {}
    new = str(body.get("new_password") or "").strip()
    # 用户 2026-10-19 定：8 位以上即可，但**必须字母 + 数字**（纯数字太弱）
    if len(new) < 8:
        return _err("新口令太短", "参数", "至少 8 位")
    if not (re.search(r"[A-Za-z]", new) and re.search(r"\d", new)):
        return _err("口令太简单", "参数", "至少同时包含字母和数字")
    store.set_setting("admin_pw_hash", store.hash_password(new))
    _audit("管理-修改口令", {"len": len(new)}, "admin")
    return jsonify({"ok": True}), 200


@app.post("/api/admin/tenants/admin")
def api_admin_tenant_admin():
    """**移交公司管理员**：把某公司的管理员改为指定账号（发布者兜底用）。"""
    _check_csrf()
    if not _admin_ok():
        return _err("管理员口令不对", "权限")
    body = request.get_json(silent=True) or {}
    code = str(body.get("code") or "").strip()
    if not store.get_tenant(code):
        return _err("公司不存在", "公司")
    users = [str(u).strip() for u in (body.get("admin_users") or []) if str(u).strip()]
    if not users:
        return _err("请至少指定一个管理员账号", "参数")
    store.add_tenant_admin(code, users[0], replace=True)      # 第一个整体替换
    for u in users[1:]:
        store.add_tenant_admin(code, u)
    _audit("管理-移交管理员", {"code": code, "admins": users}, "admin")
    return jsonify({"ok": True, "tenants": store.list_tenants()}), 200


@app.get("/api/admin/stats")
def api_admin_stats():
    """运营统计：开通公司数 / 登录人次 / 各公司登录人数 / 最近登录明细。

    ⚠️ 只统计**账号、公司码、时间、成败** —— 不含任何密码或业务数据。
    """
    if not _admin_ok():
        return _err("管理员口令不对", "权限")
    try:
        days = int(request.args.get("days") or 30)
    except Exception:                                     # noqa: BLE001
        days = 30
    return jsonify({"ok": True, "stats": store.login_stats(days),
                    "tenants": store.list_tenants()}), 200


@app.get("/api/admin/settings")
def api_admin_settings_get():
    if not _admin_ok():
        return _err("管理员口令不对", "权限")
    return jsonify({"ok": True, "settings": {
        "credential_mode": store.credential_mode()}}), 200


@app.post("/api/admin/settings")
def api_admin_settings_set():
    """切换凭据模式：disk（默认，加密落盘、重启免登）/ memory（不落盘，重启需重登）。"""
    _check_csrf()
    if not _admin_ok():
        return _err("管理员口令不对", "权限")
    mode = str((request.get_json(silent=True) or {}).get("credential_mode") or "").lower()
    if mode not in ("disk", "memory"):
        return _err("模式只能是 disk 或 memory", "参数")
    store.set_setting("credential_mode", mode)
    _audit("管理-凭据模式", {"mode": mode}, "admin")
    return jsonify({"ok": True, "settings": {"credential_mode": store.credential_mode()}}), 200


@app.get("/api/admin/tenants")
def api_admin_tenants():
    if not _admin_ok():
        return _err("管理员口令不对", "权限", "在「租户管理」页顶部输入管理员口令")
    return jsonify({"ok": True, "tenants": store.list_tenants(),
                    "admin_enabled": bool(_admin_password())}), 200


def _probe_server(base_url: str) -> dict:
    """探活一个金蝶服务器地址，**不需要真账号**。

    手法：用假账号打一次登录接口 ——
      · 回"用户名或密码错误"  → 说明**这就是个金蝶 WebAPI 端点，服务器可用** ✓
      · 回 DNS/连接类错误      → 地址不对 ✗
    这样管理员填完地址就能当场看到"可用/不可用"，不用等员工登录才发现填错。
    """
    try:
        base = _validate_base_url(base_url)
        cli = KingdeeClient(base, acctid="", username="__probe__", password="__probe__")
        try:
            cli.login()
            return {"reachable": True, "note": "可达（登录意外成功了）"}
        except KdError as exc:
            m = "%s %s" % (exc.message or "", exc.hint or "")
            if any(k in m for k in ("密码", "账号", "用户名", "用户", "登录", "账套", "数据中心")):
                return {"reachable": True, "note": "服务器可达，是金蝶 WebAPI 端点"}
            return {"reachable": False, "note": "有回应但不是金蝶登录接口：%s" % m[:100]}
    except Exception as exc:                              # noqa: BLE001
        return {"reachable": False, "note": "连不上：%s" % repr(exc)[:140]}


@app.post("/api/admin/tenants/probe")
def api_admin_probe():
    _check_csrf()
    if not _admin_ok():
        return _err(_ADMIN_FAIL, "权限")
    server = str((request.get_json(silent=True) or {}).get("server") or "").strip()
    if not server:
        return _err("请先填服务器地址", "表单")
    r = _probe_server(server)
    return jsonify({"ok": True, "probe": r}), 200


@app.post("/api/admin/tenants/status")
def api_admin_tenant_status():
    """暂停 / 启用一个租户（暂停后该公司无法登录）。"""
    _check_csrf()
    if not _admin_ok():
        return _err(_ADMIN_FAIL, "权限")
    body = request.get_json(silent=True) or {}
    code = str(body.get("code") or "").strip()
    ok = store.set_tenant_status(code, str(body.get("status") or "active"))
    _audit("管理-租户状态", {"code": code, "status": body.get("status")}, "admin")
    return jsonify({"ok": True, "changed": ok, "tenants": store.list_tenants()}), 200


@app.post("/api/admin/tenants")
def api_admin_tenant_upsert():
    """开通/更新租户：管理员**只填公司名称 + 服务器**，唯一码系统自动生成。

    设计取舍（2026-10-13 用户定）：**不让管理员维护账套/组织/发票云密钥** ——
    账套由员工登录时自己填（他本来就知道），发票云密钥由该公司进来后在租户内自己配。
    """
    _check_csrf()
    if not _admin_ok():
        return _err(_ADMIN_FAIL, "权限")
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    server = str(body.get("server") or "").strip()
    code = str(body.get("code") or "").strip()
    if not name:
        return _err("请填公司名称", "表单")
    if not server:
        return _err("请填金蝶服务器地址", "表单")
    existed = store.get_tenant(code) if code else None
    if code and existed and not body.get("allow_update"):
        return _err("公司码已存在", "租户", "换个码，或勾选「覆盖已有租户」再保存")
    if not code:
        code = store.gen_code()                    # ★ 自动生成唯一码
    # 先探活：地址不通就别存，免得员工登录时才发现
    probe = _probe_server(server)
    if not probe.get("reachable") and not body.get("force"):
        return _err("服务器地址验证未通过", "验证",
                    "%s。确认地址无误后可勾选「仍然保存」" % probe.get("note", ""))
    old = (existed.get("piazzone") or {}) if existed else {}
    r = store.upsert_tenant(code, name, server, str(body.get("acct") or "").strip(),
                            old, existed.get("orgs") if existed else [])
    _audit("管理-保存租户", {"code": code, "name": name, "server": server,
                             "created": r["created"], "probe": probe.get("note")}, "admin")
    return jsonify({"ok": True, "created": r["created"], "code": code,
                    "probe": probe, "tenants": store.list_tenants()}), 200


@app.post("/api/admin/tenants/delete")
def api_admin_tenant_delete():
    _check_csrf()
    if not _admin_ok():
        return _err(_ADMIN_FAIL, "权限")
    code = str((request.get_json(silent=True) or {}).get("code") or "").strip()
    ok = store.delete_tenant(code)
    _audit("管理-删除租户", {"code": code, "ok": ok}, "admin")
    return jsonify({"ok": True, "deleted": ok, "tenants": store.list_tenants()}), 200


# ---------------------------------------------------------------- 启动
# 持久化层：建表 + 合并随部署包下发的基线租户（tenants.seed.json）
try:
    _st = store.init()
    print("[store] 状态库就绪：%s｜租户 %d 个｜基线合并 %d 条"
          % (_st["db"], _st["tenants"], _st["seed_merged"]))
except Exception as _e:
    print("[store] 初始化失败（会话将退回内存模式）：%r" % (_e,))

_cleanup()

# ★ 2026-10-17：后台内存预热（不落盘）。可用 PORTAL_NO_WARMUP=1 关闭。
if not LOCAL_MODE and CFG.get("warmup_auto", True) and os.environ.get("PORTAL_NO_WARMUP") != "1":
    try:
        threading.Thread(target=_warmup_loop, name="warmup", daemon=True).start()
    except Exception as _e:  # noqa: BLE001
        print("[warmup] 线程启动失败：%r" % (_e,))


def main():
    print("=" * 66)
    print("  金蝶经营门户 · 本地试验版")
    print(f"  地址：http://{CFG['host']}:{CFG['port']}")
    print("  仅监听本机；金蝶账号密码不落盘，只用于建立会话。")
    print("=" * 66)
    app.run(host=CFG["host"], port=CFG["port"], debug=bool(CFG.get("debug")),
            threaded=True)


if __name__ == "__main__":
    main()
