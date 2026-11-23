"""Any workflow form is listed and approvable; only expense forms keep the tailored renderer."""
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
os.environ.setdefault("PORTAL_STATE_DIR", tempfile.mkdtemp(prefix="portal-anyform-test-"))
import workflow_actions as w
import workflow_history as h


def row(form="CN_PAYAPPLY", fid="789", aid="pay1", status="0", uid=12, number="PAY-001"):
    return [aid, "付款申请", "2026-11-23T09:00:00", uid, number, form, fid, status,
            "财务经理", "100", "示例科技", "张三", "待审批"]


# 金蝶 View 的真实形态：大写带 F 前缀，基础资料拆成 Fxxx_Id / Fxxx_Name。
VIEW = {
    "FBILLNO": "PAY-001", "FDOCUMENTSTATUS": "B", "FDATE": "2026-11-20T00:00:00",
    "FAPPLYORGID_Id": 100, "FAPPLYORGID_Name": "示例科技",
    "FAPPLYAMOUNTFOR_H": 12345.67,
    "FCREATORID_Id": 1001, "FCREATORID_Name": "张三",
    "FCONTACTUNIT_Id": 2001, "FCONTACTUNIT_Name": "某供应商",
    "FPAYAPPLYENTRY": [
        {"Id": 101697, "Seq": 1, "FSETTLETYPEID_Id": 0, "FPAYAMOUNTFOR": 276000,
         "FEACHBANKACCOUNT": "0200231159200052417", "FAPPLYAMOUNTFOR": 276000},
    ],
}


