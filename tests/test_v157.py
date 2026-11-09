"""Financial statement regressions: actual API shape, period, organization and units."""
import calendar
import datetime as dt
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import financial_reports as fr


def statement_payload(year=2026, month=9, name='测试组织', scale='元', status='0'):
    balance = [[0, 0, '资产负债表'], [1, 0, f'{year}年{month:02}月{calendar.monthrange(year, month)[1]}日'],
               [2, 0, '编制单位：'+name], [2, 5, '单位：'+scale],
               [3, 1, '期末余额'], [3, 2, '上年年末余额'], [3, 4, '期末余额'], [3, 5, '上年年末余额'],
               [9, 0, '    应收账款'], [9, 1, '150,000.00'], [9, 2, '100,000.00'],
               [9, 3, '    应付账款'], [9, 4, '50,000.00'], [9, 5, '30,000.00'],
               [13, 0, '    存货'], [13, 1, '100,000.00'], [13, 2, '60,000.00']]
    profit = [[0, 0, '利润表'], [1, 0, f'{year}年{month:02}月'], [2, 0, '编制单位：'+name], [2, 3, '单位：'+scale],
              [3, 1, '本期金额'], [3, 2, '本年金额'], [3, 3, '上期金额'],
              [4, 0, '一、营业收入'], [4, 1, '300,000.00'], [4, 2, str(300000*month)], [4, 3, '999'],
              [5, 0, '    减:营业成本'], [5, 1, '150,000.00'], [5, 2, str(150000*month)], [5, 3, '888']]
    return {'status': status, 'result': json.dumps({'items': [{'xtype': 'kdspread', 'data': {'data': [balance, profit]}}]}, ensure_ascii=False)}


class FinancialStatements(unittest.TestCase):
    def test_native_current_and_ytd_columns_and_dual_sided_balance(self):
        row = fr.parse_statement(json.dumps(statement_payload()), '101', '测试组织', 2026, 9)
        self.assertEqual([row[k] for k in ['ar_end', 'ap_end', 'inventory_end', 'revenue', 'cost']], [150000, 50000, 100000, 300000, 150000])
        self.assertEqual((row['revenue_ytd'], row['cost_ytd']), (2700000, 1350000))
        self.assertEqual(row['days'], 30)

    def test_prior_month_close_and_year_open_are_different_bases(self):
        raw = [fr.parse_statement(statement_payload(month=m), '101', '测试组织', 2026, m) for m in [8, 9]]
        raw[0]['ar_end'] = 130000
        history, ytd = fr.assemble(raw, {(2026, 9)})
        self.assertEqual(history[0]['ar'], 140000)
        self.assertEqual(ytd[0]['ar'], 125000)
        self.assertEqual(ytd[0]['days'], 273)
        self.assertEqual(ytd[0]['revenue'], 2700000)
        self.assertNotEqual(ytd[0]['ar'], history[0]['ar'])

    def test_missing_prior_is_unknown_while_native_ytd_remains_available(self):
        row = fr.parse_statement(statement_payload(), '101', '测试组织', 2026, 9)
        history, ytd = fr.assemble([row], {(2026, 9)})
        self.assertIsNone(history[0]['ar'])
        self.assertEqual(ytd[0]['ar'], 125000)
        with self.assertRaises(fr.StatementError): fr.assemble([row, row], {(2026, 9)})

    def test_january_uses_year_open_and_leap_days(self):
        row = fr.parse_statement(statement_payload(year=2024, month=1), '101', '测试组织', 2024, 1)
        history, _ = fr.assemble([row], {(2024, 1)})
        self.assertEqual(history[0]['ar'], 125000)
        row = fr.parse_statement(statement_payload(year=2024, month=2), '101', '测试组织', 2024, 2)
        _, ytd = fr.assemble([row], {(2024, 2)})
        self.assertEqual(ytd[0]['days'], 60)

    def test_wrong_period_company_unit_permission_fail_closed(self):
        for payload in [statement_payload(month=8), statement_payload(name='其他公司'), statement_payload(scale='美元'), statement_payload(status='1')]:
            with self.subTest(payload=payload['status']):
                with self.assertRaises(fr.StatementError): fr.parse_statement(payload, '101', '测试组织', 2026, 9)
        row = fr.parse_statement(statement_payload(scale='万元'), '101', '测试组织', 2026, 9)
        self.assertEqual(row['ar_end'], 1500000000)

    def test_blank_masked_invalid_and_parenthesized_negative(self):
        for value in ['', None, '***', '—', float('nan'), float('inf')]: self.assertIsNone(fr.number(value))
        self.assertEqual(fr.number('(1,200.50)'), -1200.5)
        self.assertEqual(fr.number('0.00'), 0)

    def test_completed_months_exclude_current_and_cross_year(self):
        periods = fr.periods_before(2026, 10, today=dt.date(2026, 10, 20))
        self.assertEqual((periods[0], periods[-1]), ((2025, 10), (2026, 9)))
        self.assertEqual(fr.periods_before(2027, 1, today=dt.date(2026, 10, 20)), periods)
        self.assertEqual(fr.periods_before(2026, 1, today=dt.date(2026, 1, 2))[-1], (2025, 12))

    def test_background_direct_report_only_and_older_reports_survive_missing_latest(self):
        class Client:
            def __init__(self): self.models = []
            def _post(self, path, payload, **kwargs):
                self.assert_path = path
                model = json.loads(payload['parameters'][0]); self.models.append(model)
                return statement_payload(model['Year'], model['Period'], status='1' if (model['Year'], model['Period']) == (2026, 9) else '0')
        client = Client()
        result = fr.build(client, [{'number': '101', 'name': '测试组织'}, {'number': '999', 'name': '禁止取数'}], ['101'], 2026, 10)
        self.assertEqual(len(client.models), 13)
        self.assertTrue(all(m['OrgNumber'] == '101' for m in client.models))
        self.assertEqual(client.assert_path, fr.REPORT_PATH)
        self.assertEqual(len(result['working_capital_history']), 11)
        self.assertTrue(any(r['period'] == '2026-08' for r in result['working_capital_history']))
        self.assertTrue(result['capital_errors'])


if __name__ == '__main__': unittest.main()
