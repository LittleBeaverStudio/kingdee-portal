"""Browser regression with synthetic API fixtures; never connects to a Kingdee tenant.

Run against an isolated local Flask instance:
    python tests/ui_dashboard.py --url http://127.0.0.1:8875
Artifacts contain test fixtures, not company data.
"""
import argparse
import copy
import json
import re
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ORGS = [{"number": str(101+i), "name": name} for i, name in enumerate([
    "华北运营中心", "华南销售中心", "华东制造中心", "西南业务中心", "总部共享中心"])]
MONTHS = [f"2026-{i:02d}" for i in range(1, 11)]
AMOUNTS = [284000, 312000, 268000, 348000, 386000, 361000, 429000, 408000, 476000, 512000]


def fixtures():
    bills = [{"billno": f"TEST-{i:03d}", "bill_no": f"TEST-{i:03d}", "org": ORGS[i % 5]["number"],
              "org_name": ORGS[i % 5]["name"], "staff": f"测试人员{i % 6+1}", "date": f"2026-{i % 10+1:02d}-10",
              "amount": 3000+i*450, "balance": 3000+i*450, "diff": 3000+i*450,
              "total": 12000+i*450, "unpaid": 3000+i*450, "amt": 12000+i*450,
              "settled": 9000, "applied": 12000+i*450, "pushed": 9000} for i in range(28)]
    expense = {"generated_at": "2026-10-20 10:30:00", "scope": {}, "coverage": {},
               "metrics": {"borrow_unpaid": 156377.29, "apply_unreimb": 185295.98, "unpaid": 165267.55,
                           "n_borrow": 28, "n_apply": 312, "n_reimb": 286, "apply_amt": 900000,
                           "tapply_amt": 100000, "reimb_amt": 720000, "treimb_amt": 80000},
               "monthly": {m: {"apply": a, "reimb": a*.78} for m, a in zip(MONTHS, AMOUNTS)},
               "exp_type_pie": [["差旅交通", 82000], ["业务招待", 54000], ["办公行政", 38000], ["技术服务", 24000], ["其他费用", 16000]],
               "borrow_bills": bills, "apply_bills": bills[:12], "unpaid_bills": bills[:16],
               "org_rows": [{"org": o["number"], "org_name": o["name"], "borrow_unpaid": (6-i)*12000,
                             "apply_unreimb": (4+i)*9000, "unpaid": (5-i)*15000,
                             "n_apply": 50+i, "n_reimb": 40+i} for i, o in enumerate(ORGS)],
               "aging": {k: [{"label": lab, "amount": (6-i)*7000*(j+1), "n": 20-i*3} for i, lab in enumerate(["≤30 天", "31–90 天", "91–180 天", "181–365 天", ">365 天"])] for j, k in enumerate(["borrow", "unpaid"])},
               "staff_rows": [], "status_rows": []}
    recon = [{"org": o["number"], "party": ["远景科技", "启明医疗", "新源供应链", "华盛商贸", "海川实业"][i],
              "bill": 220000+i*50000, "pay": 180000+i*56000, "diff": 40000-i*6000} for i, o in enumerate(ORGS)]
    swings = [{"org_number": o["number"], "org_name": o["name"], "code": str(112200+i), "name": "应收账款",
               "type": ["资产", "负债", "收入", "费用", "权益"][i], "delta": [186000, -74000, 132000, -43000, 21000][i],
               "cur": 500000+i*10000, "prev": 450000} for i, o in enumerate(ORGS)]
    pages = {
        "bank": {"fund": {"account_count": 18, "org_count": 5}, "cross_check": {"both": [{"org_number": o["number"], "org_name": o["name"], "fund_end": 400000+i*75000, "subject": 398000+i*75000, "delta": [4372.69, 0, -6200, 1680, 0][i]} for i, o in enumerate(ORGS)]}},
        "subject": {"swings": swings},
        "est": {"org_summary": [{"org": o["number"], "suppliers": 8+i, "notax": 260000+i*10000, "est": 210000+i*12000, "diff": 50000-i*2000} for i, o in enumerate(ORGS)],
                "detail": [{"org": o["number"], "supplier": r["party"], "notax": 260000, "est_cum": 210000, "diff_cum": 50000-i*2000, "diff_cur": 10000} for i, (o, r) in enumerate(zip(ORGS, recon))]},
        "arhx": {"recon": recon, "posneg": []},
        "aphx": {"recon": [dict(r, diff=-r["diff"]) for r in recon], "posneg": []},
        "interdim": {"orgs": [{"org": o["number"], "summary": [{"cat": "1122", "name": "应收账款", "net": 36000}], "rows": [{"dim": "远景科技", "cats": {"1122": 56000, "2203": -20000}, "net": 36000}]} for o in ORGS]},
        "goods": {"detail": [{"org": o["number"], "supplier": "启明医疗", "bill_no": "PO-"+o["number"], "material": "测试物料", "date": "2026-10-01", "receive": 90000, "settled": 85000, "invoice": 60000, "diff": 25000} for o in ORGS], "by_supplier": [{"supplier": "启明医疗", "n": 5, "diff": 125000}]},
        "expense": {"detail": [{"org": o["name"], "supplier": "新源供应链", "bill_no": "AP-"+o["number"], "type": "费用应付", "noinv": 18000, "total": 23000, "settled": 5000} for o in ORGS]},
        "dash": {"detail": []},
        "recv": {"detail": [{"org": o["name"], "link_type": "采购单", "status": "已关联", "n": 12, "amount": 100000, "total": 113000} for o in ORGS]},
        "anom": {"anom": {"anom1": [{"org": "101", "code": "MAT-1", "name": "测试物料", "stock": "主仓", "end_qty": 0, "end_amount": 3600, "reason": "期末数量为零但有金额"}], "anom2": [], "anom3": []}, "stats": {}},
        "invoice": {"detail": [{"form": "增值税专票", "supplier": "海川实业", "status": "B", "n": 8}]}}
    for key, p in pages.items():
        p["cards"] = [{"value": 8, "unit": "项", "sub": "测试数据"}]
        p["note"] = "浏览器测试夹具，仅验证展示与交互。"
    mgmt = {"generated_at": "2026-10-20 10:30:00", "period": {"year": 2026, "period": 10}, "orgs": [o["number"] for o in ORGS], "pages": pages,
            "cards": [{"id": k, "title": k, "value": 8} for k in pages], "errors": []}
    customers = [{"name": r["party"], "amount": 720000-i*100000, "receivable_amount": 720000-i*100000,
                  "unsettled_amount": 180000-i*20000, "invoice_amount": 580000-i*70000,
                  "receipt_settlement_amount": 540000-i*80000, "invoice_rate": 80.5, "bill_count": 18} for i, r in enumerate(recon)]
    ytd = {"amount": 3784000, "receipt_settlement_amount": 2840000, "invoice_amount": 3230000, "unsettled_amount": 944000, "invoice_rate": 85.4, "settlement_rate": 75.1, "unsettled_ratio": 24.9, "base_amount": 3210000}
    sales = {"generated_at": "2026-10-20 10:30:00", "coverage": {}, "period": {"start": "2026-01-01", "end": "2026-10-20"}, "summary": ytd,
             "periods": {"this_month": dict(ytd, amount=512000, mom_amount=7.56, range=["2026-10-01", "2026-10-20"]), "ytd": ytd,
                         "top1_share": 24.5, "top5_share": 68.3, "top_customers": [dict(c, share=24.5-i*4) for i, c in enumerate(customers)]},
             "monthly_trend": [{"month": m, "amount": a, "receivable_amount": a*1.1, "receipt_settlement_amount": a*.72, "invoice_amount": a*.86, "unsettled_amount": a*.38, "qty": 100} for m, a in zip(MONTHS, AMOUNTS)],
             "customer_summary": customers, "salesperson_summary": [dict(c, name=f"销售员{i+1}") for i, c in enumerate(customers)],
             "material_summary": [dict(c, name=f"物料{i+1}") for i, c in enumerate(customers)], "bill_type_summary": [dict(customers[0], name="标准销售出库")],
             "unsettled_bills": [{"bill_no": f"SAL-{i:03d}", "bill_date": f"2026-{i % 10+1:02d}-10", "customer": customers[i%5]["name"], "salesperson": f"销售员{i%5+1}", "amount": 12000+i*1000, "receivable_amount": 12000+i*1000, "receipt_settlement_amount": 5000, "unsettled_amount": 7000+i*1000} for i in range(25)]}
    curves = {o["number"]: [450000+i*65000-j*(25000+i*5000) for j in range(12)] for i, o in enumerate(ORGS)}
    curves["104"] = [95000-j*19000 for j in range(12)]
    total = [sum(vals[j] for vals in curves.values()) for j in range(12)]
    cash = {"generated_at": "2026-10-20", "orgs": ORGS, "fund_errors": [], "week_labels": [f"第{i+1}周" for i in range(12)], "week_ends": [f"2026-11-{i+1:02d}" for i in range(12)],
            "kpi": {"today_position": 2480000, "ar_nominal": 3280000, "ar_expect": 2450000, "ap_nominal": 2360000, "ap_expect": 2120000, "net_expect": 330000, "net_nominal": 920000},
            "position": {o["number"]: 450000 for o in ORGS}, "curve": {"orgs": curves, "total": total},
            "org_breaches": [{"org": "104", "org_name": ORGS[3]["name"], "week": 7, "min": -114000, "end": -114000, "alert": True}],
            "ar_detail": [{"org": o["number"], "org_name": o["name"], "party": "远景科技", "due": "2026-11-01", "nominal": 100000, "prob": .78, "expect": 78000, "src": "行为曲线"} for o in ORGS],
            "ap_detail": [{"org": o["number"], "org_name": o["name"], "party": "海川实业", "due": "2026-11-01", "nominal": 80000, "prob": .9, "expect": 72000, "src": "行为曲线"} for o in ORGS]}
    return {"expense": expense, "mgmt": mgmt, "sales": sales, "cash": cash}


