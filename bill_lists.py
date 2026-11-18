"""Complete filtered header snapshots with page-only application detail lookups."""
import json
import math
import time
from decimal import Decimal

import list_filters
import portal_actions as actions


def _query_all(kd, rl, form, fields, where):
    rows, start = [], 0
    while True:
        raw = kd._post(rl.KSVC_SUFFIX + "ExecuteBillQuery.common.kdsvc", {
            "formid": form, "data": json.dumps({"FormId": form, "FieldKeys": fields,
                "FilterString": where, "OrderString": "FID desc,FEntity_FEntryID desc" if "FEntity_FEntryID" in fields else "FID desc", "TopRowCount": 0,
                "StartRow": start, "Limit": 1000}, ensure_ascii=False)})
        kd._unwrap_error(raw)
        if not isinstance(raw, list) or any(not isinstance(r, list) for r in raw):
            raise RuntimeError("金蝶单据列表返回结构异常")
        rows.extend(raw)
        if len(raw) < 1000:
            return rows
        start += len(raw)


def _headers(kd, rl, application, filters, orgs):
    forms = (("ER_ExpenseRequest", "费用申请"), ("ER_ExpenseRequest_Travel", "出差申请")) if application else (
        ("ER_ExpReimbursement", "费用报销"), ("ER_ExpReimbursement_Travel", "差旅报销"))
    fields = ("FID,FBillNo,FDate,FDocumentStatus,FIsBorrow,FOrgAmountSum,FStaffID.FName,FOrgId.FNumber,FOrgId.FName,FReason"
              if application else "FID,FBillNo,FCreateDate,FDocumentStatus,FExpAmountSum,FProposerID.FName,FOrgId.FNumber,FOrgId.FName")
    out = []
    for form, kind in forms:
        if filters.get("kind") and (filters["kind"] == "travel") != (form.endswith("_Travel")):
            continue
        parts = list_filters.conditions(filters, person_field="FStaffID.FName" if application else "FProposerID.FName",
                                        date_field="FDate" if application else "FCreateDate", orgs=orgs, application=application)
        if application:
            parts += ["FDocumentStatus='C'", "FCloseStatus='A'"]
        if orgs == []:
            continue
        for row in _query_all(kd, rl, form, fields, " AND ".join(parts)):
            if len(row) != len(fields.split(",")):
                raise RuntimeError("金蝶单据列表字段不完整")
            if application:
                fid, number, date, status, borrow, amount, person, org, orgname, reason = row
            else:
                fid, number, date, status, amount, person, org, orgname = row
            item = dict(fid=fid, bill_no=str(number or "").strip(), kind=kind, status=str(status or ""),
                        amount=float(amount or 0), org_no=str(org or ""), org_name=str(orgname or ""))
            if application:
                item.update(date=str(date or "")[:10], staff=str(person or ""),
                            is_borrow=str(borrow).lower() in ("true", "1"), reason=str(reason or ""))
            else:
                item.update(created=str(date or "")[:19].replace("T", " "), proposer=str(person or ""),
                            status_cn=actions.BILL_STATUS_CN.get(str(status), str(status)))
            out.append(item)
    out.sort(key=lambda r: ((r.get("date", ""), int(r["fid"]), r["kind"]) if application else
                            (int(r["fid"]), r["kind"])), reverse=True)
    return out


def _summary(rows, application):
    def amount(items):
        return float(sum((Decimal(str(x["amount"])) for x in items), Decimal(0)))
    if application:
        return dict(count=len(rows), amount=amount(rows), staff=len({r["staff"] for r in rows if r["staff"]}),
                    orgs=len({r["org_no"] for r in rows if r["org_no"]}), borrow=sum(r["is_borrow"] for r in rows))
    result = dict(count=len(rows))
    for name, statuses in (("draft", ("Z", "A", "D")), ("pending", ("B",)), ("approved", ("C",))):
        chosen = [r for r in rows if r["status"] in statuses]
        result[name] = dict(count=len(chosen), amount=amount(chosen))
    return result


def page(configured, application, filters, orgs, number=1, size=20, cache=None, refresh=False):
    if number < 1 or size not in (20, 50, 100):
        raise ValueError("分页参数无效")
    kd, rl, _ = actions.get_client(configured)
    # Cache belongs to one authenticated portal session, never shared across users.
    key = json.dumps([application, filters, sorted(orgs) if orgs is not None else None], sort_keys=True)
    snapshots = cache.setdefault("bill_list_snapshots", {}) if cache is not None else {}
    saved = snapshots.get(key)
    if refresh or not saved or time.monotonic() - saved["at"] > 60:
        rows = _headers(kd, rl, application, filters, orgs)
        saved = dict(at=time.monotonic(), rows=rows, summary=_summary(rows, application))
        if len(snapshots) >= 4:
            snapshots.pop(next(iter(snapshots)))
        snapshots[key] = saved
    rows = saved["rows"]
    pages = max(1, math.ceil(len(rows) / size))
    number = min(number, pages)
    selected = [dict(r) for r in rows[(number-1)*size:number*size]]
    if application:
        for form, kind in (("ER_ExpenseRequest", "费用申请"), ("ER_ExpenseRequest_Travel", "出差申请")):
            items = [r for r in selected if r["kind"] == kind]
            if not items:
                continue
            ids = ",".join(str(int(r["fid"])) for r in items)
            values = _query_all(kd, rl, form, "FID,FEntity_FEntryID,FPUSHREIMBAMT", "FID in (" + ids + ")")
            totals, seen = {}, set()
            for fid, eid, value in values:
                pair = (str(fid), str(eid))
                if pair in seen:
                    continue
                seen.add(pair)
                value = Decimal(str(value))
                if not value.is_finite():
                    raise RuntimeError("已下推报销单金额异常")
                totals[str(fid)] = totals.get(str(fid), Decimal(0)) + value
            for r in items:
                if str(r["fid"]) not in totals:
                    raise RuntimeError("无法读取申请单已下推报销单金额")
                r["pushed_reimb_amount"] = float(totals[str(r["fid"])])
    return dict(rows=selected, summary=saved["summary"], pagination=dict(page=number, page_size=size,
                total=len(rows), pages=pages, has_next=number < pages, has_prev=number > 1))
