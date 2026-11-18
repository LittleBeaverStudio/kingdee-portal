"""Settlement invariants and the complete server submission gate; no live writes."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("PORTAL_NO_WARMUP", "1")
os.environ.setdefault("PORTAL_STATE_DIR", tempfile.mkdtemp(prefix="portal-payment-test-"))
import reimburse_payment as payment
import portal_actions
import app


class Kingdee:
    def __init__(self, amount=6, balance=8, actual=8, borrow=True, travel=False):
        self.form = payment.TRAVEL if travel else payment.EXPENSE
        self.source_form = "ER_ExpenseRequest_Travel" if travel else "ER_ExpenseRequest"
        self.bill = {"BillNo": "BX-TEST", "DocumentStatus": "A", "ExpAmountSum": amount,
                     "ReqAmountSum": 0, "RequestType": "0", "FSrcEntry": [],
                     "RecInvInfo": [{"RecInv": {"Id": 19, "FBillNo": "SPD-TEST", "FSUMALLAMOUNT": 20}}],
                     "ER_ExpenseReimbEntry": [{"Id": 20, "ExpenseAmount": amount, "BorrowAmount": 0,
                         "SourceBillType": self.source_form, "SourceBillNo": "REQ-TEST",
                         "FEntity_Link": [{"SBillId": "100", "SId": "200"}]}]}
        self.source = {"BillNo": "REQ-TEST", "DocumentStatus": "C", "IsBorrow": borrow,
                       "FEntity": [{"Id": 200, "ActualBorrowAmount": actual, "TurnoverAmount": balance}]}
        self.rows, self.calls = [], []
        self.save_error = self.corrupt = self.submit_timeout = self.read_timeout = False
        self.submit_status = "B"

    def view(self, form, fid):
        if self.read_timeout and self.bill["DocumentStatus"] == "B":
            raise SystemExit("timeout")
        return copy.deepcopy(self.bill if form == self.form else self.source)

    def query(self, *args, **kwargs):
        return copy.deepcopy(self.rows)

    def _post(self, path, payload, **kwargs):
        data = json.loads(payload["data"])
        self.calls.append((path, data, kwargs))
        if "Save.common" in path:
            if self.save_error:
                return {"Result": {"ResponseStatus": {"IsSuccess": False, "Errors": [{"Message": "mandatory"}]}}}
            model = data["Model"]
            self.bill["RequestType"] = model["FRequestType"]
            entry = self.bill["ER_ExpenseReimbEntry"][0]
            for key, value in model["FEntity"][0].items():
                if key != "FEntryID":
                    entry[key[1:]] = value
            self.bill["ReqAmountSum"] = entry["RequestAmount"]
            for key in ("SrcOffSetAmount", "RELRQAMOUNT"):
                entry["FEntity_Link"][0][key] = entry[key]
            if self.corrupt:
                entry["BorrowAmount"] *= 2
            self.bill["DocumentStatus"] = "A"
        else:
            self.bill["DocumentStatus"] = self.submit_status
            if self.submit_timeout:
                raise TimeoutError("response lost")
        return {"Result": {"ResponseStatus": {"IsSuccess": True}}}


class PaymentTests(unittest.TestCase):
    def submit(self, kd, choice="auto", token=None):
        token = token or payment.preview(kd, "300", kd.form == payment.TRAVEL)["snapshot"]
        return payment.submit_checked(kd, "service.", "300", kd.form == payment.TRAVEL,
                                      choice, token, lambda: {"ok": True})

    def test_all_business_outcomes_for_both_forms(self):
        for travel in (False, True):
            for amount, balance, choice, kind, requested, offset in (
                    (15, 8, "auto", "1", 7, 8), (8, 8, "auto", "0", 0, 8),
                    (6, 8, "retain", "0", 0, 6), (6, 8, "refund", "2", 2, 8),
                    (10, 0, "auto", "1", 10, 0)):
                with self.subTest(travel=travel, amount=amount, choice=choice):
                    kd = Kingdee(amount, balance, travel=travel)
                    r = self.submit(kd, choice)
                    self.assertTrue(r["ok"])
                    self.assertEqual(r["after_status"], "B")
                    self.assertEqual((r["payment"]["request_type"], r["payment"]["request_amount"],
                                      r["payment"]["offset"]), (kind, requested, offset))
                    self.assertEqual(len(kd.calls), 2)
                    self.assertEqual(kd.calls[0][1]["ValidateFlag"], "false")
                    self.assertTrue(kd.calls[0][1]["Model"]["FEntity"])
                    self.assertTrue(all(c[2]["retries"] == 1 for c in kd.calls))

    def test_nonborrow_uses_full_amount_for_source_link(self):
        kd = Kingdee(10, 0, 0, borrow=False)
        r = self.submit(kd)
        self.assertEqual((r["payment"]["borrow"], r["payment"]["offset"]), (0, 10))

    def test_preview_does_not_write_and_never_defaults_ambiguous_choice(self):
        kd = Kingdee()
        p = payment.preview(kd, "300", False)
        self.assertTrue(p["needs_choice"])
        self.assertEqual([o["choice"] for o in p["options"]], ["retain", "refund"])
        self.assertEqual(kd.calls, [])
        with self.assertRaisesRegex(ValueError, "请选择"):
            self.submit(kd, "auto", p["snapshot"])
        self.assertEqual(kd.calls, [])

    def test_loan_flag_is_not_actual_loan(self):
        kd = Kingdee(10, 0, 0, borrow="true")
        p = payment.preview(kd, "300", False)
        self.assertFalse(p["needs_choice"])
        self.assertEqual(p["options"][0]["request_amount"], 10)

    def test_changed_amount_balance_invoice_and_source_are_rejected(self):
        for change in ("amount", "balance", "invoice", "link", "source_flag"):
            kd = Kingdee()
            token = payment.preview(kd, "300", False)["snapshot"]
            if change == "amount":
                kd.bill["ExpAmountSum"] = kd.bill["ER_ExpenseReimbEntry"][0]["ExpenseAmount"] = 5
            elif change == "balance":
                kd.source["FEntity"][0]["TurnoverAmount"] = 7
            elif change == "invoice":
                kd.bill["RecInvInfo"][0]["RecInv"]["FSUMALLAMOUNT"] = 21
            elif change == "link":
                kd.bill["ER_ExpenseReimbEntry"][0]["FEntity_Link"][0]["SId"] = "201"
            else:
                kd.source["IsBorrow"] = False
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.submit(kd, "retain", token)
            self.assertEqual(kd.calls, [])

    def test_signed_token_rejects_tampering_expiry_and_other_bill(self):
        kd = Kingdee()
        token = payment.preview(kd, "300", False)["snapshot"]
        for wrong in ("", "0:" + token.split(":")[1], token + "bad"):
            with self.assertRaises(ValueError):
                payment.apply(kd, "300", False, "refund", wrong)
        with patch.object(payment.time, "time", return_value=float(token.split(":")[0]) + 901):
            with self.assertRaises(ValueError):
                payment.apply(kd, "300", False, "refund", token)
        with self.assertRaises(ValueError):
            payment.apply(kd, "301", False, "refund", token)
        self.assertEqual(kd.calls, [])

    def test_unknown_and_pending_sources_block(self):
        for status in ("A", "B", "Z", "D", "?"):
            kd = Kingdee()
            kd.rows = [[301, status, "100", "200"]]
            with self.subTest(status=status), self.assertRaises(ValueError):
                payment.preview(kd, "300", False)
        kd.rows = [[300, "A", "100", "200"], [301, "C", "100", "200"]]
        payment.preview(kd, "300", False)  # 自己的占用和已审核单不重复扣减余额。

    def test_bad_balances_source_flags_and_multiple_sources_block(self):
        for balance, actual in ((-1, 8), (9, 8), (None, 8), ("NaN", 8)):
            with self.subTest(balance=balance), self.assertRaises(ValueError):
                payment.preview(Kingdee(balance=balance, actual=actual), "300", False)
        for flag in (None, "unknown"):
            with self.assertRaises(ValueError):
                payment.preview(Kingdee(borrow=flag), "300", False)
        kd = Kingdee(); kd.bill["ER_ExpenseReimbEntry"][0]["FEntity_Link"].append({})
        with self.assertRaises(ValueError):
            payment.preview(kd, "300", False)

    def test_saved_mismatch_and_strict_validation_failure_never_submit(self):
        for mode in ("corrupt", "save_error"):
            kd = Kingdee(); setattr(kd, mode, True)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.submit(kd, "refund")
            self.assertEqual(len(kd.calls), 1)
            self.assertIn("Save.common", kd.calls[0][0])

    def test_invoice_failure_and_missing_confirmation_never_write(self):
        kd = Kingdee()
        token = payment.preview(kd, "300", False)["snapshot"]
        r = payment.submit_checked(kd, "service.", "300", False, "refund", token,
                                   lambda: {"ok": False, "issues": ["发票不足"]})
        self.assertFalse(r["ok"])
        with self.assertRaises(ValueError):
            payment.submit_checked(kd, "service.", "300", False, "refund", "", lambda: {"ok": True})
        self.assertEqual(kd.calls, [])

    def test_changed_balance_between_save_and_submit_blocks(self):
        kd = Kingdee(); count = 0
        token = payment.preview(kd, "300", False)["snapshot"]
        def check():
            nonlocal count
            count += 1
            if count == 2:
                kd.source["FEntity"][0]["TurnoverAmount"] = 7
            return {"ok": True}
        with self.assertRaisesRegex(ValueError, "已变化"):
            payment.submit_checked(kd, "service.", "300", False, "refund", token, check)
        self.assertEqual(len(kd.calls), 1)

    def test_timeout_confirms_state_without_resubmitting(self):
        kd = Kingdee(); kd.submit_timeout = True
        r = self.submit(kd, "refund")
        self.assertTrue(r["ok"])
        self.assertTrue(r["confirmed_after_timeout"])
        self.assertEqual(len(kd.calls), 2)
        with self.assertRaisesRegex(ValueError, "状态"):
            self.submit(kd, "refund")
        self.assertEqual(len(kd.calls), 2)

    def test_unconfirmed_submit_and_read_timeout_are_not_success(self):
        kd = Kingdee(); kd.submit_timeout = True; kd.submit_status = "A"
        self.assertFalse(self.submit(kd, "refund")["ok"])
        kd = Kingdee(); kd.read_timeout = True
        with self.assertRaisesRegex(RuntimeError, "勿重复提交"):
            self.submit(kd, "refund")

    def test_route_calls_same_gate_for_both_forms_and_rejects_bypass(self):
        for travel in (False, True):
            kd = Kingdee(travel=travel)
            with patch.object(app, "_check_csrf"), patch.object(app, "_write_gate"), \
                 patch.object(app, "_require_session", return_value={"info": {"username": "test"}}), \
                 patch.object(app, "_bind_actor"), patch.object(app, "_audit"), \
                 patch.object(portal_actions, "get_client", return_value=(kd, type("RL", (), {"KSVC_SUFFIX": "service."}), "")), \
                 patch.object(portal_actions, "precheck", return_value={"ok": True}):
                client = app.app.test_client()
                body = {"fid": "300", "is_travel": travel, "confirm": "SUBMIT"}
                self.assertFalse(client.post("/api/reimburse/submit", json=body).json["ok"])
                self.assertEqual(kd.calls, [])
                preview = client.get("/api/reimburse/payment/300" + ("?travel=1" if travel else "")).json
                self.assertTrue(preview["ok"])
                body.update(payment_choice="retain", payment_snapshot=preview["payment"]["snapshot"])
                response = client.post("/api/reimburse/submit", json=body).json
                self.assertTrue(response["ok"], response)
                self.assertEqual(kd.bill["DocumentStatus"], "B")


if __name__ == "__main__":
    unittest.main()
