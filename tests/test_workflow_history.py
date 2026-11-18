"""Real history source routing, reviewer isolation, archive merge and paging."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app
import workflow_history as h
import workflow_actions as w


def record(aid="a1", uid=12, number="REQ-1", result="审核通过", form="ER_ExpenseRequest"):
    return [aid, uid, number, form, "2026-11-18T10:00:00.123", result, "历史意见", "101", "测试组织", "test", "1"]


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.kd = Mock()
        self.rl = SimpleNamespace(KSVC_SUFFIX="svc.")
        self.sources = {h.SOURCES[0]: [record()], h.SOURCES[1]: []}
        self.calls = []
        def post(path, body):
            data = json.loads(body['data']); self.calls.append(data)
            form = data['FormId']
            if form in self.sources:
                rows = self.sources[form]
                return rows[data['StartRow']:data['StartRow']+data['Limit']]
            return [[100, 'REQ-1', '张三', 15, 'B']]
        self.kd._post.side_effect = post
        bound = patch.object(h.actions, 'get_client', return_value=(self.kd, self.rl, ''))
        bound.start(); self.addCleanup(bound.stop)

    def test_native_completed_and_archive_sources_merge_deduplicate_and_keep_repeated_approvals(self):
        self.sources[h.SOURCES[1]] = [record(), record('second-round')]
        result = h.page('', '12', h.parse({}))
        self.assertEqual(result['pagination']['total'], 2)
        self.assertEqual({x['assignment_id'] for x in result['rows']}, {'a1', 'second-round'})
        self.assertEqual(result['rows'][0]['bill_status_cn'], '审核中')
        self.assertEqual(result['rows'][0]['result'], '审核通过')
        for call in self.calls[:2]:
            self.assertIn('FReceiverId=12', call['FilterString'])
            self.assertIn('FStatus=1', call['FilterString'])
        self.kd.save.assert_not_called()

    def test_cross_user_or_incomplete_history_never_leaks_bill(self):
        for row in (record(uid=99), record()[:-1] + ['0']):
            self.sources[h.SOURCES[0]] = [row]
            with self.assertRaisesRegex(RuntimeError, '归属'):
                h.page('', '12', {})
        self.assertTrue(all(x['FormId'] in h.SOURCES for x in self.calls))

    def test_paging_crosses_1000_without_truncation_and_headers_only_for_current_page(self):
        self.sources[h.SOURCES[0]] = [record('a'+str(n), number='REQ-'+str(n)) for n in range(1235)]
        cache = {}
        first = h.page('', '12', {}, cache=cache)
        last = h.page('', '12', {}, number=62, cache=cache)
        self.assertEqual(first['pagination']['total'], 1235)
        self.assertEqual(len(first['rows']), 20)
        self.assertEqual(len(last['rows']), 15)
        self.assertFalse(last['pagination']['has_next'])
        history_calls = [c for c in self.calls if c['FormId'] == h.SOURCES[0]]
        self.assertEqual([x['StartRow'] for x in history_calls], [0, 1000])
        bill_calls = [c for c in self.calls if c['FormId'] not in h.SOURCES]
        self.assertEqual(len(bill_calls), 2)
        self.assertEqual(bill_calls[0]['FilterString'].count("'REQ-"), 20)

    def test_filters_escape_literals_include_last_day_and_result_is_own_not_bill_status(self):
        self.sources[h.SOURCES[0]] = [record('ok'), record('reject', result='驳回重审'), record('other', result='转交')]
        f = h.parse({'q': "x'%_", 'date_from':'2026-11-01', 'date_to':'2026-11-18', 'result':'reject'})
        result = h.page('', '12', f, ['101'])
        self.assertEqual([x['assignment_id'] for x in result['rows']], ['reject'])
        where = self.calls[0]['FilterString']
        self.assertIn("FBILLNO like '%x''[%][_]%'", where)
        self.assertIn("FCOMPLETEDTIME<'2026-11-19'", where)
        self.assertIn("FOrgId.FNumber in ('101')", where)
        self.assertEqual(h.decision('转交'), 'other')
        self.assertEqual(h.decision('不同意'), 'reject')

    def test_applicant_filter_runs_before_paging(self):
        self.sources[h.SOURCES[0]] = [record('a'+str(n), number='REQ-'+str(n)) for n in range(30)]
        result = h.page('', '12', h.parse({'person':'张三'}))
        self.assertEqual(result['pagination']['total'], 1)
        self.assertEqual(result['rows'][0]['bill_no'], 'REQ-1')
        self.assertTrue(any("FStaffID.FName like '%张三%'" in c['FilterString'] for c in self.calls))

    def test_cache_isolated_by_actor_filters_org_and_refresh(self):
        cache = {}
        h.page('', '12', {}, cache=cache)
        self.sources[h.SOURCES[0]] = []
        self.assertEqual(h.page('', '12', {}, cache=cache)['pagination']['total'], 1)
        self.assertEqual(h.page('', '12', {}, cache={}, refresh=True)['pagination']['total'], 0)
        self.assertEqual(h.page('', '99', {}, cache=cache)['pagination']['total'], 0)
        self.assertEqual(h.page('', '12', {'q':'new'}, cache=cache)['pagination']['total'], 0)
        self.assertEqual(h.page('', '12', {}, ['102'], cache=cache)['pagination']['total'], 0)
        self.assertEqual(h.page('', '12', {}, cache=cache, refresh=True)['pagination']['total'], 0)

    def test_empty_scope_makes_no_queries_and_bad_parameters_are_rejected(self):
        self.assertEqual(h.page('', '12', {}, [])['pagination']['total'], 0)
        self.kd._post.assert_not_called()
        for args in ({'result':'bad'}, {'form':"x'"}, {'date_from':'2026-02-30'}):
            with self.assertRaises(ValueError): h.parse(args)
        with self.assertRaises(ValueError): h.page('', '12', {}, number=0)
        with self.assertRaises(ValueError): h.page('', '12', {}, size=200)

    def test_detail_checks_owner_and_bill_identity_and_is_read_only(self):
        with patch.object(h.actions, 'request_detail', return_value={'bill_no':'REQ-1'}) as detail:
            r = h.detail('', '12', 'a1')
            self.assertTrue(r['read_only'])
            self.assertEqual(r['task']['note'], '历史意见')
            detail.assert_called_once_with('', '100', False)
        self.sources[h.SOURCES[0]] = [record(uid=99)]
        with patch.object(h.actions, 'request_detail') as detail:
            with self.assertRaises(RuntimeError): h.detail('', '12', 'a1')
            detail.assert_not_called()
        self.sources[h.SOURCES[0]] = [record()]
        with patch.object(h.actions, 'request_detail', return_value={'bill_no':'other'}):
            with self.assertRaisesRegex(RuntimeError, '编号不一致'): h.detail('', '12', 'a1')

    def test_missing_bill_keeps_historical_decision_and_never_enables_writes(self):
        with patch.object(h, '_headers', return_value={}):
            result = h.detail('', '12', 'a1')
            self.assertIsNone(result['bill'])
            self.assertTrue(result['read_only'])
            self.assertEqual(result['task']['result'], '审核通过')

    def test_archive_failure_is_not_silently_reported_as_complete_history(self):
        self.kd._post.side_effect = [[record()], RuntimeError('archive unavailable')]
        with self.assertRaisesRegex(RuntimeError, 'archive unavailable'): h.page('', '12', {})


class HistoryRoutes(unittest.TestCase):
    def setUp(self): self.client = app.app.test_client()

    def test_missing_login_cannot_read_history(self):
        with patch.object(h, 'page') as page:
            self.assertFalse(self.client.get('/api/workflow/history').json['ok'])
            page.assert_not_called()

    def test_route_uses_trusted_reviewer_and_session_and_ignores_forged_actor(self):
        cache = {}
        with patch.object(app, '_workflow_actor', return_value=('test', '12')), \
             patch.object(app, '_require_session', return_value=cache), \
             patch.object(h, 'page', return_value={'rows':[]}) as page:
            r = self.client.get('/api/workflow/history?user_id=99&page=2&page_size=50&org=101')
            self.assertTrue(r.json['ok'])
            self.assertEqual(page.call_args.args[1], '12')
            self.assertEqual(page.call_args.args[3:], (['101'], 2, 50))
            self.assertIs(page.call_args.kwargs['cache'], cache)
        with patch.object(app, '_workflow_actor', return_value=('test', '12')), \
             patch.object(h, 'detail', return_value={'read_only':True}) as detail:
            self.assertTrue(self.client.get('/api/workflow/history/a1?user_id=99').json['ok'])
            self.assertEqual(detail.call_args.args[1:], ('12','a1'))

    def test_completed_assignment_cannot_be_resubmitted_to_pending_decide(self):
        kd = Mock(); kd.query.return_value = []
        with patch.object(w.actions, 'get_client', return_value=(kd, None, '')):
            with self.assertRaises(ValueError): w.decide('', '12', 'a1', 'approve')
        kd._post.assert_not_called()


if __name__ == '__main__': unittest.main()
