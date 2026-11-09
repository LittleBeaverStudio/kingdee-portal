"""v1.5.8 浏览器回归：维度行搬家、整屏子表、Tableau 图表、现金流口径。

合成数据，不连接任何金蝶账套。
"""
import argparse
import copy
import json
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from ui_v157 import snapshots
from ui_dashboard import ORGS


def run(url):
    out = Path("artifacts/ui-v1.5.8"); out.mkdir(parents=True, exist_ok=True)
    data = snapshots()
    jobs, calls, errors = {}, [], []

    # 给销售快照补上排行维度的月度轨迹（后端 dimension_trend 的合成替身）
    dim_rows = []
    for r in data["sales"]["monthly_trend"]:
        for dim, key in [("customer", "customer"), ("salesperson", "salesperson"),
                         ("material", "material"), ("bill_type", "bill_type")]:
            dim_rows.append({"dim": dim, "name": "%s-%s" % (dim, r["month"]),
                             "month": r["month"], "amount": r["amount"],
                             "receipt_settlement_amount": r["receipt_settlement_amount"],
                             "unsettled_amount": r["unsettled_amount"]})
    data["sales"]["dimension_trend"] = dim_rows
    # 组织级投影来源：每个组织也要带一份 dimension_trend（与真实后端一致）
    for org, part in data["sales"].get("by_org", {}).items():
        part["dimension_trend"] = copy.deepcopy(dim_rows)
    # 未结单据里混入一条已全额结清的单据，验证前端不再显示它
    data["sales"]["unsettled_bills"].append(
        {"bill_no": "SAL-ZERO", "bill_date": "2026-10-10", "customer": "已结清客户",
         "salesperson": "销售员9", "amount": 1000, "receivable_amount": 1000,
         "invoice_amount": 1000, "receipt_settlement_amount": 1000, "unsettled_amount": 0})
    # 现金流：给每个组织补上与曲线同口径的流入流出（scope.js 会按组织求和，
    # 因此各组织的值要按组织数分摊，与真实后端的 kpi_by_org 结构一致）。
    k = data["cash"]["kpi"]
    k.setdefault("ar_in_expect", k["ar_expect"])
    k.setdefault("ap_out_expect", k["ap_expect"])
    k.setdefault("net_flow_expect", k["ar_expect"] - k["ap_expect"])
    n_org = len(data["cash"]["kpi_by_org"])
    for org, part in data["cash"]["kpi_by_org"].items():
        for key in ("ar_in_expect", "ap_out_expect", "net_flow_expect"):
            part[key] = k[key] / n_org
    data["cash"]["kpi_by_org"] = data["cash"]["kpi_by_org"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport=dict(width=1600, height=1000), accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))

        def document(route):
            r = route.fetch()
            route.fulfill(response=r, body=r.text().replace('data-logged-in="0"', 'data-logged-in="1"'))

        def api(route):
            path = route.request.url.split("/api/", 1)[1].split("?")[0]
            body = route.request.post_data_json if route.request.method == "POST" else {}
            calls.append(dict(path=path, body=body))
            if path == "me":
                result = dict(ok=True, login=dict(username="界面测试"), orgs=ORGS, org_trace=[], notes=[])
            elif path.startswith("dashboard/"):
                key = path.rsplit("/", 1)[1].replace("mgmt-full", "mgmt")
                d = copy.deepcopy(data[key])
                job = "test-" + str(len(jobs)); jobs[job] = d
                result = dict(ok=True, job=dict(id=job, status="running", pct=10))
            elif path.startswith("job/"):
                result = dict(ok=True, job=dict(status="done", result=jobs[path.rsplit("/", 1)[1]]))
            else:
                result = dict(ok=True)
            route.fulfill(json=result)

        page.route(re.compile(re.escape(url) + r"/\?c=test$"), document)
        page.route("**/api/**", api)
        page.goto(url + "/?c=test"); page.wait_for_load_state("networkidle")
        expect(page.locator("#v-expense .analytics")).to_be_visible()

        def view(name):
            page.locator('#nav [data-v="' + name + '"]').click()
            root = page.locator("#v-" + name)
            expect(root.locator(".analytics")).to_be_visible()
            return root

        # ---- 1. 「深入分析」字样消失，维度进入分析指标行 ----
        for name in ["expense", "sales", "cash"]:
            root = view(name)
            assert "深入分析" not in root.inner_text(), name
            assert root.locator(".a-seg").count() >= 1, name
            assert root.locator(".a-drills").count() == 0, name
            assert root.locator(".a-toolbar .a-seg").count() == 1, name

        # ---- 2. 销售：排行维度与客户贡献排行共用同一套对象 ----
        root = view("sales")
        assert root.locator('select[data-a="source"]').count() == 1, "排行维度应在分析指标行"
        assert root.locator('select[data-a="traj"]').count() == 0, "不应再有独立的轨迹维度选择器"
        root.locator('select[data-a="source"]').select_option("salesperson")
        page.wait_for_timeout(300)
        assert root.locator(".a-tline").count() >= 1, "选定排行维度后轨迹应切到面积折线"
        # 排行面板与轨迹必须是同一维度（销售员）
        panel_title = root.locator(".a-panel").nth(1).inner_text()
        assert "销售员" in panel_title, panel_title
        expect(root.locator(".a-metric strong").first).to_have_text("¥512,000.00")

        # ---- 3. 维度子表整屏（不占半屏） ----
        root = view("sales")
        root.locator('.a-seg-btn:text-is("客户")').click()
        page.wait_for_timeout(400)
        assert root.locator(".a-dimview").count() == 1
        assert root.locator(".a-grid").count() == 0, "维度子表不应再挤在两栏网格里"
        assert root.locator(".a-dimgrid").count() == 1
        widths = root.evaluate("""() => [...document.querySelectorAll('#v-sales .a-dimview > .a-panel, #v-sales .a-dimview > .a-detail')]
            .map(e => Math.round(e.getBoundingClientRect().width))""")
        assert widths and min(widths) > 1200, ("维度子表应占整个内容宽度", widths)
        assert root.locator(".a-tbars").count() >= 1, "客户维度应有 Tableau 风格条形"
        page.screenshot(path=str(out / "sales-dim-customer.png"), full_page=True)

        # ---- 4. 未结清单不再出现「未结算金额为零」 ----
        root.locator('.a-seg-btn:text-is("未结清单")').click()
        page.wait_for_timeout(400)
        assert "已结清客户" not in root.inner_text(), "已结清单据不应出现"
        rows = root.locator(".a-detail .a-table tbody tr")
        assert rows.count() > 0
        vals = root.evaluate("""() => [...document.querySelectorAll('#v-sales .a-detail .a-table tbody tr')]
            .map(r => r.lastElementChild.textContent.trim())""")
        assert all(v not in ("¥0.00", "0", "0.00") for v in vals), vals

        # 返回总览
        root.locator(".a-subback").click()
        page.wait_for_timeout(400)
        assert root.locator(".a-dimview").count() == 0

        # ---- 5. 现金流：期望流入流出与组织构成 ----
        root = view("cash")
        assert "期望流入与流出" in root.inner_text()
        assert "名义未结与期望收付" not in root.inner_text()
        assert "期望净流入（与曲线同口径）" in root.inner_text()
        assert "组织期望流入流出构成" in root.inner_text()
        assert "其中周期付款" in root.inner_text()
        # 期望流入 = 未结应收 + 预计新增；期望流出 = 未结应付 + 预计新增 + 周期付款
        expect_text = root.locator(".a-expect").inner_text()
        assert "期望流入" in expect_text and "期望流出" in expect_text
        assert "预计新增" in expect_text and "周期付款" in expect_text
        # 净流入 = 流入 − 流出（与曲线同口径）
        vals = root.evaluate("""() => {
            const rows = [...document.querySelectorAll('#v-cash .a-expect-row')];
            const num = t => Number(String(t).replace(/[^0-9.-]/g, ''));
            const inflow = num(rows[0].querySelector('strong').textContent);
            const outflow = num(rows[1].querySelector('strong').textContent);
            const net = num(document.querySelector('#v-cash .a-expect .a-selection-summary b').textContent);
            return { inflow, outflow, net };
        }""")
        assert abs((vals["inflow"] - vals["outflow"]) - vals["net"]) < 1, vals
        page.screenshot(path=str(out / "cash-home.png"), full_page=True)

        root.locator('.a-seg-btn:text-is("逐周曲线")').click()
        page.wait_for_timeout(400)
        assert root.locator(".a-dimview").count() == 1
        assert root.locator(".a-tline").count() == 1
        page.screenshot(path=str(out / "cash-dim-curve.png"), full_page=True)

        # ---- 6. 费用维度子表 ----
        root = view("expense")
        root.locator('.a-seg-btn:text-is("账龄")').click()
        page.wait_for_timeout(400)
        assert root.locator(".a-dimview").count() == 1
        page.screenshot(path=str(out / "expense-dim-aging.png"), full_page=True)

        # ---- 7. 无横向溢出 + 无 NaN ----
        for width in [1920, 1280, 768, 390]:
            page.set_viewport_size(dict(width=width, height=1000 if width > 1000 else 844))
            for name in ["expense", "sales", "cash"]:
                root = view(name)
                assert not page.evaluate("document.documentElement.scrollWidth>innerWidth+1"), (name, width)
                assert "NaN" not in root.inner_text() and "Infinity" not in root.inner_text(), (name, width)
                page.screenshot(path=str(out / f"{name}-{width}.png"), full_page=True)

        assert not errors, errors
        (out / "result.json").write_text(json.dumps(
            dict(status="passed", page_errors=errors, api_calls=len(calls),
                 data="synthetic; no live Kingdee account queried"), ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()
    print("PASS: dims in metric row; full-width dim views; Tableau charts; zero-unsettled excluded; cash caliber reconciled")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--url", default="http://127.0.0.1:8875")
    run(parser.parse_args().url)
