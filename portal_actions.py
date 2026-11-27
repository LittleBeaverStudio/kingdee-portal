# -*- coding: utf-8 -*-
"""门户的**写操作层**：报销办理（下推 → 上传发票建收票单 → 对齐金额 → 体检 → 提交）

架构定位（遵循《金蝶系列 Skill 架构约定》）
────────────────────────────────────────
    视角层（门户看板，只读）← 已有 metrics_expense / metrics_mgmt
    编排层（本文件）        ← 把「办理一次报销」拆成确定性步骤
    写操作层（skill helpers）← kingdee-expense-flow/helpers/recvin_link.py、expense_edit.py

**本文件不重复实现任何金蝶/发票云调用**，只做编排 + 门禁 + 审计。
这样口径与能力不会出现两套（skill 改了，门户自动跟着变）。

⚠️ 这是门户里**唯一的写操作入口**，安全边界与只读看板完全不同：
  · 默认关闭，需 portal_config.json 显式打开 `enable_write_ops`
  · 每个动作写审计日志（portal_audit.log，JSONL）
  · 提交（submit）必须显式二次确认，且先跑 precheck 闸门
  · 不做"换账号兜底"：底层用哪个账号由 skill 的配置决定（通常是服务账号，
    因为写收票信息需要收票池权限），门户不额外引入第三套凭据

已知限制（诚实标注）
────────────────────
· 改报销金额目前只支持**单条明细**的报销单；多条明细会明确报错而不是猜。
· 下推沿用默认转换规则（IsEnableDefaultRule=true）。
"""
from __future__ import annotations

import importlib
import hashlib
import json
import os
import sys
import threading
import time

import detail_fields
import detail_names

_SKILL_ENV_DIR = os.environ.get("PORTAL_EXPENSE_SKILL_DIR", "").strip()
SKILL_CANDIDATES = tuple(
    p for p in (
        # ⚠️ 从 SkillHub 装的技能目录名带 __skillhub 后缀（实测）。
        #    额外目录用环境变量 PORTAL_EXPENSE_SKILL_DIR 指定，
        #    不把任何开发机的绝对路径写进公开代码。
        os.path.expanduser(r"~\.workbuddy\skills\kingdee-expense-flow"),
        os.path.expanduser(r"~\.workbuddy\skills\kingdee-expense-flow__skillhub"),
        _SKILL_ENV_DIR,
    ) if p
)

# 随门户一起发布的那份（`kingdee-portal/vendor/helpers`）：
# 云端沙箱里没有装 skill，靠它把写操作跑起来。放在候选表**最后**，
# 所以本地开发时仍然优先用真正安装的 skill（vendor 只是部署用快照）。
_VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")


# ── 调用者身份：写操作以「登录者本人」执行 ──
# app.py 在每个写请求开头调用 use_credentials(登录者的金蝶凭据)，
# 于是下面所有金蝶写操作都跑在那位员工自己的身份上 —— 权限天然正确，
# 服务端也**不持有任何人的账号**（凭据来自那次登录的内存会话）。
# 用 threading.local：Flask 是 threaded=True，一个请求一个线程。
_ctx = threading.local()


def use_credentials(creds: dict | None, cache: dict | None = None) -> None:
    """绑定本次请求的操作者凭据；`cache` 传会话字典，用于复用已建立的金蝶客户端。"""
    _ctx.creds = creds or None
    _ctx.cache = cache if isinstance(cache, dict) else None


def current_credentials() -> dict | None:
    return getattr(_ctx, "creds", None)
FORM_EXPENSE = "ER_ExpReimbursement"
FORM_RECV_INV = "IV_ReceivedInvoice"

_lock = threading.RLock()
_cache: dict = {"kd": None, "at": 0.0, "skill_dir": ""}


# ────────────────────────── 基础：定位并加载 skill helpers ──────────────────────────
def find_skill_dir(configured: str = "") -> str:
    # 环境变量优先（本地想指定某一份副本时用；云端沙箱没有 skill，会自动落到 vendor）
    env = os.environ.get("PORTAL_EXPENSE_SKILL_DIR", "").strip()
    if env:
        configured = env
    if configured and os.path.isdir(os.path.join(configured, "helpers")):
        return configured
    for p in SKILL_CANDIDATES:
        if os.path.isdir(os.path.join(p, "helpers")):
            return p
    if os.path.isdir(os.path.join(_VENDOR_DIR, "helpers")):
        return _VENDOR_DIR
    # 兜底：按前缀扫一遍（应对将来目录名再变，如 __skillhub2）
    try:
        import glob
        for p in glob.glob(os.path.expanduser(r"~\.workbuddy\skills\*kingdee-expense-flow*")):
            if os.path.isdir(os.path.join(p, "helpers")):
                return p
    except Exception:                                     # noqa: BLE001
        pass
    return ""


def _load(configured: str = ""):
    """返回 (recvin_link 模块, expense_edit 模块, skill_dir)"""
    skill = find_skill_dir(configured)
    if not skill:
        raise RuntimeError(
            "找不到 kingdee-expense-flow skill（写操作依赖它的 helpers）。"
            "请在 portal_config.json 配 expense_skill_dir，或确认已安装该 skill。")
    helpers = os.path.join(skill, "helpers")
    if helpers not in sys.path:
        sys.path.insert(0, helpers)
    rl = importlib.import_module("recvin_link")
    try:
        ee = importlib.import_module("expense_edit")
    except Exception:                                     # noqa: BLE001
        ee = None
    return rl, ee, skill


def get_client(configured: str = "", ttl: int = 900):
    """拿到金蝶客户端。

    · 如果本次请求设置了调用者凭据（`use_credentials`）→ **用那位登录员工的身份**现建一个
      **不缓存**的客户端，写操作就跑在他自己名下（权限天然正确）。
    · 否则用 skill 配置里的账号（缓存复用），仅作只读兜底。
    """
    with _lock:
        rl, _ee, skill = _load(configured)
        creds = current_credentials()
        if creds:
            # ⚠️ 必须复用：`Kingdee(creds)` 会真的登录一次金蝶（1~3 秒）。
            #    早前每次调用都新建 → 报销管理每次翻页都重登，慢到没法用。
            cache = getattr(_ctx, "cache", None)
            if isinstance(cache, dict):
                kd = cache.get("act_kd")
                if kd is not None and (time.time() - float(cache.get("act_at") or 0) < 900):
                    return kd, rl, skill
                kd = rl.Kingdee(creds)
                cache["act_kd"], cache["act_at"] = kd, time.time()
                return kd, rl, skill
            return rl.Kingdee(creds), rl, skill
        if os.environ.get("PORTAL_LOCAL_MODE") == "1":
            raise RuntimeError("本地模式缺少当前登录用户凭据，拒绝使用其他账号")
        now = time.time()
        if _cache["kd"] is not None and (now - _cache["at"] < ttl):
            return _cache["kd"], rl, skill
        kd = rl.Kingdee(rl.load_kingdee_config())
        _cache.update({"kd": kd, "at": now, "skill_dir": skill})
        return kd, rl, skill


