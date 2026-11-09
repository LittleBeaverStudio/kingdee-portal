"""v1.5.8 回归：现金流口径自洽 + 销售未结清单与排行维度。

用合成数据，不连接任何金蝶账套。
"""
import collections
import datetime as dt
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import metrics_cashflow as mc
import metrics_sales as ms

TODAY = dt.date(2026, 10, 29)


def _sales_row(bill, customer, salesperson, material, bill_type, amount, settled, month):
    """构造一行规范化销售明细（字段与 metrics_sales.normalize 输出一致）。"""
    receivable = amount
    return {"bill_no": bill, "bill_date": month + "-10", "bill_type": bill_type,
            "salesperson": salesperson, "customer": customer, "material": material,
            "qty": 1.0, "price": amount, "amount": amount, "is_free": "",
            "receivable_qty": 1.0, "receivable_amount": receivable,
            "adjustment_amount": 0.0, "invoice_qty": 1.0, "invoice_amount": amount,
            "receipt_settlement_amount": settled, "settlement_adjustment": 0.0,
            "special_writeoff": 0.0, "bill_month": month,
            "unsettled_amount": receivable - settled,
            "uninvoiced_amount": max(0.0, receivable - amount),
            "unsettled_qty": 0.0}


class UnsettledBills(unittest.TestCase):
    def test_zero_unsettled_bills_are_excluded(self):
        rows = [
            _sales_row("A", "客户甲", "销售乙", "物料丙", "标准出库", 100.0, 100.0, "2026-03"),
            _sales_row("B", "客户甲", "销售乙", "物料丙", "标准出库", 100.0, 50.0, "2026-03"),
        ]
        out = ms._unsettled_bills(rows)
        self.assertEqual([r["bill_no"] for r in out], ["B"])
        self.assertTrue(all(abs(r["unsettled_amount"]) >= 0.01 for r in out))

    def test_over_settled_bill_is_kept_as_anomaly(self):
        rows = [_sales_row("C", "客户甲", "销售乙", "物料丙", "标准出库", 100.0, 130.0, "2026-03")]
        out = ms._unsettled_bills(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["unsettled_amount"], -30.0)

    def test_top_limit_counts_only_real_unsettled_bills(self):
        rows = [_sales_row("ZERO", "客户甲", "销售乙", "物料丙", "标准出库", 10.0, 10.0, "2026-03")]
        rows += [_sales_row("B%d" % i, "客户甲", "销售乙", "物料丙", "标准出库",
                            100.0, 50.0, "2026-03") for i in range(5)]
        out = ms._unsettled_bills(rows, top=3)
        self.assertEqual(len(out), 3)
        self.assertNotIn("ZERO", [r["bill_no"] for r in out])


class DimensionTrend(unittest.TestCase):
    def test_trend_uses_the_same_objects_and_order_as_the_ranking(self):
        rows = [
            _sales_row("A1", "客户甲", "销售乙", "物料丙", "标准出库", 500.0, 100.0, "2026-01"),
            _sales_row("A2", "客户甲", "销售乙", "物料丙", "标准出库", 500.0, 100.0, "2026-02"),
            _sales_row("B1", "客户丁", "销售戊", "物料己", "标准出库", 900.0, 800.0, "2026-01"),
        ]
        grouped = [r["name"] for r in ms._group(rows, "customer", "客户")]
        trend = ms._dimension_trend(rows)
        names = []
        for r in trend:
            if r["dim"] == "customer" and r["name"] not in names:
                names.append(r["name"])
        self.assertEqual(names, grouped)
        # 同一对象跨月合并为一条轨迹
        jan = [r for r in trend if r["dim"] == "customer" and r["name"] == "客户甲" and r["month"] == "2026-01"]
        feb = [r for r in trend if r["dim"] == "customer" and r["name"] == "客户甲" and r["month"] == "2026-02"]
        self.assertEqual(jan[0]["amount"], 500.0)
        self.assertEqual(feb[0]["amount"], 500.0)
        # 未结算金额按同一口径聚合
        self.assertEqual(jan[0]["unsettled_amount"], 400.0)

    def test_all_four_dimensions_are_produced(self):
        rows = [_sales_row("A1", "客户甲", "销售乙", "物料丙", "标准出库", 500.0, 100.0, "2026-01")]
        dims = {r["dim"] for r in ms._dimension_trend(rows)}
        self.assertEqual(dims, {"customer", "salesperson", "material", "bill_type"})


