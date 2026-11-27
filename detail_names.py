# -*- coding: utf-8 -*-
"""审批明细里的基础资料编码 -> 中文名称。

金蝶 View 接口对基础资料字段经常只回编码（FExpenseItemID=1001 或
F_TJKT_Base_qtr={"Number":"XM001"}），审批弹窗里看起来就是一串数字。
这里按三条路解析，命中即用、失败静默退回原值：
  1. 同一行的 Fxxx_Name / Fxxx__Name 兄弟字段；
  2. 在单据自身上查 FEntity_Fxxx.FName（权限跟随单据，最可靠）；
  3. 常见基础资料表按主键 / FNumber 反查。

只处理「像编码」的值，避免把金额、天数、备注误当成基础资料。
"""
from __future__ import annotations

import re
from copy import deepcopy

import detail_fields

# 字段（去 F、去 _H、大写）-> 基础资料类别
FIELD_KIND = {
    "EXPENSEITEMID": "EXPENSE_ITEM", "EXPID": "EXPENSE_ITEM", "COSTID": "EXPENSE_ITEM",
    "COSTDEPTID": "DEPT", "ENTRYCOSTDEPTID": "DEPT", "EXPENSEDEPTID": "DEPT",
    "EXPENSEDEPTENTRYID": "DEPT", "DEPTID": "DEPT", "MANAGEDEPTID": "DEPT",
    "PROJECTID": "PROJECT", "PROJECTNO": "PROJECT",
    "DEPARTUREID": "LOCATION", "ARRIVALID": "LOCATION",
    "TRAVELSTARTID": "LOCATION", "TRAVELENDID": "LOCATION",
    "TRAVELSTARTSITE": "LOCATION", "TRAVELENDSITE": "LOCATION",
    "CITYID": "LOCATION", "REGIONID": "LOCATION", "COUNTRYID": "LOCATION",
    "STAFFID": "EMPLOYEE", "PROPOSERID": "EMPLOYEE", "APPLICANTID": "EMPLOYEE",
    "EMPLOYEEID": "EMPLOYEE", "CREATORID": "EMPLOYEE",
    "ORGID": "ORG", "APPLYORGID": "ORG", "PAYORGID": "ORG", "SETTLEORGID": "ORG",
    "MATERIALID": "MATERIAL", "CURRENCYID": "CURRENCY", "UNITID": "UNIT",
    "SUPPLIERID": "SUPPLIER", "CUSTOMERID": "CUSTOMER", "CONTACTUNIT": "UNIT",
}
# 中文标签兜底：二开字段（F_TJKT_Base_qtr 之类）认不出 Key 时按列名判断。
LABEL_KIND = {
    "费用项目": "EXPENSE_ITEM", "费用项目编码": "EXPENSE_ITEM",
    "费用承担部门": "DEPT", "部门": "DEPT", "申请部门": "DEPT", "付款部门": "DEPT",
    "项目": "PROJECT", "项目名称": "PROJECT",
    "出发地": "LOCATION", "出发地（费控）": "LOCATION", "出发地(费控)": "LOCATION",
    "目的地": "LOCATION", "目的地（费控）": "LOCATION", "目的地(费控)": "LOCATION",
    "申请人": "EMPLOYEE", "员工": "EMPLOYEE", "经办人": "EMPLOYEE",
    "组织": "ORG", "业务组织": "ORG", "申请组织": "ORG", "付款组织": "ORG",
    "物料": "MATERIAL", "币别": "CURRENCY", "单位": "UNIT",
    "供应商": "SUPPLIER", "客户": "CUSTOMER", "往来单位": "UNIT",
}
# 类别 -> 候选 (表单, 主键候选)。主键逐个试，记住成功的组合。
LOOKUPS = {
    "EXPENSE_ITEM": (("BD_ExpenseItem", ("FEXPENSEITEMID", "FID")),),
    "DEPT": (("BD_Department", ("FDEPTID", "FID")),),
    "PROJECT": (("BD_Project", ("FPROJECTID", "FID")),),
    "LOCATION": (("BD_City", ("FID",)), ("BD_Region", ("FID",)),
                 ("BD_Country", ("FID",)), ("BD_Site", ("FID",))),
    "EMPLOYEE": (("BD_Empinfo", ("FID", "FSTAFFID")),),
    "ORG": (("ORG_Organizations", ("FORGID", "FID")), ("BD_Organization", ("FID",))),
    "MATERIAL": (("BD_MATERIAL", ("FMATERIALID", "FID")),),
    "CURRENCY": (("BD_Currency", ("FCURRENCYID", "FID")),),
    "UNIT": (("BD_MeasureUnit", ("FUNITID", "FID")),),
    "SUPPLIER": (("BD_Supplier", ("FSUPPLIERID", "FID")),),
    "CUSTOMER": (("BD_Customer", ("FCUSTID", "FID")),),
}
# 源单类型这类字段的值是单据 FormId，直接换成中文。
FORM_VALUE_LABELS = {"源单类型", "源单单据类型", "单据类型"}
FORM_VALUE_KEYS = {"SRCBILLTYPEID", "SRCBILLTYPE", "SOURCEBILLTYPEID", "SRCTRANSTYPE",
                   "SOURCETYPE", "SOURCEBILLTYPE", "SRCFORMID"}
