"""审批明细按单据类型自动识别：只显示有内容的列，中文名来自字段文档。"""
import os
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("PORTAL_NO_WARMUP", "1")
os.environ.setdefault("PORTAL_STATE_DIR", tempfile.mkdtemp(prefix="portal-detail-test-"))

import detail_fields
import portal_actions as pa


TRAVEL_ENTRY = {
    "Seq": 1, "FExpenseItemID": {"Name": "差旅费"},
    "FTravelStartDate": "2026/11/12", "FTravelEndDate": "2026/11/13",
    "F_TJKT_Base_qtr": {"Name": "昆明-永德-楚雄"},
    "FAirTicketCost": 109.0, "FLocalCost": 57.0,
    "FOtherRemoteCost": None, "FAccomFee": 159.0,
    "FTravelEndSite": "天津市", "FTravelSubsidy": 0,
    "FOtherExpense": None, "FOrgAmount": 335.0,
    "F_TJKT_Text_qtr": "", "FEntryCostDeptID": {"Name": "销售部"},
    "FDepartureID": None, "FArrivalID": None,
}


class DetailFieldsTests(unittest.TestCase):
    def test_travel_columns_only_keep_filled_fields(self):
        d = detail_fields.detail_view([dict(TRAVEL_ENTRY)], "ER_ExpenseRequest_Travel")
        labels = [c["t"] for c in d["columns"]]
        self.assertEqual(labels, ["序号", "费用项目", "开始日期", "结束日期", "目的地",
                                  "项目", "机票/火车票", "市内交通费", "住宿费",
                                  "申请金额", "费用承担部门"])
        self.assertNotIn("其他长途交通费", labels)   # None 不展示
        self.assertNotIn("出差补助", labels)         # 0 不展示
        self.assertNotIn("特殊备注", labels)         # 空串不展示
        row = dict(zip(labels, d["rows"][0]))
        self.assertEqual(row["开始日期"], "2026/11/12")
        self.assertEqual(row["项目"], "昆明-永德-楚雄")
        self.assertEqual(row["费用承担部门"], "销售部")
        nums = {c["t"]: c["num"] for c in d["columns"]}
        self.assertTrue(nums["申请金额"])
        self.assertFalse(nums["项目"])

    def test_checked_amount_is_last_column_and_aligned(self):
        rows = [dict(TRAVEL_ENTRY, Id=9001, FCheckedOrgAmount=800.5),
                dict(TRAVEL_ENTRY, Seq=2, Id=9002, FCheckedOrgAmount=100.25)]
        checked = [{"entry_id": "9001", "checked": 800.5},
                   {"entry_id": "9002", "checked": 100.25}]
        d = detail_fields.detail_view(rows, "ER_ExpenseRequest_Travel", checked=checked)
        self.assertEqual(d["columns"][-1],
                         {"t": "审核核定金额", "num": True, "money": True, "edit": True})
        self.assertEqual(len(d["rows"]), 2)
        self.assertEqual([r[-1] for r in d["rows"]], ["800.5", "100.25"])
        self.assertEqual([c["entry_id"] for c in d["checked"]], ["9001", "9002"])
        self.assertEqual(len(d["checked"]), len(d["rows"]))

    def test_checked_amount_skipped_empty_rows_keeps_alignment(self):
        rows = [dict(TRAVEL_ENTRY, Id=9001, FCheckedOrgAmount=10),
                {"FRemark": ""},
                dict(TRAVEL_ENTRY, Seq=3, Id=9003, FCheckedOrgAmount=30)]
        checked = [{"entry_id": "9001", "checked": 10},
                   {"entry_id": "", "checked": 0},
                   {"entry_id": "9003", "checked": 30}]
        d = detail_fields.detail_view(rows, "ER_ExpenseRequest_Travel", checked=checked)
        self.assertEqual(len(d["rows"]), 2)
        self.assertEqual([c["entry_id"] for c in d["checked"]], ["9001", "9003"])
        self.assertEqual([r[-1] for r in d["rows"]], ["10", "30"])

    def test_empty_entries_return_none(self):
        self.assertIsNone(detail_fields.detail_view([{"FRemark": ""}], "ER_ExpenseRequest"))

    def test_request_detail_exposes_dynamic_detail(self):
        kd = Mock()
        kd.view.return_value = {
            "BillNo": "CL-001", "Date": "2026-11-12T00:00:00", "DocumentStatus": "B",
            "StaffID": {"Name": "张三"}, "OrgId": {"Number": "101", "Name": "示例科技"},
            "OrgAmountSum": 335, "FEntity": [dict(TRAVEL_ENTRY)],
        }
        with patch.object(pa, "get_client", return_value=(kd, object(), "")):
            bill = pa.request_detail("", "1", True)
        self.assertEqual(bill["bill_no"], "CL-001")
        self.assertEqual(len(bill["detail"]["rows"]), 1)
        self.assertIn("开始日期", [c["t"] for c in bill["detail"]["columns"]])

    def test_reimbursement_detail_exposes_dynamic_detail(self):
        kd = Mock()
        kd.view.return_value = {
            "BillNo": "BX-001", "DocumentStatus": "B", "ExpAmountSum": 335,
            "FEntity": [{
                "Seq": 1, "FExpID": {"Name": "差旅费"}, "FExpenseAmount": 335,
                "FTravelStartDate": "2026/11/12", "FCityTrafficFee": 57,
                "FLodgingFee": 159, "F_TJKT_Base_83G": {"Name": "项目A"},
            }],
        }
        with patch.object(pa, "get_client", return_value=(kd, object(), "")):
            bill = pa.get_bill("", "2", True)
        labels = [c["t"] for c in bill["detail"]["columns"]]
        self.assertEqual(labels, ["序号", "费用项目", "开始日期", "项目",
                                  "市内交通费", "住宿费", "申请报销金额"])


if __name__ == "__main__":
    unittest.main()
