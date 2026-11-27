"""Current-user workflow assignments and native WorkflowAudit operations."""
import json
import re
import threading
from contextlib import contextmanager

import detail_fields
import detail_names
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


# 列表摘要字段：不同单据的金额/事由字段名不统一，按 form 探测一次后缓存。
_BRIEF_EXPR = {}
_BRIEF_AMOUNT = ("FAPPLYAMOUNTFOR_H", "FAPPLYAMOUNTFOR", "FORGAMOUNTSUM", "FEXPAMOUNTSUM",
                 "FBILLALLAMOUNT", "FALLAMOUNT", "FAMOUNT", "FAMOUNTFOR", "FPAYAMOUNTFOR_H")
_BRIEF_REASON = ("FREASON", "FCAUSE", "FREMARK", "FNOTE")
_BRIEF_REMARK = ("FREMARK", "FNOTE", "FCOMMENT", "FDESCRIPTION")
_BRIEF_APPLICANT = ("FSTAFFID.FName", "FPROPOSERID.FName", "FAPPLICANTID.FName", "FCREATORID.FName")


def bill_briefs(kd, rows):
    """按单据内码批量补 amount/reason/remark/applicant，只读；探测失败静默跳过。"""
    groups = {}
    for i, row in enumerate(rows):
        form = str(row.get("form_id") or "").strip()
        fid = str(row.get("fid") or "").strip()
        if form and fid.isdigit():
            groups.setdefault(form, []).append((i, fid))
    for form, items in groups.items():
        fids = sorted({fid for _, fid in items})
        where = "FID in (" + ",".join(fids) + ")"
        specs = (("amount", _BRIEF_AMOUNT), ("reason", _BRIEF_REASON),
                 ("remark", _BRIEF_REMARK), ("applicant", _BRIEF_APPLICANT))
        for field, candidates in specs:
            key = (form, field)
            exprs = [_BRIEF_EXPR[key]] if key in _BRIEF_EXPR else candidates
            for expr in exprs:
                if not expr:
                    continue
                try:
                    data = kd.query(form, "FID," + expr, where, 500)
                except (Exception, SystemExit):          # noqa: BLE001 - 字段不支持就换写法
                    continue
                got = {}
                for line in data or []:
                    if not isinstance(line, list) or len(line) != 2:
                        continue
                    if field == "amount":
                        value = _amount(line[1])
                        if value is not None:
                            got[str(line[0])] = value
                    else:
                        value = _text(line[1])
                        if value:
                            got[str(line[0])] = value
                if got:
                    _BRIEF_EXPR[key] = expr
                    for i, fid in items:
                        if fid in got and not rows[i].get(field):
                            rows[i][field] = got[fid]
                    break
            else:
                _BRIEF_EXPR[key] = ""
    return rows


def list_tasks(configured, uid, orgs=None, limit=201, with_briefs=False):
    uid = user_id(uid)
    kd, _, _ = actions.get_client(configured)
    where = _scope(uid) + " AND FStatus=0"
    if orgs:
        where += " AND FOrgId.FNumber in (" + ",".join(quote(o) for o in orgs) + ")"
    rows = _rows(kd, where, limit)
    if any(r["receiver_id"] != uid or r["status"] != "0" for r in rows):
        raise RuntimeError("待办归属或状态核对失败，请重新登录")
    return bill_briefs(kd, rows) if with_briefs else rows


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
DETAIL_SKIP = {"ID", "EACHBANKACCOUNT", "SETTLEBANKACCOUNT", "PAYBANKACCOUNT",
               "SRCROWID", "SRCENTRYID"}
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


def _label(key, form_id=""):
    norm = _norm(key)
    if norm in FIELD_LABELS:
        return FIELD_LABELS[norm]
    # 二开字段（F_TJKT_...）按该单据的字段文档取中文名，取不到才显示原始 Key。
    doc = detail_fields.labels(form_id)
    raw = str(key or "").strip().upper()
    for candidate in (raw, "F" + norm, norm):
        if candidate in doc:
            return doc[candidate]
    return key


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
    if _norm(key) in detail_names.FORM_VALUE_KEYS:
        return detail_names.FORM_LABELS.get(text, text)
    if key in resolved:
        return resolved[key]
    if key in FIELD_KIND and _id_text(value):
        return ""                       # 只有内码、又反查不到名称：宁可不显示
    return text


