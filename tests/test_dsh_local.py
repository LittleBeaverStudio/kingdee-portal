import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['PORTAL_LOCAL_MODE'] = '1'
os.environ['PORTAL_NO_WARMUP'] = '1'
os.environ['PORTAL_STATE_DIR'] = tempfile.mkdtemp(prefix='portal-local-test-')
import app

class FakeClient:
    def __init__(self, base_url, acctid='', acct_name='', username='', password='', **kwargs):
        self.base_url, self.acctid, self.acct_name = base_url, acctid, acct_name
        self._username, self._password = username, password
    def resolve_acctid(self): return '12345'
    def login(self): pass

class LocalModeTests(unittest.TestCase):
    def setUp(self):
        app._sessions.clear()
        app._login_hits.clear()
        self.client = app.app.test_client(use_cookies=False)
    def bootstrap(self, sid=None):
        return self.client.get('/api/bootstrap', headers={'X-Portal-Sid': sid} if sid else {}).get_json()
    def test_local_frame_policy_uses_only_trusted_exact_web_origin(self):
        with patch.dict(app.CFG, {'allow_local_embedding': True}), \
             patch.dict(os.environ, {'PORTAL_DSH_WEB_ORIGIN': 'http://127.0.0.1:45678'}):
            csp = self.client.get('/').headers['Content-Security-Policy']
        self.assertIn('dsh-app:', csp)
        self.assertIn('http://127.0.0.1:45678', csp)
        self.assertNotIn('127.0.0.1:*', csp)
        with patch.dict(app.CFG, {'allow_local_embedding': True}), \
             patch.dict(os.environ, {'PORTAL_DSH_WEB_ORIGIN': 'http://127.0.0.1:45678; frame-src *'}):
            csp = self.client.get('/').headers['Content-Security-Policy']
        self.assertNotIn('45678', csp)
        self.assertNotIn('frame-src *', csp)

    def test_public_mode_never_inherits_local_embedding_exception(self):
        with patch.object(app, 'LOCAL_MODE', False), patch.dict(app.CFG, {'allow_local_embedding': True}):
            response = self.client.get('/')
        self.assertEqual(response.headers.get('X-Frame-Options'), 'DENY')
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])

    def test_local_page_navigation_does_not_create_anonymous_sessions(self):
        for _ in range(3):
            self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(len(app._sessions), 0)
        self.assertTrue(self.bootstrap()['ok'])
        self.assertEqual(len(app._sessions), 1)

    def test_admin_blocked_and_login_requires_company(self):
        self.assertEqual(self.client.get('/api/admin/need-setup').status_code, 404)
        b = self.bootstrap()
        h = {'X-Portal-Sid': b['sid'], 'X-CSRF-Token': b['csrf']}
        r = self.client.post('/api/login', headers=h, json={'server_url':'https://example.com', 'username':'a', 'password':'b'}).get_json()
        self.assertFalse(r['ok'])
        self.assertIn('公司名称', r['error']['message'])
    @patch.object(app, '_job_discover_orgs', lambda s, j: None)
    @patch.object(app, '_validate_base_url', lambda x: x)
    @patch.object(app, 'KingdeeClient', FakeClient)
    @patch.object(app.store, 'save_session')
    def test_login_rotates_sid_and_logout_revokes(self, save):
        b = self.bootstrap()
        h = {'X-Portal-Sid': b['sid'], 'X-CSRF-Token': b['csrf']}
        r = self.client.post('/api/login', headers=h, json={'company':'测试公司', 'server_url':'https://example.com', 'username':'alice', 'password':'secret'}).get_json()
        self.assertTrue(r['ok'], r)
        self.assertNotEqual(r['sid'], b['sid'])
        save.assert_not_called()
        h['X-Portal-Sid'] = r['sid']
        self.assertTrue(self.client.get('/api/me', headers=h).get_json()['ok'])
        status = self.client.get('/api/reimburse/status', headers=h).get_json()
        self.assertEqual(status['cred_mode'], 'memory')
        self.assertFalse(self.client.get('/api/reimburse/status').get_json()['ok'])
        self.assertTrue(self.client.get('/api/job/' + r['org_job'], headers=h).get_json()['ok'])
        self.assertFalse(self.client.get('/api/job/' + r['org_job']).get_json()['ok'])
        other = self.bootstrap()
        self.assertFalse(self.client.get('/api/job/' + r['org_job'], headers={'X-Portal-Sid':other['sid']}).get_json()['ok'])
        self.assertEqual(self.bootstrap(r['sid'])['sid'], r['sid'])
        job = app._jobs[r['org_job']]
        self.assertTrue(self.client.post('/api/logout', headers=h, json={}).get_json()['ok'])
        self.assertNotIn(r['org_job'], app._jobs)
        job.done({'secret': 'late result'})
        self.assertNotIn(r['org_job'], app._jobs)
        self.assertFalse(self.client.get('/api/me', headers=h).get_json()['ok'])
    @patch.object(app, '_job_discover_orgs', lambda s, j: None)
    @patch.object(app, '_validate_base_url', lambda x: x)
    @patch.object(app, 'KingdeeClient', FakeClient)
    def test_local_login_ignores_legacy_company_code(self):
        b = self.bootstrap()
        headers = {'X-Portal-Sid': b['sid'], 'X-CSRF-Token': b['csrf']}
        with patch.object(app.store, 'get_tenant', wraps=app.store.get_tenant) as lookup:
            result = self.client.post('/api/login', headers=headers, json={
                'code': 'old-unapproved-code', 'company': '测试公司',
                'server_url': 'https://example.com', 'username': 'alice', 'password': 'secret'}).get_json()
        self.assertTrue(result['ok'], result)
        self.assertFalse(any(call.args[0] == 'old-unapproved-code' for call in lookup.call_args_list))

    def test_job_rejects_anonymous_and_other_user_in_all_modes(self):
        owner = {'client': object()}
        stranger = {'client': object()}
        job = app.Job('private', owner)
        job.done({'financial_data': 'sensitive'})
        for mode in (True, False):
            with self.subTest(local_mode=mode), patch.object(app, 'LOCAL_MODE', mode):
                with patch.object(app, '_require_session', side_effect=app.KdError('expired', '会话')):
                    self.assertFalse(self.client.get('/api/job/' + job.id).get_json()['ok'])
                with patch.object(app, '_require_session', return_value=stranger):
                    self.assertFalse(self.client.get('/api/job/' + job.id).get_json()['ok'])
                with patch.object(app, '_require_session', return_value=owner):
                    self.assertTrue(self.client.get('/api/job/' + job.id).get_json()['ok'])

    def test_expired_session_cannot_pass_csrf(self):
        b = self.bootstrap()
        with app._lock:
            app._sessions[b['sid']]['seen'] -= app.CFG['session_idle_minutes'] * 60 + 1
        response = self.client.post('/api/datacenters', headers={
            'X-Portal-Sid': b['sid'], 'X-CSRF-Token': b['csrf']}, json={}).get_json()
        self.assertFalse(response['ok'])
        self.assertEqual(response['error']['stage'], '安全')

    @patch.object(app, '_job_discover_orgs', lambda s, j: None)
    @patch.object(app, '_validate_base_url', lambda x: x)
    @patch.object(app, 'KingdeeClient', FakeClient)
    def test_local_invoice_configuration_does_not_claim_admin(self):
        b = self.bootstrap()
        h = {'X-Portal-Sid': b['sid'], 'X-CSRF-Token': b['csrf']}
        login = self.client.post('/api/login', headers=h, json={
            'company': '测试公司', 'server_url': 'https://example.com',
            'username': 'alice', 'password': 'secret'}).get_json()
        self.assertTrue(login['ok'], login)
        h['X-Portal-Sid'] = login['sid']
        with patch.object(app.portal_actions, 'find_skill_dir', return_value='helpers'), \
             patch.object(app, '_apply_tenant_piazzone'), patch.object(app, '_audit'):
            config = self.client.get('/api/reimburse/piazzone-config', headers=h).get_json()
            self.assertTrue(config['ok'], config)
            result = self.client.post('/api/reimburse/piazzone-config', headers=h, json={
                'action': 'add', 'revision': config['config']['revision'],
                'client_id': 'synthetic-id'}).get_json()
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['claimed'])
        self.assertEqual(app.store.get_tenant(result['config']['tenant'])['admin_users'], [])

    def test_local_admin_transfer_and_credential_switch_are_disabled(self):
        b = self.bootstrap()
        headers = {'X-Portal-Sid': b['sid'], 'X-CSRF-Token': b['csrf']}
        for path in ('/api/reimburse/piazzone-transfer', '/api/reimburse/piazzone-mode'):
            self.assertEqual(self.client.post(path, headers=headers, json={}).status_code, 404)
        self.assertEqual(self.client.get('/api/net/probe?url=https://evil.example/?piaozone.com').status_code, 404)

    def test_local_configuration_not_restricted_by_legacy_admin(self):
        payload = app._pz_payload({'code': 'local-test', 'admin_users': ['old-admin']}, 'alice', [])
        self.assertTrue(payload['can_edit'])
        self.assertFalse(payload['claimable'])
        self.assertEqual(payload['cred_mode'], 'memory')
        self.assertIn('本机', payload['storage'])

    def test_invalid_csrf_rejected(self):
        b = self.bootstrap()
        r = self.client.post('/api/logout', headers={'X-Portal-Sid':b['sid'], 'X-CSRF-Token':'bad'}, json={}).get_json()
        self.assertFalse(r['ok'])
    @patch.object(app, '_job_discover_orgs', lambda s, j: None)
    @patch.object(app, '_validate_base_url', lambda x: x)
    @patch.object(app, 'KingdeeClient', FakeClient)
    def test_write_uses_logged_in_actor_and_clears_thread_context(self):
        b = self.bootstrap()
        headers = {'X-Portal-Sid': b['sid'], 'X-CSRF-Token': b['csrf']}
        result = self.client.post('/api/login', headers=headers, json={
            'company': '测试公司', 'server_url': 'https://example.com',
            'username': 'alice', 'password': 'secret'}).get_json()
        self.assertTrue(result['ok'], result)
        headers['X-Portal-Sid'] = result['sid']
        seen = []
        def capture(_skill, bill, travel):
            seen.append(dict(app.portal_actions.current_credentials()))
            self.assertEqual(bill, 'TEST-1')
            return {'fid': 'mock-only'}
        with patch.object(app.portal_actions, 'find_skill_dir', return_value='helpers'), \
             patch.object(app.portal_actions, 'push', side_effect=capture), \
             patch.object(app, '_audit'):
            response = self.client.post('/api/reimburse/push', headers=headers,
                                        json={'bill_no': 'TEST-1'}).get_json()
        self.assertTrue(response['ok'], response)
        self.assertEqual(seen[0]['username'], 'alice')
        self.assertEqual(seen[0]['password'], 'secret')
        self.assertIsNone(app.portal_actions.current_credentials())

    @patch.object(app, '_job_discover_orgs', lambda s, j: None)
    @patch.object(app, '_validate_base_url', lambda x: x)
    @patch.object(app, 'KingdeeClient', FakeClient)
    def test_kingdee_permission_denial_is_not_retried_as_another_user(self):
        b = self.bootstrap()
        headers = {'X-Portal-Sid': b['sid'], 'X-CSRF-Token': b['csrf']}
        result = self.client.post('/api/login', headers=headers, json={
            'company': '测试公司', 'server_url': 'https://example.com',
            'username': 'limited', 'password': 'secret'}).get_json()
        self.assertTrue(result['ok'], result)
        headers['X-Portal-Sid'] = result['sid']
        seen = []
        def deny(_skill, _bill, _travel):
            seen.append(app.portal_actions.current_credentials()['username'])
            raise app.KdError('金蝶拒绝：没有权限', '权限')
        with patch.object(app.portal_actions, 'find_skill_dir', return_value='helpers'), \
             patch.object(app.portal_actions, 'push', side_effect=deny) as call, \
             patch.object(app, '_audit'):
            response = self.client.post('/api/reimburse/push', headers=headers,
                                        json={'bill_no': 'TEST-1'}).get_json()
        self.assertFalse(response['ok'])
        self.assertIn('没有权限', response['error']['message'])
        self.assertEqual(seen, ['limited'])
        self.assertEqual(call.call_count, 1)

    def test_invoice_config_does_not_fall_back_to_previous_company(self):
        actions = app.portal_actions
        original = dict(actions._PZ)
        try:
            with patch.object(actions, 'apply_company_credentials') as apply:
                actions.set_piazzone_companies('', [{'client_id': 'company-a', 'kingdee_orgs': '101'}])
                actions.set_piazzone_companies('', [])
                self.assertEqual(actions._PZ['companies'], [])
                apply.assert_called_with({})
            actions._PZ['companies'] = [
                {'client_id': 'company-a', 'kingdee_orgs': '101'},
                {'client_id': 'company-b', 'kingdee_orgs': '102'},
            ]
            self.assertIsNone(actions.pick_company_for_org('10'))
            self.assertEqual(actions.pick_company_for_org('102')['client_id'], 'company-b')
        finally:
            actions._PZ.clear()
            actions._PZ.update(original)

    def test_invoice_credential_fingerprint_includes_endpoint(self):
        actions = app.portal_actions
        original_pz = dict(actions._PZ)
        original_sig = actions._applied['sig']
        old_env = dict(os.environ)
        fake_module = object()
        creds = {'client_id': 'id', 'client_secret': 'sensitive-value'}
        try:
            actions._applied['sig'] = ''
            with patch.dict(sys.modules, {'piazzone_client': fake_module}), \
                 patch.object(actions.importlib, 'reload') as reload_module:
                actions._PZ['base_url'] = 'https://first.example'
                actions.apply_company_credentials(creds)
                first = actions._applied['sig']
                self.assertNotIn('sensitive-value', first)
                actions.apply_company_credentials(creds)
                self.assertEqual(reload_module.call_count, 1)
                actions._PZ['base_url'] = 'https://second.example'
                actions.apply_company_credentials(creds)
                self.assertEqual(reload_module.call_count, 2)
                self.assertNotEqual(actions._applied['sig'], first)
        finally:
            actions._PZ.clear()
            actions._PZ.update(original_pz)
            actions._applied['sig'] = original_sig
            os.environ.clear()
            os.environ.update(old_env)

    def test_write_helper_never_falls_back_to_configured_account(self):
        app.portal_actions.use_credentials(None)
        with patch.object(app.portal_actions, '_load', return_value=(object(), None, 'helpers')):
            with self.assertRaisesRegex(RuntimeError, '拒绝使用其他账号'):
                app.portal_actions.get_client()

    def test_write_gate_does_not_check_local_switch(self):
        with patch.dict(app.CFG, {'enable_write_ops': False}), patch.object(app.portal_actions, 'find_skill_dir', return_value='helpers'):
            self.assertEqual(app._write_gate(), 'helpers')

if __name__ == '__main__': unittest.main()
