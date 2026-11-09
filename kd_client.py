# -*- coding: utf-8 -*-
"""金蝶云星空 WebAPI 客户端（只读）

设计要点
--------
1. 用「登录者本人的金蝶账号」建立会话 —— 能取到什么数据完全由金蝶的权限体系决定，
   本模块不做任何权限判断、也不绕过权限。
2. 凭据只在建立会话时使用一次；不落盘、不写日志、不回显。
3. 组织列表不内置：按 组织基础资料 → 账簿 → 业务单据 三级自动发现，
   并把每一级的成功/失败原因如实带回（例如"无组织机构查看权限"）。
4. 只读：仅调用 ValidateUser / ExecuteBillQuery / View / GetSysReportData / GetDataCenterList。
"""
from __future__ import annotations

import collections
import json
import re
import threading
import time

import requests

LOGIN_PATH = "Kingdee.BOS.WebApi.ServicesStub.AuthService.ValidateUser.common.kdsvc"
QUERY_PATH = "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.ExecuteBillQuery.common.kdsvc"
VIEW_PATH = "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.View.common.kdsvc"
REPORT_PATH = "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.GetSysReportData.common.kdsvc"
DATACENTER_PATH = ("Kingdee.BOS.ServiceFacade.ServicesStub.Account.AccountService."
                   "GetDataCenterList.common.kdsvc")

MASK = "***"

# 金蝶对 WebAPI 有硬性频次限制（实测提示：每分允许调用 WebAPI 的最大次数是 500）。
# 超限时它会返回一段"限流"文案而不是报错码，所以必须靠关键字识别 + 降速重试。
RATE_HINTS = ("超过最大值", "限流", "调用WebAPI次数", "调用频率", "too many requests", "rate limit")
DEFAULT_PER_MINUTE = 420          # 留 ~16% 余量，避免踩线


class RateLimiter:
    """滑动窗口限速：保证每分钟对金蝶的调用不超过配额"""

    def __init__(self, per_minute=DEFAULT_PER_MINUTE):
        self.per_minute = max(20, int(per_minute))
        self.times = collections.deque()
        self.lock = threading.Lock()
        self.waited = 0.0

    def acquire(self):
        while True:
            with self.lock:
                now = time.time()
                while self.times and now - self.times[0] > 60.0:
                    self.times.popleft()
                if len(self.times) < self.per_minute:
                    self.times.append(now)
                    return
                wait = 60.0 - (now - self.times[0]) + 0.05
            self.waited += min(wait, 5.0)
            time.sleep(max(0.05, min(wait, 5.0)))


class KdError(Exception):
    """带判读信息的取数错误（把"白名单/账套/密码/权限/字段"分开，避免盲试）"""

    def __init__(self, message, stage="", hint="", code=None):
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.hint = hint
        self.code = code

    def to_dict(self):
        return {"stage": self.stage, "message": self.message,
                "hint": self.hint, "code": self.code}


def normalize_base_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise KdError("金蝶服务器地址为空", "配置", "填写形如 https://your.ik3cloud.com/ 的地址")
    if not url.startswith("http"):
        url = "https://" + url
    url = url.rstrip("/")
    if not url.endswith("/k3cloud"):
        url += "/k3cloud"
    return url + "/"


def extract_error_text(payload) -> str:
    """从金蝶各种错误包装里挖出人话（它会把错误伪装成数据行返回）"""
    try:
        txt = json.dumps(payload, ensure_ascii=False)
    except Exception:
        txt = str(payload)
    for pat in (r'"Message"\s*:\s*"([^"]+)"', r'"message"\s*:\s*"([^"]+)"'):
        hits = [h for h in re.findall(pat, txt) if h.strip()]
        if hits:
            return max(hits, key=len)
    return txt[:200]


