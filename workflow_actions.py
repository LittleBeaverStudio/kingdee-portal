"""Current-user workflow assignments and native WorkflowAudit operations."""
import json
import re
import threading
from contextlib import contextmanager

import portal_actions as actions
from list_filters import quote

# 费用域表单：字段映射已逐项适配，详情弹窗走精细渲染。
EXPENSE_FORMS = {
    "ER_ExpenseRequest": ("费用申请", False, True),
    "ER_ExpenseRequest_Travel": ("出差申请", True, True),
    "ER_ExpReimbursement": ("费用报销", False, False),
    "ER_ExpReimbursement_Travel": ("差旅报销", True, False),
}
# 兼容旧引用的别名：这份表只决定「是否精细渲染」，不再决定「能否出现在待办里」。
FORMS = EXPENSE_FORMS
# 常见表单中文名。不在表里的单据显示表单标识本身，照常可以审批。
KNOWN_FORMS = {
    "CN_PAYAPPLY": "付款申请单",
    "AP_PAYBILL": "付款单",
    "AR_RECEIVEBILL": "收款单",
    "AR_REFUNDBILL": "退款单",
    "AP_Payable": "应付单",
    "AR_receivable": "应收单",
    "PUR_PurchaseOrder": "采购订单",
    "PUR_Requisition": "采购申请单",
    "SAL_SaleOrder": "销售订单",
    "STK_InStock": "采购入库单",
    "SAL_OUTSTOCK": "销售出库单",
    "BD_MATERIAL": "物料",
    "BD_Supplier": "供应商",
    "BD_Customer": "客户",
    "ORG_Organizations": "组织机构",
}
FORM_LABELS = {**{k: v[0] for k, v in EXPENSE_FORMS.items()}, **KNOWN_FORMS}
FIELDS = ("FASSIGNID,FTitle,FCreateTime,FReceiverId,FBILLNUMBER,FObjectTypeId,"
          "FKEYVALUE,FStatus,FACTNAME,FOrgId.FNumber,FOrgId.FName,FSENDERID.FName,FContent")
_locks = {}
_guard = threading.Lock()


def user_id(value):
    value = str(value or "")
    if not value.isdigit() or int(value) <= 0:
        raise ValueError("无法确认当前审核人身份，请退出门户后重新登录")
    return value


def assignment_id(value):
    value = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("待办任务编号无效")
    return value


def _scope(uid):
    # 不再按单据类型过滤：任何派给本人的工作流任务都要能看见、能办理。
    return "FReceiverId=" + user_id(uid)


def _rows(kd, where, limit=201):
    raw = kd.query("WF_AssignmentBill", FIELDS, where, top=limit, order="FCreateTime DESC")
    out = []
    for r in raw:
        if len(r) != len(FIELDS.split(",")):
            raise RuntimeError("金蝶待办数据不完整，请刷新重试")
        aid, title, created, receiver, number, form, fid, status, node, org, orgname, sender, content = r
        form = str(form or "").strip()
        if not form:
            raise RuntimeError("金蝶待办缺少单据类型，请刷新重试")
        out.append(dict(assignment_id=str(aid), title=str(title or ""), created=str(created or "")[:19].replace("T", " "),
                        receiver_id=str(receiver), bill_no=str(number or "").strip(), form_id=form,
                        fid=str(fid), status=str(status), node=str(node or ""), org_no=str(org or ""),
                        org_name=str(orgname or ""), sender=str(sender or ""), content=str(content or ""),
                        kind=FORM_LABELS.get(form, form),
                        # 有单据内码才能读取详情；基础资料类审批没有单据内码，只显示不可办理。
                        can_open=str(fid or "").strip().isdigit(),
                        supported=form in EXPENSE_FORMS))
    return out


def list_tasks(configured, uid, orgs=None, limit=201):
    uid = user_id(uid)
    kd, _, _ = actions.get_client(configured)
    where = _scope(uid) + " AND FStatus=0"
    if orgs:
        where += " AND FOrgId.FNumber in (" + ",".join(quote(o) for o in orgs) + ")"
    rows = _rows(kd, where, limit)
    if any(r["receiver_id"] != uid or r["status"] != "0" for r in rows):
        raise RuntimeError("待办归属或状态核对失败，请重新登录")
    return rows


