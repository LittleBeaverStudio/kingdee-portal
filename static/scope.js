/* Local projections of the current account's authorized, already loaded snapshots. */
(function () {
  "use strict";
  function copy(x) { return JSON.parse(JSON.stringify(x)); }
  function sum(rows, key) { return rows.reduce(function (a, r) { return a + (typeof r[key] === "number" ? r[key] : 0); }, 0); }
  function add(rows) {
    var out = {};
    rows.forEach(function (r) { Object.keys(r || {}).forEach(function (k) {
      if (r[k] === null) out[k] = null;
      else if (typeof r[k] === "number" && out[k] !== null && !/rate|ratio|share|avg_price|mom_|yoy_/.test(k)) out[k] = (out[k] || 0) + r[k];
    }); });
    return out;
  }
  function rates(r) {
    [["invoice_rate", "invoice_amount"], ["settlement_rate", "receipt_settlement_amount"], ["unsettled_ratio", "unsettled_amount"]].forEach(function (p) { r[p[0]] = r.receivable_amount ? r[p[1]] / r.receivable_amount * 100 : null; });
    r.avg_price = r.qty ? r.amount / r.qty : null; return r;
  }
  function group(rows, keys, sales) {
    var map = {};
    rows.forEach(function (r) { var key = JSON.stringify(keys.map(function (k) { return r[k]; })); (map[key] || (map[key] = [])).push(r); });
    return Object.keys(map).map(function (k) { var result = add(map[k]); keys.forEach(function (key) { result[key] = map[k][0][key]; }); return sales ? rates(result) : result; });
  }
  function project(view, raw, selected, orgs) {
    var d = copy(raw), all = selected.length === orgs.length && orgs.every(function (o) { return selected.indexOf(String(o.number)) >= 0; });
    var ids = selected.map(String), names = orgs.filter(function (o) { return ids.indexOf(String(o.number)) >= 0; }).map(function (o) { return o.name; });
    function accepts(value) { return ids.indexOf(String(value)) >= 0 || names.indexOf(String(value)) >= 0; }
    function filtered(rows) { return (rows || []).filter(function (r) { return accepts(r.org_number || r.org || r.sale_org || r.org_name); }); }
    function walk(x) {
      if (Array.isArray(x)) return x.filter(function (r) { return !r || typeof r !== "object" || !(r.org_number || r.org || r.sale_org || r.org_name) || accepts(r.org_number || r.org || r.sale_org || r.org_name); }).map(walk);
      if (x && typeof x === "object") { var y = {}; Object.keys(x).forEach(function (k) { y[k] = walk(x[k]); }); return y; } return x;
    }
    if (view === "expense" && raw.by_org) {
      var parts = Object.keys(raw.by_org).filter(accepts).map(function (k) { return raw.by_org[k]; });
      d.metrics = add(parts.map(function (p) { return p.metrics; }));
      d.monthly = {}; parts.forEach(function (p) { Object.keys(p.monthly || {}).forEach(function (m) { d.monthly[m] = add([d.monthly[m] || {}, p.monthly[m]]); }); });
      ["borrow_bills", "apply_bills", "unpaid_bills", "pending_bills", "org_rows", "staff_rows"].forEach(function (key) { d[key] = [].concat.apply([], parts.map(function (p) { return p[key] || []; })); });
      d.exp_type_pie = group([].concat.apply([], parts.map(function (p) { return (p.exp_type_pie || []).map(function (r) { return { name: r[0], amount: r[1] }; }); })), ["name"]).map(function (r) { return [r.name, r.amount]; });
      d.status_rows = group([].concat.apply([], parts.map(function (p) { return p.status_rows || []; })), ["kind", "status"]);
      d.aging = {}; ["borrow", "unpaid"].forEach(function (key) { d.aging[key] = group([].concat.apply([], parts.map(function (p) { return (p.aging || {})[key] || []; })), ["label"]); });
      if (!parts.length) Object.keys(raw.metrics || {}).forEach(function (key) { d.metrics[key] = null; });
    } else if (view === "sales" && raw.by_org) {
      var saleParts = Object.keys(raw.by_org).filter(accepts).map(function (k) { return raw.by_org[k]; });
      d.summary = rates(add(saleParts.map(function (p) { return p.summary; })));
      ["monthly_trend", "customer_summary", "salesperson_summary", "material_summary", "bill_type_summary"].forEach(function (key) {
        d[key] = group([].concat.apply([], saleParts.map(function (p) { return p[key] || []; })), [key === "monthly_trend" ? "month" : "name"], true);
        d[key].sort(function (a, b) { return key === "monthly_trend" ? a.month.localeCompare(b.month) : Math.abs(b.unsettled_amount || 0) - Math.abs(a.unsettled_amount || 0); });
      });
      // 排行维度的月度轨迹：按 (dim, name, month) 合并各组织的同名对象。
      d.dimension_trend = group([].concat.apply([], saleParts.map(function (p) { return p.dimension_trend || []; })), ["dim", "name", "month"], false);
      [["customer", "customer_summary"], ["salesperson", "salesperson_summary"], ["material", "material_summary"]].forEach(function (p) { d.summary[p[0] + "_count"] = d[p[1]].length; });
      d.unsettled_bills = [].concat.apply([], saleParts.map(function (p) { return p.unsettled_bills || []; }));
      d.periods = {}; ["this_month", "prev_month", "ly_same_month", "ytd"].forEach(function (key) {
        var rows = saleParts.map(function (p) { return (p.periods || {})[key] || {}; }); d.periods[key] = Object.assign({}, (raw.periods || {})[key] || {}, rates(add(rows)));
        ["mom_amount", "yoy_amount"].forEach(function (k) { d.periods[key][k] = null; });
      });
      function growth(a, b) { return b ? (a / b - 1) * 100 : null; }
      d.periods.this_month.mom_amount = growth(d.periods.this_month.amount, d.periods.prev_month.amount);
      d.periods.this_month.yoy_amount = growth(d.periods.this_month.amount, d.periods.ly_same_month.amount);
      var customers = d.customer_summary.slice().sort(function (a, b) { return b.amount - a.amount; }), total = sum(customers, "amount");
      d.periods.top_customers = customers.slice(0, 8).map(function (r) { return { name: r.name, amount: r.amount, share: total ? r.amount / total * 100 : null }; });
      d.periods.top5_share = total ? sum(customers.slice(0, 5), "amount") / total * 100 : null;
      d.periods.top1_share = total && customers.length ? customers[0].amount / total * 100 : null;
      if (!saleParts.length) { d.summary = {}; d.periods.this_month = {}; d.periods.ytd = {}; }
    } else if (view === "mgmt") {
      d = walk(d); d.orgs = ids;
      Object.keys(d.pages || {}).forEach(function (key) {
        var p = d.pages[key];
        if (p.fund) { p.fund.grand = add(p.fund.rows || []); p.fund.org_count = (p.fund.rows || []).length; p.fund.account_count = sum((p.fund.rows || []).map(function (r) { return { n: (r.accounts || []).length }; }), "n"); }
        if (p.subject) { p.subject.total = sum(p.subject.rows || [], "fund_balance"); p.subject.books_used = (p.subject.rows || []).length; }
        if (!all) {
          // Untagged supplier totals and global counters cannot represent a selected organization.
          p.by_supplier = [];
          var detail = (p.detail || []).filter(function (r) { return r.org || r.org_number || r.org_name; });
          if (p.detail) p.detail = detail;
          var value = null;
          if (key === "invoice") value = sum(detail, "n");
          if (key === "anom") value = Object.keys(p.anom || {}).reduce(function (n, k) { return n + (p.anom[k] || []).length; }, 0);
          if (key === "interdim") value = (p.orgs || []).reduce(function (n, o) { return n + (o.rows || []).length; }, 0);
          if ((p.errors || []).length) value = null;
          p.cards = [{ value: value, unit: "项", sub: "当前组织 · 已提取明细" }];
        }
      });
      if (!all) d.cards = (d.cards || []).map(function (c) { return Object.assign({}, c, { value: null }); });
    } else if (view === "cash") {
      d = walk(d); d.position = {}; d.curve = { orgs: {}, total: [] };
      Object.keys((raw.curve || {}).orgs || {}).filter(accepts).forEach(function (k) { d.curve.orgs[k] = raw.curve.orgs[k].slice(); d.position[k] = raw.position[k]; });
      var curves = Object.keys(d.curve.orgs).map(function (k) { return d.curve.orgs[k]; });
      d.curve.total = (raw.week_labels || []).map(function (_, i) { return curves.length && curves.every(function (c) { return typeof c[i] === "number"; }) ? curves.reduce(function (a, c) { return a + c[i]; }, 0) : null; });
      if (raw.kpi_by_org) d.kpi = add(Object.keys(raw.kpi_by_org).filter(accepts).map(function (k) { return raw.kpi_by_org[k]; }));
      else if (!all) d.kpi = {};
      // Per-organization inflow/outflow composition: explains why an org curve declines.
      d.kpi_by_org = {};
      Object.keys(raw.kpi_by_org || {}).filter(accepts).forEach(function (k) { d.kpi_by_org[k] = raw.kpi_by_org[k]; });
      d.kpi.today_position = curves.length ? sum(Object.keys(d.position).map(function (k) { return { v: d.position[k] }; }), "v") : null;
      d.kpi.net_expect = d.kpi.ar_expect == null || d.kpi.ap_expect == null ? null : d.kpi.ar_expect - d.kpi.ap_expect;
      d.kpi.net_nominal = d.kpi.ar_nominal == null || d.kpi.ap_nominal == null ? null : d.kpi.ar_nominal - d.kpi.ap_nominal;
      // Curve-consistent net: matches the weekly change of the 12-week curve exactly.
      d.kpi.net_flow_expect = d.kpi.ar_in_expect == null || d.kpi.ap_out_expect == null ? null : d.kpi.ar_in_expect - d.kpi.ap_out_expect;
      var first = d.curve.total.findIndex(function (x) { return x != null && x < 0; }); d.kpi.first_negative_week = first < 0 ? null : first + 1;
    } else if (!all) {
      d = walk(d);
      // Older snapshots without organization aggregates must not masquerade as locally filtered totals.
      if (view === "expense") { d.metrics = {}; d.monthly = {}; d.exp_type_pie = []; d.aging = {}; d.status_rows = []; d.staff_rows = []; }
      if (view === "sales") { d.summary = {}; d.periods = {}; d.monthly_trend = []; ["customer_summary", "salesperson_summary", "material_summary", "bill_type_summary", "unsettled_bills", "dimension_trend"].forEach(function (k) { d[k] = []; }); }
    }
    if (!selected.length) d._emptyScope = true;
    return d;
  }
  window.PortalScope = { select: project };
})();
