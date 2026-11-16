import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("PORTAL_NO_WARMUP", "1")
os.environ.setdefault("PORTAL_STATE_DIR", tempfile.mkdtemp(prefix="portal-push-test-"))
import portal_actions
import app


class PushTests(unittest.TestCase):
    def setUp(self):
        self.kd = Mock()
        self.rl = SimpleNamespace(KSVC_SUFFIX="service.")
        self.client_patch = patch.object(portal_actions, "get_client", return_value=(self.kd, self.rl, ""))
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def test_travel_push_returns_id_and_reads_correct_form_separately(self):
        self.kd._post.return_value = {"Result": {"ResponseStatus": {
            "IsSuccess": True, "SuccessEntitys": [{"Id": 101818}]}}}
        result = portal_actions.push("", "ETR123", True)
        self.assertEqual(result["fid"], "101818")
        path, payload = self.kd._post.call_args.args
        self.assertEqual(path, "service.Push.common.kdsvc")
        self.assertEqual(self.kd._post.call_args.kwargs["retries"], 1)
        self.assertEqual(payload["formid"], "ER_ExpenseRequest_Travel")
        self.assertEqual(json.loads(payload["data"])["TargetFormId"], "ER_ExpReimbursement_Travel")
        self.assertEqual(json.loads(payload["data"])["IsDraftWhenSaveFail"], "true")
        self.kd.view.reset_mock()
        self.kd.view.return_value = {"DocumentStatus": "Z"}
        portal_actions.get_bill("", result["fid"], True)
        self.kd.view.assert_called_once_with("ER_ExpReimbursement_Travel", "101818")

    def test_uncertain_transport_is_not_retried(self):
        self.kd._post.side_effect = SystemExit("network timeout")
        with self.assertRaisesRegex(RuntimeError, "结果未确认"):
            portal_actions.push("", "ETR123", True)
        self.assertEqual(self.kd._post.call_count, 1)

    def test_failed_and_missing_id_responses_require_check(self):
        for status in ({"IsSuccess": False, "Errors": [{"Message": "error"}]},
                       {"IsSuccess": True, "SuccessEntitys": []}):
            self.kd._post.return_value = {"Result": {"ResponseStatus": status}}
            with self.assertRaisesRegex(RuntimeError, "核对"):
                portal_actions.push("", "ETR123", True)

    def test_http_push_returns_success_without_reading_details(self):
        self.kd._post.return_value = {"Result": {"ResponseStatus": {
            "IsSuccess": True, "SuccessEntitys": [{"Id": 101818}]}}}
        with patch.object(app, "_check_csrf"), patch.object(app, "_write_gate"), \
             patch.object(app, "_require_session", return_value={"info": {"username": "test"}}), \
             patch.object(app, "_bind_actor"), patch.object(app, "_audit"):
            response = app.app.test_client().post("/api/reimburse/push", json={
                "bill_no": "ETR123", "is_travel": True})
        self.assertTrue(response.json["ok"])
        self.assertEqual(response.json["result"]["fid"], "101818")
        self.kd.view.assert_called_once_with("ER_ExpReimbursement_Travel", "101818")

    def test_inherits_source_contact_only_when_missing(self):
        self.kd._post.return_value = {"Result": {"ResponseStatus": {
            "IsSuccess": True, "SuccessEntitys": [{"Id": 101818}]}}}
        self.kd.view.side_effect = [
            {},
            {"BillNo": "ETR123", "TOCONTACTUNITTYPE": "BD_Empinfo", "TOCONTACTUNIT": {"Number": "123"}},
            {"CONTACTUNIT": {"Number": "123"}},
        ]
        self.kd.save.return_value = (True, {})
        result = portal_actions.push("", "ETR123", True, "222")
        self.assertEqual(result['warning'], '')
        self.kd.save.assert_called_once_with('ER_ExpReimbursement_Travel', {
            'FID': '101818', 'FCONTACTUNITTYPE': 'BD_Empinfo', 'FCONTACTUNIT': {'FNumber': '123'}},
            need_update=['FCONTACTUNIT', 'FCONTACTUNITTYPE'], retries=1, validate_flag=False)

    def test_contact_read_failure_does_not_lose_created_bill(self):
        self.kd._post.return_value = {"Result": {"ResponseStatus": {
            "IsSuccess": True, "SuccessEntitys": [{"Id": 101818}]}}}
        self.kd.view.side_effect = SystemExit('read failed')
        result = portal_actions.push("", "ETR123", True, "222")
        self.assertEqual(result['fid'], '101818')
        self.assertIn('已生成', result['warning'])
        self.kd.save.assert_not_called()

    def test_existing_target_contact_is_not_overwritten(self):
        self.kd.view.return_value = {'CONTACTUNIT': {'Number': 'original'}}
        warning = portal_actions._inherit_contact(self.kd, 'source', 'target', '222', 'ETR123', '333')
        self.assertEqual(warning, '')
        self.kd.save.assert_not_called()

    def test_wrong_source_is_not_used(self):
        self.kd.view.side_effect = [{}, {'BillNo': 'OTHER', 'TOCONTACTUNITTYPE': 'BD_Empinfo',
                                        'TOCONTACTUNIT': {'Number': '123'}}]
        warning = portal_actions._inherit_contact(self.kd, 'source', 'target', '222', 'ETR123', '333')
        self.assertIn('不匹配', warning)
        self.kd.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