def _task(kd, uid, aid, pending=True):
    where = _scope(uid) + " AND FASSIGNID=" + quote(assignment_id(aid))
    if pending:
        where += " AND FStatus=0"
    rows = _rows(kd, where, 2)
    if len(rows) != 1 or rows[0]["receiver_id"] != user_id(uid) or rows[0]["assignment_id"] != aid:
        raise ValueError("该待办已处理、已转交或不属于你，请刷新待办列表")
    if pending and rows[0]["status"] != "0":
        raise ValueError("该待办已处理，请刷新列表")
    if not rows[0]["fid"].isdigit():
        raise ValueError("无法识别待办对应的单据内码")
    return rows[0]


def detail(configured, uid, aid):
    kd, _, _ = actions.get_client(configured)
    task = _task(kd, uid, assignment_id(aid))
    if task["form_id"] in EXPENSE_FORMS:
        _, travel, application = EXPENSE_FORMS[task["form_id"]]
        bill = (actions.request_detail(configured, task["fid"], travel) if application
                else actions.get_bill(configured, task["fid"], travel))
        if str(bill.get("bill_no") or "").strip() != task["bill_no"]:
            raise RuntimeError("待办与单据编号不一致，请刷新后重试")
        return {"task": task, "bill": bill, "is_application": application, "render": "expense"}
    bill = generic_detail(configured, task["form_id"], task["fid"], task["kind"])
    number = str(bill.get("bill_no") or "").strip()
    if number and number != task["bill_no"]:
        raise RuntimeError("待办与单据编号不一致，请刷新后重试")
    # 待办表自带组织编码/名称，比从单据上猜更可靠。
    if not bill.get("org_name"):
        bill["org_no"] = task.get("org_no") or ""
        bill["org_name"] = task.get("org_name") or ""
    return {"task": task, "bill": bill, "is_application": False, "render": "generic"}


# 通用详情：金蝶 View 返回的键是**大写带 F 前缀**（FBILLNO / FPAYAMOUNTFOR_H），
# 基础资料还拆成 Fxxx_Id / Fxxx_Name。字典一律按「去 F、去 _H、全大写」维护，
# 查表前用 _norm 归一化，避免大小写和前缀差异导致整块字段认不出来。
FIELD_LABELS = {
    "BILLNO": "单据编号", "DOCUMENTSTATUS": "单据状态", "DATE": "单据日期",
    "CREATEDATE": "创建日期", "MODIFYDATE": "修改日期", "APPROVEDATE": "审核日期",
    "BILLTYPEID": "单据类型", "APPLYORGID": "申请组织", "PAYORGID": "付款组织",
    "SETTLEORGID": "结算组织", "ORGID": "业务组织", "SALEORGID": "销售组织",
    "PURCHASEORGID": "采购组织", "STOCKORGID": "库存组织", "RECEIVEORGID": "收款组织",
    "DEPTID": "部门", "APPLYDEPTID": "申请部门", "PAYDEPTID": "付款部门",
    "EXPENSEDEPTID": "费用承担部门", "STOCKDEPTID": "库存部门",
    "STAFFID": "申请人", "PROPOSERID": "申请人", "APPLICANTID": "申请人",
    "CREATORID": "创建人", "MODIFIERID": "修改人", "APPROVERID": "审核人",
    "CONTACTUNIT": "往来单位", "RECTUNIT": "收款单位", "SUPPLIERID": "供应商",
    "CUSTOMERID": "客户", "SALERID": "销售员", "BUYERID": "采购员",
    "CONTACTUNITTYPE": "往来单位类型", "RECTUNITTYPE": "收款单位类型",
    "APPLYAMOUNTFOR": "申请金额", "PAYAMOUNTFOR": "付款金额", "AMOUNT": "金额",
    "AMOUNTFOR": "金额", "BILLALLAMOUNT": "价税合计", "TOTALAMOUNT": "总金额",
    "ALLAMOUNT": "总金额", "EXPAMOUNTSUM": "报销金额", "ORGAMOUNTSUM": "申请金额",
    "TAXAMOUNT": "税额", "TAXRATE": "税率", "CURRENCYID": "币别",
    "EXCHANGERATE": "汇率", "PAYPURPOSEID": "付款用途", "EXPECTPAYDATE": "预计付款日期",
    "COSTID": "费用项目", "EXPENSEITEMID": "费用项目", "PROJECTID": "项目",
    "SETTLETYPEID": "结算方式", "PAYCONDITION": "付款条件",
    "PAYTYPEID": "付款方式", "SRCBILLNO": "源单编号", "SRCBILLTYPEID": "源单类型",
    "CLOSESTATUS": "关闭状态", "CANCELSTATUS": "作废状态", "REASON": "事由",
    "REMARK": "备注", "NOTE": "备注", "COMMENT": "备注", "DESCRIPTION": "说明",
    "MATERIALID": "物料", "MATERIALNAME": "物料名称", "SPECIFICATION": "规格型号",
    "UNITID": "单位", "QTY": "数量", "PRICE": "单价", "SEQ": "序号",
    "EACHBANKACCOUNT": "收款银行账号", "EACHBANKNAME": "收款银行", "EACHCCOUNTNAME": "收款户名",
    "SETTLEBANKACCOUNT": "结算账号", "PAYBANKACCOUNT": "付款账号",
    "ID": "内码",
}
AMOUNT_KEYS = ("APPLYAMOUNTFOR", "PAYAMOUNTFOR", "BILLALLAMOUNT", "TOTALAMOUNT", "ALLAMOUNT",
               "AMOUNT", "AMOUNTFOR", "APPLYAMOUNT", "PAYAMOUNT", "EXPAMOUNTSUM", "ORGAMOUNTSUM")
