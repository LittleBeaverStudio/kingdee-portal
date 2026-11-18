"""Workflow identity, task isolation, stale/duplicate handling and API transport."""
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
os.environ.setdefault("PORTAL_STATE_DIR", tempfile.mkdtemp(prefix="portal-workflow-test-"))
import app
import workflow_actions as w
from kd_client import KingdeeClient
import list_filters as f


def row(status="0", uid=12, aid="abc123", form="ER_ExpReimbursement", fid="456"):
    return [aid, "test", "2026-11-18T10:00:00", uid, "BX-TEST", form, fid, status,
            "财务", "101", "测试组织", "tester", "待处理"]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.kd = Mock(base="https://example/k3cloud/")
        self.kd.cfg = {"acctid": "1"}
        self.rl = SimpleNamespace(KSVC_SUFFIX="service.")
        bound = patch.object(w.actions, "get_client", return_value=(self.kd, self.rl, ""))
        bound.start(); self.addCleanup(bound.stop)

    def success(self):
        self.kd.query.side_effect = [[row()], [row()], [row()], [row("1")]]
        self.kd._post.return_value = {"Result": {"ResponseStatus": {"IsSuccess": True}}}

    def test_login_keeps_trusted_user_id_without_user_directory_permission(self):
        kd = KingdeeClient("https://example", acctid="1", username="tester", password="private")
        with patch.object(kd, "_post", return_value={"LoginResultType": 1, "Context": {"UserId": 12}}):
            kd.login()
        self.assertEqual(kd.user_id, "12")

    def test_list_scopes_to_logged_in_reviewer_not_applicant(self):
        self.kd.query.return_value = [row()]
        self.assertEqual(w.list_tasks("", "12", orgs=["101"])[0]["fid"], "456")
        where = self.kd.query.call_args.args[2]
        self.assertIn("FReceiverId=12", where)
        self.assertIn("FStatus=0", where)
        self.assertNotIn("Proposer", where)

    def test_cross_user_rows_are_rejected_even_if_upstream_filter_is_ignored(self):
        self.kd.query.return_value = [row(uid=99)]
        with self.assertRaisesRegex(RuntimeError, "归属"):
            w.list_tasks("", "12")
        with self.assertRaises(ValueError):
            w.detail("", "12", "abc123")
        self.kd.view.assert_not_called()

    def test_detail_requires_current_task_before_reading_bill(self):
        self.kd.query.return_value = []
        with self.assertRaisesRegex(ValueError, "已处理"):
            w.detail("", "12", "abc123")
        self.kd.view.assert_not_called()

    def test_approve_and_reject_use_workflow_api_once_then_verify_task(self):
        for decision, value in (("approve", 1), ("reject", 2)):
            self.kd.reset_mock(); self.success()
            result = w.decide("", "12", "abc123", decision)
            self.assertTrue(result["ok"])
            self.assertEqual(result["task_status"], "1")
            path, payload = self.kd._post.call_args.args
            self.assertEqual(path, "service.WorkflowAudit.common.kdsvc")
            self.assertEqual(json.loads(payload["data"]), {
                "FormId": "ER_ExpReimbursement", "Ids": "456", "Numbers": [], "UserId": 12, "ApprovalType": value})
            self.assertEqual(self.kd._post.call_args.kwargs["retries"], 1)
            self.kd.save.assert_not_called()

    def test_unsupported_decision_or_malicious_identity_never_writes(self):
        for uid, aid, decision in (("12 OR 1=1", "abc123", "approve"), ("12", "x' OR 1=1", "approve"),
                                   ("12", "abc123", "terminate"), ("", "abc123", "approve")):
            with self.assertRaises(ValueError):
                w.decide("", uid, aid, decision)
        self.kd._post.assert_not_called()

    def test_stale_and_ambiguous_tasks_do_not_approve_another_node(self):
        for queries in ([[]], [[row()], []], [[row()], [row()], [row(), row(aid="other")]]):
            self.kd.query.side_effect = queries
            with self.assertRaises(ValueError):
                w.decide("", "12", "abc123", "approve")
        self.kd._post.assert_not_called()

    def test_transport_timeout_never_retries_or_claims_success(self):
        self.success(); self.kd._post.side_effect = TimeoutError("timeout")
        with self.assertRaisesRegex(RuntimeError, "结果未确认"):
            w.decide("", "12", "abc123", "approve")
        self.assertEqual(self.kd._post.call_count, 1)

    def test_business_rejection_preserves_original_error(self):
        self.success()
        self.kd._post.return_value = {"Result": {"ResponseStatus": {"IsSuccess": False, "Errors": [{"Message": "该节点审批意见必录"}]}}}
        self.assertEqual(w.decide("", "12", "abc123", "reject")["message"], "该节点审批意见必录")

    def test_api_success_without_completed_task_remains_unconfirmed(self):
        for after in ([], [row()], [row("3")], [row("1", uid=99)]):
            self.success(); self.kd.query.side_effect = [[row()], [row()], [row()], after]
            with self.assertRaisesRegex(RuntimeError, "状态未确认"):
                w.decide("", "12", "abc123", "approve")

    def test_same_document_cannot_be_approved_simultaneously(self):
        with w._single_action(("tenant", "doc")):
            with self.assertRaisesRegex(ValueError, "正在审批"):
                with w._single_action(("tenant", "doc")):
                    self.fail("concurrent lock acquired")
        self.assertFalse(w._locks)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_missing_session_and_csrf_cannot_execute_approval(self):
        with patch.object(w, "decide") as decide:
            self.assertFalse(self.client.get("/api/workflow/tasks").json["ok"])
            self.assertFalse(self.client.post("/api/workflow/decide", json={"confirm": "WORKFLOW"}).json["ok"])
            decide.assert_not_called()

    def test_approval_ignores_forged_reviewer_and_document_fields(self):
        with patch.object(app, "_check_csrf"), patch.object(app, "_workflow_actor", return_value=("tester", "12")), \
             patch.object(app, "_audit"), patch.object(w, "decide", return_value={"ok": True}) as decide:
            body = {"assignment_id": "abc123", "decision": "approve", "confirm": "WORKFLOW", "user_id": 99, "fid": "other"}
            self.assertTrue(self.client.post("/api/workflow/decide", json=body).json["ok"])
            self.assertEqual(decide.call_args.args[1:], ("12", "abc123", "approve"))

    def test_confirmation_and_rejection_note_required_before_write(self):
        with patch.object(app, "_check_csrf"), patch.object(app, "_workflow_actor", return_value=("tester", "12")), \
             patch.object(app, "_audit"), patch.object(w, "decide") as decide:
            self.assertFalse(self.client.post("/api/workflow/decide", json={"assignment_id": "abc123"}).json["ok"])
            self.assertFalse(self.client.post("/api/workflow/decide", json={"assignment_id": "abc123", "decision": "reject", "confirm": "WORKFLOW"}).json["ok"])
            decide.assert_not_called()