FORM_LABELS = {
    "AP_Payable": "应付单", "AR_receivable": "应收单", "AP_PAYBILL": "付款单",
    "CN_PAYAPPLY": "付款申请单", "AR_RECEIVEBILL": "收款单", "AR_REFUNDBILL": "退款单",
    "PUR_PurchaseOrder": "采购订单", "PUR_Requisition": "采购申请单",
    "SAL_SaleOrder": "销售订单", "STK_InStock": "采购入库单", "SAL_OUTSTOCK": "销售出库单",
    "ER_ExpenseRequest": "费用申请单", "ER_ExpenseRequest_Travel": "出差申请单",
    "ER_ExpReimbursement": "费用报销单", "ER_ExpReimbursement_Travel": "差旅报销单",
}
# 这些列审批时用不上，或用户明确不要显示。
SKIP_LABELS = {
    "源单行内码", "源单分录内码", "源单行号",
    "出发地（费控）", "出发地(费控)", "目的地（费控）", "目的地(费控)",
}

# 只有这些键才做「按列名猜」的兜底，避免把长文本列当基础资料。
_GUESS_SUFFIX = re.compile(r"(?:_?ID|_Id|_id)$,?")

_miss = set()          # (表单, 主键) 查不通，进程内不再重试
_formula_miss = set()  # (表单, 字段表达式) 查不通


def _display_name(value):
    """从金蝶值里取出可读名称：兼容多语言列表 [{"Key":2052,"Value":"中文"}]、
    基础资料对象 {"Name":...} 和普通字符串。只认非编码文本，失败返回空串。"""
    if value is None or isinstance(value, (bool, int, float)):
        return ""
    if isinstance(value, dict):
        for key in ("Name", "FName", "Value", "FValue"):
            got = _display_name(value.get(key))
            if got:
                return got
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict) and str(item.get("Key")) in ("2052", "2052.0"):
                got = _display_name(item.get("Value") or item.get("FValue"))
                if got:
                    return got
        for item in value:
            got = _display_name(item)
            if got:
                return got
        return ""
    text = str(value).strip()
    return "" if (not text or _code_like(text)) else text


def norm(key):
    """F_TJKT_Base_qtr / FExpenseItemID -> F_TJKT_BASE_QTR / EXPENSEITEMID"""
    text = str(key or "").strip()
    for suffix in ("_ID", "_Id", "_id", "_NAME", "_Name", "_name", "_H", "_h"):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            break
    if text.lower().startswith("f"):
        text = text[1:]
    return text.upper()