APPLICANT_KEYS = ("STAFFID", "PROPOSERID", "APPLICANTID", "CREATORID")
ORG_KEYS = ("APPLYORGID", "PAYORGID", "ORGID", "SETTLEORGID", "SALEORGID", "PURCHASEORGID")
# 摘要已经展示，或按用户要求不展示的字段（日期只留单据日期）。
SKIP_FIELDS = {"ID", "BILLNO", "DOCUMENTSTATUS", "CREATEDATE", "MODIFYDATE", "APPROVEDATE",
               "BILLTYPEID", "EXCHANGERATE", "CLOSESTATUS", "CANCELSTATUS",
               "CONTACTUNITTYPE", "RECTUNITTYPE"}
# 明细里明确不展示的列（银行账号等信息量大但审批时用不上）。
DETAIL_SKIP = {"ID", "EACHBANKACCOUNT", "SETTLEBANKACCOUNT", "PAYBANKACCOUNT"}
# 明细列的业务优先级：序号最前，费用项目/承担部门/项目紧跟，其余按字典顺序。
DETAIL_FIRST = {"SEQ": 0, "EXPENSEITEMID": 1, "COSTID": 2, "EXPENSEDEPTID": 3, "PROJECTID": 4}
# 表头展示优先级：审批时最需要看的先出现，避免被 12 条上限截掉。
FIELD_FIRST = {"DATE": 0, "EXPENSEDEPTID": 1, "COSTID": 2, "PROJECTID": 3,
               "PAYPURPOSEID": 4, "CONTACTUNIT": 5, "RECTUNIT": 6,
               "APPLYAMOUNTFOR": 7, "PAYAMOUNTFOR": 8, "AMOUNT": 9,
               "APPLYORGID": 10, "PAYORGID": 11}
