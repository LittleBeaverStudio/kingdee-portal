"""Exercise frame-ancestors in Chromium using isolated local test servers."""
import os, sys, tempfile, threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(PORTAL_LOCAL_MODE='1', PORTAL_NO_WARMUP='1', PORTAL_STATE_DIR=tempfile.mkdtemp(prefix='dsh-frame-'))
import app

class Parent(BaseHTTPRequestHandler):
    def do_GET(self):
        content = ("<!doctype html><iframe src='http://127.0.0.1:%d/'></iframe>" % portal.server_port).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(content)
    def log_message(self, *_args): pass

portal = make_server('127.0.0.1', 0, app.app, threaded=True)
allowed = ThreadingHTTPServer(('127.0.0.1', 0), Parent)
denied = ThreadingHTTPServer(('127.0.0.1', 0), Parent)
os.environ['PORTAL_DSH_WEB_ORIGIN'] = 'http://127.0.0.1:%d' % allowed.server_port
servers = [portal, allowed, denied]
threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
for t in threads: t.start()
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe', headless=True)
        page = browser.new_page()
        page.goto('http://127.0.0.1:%d/' % allowed.server_port)
        page.frame_locator('iframe').locator('#screen-login').wait_for(state='visible', timeout=10000)
        print('Allowed exact origin: PASS')
        page.goto('http://127.0.0.1:%d/' % denied.server_port)
        page.wait_for_timeout(1000)
        assert page.frame_locator('iframe').locator('#screen-login').count() == 0
        print('Different loopback port denied: PASS')
        browser.close()
finally:
    for s in servers: s.shutdown()
    for t in threads: t.join(timeout=5)