def drop_client():
    with _lock:
        _cache.update({"kd": None, "at": 0.0})


# ────────────────────────── 发票云凭据（公司级 API Key）──────────────────────────
# 为什么要单独一块：金蝶写操作用的是"登录者本人"的身份（见 use_credentials），
# 但**调发票云用的是公司级接口密钥**（client_id/client_secret/tax_no），跟哪个员工无关。
#
# 存哪：**只在内存**（进程级）。原因 ——
#   · skill 的新版凭据存储基于 Windows DPAPI，**云端沙箱是 Linux，用不了**；
#   · 拿不到操作系统级保护时，落盘就一定是明文，那还不如不落盘；
#   · 代价：服务重启/重新部署后需要在这里重填一次（通常一次就够用很久）。
# 回显策略：**永不回显密钥原文**，只回报"是否已配置"和 client_id 尾 4 位。
_PZ = {"base_url": "https://api.piaozone.com", "companies": []}
# 字段顺序与 skill 的配置向导保持一致（购方企业全称 → 购方税号 → client_id → client_secret）
# 另加 kingdee_orgs：该税号对应的**金蝶组织编码**，上传时按单据组织自动选对那家授权。
PZ_FIELDS = ("ghf_mc", "tax_no", "client_id", "client_secret", "kingdee_orgs", "eid")


def set_piazzone_companies(base_url: str, companies) -> None:
    """保存发票云配置（多家公司）。**只在内存**，见文件顶部说明。

    每家公司一个 dict：`{ghf_mc, tax_no, client_id, client_secret, kingdee_orgs}`。
    没填 client_id 的整行忽略（免得留下空壳）。
    """
    if str(base_url or "").strip():
        _PZ["base_url"] = str(base_url).strip().rstrip("/")
    out = []
    for c in (companies or []):
        if not isinstance(c, dict):
            continue
        row = {k: str(c.get(k) or "").strip() for k in PZ_FIELDS}
        if not row["client_id"]:
            continue
        out.append(row)
    _PZ["companies"] = out
    # 只有一家时也顺手应用一次，保证"配完即可用"
    if out:
        apply_company_credentials(out[0])
    else:
        # No tenant authorization: clear the previous tenant's environment too.
        apply_company_credentials({})


_PZ_ENVS = (("client_id", "PIAZZONE_CLIENT_ID"),
            ("client_secret", "PIAZZONE_CLIENT_SECRET"),
            ("tax_no", "PIAZZONE_TAX_NO"),
            ("ghf_mc", "PIAZZONE_GHF_MC"),
            ("eid", "PIAZZONE_EID"))
_applied = {"sig": ""}


def apply_company_credentials(co: dict) -> None:
    """把某家公司的凭据注入 `piazzone_client`。

    ⚠️ 三个坑（2026-10-19 修复，直接导致"早期能跑、后来报基础授权信息有误"）：
      1. **旧值不清会串味**：原来只"有值才设"，切主体时上一家的 tin/ghf_mc 会残留，
         结果 client_id 是 B 家的、税号是 A 家的 → 发票云判「基础授权信息有误」。
         现在先把全部 PIAZZONE_* 清一遍再设。
      2. **`sys.modules.pop` 对已持有的引用无效**：`recvin_link` 在模块加载时
         `import piazzone_client as pz` 并一直持有，pop 只影响"下次 import"，
         上传时用的还是旧模块对象（旧凭据）。改用 `importlib.reload(模块对象)` 就地更新。
      3. 缺 client_id 时模块导入会 `SystemExit`（继承 BaseException），必须提前返回 + 捕获。
    """
    # Include endpoint in the fingerprint: identical credentials at a different
    # server still require a reload. Never retain the plaintext secret in the cache.
    fingerprint = [str(co.get(k) or "") for k, _ in _PZ_ENVS]
    fingerprint.append(str(_PZ.get("base_url") or ""))
    sig = hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False).encode("utf-8")).hexdigest()
    if _applied["sig"] == sig:
        return                                  # 同一主体，重复请求不必再 reload
    for _, env_k in _PZ_ENVS:
        os.environ.pop(env_k, None)             # ★ 先清空，避免上一家残留
    for src_k, env_k in _PZ_ENVS:
        v = str(co.get(src_k) or "").strip()
        if v:
            os.environ[env_k] = v
    if _PZ.get("base_url"):
        os.environ["PIAZZONE_BASE_URL"] = str(_PZ["base_url"])
    if not str(co.get("client_id") or "").strip():
        _applied["sig"] = ""
        return                                  # 没有 client_id 就别导入（会 SystemExit）
    try:
        mod = sys.modules.get("piazzone_client")
        if mod is not None:
            importlib.reload(mod)               # ★ 就地更新，已持有的引用同步生效
        else:
            importlib.import_module("piazzone_client")
        _applied["sig"] = sig
    except BaseException as exc:                # noqa: BLE001  SystemExit 也要接
        _applied["sig"] = ""
        sys.stderr.write("[piazzone] 注入凭据失败：%r\n" % (exc,))


def pick_company_for_org(org_no: str) -> dict | None:
    """按金蝶组织挑开票主体。

    顺序（用户 2026-10-19 定）：① 组织编码精确匹配 → ② kingdee_orgs 里含该编码/名称（容错写法）
    → ③ 只有一家就直接用它 → ④ **多家都没匹配时用「默认那家」**（先给一个默认，用户可再改），
       并带上 `_fallback` 标记，让上传结果提示"本次用的是默认主体"。
    """
    cos = _PZ.get("companies") or []
    org = str(org_no or "").strip()
    if org:
        for c in cos:
            codes = [x.strip() for x in str(c.get("kingdee_orgs") or "").replace("，", ",").split(",")
                     if x.strip()]
            if org in codes:
                return dict(c)
        if os.environ.get("PORTAL_LOCAL_MODE") != "1":
            for c in cos:                                 # legacy mixed text compatibility
                raw = str(c.get("kingdee_orgs") or "")
                if raw and org in raw:
                    return dict(c)
    if not cos:
        return None
    if len(cos) == 1:
        return dict(cos[0])
    if os.environ.get("PORTAL_LOCAL_MODE") == "1":
        # Multiple authorities: an unmatched organization must never use another company.
        return None
    default_id = str(_PZ.get("default_id") or "")
    for c in cos:
        if c.get("id") == default_id:
            d = dict(c); d["_fallback"] = True
            return d
    d = dict(cos[0]); d["_fallback"] = True
    return d


def piazzone_status() -> dict:
    """给前端看的发票云配置（**不含密钥原文**，只报尾 4 位）。"""
    cos = []
    for c in (_PZ.get("companies") or []):
        d = {k: c.get(k, "") for k in PZ_FIELDS}
        d["client_secret"] = ""                          # 永不回显
        d["secret_tail"] = (c.get("client_secret") or "")[-4:]
        d["has_secret"] = bool(c.get("client_secret"))
        cos.append(d)
    return {
        "base_url": _PZ["base_url"],
        "companies": cos,
        "configured": any(c.get("client_id") for c in cos),
        "storage": "内存（不落盘）",
        "note": "配一次所有人都能用，直到服务重启；重新部署后需重填。密钥不回显、不写日志。",
    }


