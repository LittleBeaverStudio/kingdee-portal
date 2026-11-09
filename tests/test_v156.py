"""Regressions for local organization projections, authorization persistence and forecast math."""
import copy
import datetime as dt
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['PORTAL_NO_WARMUP'] = '1'
os.environ['PORTAL_STATE_DIR'] = tempfile.mkdtemp(prefix='kingdee-unit-')
from invoice_config import merge_org_rows
from financial_health import working_capital
import metrics_cashflow as cash
import metrics_expense as expense
import metrics_sales as sales
import metrics_mgmt_full as mgmt
import app
import store


class InvoicePersistence(unittest.TestCase):
    def setUp(self):
        self.cos = [dict(id='a', client_id='old', client_secret='secret-a', ghf_mc='示例公司甲', kingdee_orgs='101'),
                    dict(id='b', client_id='company-b', client_secret='secret-b', ghf_mc='示例公司乙', kingdee_orgs='102')]

    def test_subset_save_preserves_missing_company_and_secrets(self):
        out = merge_org_rows(self.cos, [dict(org_no='101', client_id='new', client_secret='')])
        self.assertEqual(out[1], self.cos[1])
        self.assertEqual(out[0]['client_secret'], 'secret-a')
        self.assertEqual(out[0]['id'], 'a')
        self.assertEqual(self.cos[0]['client_id'], 'old')

    def test_blank_cells_never_delete(self):
        self.assertEqual(merge_org_rows(self.cos, [dict(org_no='102', client_id='')]), self.cos)

    def test_split_shared_authorization(self):
        cos = [dict(self.cos[0], kingdee_orgs='101,102')]
        out = merge_org_rows(cos, [dict(org_no='101', client_id='new')])
        self.assertEqual(out[0]['kingdee_orgs'], '102')
        self.assertEqual(out[1]['kingdee_orgs'], '101')
        self.assertEqual(out[1]['client_secret'], 'secret-a')
        self.assertNotEqual(out[0]['id'], out[1]['id'])

    def test_join_existing_client(self):
        out = merge_org_rows(self.cos, [dict(org_no='102', client_id='old')])
        self.assertEqual(out[0]['kingdee_orgs'], '101,102')
        self.assertEqual(out[0]['client_secret'], 'secret-a')
        self.assertEqual(out[1]['kingdee_orgs'], '')

    def test_saved_org_remains_visible_and_no_secret_in_payload(self):
        tenant = dict(code='synthetic', piazzone=dict(companies=self.cos), admin_users=['user'])
        payload = app._pz_payload(tenant, 'user', [{'number': '101', 'name': '组织一'}])
        self.assertEqual([x['no'] for x in payload['orgs']], ['101', '102'])
        self.assertNotIn('secret-b', repr(payload))
        self.assertTrue(payload['revision'])

    def test_sqlite_revision_compare_and_swap(self):
        c = store._db()
        c.execute("INSERT INTO tenants(code,name,server,acct,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                  ('test-cas', 'test', 'https://example.invalid', '', 'active', 1, 1))
        c.commit()
        before = store.piazzone_revision({})
        self.assertTrue(store.set_tenant_piazzone('test-cas', dict(companies=self.cos), before))
        self.assertFalse(store.set_tenant_piazzone('test-cas', dict(companies=[]), before))
        self.assertEqual(len(store.get_tenant('test-cas')['piazzone']['companies']), 2)


class DataMath(unittest.TestCase):
    def test_ccc_inputs_leaf_dedup_missing_and_credit_sign(self):
        level = {'1122': dict(begin=99999, end=99999), '1122.01': dict(begin=90, end=110),
                 '2202': dict(begin=-70, end=-90), '1405': dict(begin=30, end=50),
                 '6001': dict(debit=0, credit=300), '6401': dict(debit=200, credit=0)}
        d = working_capital(level, '101', 'book', 2026, 9)
        self.assertEqual((d['ar'], d['ap'], d['inventory'], d['revenue'], d['cost'], d['days']), (100, 80, 40, 300, 200, 30))
        del level['1405']
        self.assertIsNone(working_capital(level, '101', 'book', 2026, 9)['inventory'])
        level['6001']['valid_flow'] = False
        self.assertIsNone(working_capital(level, '101', 'book', 2026, 9)['revenue'])

    def test_general_ledger_reports_are_bound_to_the_organization_book(self):
        class GL:
            def query_all(self, *args, **kw): return []
            def sys_report(self, report, fields, model, **kw):
                self.book = model['FACCTBOOKID']['FNumber']
                return []
        gl = GL()
        d = mgmt.src_est(gl, ['101'], dt.date.today(), '', lambda *_: None,
                         books=[dict(org_number='101', book_number='book-a')])
        self.assertEqual(gl.book, 'book-a')
        self.assertFalse(d['errors'])
        d = mgmt.src_est(gl, ['102'], dt.date.today(), '', lambda *_: None,
                         books=[dict(org_number='101', book_number='book-a')])
        self.assertTrue(d['errors'])
        self.assertIsNone(d['cards'][0]['value'])

    def test_expense_reuses_four_form_results_for_all_organizations(self):
        def fetch(client, form, family, **kw):
            fields = expense.BASE_FIELDS[family].split(',')
            rows = []
            for org, amount in [('101', 100), ('102', 200)]:
                row = dict(FID=org, FBillNo=org, FDate='2026-10-01', FDocumentStatus='C', FCancelStatus='A',
                           FCloseStatus='A', FIsBorrow=False, FOrgAmountSum=amount, FExpAmountSum=amount,
                           FReimbNotPayAmount=amount, FExpenseAmount=amount)
                row.update({'FOrgId.FNumber': org, 'FOrgId.FName': org, 'FPayOrgId.FNumber': org, 'FPayOrgId.FName': org})
                rows.append([row.get(k, 0) for k in fields])
            return rows, fields, [], []
        with patch.object(expense, 'fetch_form', side_effect=fetch) as q:
            d = expense.build(None)
        self.assertEqual(q.call_count, 4)
        self.assertEqual(d['by_org']['101']['metrics']['unpaid'], 200)
        self.assertEqual(d['by_org']['102']['metrics']['unpaid'], 400)
        self.assertEqual(d['metrics']['unpaid'], 600)

    def test_missing_fund_position_does_not_create_a_zero_based_forecast(self):
        with patch.object(cash, 'fetch_fund', return_value=({}, ['denied'], {})), \
             patch.object(cash, '_fetch_side', return_value=[]), \
             patch.object(cash, 'fetch_paybills', return_value=[]), \
             patch.object(cash, 'project_pipeline', return_value=([], {})):
            d = cash.build(None, orgs=[dict(number='101', name='one')])
        self.assertEqual(d['curve']['total'], [None] * 12)
        self.assertEqual(d['org_breaches'], [])

    def test_session_cache_does_not_cross_accounts_or_sessions(self):
        a = dict(tenant='same', info=dict(username='ceo'))
        b = dict(tenant='same', info=dict(username='sales'))
        c = dict(tenant='same', info=dict(username='ceo'))
        self.assertNotEqual(app._ck(a, 'expense'), app._ck(b, 'expense'))
        self.assertNotEqual(app._ck(a, 'expense'), app._ck(c, 'expense'))

    def test_sales_organization_aggregates_without_extra_fetch(self):
        row = {k: '' if k in sales.TEXT_COLS else 0 for k in sales.COLS}
        def raw(org, amount):
            d = dict(row, sale_org=org, amount=amount, receivable_amount=amount,
                     customer='同一客户', bill_date=dt.date.today().isoformat(), bill_no=org)
            return [d[k] for k in sales.COLS]
        with patch.object(sales, 'fetch_rows', side_effect=[([raw('org1', 100), raw('org2', 200)], []), ([], [])]) as fetch:
            d = sales.build(None)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(d['by_org']['org1']['summary']['amount'], 100)
        self.assertEqual(d['by_org']['org2']['summary']['amount'], 200)

    def test_cash_org_forecasts_do_not_multiply_receipts_and_payments(self):
        today = dt.date.today()
        rec = cash._Rec(billno='test', org_name='one', org='101', party='customer', orig=120, left=120, due=today, created=today)
        with patch.object(cash, 'fetch_fund', return_value=({'101': 1000, '102': 2000}, [], {})), \
             patch.object(cash, '_fetch_side', side_effect=[[rec], []]), \
             patch.object(cash, 'fetch_paybills', return_value=[dict(org='102', category='salary', date=today, amount=50)]), \
             patch.object(cash, 'project_pipeline', return_value=([], {})):
            d = cash.build(None, orgs=[dict(number='101', name='one'), dict(number='102', name='two')])
        self.assertEqual(d['curve']['orgs']['102'][-1], 1900)
        self.assertAlmostEqual(d['curve']['orgs']['101'][-1], 1000 + d['kpi']['ar_expect'], places=1)
        for i, total in enumerate(d['curve']['total']):
            self.assertAlmostEqual(total, sum(x[i] for x in d['curve']['orgs'].values()), places=2)

    def test_no_permission_invoice_and_stock_counts_are_unknown(self):
        class Denied:
            def query_all(self, *a, **k): raise RuntimeError('denied')
            def sys_report(self, *a, **k): raise RuntimeError('denied')
        for d in [mgmt.src_invoice(Denied(), ['101'], '2026-01-01', lambda *_: None),
                  mgmt.src_anom(Denied(), ['101'], dt.date.today(), lambda *_: None)]:
            self.assertTrue(d['errors'])
            self.assertIsNone(d['cards'][0]['value'])


if __name__ == '__main__':
    unittest.main()
