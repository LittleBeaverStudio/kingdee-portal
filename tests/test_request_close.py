"""Application amount aggregation and the Kingdee close operation boundary."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("PORTAL_NO_WARMUP", "1")
os.environ.setdefault("PORTAL_STATE_DIR", tempfile.mkdtemp(prefix="portal-close-test-"))
import portal_actions as actions
import app


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.kd = Mock()
        self.rl = SimpleNamespace(KSVC_SUFFIX="service.")
        bound = patch.object(actions, "get_client", return_value=(self.kd, self.rl, ""))
        bound.start(); self.addCleanup(bound.stop)

    def source(self, **changes):
        return {"BillNo": "REQ-TEST", "DocumentStatus": "C", "FCloseStatus": "A", **changes}

    def test_exact_amount_field_aggregates_entries_without_duplicating_header(self):
        def query(form, fields, condition, **kwargs):
            if fields == "FID,FEntity_FEntryID,FPUSHREIMBAMT":
                return [[100, 1, 20.1], [100, 2, 5.2], [100, 2, 5.2]]
            return [[100, "REQ-TEST", "2026-11-18", "C", "A", True, 50,
                     "tester", "123", "101", "test org", "reason"]]
        self.kd.query.side_effect = query
        rows = actions.list_pushable()
        self.assertEqual(len(rows), 2)  # 两种申请单可以具有相同内码。
        self.assertEqual([r["pushed_reimb_amount"] for r in rows], [25.3, 25.3])
        self.assertEqual([r["amount"] for r in rows], [50, 50])
        self.assertEqual(self.kd.query.call_count, 4)
        self.kd.view.assert_not_called()

    def test_amount_lookup_failure_never_displays_zero(self):
        self.kd.query.side_effect = [
            [[100, "REQ-TEST", "2026-11-18", "C", "A", False, 50, "tester", "123", "101", "org", ""]], []]
        with self.assertRaisesRegex(RuntimeError, "无法读取"):
            actions.list_pushable()

    def test_amount_query_respects_org_and_staff_filters(self):
        self.kd.query.return_value = [[100, "REQ-TEST", "2026-11-18", "C", "A", False, 50,
                                      "other", "123", "102", "org", ""]]
        self.assertEqual(actions.list_pushable(orgs=["101"], staff_name="tester"), [])
        self.assertEqual(self.kd.query.call_count, 2)

    def test_close_uses_official_operation_and_reads_correct_status_for_both_forms(self):
        for travel in (False, True):
            self.kd.reset_mock()
            self.kd.view.side_effect = [self.source(), self.source(FCloseStatus="B")]
            self.kd._post.return_value = {"Result": {"ResponseStatus": {"IsSuccess": True}}}
            result = actions.close_request("", "100", travel, "REQ-TEST")
            self.assertTrue(result["ok"])
            self.assertEqual(result["close_status"], "B")
            path, payload = self.kd._post.call_args.args
            self.assertEqual(path, "service.ExecuteOperation.common.kdsvc")
            self.assertEqual(payload["opNumber"], "BillClose")
            self.assertEqual(payload["formid"], "ER_ExpenseRequest_Travel" if travel else "ER_ExpenseRequest")
            self.assertEqual(json.loads(payload["data"]), {"Ids": "100", "Numbers": []})
            self.assertEqual(self.kd._post.call_args.kwargs["retries"], 1)
            self.assertEqual(self.kd.view.call_count, 2)
            self.kd.save.assert_not_called()

    def test_already_closed_is_verified_without_reexecuting(self):
        self.kd.view.return_value = self.source(FCloseStatus="B")
        self.assertTrue(actions.close_request("", "100", bill_no="REQ-TEST")["already_closed"])
        self.kd._post.assert_not_called()

    def test_nonapproved_cancelled_unknown_and_wrong_bill_cannot_close(self):
        for source in (self.source(DocumentStatus="A"), self.source(DocumentStatus="B"),
                       self.source(FCancelStatus="B"), self.source(FCloseStatus=""),
                       self.source(BillNo="OTHER")):
            self.kd.view.return_value = source
            with self.subTest(source=source), self.assertRaises(ValueError):
                actions.close_request("", "100", bill_no="REQ-TEST")
        self.kd._post.assert_not_called()

    def test_invalid_id_never_reaches_kingdee(self):
        for fid in ("", "100 OR 1=1", "abc"):
            with self.assertRaises(ValueError):
                actions.close_request("", fid)
        self.kd.view.assert_not_called()

    def test_business_refusal_is_returned_without_bypassing_plugins(self):
        self.kd.view.return_value = self.source()
        message = "申请单借款已存在借款移转单，不能关闭"
        self.kd._post.return_value = {"Result": {"ResponseStatus": {"IsSuccess": False,
                                    "Errors": [{"Message": message}]}}}
        result = actions.close_request("", "100", bill_no="REQ-TEST")
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], message)
        self.assertEqual(self.kd._post.call_count, 1)
        self.kd.save.assert_not_called()

    def test_transport_timeout_rereads_state_without_retry(self):
        for exc in (TimeoutError("timeout"), SystemExit("timeout")):
            self.kd.reset_mock()
            self.kd.view.side_effect = [self.source(), self.source(FCloseStatus="B")]
            self.kd._post.side_effect = exc
            result = actions.close_request("", "100", bill_no="REQ-TEST")
            self.assertTrue(result["confirmed_after_timeout"])
            self.assertEqual(self.kd._post.call_count, 1)

    def test_success_without_closed_state_and_unknown_transport_never_report_success(self):
        for timeout in (False, True):
            self.kd.view.side_effect = [self.source(), self.source()]
            self.kd._post.side_effect = TimeoutError("timeout") if timeout else None
            self.kd._post.return_value = {"Result": {"ResponseStatus": {"IsSuccess": True}}}
            with self.assertRaisesRegex(RuntimeError, "结果未确认"):
                actions.close_request("", "100", bill_no="REQ-TEST")

    def test_closed_source_blocks_direct_and_stale_push(self):
        self.kd.view.return_value = self.source(FCloseStatus="B")
        self.kd.query.return_value = [[100, "REQ-TEST"]]
        for travel in (False, True):
            for fid in ("100", ""):
                with self.subTest(travel=travel, fid=fid), self.assertRaisesRegex(ValueError, "已关闭"):
                    actions.push("", "REQ-TEST", travel, fid)
        self.kd._post.assert_not_called()

    def test_unapproved_source_cannot_push(self):
        self.kd.view.return_value = self.source(DocumentStatus="B")
        with self.assertRaisesRegex(ValueError, "未审核"):
            actions.push("", "REQ-TEST", False, "100")
        self.kd._post.assert_not_called()

    def test_close_route_requires_confirmation_and_audits_result(self):
        with patch.object(app, "_check_csrf"), patch.object(app, "_write_gate"), \
             patch.object(app, "_require_session", return_value={"info": {"username": "tester"}}), \
             patch.object(app, "_bind_actor", return_value="tester"), patch.object(app, "_audit") as audit, \
             patch.object(actions, "close_request", return_value={"ok": True, "close_status": "B"}) as close:
            client = app.app.test_client()
            body = {"fid": "100", "bill_no": "REQ-TEST", "is_travel": True}
            self.assertFalse(client.post("/api/reimburse/request-close", json=body).json["ok"])
            close.assert_not_called()
            body["confirm"] = "CLOSE"
            self.assertTrue(client.post("/api/reimburse/request-close", json=body).json["ok"])
            close.assert_called_once_with(str(app.CFG.get("expense_skill_dir") or ""), "100", True, "REQ-TEST")
            self.assertEqual(audit.call_args.args[0], "申请单-关闭")

    def test_close_route_authentication_and_csrf_required(self):
        client = app.app.test_client()
        response = client.post("/api/reimburse/request-close", json={"fid": "100", "confirm": "CLOSE"})
        self.assertFalse(response.json["ok"])
        self.assertEqual(response.json["error"]["stage"], "安全")
        self.kd._post.assert_not_called()
        with patch.object(app, "_check_csrf"), patch.object(app, "_write_gate"), \
             patch.object(app, "_require_session", side_effect=app.KdError("未登录", "会话")), \
             patch.object(actions, "close_request") as close:
            self.assertFalse(client.post("/api/reimburse/request-close", json={"fid": "100", "confirm": "CLOSE"}).json["ok"])
            close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
