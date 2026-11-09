"""Synthetic browser checks for v1.5.7; no Kingdee connection or business writes."""
import argparse
import copy
import json
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from ui_dashboard import fixtures, ORGS
from test_v157 import statement_payload
import financial_reports as fr


def snapshots():
    data = fixtures()
    for view in ['expense', 'sales']:
        raw = data[view]
        raw['by_org'] = {}
        for i, org in enumerate(ORGS):
            part = copy.deepcopy({k: v for k, v in raw.items() if k != 'by_org'})
            factor = (i + 1) / 15
            def scale(obj):
                if isinstance(obj, dict): return {k: scale(v) for k, v in obj.items()}
                if isinstance(obj, list): return [scale(x) for x in obj]
                return obj * factor if isinstance(obj, (int, float)) else obj
            for key in (['metrics', 'monthly', 'exp_type_pie', 'aging'] if view == 'expense' else ['summary', 'periods', 'monthly_trend', 'customer_summary', 'salesperson_summary', 'material_summary', 'bill_type_summary']):
                part[key] = scale(part[key])
            for key in (['borrow_bills', 'apply_bills', 'unpaid_bills', 'org_rows'] if view == 'expense' else ['unsettled_bills']):
                part[key] = [dict(r) for r in part[key] if r.get('org', org['number']) == org['number']]
            raw['by_org'][org['number'] if view == 'expense' else org['name']] = part
    data['mgmt']['working_capital'] = [dict(org=o['number'], book=o['number'], year=2026, month=9, days=30,
                                            ar=150000, ap=50000, inventory=100000, revenue=300000, cost=150000) for o in ORGS]
    periods = fr.periods_before(2026, 10)
    raw = [fr.parse_statement(statement_payload(y, m, o['name']), o['number'], o['name'], y, m) for o in ORGS for y, m in fr.periods_before(2026, 10, count=13)]
    history, ytd = fr.assemble(raw, set(periods))
    data['mgmt'].update(working_capital_history=history, working_capital_ytd=ytd, capital_pending=False, capital_periods=[f'{y}-{m:02}' for y,m in periods], capital_errors=[])
    data['cash']['kpi_by_org'] = {o['number']: {k: v / 5 for k, v in data['cash']['kpi'].items()} for o in ORGS}
    return data