# 基础资料内码 -> 需要反查名称的类别。金蝶 View 常只回内码，不反查就是一片数字。
FIELD_KIND = {
    "APPLYORGID": "ORG", "PAYORGID": "ORG", "ORGID": "ORG", "SETTLEORGID": "ORG",
    "SALEORGID": "ORG", "PURCHASEORGID": "ORG", "RECEIVEORGID": "ORG", "STOCKORGID": "ORG",
    "CREATORID": "PERSON", "MODIFIERID": "PERSON", "APPROVERID": "PERSON",
    "STAFFID": "PERSON", "PROPOSERID": "PERSON", "APPLICANTID": "PERSON",
    "DEPTID": "DEPT", "APPLYDEPTID": "DEPT", "PAYDEPTID": "DEPT",
    "EXPENSEDEPTID": "DEPT", "STOCKDEPTID": "DEPT",
    "COSTID": "ITEM", "EXPENSEITEMID": "ITEM", "PROJECTID": "PROJECT",
    "CONTACTUNIT": "UNIT", "RECTUNIT": "UNIT",
    "SETTLETYPEID": "SETTLE", "CURRENCYID": "CURRENCY",
    "SUPPLIERID": "SUPPLIER", "CUSTOMERID": "CUSTOMER",
}
# 每个类别可尝试的（表单, 主键候选）。表单标识在不同版本/二开环境下会变，逐个试并缓存结果。
LOOKUPS = {
    "ORG": (("ORG_Organizations", ("FOrgId", "FOrgID", "FID")),
            ("BD_Organization", ("FOrgId", "FID"))),
    "PERSON": (("BD_Empinfo", ("FID", "FStaffId")),),
    "DEPT": (("BD_Department", ("FDEPTID", "FDeptId", "FID")),),
    "ITEM": (("BD_ExpenseItem", ("FEXPENSEITEMID", "FID")),),
    "PROJECT": (("BD_Project", ("FPROJECTID", "FID")),),
    "UNIT": (("BD_Customer", ("FCUSTID", "FID")), ("BD_Supplier", ("FSUPPLIERID", "FID"))),
    "CUSTOMER": (("BD_Customer", ("FCUSTID", "FID")),),
    "SUPPLIER": (("BD_Supplier", ("FSUPPLIERID", "FID")),),
    "SETTLE": (("BD_SettleType", ("FSETTLETYPEID", "FID")),),
    "CURRENCY": (("BD_Currency", ("FCURRENCYID", "FID")),),
}
_lookup_cache = {}
_lookup_miss = set()
_lookup_notes = []
_probe_expr = {}
# 这些单据固定保留费用归集三列，即使当前单据没填，审批时也要能看到口径。
FORCE_COLUMNS_FORMS = {"CN_PAYAPPLY", "AP_PAYBILL"}
# 字段名取自 kingdee-data-exporter 的 field-docs（该租户二开较多：
# 备注、项目都是自定义字段，标准字段名取不到）。
# 付款申请单没有独立的「申请人」，业务上用创建人。
FORM_FIELDS = {
    "CN_PAYAPPLY": {
        "org": "FAPPLYORGID",
        "applicant": "FCREATORID",
        "remark": "F_TJKT_Text_qtr1",
        "date": "FDATE",
        "amount": "FAPPLYAMOUNTFOR_H",
        "project": "F_TJKT_Base_83g_re5_tzk",
    },
}


def _norm(key):
    """FPAYAMOUNTFOR_H / FPayAmountForH / PayAmountForH -> PAYAMOUNTFOR"""
    text = str(key or "").strip()
    for suffix in ("_ID", "_Id", "_id", "_NAME", "_Name", "_name"):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            break
    if len(text) > 1 and text[0] in ("F", "f"):
        text = text[1:]
    upper = text.upper()
    if upper.endswith("_H"):
        text = text[:-2]          # FPAYAMOUNTFOR_H 本位币
    elif upper.endswith("FORH"):
        text = text[:-1]          # PayAmountForH 驼峰写法
    return text.upper()


def _text(value):
    """把 View 返回的多语言名/基础资料对象压成一行可显示文本。失败返回空串，不抛错。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        # 6 位小数足够财务口径，同时避免 %g 把 12345.67 截成 12345.7。
        return ("%.6f" % value).rstrip("0").rstrip(".")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        # 金蝶日期常带 T00:00:00，只留日期部分。
        return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.]+", text) else text
    if isinstance(value, dict):
        for key in ("Name", "FName"):
            text = _text(value.get(key))
            if text:
                return text
        for key in ("Number", "FNumber"):
            if key in value and not isinstance(value[key], (dict, list)):
                text = _text(value[key])
                if text:
                    return text
        return ""
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and str(item.get("Key")) == "2052":
                return _text(item.get("Value"))
        for item in value:
            text = _text(item)
            if text:
                return text
    return ""


def _amount(value):
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _label(key):
    return FIELD_LABELS.get(_norm(key), key)


def _index(data):
    """把 View 结果归一化成 {NORMKEY: value}，让 Fxxx_Id / Fxxx_Name / 嵌套对象都能取到。"""
    out, names = {}, {}
    for key, value in data.items():
        name = str(key)
        norm = _norm(name)
        if not norm:
            continue
        if name.upper().endswith("_NAME"):
            if _text(value):
                names[norm] = value
        elif norm not in out or not _text(out[norm]):
            out[norm] = value
    out.update(names)
    return out


def _readable(key, value):
    """纯内码的基础资料字段（如 ApplyOrgId=101）没有可读名称时不要硬显示。"""
    text = _text(value)
    if not text:
        return ""
    if _norm(key).endswith("ID") and text.isdigit():
        return ""
    return text


def _id_text(value):
    """字段值是不是一个基础资料内码（整数或 GUID）；是则返回，否则空串。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        return ""
    text = str(value).strip()
    if text.isdigit():
        return text
    if len(text) >= 16 and re.fullmatch(r"[0-9a-fA-F][0-9a-fA-F-]{15,39}", text):
        return text
    return ""