def audit(path: str, action: str, detail: dict, actor: str = "") -> None:
    """写审计日志（JSONL，追加）。写失败不影响主流程，但要打到 stderr。"""
    try:
        rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "actor": actor,
               "action": action, "detail": detail}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:                              # noqa: BLE001
        sys.stderr.write("[audit] 写日志失败：%r\n" % (exc,))


# ────────────────────────── 只看：可下推的申请单 ──────────────────────────
def list_pushable(configured: str = "", orgs=None, staff_name: str = "",
                  include_borrow: bool = True, limit: int = 200, filters=None) -> list:
    """列出「可以下推报销单」的费用/出差申请单。

    判据（与 skill 口径一致）：已审核(`C`) + 未作废 + 关闭状态=`A`(未关闭)。
    借款单一并列出（借款单同样可以下推报销），用 `is_borrow` 区分。
    """
    kd, _rl, _skill = get_client(configured)
    fields = ("FID,FBillNo,FDate,FDocumentStatus,FCloseStatus,FIsBorrow,FOrgAmountSum,"
              "FStaffID.FName,FStaffID.FSTAFFNUMBER,FOrgId.FNumber,FOrgId.FName,FReason")
    out = []
    for form, kind in (("ER_ExpenseRequest", "费用申请"),
                       ("ER_ExpenseRequest_Travel", "出差申请")):
        if filters and filters.get("kind") and (filters["kind"] == "travel") != (kind == "出差申请"):
            continue
        from list_filters import conditions
        where = ["FDocumentStatus='C'", "FCloseStatus='A'"] + conditions(
            filters, person_field="FStaffID.FName", date_field="FDate", orgs=orgs, application=True)
        try:
            rows = kd.query(form, fields,
                            " AND ".join(where), top=limit,
                            order="FDate desc")
        except Exception:                                 # noqa: BLE001
            continue
        idx = {f: i for i, f in enumerate(fields.split(","))}
        form_rows = []
        for r in rows or []:
            def g(k):
                i = idx.get(k)
                return r[i] if i is not None and i < len(r) else ""
            org_no = str(g("FOrgId.FNumber") or "")
            if orgs and org_no not in orgs:
                continue
            if staff_name and str(g("FStaffID.FName") or "").strip() != staff_name:
                continue
            is_borrow = str(g("FIsBorrow")).strip().lower() in ("true", "1")
            if is_borrow and not include_borrow:
                continue
            form_rows.append({
                "fid": g("FID"), "bill_no": str(g("FBillNo") or "").strip(),
                "date": str(g("FDate") or "")[:10], "kind": kind,
                "staff": str(g("FStaffID.FName") or "").strip(),
                "org_no": org_no, "org_name": str(g("FOrgId.FName") or "").strip(),
                "amount": float(g("FOrgAmountSum") or 0),
                "is_borrow": is_borrow,
                "reason": str(g("FReason") or "").strip(),
            })
        # 金额字段在明细中：先选本页单据，再批量读明细，避免多明细重复表头金额。
        ids = sorted({str(r["fid"]) for r in form_rows if str(r["fid"]).isdigit()})
        if ids:
            amounts = kd.query(form, "FID,FEntity_FEntryID,FPUSHREIMBAMT",
                               "FID in (%s)" % ",".join(ids), top=10001)
            if len(amounts) >= 10001:
                raise RuntimeError("申请单明细过多，无法完整读取已下推金额，请缩小查询范围")
            from decimal import Decimal
            totals, seen, present = {}, set(), set()
            for row in amounts:
                key = (str(row[0]), str(row[1]))
                if key in seen:
                    continue
                seen.add(key)
                present.add(key[0])
                value = Decimal(str(row[2]))
                if not value.is_finite():
                    raise RuntimeError("已下推报销单金额异常，请在金蝶核对")
                totals[key[0]] = totals.get(key[0], Decimal("0")) + value
            for item in form_rows:
                if str(item["fid"]) not in present:
                    raise RuntimeError("无法读取申请单的已下推报销单金额，请刷新重试")
                item["pushed_reimb_amount"] = float(totals[str(item["fid"])])
        out.extend(form_rows)
    out.sort(key=lambda x: (x["date"], x["fid"]), reverse=True)
    return out[:limit]


def _request_state(kd, form: str, fid: str, bill_no: str = "") -> dict:
    """按内码核对来源；列表打开后可能已被关闭，写入前必须重读。"""
    if not str(fid).isdigit():
        raise ValueError("申请单内码无效，请刷新申请单列表")
    source = kd.view(form, str(fid))
    if bill_no and str(source.get("BillNo") or "").strip() != bill_no:
        raise ValueError("申请单编号与内码不匹配，请刷新列表")
    return source


def _check_request_open(source: dict) -> None:
    if str(source.get("DocumentStatus") or "").strip() != "C":
        raise ValueError("申请单未审核，不能下推报销单或关闭")
    if str(source.get("FCancelStatus") or source.get("CancelStatus") or "").strip() == "B":
        raise ValueError("申请单已作废，不能下推报销单或关闭")
    closed = str(source.get("FCloseStatus") or "").strip()
    if closed == "B":
        raise ValueError("申请单已关闭，不能继续下推报销单")
    if closed != "A":
        raise ValueError("无法确认申请单关闭状态，请刷新列表或到金蝶核对")


def close_request(configured: str, fid: str, is_travel: bool = False, bill_no: str = "") -> dict:
    """调用金蝶整单关闭操作，保留其插件/权限/借款业务校验，回读关闭状态。"""
    kd, rl, _ = get_client(configured)
    form = "ER_ExpenseRequest_Travel" if is_travel else "ER_ExpenseRequest"
    source = _request_state(kd, form, fid, bill_no)
    number = str(source.get("BillNo") or "").strip()
    if str(source.get("FCloseStatus") or "").strip() == "B":
        return {"ok": True, "fid": str(fid), "bill_no": number, "already_closed": True,
                "close_status": "B"}
    _check_request_open(source)
    uncertain = False
    try:
        raw = kd._post(rl.KSVC_SUFFIX + "ExecuteOperation.common.kdsvc", {
            "formid": form, "opNumber": "BillClose",
            "data": json.dumps({"Ids": str(fid), "Numbers": []}, ensure_ascii=False)},
            retries=1, timeout=90)
        status = (raw.get("Result") or {}).get("ResponseStatus") or {}
        if not status.get("IsSuccess"):
            message = "；".join(str(e.get("Message") or "") for e in (status.get("Errors") or []))
            return {"ok": False, "fid": str(fid), "bill_no": number,
                    "message": message or "金蝶未确认关闭成功，请刷新申请单列表核对"}
    except (Exception, SystemExit):
        uncertain = True
    try:
        after = _request_state(kd, form, fid, number)
    except (Exception, SystemExit) as exc:
        raise RuntimeError("关闭结果未确认，请先刷新申请单列表或到金蝶核对，勿重复操作") from exc
    if str(after.get("FCloseStatus") or "").strip() != "B":
        raise RuntimeError("关闭结果未确认，请先刷新申请单列表或到金蝶核对，勿重复操作")
    return {"ok": True, "fid": str(fid), "bill_no": number, "close_status": "B",
            "confirmed_after_timeout": uncertain}


