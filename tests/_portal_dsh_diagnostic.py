from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe', headless=True, args=['--no-sandbox'])
    page = browser.new_page()
    page.on('pageerror', lambda e: print('PAGE ERROR:', e))
    page.on('console', lambda m: print('CONSOLE:', m.type, m.text) if m.type == 'error' else None)
    page.on('requestfailed', lambda r: print('REQUEST FAILED:', r.url, r.failure))
    page.goto('http://127.0.0.1:8985/', wait_until='networkidle', timeout=20000)
    print('TITLE:', page.title())
    print('BODY:', page.locator('body').inner_text()[:500])
    print('VISIBLE ADMIN:', page.locator('#screen-admin-login').is_visible())
    browser.close()
