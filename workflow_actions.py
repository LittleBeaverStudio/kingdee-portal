"""Current-user workflow assignments and native WorkflowAudit operations."""
import json
import re
import threading
from contextlib import contextmanager

import portal_actions as actions
from list_filters import quote

FORMS = {
    "ER_ExpenseRequest": ("费用申请", False, True),
    "ER_ExpenseRequest_Travel": ("出差申请", True, True),
    "ER_ExpReimbursement": ("费用报销", False, False),
    "ER_ExpReimbursement_Travel": ("差旅报销", True, False),
}
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
    return "FReceiverId=" + user_id(uid) + " AND FObjectTypeId in (" + ",".join(quote(f) for f in FORMS) + ")"


def _rows(kd, where, limit=201):
    raw = kd.query("WF_AssignmentBill", FIELDS, where, top=limit, order="FCreateTime DESC")
    out = []
    for r in raw:
        if len(r) != len(FIELDS.split(",")):
            raise RuntimeError("金蝶待办数据不完整，请刷新重试")
        aid, title, created, receiver, number, form, fid, status, node, org, orgname, sender, content = r
        if str(form) not in FORMS:
            raise RuntimeError("金蝶返回了不支持的待办单据类型")
        out.append(dict(assignment_id=str(aid), title=str(title or ""), created=str(created or "")[:19].replace("T", " "),
                        receiver_id=str(receiver), bill_no=str(number or "").strip(), form_id=str(form),
                        fid=str(fid), status=str(status), node=str(node or ""), org_no=str(org or ""),
                        org_name=str(orgname or ""), sender=str(sender or ""), content=str(content or ""),
                        kind=FORMS[str(form)][0]))
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
    _, travel, application = FORMS[task["form_id"]]
    bill = (actions.request_detail(configured, task["fid"], travel) if application
            else actions.get_bill(configured, task["fid"], travel))
    if str(bill.get("bill_no") or "").strip() != task["bill_no"]:
        raise RuntimeError("待办与单据编号不一致，请刷新后重试")
    return {"task": task, "bill": bill, "is_application": application}


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
        try:
            raw = kd._post(rl.KSVC_SUFFIX + "WorkflowAudit.common.kdsvc",
                           {"data": json.dumps(data, ensure_ascii=False)}, retries=1, timeout=90)
        except (Exception, SystemExit) as exc:
            raise RuntimeError("审批结果未确认，请刷新待办并核对金蝶审批记录，勿重复操作") from exc
        status = ((raw.get("Result") or {}).get("ResponseStatus") or {}) if isinstance(raw, dict) else {}
        if status.get("IsSuccess") is not True:
            message = "；".join(str(e.get("Message") or "") for e in status.get("Errors", []) if isinstance(e, dict))
            return {"ok": False, "assignment_id": aid, "message": message or "金蝶未确认审批成功，请刷新并核对审批记录"}
        try:
            after = _rows(kd, _scope(uid) + " AND FASSIGNID=" + quote(aid), 2)
        except (Exception, SystemExit) as exc:
            raise RuntimeError("审批已返回成功，但任务状态未确认，请刷新并核对审批记录，勿重复操作") from exc
        if len(after) != 1 or after[0]["receiver_id"] != uid or after[0]["assignment_id"] != aid or after[0]["status"] != "1":
            raise RuntimeError("审批已返回成功，但任务完成状态未确认，请刷新并核对审批记录，勿重复操作")
        return {"ok": True, "assignment_id": aid, "bill_no": task["bill_no"], "form_id": task["form_id"],
                "decision": decision, "task_status": "1", "node": task["node"]}