def _push_request_guard(kd, form: str, source_fid: str, number: str) -> None:
    if not source_fid:
        rows = kd.query(form, "FID,FBillNo", "FBillNo='%s'" % number.replace("'", "''"), top=2)
        if len(rows) != 1 or str(rows[0][1] or "").strip() != number:
            raise ValueError("无法确认申请单，请刷新列表后下推")
        source_fid = str(rows[0][0])
    _check_request_open(_request_state(kd, form, source_fid, number))


# ────────────────────────── 写：下推 ──────────────────────────
def push(configured: str, bill_no_or_fid: str, is_travel: bool = False, source_fid: str = "") -> dict:
    """下推生成报销单。返回 {fid, bill_no, status, amount}。"""
    kd, rl, _skill = get_client(configured)
    src_form = "ER_ExpenseRequest_Travel" if is_travel else "ER_ExpenseRequest"
    tgt_form = "ER_ExpReimbursement_Travel" if is_travel else FORM_EXPENSE
    _push_request_guard(kd, src_form, source_fid, str(bill_no_or_fid))
    payload = {"formid": src_form, "data": json.dumps({
        "Ids": "", "Numbers": [str(bill_no_or_fid)], "RuleId": "",
        "TargetFormId": tgt_form, "IsEnableDefaultRule": "true",
        # 发票尚未上传时正式保存可能不通过；允许金蝶暂存草稿供后续办理。
        "IsDraftWhenSaveFail": "true", "CustomParams": {}}, ensure_ascii=False)}
    # 下推是非幂等写操作：超时后金蝶可能已建单，不能沿用 _post 的自动重试。
    try:
        r = kd._post(rl.KSVC_SUFFIX + "Push.common.kdsvc", payload, retries=1)  # noqa: SLF001
    except (Exception, SystemExit) as exc:
        raise RuntimeError("下推结果未确认，请先到报销单列表或金蝶核对是否已生成，确认后再操作。"
                           "接口信息：%s" % str(exc)[:200]) from exc
    if not isinstance(r, dict) or not isinstance(r.get("Result"), dict):
        raise RuntimeError("下推结果未确认，请先到报销单列表或金蝶核对是否已生成，确认后再操作。")
    res = (r.get("Result") or {}).get("ResponseStatus") or {}
    if not res.get("IsSuccess"):
        errs = res.get("Errors") or []
        msg = json.dumps(errs[:2], ensure_ascii=False)[:400]
        raise RuntimeError("金蝶返回下推失败：%s。请先核对报销单列表，确认没有生成单据后再操作。" % msg)
    succ = res.get("SuccessEntitys") or []
    if not succ:
        raise RuntimeError("下推结果未确认（金蝶未返回新单 ID），请先核对报销单列表。")
    fid = str(succ[0].get("Id") or "")
    if not fid:
        raise RuntimeError("下推结果未确认（金蝶未返回新单 ID），请先核对报销单列表。")
    # 建单已经成功：补充往来单位失败时只能提示，绝不能再报下推失败。
    result = {"fid": fid, "bill_no": str(succ[0].get("Number") or "")}
    result["warning"] = _inherit_contact(kd, src_form, tgt_form, str(source_fid or ""),
                                         str(bill_no_or_fid), fid)
    payment_warning = _set_nonborrow_payment(kd, src_form, tgt_form, str(source_fid or ""),
                                             str(bill_no_or_fid), fid)
    result["warning"] = " ".join(x for x in (result["warning"], payment_warning) if x)
    return result


def _set_nonborrow_payment(kd, src_form, tgt_form, source_fid, source_number, target_fid):
    """Only set payment when both the source and generated entries prove no loan exists."""
    warning = "报销单已生成，但付款类型未能自动确认，请在金蝶核对后再提交。"
    try:
        if not source_fid:
            return warning
        source = kd.view(src_form, source_fid)
        if str(source.get("BillNo") or "").strip() != source_number:
            return warning
        if str(source.get("IsBorrow")).strip().lower() not in ("false", "0"):
            return ""  # 借款在提交时按实时余额计算，余额有剩余时请员工选择处理方式。
        target = kd.view(tgt_form, target_fid)
        entries = target.get("ER_ExpenseReimbEntry") or []
        if "FSrcEntry" not in target or not entries or any("BorrowAmount" not in e
                              or str(e.get("IsFromBorrow")).strip().lower() not in ("false", "0")
                              or float(e.get("BorrowAmount") or 0) != 0 for e in entries):
            return warning
        if any(float(e.get("FTurnoverAmount") or 0) != 0 or str(e.get("FSrcBillNo") or "").strip()
               for e in (target.get("FSrcEntry") or [])):
            return warning
        if str(target.get("RequestType")) == "1":
            return ""
        if str(target.get("DocumentStatus") or "").upper() not in ("Z", "A", "D"):
            return warning
        ok, _ = kd.save(tgt_form, {"FID": target_fid, "FRequestType": "1"},
                        need_update=["FRequestType"], retries=1, validate_flag=False)
        if ok and str(kd.view(tgt_form, target_fid).get("RequestType")) == "1":
            return ""
    except (Exception, SystemExit):
        pass
    return warning


def _inherit_contact(kd, src_form: str, tgt_form: str, source_fid: str,
                     source_number: str, target_fid: str) -> str:
    """只在目标为空且来源明确时沿用申请单往来单位；失败不改变建单结果。"""
    try:
        target = kd.view(tgt_form, target_fid)
        if target.get("CONTACTUNIT"):
            return ""
        if not source_fid:
            return "报销单已生成，但未能确认申请单往来单位，请在提交前核对。"
        source = kd.view(src_form, source_fid)
        if str(source.get("BillNo") or "").strip() != source_number:
            return "报销单已生成，但申请单信息不匹配，请在提交前核对往来单位。"
        contact = source.get("TOCONTACTUNIT") or source.get("ContactUnit") or source.get("CONTACTUNIT") or {}
        contact_type = source.get("TOCONTACTUNITTYPE") or source.get("ContactUnitType") or source.get("CONTACTUNITTYPE")
        number = str(contact.get("Number") or "").strip() if isinstance(contact, dict) else ""
        if not number or not isinstance(contact_type, str) or not contact_type.strip():
            return "报销单已生成，但申请单没有可识别的往来单位，请在提交前核对。"
        model = {"FID": target_fid, "FCONTACTUNITTYPE": contact_type,
                 "FCONTACTUNIT": {"FNumber": number}}
        ok, _ = kd.save(tgt_form, model, need_update=["FCONTACTUNIT", "FCONTACTUNITTYPE"],
                        retries=1, validate_flag=False)
        if ok:
            reread = kd.view(tgt_form, target_fid)
            saved = reread.get("CONTACTUNIT") or {}
            if isinstance(saved, dict) and str(saved.get("Number") or "").strip() == number:
                return ""
        return "报销单已生成，但未能确认往来单位已带入，请在提交前核对。"
    except (Exception, SystemExit):
        return "报销单已生成，但读取或带入往来单位失败，请在提交前核对。"