def _pk_candidates(form):
    base = form.split("_", 1)[-1]
    return ("FID", "F" + base + "ID", "F" + base.upper() + "ID")


def _query_names(kd, form, pks, ids):
    """一个表单里按主键批量取名称；失败的表单/主键组合记住，不再重复探测。"""
    numeric = all(i.isdigit() for i in ids)
    literal = ",".join(ids) if numeric else ",".join(quote(i) for i in ids)
    for pk in pks:
        if (form, pk) in _lookup_miss:
            continue
        try:
            # 第 4 个位置参数：vendor Kingdee 叫 top，kd_client 叫 limit，位置传参两边通用。
            rows = kd.query(form, pk + ",FNumber,FName", pk + " in (" + literal + ")", 500)
            out = {}
            for row in rows or []:
                if isinstance(row, list) and len(row) >= 3:
                    name = str(row[2] or "").strip() or str(row[1] or "").strip()
                    if name:
                        out[str(row[0])] = name
        except (Exception, SystemExit) as exc:     # noqa: BLE001 - 表单不存在就换下一个候选
            _lookup_miss.add((form, pk))
            if len(_lookup_notes) < 6:
                _lookup_notes.append(form + "." + pk + "：" + str(exc)[:80])
            continue
        if out:
            return out
    return {}


def _names(kd, kind, ids):
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    cached = _lookup_cache.get(kind)
    for form, pks in ([cached] if cached else LOOKUPS.get(kind, ())):
        found = _query_names(kd, form, pks, ids)
        if found:
            _lookup_cache[kind] = (form, pks)
            return found
    return {}


def _fetch(kd, form_id, fid, expr):
    """按字段表达式直接查单据取一个值，缓存「哪个表达式有效」而不是值。

    先试 `.FName`（基础资料取名称），再试 `.FNumber`，最后试原字段（文本字段）。
    """
    key = (form_id, expr)
    known = _probe_expr.get(key)
    order = (expr + ".FName", expr + ".FNumber", expr)
    first_error = ""
    for candidate in ([known] if known else order):
        try:
            rows = kd.query(form_id, candidate, "FID=" + str(fid), 1)
        except (Exception, SystemExit) as exc:     # noqa: BLE001 - 换下一个写法
            first_error = first_error or (form_id + "." + candidate + "：" + str(exc)[:60])
            continue
        if isinstance(rows, list) and rows and isinstance(rows[0], list) and rows[0]:
            _probe_expr[key] = candidate
            return _text(rows[0][0])
        if "." not in candidate:
            # 原字段本身能查通、只是没值：判定为字段有效但为空。
            _probe_expr[key] = candidate
            return ""
        # 子属性没值：多半是文本字段，继续试下一种写法。
    _probe_expr[key] = ""
    if first_error and len(_lookup_notes) < 8:
        _lookup_notes.append(first_error)
    return ""


def _header_names(kd, form_id, fid, data, norms):
    """用单据自身的 Fxxx.FName 取名称：不依赖基础资料表名，权限跟随单据。

    ExecuteBillQuery 支持在单据表上直接取 `FAPPLYORGID.FName`。一次查全部字段，
    失败时退化为逐字段，避免一个多态字段拖垮整批。
    """
    bases = {}
    for key in data:
        name = str(key)
        base = name
        for suffix in ("_Id", "_ID", "_Name", "_NAME"):
            if base.endswith(suffix):
                base = base[:-len(suffix)]
                break
        if base[:1] in ("F", "f"):
            bases.setdefault(_norm(name), base)
    picked = [(n, bases[n]) for n in norms if n in bases]
    if not picked:
        return {}
    where = "FID=" + str(fid)
    fields = ["FID"] + [b + ".FName" for _, b in picked]
    try:
        rows = kd.query(form_id, ",".join(fields), where, 1)
        if rows and isinstance(rows[0], list) and len(rows[0]) == len(fields):
            return {n: str(v).strip() for (n, _), v in zip(picked, rows[0][1:]) if str(v or "").strip()}
    except (Exception, SystemExit) as exc:         # noqa: BLE001 - 有字段不支持就逐个试
        if len(_lookup_notes) < 6:
            _lookup_notes.append(form_id + " 表头批量取名称：" + str(exc)[:80])
    out = {}
    for norm, base in picked:
        try:
            rows = kd.query(form_id, base + ".FName", where, 1)
        except (Exception, SystemExit) as exc:     # noqa: BLE001
            if len(_lookup_notes) < 6:
                _lookup_notes.append(form_id + "." + base + ".FName：" + str(exc)[:80])
            continue
        if rows and isinstance(rows[0], list) and rows[0] and str(rows[0][0] or "").strip():
            out[norm] = str(rows[0][0]).strip()
    return out


