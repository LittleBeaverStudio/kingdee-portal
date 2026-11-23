"""Current-reviewer completed assignments, including Kingdee archived workflows."""
import json
import math
import re
import time

import list_filters
import portal_actions as actions
import workflow_actions as workflow

SOURCES = ("WF_AssignmentHisBill", "WF_AssignmentBillEndHis")
FIELDS = ("FASSIGNID,FReceiverId,FBILLNO,FObjectTypeId,FCOMPLETEDTIME,"
          "FPreResultName,FPreDisposition,FOrgId.FNumber,FOrgId.FName,FTitle,FStatus")
FORM_ID = re.compile(r"[A-Za-z0-9_]{1,60}")
GENERIC_FIELDS = "FID,FBillNo,FDocumentStatus"


def parse(args):
    filters = list_filters.parse({k: args.get(k, "") for k in ("q", "person", "date_from", "date_to")})
    filters["form"] = str(args.get("form") or "")
    filters["result"] = str(args.get("result") or "")
    if filters["form"] and not FORM_ID.fullmatch(filters["form"]):
        raise ValueError("审批类型或处理结果筛选无效")
    if filters["result"] not in ("", "approve", "reject", "other"):
        raise ValueError("审批类型或处理结果筛选无效")
    return filters


def decision(result):
    if result in ("审核通过", "审批通过", "通过", "同意"):
        return "approve"
    if result in ("驳回重审", "驳回", "不同意", "审核不通过", "审批不通过"):
        return "reject"
    return "other"


def _query(kd, rl, form, fields, where, order):
    start = 0
    while True:
        raw = kd._post(rl.KSVC_SUFFIX + "ExecuteBillQuery.common.kdsvc", {
            "formid": form, "data": json.dumps({"FormId": form, "FieldKeys": fields,
                "FilterString": where, "OrderString": order, "TopRowCount": 0,
                "StartRow": start, "Limit": 1000}, ensure_ascii=False)})
        kd._unwrap_error(raw)
        if not isinstance(raw, list) or any(not isinstance(r, list) or len(r) != len(fields.split(',')) for r in raw):
            raise RuntimeError("金蝶审批历史返回结构异常")
        yield from raw
        if len(raw) < 1000:
            return
        start += len(raw)


def _where(uid, filters, orgs):
    parts = [workflow._scope(uid), "FStatus=1"]
    if filters.get("form"):
        parts.append("FObjectTypeId=" + list_filters.quote(filters["form"]))
    parts += list_filters.conditions({k: filters.get(k, "") for k in ("date_from", "date_to")},
        person_field="", date_field="FCOMPLETEDTIME", orgs=orgs)
    if filters.get("q"):
        parts.append(list_filters.contains("FBILLNO", filters["q"]))
    return " AND ".join(parts)


def _records(kd, rl, uid, filters, orgs, aid=None):
    if orgs == []:
        return []
    where = _where(uid, filters, orgs)
    if aid:
        where += " AND FASSIGNID=" + list_filters.quote(workflow.assignment_id(aid))
    found = {}
    for source in SOURCES:
        for r in _query(kd, rl, source, FIELDS, where, "FCOMPLETEDTIME DESC,FASSIGNID DESC"):
            ident, receiver, number, form, completed, result, note, org, orgname, title, status = r
            if str(receiver) != uid or str(status) != "1" or not str(form or "").strip():
                raise RuntimeError("审批历史归属或状态核对失败，请重新登录")
            ident = workflow.assignment_id(ident)
            if aid and ident != aid:
                raise RuntimeError("审批历史编号核对失败")
            result = str(result or "").strip()
            item = dict(assignment_id=ident, receiver_id=uid, bill_no=str(number or "").strip(),
                form_id=str(form), completed=str(completed or ""), result=result or "未提供处理结果",
                decision=decision(result), note=str(note or "").strip(), org_no=str(org or ""),
                org_name=str(orgname or ""), title=str(title or ""),
                kind=workflow.FORM_LABELS.get(str(form), str(form)),
                source=source, read_only=True)
            if ident in found and any(found[ident][k] != item[k] for k in ("bill_no", "form_id", "receiver_id")):
                raise RuntimeError("审批历史记录冲突，请在金蝶核对")
            found.setdefault(ident, item)
    rows = [x for x in found.values() if not filters.get("result") or x["decision"] == filters["result"]]
    rows.sort(key=lambda x: (x["completed"], x["assignment_id"]), reverse=True)
    return rows