# ────────────────────────── 读：报销单详情 ──────────────────────────
def get_bill(configured: str, bill_fid: str, is_travel: bool = False) -> dict:
    """读报销单：表头 + 明细行 + 收票信息行（用于办理页展示与体检）。"""
    kd, _rl, _skill = get_client(configured)
    form = "ER_ExpReimbursement_Travel" if is_travel else FORM_EXPENSE
    o = kd.view(form, bill_fid)
    # 不同账套/版本报销明细的子表键不同：老版 ER_ExpenseReimbEntry，字段文档里是 FEntity。
    ents = o.get("ER_ExpenseReimbEntry") or o.get("FEntity") or []
    # 明细里的费用项目/部门/项目等只回编码时，补成中文名（含二开字段）。
    ents = detail_names.resolve(ents, form, bill_fid, kd,
                                entry_keys=("FEntity", "ER_ExpenseReimbEntry"))
    recv = o.get("RecInvInfo") or []                       # ⚠️ View 子表键不带 F 前缀

    def flat_name(v):
        if isinstance(v, dict):
            nm = v.get("Name") or v.get("FName")
            if isinstance(nm, list):
                for it in nm:
                    if str(it.get("Key")) == "2052":
                        return str(it.get("Value") or "")
            if isinstance(nm, str):
                return nm
            return str(v.get("Number") or "")
        if isinstance(v, list):
            for it in v:
                if str(it.get("Key")) == "2052":
                    return str(it.get("Value") or "")
        return ""

    recv_rows = []
    for r in recv:
        ri = r.get("RecInv") or {}
        recv_rows.append({
            "entry_id": r.get("Id"),
            "bill_no": str(ri.get("FBillNo") or "").strip() if isinstance(ri, dict) else "",
            "amount": float(ri.get("FSUMALLAMOUNT") or 0) if isinstance(ri, dict) else 0,
            # 发票原件预览地址（发票云返回）—— 前端用它显示缩略图
            "pdf_url": (str(ri.get("FPDFURL") or "") if isinstance(ri, dict) else ""),
            "invoice_no": (str(ri.get("FIVNUMBER") or "") if isinstance(ri, dict) else ""),
        })
    return {
        "fid": bill_fid,
        "bill_no": str(o.get("BillNo") or "").strip(),
        "status": str(o.get("DocumentStatus") or "").strip(),
        "request_type": str(o.get("RequestType") if o.get("RequestType") is not None else ""),
        "payment_basis": ("unknown" if "FSrcEntry" not in o or not ents or any(
                              "IsFromBorrow" not in e or "BorrowAmount" not in e for e in ents)
                          else "borrow" if any(str(e.get("IsFromBorrow")).strip().lower() not in ("false", "0")
                                               or float(e.get("BorrowAmount") or 0) != 0 for e in ents)
                          or any(float(e.get("FTurnoverAmount") or 0) != 0
                                 or str(e.get("FSrcBillNo") or "").strip() for e in (o.get("FSrcEntry") or []))
                          else "nonborrow"),
        # ★ 组织必须带出来：上传发票建收票单时要用它做来源/结算组织，
        #   前端也用它做默认值（早前硬编码 101 是 bug）
        "org_no": str((o.get("OrgID") or {}).get("Number") or "").strip(),
        "org_name": flat_name(o.get("OrgID")),
        "amount": float(o.get("ExpAmountSum") or 0),
        # 申请金额（源申请单带过来的额度）：让用户在"申请额度 / 发票合计"之间自己定
        "req_amount": float(o.get("ReqAmountSum") or 0),
        "contact": flat_name(o.get("CONTACTUNIT")),
        "proposer": flat_name(o.get("ProposerID")),
        "entries": [{"entry_id": e.get("Id"), "seq": e.get("Seq"),
                     "amount": float(e.get("ExpenseAmount") or 0),
                     "exp_item": flat_name(e.get("ExpID"))} for e in ents],
        # 审批弹窗用：按单据类型自动识别的有内容明细列（老的 entries 保留给办理页）。
        "detail": detail_fields.detail_view(ents, form),
        "recv_rows": recv_rows,
        "recv_total": round(sum(x["amount"] for x in recv_rows), 2),
        "submittable": str(o.get("DocumentStatus") or "").strip() in ("Z", "A", "B"),
    }


# ────────────────────────── 写：删除 / 读：申请单详情 ──────────────────────────
DELETE_PATH = "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.Delete.common.kdsvc"
# 允许删除的单据状态（skill 里实测结论：Delete 只放 Z/A/D）
DELETABLE_STATUS = {"Z": "暂存", "A": "创建", "D": "重新审核"}


def delete_bill(configured: str, bill_fid: str, is_travel: bool = False) -> dict:
    """删除报销单（只允许 Z/A/D；审核中/已审核必须先撤销或反审核）。

    场景（用户 2026-10-18）：一张申请单可以下推好几张报销单，重复下推的那几张要能删掉。
    ⚠️ 不做"猜"：状态不在白名单就明确拒绝，并告诉用户下一步该做什么。
    """
    kd, _rl, _skill = get_client(configured)
    form = "ER_ExpReimbursement_Travel" if is_travel else FORM_EXPENSE
    info = get_bill(configured, bill_fid, is_travel)
    status = str(info.get("status") or "").strip().upper()
    if status not in DELETABLE_STATUS:
        return {"ok": False, "stage": "删除", "errcode": "STATUS",
                "status": status, "bill_no": info.get("bill_no") or "",
                "message": "当前状态 %s 不能直接删除" % (status or "未知"),
                "hint": ("审核中的单据请先在金蝶里撤销提交；已审核的请先反审核。"
                         "门户只删暂存/创建/重新审核状态的单，避免绕过审批。")}
    payload = {"formid": form,
               "data": json.dumps({"CreateOrgId": 0, "Numbers": [], "Ids": str(bill_fid)},
                                  ensure_ascii=False)}
    data = kd._post(DELETE_PATH, payload, timeout=60)
    res = {}
    if isinstance(data, dict):
        res = data.get("Result") or data
    ok = bool(res.get("ResponseStatus", {}).get("IsSuccess")) if isinstance(res, dict) else False
    if not ok:
        msg = ""
        if isinstance(res, dict):
            errs = (res.get("ResponseStatus") or {}).get("Errors") or []
            msg = "；".join(str(e.get("Message") or "") for e in errs if isinstance(e, dict))[:200]
        return {"ok": False, "stage": "删除", "errcode": "KD",
                "status": status, "bill_no": info.get("bill_no") or "",
                "message": msg or "金蝶拒绝了删除操作"}
    audit_detail = {"fid": bill_fid, "bill_no": info.get("bill_no") or "",
                    "status_before": status, "amount": info.get("amount")}
    return {"ok": True, "bill_no": info.get("bill_no") or "",
            "status_before": status, "audit": audit_detail}


