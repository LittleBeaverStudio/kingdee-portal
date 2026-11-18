"""Reconcile reimbursement settlement with its one source application.

Preview never writes. A signed snapshot binds confirmation to the current amount,
source balance and invoice rows. Apply writes explicit amounts once and verifies
the resulting header, entry and source link before Submit may be called.
"""
import hashlib
import hmac
import json
import secrets
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

EXPENSE = "ER_ExpReimbursement"
TRAVEL = "ER_ExpReimbursement_Travel"
_KEY = secrets.token_bytes(32)


def money(value):
    try:
        result = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if not result.is_finite():
            raise ValueError()
        return result
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError("金额缺失或异常，请在金蝶核对") from None


def boolean(value):
    text = str(value).strip().lower()
    if text not in ("true", "false", "1", "0"):
        raise ValueError("无法确认申请单的借款标记，请在金蝶核对")
    return text in ("true", "1")


def context(kd, fid, travel):
    form = TRAVEL if travel else EXPENSE
    source_form = "ER_ExpenseRequest_Travel" if travel else "ER_ExpenseRequest"
    bill = kd.view(form, str(fid))
    if str(bill.get("DocumentStatus") or "").upper() not in ("Z", "A", "D"):
        raise ValueError("当前单据状态不能办理或重复提交，请刷新列表")
    entries = bill.get("ER_ExpenseReimbEntry") or []
    if len(entries) != 1:
        raise ValueError("仅支持一条报销明细，请在金蝶办理")
    entry = entries[0]
    links = entry.get("FEntity_Link") or []
    if len(links) != 1 or str(entry.get("SourceBillType") or "").strip() != source_form:
        raise ValueError("无法确认唯一来源，请在金蝶办理")
    link = links[0]
    sid, seid = str(link.get("SBillId") or ""), str(link.get("SId") or "")
    if not sid.isdigit() or not seid.isdigit() or not entry.get("Id"):
        raise ValueError("来源关联不完整，请在金蝶办理")
    source = kd.view(source_form, sid)
    matches = [e for e in (source.get("FEntity") or []) if str(e.get("Id")) == seid]
    if (len(matches) != 1 or str(source.get("DocumentStatus")) != "C"
            or str(source.get("BillNo") or "").strip() != str(entry.get("SourceBillNo") or "").strip()):
        raise ValueError("申请单来源或审核状态不匹配，请在金蝶核对")
    # 本入口不分配其他借款单：独立借款来源需要金蝶完整的冲借款流程。
    if any(str(e.get("FSrcBillNo") or "").strip() or money(e.get("FTurnoverAmount", 0)) != 0
           or money(e.get("FSrcWriteOffAmount", 0)) != 0 for e in (bill.get("FSrcEntry") or [])):
        raise ValueError("本单还关联了其他借款，请在金蝶办理")
    se = matches[0]
    is_borrow = boolean(source.get("IsBorrow"))
    balance = money(se.get("TurnoverAmount")) if is_borrow else money(0)
    actual = money(se.get("ActualBorrowAmount")) if is_borrow else money(0)
    amount = money(bill.get("ExpAmountSum"))
    if amount <= 0 or money(entry.get("ExpenseAmount")) != amount or not 0 <= balance <= actual:
        raise ValueError("实际借款余额或报销金额异常，请在金蝶核对")
    if is_borrow:
        # 只查询同一种申请单的来源，避免不同业务表内码相同产生误判。
        rows = kd.query(form, "FID,FDocumentStatus,FEntity_Link_FSBillId,FEntity_Link_FSId",
                        "FEntity_Link_FSBillId='%s'" % sid, top=201)
        if len(rows) >= 201:
            raise ValueError("关联单据过多，无法确认借款占用")
        for row in rows:
            if len(row) < 4 or str(row[1]) not in ("Z", "A", "B", "C", "D"):
                raise ValueError("关联单据状态无法确认，请在金蝶核对")
            if (str(row[0]) != str(fid) and str(row[3]) == seid and str(row[1]) != "C"):
                raise ValueError("该借款存在其他未完成报销单，请先在金蝶核对占用")
    # 不包含即将重算的付款字段，允许严格保存后用同一快照重新核对来源。
    state = {"form": form, "fid": str(fid), "entry": str(entry["Id"]),
             "source_form": source_form, "source": sid, "source_entry": seid,
             "source_bill": source.get("BillNo"), "source_status": source.get("DocumentStatus"),
             "status": bill.get("DocumentStatus"), "amount": str(amount),
             "is_borrow": is_borrow, "actual": str(actual), "balance": str(balance),
             "org": str((bill.get("OrgID") or {}).get("Id") or ""),
             "invoices": sorted((str((r.get("RecInv") or {}).get("Id") or ""),
                                 str((r.get("RecInv") or {}).get("FBillNo") or ""),
                                 str((r.get("RecInv") or {}).get("FSUMALLAMOUNT") or 0))
                                for r in (bill.get("RecInvInfo") or []))}
    return {"bill": bill, "entry": entry, "state": state,
            "amount": amount, "balance": balance, "actual": actual, "is_borrow": is_borrow}