def _entries(rows, kd=None, form_id="", fid="", entry_keys=()):
    """从 View 的明细数组里自动识别表格：同名字段只留一列，只显示有内容的列。"""
    if kd:
        rows = detail_names.resolve(rows, form_id, fid, kd, entry_keys=entry_keys)
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
        text = _text(cell)
        if _norm(key) in detail_names.FORM_VALUE_KEYS:
            return detail_names.FORM_LABELS.get(text, text)
        return text

    hit = {}
    for row in sample:
        for key, cell in row.items():
            if _norm(key) in ("ID", "ENTRYID") or (isinstance(cell, list) and key in ("Name", "FName")):
                continue
            if cell_text(key, cell):
                hit[key] = hit.get(key, 0) + 1
    if not hit:
        return None
    first = list(sample[0])
    # 同一中文标签只留一列：Fxxx 与 Fxxx_Id 同时回传时保留能读出名称的那列。
    groups = {}
    for key in hit:
        groups.setdefault(_label(key, form_id), []).append(key)
    picked = []
    for keys in groups.values():
        if all(_norm(k) in DETAIL_SKIP for k in keys):
            continue
        usable = [k for k in keys if _norm(k) not in DETAIL_SKIP]
        picked.append(next((k for k in usable if not _id_text(sample[0].get(k))), usable[0]))
    order = {k: i for i, k in enumerate(FIELD_LABELS)}
    picked.sort(key=lambda k: (0 if _label(k, form_id) != k else 1, DETAIL_FIRST.get(_norm(k), 50),
                               order.get(_norm(k), 999), -hit[k],
                               first.index(k) if k in first else 999))
    keys = picked[:30]
    body = []
    for row in sample:
        line = [cell_text(k, row.get(k)) for k in keys]
        if any(line):
            body.append(line)
    columns = []
    for i, k in enumerate(keys):
        label = _label(k, form_id)
        num, money = detail_fields.column_flags(label, [r[i] for r in body])
        columns.append(dict(t=label, num=num, money=money))
    return dict(columns=columns, rows=body) if body else None