class _FakeClient:
    """按 form_id 返回合成行的金蝶客户端替身。"""

    def __init__(self, ar_rows, ap_rows, pay_rows, fund_rows):
        self.ar_rows, self.ap_rows, self.pay_rows, self.fund_rows = ar_rows, ap_rows, pay_rows, fund_rows
        self.calls = []

    def query_all(self, form_id, fields, filter_string="", page=2000, max_rows=200000, order=""):
        self.calls.append((form_id, filter_string))
        if form_id == "AR_receivable":
            return self.ar_rows
        if form_id == "AP_Payable":
            return self.ap_rows
        if form_id == "AP_PayBill":
            return self.pay_rows
        return []

    def sys_report(self, report_id, fields, model, scheme_id=""):
        return self.fund_rows


def _ar_row(bill, party, due, left, created):
    # FID,FBillNo,FCUSTOMERID.FName,FSETTLEORGID.FNumber,FSETTLEORGID.FName,
    # FENDDATE_H,FALLAMOUNT_D,FWRITTENOFFSTATUS,FNORECEIVEAMOUNT,FCREATEDATE
    return [1, bill, party, "101", "测试组织", due.isoformat(), left * 2, "A",
            left, created.isoformat()]


def _ap_row(bill, party, due, left, created):
    return [1, bill, party, "101", "测试组织", due.isoformat(), left * 2, "A",
            left, created.isoformat()]


def _fund_row(today_bal):
    # 至少 17 列，取 13/14/15/16 = 期初/收入/支出/本日余额
    row = [0] * 17
    row[3], row[13], row[16] = "账户A", 1000.0, today_bal
    return row


class CashCaliber(unittest.TestCase):
    """现金流：曲线与期望口径必须一致（这是「走势向下却没有应付流出期望」的根因）。"""

    def _build(self, periodic=True):
        ar = [_ar_row("AR1", "客户甲", TODAY + dt.timedelta(days=20), 1000.0, TODAY - dt.timedelta(days=10))]
        ap = [_ap_row("AP1", "供应商乙", TODAY + dt.timedelta(days=20), 800.0, TODAY - dt.timedelta(days=10))]
        pay = []
        if periodic:
            pay = [[1, (TODAY - dt.timedelta(days=30)).isoformat(), "101", "工资发放付款单",
                    "工资", 500.0]]
        client = _FakeClient(ar, ap, pay, [_fund_row(10000.0)])
        return mc.build(client, orgs=[{"number": "101", "name": "测试组织"}], org_numbers=["101"])

    def test_net_flow_expect_equals_the_curve_delta(self):
        data = self._build()
        k = data["kpi"]
        curve = data["curve"]["orgs"]["101"]
        self.assertIsNotNone(k["ar_in_expect"])
        self.assertIsNotNone(k["ap_out_expect"])
        self.assertAlmostEqual(k["net_flow_expect"],
                               k["ar_in_expect"] - k["ap_out_expect"], places=2)
        # 曲线末值 − 起始头寸 = 12 周期望流入 − 期望流出
        position = data["position"]["101"]
        self.assertAlmostEqual(curve[-1] - position, k["net_flow_expect"], places=2)

    def test_outflow_covers_ap_pipeline_and_periodic_payments(self):
        data = self._build()
        k = data["kpi"]
        self.assertGreater(k["periodic_total"], 0)
        # 周期付款属于流出，必须计入 ap_out_expect，否则「曲线向下但没有应付流出」
        self.assertAlmostEqual(k["ap_out_expect"],
                               k["ap_expect"] + k["pipe_ap"] + k["periodic_total"], places=2)
        self.assertGreater(k["ap_out_expect"], k["ap_expect"])

    def test_per_org_kpi_carries_the_same_caliber(self):
        data = self._build()
        per = data["kpi_by_org"]["101"]
        self.assertAlmostEqual(per["net_flow_expect"],
                               per["ar_in_expect"] - per["ap_out_expect"], places=2)
        self.assertAlmostEqual(per["ap_out_expect"],
                               per["ap_expect"] + per["pipe_ap"] + per["periodic_total"], places=2)

    def test_curve_declines_when_periodic_payments_exceed_inflow(self):
        data = self._build()
        curve = data["curve"]["orgs"]["101"]
        self.assertLess(curve[-1], curve[0])


if __name__ == "__main__":
    unittest.main()