def _signature(ctx, issued):
    data = json.dumps([issued, ctx["state"]], ensure_ascii=False, sort_keys=True).encode()
    return hmac.new(_KEY, data, hashlib.sha256).hexdigest()


def _verify_snapshot(ctx, snapshot):
    try:
        issued, signature = str(snapshot).split(":", 1)
        age = time.time() - int(issued)
        valid = 0 <= age <= 900 and hmac.compare_digest(signature, _signature(ctx, issued))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("报销金额、发票或借款余额已变化，或确认已过期，请重新点击提交并确认")


def plan(ctx, choice):
    amount, balance = ctx["amount"], ctx["balance"]
    if balance > amount:
        if choice not in ("retain", "refund"):
            raise ValueError("请选择剩余借款留待下次报销，或本次退款")
        kind = "2" if choice == "refund" else "0"
    else:
        if choice != "auto":
            raise ValueError("当前没有可选择的借款余额，请重新确认")
        kind = "1" if amount > balance else "0"
    borrowed = min(balance, amount)
    requested = balance - amount if kind == "2" else amount - balance if kind == "1" else money(0)
    # 无借款时来源关联记录的是报销金额；有借款时记录实际冲借款和退款总额。
    offset = (balance if kind == "2" else borrowed) if ctx["is_borrow"] else amount
    return {"choice": choice, "request_type": kind, "amount": float(amount),
            "balance": float(balance), "borrow": float(borrowed),
            "request_amount": float(requested), "offset": float(offset)}


def preview(kd, fid, travel):
    ctx = context(kd, fid, travel)
    issued = str(int(time.time()))
    choices = ("retain", "refund") if ctx["balance"] > ctx["amount"] else ("auto",)
    return {"snapshot": issued + ":" + _signature(ctx, issued),
            "needs_choice": len(choices) > 1, "amount": float(ctx["amount"]),
            "balance": float(ctx["balance"]), "bill_no": ctx["bill"].get("BillNo") or "",
            "options": [plan(ctx, c) for c in choices]}


def verify_bill(bill, ctx, expected):
    entries = bill.get("ER_ExpenseReimbEntry") or []
    if len(entries) != 1:
        raise ValueError("金蝶保存后明细发生变化，请在金蝶核对，勿重复提交")
    entry = entries[0]
    links = entry.get("FEntity_Link") or []
    amounts = {"SRCBORROWAMOUNT": expected["balance"], "BorrowAmount": expected["borrow"],
               "RequestAmount": expected["request_amount"], "ReqSubmitAmount": expected["request_amount"],
               "LocReqSubmitAmount": expected["request_amount"], "SrcOffSetAmount": expected["offset"],
               "RELRQAMOUNT": expected["offset"], "ExpenseAmount": expected["amount"]}
    if (str(bill.get("RequestType")) != expected["request_type"]
            or money(bill.get("ExpAmountSum")) != money(expected["amount"])
            or money(bill.get("ReqAmountSum")) != money(expected["request_amount"])
            or str(entry.get("Id")) != ctx["state"]["entry"]
            or any(money(entry.get(k)) != money(v) for k, v in amounts.items())
            or len(links) != 1 or str(links[0].get("SBillId")) != ctx["state"]["source"]
            or str(links[0].get("SId")) != ctx["state"]["source_entry"]
            or money(links[0].get("SrcOffSetAmount")) != money(expected["offset"])
            or money(links[0].get("RELRQAMOUNT")) != money(expected["offset"])):
        raise ValueError("金蝶保存后付款、冲借款金额或来源关联不一致，请在金蝶核对，勿提交")


