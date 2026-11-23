"""Any-form approval UI: a payment application renders through the generic detail view."""
import argparse
import re
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from ui_dashboard import ORGS


def run(url):
    task = dict(assignment_id='pay1', title='付款申请', bill_no='FKSQD00800', form_id='CN_PAYAPPLY', fid='789',
                kind='付款申请单', node='财务经理', sender='申请人甲', content='', org_no='100',
                org_name='测试组织', created='2026-11-23 09:00:00', can_open=True, supported=False)
    blocked = dict(task, assignment_id='noopen', bill_no='CUS-001', form_id='BD_Customer', fid='',
                   kind='客户', can_open=False)
    bill = dict(bill_no='FKSQD00800', form_id='CN_PAYAPPLY', kind='付款申请单', status='B', status_cn='审核中',
                date='2026-11-23', applicant='张三', org_no='001', org_name='示例科技', amount=276000,
                remark='按月结付款，发票已核对',
                fields=[],
                entries=dict(columns=[{'t': '序号'}, {'t': '费用项目'}, {'t': '费用承担部门'},
                                      {'t': '项目'}, {'t': '付款金额'}],
                             rows=[['1', '差旅费', '财务部', '项目A', '276000']]),
                generic=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(viewport={'width': 1440, 'height': 1000}).new_page()
        errors, writes = [], []

        def document(route):
            r = route.fetch()
            route.fulfill(response=r, body=r.text().replace('data-logged-in="0"', 'data-logged-in="1"'))

        def api(route):
            path = urlsplit(route.request.url).path.removeprefix('/api/')
            if path == 'me':
                result = dict(ok=True, login=dict(username='审核人乙'), orgs=ORGS, org_trace=[], notes=[])
            elif path == 'workflow/tasks':
                result = dict(ok=True, rows=[task, blocked])
            elif path == 'workflow/task/pay1':
                result = dict(ok=True, task=task, bill=bill, is_application=False, render='generic')
            elif path == 'workflow/decide':
                writes.append(route.request.post_data_json)
                result = dict(ok=True, result=dict(task_status='1'))
            else:
                result = dict(ok=True)
            route.fulfill(json=result)

        page.route(re.compile(re.escape(url) + r'/\?c=test$'), document)
        page.route('**/api/**', api)
        page.goto(url + '/?c=test', wait_until='domcontentloaded')
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.locator('#nav [data-v="workflow"]').click()
        expect(page.locator('#v-workflow')).to_contain_text('付款申请单')
        expect(page.locator('#v-workflow')).to_contain_text('无单据内码')
        page.locator('[data-wf-open]').first.click()
        expect(page.locator('.modal')).to_contain_text('费用项目')
        expect(page.locator('.modal')).to_contain_text('差旅费')
        expect(page.locator('.modal')).to_contain_text('备注')
        expect(page.locator('.modal')).to_contain_text('276000')
        expect(page.locator('.modal')).to_contain_text('张三')
        expect(page.locator('#wf-approve')).to_be_visible()
        from pathlib import Path
        out = Path('artifacts/ui-workflow'); out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / 'any-form.png'), full_page=True)
        page.locator('#wf-approve').click()
        expect(page.locator('#wf-submit')).to_be_visible()
        page.locator('#wf-submit').click()
        expect(page.locator('.modal')).to_contain_text('审批完成')
        assert writes and writes[0]['decision'] == 'approve', writes
        assert not errors, errors
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Horizontal overflow'
        browser.close()
    print('Any-form approval UI passed: generic detail, entries and decision')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8879')
    run(parser.parse_args().url.rstrip('/'))