def request_detail(configured: str, bill_fid: str, is_travel: bool = False) -> dict:
    """读费用/出差申请单的内容（表头 + 费用明细），用于「查看」弹窗。

    已审核的申请单是审批依据，门户**不提供编辑**；要看内容、要对账，这里给全。
    ⚠️ View 只认**内码**（传单号会报 "Id 的值 ... 类型错误，无法转换为整型"）。
    """
    kd, _rl, _skill = get_client(configured)
    form = "ER_ExpenseRequest_Travel" if is_travel else "ER_ExpenseRequest"
    o = kd.view(form, str(bill_fid))

    def flat(v):
        if isinstance(v, dict):
            nm = v.get("Name") or v.get("FName")
            if isinstance(nm, list):
                for it in nm:
                    if str(it.get("Key")) == "2052":
                        return str(it.get("Value") or "")
            if isinstance(nm, str):
                return nm
            return str(v.get("Number") or "")
        if isinstance(v, list):
            for it in v:
                if str(it.get("Key")) == "2052":
                    return str(it.get("Value") or "")
        return ""

    # ⚠️ 实测（2026-10-18）：ER_ExpenseRequest 的 View 单据体键是 **FEntity**，
    #    金额在 FLocAmount（明细金额），不是 Amount/ExpAmount —— 之前试
    #    ReqEntry/ExpenseEntry/Entry 全都取不到，明细恒为 0 行。
    ents = (o.get("FEntity") or o.get("ReqEntry") or [])
    ents = detail_names.resolve(ents, form, str(bill_fid), kd,
                                entry_keys=("FEntity", "ReqEntry"))
    entries = []
    checked_rows = []
    checked_seq = []          # 与 ents 同序，供费用明细「审核核定金额」列逐行对齐
    for e in ents:
        amt = (e.get("FLocAmount") or e.get("CheckedOrgAmount")
               or e.get("FPushReimbAmount") or e.get("Amount") or 0)
        entries.append({
            "seq": e.get("Seq"),
            "item": flat(e.get("ExpenseItemID") or e.get("ExpID")),
            "amount": float(amt or 0),
            "pushed": float(e.get("FPushReimbAmount") or 0),
            "note": str(e.get("Note") or e.get("Remark") or "").strip(),
        })
        # 审批同意时可顺手改核定金额：带出分录内码和现值，前端只提交改动行。
        entry_id = str(e.get("Id") or e.get("FEntryID") or "").strip()
        try:
            checked = round(float(e.get("FCheckedOrgAmount") or 0), 2)
        except (TypeError, ValueError):
            checked = 0.0
        checked_seq.append({"entry_id": entry_id if entry_id.isdigit() else "",
                            "checked": checked})
        if entry_id.isdigit():
            checked_rows.append({"entry_id": entry_id, "seq": e.get("Seq"),
                                 "item": flat(e.get("ExpenseItemID") or e.get("ExpID")),
                                 "checked": checked})
    # 出差申请单的「申请组织」在 View 里叫 FOrgID，费用申请单叫 OrgId，逐个兼容。
    org_obj = (o.get("OrgId") or o.get("OrgID") or o.get("FOrgID") or o.get("FOrgId")
               or o.get("ApplyOrgID") or o.get("FApplyOrgID") or o.get("ApplyOrgId") or {})
    org_no = ""
    org_name = ""
    if isinstance(org_obj, dict):
        org_no = str(org_obj.get("Number") or org_obj.get("FNumber") or "").strip()
        org_name = flat(org_obj)
    elif org_obj:
        org_name = str(org_obj).strip()
    return {
        "bill_no": str(o.get("BillNo") or "").strip() or str(bill_fid),
        "date": str(o.get("Date") or "")[:10],
        "status": str(o.get("DocumentStatus") or "").strip(),
        "staff": flat(o.get("StaffID")),
        "org_no": org_no,
        "org_name": org_name,
        "amount": float(o.get("OrgAmountSum") or o.get("AmountSum") or 0),
        "reason": str(o.get("Reason") or o.get("Cause") or "").strip(),
        "is_borrow": str(o.get("IsBorrow")).strip().lower() == "true",
        "entries": entries,
        "detail": detail_fields.detail_view(ents, form, checked=checked_seq),
        "checked_rows": checked_rows,
        "close_status": str(o.get("CloseStatus") or "").strip(),
    }


# ────────────────────────── 写：上传发票 → 建收票单 → 挂单 ──────────────────────────
def upload_invoice_to_bill(configured: str, bill_fid: str, file_path: str,
                           filename: str, org_no: str, item_name: str,
                           bill_no: str = "", is_travel: bool = False) -> dict:
    """一步完成：上传发票（真实验票）→ 建收票单（手填真流水号）→ 挂到报销单。

    复用 skill 的能力（`recvin_link.Kingdee.upload_invoice` / `create_recv_invoice` / `link`），
    本函数只负责串起来 + 把结果整理成前端好用的结构。
    """
    kd, _rl, _skill = get_client(configured)
    # 多家开票主体时，按单据组织选对应授权（用户 2026-10-14：一家租户下可配多家）。
    # 选不到就明说，不猜 —— 猜错会把发票记到另一家主体上。
    co = pick_company_for_org(org_no)
    if not co:
        cos = _PZ.get("companies") or []
        n = len(cos)
        if not n:
            return {"ok": False, "stage": "上传发票", "errcode": "NOCFG",
                    "message": "没有可用的发票云授权",
                    "hint": "请在「发票云配置」页添加开票主体"}
        detail = "；".join(
            "%s 登记的是「%s」" % ((c.get("ghf_mc") or c.get("client_id") or "未命名主体"),
                                  (c.get("kingdee_orgs") or "空"))
            for c in cos)
        return {"ok": False, "stage": "上传发票", "errcode": "NOCFG",
                "message": "配了 %d 家开票主体，但都没有登记组织 %s" % (n, org_no),
                "hint": ("当前：%s。请在「发票云配置」把组织 %s 填到负责它的那家主体上"
                         "（多家主体时必须填，否则无法判断用哪家查验发票）。" % (detail, org_no))}
    apply_company_credentials(co)
    up = kd.upload_invoice(file_path, bxd_key=str(bill_fid), bill_no=str(bill_no or ""))
    if not up.get("ok"):
        return {"ok": False, "stage": "上传发票", "errcode": up.get("errcode"),
                "message": up.get("description") or "上传失败",
                "hint": ("未找到发票云凭证" if up.get("errcode") == "NOCFG"
                         else "换更清晰的原件（XML/PDF 优于拍照件）"
                         if up.get("errcode") == "1999" else "")}
    # 项目名称改为**可选**（2026-10-10 用户确认）：
    # 发票云确实不返回发票明细项目名称（详情接口需 client_id+未知参数，暂未打通），
    # 但**不合并生成费用明细时，金蝶会沿用申请单上的费用项目名称** —— 所以不必强填。
    # 传了就写进收票单明细行；不传则不带 FEntity，交给金蝶按申请单口径处理。
    # 兜底：发票云说"成功"但既没发票号也没流水号 → 这不是发票，交给上层转附件
    if not up.get("invoice_no") and not up.get("serial_no"):
        return {"ok": False, "stage": "识别", "errcode": "NOTINVOICE",
                "message": "发票云未返回发票号/流水号（不是发票）", "upload": up}

    bill_no2, fid, created, note = kd.create_recv_invoice(
        invoice_no=up.get("invoice_no"), amount=float(up.get("total") or 0),
        tax_amount=float(up.get("tax") or 0),
        open_date=time.strftime("%Y-%m-%d"),
        seller_name=up.get("seller") or "", seller_tax=up.get("seller_tax") or "",
        buyer_name="", buyer_tax=up.get("buyer_tax") or "",
        org_no=str(org_no), item_name=item_name,
        tax_rate=float(up.get("tax_rate") or 0),
        serial_no=up.get("serial_no"), pdf_url=up.get("pdf_url") or "")
    if not bill_no2:
        return {"ok": False, "stage": "建收票单", "message": "未返回收票单号", "upload": up}

    # 回读核对：cookbook 警告过 Save 返回 IsSuccess 也可能静默丢字段
    rows = kd.query(FORM_RECV_INV, "FBillNo,FPIAOZONESERIALNUMBER,FGENERATETYPE,FISEXAMINE",
                    "FBillNo='%s'" % bill_no2, top=2)
    serial_ok = bool(rows) and str(rows[0][1] or "").strip() == str(up.get("serial_no") or "")
    link_res = kd.link(bill_fid, [bill_no2], formid=(
        "ER_ExpReimbursement_Travel" if is_travel else FORM_EXPENSE))
    warn = ""
    if co.get("_fallback"):
        warn = ("组织 %s 没有单独配开票主体，本次用的是默认主体「%s」。"
                "若不对，请到「发票云配置」把该组织指到正确的主体。" % (
                    org_no, co.get("ghf_mc") or co.get("client_id") or ""))
    return {
        "ok": True, "stage": "完成",
        "warning": warn,
        "recvin_bill_no": bill_no2, "recvin_fid": fid, "created": bool(created), "note": note,
        "serial_ok": serial_ok,
        "invoice_no": up.get("invoice_no"), "amount": up.get("total"),
        "tax": up.get("tax"), "seller": up.get("seller"),
        "pdf_url": up.get("pdf_url") or "",          # 发票原件预览地址（给前端出缩略图）
        "linked": len(link_res.get("written") or []),
        "skipped": len(link_res.get("skipped") or []),
        "upload": up,
    }