def _resolve_all(kd, form_id, fid, index, data):
    """把表头里的基础资料内码批量换成名称。返回 {字段名: 显示文本}。"""
    resolved = {}
    norms = [k for k in FIELD_KIND if _id_text(index.get(k))]
    if norms:
        resolved.update(_header_names(kd, form_id, fid, data, norms))
    unit_type = _text(index.get("CONTACTUNITTYPE"))
    rect_type = _text(index.get("RECTUNITTYPE"))
    wanted, direct = {}, {}
    for key, kind in FIELD_KIND.items():
        if key in resolved:
            continue
        ident = _id_text(index.get(key))
        if not ident:
            continue
        if kind == "UNIT":
            form = unit_type if unit_type.startswith("BD_") else (
                rect_type if key == "RECTUNIT" and rect_type.startswith("BD_") else "")
            if form:
                direct.setdefault((form, ident), []).append(key)
                continue
        wanted.setdefault(kind, []).append((key, ident))
    for kind, pairs in wanted.items():
        names = _names(kd, kind, [i for _, i in pairs])
        for key, ident in pairs:
            if names.get(ident):
                resolved[key] = names[ident]
    for (form, ident), keys in direct.items():
        names = _query_names(kd, form, _pk_candidates(form), [ident])
        for key in keys:
            if names.get(ident):
                resolved[key] = names[ident]
    return resolved


def _display(key, value, resolved):
    text = _text(value)
    if key in resolved:
        return resolved[key]
    if key in FIELD_KIND and _id_text(value):
        return ""                       # 只有内码、又反查不到名称：宁可不显示
    return text


def _entries(rows, kd=None):
    """从 View 的第一个明细数组里猜出表格：同名字段只留一列，最多 8 列 80 行。"""
    sample = [r for r in rows if isinstance(r, dict)][:80]
    if not sample:
        return None
    pending = {}
    for row in sample:
        for key, cell in row.items():
            kind = FIELD_KIND.get(_norm(key))
            ident = _id_text(cell)
            if kind and ident:
                pending.setdefault(kind, set()).add(ident)
    names = {}
    if kd:
        for kind, ids in pending.items():
            for ident, name in _names(kd, kind, ids).items():
                names[(kind, ident)] = name

    def cell_text(key, cell):
        kind = FIELD_KIND.get(_norm(key))
        ident = _id_text(cell)
        if kind and ident:
            return names.get((kind, ident), "")
        return _text(cell)

    hit = {}
    for row in sample:
        for key, cell in row.items():
            if _norm(key) == "ID" or (isinstance(cell, list) and key in ("Name", "FName")):
                continue
            if cell_text(key, cell):
                hit[key] = hit.get(key, 0) + 1
    if not hit:
        return None
    first = list(sample[0])
    # 同一中文标签只留一列：Fxxx 与 Fxxx_Id 同时回传时保留能读出名称的那列。
    groups = {}
    for key in hit:
        groups.setdefault(_label(key), []).append(key)
    picked = []
    for keys in groups.values():
        if all(_norm(k) in DETAIL_SKIP for k in keys):
            continue
        usable = [k for k in keys if _norm(k) not in DETAIL_SKIP]
        picked.append(next((k for k in usable if not _id_text(sample[0].get(k))), usable[0]))
    order = {k: i for i, k in enumerate(FIELD_LABELS)}
    picked.sort(key=lambda k: (0 if _label(k) != k else 1, DETAIL_FIRST.get(_norm(k), 50),
                               order.get(_norm(k), 999), -hit[k],
                               first.index(k) if k in first else 999))
    keys = picked[:8]
    columns = [dict(t=_label(k)) for k in keys]
    body = []
    for row in sample:
        line = [cell_text(k, row.get(k)) for k in keys]
        if any(line):
            body.append(line)
    return dict(columns=columns, rows=body) if body else None