class AnyFormTests(unittest.TestCase):
    def setUp(self):
        self.kd = Mock(base="https://example/k3cloud/")
        self.kd.cfg = {"acctid": "1"}
        self.rl = SimpleNamespace(KSVC_SUFFIX="service.")
        w._lookup_cache.clear(); w._lookup_miss.clear(); w._probe_expr.clear(); w._lookup_notes.clear()
        bound = patch.object(w.actions, "get_client", return_value=(self.kd, self.rl, ""))
        bound.start(); self.addCleanup(bound.stop)

    def test_payment_application_is_listed_without_type_filter(self):
        self.kd.query.return_value = [row()]
        tasks = w.list_tasks("", "12")
        self.assertEqual(tasks[0]["form_id"], "CN_PAYAPPLY")
        self.assertEqual(tasks[0]["kind"], "付款申请单")
        self.assertTrue(tasks[0]["can_open"])
        where = self.kd.query.call_args.args[2]
        self.assertNotIn("FObjectTypeId", where)
        self.assertIn("FReceiverId=12", where)

    def test_unmapped_form_keeps_identifier_and_is_still_approvable(self):
        self.kd.query.return_value = [row(form="ZZ_CustomBill", aid="z1")]
        self.assertEqual(w.list_tasks("", "12")[0]["kind"], "ZZ_CustomBill")
        self.kd.query.side_effect = [[row(form="ZZ_CustomBill", aid="z1")], [row(form="ZZ_CustomBill", aid="z1")],
                                     [row(form="ZZ_CustomBill", aid="z1")], [row(form="ZZ_CustomBill", aid="z1", status="1")]]
        self.kd._post.return_value = {"Result": {"ResponseStatus": {"IsSuccess": True}}}
        result = w.decide("", "12", "z1", "approve")
        self.assertTrue(result["ok"])
        payload = json.loads(self.kd._post.call_args.args[1]["data"])
        self.assertEqual(payload["FormId"], "ZZ_CustomBill")

    def test_task_without_numeric_bill_id_is_shown_but_not_openable(self):
        self.kd.query.return_value = [row(form="BD_Customer", fid="abc")]
        task = w.list_tasks("", "12")[0]
        self.assertFalse(task["can_open"])
        with self.assertRaisesRegex(ValueError, "单据内码"):
            w.detail("", "12", task["assignment_id"])

    def test_generic_detail_extracts_header_fields_and_entries(self):
        self.kd.view.return_value = VIEW
        bill = w.generic_detail("", "CN_PAYAPPLY", "789")
        self.assertEqual(bill["bill_no"], "PAY-001")
        self.assertEqual(bill["status_cn"], "审核中")
        self.assertEqual(bill["applicant"], "张三")
        self.assertEqual(bill["org_name"], "示例科技")
        self.assertAlmostEqual(bill["amount"], 12345.67)
        labels = [f["label"] for f in bill["fields"]]
        self.assertIn("往来单位", labels)
        self.assertIn("单据日期", labels)
        self.assertNotIn("FAPPLYORGID", labels)
        self.assertEqual([c["t"] for c in bill["entries"]["columns"]],
                         ["序号", "费用项目", "费用承担部门", "项目", "申请金额", "付款金额"])
        cols = [c["t"] for c in bill["entries"]["columns"]]
        self.assertEqual(bill["entries"]["rows"][0][cols.index("付款金额")], "276000")
        self.kd.view.assert_called_once_with("CN_PAYAPPLY", "789")

    def test_internal_ids_are_resolved_to_business_names(self):
        view = {
            "FBILLNO": "FKSQD00800", "FDOCUMENTSTATUS": "B", "FDATE": "2026-11-23T00:00:00",
            "FAPPLYORGID_Id": 100, "FCREATORID_Id": 2001,
            "FCONTACTUNIT_Id": 674550, "FCONTACTUNITTYPE": "BD_Supplier",
            "FPAYAMOUNTFOR_H": 276000, "FREMARK": "测试备注",
            "FCOSTID_Id": 31, "FEXPENSEDEPTID_Id": 7, "FPROJECTID_Id": 9,
            "FPAYAPPLYENTRY": [{"Seq": 1, "FCOSTID_Id": 31, "FPAYAMOUNTFOR": 276000}],
        }
        table = {
            "ORG_Organizations": [[100, "001", "示例科技"]],
            "BD_Empinfo": [[2001, "E001", "张三"]],
            "BD_Supplier": [[674550, "S001", "测试供应商"]],
            "BD_ExpenseItem": [[31, "C01", "差旅费"]],
            "BD_Department": [[7, "D01", "财务部"]],
            "BD_Project": [[9, "P01", "项目A"]],
        }
        self.kd.query.side_effect = lambda form, fields, where, *a, **kw: table.get(form, [])
        self.kd.view.return_value = view
        bill = w.generic_detail("", "CN_PAYAPPLY", "789")
        self.assertEqual(bill["applicant"], "张三")
        self.assertEqual(bill["org_name"], "示例科技")
        labels = {f["label"]: f["value"] for f in bill["fields"]}
        self.assertEqual(labels["往来单位"], "测试供应商")
        self.assertEqual(labels["费用项目"], "差旅费")
        self.assertEqual(labels["费用承担部门"], "财务部")
        self.assertEqual(labels["项目"], "项目A")
        self.assertEqual(bill["remark"], "测试备注")
        self.assertNotIn("创建日期", labels)
        self.assertNotIn("汇率", labels)
        self.assertNotIn("关闭状态", labels)
        self.assertEqual([c["t"] for c in bill["entries"]["columns"]],
                         ["序号", "费用项目", "费用承担部门", "项目", "付款金额"])

    def test_customized_payment_form_uses_documented_field_names(self):
        # 真实租户：View 只回内码，申请人/组织/备注/项目都是二开字段。
        view = {
            "FBILLNO": "FKSQD00800", "FDOCUMENTSTATUS": "B", "FDATE": "2026-11-23T00:00:00",
            "FAPPLYORGID_Id": 1, "FCREATORID_Id": 100,
            "FPAYAPPLYENTRY": [{"Id": 11, "Seq": 1, "FCOSTID_Id": 31, "FPAYAMOUNTFOR": 276000}],
        }
        answers = {
            "FAPPLYORGID.FName": [["示例科技"]],
            "FCREATORID.FName": [["张三"]],
            "F_TJKT_Text_qtr1": [["月结 30 天"]],
            "F_TJKT_Base_83g_re5_tzk.FName": [["项目A"]],
            "FEXPENSEITEMID,FNumber,FName": [[31, "C01", "差旅费"]],
        }
        self.kd.query.side_effect = lambda form, fields, where, *a, **kw: answers.get(fields, [])
        self.kd.view.return_value = view
        bill = w.generic_detail("", "CN_PAYAPPLY", "789")
        self.assertEqual(bill["applicant"], "张三")
        self.assertEqual(bill["org_name"], "示例科技")
        self.assertEqual(bill["remark"], "月结 30 天")
        cols = {c["t"]: i for i, c in enumerate(bill["entries"]["columns"])}
        self.assertEqual(bill["entries"]["rows"][0][cols["项目"]], "项目A")
        self.assertEqual(bill["entries"]["rows"][0][cols["费用项目"]], "差旅费")

    def test_generic_detail_rejects_non_numeric_bill_id(self):
        with self.assertRaises(ValueError):
            w.generic_detail("", "CN_PAYAPPLY", "x' OR 1=1")
        self.kd.view.assert_not_called()

    def test_payment_application_detail_uses_generic_renderer(self):
        self.kd.query.return_value = [row()]
        self.kd.view.return_value = VIEW
        result = w.detail("", "12", "pay1")
        self.assertEqual(result["render"], "generic")
        self.assertFalse(result["is_application"])
        self.assertEqual(result["bill"]["bill_no"], "PAY-001")

    def test_expense_form_still_uses_tailored_renderer(self):
        self.kd.query.return_value = [row(form="ER_ExpenseRequest", fid="456", number="REQ-1")]
        with patch.object(w.actions, "request_detail", return_value={"bill_no": "REQ-1"}) as detail:
            result = w.detail("", "12", "pay1")
        self.assertEqual(result["render"], "expense")
        self.assertTrue(result["is_application"])
        detail.assert_called_once_with("", "456", False)


class HistoryAnyFormTests(unittest.TestCase):
    def test_form_filter_accepts_known_and_custom_identifiers(self):
        for form in ("CN_PAYAPPLY", "ZZ_CustomBill"):
            self.assertEqual(h.parse({"form": form})["form"], form)
        for bad in ("x' OR 1=1", "a b", "A" * 61):
            with self.assertRaises(ValueError):
                h.parse({"form": bad})

    def test_headers_query_three_common_fields_for_unmapped_form(self):
        kd = Mock()
        calls = []

        def post(path, body):
            data = json.loads(body["data"]); calls.append(data)
            return [[789, "PAY-001", "B"]]

        kd._post.side_effect = post
        headers = h._headers(kd, SimpleNamespace(KSVC_SUFFIX="svc."),
                             [dict(form_id="CN_PAYAPPLY", bill_no="PAY-001")])
        self.assertEqual(headers[("CN_PAYAPPLY", "PAY-001")]["fid"], "789")
        self.assertIsNone(headers[("CN_PAYAPPLY", "PAY-001")]["amount"])
        self.assertEqual(calls[0]["FormId"], "CN_PAYAPPLY")
        self.assertEqual(calls[0]["FieldKeys"], "FID,FBillNo,FDocumentStatus")


if __name__ == "__main__":
    unittest.main()