def apply(kd, fid, travel, choice, snapshot, service="Kingdee.BOS.WebApi.ServicesStub.DynamicFormService."):
    ctx = context(kd, fid, travel)
    _verify_snapshot(ctx, snapshot)
    expected = plan(ctx, choice)
    fields = {"FEntryID": ctx["entry"]["Id"], "FSRCBORROWAMOUNT": expected["balance"],
              "FBorrowAmount": expected["borrow"], "FRequestAmount": expected["request_amount"],
              "FReqSubmitAmount": expected["request_amount"], "FLocReqSubmitAmount": expected["request_amount"],
              "FSrcOffSetAmount": expected["offset"], "FRELRQAMOUNT": expected["offset"]}
    # 一次保存包含所有金额；不能再使用仅传 FID 的“空保存”探针，
    # 它会执行金蝶实体服务，并可能再次累计 BorrowAmount。
    # 沿用门户保存参数；发票金额、流水号、组织由 submit_checked 的只读闸门强制检查。
    try:
        data = {"NeedUpDateFields": ["FRequestType"] + [k for k in fields if k != "FEntryID"],
                "NeedReturnFields": [], "IsDeleteEntry": "false", "SubSystemId": "",
                "IsVerifyBaseDataField": "false", "IsEntryBatchFill": "true", "ValidateFlag": "false",
                "NumberSearch": "true", "IsAutoAdjustField": "true", "InterationFlags": "",
                "IgnoreInterationFlag": "true", "IsControlPrecision": "false", "ValidateRepeatJson": "true",
                "Model": {"FID": str(fid), "FRequestType": expected["request_type"], "FEntity": [fields]}}
        raw = kd._post(service + "Save.common.kdsvc", {"formid": ctx["state"]["form"],
                       "data": json.dumps(data, ensure_ascii=False)}, retries=1, timeout=90)
        status = (raw.get("Result") or {}).get("ResponseStatus") or {}
        if not status.get("IsSuccess"):
            raise ValueError("金蝶保存未通过：" + "；".join(
                str(e.get("Message") or "") for e in (status.get("Errors") or []))[:350])
        after = context(kd, fid, travel)
        # 暂存保存会变成已保存；除此以外的来源、金额和发票应保持不变。
        state = dict(after["state"])
        state["status"] = ctx["state"]["status"]
        if state != ctx["state"]:
            raise ValueError("保存期间来源余额或单据内容发生变化，请刷新后重新确认")
        verify_bill(after["bill"], ctx, expected)
    except (SystemExit, OSError, TimeoutError) as exc:
        raise RuntimeError("保存结果未确认，请先刷新本单核对，勿重新下推或重复提交：" + str(exc)[:150]) from exc
    return expected, after


def submit_checked(kd, service, fid, travel, choice, snapshot, precheck):
    # 所有入口（包括直接请求 API）都必须完成来源快照确认和发票闸门。
    if not snapshot or not choice:
        raise ValueError("请重新点击提交，核对退款/付款金额并确认")
    check = precheck()
    if not check["ok"]:
        return {"ok": False, "message": "；".join(check["issues"]) or check["gate_message"]}
    expected, prepared = apply(kd, fid, travel, choice, snapshot, service)
    check = precheck()
    if not check["ok"]:
        return {"ok": False, "message": "；".join(check["issues"]) or check["gate_message"]}
    final = context(kd, fid, travel)
    if final["state"] != prepared["state"]:
        raise ValueError("提交前单据或借款余额已变化，请刷新后重新确认")
    verify_bill(final["bill"], final, expected)
    before_status = final["bill"].get("DocumentStatus")
    uncertain = False
    # 不调用 helper 的 submit：它的 FID 空保存探针会再次执行实体服务。
    # 这里已保存并回读所有金额，直接单次 Submit，禁止传输层自动重试。
    try:
        raw = kd._post(service + "Submit.common.kdsvc", {"formid": final["state"]["form"],
            "data": json.dumps({"CreateOrgId": 0, "Numbers": [], "Ids": str(fid),
                                "SelectedPostId": 0, "UseOrgId": 0, "NetworkCtrl": "",
                                "IgnoreInterationFlag": ""}, ensure_ascii=False)}, retries=1, timeout=120)
        status = (raw.get("Result") or {}).get("ResponseStatus") or {}
        if not status.get("IsSuccess"):
            return {"ok": False, "message": "；".join(str(e.get("Message") or "")
                    for e in (status.get("Errors") or [])) or "金蝶未确认提交成功，请刷新本单核对状态"}
    except (Exception, SystemExit):
        uncertain = True
    try:
        after = kd.view(final["state"]["form"], str(fid))
    except (Exception, SystemExit) as exc:
        raise RuntimeError("提交结果未确认，请先到报销单列表或金蝶核对本单，勿重复提交") from exc
    if str(after.get("DocumentStatus")) not in ("B", "C"):
        return {"ok": False, "message": "提交结果未确认，请先到金蝶核对本单状态，勿重复提交"}
    warning = ""
    try:
        verify_bill(after, final, expected)
    except ValueError as exc:
        warning = "单据已进入审批，请勿重复提交；" + str(exc)
    return {"ok": True, "before_status": before_status, "after_status": after.get("DocumentStatus"),
            "bill_no": str(after.get("BillNo") or ""), "amount": expected["amount"],
            "payment": expected, "warning": warning, "confirmed_after_timeout": uncertain}