def generic_detail(configured, form_id, fid, kind=""):
    """任意单据的只读详情：用通用 View 接口读全字段，不依赖逐表单适配。

    审批动作本身只需要 FormId + 内码，与这里读到多少字段无关；读不到时抛错，
    由上层提示「在金蝶查看」，不会影响审批按钮。
    """
    form_id = str(form_id or "").strip()
    if not form_id or not str(fid).isdigit():
        raise ValueError("无法识别该待办对应的单据")
    kd, _, _ = actions.get_client(configured)
    data = kd.view(form_id, str(fid))
    if not isinstance(data, dict) or not data:
        raise RuntimeError("金蝶未返回该单据内容（类型未适配或无权查看），请到金蝶核对")
    index = _index(data)
    _lookup_notes.clear()
    resolved = _resolve_all(kd, form_id, str(fid), index, data)
    label_order = {k: i for i, k in enumerate(FIELD_LABELS)}
    picked = []
    for key, label in FIELD_LABELS.items():
        if key in SKIP_FIELDS:
            continue
        text = _display(key, index.get(key), resolved)
        if text:
            picked.append((key, dict(label=label, value=text)))
    picked.sort(key=lambda x: (FIELD_FIRST.get(x[0], 50), label_order.get(x[0], 999)))
    fields = [item for _, item in picked[:12]]
    amount = None
    for key in AMOUNT_KEYS:
        amount = _amount(index.get(key))
        if amount is not None:
            break
    applicant = next((_display(k, index.get(k), resolved) for k in APPLICANT_KEYS
                      if _display(k, index.get(k), resolved)), "")
    org_key = next((k for k in ORG_KEYS if _display(k, index.get(k), resolved)), None)
    org_no, org_name = "", (_display(org_key, index.get(org_key), resolved) if org_key else "")
    org_raw = index.get(org_key) if org_key else None
    if isinstance(org_raw, dict):
        org_no = _text(org_raw.get("Number")) or _text(org_raw.get("FNumber"))
    # 二开单据的字段名和标准字段对不上，按字段文档兜底再取一遍。
    spec = FORM_FIELDS.get(form_id, {})
    if spec:
        if not applicant and spec.get("applicant"):
            applicant = _fetch(kd, form_id, str(fid), spec["applicant"])
        if not org_name and spec.get("org"):
            org_name = _fetch(kd, form_id, str(fid), spec["org"])
    project = _fetch(kd, form_id, str(fid), spec["project"]) if spec.get("project") else ""
    entries = None
    for value in data.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            entries = _entries(value, kd)
            if entries:
                break
    if entries:
        # 明细没带费用归集字段时用表头的值补列，然后统一排到「序号」之后。
        cols = entries["columns"]
        have = [c["t"] for c in cols]
        force = form_id in FORCE_COLUMNS_FORMS
        for key in ("COSTID", "EXPENSEDEPTID", "PROJECTID"):
            label = FIELD_LABELS[key]
            if label in have:
                continue
            text = _display(key, index.get(key), resolved)
            if key == "PROJECTID" and not text:
                text = project
            if not text and not force:
                continue
            cols.append({"t": label})
            for row in entries["rows"]:
                row.append(text or "")
            have.append(label)
        wanted = (FIELD_LABELS["COSTID"], FIELD_LABELS["EXPENSEDEPTID"], FIELD_LABELS["PROJECTID"])

        def rank(name):
            if name == "序号":
                return 0
            return wanted.index(name) + 1 if name in wanted else 9

        order = sorted(range(len(cols)), key=lambda i: (rank(cols[i]["t"]), i))
        if order != list(range(len(cols))):
            entries["columns"] = [cols[i] for i in order]
            entries["rows"] = [[row[i] for i in order] for row in entries["rows"]]
    status = _text(index.get("DOCUMENTSTATUS"))
    remark = next((_text(index.get(k)) for k in ("REMARK", "COMMENT", "NOTE") if _text(index.get(k))), "")
    if not remark and spec.get("remark"):
        remark = _fetch(kd, form_id, str(fid), spec["remark"])
    return {
        "bill_no": _text(index.get("BILLNO")),
        "form_id": form_id,
        "kind": kind or FORM_LABELS.get(form_id, form_id),
        "status": status,
        "status_cn": actions.BILL_STATUS_CN.get(status, status),
        "date": _text(index.get("DATE")),
        "applicant": applicant,
        "org_no": org_no,
        "org_name": org_name,
        "amount": amount,
        "remark": remark,
        "lookup_error": "" if applicant else "；".join(_lookup_notes[:2])[:200],
        "fields": fields[:12],
        "entries": entries,
        "generic": True,
    }


