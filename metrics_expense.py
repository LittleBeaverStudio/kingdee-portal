# -*- coding: utf-8 -*-
"""费用报销看板 · 指标口径（v2：一次查询取回单据体，不逐张 View）

口径（与现有「员工费用报销管理仪表盘」v2.1 完全一致）
------------------------------------------------------
1) 借款未还   = 申请单「申请借款=是」的 借款余额
               借款余额 = Σ(实际借款 − 已报销 − 已还款 − 核销)
   ⚠️ FAvailableLoanAmount 是员工级可用额度，不是本单余额，不能用。
2) 已申请未报销 = 申请单「申请借款=否」且「关闭状态=A(未关闭)」的
                 申请金额 − Σ(已下推报销单金额)
3) 报销未付款 = 报销单单据体 Σ(ReimbNotPayAmount)
有效单据 = 已审核(C) 且 未作废

v2 的关键改动（回答"是不是取多了"）
------------------------------------
实测：`ExecuteBillQuery` 的 FieldKeys **可以直接写单据体字段名**（无需 FEntity_ 前缀），
返回「一行一条单据体」的明细表，表头字段在每行重复。因此：
  旧做法 = 1 次表头查询 + 逐张 View（本账套 1600+ 次调用，受金蝶 500 次/分钟限流）
  新做法 = 每个单据类型 1 次查询 = 4 次调用，覆盖 100%，且不再需要张数上限
（金额与逐张 View 已抽样逐单核对一致）

字段自适应：不同账套/版本的单据体可选字段不一样（例如本账套没有 FSeq、Causa），
fetch_form 会解析金蝶的「元数据中标识为 XXX 的字段不存在」并逐个剔除后重试，
所以字段清单可以尽量全，缺什么自己降级；但**口径必需的字段**被剔除时会明确告警。
"""
from __future__ import annotations

import collections
import datetime
import re
import time

FIELDS_APPLY_BASE = (
    "FID,FBillNo,FDate,FDocumentStatus,FCancelStatus,FCloseStatus,FIsBorrow,"
    "FOrgAmountSum,FCheckedOrgAmountSum,FStaffID.FSTAFFNUMBER,FStaffID.FName,"
    "FDeptID.FName,FReason,FOrgId.FNumber,FOrgId.FName,"
    "FActualBorrowAmount,FReimburseAmount,FRefundAmount,FOffsetAmount,FPushReimbAmount"
)
FIELDS_REIMB_BASE = (
    "FID,FBillNo,FDate,FDocumentStatus,FCancelStatus,FExpAmountSum,"
    "FProposerID.FSTAFFNUMBER,FProposerID.FName,FRequestDeptID.FName,FExpenseDeptID.FName,"
    "Causa,FPayOrgId.FNumber,FPayOrgId.FName,"
    "FExpenseAmount,FReimbNotPayAmount,FExpID.FName"
)
BASE_FIELDS = {"apply": FIELDS_APPLY_BASE, "reimb": FIELDS_REIMB_BASE}

FORMS = [
    ("apply", "费用申请单", "ER_ExpenseRequest", "apply"),
    ("tapply", "出差申请单", "ER_ExpenseRequest_Travel", "apply"),
    ("reimb", "费用报销单", "ER_ExpReimbursement", "reimb"),
    ("treimb", "差旅费报销单", "ER_ExpReimbursement_Travel", "reimb"),
]

# 被剔除这些字段就无法按口径出数 —— 必须告警，不允许静默出 0
ESSENTIAL = {
    "apply": {"FBillNo", "FDocumentStatus", "FIsBorrow", "FOrgAmountSum", "FCloseStatus",
              "FActualBorrowAmount", "FReimburseAmount", "FRefundAmount", "FOffsetAmount",
              "FPushReimbAmount"},
    "reimb": {"FBillNo", "FDocumentStatus", "FExpAmountSum", "FReimbNotPayAmount"},
}
# 这些字段属于"锦上添花"，缺了不影响三大指标
OPTIONAL_OK = {"Causa", "FSeq", "FDeptID.FName", "FRequestDeptID.FName", "FExpenseDeptID.FName",
               "FReason", "FProposerID.FSTAFFNUMBER", "FStaffID.FSTAFFNUMBER", "FExpID.FName",
               "FCheckedOrgAmountSum", "FOrgId.FNumber", "FOrgId.FName",
               "FPayOrgId.FNumber", "FPayOrgId.FName"}