def _headers(kd, rl, rows, person=""):
    """批量补当前单据的表头。已知类型取申请人和金额；其余类型只取通用三字段。"""
    headers = {}
    groups = {}
    for item in rows:
        if item["bill_no"]:
            groups.setdefault(item["form_id"], set()).add(item["bill_no"])
    for form, numbers in groups.items():
        numbers = sorted(numbers)
        known = form in workflow.EXPENSE_FORMS
        if known:
            _, _, application = workflow.EXPENSE_FORMS[form]
            person_field = "FStaffID.FName" if application else "FProposerID.FName"
            amount_field = "FOrgAmountSum" if application else "FExpAmountSum"
            fields = "FID,FBillNo," + person_field + "," + amount_field + ",FDocumentStatus"
        else:
            fields = GENERIC_FIELDS
        for start in range(0, len(numbers), 100):
            where = "FBillNo in (" + ','.join(list_filters.quote(n) for n in numbers[start:start+100]) + ")"
            if person and known:
                where += " AND " + list_filters.contains(person_field, person)
            for row in _query(kd, rl, form, fields, where, "FID DESC"):
                if known:
                    fid, number, applicant, amount, status = row
                else:
                    fid, number, status = row
                    applicant, amount = "", None
                key = (form, str(number).strip())
                if key in headers:
                    raise RuntimeError("审批记录对应多张同号单据，请在金蝶核对")
                headers[key] = dict(fid=str(fid), applicant=str(applicant or ""),
                    amount=(None if amount is None else float(amount or 0)),
                    bill_status=str(status or ""), bill_status_cn=actions.BILL_STATUS_CN.get(str(status), str(status)))
    return headers


def page(configured, uid, filters, orgs=None, number=1, size=20, cache=None, refresh=False):
    uid = workflow.user_id(uid)
    if number < 1 or size not in (20, 50, 100):
        raise ValueError("分页参数无效")
    kd, rl, _ = actions.get_client(configured)
    key = json.dumps([uid, filters, sorted(orgs) if orgs is not None else None], sort_keys=True)
    snapshots = cache.setdefault("workflow_history_snapshots", {}) if cache is not None else {}
    saved = snapshots.get(key)
    if refresh or not saved or time.monotonic() - saved["at"] > 60:
        rows = _records(kd, rl, uid, filters, orgs)
        if filters.get("person"):
            headers = _headers(kd, rl, rows, filters["person"])
            # 非费用类单据不知道申请人字段，无法核实，按申请人筛选时只能排除。
            rows = [x for x in rows if x["form_id"] in workflow.EXPENSE_FORMS
                    and (x["form_id"], x["bill_no"]) in headers]
        saved = dict(at=time.monotonic(), rows=rows)
        if len(snapshots) >= 4:
            snapshots.pop(next(iter(snapshots)))
        snapshots[key] = saved
    rows = saved["rows"]
    pages = max(1, math.ceil(len(rows) / size))
    number = min(number, pages)
    selected = [dict(x) for x in rows[(number-1)*size:number*size]]
    headers = _headers(kd, rl, selected)
    for item in selected:
        header = headers.get((item["form_id"], item["bill_no"]))
        item.update(header or dict(applicant="", amount=None, bill_status="", bill_status_cn="无法读取当前单据"))
        item["bill_available"] = header is not None
    return dict(rows=selected, pagination=dict(page=number, page_size=size, total=len(rows), pages=pages,
        has_prev=number > 1, has_next=number < pages, unit="项"))


def detail(configured, uid, aid):
    uid, aid = workflow.user_id(uid), workflow.assignment_id(aid)
    kd, rl, _ = actions.get_client(configured)
    rows = _records(kd, rl, uid, {}, None, aid)
    if len(rows) != 1:
        raise ValueError("该审批记录不存在或不属于你，请刷新列表")
    task = rows[0]
    headers = _headers(kd, rl, rows)
    header = headers.get((task["form_id"], task["bill_no"]))
    form = task["form_id"]
    known = form in workflow.EXPENSE_FORMS
    travel, application = workflow.EXPENSE_FORMS[form][1:] if known else (False, False)
    render = "expense" if known else "generic"
    if not header:
        return dict(task=task, bill=None, is_application=application, read_only=True, render=render,
            bill_error="当前单据已删除或无权查看，审批记录仍保留。")
    task.update(header)
    if known:
        bill = (actions.request_detail(configured, header["fid"], travel) if application else
                actions.get_bill(configured, header["fid"], travel))
        if str(bill.get("bill_no") or "").strip() != task["bill_no"]:
            raise RuntimeError("审批记录与当前单据编号不一致，请刷新后重试")
        return dict(task=task, bill=bill, is_application=application, read_only=True, render=render)
    bill = workflow.generic_detail(configured, form, header["fid"], task["kind"])
    number = str(bill.get("bill_no") or "").strip()
    if number and number != task["bill_no"]:
        raise RuntimeError("审批记录与当前单据编号不一致，请刷新后重试")
    return dict(task=task, bill=bill, is_application=False, read_only=True, render=render)