@contextmanager
def _single_action(key):
    with _guard:
        lock, count = _locks.get(key, (threading.Lock(), 0))
        _locks[key] = (lock, count + 1)
    acquired = lock.acquire(blocking=False)
    try:
        if not acquired:
            raise ValueError("该单据正在审批，请等待结果后刷新")
        yield
    finally:
        if acquired:
            lock.release()
        with _guard:
            _, count = _locks[key]
            if count == 1:
                del _locks[key]
            else:
                _locks[key] = (lock, count - 1)


def decide(configured, uid, aid, decision):
    uid, aid = user_id(uid), assignment_id(aid)
    if decision not in ("approve", "reject"):
        raise ValueError("只支持同意或驳回")
    kd, rl, _ = actions.get_client(configured)
    task = _task(kd, uid, aid)
    key = (str(getattr(kd, "base", "")), str(kd.cfg.get("acctid") or ""), uid, task["form_id"], task["fid"])
    with _single_action(key):
        task = _task(kd, uid, aid)
        # WorkflowAudit identifies a document and reviewer, not an assignment.
        # Refuse ambiguous simultaneous tasks for the same reviewer/document.
        peers = _rows(kd, _scope(uid) + " AND FStatus=0 AND FObjectTypeId=" + quote(task["form_id"])
                      + " AND FKEYVALUE=" + quote(task["fid"]), 3)
        if len(peers) != 1 or peers[0]["assignment_id"] != aid:
            raise ValueError("同一单据存在多个待办节点，无法唯一确定本次审批，请在金蝶核对")
        data = {"FormId": task["form_id"], "Ids": task["fid"], "Numbers": [],
                "UserId": int(uid), "ApprovalType": 1 if decision == "approve" else 2}
        # WorkflowAudit 是 DynamicFormService 的 special 方法，外层只带 data。
        # 各版本对 Ids/Numbers 的类型要求不一致（有的要字符串、有的要数组），
        # 命中「请传入Ids或Numbers参数」说明参数没被接受、审批尚未执行，可安全换写法再试。
        attempts = [data,
                    dict(data, Ids=[str(task["fid"])]),
                    dict(data, Ids="", Numbers=[task["bill_no"]]) if task["bill_no"] else None]
        status, message = {}, ""
        for payload in [x for x in attempts if x]:
            try:
                raw = kd._post(rl.KSVC_SUFFIX + "WorkflowAudit.common.kdsvc",
                               {"data": json.dumps(payload, ensure_ascii=False)}, retries=1, timeout=90)
            except (Exception, SystemExit) as exc:
                raise RuntimeError("审批结果未确认，请刷新待办并核对金蝶审批记录，勿重复操作") from exc
            status = ((raw.get("Result") or {}).get("ResponseStatus") or {}) if isinstance(raw, dict) else {}
            if status.get("IsSuccess") is True:
                break
            message = "；".join(str(e.get("Message") or "") for e in status.get("Errors", []) if isinstance(e, dict))
            if "Ids" not in message and "Numbers" not in message:
                break
        if status.get("IsSuccess") is not True:
            return {"ok": False, "assignment_id": aid, "message": message or "金蝶未确认审批成功，请刷新并核对审批记录"}
        try:
            after = _rows(kd, _scope(uid) + " AND FASSIGNID=" + quote(aid), 2)
        except (Exception, SystemExit) as exc:
            raise RuntimeError("审批已返回成功，但任务状态未确认，请刷新并核对审批记录，勿重复操作") from exc
        # 审批通过后任务会离开本表（转下一节点或归档到 WF_AssignmentHisBill），
        # 查不到就是已完成；只有「还留在待办且不是已完成」才算异常。
        if after and (len(after) != 1 or after[0]["receiver_id"] != uid or after[0]["status"] != "1"):
            raise RuntimeError("审批已返回成功，但任务完成状态未确认，请刷新并核对审批记录，勿重复操作")
        return {"ok": True, "assignment_id": aid, "bill_no": task["bill_no"], "form_id": task["form_id"],
                "decision": decision, "task_status": "1", "node": task["node"]}
