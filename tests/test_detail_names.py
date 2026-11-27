"""审批明细：编码转名称 + 金额列标记。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("PORTAL_NO_WARMUP", "1")
os.environ.setdefault("PORTAL_STATE_DIR", tempfile.mkdtemp(prefix="portal-names-test-"))

import detail_fields
import detail_names
import portal_actions as pa
import workflow_actions as wa


FORMULA_NAMES = {
    "FEntity_FExpenseItemID.FName": "差旅费",
    "FEntity_F_TJKT_Base_qtr.FName": "昆明-永德-楚雄",
    "FEntity_FEntryCostDeptID.FName": "销售部",
    "FEntity_FDepartureID.FName": "北京市",
    "FEntity_FArrivalID.FName": "天津市",
}


def fake_query(form, expr, where=None, top=None, **kwargs):
    if ".FName" in expr:
        parts = [p.strip() for p in str(expr).split(",")][1:]
        return [[1] + [FORMULA_NAMES.get(p, "") for p in parts]]
    if form == "BD_ExpenseItem":
        return [[1001, "FY001", "差旅费"]]
    if form == "BD_Department":
        return [[2001, "BM001", "销售部"]]
    return []


class DetailNamesTests(unittest.TestCase):
    def setUp(self):
        detail_names._miss.clear()
        detail_names._formula_miss.clear()

    def test_sibling_name_field_is_used_first(self):
        rows = detail_names.resolve(
            [{"FExpenseItemID": "FY001", "FExpenseItemID_Name": "差旅费"}],
            "ER_ExpenseRequest", kd=Mock())
        self.assertEqual(rows[0]["FExpenseItemID"], "差旅费")

    def test_base_data_lookup_by_number(self):
        kd = Mock()
        kd.query.side_effect = fake_query
        rows = detail_names.resolve(
            [{"FExpenseItemID": {"Number": "FY001"}, "FEntryCostDeptID": "BM001"}],
            "ER_ExpenseRequest", "123", kd, entry_keys=("NoMatchEntry",))
        self.assertEqual(rows[0]["FExpenseItemID"], "差旅费")
        self.assertEqual(rows[0]["FEntryCostDeptID"], "销售部")
        self.assertTrue(any("BD_" in call.args[0] for call in kd.query.call_args_list))
        # 关键回归：字母编码必须按 FNumber 查，不能按主键查（否则反查不到名称）。
        wheres = [call.args[2] for call in kd.query.call_args_list if len(call.args) > 2]
        self.assertTrue(any("FNumber" in w and "FY001" in w for w in wheres), wheres)

    def test_request_detail_resolves_codes_and_marks_money(self):
        kd = Mock()
        kd.query.side_effect = fake_query
        kd.view.return_value = {
            "BillNo": "CL-002", "Date": "2026-11-27T00:00:00", "DocumentStatus": "B",
            "StaffID": {"Name": "张三"}, "OrgId": {"Number": "101", "Name": "示例科技"},
            "OrgAmountSum": 335,
            "FEntity": [{
                "Seq": 1,
                "FExpenseItemID": {"Number": "FY001"},
                "FTravelStartDate": "2026/11/12", "FTravelEndDate": "2026/11/13",
                "F_TJKT_Base_qtr": {"Number": "XM001"},
                "FEntryCostDeptID": {"Number": "BM001"},
                "FDepartureID": "DQ001", "FArrivalID": "DQ002",
                "FAirTicketCost": 109.0, "FLocalCost": 57.0, "FAccomFee": 159.0,
                "FOrgAmount": 335.0,
            }],
        }
        with patch.object(pa, "get_client", return_value=(kd, object(), "")):
            bill = pa.request_detail("", "123", True)
        detail = bill["detail"]
        labels = [c["t"] for c in detail["columns"]]
        row = dict(zip(labels, detail["rows"][0]))
        self.assertEqual(row["费用项目"], "差旅费")
        self.assertEqual(row["项目"], "昆明-永德-楚雄")
        self.assertEqual(row["费用承担部门"], "销售部")
        self.assertNotIn("出发地（费控）", labels)
        self.assertNotIn("目的地（费控）", labels)
        flags = {c["t"]: (c["num"], c["money"]) for c in detail["columns"]}
        self.assertEqual(flags["申请金额"], (True, True))
        self.assertEqual(flags["机票/火车票"], (True, True))
        self.assertEqual(flags["住宿费"], (True, True))
        self.assertEqual(flags["序号"], (False, False))
        self.assertEqual(flags["项目"], (False, False))
        self.assertEqual(flags["开始日期"], (False, False))

    def test_generic_approval_resolves_codes_and_marks_numbers(self):
        kd = Mock()
        kd.query.side_effect = fake_query
        kd.view.return_value = {
            "FBillNo": "FK-001", "FDocumentStatus": "B", "FDate": "2026-11-27",
            "FEntity": [
                {"FSeq": 1, "FExpenseItemID": {"Number": "FY001"}, "FAmount": 1200.5},
                {"FSeq": 2, "FExpenseItemID": {"Number": "FY001"}, "FAmount": 800},
            ],
        }
        with patch.object(wa.actions, "get_client", return_value=(kd, object(), "")):
            result = wa.generic_detail("", "CN_PAYAPPLY", "123", "付款申请单")
        labels = [c["t"] for c in result["entries"]["columns"]]
        self.assertEqual(result["entries"]["rows"][0][labels.index("费用项目")], "差旅费")
        flags = {c["t"]: (c["num"], c["money"]) for c in result["entries"]["columns"]}
        self.assertEqual(flags["金额"], (True, True))

    def test_source_type_translated_and_hidden_columns(self):
        kd = Mock()
        kd.query.side_effect = fake_query
        rows = [{"Seq": 1, "FExpenseItemID": "FY001",
                 "FSourceBillType": "AP_Payable", "FSrcRowId": 10001,
                 "FDepartureID": "DQ001", "FArrivalID": "DQ002"}]
        resolved = detail_names.resolve(rows, "ER_ExpenseRequest", "123", kd,
                                        entry_keys=("FEntity",))
        self.assertEqual(resolved[0]["FSourceBillType"], "应付单")
        d = detail_fields.detail_view(resolved, "ER_ExpenseRequest")
        labels = [c["t"] for c in d["columns"]]
        self.assertIn("源单类型", labels)
        self.assertEqual(d["rows"][0][labels.index("源单类型")], "应付单")
        self.assertNotIn("源单行内码", labels)
        self.assertNotIn("出发地（费控）", labels)
        self.assertNotIn("目的地（费控）", labels)

    def test_generic_source_type_translated(self):
        kd = Mock()
        kd.query.side_effect = fake_query
        kd.view.return_value = {
            "FBillNo": "FK-002", "FDocumentStatus": "B", "FDate": "2026-11-27",
            "FEntity": [{"FSeq": 1, "FSOURCETYPE": "AP_Payable",
                         "FSRCROWID": 99, "FAmount": 100}],
        }
        with patch.object(wa.actions, "get_client", return_value=(kd, object(), "")):
            result = wa.generic_detail("", "CN_PAYAPPLY", "123", "付款申请单")
        labels = [c["t"] for c in result["entries"]["columns"]]
        self.assertEqual(result["entries"]["rows"][0][labels.index("源单类型")], "应付单")
        self.assertNotIn("源单行内码", labels)

    def test_payment_application_shows_request_amount(self):
        kd = Mock()
        kd.query.side_effect = fake_query
        kd.view.return_value = {
            "FBillNo": "FK-003", "FDocumentStatus": "B", "FDate": "2026-11-27",
            "FAPPLYAMOUNTFOR_H": 12345.67, "FPAYAMOUNTFOR_H": 0,
        }
        with patch.object(wa.actions, "get_client", return_value=(kd, object(), "")):
            result = wa.generic_detail("", "CN_PAYAPPLY", "123", "付款申请单")
        self.assertEqual(result["amount_label"], "申请付款金额")
        self.assertEqual(result["amount"], 12345.67)
        labels = [f["label"] for f in result["fields"]]
        self.assertIn("申请付款金额", labels)
        self.assertNotIn("应付金额", labels)

    def test_request_detail_accepts_forgid(self):
        kd = Mock()
        kd.query.side_effect = fake_query
        kd.view.return_value = {
            "BillNo": "CL-003", "Date": "2026-11-27T00:00:00", "DocumentStatus": "B",
            "FOrgID": {"Number": "101", "Name": "北京埃诺登"}, "FEntity": [],
        }
        with patch.object(pa, "get_client", return_value=(kd, object(), "")):
            bill = pa.request_detail("", "123", True)
        self.assertEqual(bill["org_no"], "101")
        self.assertEqual(bill["org_name"], "北京埃诺登")

    def test_amount_looks_like_code_is_left_alone(self):
        kd = Mock()
        kd.query.side_effect = fake_query
        rows = detail_names.resolve([{"FAmount": "1200.50", "FRemark": "FY001"}],
                                    "ER_ExpenseRequest", "123", kd,
                                    entry_keys=("FEntity",))
        self.assertEqual(rows[0]["FAmount"], "1200.50")
        self.assertEqual(rows[0]["FRemark"], "FY001")


if __name__ == "__main__":
    unittest.main()
