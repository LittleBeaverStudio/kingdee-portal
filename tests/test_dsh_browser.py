"""Browser smoke test using a mocked Kingdee API; no real business data is touched."""
import os, sys, tempfile, threading
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(PORTAL_LOCAL_MODE='1', PORTAL_NO_WARMUP='1', PORTAL_STATE_DIR=tempfile.mkdtemp(prefix='dsh-browser-'))
import app
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright

class FakeClient:
    def __init__(self, base_url, acctid='', acct_name='', username='', password='', **kwargs):
        self.base_url, self.acctid, self.acct_name = base_url, acctid, acct_name
        self._username, self._password = username, password
    def resolve_acctid(self): return '12345'
    def login(self): pass

def discover(s, job, force=False):
    job.done({'orgs': [], 'org_trace': [], 'notes': []})

with patch.object(app, '_validate_base_url', lambda x: x), patch.object(app, 'KingdeeClient', FakeClient), patch.object(app, '_job_discover_orgs', discover):
    server = make_server('127.0.0.1', 0, app.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe', headless=True)
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto('http://127.0.0.1:%d/?c=legacy-company' % server.server_port, wait_until='networkidle')
            assert page.locator('#screen-login').is_visible()
            assert page.locator('#connection-settings').is_hidden(), 'Connection settings should be hidden initially'
            assert page.locator('#f-company').is_visible()
            assert page.locator('#f-local-server').is_visible()
            assert page.evaluate('window.__tenantCode') == ''
            page.locator('#f-company').fill('测试公司')
            page.locator('#f-local-server').fill('https://example.com')
            page.locator('#f-acct').evaluate("el => { el.value = 'previous-account'; }")
            page.locator('#f-local-server').fill('https://other.example.com')
            assert page.locator('#f-acct').input_value() == '', 'Changing server must clear selected account'
            page.locator('#f-user').fill('alice')
            page.locator('#f-pass').fill('example-password')
            page.locator('#btn-login').click()
            page.locator('#screen-app').wait_for(state='visible', timeout=10000)
            sid = page.evaluate("JSON.parse(sessionStorage.getItem('portal_boot_local')).sid")
            assert not page.evaluate("localStorage.getItem('portal_boot_local')")
            page.reload(wait_until='networkidle')
            page.locator('#screen-app').wait_for(state='visible', timeout=10000)
            assert page.evaluate("JSON.parse(sessionStorage.getItem('portal_boot_local')).sid") == sid
            page.locator('#btn-logout').click()
            page.locator('#screen-login').wait_for(state='visible', timeout=10000)
            assert page.locator('#f-company').input_value() == '测试公司'
            assert page.locator('#f-local-server').input_value() == 'https://other.example.com'
            assert page.locator('#f-pass').input_value() == ''
            assert not errors, errors
            print('Browser login, reload and logout: PASS')
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