def run(url):
    out = Path('artifacts/ui-v1.5.7'); out.mkdir(parents=True, exist_ok=True)
    data = snapshots()
    errors, calls = [], []
    cfg_path = Path('portal_config.json')
    if not cfg_path.exists():
        cfg_path = Path('portal_config.example.json')
    version = json.loads(cfg_path.read_text(encoding='utf-8'))['version']
    config = dict(tenant='test', tenant_name='示例公司甲测试', can_edit=True, revision='r1', orgs=[dict(no='101', name='组织一'), dict(no='999', name='已保存组织')],
                  companies=[dict(id='one', ghf_mc='示例公司甲', client_id='cid-one', kingdee_orgs='101', has_secret=True),
                             dict(id='two', ghf_mc='示例公司乙', client_id='cid-two', kingdee_orgs='', has_secret=True)], cred_mode='memory')
    jobs = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport=dict(width=1600, height=1080), accept_downloads=True)
        page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e)))
        def document(route):
            r = route.fetch(); route.fulfill(response=r, body=r.text().replace('data-logged-in="0"', 'data-logged-in="1"'))
        def api(route):
            path = route.request.url.split('/api/', 1)[1].split('?')[0]
            body = route.request.post_data_json if route.request.method == 'POST' else {}
            calls.append(dict(path=path, body=body))
            if path == 'me': result = dict(ok=True, login=dict(username='界面测试'), orgs=ORGS, org_trace=[], notes=[])
            elif path.startswith('dashboard/'):
                key = path.rsplit('/', 1)[1].replace('mgmt-full', 'mgmt')
                d = copy.deepcopy(data[key]); phase = (body or {}).get('phase')
                if key == 'mgmt':
                    deferred = ['interdim', 'dash', 'recv', 'anom', 'invoice']
                    d['pages'] = {k: v for k, v in d['pages'].items() if (k in deferred) == (phase == 'detail')}
                    d['cards'] = [c for c in d['cards'] if c['id'] in d['pages']]
                    d['deferred_sources'] = deferred if phase == 'overview' else []
                    if phase == 'overview':
                        for field in ['working_capital_history', 'working_capital_ytd', 'capital_periods']: d.pop(field, None)
                        d['capital_pending'] = True
                    if phase == 'capital':
                        d = {k:v for k,v in d.items() if k.startswith('capital_') or k.startswith('working_capital_')}
                        d.update(pages={}, cards=[], orgs=[o['number'] for o in ORGS])
                job = 'test-' + str(len(jobs)); jobs[job] = d
                result = dict(ok=True, job=dict(id=job, status='running', pct=10))
            elif path.startswith('job/'):
                result = dict(ok=True, job=dict(status='done', result=jobs[path.rsplit('/', 1)[1]]))
            elif path == 'reimburse/status': result = dict(ok=True, enabled=True)
            elif path == 'reimburse/piazzone-config':
                if route.request.method == 'POST':
                    assert body['revision'] == config['revision']; config['revision'] = 'r2'
                result = dict(ok=True, config=config)
            elif path == 'admin/tenants': result = dict(ok=True, tenants=[])
            elif path == 'admin/need-setup': result = dict(ok=True, need_setup=False)
            elif path == 'admin/stats': result = dict(ok=True, stats=dict(total={}, today={}), tenants=[])
            elif path == 'admin/credential-mode': result = dict(ok=True, credential_mode='memory')
            else: result = dict(ok=True)
            route.fulfill(json=result)
        page.route(re.compile(re.escape(url) + r'/\?c=test$'), document)
        page.route('**/api/**', api)
        page.goto(url + '/?c=test'); page.wait_for_load_state('networkidle')
        expect(page.locator('#v-expense .analytics')).to_be_visible()
        expect(page.locator('.brand-copy small')).to_have_text('v' + version)
        # Navigate while sequential requests are pending: no second request for the same view.
        page.locator('#nav [data-v="cash"]').click()
        expect(page.locator('#v-cash .analytics')).to_be_visible(timeout=20000)
        expect(page.locator("#v-mgmt .a-source:not([disabled])")).to_have_count(12, timeout=20000)
        req = [c for c in calls if c['path'].startswith('dashboard/')]
        assert [c['path'] for c in req] == ['dashboard/expense', 'dashboard/mgmt-full', 'dashboard/sales', 'dashboard/cash', 'dashboard/mgmt-full', 'dashboard/mgmt-full'], req
        assert [r['body']['phase'] for r in req if r['path'] == 'dashboard/mgmt-full'] == ['overview', 'capital', 'detail']

        def view(name):
            page.locator('#nav [data-v="' + name + '"]').click()
            root = page.locator('#v-' + name)
            if name in data: expect(root.locator('.analytics')).to_be_visible()
            return root
        def choose(no=None):
            page.locator('#btn-orgs').click()
            page.evaluate('''no => {
              let all=document.getElementById('chip-all');
              if(all.classList.contains('on')) all.click();
              else { all.click(); all=document.getElementById('chip-all'); all.click(); }
              if(no) document.querySelector('#org-chips [data-no="'+no+'"]').click();
            }''', no)
            page.locator('#btn-orgs-close').click()
        def all_orgs():
            page.locator('#btn-orgs').click()
            page.evaluate('''() => {let all=document.getElementById('chip-all'); if(!all.classList.contains('on'))all.click();}''')
            page.locator('#btn-orgs-close').click()

        count = len(calls)
        root = view('expense'); choose('101')
        expect(root.locator('.a-metric strong').first).to_have_text('¥10,425.15')
        assert not page.locator('.analytics select[data-a="org"],.analytics select[data-a="range"]').count()
        assert not root.locator('.data-status,.cache-bar,.a-heading').count()
        all_orgs()
        chart = root.locator('[data-zoom-chart]').first
        before = chart.locator('[data-date]').count()
        chart.hover(); page.mouse.wheel(0, -300)
        expect(root.locator('[data-zoom-chart] [data-date]')).to_have_count(before - 2)
        chart = root.locator('[data-zoom-chart]').first; chart.focus(); chart.press('-'); root.locator('[data-zoom-chart]').first.press('-')
        expect(root.locator('[data-zoom-chart] [data-date]')).to_have_count(before)
        root.locator('[data-a="legend"]').first.click(); expect(root.locator('.a-legend-item.off')).to_have_count(1)
        page.locator('#screen-app [data-theme-toggle]').click()
        assert len(calls) == count, 'Local filtering, zoom, theme and navigation must make no API calls'
        root = view('mgmt')
        expect(root.locator('.a-capital-item strong').nth(3)).to_have_text('25.0天')
        expect(root.locator('.a-capital-reference b').first).to_have_text('12.6天')
        expect(root.locator('.a-capital-period')).to_contain_text('2026-09')
        capital_chart = root.locator('[data-zoom-chart="capital-trend"]')
        expect(capital_chart.locator('[data-date]')).to_have_count(12)
        assert '¥' not in capital_chart.locator('[data-date]').last.get_attribute('data-tip')
        capital_chart.hover(); page.mouse.wheel(0, -300)
        expect(root.locator('[data-zoom-chart="capital-trend"] [data-date]')).to_have_count(10)
        expect(root.locator('.a-capital-item strong').nth(3)).to_have_text('25.0天')
        root.locator('[data-a="cell"][data-value="101:est"]').click()
        expect(root.locator('.a-metric strong').nth(1)).to_have_text('¥50,000.00')
        expect(root.locator('.a-detail .a-table tbody tr')).to_have_count(1)
        for source in data['mgmt']['pages']:
            root.locator('.a-source[data-value="' + source + '"]').click()
            expect(root.locator('[data-sub-back]')).to_be_visible()
            root.locator('[data-sub-back]').click()
        root.locator('[data-a="cell"][data-value="101:arhx"]').click()
        expect(root.locator('select[data-a="source"]')).to_have_value('arhx')
        root.locator('[data-a="search"]').fill('远景')
        with page.expect_download() as down: root.locator('[data-a="export"]').click()
        down.value.save_as(str(out / 'filtered.csv'))
        all_orgs()
        root = view('sales'); choose('102')
        expect(root.locator('.a-metric strong').first).to_have_text('¥68,266.67')
        root = view('cash'); choose('104')
        expect(root.locator('.a-metric strong').nth(1)).to_have_text('¥-114,000.00')
        choose(); expect(root).to_contain_text('请在右上角选择组织'); all_orgs()
        before = len([c for c in calls if c['path'].startswith('dashboard/')])
        page.locator('#btn-refresh').click()
        expect(page.locator('#btn-refresh')).to_have_attribute('aria-busy', 'false', timeout=10000)
        req = [c for c in calls if c['path'].startswith('dashboard/')]
        assert len(req) == before + 1 and req[-1]['body']['force'] is True
        assert len(req[-1]['body']['orgs']) == len(ORGS)
        assert not page.evaluate('Object.keys(localStorage).some(k=>k.startsWith("portal_cache:"))')

        root = view('pzcfg')
        expect(root.locator('.pz-co-title')).to_contain_text('示例公司乙')
        assert '公司：' not in root.inner_text()
        root.locator('#pz-save-orgs').click()
        expect(root).to_contain_text('已保存')
        assert [c for c in calls if c['path'] == 'reimburse/piazzone-config'][-1]['body']['revision'] == 'r1'

        for width in [1920, 390, 768]:
            page.set_viewport_size(dict(width=width, height=1000 if width > 1000 else 844))
            for name in ['expense', 'mgmt', 'sales', 'cash', 'guide']:
                root = view(name)
                assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1'), (name, width)
                assert 'NaN' not in root.inner_text() and 'Infinity' not in root.inner_text()
                page.screenshot(path=str(out / f'{name}-{width}.png'), full_page=True, animations='disabled')
            if width == 1920: assert page.locator('.guide-layout').bounding_box()['width'] > 1500
        # Statement coverage, latest valid month and annual reference stay local.
        sparse = copy.deepcopy(data['mgmt'])
        for row in sparse['working_capital_history']:
            if row['org'] == '101': row['inventory'] = None
        html = page.evaluate("([d,o])=>PortalAnalytics.render('mgmt',d,{orgs:o,selected:['101']})", [sparse, ORGS])
        assert '暂不判断资金周期' in html and 'NaN' not in html
        html = page.evaluate("([d,o])=>PortalAnalytics.render('mgmt',d,{orgs:o,selected:o.map(x=>x.number)})", [sparse, ORGS])
        assert '共同覆盖 4 / 5' in html and '25.0<small>天' in html
        sparse = copy.deepcopy(data['mgmt'])
        for row in sparse['working_capital_history']:
            if row['period'] == '2026-09': row['revenue'] = row['cost'] = 0
        html = page.evaluate("([d,o])=>PortalAnalytics.render('mgmt',d,{orgs:o,selected:o.map(x=>x.number)})", [sparse, ORGS])
        assert '2026-08 · 最近可用完整月' in html
        sparse['working_capital_history'] += [sparse['working_capital_history'][-1]]
        html = page.evaluate("([d,o])=>PortalAnalytics.render('mgmt',d,{orgs:o,selected:['105']})", [sparse, ORGS])
        assert 'NaN' not in html and 'Infinity' not in html
        weighted = copy.deepcopy(data['mgmt'])
        for row in weighted['working_capital_history']:
            if row['org'] == '101': row['revenue'] = 100000
            if row['org'] == '102': row['revenue'] = 500000
        html = page.evaluate("([d,o])=>PortalAnalytics.render('mgmt',d,{orgs:o,selected:['101','102']})", [weighted, ORGS])
        assert '15.0<small>天' in html and '共同覆盖 2 / 2' in html
        # Admin UI checks with synthetic auth, without changing the server's real admin password.
        admin = context.new_page(); admin.on('pageerror', lambda e: errors.append(str(e)))
        admin.route('**/api/**', api); admin.goto(url + '/'); admin.wait_for_load_state('networkidle')
        assert admin.locator('label[for="f-admin-pw"]').count() == 0
        assert admin.locator('.input-wrap > svg').count() == 0
        admin.locator('#f-admin-pw').fill('synthetic-test'); admin.locator('#btn-admin-login').click()
        expect(admin.locator('.adm-wrap')).to_be_visible()
        admin.set_viewport_size(dict(width=1920, height=1080))
        for tab in ['companies', 'stats', 'security']:
            admin.locator('#nav [data-adm="' + tab + '"]').click()
            expect(admin.locator('.adm-wrap')).to_be_visible()
            assert admin.locator('.adm-wrap').bounding_box()['width'] > 1500
            admin.screenshot(path=str(out / f'admin-{tab}.png'), full_page=True)
        assert not errors, errors
        (out / 'result.json').write_text(json.dumps(dict(status='passed', page_errors=errors, api_calls=len(calls), data='synthetic; no live Kingdee account queried'), ensure_ascii=False, indent=2), encoding='utf-8')
        browser.close()
    print('PASS: sequential loading; local organization, zoom, theme and drill; refresh; CCC; preserved invoice config; full-width guide/admin; responsive layouts')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--url', default='http://127.0.0.1:8875')
    run(parser.parse_args().url)