# ────────────────────────── 写：对齐报销金额 ──────────────────────────
def set_amount(configured: str, bill_fid: str, amount: float,
               is_travel: bool = False) -> dict:
    """把报销金额对齐到指定值。

    ⚠️ 目前只支持**单条明细**的报销单（多明细会明确报错，不猜）。
    口径（2026-10-10 用户确认）：报销金额**可以**大于申请金额，业务允许 —— 这里不做硬拦截。
    """
    kd, rl, skill = get_client(configured)
    rl2, ee, _ = _load(configured)
    if ee is None:
        raise RuntimeError("找不到 expense_edit.py（改金额依赖它）")
    form = "ER_ExpReimbursement_Travel" if is_travel else FORM_EXPENSE
    info = get_bill(configured, bill_fid, is_travel)
    ents = info["entries"]
    if not ents:
        raise RuntimeError("这张报销单没有明细行，无法改金额（请先在金蝶里补明细）")
    if len(ents) > 1:
        raise RuntimeError(
            "这张报销单有 %d 条明细，门户暂不支持自动分配（避免猜错费用项目）。"
            "请在金蝶里调整，或告知我要按哪一行填。" % len(ents))
    ee.set_detail(kd, form, bill_fid, ents[0]["entry_id"], amount=float(amount))
    after = get_bill(configured, bill_fid, is_travel)
    return {"ok": True, "amount": after["amount"], "before": info["amount"],
            "recv_total": after["recv_total"]}


# ────────────────────────── 读/写：体检与提交 ──────────────────────────
def precheck(configured: str, bill_fid: str, is_travel: bool = False, *, check_payment: bool = True) -> dict:
    """提交前体检（闸门本体，只读）。返回结论供前端展示。"""
    kd, _rl, _skill = get_client(configured)
    form = "ER_ExpReimbursement_Travel" if is_travel else FORM_EXPENSE
    info = get_bill(configured, bill_fid, is_travel)
    issues = []
    payment = None
    if check_payment:
        try:
            payment = payment_preview(configured, bill_fid, is_travel)
        except (ValueError, RuntimeError) as exc:
            issues.append(str(exc))
    if not info["recv_rows"]:
        issues.append("没有收票信息行（发票未挂上）")
    if "contact" in info and not info["contact"]:
        issues.append("往来单位为空，请核对申请单的往来单位并补齐后提交")
    if info["recv_rows"] and info["recv_total"] + 0.005 < info["amount"]:
        issues.append("发票价税合计 %.2f 小于报销金额 %.2f"
                      % (info["recv_total"], info["amount"]))
    try:
        gate = kd.precheck(bill_fid, form)
        blocks = gate.get("blocks") or []
        gate_ok, gate_msg = not blocks, "；".join(blocks) or "闸门通过"
    except SystemExit as exc:
        gate_ok, gate_msg = False, str(exc)[:200]
    except Exception as exc:                              # noqa: BLE001
        gate_ok, gate_msg = False, repr(exc)[:200]
    return {"ok": not issues and gate_ok, "issues": issues,
            "gate_ok": gate_ok, "gate_message": gate_msg,
            "amount": info["amount"], "recv_total": info["recv_total"],
            "recv_rows": len(info["recv_rows"]), "status": info["status"], "payment": payment}


def payment_preview(configured: str, fid: str, is_travel: bool = False) -> dict:
    import reimburse_payment
    kd, _, _ = get_client(configured)
    return reimburse_payment.preview(kd, fid, is_travel)


def submit(configured: str, bill_fid: str, is_travel: bool = False,
           choice: str = "", snapshot: str = "") -> dict:
    import reimburse_payment as payment
    kd, rl, _ = get_client(configured)
    return payment.submit_checked(kd, rl.KSVC_SUFFIX, bill_fid, is_travel, choice, snapshot,
                                  lambda: precheck(configured, bill_fid, is_travel, check_payment=False))