ENTRY_META = {
    "apply": ["FActualBorrowAmount", "FReimburseAmount", "FRefundAmount",
              "FOffsetAmount", "FPushReimbAmount"],
    "reimb": ["FExpenseAmount", "FReimbNotPayAmount", "FExpID.FName"],
}
HDR_META = {
    "apply": ["FID", "FBillNo", "FDate", "FDocumentStatus", "FCancelStatus", "FCloseStatus",
              "FIsBorrow", "FOrgAmountSum", "FCheckedOrgAmountSum", "FStaffID.FName",
              "FDeptID.FName", "FReason", "FOrgId.FNumber", "FOrgId.FName"],
    "reimb": ["FID", "FBillNo", "FDate", "FDocumentStatus", "FCancelStatus", "FExpAmountSum",
              "FProposerID.FName", "FRequestDeptID.FName", "FExpenseDeptID.FName", "Causa",
              "FPayOrgId.FNumber", "FPayOrgId.FName"],
}
FIELD_MISSING_RE = re.compile(r"标识为([A-Za-z0-9_.]+)的字段不存在")
ROW_LIMIT = 20000


def f2(v):
    try:
        if isinstance(v, str):
            v = v.replace(",", "").strip()
            if v == "":
                return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def is_true(v):
    return str(v).strip().lower() == "true"