def generic_detail(configured, form_id, fid, kind=""):
    """任意单据的只读详情：用通用 View 接口读全字段，不依赖逐表单适配。

    明细只保留有内容的列（最多 30 列、前 80 行）；按单据类型用字段文档补中文名。
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
        # 付款申请单上「应付金额」恒为 0，审批时看的是申请付款金额。
        if form_id == "CN_PAYAPPLY" and key in ("PAYAMOUNTFOR", "PAYTOTALAMOUNTFOR"):
            continue
        if form_id == "CN_PAYAPPLY" and key == "APPLYAMOUNTFOR":
            label = "申请付款金额"
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
    # 付款申请单：优先按字段文档里的「申请付款金额」取值，避免落到应付金额 0。
    if form_id == "CN_PAYAPPLY" and not amount and spec.get("amount"):
        fetched = _amount(_fetch(kd, form_id, str(fid), spec["amount"]))
        if fetched is not None:
            amount = fetched
    if spec:
        if not applicant and spec.get("applicant"):
            applicant = _fetch(kd, form_id, str(fid), spec["applicant"])
        if not org_name and spec.get("org"):
            org_name = _fetch(kd, form_id, str(fid), spec["org"])
    project = _fetch(kd, form_id, str(fid), spec["project"]) if spec.get("project") else ""
    entries = None
    for entry_key, value in data.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            entries = _entries(value, kd, form_id, str(fid), (entry_key,))
            if entries:
                break
    if entries:
        # 明细没带费用归集字段时，用表头有值的字段补列，排到「序号」之后。
        # 用户 2026-11-27 口径：只显示有内容的字段，不再为空列占位。
        cols = entries["columns"]
        have = [c["t"] for c in cols]
        for key in ("COSTID", "EXPENSEDEPTID", "PROJECTID"):
            label = FIELD_LABELS[key]
            if label in have:
                continue
            text = _display(key, index.get(key), resolved)
            if key == "PROJECTID" and not text:
                text = project
            if not text:
                continue
            cols.append({"t": label, "num": False, "money": False})
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
        "amount_label": "申请付款金额" if form_id == "CN_PAYAPPLY" else "金额",
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


def save_checked_amounts(kd, rl, form_id, fid, entries):
    """审批同意时把调整的核定金额写回申请单。

    只支持费用/出差申请单；先 Save、成功并复核后再由 decide 走 WorkflowAudit。
    任何一步失败都抛错中止审批，绝不出现「金额没改但单已批」。
    """
    if form_id not in ("ER_ExpenseRequest", "ER_ExpenseRequest_Travel"):
        raise ValueError("该单据类型不支持审批时修改核定金额")
    if not str(fid).isdigit():
        raise ValueError("单据内码无效")
    clean = []
    for item in (entries or [])[:100]:
        if not isinstance(item, dict):
            raise ValueError("核定金额数据格式无效")
        eid = str(item.get("entry_id") or "").strip()
        if not eid.isdigit():
            raise ValueError("核定金额对应的分录内码无效")
        try:
            amount = round(float(item.get("checked")), 2)
        except (TypeError, ValueError):
            raise ValueError("核定金额格式无效")
        if not 0 <= amount <= 1000000000:
            raise ValueError("核定金额超出允许范围")
        # ⚠️ 分录内码必须传整数：金蝶 Save 的分录定位按数字匹配，
        # 传字符串 "9001" 时 IsSuccess 仍为 true，但字段不会落到目标分录。
        clean.append({"FEntryID": int(eid), "FCheckedOrgAmount": amount,
                      "FCheckedLocAmount": amount})
    if not clean:
        return {"changed": False}
    data = {
        "NeedUpDateFields": ["FCheckedOrgAmount", "FCheckedLocAmount"],
        # ⚠️ 同调用回读：用与保存一致的裸字段名（不带 FEntity_ 前缀）。
        # 该租户的 ExecuteBillQuery 元数据不存在 FEntity_FCheckedOrgAmount，
        # 二次查询会报 MsgCode=9；View 对审核中单据又把核定字段回成 0。
        # 只有 Save 响应里金蝶自己回传的字段值才可信。
        "NeedReturnFields": ["FEntryID", "FCheckedOrgAmount", "FCheckedLocAmount"],
        "IsDeleteEntry": "false", "SubSystemId": "",
        "IsVerifyBaseDataField": "false", "IsEntryBatchFill": "true",
        # ⚠️ IsAutoAdjustField=false：核定金额是人工值，开启自动调整可能被
        # 金蝶按申请金额重算回去，Save 成功但回读仍是旧值。
        "ValidateFlag": "false", "NumberSearch": "true", "IsAutoAdjustField": "false",
        "InterationFlags": "", "IgnoreInterationFlag": "true",
        "IsControlPrecision": "false", "ValidateRepeatJson": "true",
        "Model": {"FID": str(fid), "FEntity": clean},
    }
    raw = kd._post(rl.KSVC_SUFFIX + "Save.common.kdsvc",
                   {"formid": form_id, "data": json.dumps(data, ensure_ascii=False)},
                   retries=1, timeout=90)
    status = ((raw.get("Result") or {}).get("ResponseStatus") or {}) if isinstance(raw, dict) else {}
    if status.get("IsSuccess") is not True:
        msg = "；".join(str(e.get("Message") or "") for e in status.get("Errors", []) if isinstance(e, dict))
        raise RuntimeError("核定金额写回失败，审批未执行：" + (msg or "金蝶未确认")[:200])
    # 写后核对：只读 Save 响应回传值。任何二次查询/View 都不可靠（见上）。
    returned = _returned_checked_amounts(raw)
    unverified = []
    for item in clean:
        eid = str(item["FEntryID"])
        want = item["FCheckedOrgAmount"]
        got = returned.get(eid)
        if got is None:
            unverified.append(eid)
            continue
        if abs(got - want) > 0.01:
            # 金蝶明确回传了不同的值 → 真失败，中止审批。
            raise RuntimeError(
                "核定金额写回后复核不一致（分录 %s：期望 %.2f，金蝶回传 %.2f），"
                "审批未执行，请到金蝶核对" % (eid, want, got))
    # 金蝶未回传核定字段时不硬卡审批（否则本功能完全不可用），但要明确告警。
    return {"changed": True, "entries": len(clean),
            "unverified": unverified,
            "verify_note": ("已提交金蝶保存，但接口未回传核定金额供复核，请在金蝶核对是否写入"
                            if unverified else "")}


def _returned_checked_amounts(raw):
    """从 Save 响应里挖出金蝶回传的分录核定金额，返回 {分录内码: 金额}。

    金蝶不同版本回传位置不一：Result 下直接是 FEntity 数组，或 NeedReturnFields
    回写成扁平键。这里做有限深度的宽容遍历，只认「分录内码键 + 核定金额键」同现的节点。
    解析不到就返回空字典 —— 调用方据此告警，不硬卡审批。
    """
    out = {}
    root = raw.get("Result") if isinstance(raw, dict) else None
    if not isinstance(root, dict):
        return out
    id_keys = ("FEntryID", "Id", "FENTRYID")
    amt_keys = ("FCheckedOrgAmount", "FCHECKEDORGAMOUNT")

    def walk(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, dict):
            eid = next((str(node[k]).strip() for k in id_keys if node.get(k) not in (None, "")), "")
            amt = next((node[k] for k in amt_keys if node.get(k) not in (None, "")), None)
            if eid.isdigit() and amt is not None:
                try:
                    out[eid] = float(amt)
                except (TypeError, ValueError):
                    pass
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(root)
    return out


def decide(configured, uid, aid, decision, checked_amounts=None):
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
        saved = {"changed": False}
        if decision == "approve" and checked_amounts:
            saved = save_checked_amounts(kd, rl, task["form_id"], task["fid"], checked_amounts)
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
                "decision": decision, "task_status": "1", "node": task["node"], "checked_amounts": saved}