def attach_file(configured: str, bill_fid: str, file_path: str, alias: str = "",
                is_travel: bool = False) -> dict:
    """把文件挂到报销单的**附件**（非发票材料走这里：行程单、说明、扫描件）。

    复用 `expense_edit.attach_file`（它已内置「IsLast=true」+ 回环下载比 md5 的校验 ——
    cookbook 记过：漏传 IsLast 时接口照样返回成功，但内容根本没落库）。
    """
    kd, _rl, _skill = get_client(configured)
    _r2, ee, _ = _load(configured)
    if ee is None:
        raise RuntimeError("找不到 expense_edit.py（附件上传依赖它）")
    form = "ER_ExpReimbursement_Travel" if is_travel else FORM_EXPENSE
    info = get_bill(configured, bill_fid, is_travel)
    _bn = str(info.get("bill_no") or "").strip()
    # ★ 2026-10-19（第二次修正）：**BillNO 必须是真实单据编号**。
    #   · 传空 → 接口报「参数 BillNO 的值不能为空」，附件传不上去；
    #   · 传 FID 顶替 → 接口返回成功、也能下载，但**金蝶界面按编号过滤，看不到**（孤儿附件）。
    #   草稿单（暂存 Z）没有编号，所以先触发一次保存拿编号。
    if not _bn:
        try:
            set_amount(configured, bill_fid, float(info.get("amount") or 0), is_travel=is_travel)
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "stage": "附件",
                    "message": "这张单还没保存（没有单据编号），附件传不上去；"
                               "自动保存也失败：%s" % repr(exc)[:150]}
        info = get_bill(configured, bill_fid, is_travel)
        _bn = str(info.get("bill_no") or "").strip()
    if not _bn:
        return {"ok": False, "stage": "附件",
                "message": "单据保存后仍未拿到编号，附件无法上传。"
                           "常见原因：下推金额超出源申请单的可下推余额、或有必填项缺失。"
                           "请先在金蝶里把这张单保存成功，再回来传附件。"}
    file_id = ee.attach_file(kd, form, bill_fid, _bn, file_path, alias=(alias or None))
    return {"ok": True, "file_id": file_id, "bill_no": _bn}


def upload_or_attach(configured: str, bill_fid: str, file_path: str, filename: str,
                     org_no: str, item_name: str, bill_no: str = "",
                     is_travel: bool = False) -> dict:
    """一个文件进来：**先按发票处理；识别不出是发票就自动转附件**。

    用户口径（2026-10-11）：「凡是识别出来不是发票的都上传到附件」。
    判据用发票云的识别结果：
      · `0000` 且拿到流水号 → 发票，进收票信息
      · `1999 识别异常`     → **不是发票**（行程单/说明/无关图片）→ 自动转附件
      · 其它错误（网络/未配凭证）→ **不转**，直接报错
        （免得把网络问题伪装成"已当附件传好了"，那样用户会以为办完了）
    """
    # ★ 2026-10-19：**先按文件名排除"根本不是发票"的文件**，直接进附件，不碰发票云。
    #   原因：行程单/报销单这类文件送进发票云会被当成发票走 getUserKey，既慢又可能因
    #   授权/组织匹配问题报 1101，而它本来就不该产生收票单。
    _NOT_INV = ("行程单", "行程报销单", "报销单", "申请单", "说明", "明细", "订单", "凭证", "小票")
    _nm = str(filename or os.path.basename(file_path or ""))
    if any(h in _nm for h in _NOT_INV):
        try:
            at = attach_file(configured, bill_fid, file_path, alias=filename, is_travel=is_travel)
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "route": "attach", "stage": "转附件",
                    "message": "不是发票，但传到附件时失败：%s" % repr(exc)[:160]}
        return {"ok": True, "route": "attach", "stage": "完成",
                "file_id": at.get("file_id"),
                "message": "不是发票，已上传到附件"}

    try:
        res = upload_invoice_to_bill(configured, bill_fid, file_path, filename,
                                     org_no, item_name, bill_no, is_travel)
    except BaseException as exc:                          # noqa: BLE001  SystemExit 也接
        # 任何未预期的异常都收敛成结构化错误，别让它冒成 500（前端只会显示"网络异常"）
        return {"ok": False, "route": "invoice", "stage": "上传发票",
                "message": "处理失败：%s: %s" % (type(exc).__name__, str(exc)[:160])}
    if res.get("ok"):
        res["route"] = "invoice"
        return res
    if str(res.get("errcode")) in ("1999", "NOTINVOICE"):
        try:
            at = attach_file(configured, bill_fid, file_path, alias=filename, is_travel=is_travel)
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "route": "attach", "stage": "转附件",
                    "message": "发票云识别不出这是发票，转附件时又失败：%s" % repr(exc)[:180]}
        return {"ok": True, "route": "attach", "stage": "完成",
                "file_id": at.get("file_id"), "message": "发票云未识别为发票 → 已作为附件上传"}
    res["route"] = "invoice"
    return res


# ────────────────────────── 读：我提交过的报销单 + 状态 ──────────────────────────
# 金蝶单据状态（费用/差旅报销单）：Z 暂存 · A 已保存 · B 已提交(审核中) · C 已审核 · D 作废
BILL_STATUS_CN = {"Z": "暂存", "A": "已保存", "B": "审核中", "C": "已审核", "D": "作废"}


def list_bills(configured: str = "", orgs=None, proposer: str = "", limit: int = 60,
               only_pending: bool = False, filters=None) -> list:
    """列出报销单（默认最近 N 张），带状态中文名，供"提交后查看进度"用。

    ⚠️ 这里**不逐单读收票信息**（那是每单一次 View，太贵）；需要看某张的发票时再单独打开。
    """
    kd, _rl, _skill = get_client(configured)
    fields = ("FID,FBillNo,FDocumentStatus,FExpAmountSum,FProposerID.FName,"
              "FOrgId.FNumber,FOrgId.FName,FCreateDate")
    out = []
    for form, kind in ((FORM_EXPENSE, "费用报销"), ("ER_ExpReimbursement_Travel", "差旅报销")):
        if filters and filters.get("kind") and (filters["kind"] == "travel") != (kind == "差旅报销"):
            continue
        from list_filters import conditions, quote
        where = conditions(filters, person_field="FProposerID.FName", date_field="FCreateDate", orgs=orgs)
        if proposer:
            where.append("FProposerID.FName=" + quote(proposer))
        if only_pending:
            where.append("FDocumentStatus='B'")
        try:
            rows = kd.query(form, fields, " AND ".join(where), top=limit, order="FID desc")
        except Exception:                                 # noqa: BLE001
            continue
        idx = {f: i for i, f in enumerate(fields.split(","))}
        for r in rows or []:
            def g(k):
                i = idx.get(k)
                return r[i] if i is not None and i < len(r) else ""
            org_no = str(g("FOrgId.FNumber") or "")
            st = str(g("FDocumentStatus") or "").strip()
            who = str(g("FProposerID.FName") or "").strip()
            if orgs and org_no not in orgs:
                continue
            if proposer and who != proposer:
                continue
            if only_pending and st not in ("B",):
                continue
            out.append({
                "fid": g("FID"), "bill_no": str(g("FBillNo") or "").strip(),
                "kind": kind, "status": st, "status_cn": BILL_STATUS_CN.get(st, st),
                "amount": float(g("FExpAmountSum") or 0),
                "proposer": who, "org_no": org_no,
                "org_name": str(g("FOrgId.FName") or "").strip(),
                "created": str(g("FCreateDate") or "")[:19].replace("T", " "),
            })
    out.sort(key=lambda x: int(x["fid"] or 0), reverse=True)
    return out[:limit]