def pick_name(v):
    """多语言字段 / 引用字段取中文名：兼容 str / list[dict{Key,Value}] / dict"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        nm = v.get("Name") or v.get("FName")
        if nm is not None and nm is not v:
            got = pick_name(nm)
            if got:
                return got
        return str(v.get("Number") or "").strip()
    if isinstance(v, list):
        for it in v:
            if isinstance(it, dict) and str(it.get("Key")) == "2052":
                return str(it.get("Value") or "").strip()
        for it in v:
            got = pick_name(it)
            if got:
                return got
    return ""


def s0(v):
    return "" if v is None else str(v).strip()


def fetch_form(client, form_id, family, progress=None):
    """一次查询取回表头 + 单据体；不存在的字段按金蝶报错自动逐个剔除。

    返回 (rows, fields, dropped, warnings)
    """
    fields = [f.strip() for f in BASE_FIELDS[family].split(",") if f.strip()]
    dropped, warns = [], []
    for _ in range(len(fields)):
        try:
            rows = client.query_all(form_id, ",".join(fields), page=ROW_LIMIT,
                                    max_rows=ROW_LIMIT * 2, order="FID")
        except Exception as exc:  # noqa: BLE001
            msg = str(getattr(exc, "message", exc))
            m = FIELD_MISSING_RE.search(msg)
            if not m or m.group(1) not in fields:
                return [], fields, dropped, [f"查询失败：{msg[:160]}"]
            bad = m.group(1)
            fields.remove(bad)
            dropped.append(bad)
            if bad not in OPTIONAL_OK and progress:
                progress(f"{form_id}：字段 {bad} 不存在，已剔除")
            continue
        if dropped:
            essential_dropped = [d for d in dropped if d in ESSENTIAL[family]]
            if essential_dropped:
                warns.append(f"口径必需字段缺失：{'、'.join(essential_dropped)}"
                             f"（{form_id} 的实际数据可能不准确）")
        if len(rows) >= ROW_LIMIT:
            warns.append(f"{form_id} 返回行数达到单次上限 {ROW_LIMIT}，可能存在遗漏；"
                         "建议按组织分批取数")
        return rows, fields, dropped, warns
    return [], fields, dropped, [f"字段全部被剔除，无法查询 {form_id}"]


def parse_bills(rows, fields, family):
    """把「一行一条单据体」的明细表折叠成「一张单一条记录」"""
    idx = {f: i for i, f in enumerate(fields)}
    hdr_keys = [k for k in HDR_META[family] if k in idx]
    ent_keys = [k for k in ENTRY_META[family] if k in idx]
    bills = {}
    for row in rows:
        if not isinstance(row, list):
            continue
        no_i = idx.get("FBillNo")
        if no_i is None or no_i >= len(row):
            continue
        no = s0(row[no_i])
        if not no:                       # 金蝶会返回少量空单号的脏行
            continue
        rec = bills.get(no)
        if rec is None:
            rec = {"billno": no, "entries": []}
            for k in hdr_keys:
                rec[k] = row[idx[k]] if idx[k] < len(row) else None
            bills[no] = rec
        ent = {k: (row[idx[k]] if idx[k] < len(row) else None) for k in ent_keys}
        rec["entries"].append(ent)
    return bills


def build(client, org_numbers=None, progress=None, max_bills=0, _forms=None, **_ignored):
    """org_numbers=None 表示不过滤（全部有权组织）。max_bills 仅作安全阀（0=不限）。"""
    p = progress or (lambda *_: None)
    org_set = {s0(x) for x in (org_numbers or []) if s0(x)}

    p("①/② 读取四类单据（表头 + 单据体，每类一次查询）")
    forms, field_warnings = {}, []
    if _forms is not None:
        forms = _forms
    else:
        for key, name, form_id, family in FORMS:
            rows, fields, dropped, warns = fetch_form(client, form_id, family, progress=p)
            for w in warns:
                field_warnings.append(f"{name}：{w}")
            if not rows:
                forms[key] = {"name": name, "form_id": form_id, "family": family,
                              "bills": {}, "error": "；".join(warns) or "无数据"}
                continue
            bills = parse_bills(rows, fields, family)
            forms[key] = {"name": name, "form_id": form_id, "family": family, "bills": bills,
                          "fields": fields, "dropped": dropped, "row_count": len(rows)}
            p(f"    {name}：{len(rows)} 行 → {len(bills)} 张单"
              + (f"（剔除字段：{'、'.join(dropped)}）" if dropped else ""))
            time.sleep(0.05)

    def org_of(rec):
        for a, b in (("FOrgId.FNumber", "FOrgId.FName"), ("FPayOrgId.FNumber", "FPayOrgId.FName")):
            if rec.get(a):
                return s0(rec[a]), s0(rec.get(b))
        return "", ""

    kept, total_before = {}, 0
    for key, f in forms.items():
        if f.get("error"):
            kept[key] = {}
            continue
        out = {}
        for no, rec in f["bills"].items():
            total_before += 1
            on, onm = org_of(rec)
            rec["_org"], rec["_orgname"] = on, onm
            if not org_set or on in org_set:
                out[no] = rec
        kept[key] = out

    n_kept = sum(len(v) for v in kept.values())
    p(f"②/② 组织过滤后：{n_kept} 张单（过滤前 {total_before} 张）")

    truncated = 0
    if max_bills and n_kept > max_bills:
        ordered = []
        for key in kept:
            for no, rec in kept[key].items():
                ordered.append((f2(rec.get("FExpAmountSum")) or f2(rec.get("FOrgAmountSum")),
                                key, no))
        ordered.sort(reverse=True)
        uth = {x[2] for x in ordered[:max_bills]}
        for key in kept:
            before = len(kept[key])
            kept[key] = {no: r for no, r in kept[key].items() if no in uth}
            truncated += before - len(kept[key])

    def valid(rec):
        return s0(rec.get("FDocumentStatus")) == "C" and s0(rec.get("FCancelStatus")) != "B"

    borrow_unpaid, borrow_bills = 0.0, []
    apply_unreimb, apply_bills = 0.0, []
    unpaid, unpaid_bills = 0.0, []
    exp_type = collections.defaultdict(float)

    for key in ("apply", "tapply"):
        for no, rec in kept.get(key, {}).items():
            if not valid(rec):
                continue
            ents = rec["entries"]
            if is_true(rec.get("FIsBorrow")):
                actual = sum(f2(e.get("FActualBorrowAmount")) for e in ents)
                re = sum(f2(e.get("FReimburseAmount")) for e in ents)
                rf = sum(f2(e.get("FRefundAmount")) for e in ents)
                of = sum(f2(e.get("FOffsetAmount")) for e in ents)
                bal = actual - re - rf - of
                if bal > 0.01:
                    borrow_unpaid += bal
                    borrow_bills.append({
                        "billno": no, "staff": pick_name(rec.get("FStaffID.FName")) or "未知",
                        "org": rec.get("_org", ""), "org_name": rec.get("_orgname", ""),
                        "date": s0(rec.get("FDate"))[:10], "amt": actual,
                        "settled": re + rf + of, "balance": bal,
                        "kind": "借款申请" if key == "apply" else "出差-借款",
                    })
            else:
                if s0(rec.get("FCloseStatus")) != "A":
                    continue
                amt = f2(rec.get("FOrgAmountSum"))
                pushed = sum(f2(e.get("FPushReimbAmount")) for e in ents)
                diff = amt - pushed
                if diff > 0.01:
                    apply_unreimb += diff
                    apply_bills.append({
                        "billno": no, "staff": pick_name(rec.get("FStaffID.FName")) or "未知",
                        "org": rec.get("_org", ""), "org_name": rec.get("_orgname", ""),
                        "date": s0(rec.get("FDate"))[:10], "amt": amt,
                        "pushed": pushed, "diff": diff,
                        "kind": "出差申请" if key == "tapply" else "费用申请",
                    })

    for key in ("reimb", "treimb"):
        for no, rec in kept.get(key, {}).items():
            ents = rec["entries"]
            if valid(rec):
                b_unpaid = sum(f2(e.get("FReimbNotPayAmount")) for e in ents)
                b_total = sum(f2(e.get("FExpenseAmount")) for e in ents)
                for e in ents:
                    exp_type[pick_name(e.get("FExpID.FName")) or "未分类"] += \
                        f2(e.get("FExpenseAmount"))
                if b_unpaid > 0.01:
                    unpaid += b_unpaid
                    unpaid_bills.append({
                        "billno": no, "staff": pick_name(rec.get("FProposerID.FName")) or "未知",
                        "org": rec.get("_org", ""), "org_name": rec.get("_orgname", ""),
                        "date": s0(rec.get("FDate"))[:10], "total": b_total, "unpaid": b_unpaid,
                        "kind": "费用报销" if key == "reimb" else "差旅报销",
                        "reason": s0(rec.get("Causa"))[:40],
                    })

    def sum_hdr(key, fld):
        return sum(f2(r.get(fld)) for r in kept.get(key, {}).values()
                   if s0(r.get("FCancelStatus")) != "B")

    apply_amt = sum_hdr("apply", "FOrgAmountSum")
    tapply_amt = sum_hdr("tapply", "FOrgAmountSum")
    reimb_amt = sum_hdr("reimb", "FExpAmountSum")
    treimb_amt = sum_hdr("treimb", "FExpAmountSum")

    pending_bills = []
    for key, f in forms.items():
        if f.get("error"):
            continue
        amt_key = "FExpAmountSum" if f["family"] == "reimb" else "FOrgAmountSum"
        nm_key = "FProposerID.FName" if f["family"] == "reimb" else "FStaffID.FName"
        for no, rec in kept.get(key, {}).items():
            st = s0(rec.get("FDocumentStatus"))
            if st not in ("B", "Z") or s0(rec.get("FCancelStatus")) == "B":
                continue
            pending_bills.append({
                "billno": no, "kind": f["name"],
                "staff": pick_name(rec.get(nm_key)) or "未知",
                "org": rec.get("_org", ""), "org_name": rec.get("_orgname", ""),
                "date": s0(rec.get("FDate"))[:10], "amt": f2(rec.get(amt_key)),
                "status": "审核中" if st == "B" else "草稿",
            })
    pending_bills.sort(key=lambda x: (x["status"] != "审核中", -x["amt"]))

    staff = collections.defaultdict(lambda: {
        "dept": "", "apply": 0.0, "reimb": 0.0, "borrow_unpaid": 0.0,
        "apply_unreimb": 0.0, "unpaid": 0.0, "n_apply": 0, "n_reimb": 0})
    for key in ("apply", "tapply"):
        for rec in kept.get(key, {}).values():
            nm = pick_name(rec.get("FStaffID.FName")) or "未知"
            staff[nm]["apply"] += f2(rec.get("FOrgAmountSum"))
            staff[nm]["n_apply"] += 1
            d = pick_name(rec.get("FDeptID.FName"))
            if d:
                staff[nm]["dept"] = d
    for key in ("reimb", "treimb"):
        for rec in kept.get(key, {}).values():
            nm = pick_name(rec.get("FProposerID.FName")) or "未知"
            staff[nm]["reimb"] += f2(rec.get("FExpAmountSum"))
            staff[nm]["n_reimb"] += 1
            d = pick_name(rec.get("FExpenseDeptID.FName")) or \
                pick_name(rec.get("FRequestDeptID.FName"))
            if d and not staff[nm]["dept"]:
                staff[nm]["dept"] = d
    for b in borrow_bills:
        staff[b["staff"]]["borrow_unpaid"] += b["balance"]
    for b in apply_bills:
        staff[b["staff"]]["apply_unreimb"] += b["diff"]
    for b in unpaid_bills:
        staff[b["staff"]]["unpaid"] += b["unpaid"]
    staff_rows = [{"name": k, **v} for k, v in staff.items()]
    staff_rows.sort(key=lambda x: -(x["borrow_unpaid"] + x["unpaid"] + x["apply_unreimb"]))

    monthly = collections.defaultdict(lambda: {"apply": 0.0, "reimb": 0.0})
    for key in ("apply", "tapply", "reimb", "treimb"):
        fam = forms[key]["family"]
        amt_key = "FExpAmountSum" if fam == "reimb" else "FOrgAmountSum"
        for rec in kept.get(key, {}).values():
            ym = s0(rec.get("FDate"))[:7]
            if ym:
                monthly[ym][fam] += f2(rec.get(amt_key))

    exp_items = sorted(exp_type.items(), key=lambda x: -x[1])
    pie = exp_items[:8]
    other = sum(v for _, v in exp_items[8:])
    if other > 0.01:
        pie.append(("其他", other))

    orgs_in_data = {}
    for key in kept:
        for rec in kept[key].values():
            if rec.get("_org"):
                orgs_in_data.setdefault(rec["_org"], rec.get("_orgname") or rec["_org"])

    # ---- 补全维度 1：按组织汇总（三大指标 + 单数，老板视角先看组织） ----
    org_agg = collections.defaultdict(lambda: {
        "borrow_unpaid": 0.0, "apply_unreimb": 0.0, "unpaid": 0.0,
        "n_apply": 0, "n_reimb": 0, "n_borrow": 0, "apply_amt": 0.0, "reimb_amt": 0.0})
    for b in borrow_bills:
        c = org_agg[b["org"] or "未知"]
        c["borrow_unpaid"] += b["balance"]
        c["n_borrow"] += 1
    for b in apply_bills:
        c = org_agg[b["org"] or "未知"]
        c["apply_unreimb"] += b["diff"]
    for b in unpaid_bills:
        c = org_agg[b["org"] or "未知"]
        c["unpaid"] += b["unpaid"]
    for key in ("apply", "tapply", "reimb", "treimb"):
        fam = forms[key]["family"]
        for rec in kept.get(key, {}).values():
            c = org_agg[rec.get("_org") or "未知"]
            if fam == "reimb":
                c["n_reimb"] += 1
                c["reimb_amt"] += f2(rec.get("FExpAmountSum"))
            else:
                c["n_apply"] += 1
                c["apply_amt"] += f2(rec.get("FOrgAmountSum"))
    org_rows = []
    for org, c in org_agg.items():
        org_rows.append({
            "org": org, "org_name": orgs_in_data.get(org, org),
            "borrow_unpaid": round(c["borrow_unpaid"], 2),
            "apply_unreimb": round(c["apply_unreimb"], 2),
            "unpaid": round(c["unpaid"], 2),
            "n_apply": c["n_apply"], "n_reimb": c["n_reimb"], "n_borrow": c["n_borrow"],
            "apply_amt": round(c["apply_amt"], 2), "reimb_amt": round(c["reimb_amt"], 2),
        })
    org_rows.sort(key=lambda x: -(x["borrow_unpaid"] + x["apply_unreimb"] + x["unpaid"]))

    # ---- 补全维度 2：账龄分档（按单据日期；借款未还 + 报销未付款两类） ----
    today_d = datetime.date.today()

    def _age(dstr):
        try:
            d = datetime.date.fromisoformat((dstr or "")[:10])
        except ValueError:
            return None
        return (today_d - d).days

    AGES = (("≤30 天", 0, 30), ("31–90 天", 31, 90), ("91–180 天", 91, 180),
            ("181–365 天", 181, 365), (">365 天", 366, 10 ** 9))
    aging = {"borrow": [], "unpaid": []}
    for label, lo, hi in AGES:
        aging["borrow"].append({
            "label": label,
            "amount": round(sum(b["balance"] for b in borrow_bills
                                if (_age(b["date"]) or 0) >= lo and (_age(b["date"]) or 0) <= hi), 2),
            "n": sum(1 for b in borrow_bills
                     if lo <= (_age(b["date"]) or 0) <= hi)})
        aging["unpaid"].append({
            "label": label,
            "amount": round(sum(b["unpaid"] for b in unpaid_bills
                                if (_age(b["date"]) or 0) >= lo and (_age(b["date"]) or 0) <= hi), 2),
            "n": sum(1 for b in unpaid_bills
                     if lo <= (_age(b["date"]) or 0) <= hi)})

    # ---- 补全维度 3：四类单据 × 单据状态分布（暂存/审核中/已审核/作废） ----
    STATUS_NAMES = {"Z": "暂存", "B": "审核中", "C": "已审核", "A": "已保存", "D": "作废"}
    st_agg = collections.defaultdict(lambda: {"n": 0, "amt": 0.0})
    for key in ("apply", "tapply", "reimb", "treimb"):
        fam = forms[key]["family"]
        amt_key = "FExpAmountSum" if fam == "reimb" else "FOrgAmountSum"
        for rec in kept.get(key, {}).values():
            st = s0(rec.get("FDocumentStatus")) or "?"
            if s0(rec.get("FCancelStatus")) == "B":
                st = "D"
            cell = st_agg[(forms[key]["name"], STATUS_NAMES.get(st, st))]
            cell["n"] += 1
            cell["amt"] += f2(rec.get(amt_key))
    status_rows = [{"kind": k[0], "status": k[1], "n": v["n"], "amt": round(v["amt"], 2)}
                   for k, v in sorted(st_agg.items(), key=lambda kv: (kv[0][0], -kv[1]["n"]))]

    header_errors = [f"{f['name']}：{f.get('error')}" for f in forms.values() if f.get("error")]
    dropped_all = {f["name"]: f.get("dropped") for f in forms.values() if f.get("dropped")}

    result = {
        "metrics": {
            "apply_amt": round(apply_amt, 2), "tapply_amt": round(tapply_amt, 2),
            "reimb_amt": round(reimb_amt, 2), "treimb_amt": round(treimb_amt, 2),
            "borrow_unpaid": round(borrow_unpaid, 2),
            "apply_unreimb": round(apply_unreimb, 2),
            "unpaid": round(unpaid, 2),
            "n_apply": len(kept.get("apply", {})) + len(kept.get("tapply", {})),
            "n_reimb": len(kept.get("reimb", {})) + len(kept.get("treimb", {})),
            "n_borrow": len(borrow_bills),
        },
        "borrow_bills": sorted(borrow_bills, key=lambda x: -x["balance"])[:60],
        "apply_bills": sorted(apply_bills, key=lambda x: -x["diff"])[:60],
        "unpaid_bills": sorted(unpaid_bills, key=lambda x: -x["unpaid"])[:60],
        "pending_bills": pending_bills[:60],
        "staff_rows": staff_rows[:60],
        "monthly": {k: {kk: round(vv, 2) for kk, vv in v.items()}
                    for k, v in sorted(monthly.items())},
        "exp_type_pie": [[k, round(v, 2)] for k, v in pie],
        "org_rows": org_rows,
        "aging": aging,
        "status_rows": status_rows,
        "orgs_in_data": [{"number": k, "name": v} for k, v in sorted(orgs_in_data.items())],
        "coverage": {
            "method": "ExecuteBillQuery 一次查询取回单据体（无需逐张 View）",
            "rows_fetched": {f["name"]: f.get("row_count", 0) for f in forms.values()},
            "bills_before_org_filter": total_before,
            "bills_after_org_filter": n_kept,
            "bills_need_detail": n_kept,
            "bills_with_detail": n_kept - truncated,
            "truncated": truncated,
            "max_bills": max_bills,
            "org_filter": sorted(org_set),
            "header_errors": header_errors,
            "warnings": field_warnings,
            "dropped_fields": dropped_all,
        },
    }
    # A failed form must not be interpreted as a measured zero.
    missing_apply = any(forms[k].get("error") for k in ("apply", "tapply"))
    missing_reimb = any(forms[k].get("error") for k in ("reimb", "treimb"))
    if missing_apply:
        for key in ("borrow_unpaid", "apply_unreimb", "n_borrow", "n_apply", "apply_amt", "tapply_amt"):
            result["metrics"][key] = None
    if missing_reimb:
        for key in ("unpaid", "n_reimb", "reimb_amt", "treimb_amt"):
            result["metrics"][key] = None
    if _forms is None:
        numbers = sorted(org_set or set(orgs_in_data))
        result["by_org"] = {no: build(None, org_numbers=[no], max_bills=max_bills,
                                      _forms=forms) for no in numbers}
    return result