class FilterTests(unittest.TestCase):
    def test_date_range_rejects_invalid_or_reversed_input(self):
        for args in ({"date_from": "2026-02-30"}, {"date_from": "2026-11-19", "date_to": "2026-11-18"}, {"status": "x' OR 1=1"}):
            with self.assertRaises(ValueError): f.parse(args)

    def test_literals_and_last_day_time_are_handled(self):
        where = f.conditions(f.parse({"q": "x' OR 1=1%_", "date_to": "2026-11-18", "status": "draft"}),
                             person_field="FProposerID.FName", date_field="FCreateDate")
        self.assertIn("x'' OR 1=1[%][_]", where[0])
        self.assertIn("FCreateDate<'2026-11-19'", where)
        self.assertIn("FDocumentStatus in ('Z','A','D')", where)

    def test_filters_are_applied_upstream_before_list_limit(self):
        kd = Mock(); kd.query.return_value = []
        with patch.object(w.actions, "get_client", return_value=(kd, None, "")):
            w.actions.list_bills(filters=f.parse({"q": "OLD-BILL", "kind": "travel", "person": "张三", "status": "C"}), orgs=["101"], limit=201)
        kd.query.assert_called_once()
        form, _, where = kd.query.call_args.args
        self.assertEqual(form, "ER_ExpReimbursement_Travel")
        self.assertIn("FBillNo like '%OLD-BILL%'", where)
        self.assertIn("FDocumentStatus='C'", where)
        self.assertIn("FOrgId.FNumber in ('101')", where)
        self.assertEqual(kd.query.call_args.kwargs["top"], 201)


if __name__ == "__main__":
    unittest.main()