def _is_error_wrapper(value) -> bool:
    """金蝶把错误包成 [[{"Result":{...}}]] 或 {"Result":{...}}，必须判掉"""
    if isinstance(value, dict):
        return True
    if isinstance(value, list):
        if not value:
            return False
        first = value[0]
        if isinstance(first, dict):
            return True
        if isinstance(first, list) and first and isinstance(first[0], dict):
            return True
    return False


class KingdeeClient:
    def __init__(self, base_url, acctid="", acct_name="", username="", password="",
                 timeout=60, verbose=False, per_minute=DEFAULT_PER_MINUTE):
        self.base_url = normalize_base_url(base_url)
        self.acctid = str(acctid or "").strip()
        self.acct_name = str(acct_name or "").strip()
        self._username = username
        self._password = password
        self.timeout = timeout
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.login_name = ""
        self.limiter = RateLimiter(per_minute)
        self.api_calls = 0
        self.rate_retries = 0
        self._lock = threading.Lock()

    # ---------- 基础 ----------
    def _log(self, *a):
        if self.verbose:
            print("[kd]", *a)

    def _post(self, path, payload, timeout=None, max_rate_retries=4):
        """统一出口：过限速 → 识别金蝶限流文案 → 降速重试 → 解析 JSON"""
        url = self.base_url + path
        attempt = 0
        while True:
            self.limiter.acquire()
            with self._lock:
                self.api_calls += 1
            try:
                r = self.session.post(url, json=payload, timeout=timeout or self.timeout)
            except Exception as exc:
                raise KdError(f"请求失败：{type(exc).__name__}", "网络",
                              "确认服务器地址可达、URL 含 /k3cloud/、内网/VPN 已连接") from exc
            if r.status_code != 200:
                raise KdError(f"HTTP {r.status_code}", "网络",
                              "403 且空响应多为路径不对；502/504 多为网关或代理问题")
            text = r.text or ""
            if any(h in text for h in RATE_HINTS):
                attempt += 1
                self.rate_retries += 1
                if attempt > max_rate_retries:
                    raise KdError("金蝶 WebAPI 调用被限流（每分钟请求次数超上限）", "限流",
                                  "已自动降速重试多次仍被限流：请少勾选几个组织、"
                                  "或等 1 分钟后点「重新取数」；"
                                  "也请检查是否与其它金蝶取数任务同时运行。")
                time.sleep(5.0 + attempt * 2.0)
                continue
            try:
                return r.json()
            except Exception as exc:
                raise KdError("返回内容不是 JSON（地址可能指向了网关或非金蝶站点）",
                              "响应解析") from exc

    # ---------- 账套 ----------
    @staticmethod
    def list_datacenters(base_url, timeout=30):
        """免认证的账套列表，用于 acct_name → acctid 解析"""
        url = normalize_base_url(base_url) + DATACENTER_PATH
        try:
            r = requests.post(url, json={}, timeout=timeout)
        except Exception as exc:
            raise KdError(f"请求失败：{type(exc).__name__}", "网络") from exc
        if r.status_code != 200:
            raise KdError(f"HTTP {r.status_code}", "网络")
        text = (r.text or "").strip()
        data = None
        try:
            data = r.json()
        except Exception:
            data = None
        if data is None or isinstance(data, str):
            raw = data if isinstance(data, str) else text
            raw = (raw or "").strip()
            if raw.startswith("H4sI"):
                import base64
                import gzip
                raw = gzip.decompress(base64.b64decode(raw)).decode("utf-8", "replace")
            data = json.loads(raw)
        rows = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for key in ("Data", "data", "DataCenterList", "Result"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
        out = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            low = {str(k).lower(): v for k, v in item.items()}
            if not str(low.get("id") or "").strip():
                continue
            out.append({"id": str(low.get("id")).strip(),
                        "name": str(low.get("name") or "").strip(),
                        "number": str(low.get("number") or "").strip()})
        return out

    def resolve_acctid(self):
        if self.acctid:
            return self.acctid
        # ★ 2026-10-19：先查账套列表再判断（原来"没填名字"直接报错，导致单账套服务器
        #   也必须手填账套 —— 实测 zyzm 服务器只有 1 个账套，完全能自动）
        centers = self.list_datacenters(self.base_url)
        if not centers:
            raise KdError("服务器没有返回账套", "账套", "请确认服务器地址是否正确")
        if not self.acct_name:
            if len(centers) == 1:
                self.acctid = centers[0]["id"]
                return self.acctid
            names = "、".join(c["name"] for c in centers[:10])
            raise KdError("该服务器有 %d 个账套，需要指定" % len(centers), "账套",
                          "可用账套：%s" % names)
        hits = [c for c in centers if c["name"] == self.acct_name] or \
               [c for c in centers if self.acct_name in c["name"]]
        if len(hits) == 1:
            self.acctid = hits[0]["id"]
            return self.acctid
        if not hits:
            names = "、".join(c["name"] for c in centers[:10])
            raise KdError(f"账套「{self.acct_name}」不在服务器上", "账套",
                          f"可用账套：{names}")
        raise KdError(f"账套名称匹配到 {len(hits)} 个，请写完整名称", "账套")

    # ---------- 登录 ----------
    def login(self):
        acctid = self.resolve_acctid()
        data = self._post(LOGIN_PATH, {"acctid": acctid, "username": self._username,
                                      "password": self._password, "lcid": 2052})
        if not isinstance(data, dict):
            raise KdError("登录返回结构异常", "登录")
        if data.get("LoginResultType") == 1:
            self.login_name = str(self._username or "")
            self._log("登录成功")
            return True
        msg = str(data.get("Message") or extract_error_text(data))
        code = data.get("MessageCode", data.get("MsgCode"))
        hint = "核对账号、密码（注意大小写与首尾空格）。密码连续错约 5 次会锁号，不要反复重试。"
        if str(code) == "11" or "WebAPI" in msg:
            hint = ("该账号未获授权调用 WebAPI。管理员操作：基础管理 → 公共设置 → 参数设置 → "
                    "基础管理 → BOS平台 → WebAPI → 「允许调用WebAPI接口用户」加入此账号。")
        raise KdError(msg, "登录", hint, code)

    # ---------- 通用查询 ----------
    def query(self, form_id, field_keys, filter_string="", limit=2000,
              start=0, order="", timeout=None):
        if isinstance(field_keys, (list, tuple)):
            field_keys = ",".join(field_keys)
        payload = {"formid": form_id, "data": json.dumps({
            "FormId": form_id, "FieldKeys": field_keys, "FilterString": filter_string,
            "OrderString": order, "TopRowCount": 0, "StartRow": start, "Limit": limit,
        }, ensure_ascii=False)}
        data = self._post(QUERY_PATH, payload, timeout=timeout)
        if _is_error_wrapper(data):
            raise KdError(extract_error_text(data), "取数", code="query")
        if not isinstance(data, list):
            raise KdError(f"返回结构异常：{str(data)[:150]}", "取数")
        return data

    def query_all(self, form_id, field_keys, filter_string="", page=2000,
                  max_rows=200000, order=""):
        rows, start = [], 0
        while True:
            batch = self.query(form_id, field_keys, filter_string, limit=page,
                               start=start, order=order)
            rows.extend(batch)
            if len(batch) < page or len(rows) >= max_rows:
                break
            start += page
            time.sleep(0.05)
        return rows[:max_rows]

    def view(self, form_id, number, retries=2):
        payload = {"formid": form_id,
                   "data": json.dumps({"FormId": form_id, "Number": number}, ensure_ascii=False)}
        for attempt in range(retries + 1):
            try:
                data = self._post(VIEW_PATH, payload, timeout=60)
                res = (data or {}).get("Result", {}) if isinstance(data, dict) else {}
                if res.get("ResponseStatus", {}).get("IsSuccess") is False:
                    return None
                return res.get("Result") or None
            except KdError:
                if attempt >= retries:
                    return None
                time.sleep(0.4 * (attempt + 1))
        return None

    def sys_report(self, form_id, field_keys, model, scheme_id="", page=10000, timeout=180):
        if isinstance(field_keys, (list, tuple)):
            field_keys = ",".join(field_keys)
        rows, start = [], 0
        while True:
            payload = {"formid": form_id, "data": json.dumps({
                "FieldKeys": field_keys, "SchemeId": scheme_id, "StartRow": start,
                "Limit": page, "IsVerifyBaseDataField": "true", "FilterString": [],
                "Model": model,
            }, ensure_ascii=False)}
            data = self._post(REPORT_PATH, payload, timeout=timeout)
            res = (data or {}).get("Result", {}) if isinstance(data, dict) else {}
            if not res.get("IsSuccess", False):
                raise KdError(extract_error_text(res or data), "报表",
                              "报表接口对参数极敏感：确认账簿编码、期间、方案 ID 与账号权限")
            batch = res.get("Rows") or []
            rows.extend(batch)
            total = res.get("RowCount") or 0
            if not batch or len(batch) < page or len(rows) >= total:
                break
            start += len(batch)
        return rows

    # ---------- 组织自动发现（三级回退，不内置组织名） ----------
    ORG_BASEDATA = [
        ("ORG_Organizations", "FOrgId,FNumber,FName"),
        ("BD_Organization", "FOrgId,FNumber,FName"),
        ("ORG_Organizations", "FOrgID,FNumber,FName"),
        ("BD_Org", "FOrgId,FNumber,FName"),
    ]
    ORG_FROM_BILLS = [
        ("AR_receivable", "FSaleOrgId"),
        ("AP_Payable", "FPayOrgId"),
        ("ER_ExpenseRequest", "FOrgId"),
        ("ER_ExpReimbursement", "FPayOrgId"),
        ("SAL_OUTSTOCK", "FSaleOrgId"),
    ]

    def discover_orgs(self, max_per_source=4000):
        """返回 {"orgs":[{"number","name","sources":[...]}], "trace":[...], "notes":[...]}

        三级来源（都为「不内置组织名」服务）：
          L1 组织基础资料 —— 最全，但需要「组织机构查看权限」
          L2 账簿 BD_AccountBook —— 带 FAccountOrgID，直接给出 组织编码 + 组织名（财务类账号可读）
          L3 业务单据的组织字段 —— 权限兜底，只能看到有权限的单据所属组织
        """
        trace, notes = [], []
        merged = {}
        named_only = {}

        def add(number, name, source):
            number = str(number or "").strip()
            name = str(name or "").strip()
            if not number and not name:
                return
            if not number:                      # 只有名字的，先存着，稍后尝试并入有编码的组织
                named_only[name] = source
                return
            rec = merged.setdefault(number, {"number": number, "name": name, "sources": []})
            if name and (not rec["name"] or len(name) > len(rec["name"])):
                rec["name"] = name
            if source not in rec["sources"]:
                rec["sources"].append(source)

        # L1 组织基础资料
        for form_id, fields in self.ORG_BASEDATA:
            try:
                rows = self.query(form_id, fields, limit=2000, order="FNumber")
            except KdError as exc:
                trace.append({"level": "L1", "form": form_id, "ok": False,
                              "message": exc.message[:120]})
                continue
            got = 0
            for row in rows:
                if isinstance(row, list) and len(row) >= 3 and str(row[1]).strip():
                    add(row[1], row[2], f"基础资料 {form_id}")
                    got += 1
            trace.append({"level": "L1", "form": form_id, "ok": got > 0, "rows": got})
            if got:
                notes.append("组织列表来自「组织基础资料」，覆盖最全。")
                break

        # L2 账簿（字段顺序：FNumber=账簿编码, FName=账簿名, FAccountOrgID.FNumber/FName=组织）
        try:
            rows = self.query("BD_AccountBook",
                              "FNumber,FName,FAccountOrgID.FNumber,FAccountOrgID.FName",
                              limit=500, order="FNumber")
            got = 0
            for row in rows:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                if len(row) >= 4 and (row[2] or row[3]):
                    add(row[2], row[3], "账簿 BD_AccountBook")
                    got += 1
                elif str(row[1]).strip():
                    named_only[str(row[1]).strip()] = "账簿名称"
            trace.append({"level": "L2", "form": "BD_AccountBook", "ok": got > 0, "rows": got})
        except KdError as exc:
            trace.append({"level": "L2", "form": "BD_AccountBook", "ok": False,
                          "message": exc.message[:120]})

        # L3 业务单据的组织字段
        for form_id, field in self.ORG_FROM_BILLS:
            try:
                rows = self.query(form_id, f"FID,{field}.FNumber,{field}.FName",
                                  limit=max_per_source, order="FID desc")
            except KdError as exc:
                trace.append({"level": "L3", "form": form_id, "ok": False,
                              "message": exc.message[:100]})
                continue
            got = 0
            for row in rows:
                if isinstance(row, list) and len(row) >= 3 and (row[1] or row[2]):
                    add(row[1], row[2], f"单据 {form_id}")
                    got += 1
            trace.append({"level": "L3", "form": form_id, "ok": got > 0, "rows": got})

        orgs = sorted(merged.values(), key=lambda x: (x["number"] or "zzz", x["name"]))

        # 用只有名字的来源补全缺失的组织名（按包含关系，不写死任何公司名）
        for o in orgs:
            if o["name"]:
                continue
            for nm in named_only:
                if nm and (nm in o["number"] or o["number"] in nm):
                    o["name"] = nm
                    break

        if not orgs and named_only:
            orgs = [{"number": "", "name": nm, "sources": [src]}
                    for nm, src in sorted(named_only.items())]
            notes.append("只从账簿名称推断出组织（没有拿到组织编码），组织筛选将按名称进行。")

        if orgs and not any(o["sources"] and o["sources"][0].startswith("基础资料") for o in orgs):
            notes.append("当前账号无「组织机构」查看权限（金蝶会直接拒绝），"
                         "组织列表改为从账簿与你有权访问的单据中提取；"
                         "因此只覆盖「有账簿或有单据」的组织。")
        if not orgs:
            notes.append("未能发现任何组织。常见原因：账号权限过窄（看不到账簿也看不到单据）、"
                         "或该账套的组织基础资料 FormId 与默认不同。")
        return {"orgs": orgs, "trace": trace, "notes": notes}

    def discover_account_books(self, limit=500):
        """账簿列表（科目余额表报表需要）。

        字段顺序已实测确认：FNumber=账簿编码, FName=账簿名,
        FAccountOrgID.FNumber/FName = 账簿所属组织（编码/名称）。
        """
        try:
            rows = self.query("BD_AccountBook",
                              "FNumber,FName,FAccountOrgID.FNumber,FAccountOrgID.FName",
                              limit=limit, order="FNumber")
        except KdError:
            try:
                rows = self.query("BD_AccountBook", "FNumber,FName", limit=limit, order="FNumber")
            except KdError:
                return []
        out = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 2:
                continue
            no = str(row[0] or "").strip()
            nm = str(row[1] or "").strip()
            org_no = str(row[2]).strip() if len(row) > 2 and row[2] else ""
            org_nm = str(row[3]).strip() if len(row) > 3 and row[3] else ""
            if not no:
                continue
            out.append({"number": no, "name": nm, "org_number": org_no, "org_name": org_nm})
        return out

    # ---------- 汇总 ----------
    def describe(self):
        return {"base_url": self.base_url, "acctid": self.acctid,
                "username": self.login_name}