def _ident(value):
    """取编码/内码：支持字符串、数字、{"Number":..} / {"Id":..}。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        text = str(int(value)) if float(value).is_integer() else ""
        return text if text and text != "0" else ""
    if isinstance(value, dict):
        for key in ("Number", "FNumber", "Id", "FID", "FId"):
            text = _ident(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        for item in value:
            text = _ident(item)
            if text:
                return text
        return ""
    text = str(value).strip()
    if not text or text == "0":
        return ""
    return text


def _code_like(value):
    """是不是「编码」而不是名称/日期/金额。"""
    text = _ident(value)
    if not text or len(text) > 64:
        return False
    if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T].*)?", text):
        return False
    # 名称通常含中文或空格；编码是纯数字，或字母数字下划线短码。
    return bool(re.fullmatch(r"\d+|[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", text))


def _kind(key, label=""):
    n = norm(key)
    if n in FIELD_KIND:
        return FIELD_KIND[n]
    if label in LABEL_KIND:
        return LABEL_KIND[label]
    raw = str(key or "")
    # 二开基础资料常见写法：F_TJKT_Base_qtr（项目）、Fxxx_Id。
    if re.search(r"(?:^|_)Base(?:_|$)", raw, re.I) or _GUESS_SUFFIX.search(raw):
        return "GENERIC"
    return ""


def _sibling_name(row, key):
    # 先看值自身：金蝶常把 Name 放在同一个对象里 {"Number":"FY001","Name":[...]}。
    # 早前只看兄弟字段，字段值自带 Name 时白白走到查库、失败就留编码。
    own = _display_name(row.get(key))
    if own:
        return own
    raw = str(key)
    cands = [raw + "_Name", raw + "_NAME", raw + "__Name", raw + "_FName"]
    if raw.upper().endswith("ID"):
        cands += [raw[:-2] + "Name", raw[:-2] + "_Name", raw[:-2] + "_NAME"]
    for cand in cands:
        for k, v in row.items():
            if str(k).lower() == cand.lower():
                got = _display_name(v)
                if got:
                    return got
    # 任意键名归一后同字段（F_TJKT_Base_qtr / F_TJKT_BASE_QTR / Fexpenseitemid）。
    target = norm(key)
    for k, v in row.items():
        if str(k) == str(key) or norm(k) != target:
            continue
        got = _display_name(v)
        if got:
            return got
    return ""


def _query(kd, form, expr, where, top):
    try:
        rows = kd.query(form, expr, where, top)
        return rows if isinstance(rows, list) else []
    except (Exception, SystemExit):
        return []


def _row_cell(row, idx):
    if isinstance(row, list) and len(row) > idx:
        cell = row[idx]
        # FName 常返回 [{"Key":2052,"Value":"中文"}]，直接 str 会显示成字典串。
        return _display_name(cell) or str(cell or "").strip()
    return ""


def _by_formula(kd, form_id, fid, entry_keys, keys):
    """在单据上直接取 FEntity_Fxxx.FName，返回 {原始Key: [按分录顺序的名称]}。

    先批量查一次；某个二开字段表达式无效会拖垮整批，因此再逐字段换写法重试，
    保证费用项目/部门这类关键列不会因为无关字段而丢掉名称。
    """
    out = {}
    if not keys:
        return out
    where = "FID=" + str(fid)
    for entry in entry_keys:
        if not entry:
            continue
        exprs = []
        for key in keys:
            raw = str(key)
            expr = raw if raw.upper().startswith(entry.upper() + "_") else entry + "_" + raw
            exprs.append(expr + ".FName")
        rows = _query(kd, form_id, "FID," + ",".join(exprs), where, 500)
        if not rows:
            continue
        for i, key in enumerate(keys):
            names = [n if n and not _code_like(n) else ""
                     for n in (_row_cell(row, 1 + i) for row in rows)]
            if any(names):
                out[key] = names
        if len(out) == len(keys):
            break
    for key in keys:
        if key in out:
            continue
        raw = str(key)
        tries = []
        for entry in entry_keys:
            if entry:
                tries.append((raw if raw.upper().startswith(entry.upper() + "_")
                              else entry + "_" + raw) + ".FName")
        tries.append(raw + ".FName")
        for expr in dict.fromkeys(tries):
            rows = _query(kd, form_id, "FID," + expr, where, 500)
            if not rows:
                continue
            names = [n if n and not _code_like(n) else ""
                     for n in (_row_cell(row, 1) for row in rows)]
            if any(names):
                out[key] = names
                _formula_miss.discard((form_id, expr))
                break
            _formula_miss.add((form_id, expr))
    return out


def _by_base_data(kd, groups):
    """按基础资料表反查，返回 {(kind, ident): name}。

    ⚠️ 数字内码按主键查，字母数字编码（FY001 之类）必须按 FNumber 查；
    早前一律按主键查，导致费用申请/出差申请的编码字段反查不到名称。
    """
    found = {}
    for kind, idents in groups.items():
        numeric_ids = sorted(i for i in idents if i.isdigit())
        text_ids = sorted(i for i in idents if not i.isdigit())
        for form, pks in LOOKUPS.get(kind, ()):
            for pk in pks:
                if (form, pk) in _miss:
                    continue
                got, queried = {}, False
                for field, ids in ((pk, numeric_ids), ("FNumber", text_ids)):
                    if not ids:
                        continue
                    literal = (",".join(ids) if field != "FNumber" else
                               ",".join("'" + i.replace("'", "''") + "'" for i in ids))
                    rows = _query(kd, form, pk + ",FNumber,FName",
                                  field + " in (" + literal + ")", 500)
                    if rows:
                        queried = True
                    for row in rows:
                        if not isinstance(row, list) or len(row) < 3:
                            continue
                        name = _display_name(row[2])
                        if not name:
                            continue
                        for cell in row[:2]:
                            ident = str(cell or "").strip()
                            if ident:
                                got[ident] = name
                if got:
                    for ident in idents:
                        if ident in got:
                            found[(kind, ident)] = got[ident]
                    break
                if not queried:
                    _miss.add((form, pk))
            if any(k == kind for k, _ in found):
                break
    return found


def _apply_resolved(rows, resolved):
    out = deepcopy(list(rows or []))
    for (i, key), name in resolved.items():
        if name and 0 <= i < len(out) and isinstance(out[i], dict):
            out[i][key] = name
    return out


def resolve(rows, form_id="", fid="", kd=None, entry_keys=(), label_map=None):
    """返回补好名称的明细行（深拷贝，不改原数据）。

    entry_keys: View 上明细数组的键，如 ("FEntity", "ER_ExpenseReimbEntry")；也可传 ("",)，
    表示直接用字段名查单据表头。
    """
    sample = [r for r in (rows or []) if isinstance(r, dict)]
    if not sample or kd is None:
        return deepcopy(list(rows or []))
    raw_labels = detail_fields.labels(form_id) if label_map is None else label_map
    labels = {}
    for k, v in raw_labels.items():
        labels[norm(k)] = v
        labels[str(k).strip().upper()] = v
    candidates = {}   # 原始 key -> {kind, idents:{行号:编码}}
    resolved = {}     # (行号, 原始key) -> 名称
    for i, row in enumerate(sample):
        for key, value in row.items():
            n = norm(key)
            label = labels.get(n) or labels.get(str(key).strip().upper()) or ""
            if label in SKIP_LABELS:
                continue
            if not _code_like(value):
                continue
            if label in FORM_VALUE_LABELS or n in FORM_VALUE_KEYS:
                text = str(value).strip()
                name = FORM_LABELS.get(text) or FORM_LABELS.get(text.split("_")[-1]) or ""
                if name:
                    resolved[(i, key)] = name
                continue
            kind = _kind(key, label)
            if not kind:
                continue
            info = candidates.setdefault(key, {"kind": kind, "cells": {}})
            info["cells"][i] = _ident(value)
    if not candidates:
        return _apply_resolved(rows, resolved)

    # 1) 同行兄弟字段已有名称的，直接用。
    todo = {}
    for key, info in candidates.items():
        for i, ident in info["cells"].items():
            name = _sibling_name(sample[i], key)
            if name:
                resolved[(i, key)] = name
            else:
                todo.setdefault(key, []).append(i)

    # 2) 单据字段表达式批量取名称。
    entries = [e for e in entry_keys if e] + [""]
    if todo:
        by_formula = _by_formula(kd, form_id, fid, entries, list(todo))
        for key, names in by_formula.items():
            for i in todo.get(key, []):
                if i < len(names) and names[i]:
                    resolved[(i, key)] = names[i]

    # 3) 还查不到的，按基础资料表反查。
    pending = {}
    for key, indexes in todo.items():
        kind = candidates[key]["kind"]
        for i in indexes:
            if (i, key) in resolved:
                continue
            ident = candidates[key]["cells"][i]
            pending.setdefault(kind, set()).add(ident)
    if pending:
        names = _by_base_data(kd, pending)
        for key, indexes in todo.items():
            kind = candidates[key]["kind"]
            for i in indexes:
                if (i, key) in resolved:
                    continue
                ident = candidates[key]["cells"][i]
                name = names.get((kind, ident))
                if name:
                    resolved[(i, key)] = name

    return _apply_resolved(rows, resolved)
