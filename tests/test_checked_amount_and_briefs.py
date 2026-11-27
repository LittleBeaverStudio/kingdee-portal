"""审批列表摘要、编码自身名称、核定金额写回的回归测试。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 本文件按字母序最先被 discover 导入；app 模块全局只初始化一次，
# 必须和 dsh 本地模式测试用同一环境，否则后续 local-mode 测试拿到非本地实例。
os.environ["PORTAL_LOCAL_MODE"] = "1"
os.environ.setdefault("PORTAL_NO_WARMUP", "1")
os.environ.setdefault("PORTAL_STATE_DIR", tempfile.mkdtemp(prefix="portal-checked-test-"))

import app
import detail_names
import portal_actions as pa
import workflow_actions as w


def row(status="0", form="ER_ExpenseRequest", fid="456"):
    return ["abc123", "test", "2026-11-18T10:00:00", 12, "SQ-TEST", form, fid, status,
            "财务", "101", "测试组织", "tester", "待处理"]


class OwnNameTests(unittest.TestCase):
    def setUp(self):
        detail_names._miss.clear()
        detail_names._formula_miss.clear()

    def test_name_inside_value_object_is_used(self):
        kd = Mock()
        kd.query.return_value = []
        rows = detail_names.resolve([
            {"FExpenseItemID": {"Number": "FY001", "Name": [{"Key": 2052, "Value": "差旅费"}]}},
            {"F_TJKT_Base_qtr": {"Number": "XM001", "Name": "昆明项目"}},
        ], "ER_ExpenseRequest_Travel", "123", kd, entry_keys=("FEntity",))
        self.assertEqual(rows[0]["FExpenseItemID"], "差旅费")
        self.assertEqual(rows[1]["F_TJKT_Base_qtr"], "昆明项目")
        kd.query.assert_not_called()


class BriefTests(unittest.TestCase):
    def setUp(self):
        w._BRIEF_EXPR.clear()

    def test_task_list_fills_amount_reason_from_bill(self):
        kd = Mock()
        def query(form, expr, where=None, top=None, **kw):
            if form == "WF_AssignmentBill":
                return [row()]
            if expr == "FID,FAPPLYAMOUNTFOR_H":
                return [["456", 1234.5]]
            if expr == "FID,FREASON":
                return [["456", "客户拜访"]]
            return []
        kd.query.side_effect = query
        with patch.object(w.actions, "get_client", return_value=(kd, None, "")):
            rows = w.list_tasks("", "12", with_briefs=True)
        self.assertEqual(rows[0]["amount"], 1234.5)
        self.assertEqual(rows[0]["reason"], "客户拜访")

    def test_brief_failure_never_breaks_list(self):
        kd = Mock()
        kd.query.side_effect = lambda *a, **k: [row()] if a[0] == "WF_AssignmentBill" else (_ for _ in ()).throw(RuntimeError("no field"))
        with patch.object(w.actions, "get_client", return_value=(kd, None, "")):
            rows = w.list_tasks("", "12", with_briefs=True)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("amount", rows[0])


class CheckedAmountTests(unittest.TestCase):
    def setUp(self):
        self.kd = Mock(base="https://example/k3cloud/")
        self.kd.cfg = {"acctid": "1"}
        self.rl = SimpleNamespace(KSVC_SUFFIX="service.")
        bound = patch.object(w.actions, "get_client", return_value=(self.kd, self.rl, ""))
        bound.start()
        self.addCleanup(bound.stop)

    def test_save_then_audit_with_returned_amount(self):
        self.kd.query.side_effect = [[row()], [row()], [row()], [row("1")]]
        self.kd._post.side_effect = [
            {"Result": {"ResponseStatus": {"IsSuccess": True},
                        "FEntity": [{"FEntryID": 9001, "FCheckedOrgAmount": 800.5}]}},
            {"Result": {"ResponseStatus": {"IsSuccess": True}}},
        ]
        result = w.decide("", "12", "abc123", "approve",
                          checked_amounts=[{"entry_id": "9001", "checked": 800.5}])
        self.assertTrue(result["ok"])
        self.assertTrue(result["checked_amounts"]["changed"])
        self.assertEqual(result["checked_amounts"]["unverified"], [])
        save = json.loads(self.kd._post.call_args_list[0].args[1]["data"])
        self.assertEqual(save["Model"]["FEntity"][0]["FCheckedOrgAmount"], 800.5)
        self.assertIn("FCheckedOrgAmount", save["NeedReturnFields"])   # 同调用回读
        self.assertEqual(self.kd._post.call_args_list[1].args[0], "service.WorkflowAudit.common.kdsvc")

    def test_returned_mismatch_after_save_aborts_approval(self):
        """金蝶明确回传了不同的值 → 真失败，审批中止。"""
        self.kd._post.return_value = {"Result": {"ResponseStatus": {"IsSuccess": True},
                                      "FEntity": [{"FEntryID": 9001, "FCheckedOrgAmount": 0}]}}
        with self.assertRaisesRegex(RuntimeError, "金蝶回传 0.00"):
            w.save_checked_amounts(self.kd, self.rl, "ER_ExpenseRequest", "456",
                                   [{"entry_id": "9001", "checked": 500}])
        self.kd._post.assert_called_once()   # 只有 Save，没有 Audit

    def test_no_returned_value_allows_approval_with_warning(self):
        """租户不回传核定字段时不硬卡审批，但要给出告警。"""
        kd = Mock()
        kd._post.return_value = {"Result": {"ResponseStatus": {"IsSuccess": True}}}
        result = w.save_checked_amounts(kd, self.rl, "ER_ExpenseRequest", "123",
                                        [{"entry_id": "9001", "checked": 500}])
        self.assertTrue(result["changed"])
        self.assertEqual(result["unverified"], ["9001"])
        self.assertIn("请在金蝶核对", result["verify_note"])
        kd.query.assert_not_called()   # 不再触发 MsgCode=9 的失效查询
        kd.view.assert_not_called()    # 也不依赖恒回 0 的 View

    def test_save_uses_integer_entry_id_and_disables_auto_adjust(self):
        kd = Mock()
        kd._post.return_value = {"Result": {"ResponseStatus": {"IsSuccess": True}}}
        w.save_checked_amounts(kd, self.rl, "ER_ExpenseRequest", "123",
                               [{"entry_id": "9001", "checked": 500}])
        payload = json.loads(kd._post.call_args_list[0].args[1]["data"])
        self.assertEqual(payload["Model"]["FEntity"][0]["FEntryID"], 9001)
        self.assertEqual(payload["IsAutoAdjustField"], "false")

    def test_systemexit_from_client_never_bubbles_as_field_error(self):
        """vendor 客户端抛 SystemExit（非 Exception）的历史坑：必须被兜住为可读错误。"""
        kd = Mock()
        def die(*a, **k):
            raise SystemExit('金蝶接口报错（MsgCode=9）：字段不存在')
        kd._post.side_effect = die
        with self.assertRaises(SystemExit):
            # 写回路径本身不捕获 SystemExit（由路由统一兜底），此处仅确认它是
            # 会冒泡的类型；真正的守护在 app.py 的 except (Exception, SystemExit)。
            w.save_checked_amounts(kd, self.rl, "ER_ExpenseRequest", "456",
                                   [{"entry_id": "9001", "checked": 500}])
        client = app.app.test_client()
        with patch.object(app, "_check_csrf"), patch.object(app, "_workflow_actor", return_value=("tester", "12")), \
             patch.object(app, "_audit"), \
             patch.object(w, "decide", side_effect=SystemExit("金蝶接口报错（MsgCode=9）")):
            resp = client.post("/api/workflow/decide", json={
                "assignment_id": "abc123", "decision": "approve", "confirm": "WORKFLOW"})
        self.assertFalse(resp.json["ok"])   # 路由返回结构化错误，不是 500

    def test_invalid_entry_id_never_writes(self):
        for bad in ({"entry_id": "x' OR 1=1", "checked": 10},
                    {"entry_id": "9001", "checked": -1}):
            with self.assertRaises(ValueError):
                w.save_checked_amounts(self.kd, self.rl, "ER_ExpenseRequest", "456", [bad])
        self.kd._post.assert_not_called()

    def test_request_detail_exposes_checked_rows(self):
        kd = Mock()
        kd.query.return_value = []
        kd.view.return_value = {
            "BillNo": "SQ-1", "Date": "2026-11-27T00:00:00", "DocumentStatus": "B",
            "FEntity": [{"Id": 9001, "Seq": 1, "FExpenseItemID": {"Name": "差旅费"},
                         "FCheckedOrgAmount": 800.5, "FOrgAmount": 900}],
        }
        with patch.object(pa, "get_client", return_value=(kd, None, "")):
            bill = pa.request_detail("", "456", True)
        self.assertEqual(bill["checked_rows"][0]["entry_id"], "9001")
        self.assertEqual(bill["checked_rows"][0]["checked"], 800.5)
        d = bill["detail"]
        self.assertEqual(d["columns"][-1]["t"], "审核核定金额")
        self.assertEqual(d["rows"][0][-1], "800.5")
        self.assertEqual(d["checked"][0]["entry_id"], "9001")

    def test_route_passes_checked_amounts_only_on_approve(self):
        client = app.app.test_client()
        with patch.object(app, "_check_csrf"), patch.object(app, "_workflow_actor", return_value=("tester", "12")), \
             patch.object(app, "_audit"), patch.object(w, "decide", return_value={"ok": True}) as decide:
            body = {"assignment_id": "abc123", "decision": "approve", "confirm": "WORKFLOW",
                    "checked_amounts": [{"entry_id": "9001", "checked": 800.5}]}
            self.assertTrue(client.post("/api/workflow/decide", json=body).json["ok"])
            self.assertEqual(decide.call_args.kwargs["checked_amounts"], body["checked_amounts"])
        with patch.object(app, "_check_csrf"), patch.object(app, "_workflow_actor", return_value=("tester", "12")), \
             patch.object(app, "_audit"), patch.object(w, "decide", return_value={"ok": True}) as decide:
            body["decision"] = "reject"
            body["note"] = "不同意"
            client.post("/api/workflow/decide", json=body)
            self.assertIsNone(decide.call_args.kwargs["checked_amounts"])


if __name__ == "__main__":
    unittest.main()