def run(url):
    data = fixtures()
    out = Path("artifacts/ui-v1.5.5"); out.mkdir(parents=True, exist_ok=True)
    failures, calls = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1080}, accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda e: failures.append(str(e)))

        def document(route):
            response = route.fetch()
            route.fulfill(response=response, body=response.text().replace('data-logged-in="0"', 'data-logged-in="1"'))

        def api(route):
            path = route.request.url.split("/api/", 1)[1].split("?", 1)[0]
            calls.append(path)
            if path == "me": result = {"ok": True, "login": {"username": "界面测试", "name": "测试账号"}, "orgs": ORGS, "org_trace": [], "notes": []}
            elif path.startswith("dashboard/"):
                key = path.split("/")[-1].replace("mgmt-full", "mgmt")
                result = {"ok": True, "job": {"status": "done", "result": copy.deepcopy(data[key])}}
            else: result = {"ok": True}
            route.fulfill(json=result)

        page.route(re.compile(re.escape(url) + r"/\?c=test$"), document)
        page.route("**/api/**", api)
        page.goto(url + "/?c=test")
        page.wait_for_load_state("networkidle")
        expect(page.locator('#v-expense .analytics')).to_be_visible()
        expect(page.locator('.brand-copy small')).to_have_text("v1.5.5")

        def view(name):
            page.locator('#nav [data-v="'+name+'"]').click()
            expect(page.locator('#v-'+name+' .analytics')).to_be_visible()
            return page.locator('#v-'+name)

        # Expense: real line mark changes the month slice, ranking filters organizations.
        root = page.locator('#v-expense')
        root.locator('[data-a="month"][data-value="2026-10"]').click()
        expect(root.locator('.a-filterbar')).to_contain_text("2026-10")
        expect(root.locator('.a-table tbody tr')).to_have_count(4)
        root.locator('[data-a="reset"]').click()
        root.locator('[data-a="org"][data-value="101"]').click()
        expect(root.locator('.a-table tbody tr')).to_have_count(10)
        root.locator('[data-a="reset"]').click()
        root.locator('[data-a="legend"]').first.click()
        expect(root.locator('.a-legend-item.off')).to_have_count(1)
        root.locator('[data-a="reset"]').click()
        # Use the actual theme control; preserve an active mark while redrawing all views.
        root.locator('[data-a="org"][data-value="101"]').click()
        page.locator('#screen-app [data-theme-toggle]').click()
        expect(page.locator('html')).to_have_attribute("data-theme", "dark")
        expect(root.locator('.a-filterbar')).to_contain_text("华北运营中心")
        page.locator('#screen-app [data-theme-toggle]').click()
        expect(page.locator('html')).to_have_attribute("data-theme", "light")
        root.locator('[data-a="reset"]').click()
        for theme in ["dark", "light"]:
            page.evaluate("t => window.portalTheme.save(t)", theme)
            page.locator('html').evaluate('(el,t)=>el.setAttribute("data-theme",t)', theme)
            for name in data:
                root = view(name)
                expect(root.locator('.a-metric')).to_have_count(4)
                assert not root.locator('.analytics').inner_text().count("NaN")
                page.screenshot(path=str(out / f"{name}-{theme}-desktop.png"), full_page=True, animations="disabled")

        root = view("mgmt")
        before = len(calls)
        root.locator('[data-a="cell"][data-value="101:est"]').click()
        expect(root.locator('.a-table tbody tr')).to_have_count(1)
        expect(root.locator('.a-table tbody')).to_contain_text("华北运营中心")
        expect(root.locator('.a-metric strong').nth(1)).to_have_text("¥50,000.00")
        assert len(calls) == before, "Chart interactions must not issue new Kingdee requests"
        root.locator('[data-a="reset"]').click()
        root.locator('[data-a="type"][data-value="负债"]').click()
        expect(root.locator('select[data-a="source"]')).to_have_value("subject")
        expect(root.locator('.a-table tbody tr')).to_have_count(1)
        expect(root.locator('.a-table tbody')).to_contain_text("负债")
        root.locator('[data-a="reset"]').click()
        for source in data["mgmt"]["pages"]:
            root.locator('select[data-a="source"]').select_option(source)
            expect(root.locator('.a-detail')).to_be_visible()
        root.locator('[data-a="reset"]').click()
        for source in data["mgmt"]["pages"]:
            root.locator('.a-source[data-value="'+source+'"]').click()
            expect(root.locator('[data-sub-back]')).to_be_visible()
            root.locator('[data-sub-back]').click()
        root.locator('[data-a="sort"][data-value="amount"]').click()
        expect(root.locator('.a-table tbody tr').first).to_contain_text("50,000.00")
        root.locator('[data-a="search"]').fill("启明")
        expect(root.locator('.a-table tbody tr')).to_have_count(1)
        with page.expect_download() as download:
            root.locator('[data-a="export"]').click()
        download.value.save_as(str(out / "filtered.csv"))
        exported = (out / "filtered.csv").read_text(encoding="utf-8-sig")
        assert "启明医疗" in exported and "远景科技" not in exported
        root.locator('[data-a="reset"]').click()

        root = view("sales")
        root.locator('[data-a="party"][data-value="远景科技"]').click()
        expect(root.locator('.a-table tbody tr')).to_have_count(5)
        expect(root.locator('.a-metric strong').first).to_have_text("¥720,000.00")
        root.locator('[data-a="reset"]').click()
        root.locator('[data-a="month"][data-value="2026-10"]').click()
        expect(root.locator('.a-table tbody tr')).to_have_count(2)
        expect(root.locator('.a-progress b').first).to_have_text("78.2%")
        root.locator('[data-a="reset"]').click()
        root.locator('select[data-a="source"]').select_option("material")
        root.locator('[data-a="party"]').first.click()
        expect(root.locator('.a-detail')).to_contain_text("物料汇总工作表")
        root.locator('[data-a="reset"]').click()

        root = view("cash")
        root.locator('select[data-a="org"]').select_option("104")
        expect(root.locator('.a-metric strong').nth(1)).to_have_text("¥-114,000.00")
        root.locator('[data-a="month"][data-value="第8周"]').click()
        expect(root.locator('.a-table tbody tr')).to_have_count(1)
        expect(root.locator('.a-detail')).to_contain_text("第8周头寸工作表")
        root.locator('[data-a="reset"]').click()
        # Keyboard activation and accessible tooltip.
        mark = root.locator('[data-a="month"]').first
        mark.focus(); expect(page.locator('.a-tooltip')).to_be_visible(); mark.press("Enter")
        expect(root.locator('.a-filterbar')).to_contain_text("第1周")
        root.locator('[data-a="reset"]').click()

        for width in [390, 768]:
            page.set_viewport_size({"width": width, "height": 844})
            for name in data:
                root = view(name)
                overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                assert not overflow, f"Page overflow at {width}: {name}"
                assert root.locator('.a-metrics').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1'), f"KPI overflow: {name}"
                page.screenshot(path=str(out / f"{name}-light-{width}.png"), full_page=True, animations="disabled")

        # Edge responses: empty, missing source, partial/negative numbers, and hostile labels.
        for name in data:
            html = page.evaluate('''([v,d,orgs])=>window.PortalAnalytics.render(v,d,{orgs})''', [name, {}, ORGS])
            assert "NaN" not in html and "undefined" not in html and "Infinity" not in html
        sparse = copy.deepcopy(data["mgmt"])
        del sparse["pages"]["bank"]
        sparse["errors"] = ["头寸查询失败"]
        html = page.evaluate('''([d,o])=>window.PortalAnalytics.render("mgmt",d,{orgs:o})''', [sparse, ORGS])
        assert "未取得" in html
        malicious = copy.deepcopy(data["expense"])
        malicious["exp_type_pie"] = [["<img src=x onerror=alert(1)>", 800], ["负调整", -200]]
        html = page.evaluate('''([d,o])=>window.PortalAnalytics.render("expense",d,{orgs:o})''', [malicious, ORGS])
        assert "<img src=x" not in html and "&lt;img" in html and "a-ring-segment" not in html
        partial_cash = copy.deepcopy(data["cash"])
        partial_cash["curve"]["total"][3] = None
        partial_cash["org_breaches"] = []
        partial_cash["fund_errors"] = ["部分头寸未取得"]
        html = page.evaluate('''([d,o])=>window.PortalAnalytics.render("cash",d,{orgs:o})''', [partial_cash, ORGS])
        assert "数据不完整" in html and "各组织未见穿底" not in html
        # Large monetary values must remain inside the two-column mobile KPI strip.
        large = copy.deepcopy(data["expense"])
        large["metrics"]["borrow_unpaid"] = -1234567890.12
        page.evaluate('''([d,o])=>{document.getElementById("v-cash").innerHTML=window.PortalAnalytics.render("expense",d,{orgs:o});}''', [large, ORGS])
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.locator('#v-cash .a-metrics').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1'), "Large KPI overflow"
        assert not failures, failures
        (out / "result.json").write_text(json.dumps({"status": "passed", "page_errors": failures, "api_requests": len(calls), "screenshots": 16, "data": "synthetic API fixtures; no live tenant queried"}, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()
        print("PASS: four dashboards, 12 sources, filtering, sort, export, drill/back, themes, keyboard, 390/768 layouts, empty/partial/signed data")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8875")
    run(parser.parse_args().url)
