/* Analytical workbooks. All marks are derived from dashboard responses; no demo data here. */
(function () {
  "use strict";
  var states = {}, tables = {}, contexts = {};
  var titles = { expense: ["EXPENSE INTELLIGENCE", "费用与资金占用"], mgmt: ["MANAGEMENT ACCOUNTING", "财务健康与风险"], sales: ["SALES PERFORMANCE", "销售与回款表现"], cash: ["CASH FLOW OUTLOOK", "未来 12 周资金展望"] };
  var sources = { bank: "头寸与科目核对", subject: "科目余额波动", est: "应付与暂估", interdim: "往来核算维度", arhx: "应收核销", aphx: "应付核销", goods: "有货未回票", expense: "非货未回票", dash: "横杠单号", recv: "收票单", anom: "存货异常", invoice: "未审核发票" };
  var cats = { "1122": "应收账款", "1123": "预付账款", "1221": "其他应收", "2203": "预收账款", "2202ZG": "应付暂估", "2202MX": "应付明细", "2241": "其他应付" };
  function state(v) { return states[v] || (states[v] = { org: "", month: "", type: "", party: "", source: v === "mgmt" ? "est" : (v === "sales" ? "all" : ""), metric: "amount", range: "all", zoom: {}, search: "", sort: "", desc: true, page: 0, hidden: {}, dim: "" }); }
  function esc(x) { return String(x == null ? "" : x).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function n(x) { return x == null || x === "" || !isFinite(Number(x)) ? 0 : Number(x); }
  function present(x) { return x !== null && x !== undefined && x !== "" && isFinite(Number(x)); }
  function num(x, d) { return present(x) ? Number(x).toLocaleString("zh-CN", { minimumFractionDigits: d == null ? 2 : d, maximumFractionDigits: d == null ? 2 : d }) : "—"; }
  function money(x) { return present(x) ? "¥" + num(x) : "—"; }
  function compact(x) { if (!present(x)) return "—"; var a = Math.abs(Number(x)); return a >= 1e8 ? num(Number(x) / 1e8, 2) + "亿" : a >= 1e4 ? num(Number(x) / 1e4, 2) + "万" : num(x, a < 100 ? 2 : 0); }
  function sum(xs, key, abs) { return xs.reduce(function (a, x) { return a + (abs ? Math.abs(n(x[key])) : n(x[key])); }, 0); }
  function color(i) { return "var(--a-" + ["blue", "teal", "amber", "violet", "slate", "red"][i % 6] + ")"; }
  function empty(text) { return '<div class="a-empty"><svg class="icon"><use href="#i-chart"/></svg><span>' + esc(text || "当前范围暂无数据") + "</span></div>"; }
  function act(field, value, label, cls, extra) { return '<button type="button" class="' + (cls || "a-chip") + '" data-a="' + esc(field) + '" data-value="' + esc(value) + '"' + (extra || "") + ">" + label + "</button>"; }
  function select(field, label, options, value) { return '<label class="a-select"><span>' + esc(label) + '</span><select data-a="' + esc(field) + '" aria-label="' + esc(label) + '">' + options.map(function (x) { return '<option value="' + esc(x[0]) + '"' + (String(value) === String(x[0]) ? " selected" : "") + ">" + esc(x[1]) + "</option>"; }).join("") + "</select></label>"; }
  function group(xs, label, key) { var map = Object.create(null); xs.forEach(function (r) { if (!present(r[key])) return; var k = String(r[label] || "未分类"); map[k] = (map[k] || 0) + n(r[key]); }); return Object.keys(map).map(function (k) { return { label: k, v: map[k] }; }).sort(function (a, b) { return Math.abs(b.v) - Math.abs(a.v); }); }
  function orgNo(r, v) {
    var val = String(r.org_number || r.org || r.org_name || ""), os = contexts[v].orgs || [];
    var hit = os.find(function (o) { return String(o.number) === val || o.name === val; });
    return hit ? String(hit.number) : val;
  }
  function orgName(no, v) { var hit = (contexts[v].orgs || []).find(function (o) { return String(o.number) === String(no); }); return hit ? hit.name : no; }
  function inOrg(rows, v) { var selected = state(v).org; return selected ? rows.filter(function (r) { return orgNo(r, v) === selected; }) : rows; }
  function panel(index, title, content, tag, cls) { return '<section class="a-panel ' + (cls || "") + '"><div class="a-panel-head"><h3><span>' + index + "</span>" + esc(title) + '</h3><span class="a-caption">' + (tag || "") + "</span></div>" + content + "</section>"; }
  function metric(label, value, foot, tone, spark) {
    var svg = "";
    if (spark && spark.length > 1) {
      var min = Math.min.apply(null, spark), max = Math.max.apply(null, spark), span = max - min || 1;
      svg = '<svg class="a-spark" viewBox="0 0 100 28" aria-hidden="true"><polyline points="' + spark.map(function (v, i) { return (i / (spark.length - 1) * 100).toFixed(2) + "," + (26 - (v - min) / span * 23).toFixed(2); }).join(" ") + '"/></svg>';
    }
    return '<article class="a-metric ' + (tone || "") + (String(value).length > 14 ? " a-long-number" : "") + '"><span class="a-metric-label">' + esc(label) + '</span><strong>' + esc(value) + '</strong><div class="a-metric-foot"><span>' + esc(foot || "") + "</span>" + svg + "</div></article>";
  }
  function head(v, d, controls) {
    var s = state(v), filters = [];
    [["type", s.type], ["party", s.party]].forEach(function (x) { if (x[1]) filters.push(act(x[0], "", esc(x[1]) + ' ×', "a-filter")); });
    return '<div class="analytics" data-workbook="' + v + '"><div class="a-toolbar"' + (!controls ? ' hidden' : '') + '><div class="a-controls">' + (controls || "") + '</div></div>' + (filters.length ? '<div class="a-filterbar">' + filters.join("") + '</div>' : "");
  }
  function tips(text) { return ' data-tip="' + esc(text) + '"'; }
  function bars(rows, field, picked, opts) {
    opts = opts || {}; rows = rows.filter(function (r) { return present(r.v); });
    if (!rows.length) return empty();
    var max = Math.max.apply(null, rows.map(function (r) { return Math.abs(n(r.v)); })) || 1;
    var hasNegative = rows.some(function (r) { return n(r.v) < 0; });
    return '<div class="a-ranks' + (hasNegative ? " diverge" : "") + '">' + rows.slice(0, opts.limit || 8).map(function (r, i) {
      var val = n(r.v), key = r.key == null ? r.label : r.key, on = String(picked) === String(key), fmt = opts.count ? num(r.v, 0) : money(r.v);
      var w = Math.abs(val) / max * (hasNegative ? 50 : 100), left = hasNegative ? (val < 0 ? 50 - w : 50) : 0;
      var c = opts.semantic && val > 0 ? "var(--a-amber)" : val < 0 ? "var(--a-teal)" : color(opts.multicolor ? i : 0);
      return '<button type="button" class="a-rank' + (on ? " selected" : "") + '" data-a="' + field + '" data-value="' + esc(key) + '" aria-pressed="' + on + '"' + tips(r.label + "\n" + fmt + (r.note ? "\n" + r.note : "")) + '><span class="a-rank-label"><i>' + String(i + 1).padStart(2, "0") + "</i><span>" + esc(r.label) + '</span></span><span class="a-rank-track"><i style="left:' + left.toFixed(2) + "%;width:" + w.toFixed(2) + "%;background:" + c + '"></i></span><b>' + esc(opts.count ? num(r.v, 0) : compact(r.v)) + "</b></button>";
    }).join("") + "</div>";
  }
  function legend(series, chartId, s) {
    return '<div class="a-legend">' + series.map(function (r, i) { var off = (s.hidden[chartId] || []).indexOf(r.key || r.name) >= 0; return act("legend", chartId + ":" + (r.key || r.name), '<i style="background:' + (r.color || color(i)) + '"></i>' + esc(r.name), "a-legend-item" + (off ? " off" : ""), ' aria-pressed="' + !off + '"'); }).join("") + "</div>";
  }
  function line(labels, series, chartId, v, field, selected) {
    if (!labels.length) return empty();
    var s = state(v), totalLabels = labels.length, zoom = s.zoom[chartId] || { count: Math.min(12, totalLabels), start: Math.max(0, totalLabels - 12) };
    zoom.count = Math.min(totalLabels, Math.max(2, zoom.count)); zoom.start = Math.max(0, Math.min(totalLabels - zoom.count, zoom.start)); s.zoom[chartId] = zoom;
    labels = labels.slice(zoom.start, zoom.start + zoom.count);
    series = series.map(function (r) { return Object.assign({}, r, { values: r.values.slice(zoom.start, zoom.start + zoom.count), notes: (r.notes || []).slice(zoom.start, zoom.start + zoom.count) }); });
    var hidden = s.hidden[chartId] || [], shown = series.filter(function (r) { return hidden.indexOf(r.key || r.name) < 0; });
    var vals = [0]; shown.forEach(function (r) { r.values.forEach(function (x) { if (present(x)) vals.push(Number(x)); }); });
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals), span = hi - lo || 1;
    hi += span * .12; if (lo < 0) lo -= span * .08;
    var W = 780, H = 270, left = 60, right = 22, top = 20, bottom = 38;
    function x(i) { return left + (labels.length === 1 ? (W - left - right) / 2 : i / (labels.length - 1) * (W - left - right)); }
    function y(a) { return top + (hi - a) / (hi - lo || 1) * (H - top - bottom); }
    var out = ['<div class="a-chart-scroll" data-zoom-chart="' + chartId + '" data-total="' + totalLabels + '" tabindex="0" aria-label="滚轮缩放日期，Shift 加滚轮平移；键盘加减缩放，左右键平移"><svg class="a-line-chart" viewBox="0 0 ' + W + " " + H + '" role="group" aria-label="' + esc(chartId) + '">'];
    for (var j = 0; j <= 4; j++) { var value = lo + (hi - lo) * j / 4, yy = y(value); out.push('<line class="a-gridline" x1="' + left + '" x2="' + (W - right) + '" y1="' + yy + '" y2="' + yy + '"/><text class="a-axis" x="' + (left - 10) + '" y="' + (yy + 4) + '" text-anchor="end">' + compact(value) + "</text>"); }
    if (lo < 0) out.push('<line class="a-zero" x1="' + left + '" x2="' + (W - right) + '" y1="' + y(0) + '" y2="' + y(0) + '"/>');
    series.forEach(function (r, i) {
      if (hidden.indexOf(r.key || r.name) >= 0) return;
      var points = r.values.map(function (a, idx) { return present(a) ? [x(idx), y(Number(a))] : null; });
      // Split at unavailable points instead of drawing a fabricated zero or a connecting segment.
      var segments = [], segment = [];
      points.forEach(function (p) { if (p) segment.push(p); else if (segment.length) { segments.push(segment); segment = []; } });
      if (segment.length) segments.push(segment);
      segments.forEach(function (ps) { out.push('<polyline class="a-series" style="stroke:' + (r.color || color(i)) + '" points="' + ps.map(function (p) { return p.map(function (q) { return q.toFixed(2); }).join(","); }).join(" ") + '"' + (r.dash ? ' stroke-dasharray="5 5"' : "") + '/>'); });
      points.forEach(function (p) { if (p) out.push('<circle cx="' + p[0] + '" cy="' + p[1] + '" r="3" style="fill:' + (r.color || color(i)) + '"/>'); });
    });
    labels.forEach(function (lab, i) {
      var step = labels.length > 18 ? 3 : labels.length > 12 ? 2 : 1;
      if (i % step === 0 || i === labels.length - 1) out.push('<text class="a-axis" x="' + x(i) + '" y="' + (H - 12) + '" text-anchor="middle">' + esc(String(lab).replace(/^20\d\d-/, "")) + "</text>");
      var text = lab + "\n" + shown.map(function (r) { return r.name + "：" + (r.unit === "天" ? num(r.values[i], 1) + " 天" : money(r.values[i])) + (r.notes[i] ? " · " + r.notes[i] : ""); }).join("\n");
      var colWidth = Math.min(56, (W - left - right) / labels.length);
      out.push('<g class="a-mark' + (String(selected) === String(lab) ? " selected" : "") + '" tabindex="0" role="img" aria-label="' + esc(text) + '" aria-pressed="' + (String(selected) === String(lab)) + '" data-date="' + esc(lab) + '"' + tips(text) + '><rect x="' + (x(i) - colWidth / 2) + '" y="' + top + '" width="' + colWidth + '" height="' + (H - top - bottom) + '" rx="5"/><line x1="' + x(i) + '" x2="' + x(i) + '" y1="' + top + '" y2="' + (H - bottom) + '"/></g>');
    });
    out.push("</svg></div>" + legend(series, chartId, s)); return out.join("");
  }
  function ring(rows, picked) {
    if (!rows.length) return empty();
    // Negative adjustments stay in the signed bar chart, rather than a misleading pie slice.
    if (rows.some(function (r) { return n(r.v) < 0; })) return bars(rows, "type", picked, { multicolor: true });
    var total = sum(rows, "v"); if (!total) return empty("费用类型金额为零");
    var acc = 0, segments = rows.map(function (r, i) {
      var share = n(r.v) / total * 100, offset = -acc; acc += share;
      return '<circle class="a-ring-segment' + (picked && picked !== r.label ? " dimmed" : "") + '" cx="72" cy="72" r="54" fill="none" stroke="' + color(i) + '" stroke-width="16" pathLength="100" stroke-dasharray="' + share + " " + (100 - share) + '" stroke-dashoffset="' + offset + '" transform="rotate(-90 72 72)" tabindex="0" role="button" aria-label="' + esc(r.label + " " + money(r.v)) + '" data-a="type" data-value="' + esc(r.label) + '"' + tips(r.label + "\n" + money(r.v) + "\n占比 " + num(share, 1) + "%") + '/>';
    });
    return '<div class="a-ring-layout"><svg viewBox="0 0 144 144" class="a-ring" role="group" aria-label="费用类型构成">' + segments.join("") + '<text x="72" y="67" text-anchor="middle" class="a-ring-caption">报销金额</text><text x="72" y="90" text-anchor="middle" class="a-ring-total">' + compact(total) + '</text></svg><div class="a-ring-key">' + rows.slice(0, 6).map(function (r, i) { return act("type", r.label, '<i style="background:' + color(i) + '"></i><span>' + esc(r.label) + '</span><b>' + num(n(r.v) / total * 100, 1) + "%</b>", "a-ring-item" + (picked === r.label ? " selected" : "")); }).join("") + "</div></div>";
  }
  function detail(v, title, columns, rows, controls, note, visual) {
    var s = state(v), q = s.search.trim().toLowerCase(), filtered = rows.filter(function (r) { return !q || columns.some(function (c) { return String(r[c.key] == null ? "" : r[c.key]).toLowerCase().indexOf(q) >= 0; }); });
    var col = columns.find(function (c) { return c.key === s.sort; });
    if (col) filtered.sort(function (a, b) { var x = a[col.key], y = b[col.key], c = col.numeric ? n(x) - n(y) : String(x || "").localeCompare(String(y || ""), "zh-CN"); return (s.desc ? -1 : 1) * c; });
    tables[v] = { columns: columns, rows: filtered, title: title };
    var size = 10, pages = Math.max(1, Math.ceil(filtered.length / size)); s.page = Math.min(s.page, pages - 1);
    var pageRows = filtered.slice(s.page * size, (s.page + 1) * size);
    var content = '<div class="a-table-tools"><div class="a-controls">' + (controls || "") + '</div><div class="a-table-actions"><input class="a-search" data-a="search" aria-label="搜索明细" placeholder="搜索单号、组织、往来方…" value="' + esc(s.search) + '">' + act("export", "", '<svg class="icon"><use href="#i-file"/></svg>导出', "a-text-btn") + '</div></div>';
    if (visual) content += visual;
    content += pageRows.length ? '<div class="a-table-scroll"><table class="a-table"><thead><tr>' + columns.map(function (c) { return '<th' + (c.numeric ? ' class="num"' : "") + '>' + act("sort", c.key, esc(c.label) + '<span class="a-sort">' + (s.sort === c.key ? (s.desc ? "↓" : "↑") : "↕") + "</span>", "a-sort-btn") + '</th>'; }).join("") + '</tr></thead><tbody>' + pageRows.map(function (r) { return '<tr>' + columns.map(function (c) { return '<td' + (c.numeric ? ' class="num"' : "") + '>' + esc(c.money ? money(r[c.key]) : c.percent ? (present(r[c.key]) ? num(r[c.key], 1) + "%" : "—") : c.numeric ? num(r[c.key], c.decimals || 0) : (r[c.key] == null || r[c.key] === "" ? "—" : r[c.key])) + '</td>'; }).join("") + '</tr>'; }).join("") + '</tbody></table></div>' : empty("没有符合筛选条件的已提取明细");
    content += '<div class="a-table-foot"><span>' + esc(note || "已提取明细") + ' · ' + filtered.length + ' 行</span><div>' + act("page", Math.max(0, s.page - 1), "←", "a-page", ' aria-label="上一页"' + (!s.page ? " disabled" : "")) + '<b>' + (s.page + 1) + ' / ' + pages + '</b>' + act("page", Math.min(pages - 1, s.page + 1), "→", "a-page", ' aria-label="下一页"' + (s.page === pages - 1 ? " disabled" : "")) + '</div></div>';
    return panel("DATA", title, content, "可排序 / 筛选 / 导出", "a-detail");
  }
  function col(key, label, kind) { return { key: key, label: label, numeric: !!kind, money: kind === "money", percent: kind === "percent" }; }
  /* 深入分析维度：搬进「分析指标」行，不再单独占一行，也不带「深入分析」字样。 */
  function dimNav(items, active) {
    return '<div class="a-seg" role="group" aria-label="分析维度">' + items.map(function (x) {
      return act("dim", x[0], esc(x[1]), "a-seg-btn" + (active === x[0] ? " selected" : ""));
    }).join("") + "</div>";
  }
  function dimHead(title, desc) {
    return '<div class="a-subhead"><div><h3>' + esc(title) + '</h3><p>' + esc(desc) + "</p></div>"
      + act("dim", "", "返回总览", "a-subback") + "</div>";
  }
  function periodMonths(keys, s) { return s.range === "all" ? keys : keys.slice(-Number(s.range)); }

  /* ---------------- Tableau 风格图表 ----------------
     水平条形：条末端直标金额 + 占比，单色强调，无多余描边。
     面积折线：仅横向网格、末端直标、悬停竖线定位，用于月度趋势。 */
  function tbars(rows, field, picked, opts) {
    opts = opts || {};
    var list = rows.filter(function (r) { return present(r.v); })
      .sort(function (a, b) { return Math.abs(n(b.v)) - Math.abs(n(a.v)); });
    if (!list.length) return empty();
    var total = list.reduce(function (a, r) { return a + Math.abs(n(r.v)); }, 0);
    var max = list.reduce(function (a, r) { return Math.max(a, Math.abs(n(r.v))); }, 0) || 1;
    var limit = opts.limit || list.length;
    return '<div class="a-tbars">' + list.slice(0, limit).map(function (r, i) {
      var val = n(r.v), key = r.key == null ? r.label : r.key, on = String(picked) === String(key);
      var share = total ? Math.abs(val) / total * 100 : 0;
      var c = r.color || (val < 0 ? "var(--a-red)" : (opts.color || "var(--a-blue)"));
      var body = '<span class="a-tbar-lab"><i>' + String(i + 1).padStart(2, "0") + '</i><span>' + esc(r.label) + '</span></span>'
        + '<span class="a-tbar-track"><i style="width:' + (Math.abs(val) / max * 100).toFixed(2) + '%;background:' + c + '"></i></span>'
        + '<b>' + (opts.count ? num(val, 0) : money(val)) + '</b><em>' + num(share, 1) + '%</em>';
      return act(field, key, body, "a-tbar" + (on ? " selected" : ""),
        tips(r.label + "\n" + money(val) + "\n占比 " + num(share, 1) + "%" + (r.note ? "\n" + r.note : "")));
    }).join("") + "</div>";
  }

  function tline(labels, series, chartId, v, field, selected) {
    if (!labels.length) return empty();
    var s = state(v);
    var vals = [0];
    series.forEach(function (r) { r.values.forEach(function (x) { if (present(x)) vals.push(Number(x)); }); });
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals), span = hi - lo || 1;
    hi += span * .16; if (lo < 0) lo -= span * .1;
    var W = 1180, H = 330, left = 74, right = 96, top = 26, bottom = 42;
    function x(i) { return left + (labels.length === 1 ? (W - left - right) / 2 : i / (labels.length - 1) * (W - left - right)); }
    function y(a) { return top + (hi - a) / (hi - lo || 1) * (H - top - bottom); }
    var out = ['<div class="a-chart-scroll" data-zoom-chart="' + chartId + '" data-total="' + labels.length + '" tabindex="0" aria-label="滚轮缩放日期"><svg class="a-tline" viewBox="0 0 ' + W + " " + H + '" role="group" aria-label="' + esc(chartId) + '">'];
    for (var j = 0; j <= 4; j++) {
      var value = lo + (hi - lo) * j / 4, yy = y(value);
      out.push('<line class="a-gridline" x1="' + left + '" x2="' + (W - right) + '" y1="' + yy + '" y2="' + yy + '"/>'
        + '<text class="a-axis" x="' + (left - 12) + '" y="' + (yy + 4) + '" text-anchor="end">' + compact(value) + "</text>");
    }
    if (lo < 0) out.push('<line class="a-zero" x1="' + left + '" x2="' + (W - right) + '" y1="' + y(0) + '" y2="' + y(0) + '"/>');
    // 末端直标先算好位置，再统一防重叠（否则多条线同高时标签会叠在一起）。
    var endLabels = [];
    series.forEach(function (r, i) {
      var c = r.color || color(i), pts = r.values.map(function (a, idx) { return present(a) ? [x(idx), y(Number(a))] : null; });
      var segs = [], seg = [];
      pts.forEach(function (p) { if (p) seg.push(p); else if (seg.length) { segs.push(seg); seg = []; } });
      if (seg.length) segs.push(seg);
      segs.forEach(function (ps) {
        var path = ps.map(function (p) { return p[0].toFixed(2) + "," + p[1].toFixed(2); }).join(" ");
        // 面积只在「连续有值」的区段内闭合，缺口不补零（避免画出假的塌陷）。
        if (r.area !== false && ps.length > 1) {
          out.push('<polygon class="a-tarea' + (r.context ? " context" : "") + '" style="fill:' + c + '" points="' + ps[0][0].toFixed(2) + "," + y(lo).toFixed(2) + " " + path + " " + ps[ps.length - 1][0].toFixed(2) + "," + y(lo).toFixed(2) + '"/>');
        }
        out.push('<polyline class="a-tline-stroke' + (r.context ? " context" : "") + '" style="stroke:' + c + '" points="' + path + '"/>');
      });
      var last = -1;
      for (var q = r.values.length - 1; q >= 0; q--) if (present(r.values[q])) { last = q; break; }
      if (last >= 0) {
        out.push('<circle class="a-tdot' + (r.context ? " context" : "") + '" cx="' + x(last).toFixed(2) + '" cy="' + y(Number(r.values[last])).toFixed(2) + '" r="3.5" style="fill:' + c + '"/>');
        endLabels.push({ x: x(last) + 10, y: y(Number(r.values[last])), c: c, text: compact(r.values[last]), context: r.context });
      }
    });
    endLabels.sort(function (a, b) { return a.y - b.y; });
    for (var e = 1; e < endLabels.length; e++) {
      if (endLabels[e].y - endLabels[e - 1].y < 13) endLabels[e].y = endLabels[e - 1].y + 13;
    }
    endLabels.forEach(function (el) {
      out.push('<text class="a-tendlabel' + (el.context ? " context" : "") + '" x="' + el.x.toFixed(2) + '" y="' + (el.y + 4).toFixed(2) + '" style="fill:' + el.c + '">' + el.text + "</text>");
    });
    labels.forEach(function (lab, i) {
      var step = labels.length > 18 ? 3 : labels.length > 12 ? 2 : 1;
      if (i % step === 0 || i === labels.length - 1) {
        out.push('<text class="a-axis" x="' + x(i).toFixed(2) + '" y="' + (H - 14) + '" text-anchor="middle">' + esc(String(lab).replace(/^20\d\d-/, "")) + "</text>");
      }
      var text = lab + "\n" + series.map(function (r) { return r.name + "：" + money(r.values[i]); }).join("\n");
      var colWidth = Math.min(72, (W - left - right) / labels.length);
      out.push('<g class="a-mark' + (String(selected) === String(lab) ? " selected" : "") + '" tabindex="0" role="img" aria-label="' + esc(text) + '" data-date="' + esc(lab) + '"' + tips(text) + '><rect x="' + (x(i) - colWidth / 2).toFixed(2) + '" y="' + top + '" width="' + colWidth.toFixed(2) + '" height="' + (H - top - bottom) + '" rx="5"/><line x1="' + x(i).toFixed(2) + '" x2="' + x(i).toFixed(2) + '" y1="' + top + '" y2="' + (H - bottom) + '"/></g>');
    });
    out.push("</svg></div>");
    out.push('<div class="a-legend a-legend-top">' + series.map(function (r, i) {
      var c = r.color || color(i);
      return '<span class="a-legend-item' + (r.context ? " context" : "") + '"><i style="background:' + c + '"></i>' + esc(r.name) + "</span>";
    }).join("") + "</div>");
    return out.join("");
  }

  function expense(d) {
    var v = "expense", s = state(v), m = d.metrics || {}, monthly = d.monthly || {}, keys = periodMonths(Object.keys(monthly).sort(), s);
    var records = [];
    [["borrow_bills", "借款未还", "balance"], ["apply_bills", "申请未报销", "diff"], ["unpaid_bills", "报销未付款", "unpaid"]].forEach(function (spec) { (d[spec[0]] || []).forEach(function (r) { records.push({ billno: r.billno, staff: r.staff, org: r.org, org_name: r.org_name || r.org, date: r.date, kind: spec[1], amount: r[spec[2]] }); }); });
    var filtered = records.filter(function (r) { return (!s.month || String(r.date).slice(0, 7) === s.month) && (!s.org || orgNo(r, v) === s.org); });
    var orgs = d.org_rows || [], orgOptions = [["", "全部组织"]].concat(orgs.map(function (r) { return [orgNo(r, v), r.org_name || r.org]; }));
    var typeHit = (d.exp_type_pie || []).find(function (r) { return r[0] === s.type; });
    var expDims = [["borrow", "借款"], ["apply", "未报销"], ["unpaid", "未付款"], ["bills", "单据规模"], ["orgs", "组织"], ["aging", "账龄"], ["status", "状态"], ["staff", "人员"], ["charts", "趋势与结构"]];
    var h = head(v, d, dimNav(expDims, s.dim));
    // Headline KPIs stay at the complete API scope. Detail slices are explicitly separate.
    h += '<div class="a-metrics">' + metric("借款未还", money(m.borrow_unpaid), num(m.n_borrow, 0) + " 笔 · 全部已选组织", "amber") + metric("已申请未报销", money(m.apply_unreimb), "全部已选组织 · 未关闭申请", "blue") + metric("报销未付款", money(m.unpaid), "全部已选组织 · 待支付", "teal") + metric("申请 / 报销单", num(m.n_apply, 0) + " / " + num(m.n_reimb, 0), "全部已选组织 · 张", "slate") + '</div>';
    if (s.dim) return h + expenseDimView(d, s, expDims) + '</div>';
    h += '<div class="a-grid a-wide-left">' + panel("01", "申请与报销趋势", line(keys, [{ name: "申请金额", values: keys.map(function (k) { return monthly[k].apply; }) }, { name: "报销金额", values: keys.map(function (k) { return monthly[k].reimb; }) }], "expense-trend", v, "month", s.month), "元 · 月度") + panel("02", "费用类型构成", ring((d.exp_type_pie || []).map(function (r) { return { label: r[0], v: r[1] }; }), s.type) + (typeHit ? '<div class="a-selection-summary"><span>' + esc(typeHit[0]) + '</span><b>' + money(typeHit[1]) + '</b></div>' : ""), "全部已选组织 · 报销单据体") + '</div>';
    var ag = d.aging || {}, agingLabels = (ag.borrow || []).map(function (r) { return r.label; });
    h += '<div class="a-grid">' + panel("03", "组织资金占用", bars(orgs.map(function (r) { return { label: r.org_name || r.org, key: orgNo(r, v), v: n(r.borrow_unpaid) + n(r.apply_unreimb) + n(r.unpaid), note: "借款 + 未报销 + 未付款" }; }), "org", s.org), "全部已选组织 · 元") + panel("04", "未结账龄", agingBars(agingLabels, ag.borrow || [], ag.unpaid || []), "全部已选组织 · 元") + '</div>';
    if (s.month) { var mm = monthly[s.month] || {}; h += '<div class="a-selection-summary"><span>' + esc(s.month) + ' 月度汇总</span><b>申请 ' + money(mm.apply) + '</b><b>报销 ' + money(mm.reimb) + '</b></div>'; }
    h += detail(v, "未结单据工作表", [col("billno", "单号"), col("kind", "未结类型"), col("staff", "人员"), col("org_name", "组织"), col("date", "日期"), col("amount", "未结金额", "money")], filtered, "", "每类最多 60 张；费用类型仅联动构成图");
    return h + '</div>';
  }

  /* 费用看板的维度子表：整屏展示。 */
  function expenseDimView(d, s, dims) {
    var v = "expense", id = s.dim, m = d.metrics || {}, ag = d.aging || {};
    var titles = { borrow: "借款未还明细", apply: "已申请未报销", unpaid: "报销未付款", bills: "四类单据规模", orgs: "按组织汇总", aging: "账龄分析", status: "单据状态分布", staff: "按人员汇总", charts: "趋势与结构" };
    if (id === "borrow") return '<div class="a-dimview">' + dimHead(titles[id], "实际借款 − 已报销 − 已还款 − 核销 > 0（按余额降序）")
      + detail(v, titles[id], [col("billno", "单号"), col("staff", "人员"), col("org_name", "组织"), col("date", "日期"), col("amt", "实际借款", "money"), col("settled", "已结", "money"), col("balance", "未还余额", "money")], d.borrow_bills || [], "", "每类最多 60 张") + "</div>";
    if (id === "apply") return '<div class="a-dimview">' + dimHead(titles[id], "未关闭申请单：申请金额 − 已下推报销金额")
      + detail(v, titles[id], [col("billno", "单号"), col("staff", "人员"), col("org_name", "组织"), col("date", "日期"), col("amt", "申请金额", "money"), col("pushed", "已下推", "money"), col("diff", "差额", "money")], d.apply_bills || [], "", "每类最多 60 张") + "</div>";
    if (id === "unpaid") return '<div class="a-dimview">' + dimHead(titles[id], "报销单据体未付款金额 > 0（按未付降序）")
      + detail(v, titles[id], [col("billno", "单号"), col("staff", "人员"), col("org_name", "组织"), col("date", "日期"), col("total", "报销金额", "money"), col("unpaid", "未付款", "money")], d.unpaid_bills || [], "", "每类最多 60 张") + "</div>";
    if (id === "bills") return '<div class="a-dimview">' + dimHead(titles[id], "金额合计（全部单据，不论审核状态）")
      + detail(v, "单据状态分布", [col("kind", "单据类型"), col("status", "状态"), col("n", "张数", "number"), col("amt", "金额", "money")], d.status_rows || [], "", "按类型与状态聚合") + "</div>";
    if (id === "orgs") return '<div class="a-dimview">' + dimHead(titles[id], "三大指标 + 单据规模的组织分布")
      + detail(v, titles[id], [col("org_name", "组织"), col("borrow_unpaid", "借款未还", "money"), col("apply_unreimb", "已申请未报销", "money"), col("unpaid", "报销未付款", "money"), col("n_apply", "申请单", "number"), col("n_reimb", "报销单", "number")], d.org_rows || [], "", "全部已选组织") + "</div>";
    if (id === "aging") {
      var labels = (ag.borrow || []).map(function (r) { return r.label; });
      return '<div class="a-dimview">' + dimHead(titles[id], "按单据日期距今天数分档（越旧越要盯）")
        + '<div class="a-panel a-panel-full">' + agingBars(labels, ag.borrow || [], ag.unpaid || []) + "</div>"
        + detail(v, "账龄明细", [col("label", "账龄"), col("borrow_amount", "借款未还", "money"), col("borrow_n", "笔数", "number"), col("unpaid_amount", "报销未付款", "money"), col("unpaid_n", "笔数", "number")], labels.map(function (lab, i) { var b = (ag.borrow || [])[i] || {}, u = (ag.unpaid || [])[i] || {}; return { label: lab, borrow_amount: b.amount, borrow_n: b.n, unpaid_amount: u.amount, unpaid_n: u.n }; }), "", "全部已选组织") + "</div>";
    }
    if (id === "status") return '<div class="a-dimview">' + dimHead(titles[id], "暂存 / 审核中 / 已审核 / 作废（按类型聚合）")
      + detail(v, titles[id], [col("kind", "单据类型"), col("status", "状态"), col("n", "张数", "number"), col("amt", "金额", "money")], d.status_rows || [], "", "全部已选组织") + "</div>";
    if (id === "staff") return '<div class="a-dimview">' + dimHead(titles[id], "按未结金额降序（借款未还 + 未报销 + 未付款）")
      + detail(v, titles[id], [col("name", "人员"), col("dept", "部门"), col("borrow_unpaid", "借款未还", "money"), col("apply_unreimb", "已申请未报销", "money"), col("unpaid", "报销未付款", "money"), col("n_apply", "申请单", "number"), col("n_reimb", "报销单", "number")], d.staff_rows || [], "", "全部已选组织") + "</div>";
    var mk = d.monthly || {}, mk2 = Object.keys(mk).sort();
    return '<div class="a-dimview">' + dimHead(titles[id], "月度趋势（申请 vs 报销）+ 费用类型分布")
      + '<div class="a-panel a-panel-full">' + tline(mk2, [{ name: "申请金额", key: "apply", values: mk2.map(function (k) { return mk[k].apply; }) }, { name: "报销金额", key: "reimb", values: mk2.map(function (k) { return mk[k].reimb; }), color: "var(--a-teal)" }], "expense-trend-dim", v, "month", s.month) + "</div>"
      + '<div class="a-panel a-panel-full">' + ring((d.exp_type_pie || []).map(function (r) { return { label: r[0], v: r[1] }; }), s.type) + "</div></div>";
  }
  function agingBars(labels, a, b) {
    if (!labels.length) return empty();
    var max = Math.max.apply(null, a.concat(b).map(function (r) { return Math.abs(n(r.amount)); })) || 1;
    return '<div class="a-aging">' + labels.map(function (label, i) { return '<div class="a-age-row"><span>' + esc(label) + '</span><div>' + [a[i] || {}, b[i] || {}].map(function (r, j) { return '<div class="a-age-track" tabindex="0"' + tips(label + "\n" + ["借款未还", "报销未付款"][j] + " " + money(r.amount) + "\n" + num(r.n, 0) + " 笔") + '><i style="width:' + Math.abs(n(r.amount)) / max * 100 + '%;background:' + color(j) + '"></i></div>'; }).join("") + '</div><b>' + compact(n((a[i] || {}).amount) + n((b[i] || {}).amount)) + '</b></div>'; }).join("") + '<div class="a-static-legend"><span><i style="background:' + color(0) + '"></i>借款未还</span><span><i style="background:' + color(1) + '"></i>报销未付款</span></div></div>';
  }

  function mgmtRows(d, source) {
    var p = (d.pages || {})[source] || {}, rows = [];
    if (source === "bank") rows = ((p.cross_check || {}).both || []).map(function (r) { return { org: orgNo(r, "mgmt"), name: r.org_name || r.org_number, item: "头寸 / 科目核对", amount: r.delta, base: r.fund_end, compare: r.subject }; });
    else if (source === "subject") rows = (p.swings || []).map(function (r) { return { org: orgNo(r, "mgmt"), name: r.name, item: r.type, bill: r.code, amount: r.delta, base: r.cur, compare: r.prev }; });
    else if (source === "est") rows = (p.detail || []).map(function (r) { return { org: orgNo(r, "mgmt"), name: r.supplier, item: "暂估差异", amount: r.diff_cum, base: r.notax, compare: r.est_cum }; });
    else if (source === "arhx" || source === "aphx") rows = (p.recon || []).map(function (r) { return { org: orgNo(r, "mgmt"), name: r.party, item: "核销差异", amount: r.diff, base: r.bill, compare: r.pay }; });
    else if (source === "interdim") (p.orgs || []).forEach(function (o) { (o.rows || []).forEach(function (r) { rows.push({ org: orgNo(o, "mgmt"), name: r.dim, item: Object.keys(r.cats || {}).map(function (k) { return cats[k] || k; }).join(" / "), amount: r.net }); }); });
    else if (source === "anom") Object.keys(p.anom || {}).forEach(function (key) { (p.anom[key] || []).forEach(function (r) { rows.push({ org: orgNo(r, "mgmt"), name: r.name, item: r.reason, bill: r.code, amount: r.end_amount }); }); });
    else rows = (p.detail || []).map(function (r) { return { org: orgNo(r, "mgmt"), name: r.supplier || r.party || r.org || r.form, item: r.material || r.type || r.link_type || r.status || r.side || "", bill: r.bill_no, amount: source === "goods" ? r.diff : source === "expense" ? r.noinv : source === "recv" ? r.total : r.amount, count: r.n, base: source === "goods" ? r.settled : null, compare: source === "goods" ? r.invoice : null }; });
    rows.forEach(function (r) { r.org_name = orgName(r.org, "mgmt"); }); return rows;
  }
  function matrix(d, orgs) {
    var pages = d.pages || {}, s = state("mgmt"), specs = [["bank", "头寸差额"], ["est", "暂估差额"], ["arhx", "应收核销"], ["aphx", "应付核销"]];
    var data = specs.map(function (sp) { if (!pages[sp[0]]) return null; if (sp[0] === "est") return (pages.est.org_summary || []).map(function (r) { return { org: orgNo(r, "mgmt"), amount: r.diff }; }); return mgmtRows(d, sp[0]); });
    var values = orgs.map(function (o) { return data.map(function (rows) { if (!rows) return null; var found = rows.filter(function (r) { return r.org === o; }); return found.length && found.every(function (r) { return present(r.amount); }) ? sum(found, "amount") : null; }); });
    if (!orgs.length) return empty("暂无可对照的组织数据");
    var maxima = specs.map(function (_, i) { return Math.max.apply(null, values.map(function (vs) { return Math.abs(n(vs[i])); })) || 1; });
    return '<div class="a-matrix-scroll"><table class="a-matrix"><thead><tr><th>组织 / 差额</th>' + specs.map(function (sp) { return '<th>' + esc(sp[1]) + '</th>'; }).join("") + '</tr></thead><tbody>' + orgs.map(function (org, idx) { return '<tr class="' + (s.org && s.org !== org ? "a-dimmed" : "") + '"><th>' + act("org", org, '<small>' + esc(org) + '</small>' + esc(orgName(org, "mgmt")), "a-matrix-org" + (s.org === org ? " selected" : "")) + '</th>' + specs.map(function (sp, j) { var val = values[idx][j], alpha = val == null || !val ? 0 : .12 + Math.abs(val) / maxima[j] * .62; return '<td>' + act("cell", org + ":" + sp[0], '<span>' + (val == null ? "—" : compact(val)) + '</span>', "a-cell" + (s.org === org && s.source === sp[0] ? " selected" : ""), ' style="--cell-color:' + (n(val) < 0 ? "var(--a-teal-rgb)" : "var(--a-amber-rgb)") + ';--cell-alpha:' + alpha.toFixed(3) + '"' + tips(orgName(org, "mgmt") + "\n" + sp[1] + " " + money(val))) + '</td>'; }).join("") + '</tr>'; }).join("") + '</tbody></table></div><div class="a-matrix-legend"><span>负差额</span><i></i><span>正差额</span><em>各列独立色阶 · 元</em></div>';
  }
  function capitalPanel(d) {
    var rows = d.working_capital_history || [], annual = d.working_capital_ytd || [], ids = (contexts.mgmt.selected || d.orgs || []).map(String);
    var labels = d.capital_periods || Array.from(new Set(rows.map(function (r) { return r.period; }))).sort();
    var specs = [["DSO", "应收周转天数", "ar", "revenue", "blue"], ["DIO", "存货周转天数", "inventory", "cost", "amber"], ["DPO", "应付周转天数", "ap", "cost", "teal"], ["CCC", "现金转换周期", "", "", "violet"]];
    function aggregate(data, period) {
      var byOrg = {}, out = { period: period }, common = [];
      data.filter(function (r) { return r.period === period && ids.indexOf(String(r.org)) >= 0; }).forEach(function (r) { (byOrg[r.org] || (byOrg[r.org] = [])).push(r); });
      function valid(r, spec) { return r && present(r[spec[2]]) && r[spec[2]] >= 0 && present(r[spec[3]]) && r[spec[3]] > 0 && present(r.days) && r.days > 0; }
      ids.forEach(function (id) { var rs = byOrg[id] || []; if (rs.length === 1 && specs.slice(0, 3).every(function (sp) { return valid(rs[0], sp); })) common.push(rs[0]); });
      specs.slice(0, 3).forEach(function (sp) {
        var eligible = common.length ? common : ids.map(function (id) { var rs = byOrg[id] || []; return rs.length === 1 && valid(rs[0], sp) ? rs[0] : null; }).filter(Boolean);
        var days = eligible.length ? eligible[0].days : null;
        if (eligible.some(function (r) { return r.days !== days; })) eligible = [];
        out[sp[0]] = { value: eligible.length ? sum(eligible, sp[2]) / sum(eligible, sp[3]) * days : null,
          orgs: eligible.map(function (r) { return String(r.org); }).sort(), balance: eligible.length ? sum(eligible, sp[2]) : null, flow: eligible.length ? sum(eligible, sp[3]) : null };
      });
      var full = common.length && [out.DSO, out.DIO, out.DPO].every(function (r) { return present(r.value); });
      out.CCC = { value: full ? out.DSO.value + out.DIO.value - out.DPO.value : null, orgs: full ? common.map(function (r) { return String(r.org); }).sort() : [] };
      out.byOrg = byOrg; return out;
    }
    var trend = labels.map(function (lab) { return aggregate(rows, lab); });
    var candidates = trend.filter(function (r) { return present(r.CCC.value); });
    if (!candidates.length) candidates = trend.filter(function (r) { return specs.some(function (sp) { return present(r[sp[0]].value); }); });
    var latest = candidates[candidates.length - 1] || aggregate(rows, labels[labels.length - 1] || "");
    var ytd = aggregate(annual, latest.period), index = labels.indexOf(latest.period), prior = index > 0 ? trend[index - 1] : null;
    var body = '<div class="a-capital-period"><div><span class="a-eyebrow">WORKING CAPITAL / 财报口径</span><b>' + (d.capital_pending ? '月度财报后台加载中' : latest.period ? esc(latest.period) + ' · 最近可用完整月' : '尚无可用完整月财报') + '</b></div><span>' + (present(latest.CCC.value) ? '共同覆盖 ' + latest.CCC.orgs.length + ' / ' + ids.length + ' 个已选组织' : '按各指标有效组织呈现') + '</span></div>';
    body += '<div class="a-capital-flow">' + specs.map(function (sp) {
      var now = latest[sp[0]], yy = ytd[sp[0]], old = prior && prior[sp[0]], comparable = old && present(old.value) && present(now.value) && JSON.stringify(old.orgs) === JSON.stringify(now.orgs);
      var delta = comparable ? now.value - old.value : null;
      return '<article class="a-capital-item ' + sp[4] + '"><div><b>' + sp[0] + '</b><span>' + sp[1] + '</span></div><strong>' + num(now.value, 1) + '<small>天</small></strong><p class="a-capital-change">较上月 ' + (delta == null ? '—' : (delta > 0 ? '+' : '') + num(delta, 1) + ' 天') + '</p><div class="a-capital-reference"><span>本年累计</span><b>' + num(yy.value, 1) + '<small>天</small></b></div><p>月度覆盖 ' + now.orgs.length + ' / ' + ids.length + ' · 累计覆盖 ' + yy.orgs.length + ' / ' + ids.length + '</p></article>';
    }).join("") + '</div>';
    body += '<div class="a-capital-status">' + (d.capital_pending ? '四个首页优先加载；月度财报完成后自动呈现。' : present(latest.CCC.value) ? 'CCC ' + (latest.CCC.value < 0 ? '为负：收款周期短于付款周期' : '反映约 ' + num(latest.CCC.value, 1) + ' 天经营资金占用') + '。指标按余额与收入 / 成本合计计算；环比仅比较相同覆盖组织。' : '未取得完整财报余额或正的营业收入 / 成本，暂不判断资金周期。') + '</div>';
    if (rows.length) body += '<div class="a-capital-trend"><div class="a-capital-trend-head"><b>月度变化</b><span>天 · 滚轮缩放日期 · 图例切换指标</span></div>' + line(labels, specs.map(function (sp) { return { name: sp[0], unit: "天", color: "var(--a-" + sp[4] + ")", values: trend.map(function (r) { return r[sp[0]].value; }), notes: trend.map(function (r) { return '覆盖 ' + r[sp[0]].orgs.length + ' / ' + ids.length + ' 组织'; }) }; }), "capital-trend", "mgmt", "", "") + '</div>';
    body += '<details class="a-method a-capital-method"><summary>财报来源、计算口径与组织覆盖</summary><p>' + esc(d.capital_source || '金蝶财报 · 资产负债表 + 利润表') + '。月度平均余额 =（上月财报期末 + 本月期末）÷ 2；累计平均余额 =（年初 + 本月期末）÷ 2。月度分母用本期收入 / 成本，累计用财报本年金额；乘对应自然天数。</p><p>DSO = 平均应收 ÷ 营业收入 × 天数；DIO = 平均存货 ÷ 营业成本 × 天数；DPO = 平均应付 ÷ 营业成本 × 天数；CCC = DSO + DIO − DPO。DPO 采用成本近似采购额；应收为财报净额，未校正税额，组织间交易未合并抵销。</p><p>存在共同完整组织时，四项使用同一组织集合；缺数组织不计入汇总。下表列出当前月数据，趋势悬停查看各月覆盖；覆盖变化时应结合组织明细判断。</p>';
    body += '<div class="a-table-scroll"><table class="a-table"><thead><tr><th>已选组织</th><th>平均应收</th><th>平均存货</th><th>平均应付</th><th>本期营业收入</th><th>本期营业成本</th><th>本月覆盖</th></tr></thead><tbody>' + ids.map(function (id) {
      var rs = latest.byOrg[id] || [], r = rs.length === 1 ? rs[0] : {}, covered = latest.CCC.orgs.indexOf(id) >= 0;
      var note = covered ? '已纳入共同口径' : rs.length > 1 ? '财报不唯一' : !rs.length ? '本月财报未取得' : [r.ar, r.ap, r.inventory].some(function (v) { return !present(v); }) ? '缺少本月 / 上月余额' : [r.ar, r.ap, r.inventory].some(function (v) { return v < 0; }) ? '负余额，请核对重分类' : '收入 / 成本缺失或非正';
      return '<tr><td>' + esc(orgName(id, 'mgmt')) + '</td>' + ['ar', 'inventory', 'ap', 'revenue', 'cost'].map(function (k) { return '<td class="num">' + money(r[k]) + '</td>'; }).join('') + '<td>' + note + '</td></tr>';
    }).join('') + '</tbody></table></div>';
    (d.capital_errors || []).filter(function (r) { return !r.org || ids.indexOf(String(r.org)) >= 0; }).slice(0, 40).forEach(function (e) { body += '<p>' + esc((e.org ? orgName(e.org, 'mgmt') + ' · ' : '') + (e.period ? e.period + ' · ' : '') + e.reason) + '</p>'; });
    body += '</details>';
    return panel("CYCLE", "营运资金效率", body, "最近完整月 + 月度趋势 + 本年累计", "a-capital-panel");
  }
  function mgmt(d) {
    var v = "mgmt", s = state(v), p = d.pages || {}, known = Object.keys(sources), orgSet = {};
    known.forEach(function (key) { mgmtRows(d, key).forEach(function (r) { if (r.org) orgSet[r.org] = true; }); });
    (d.orgs || []).forEach(function (o) { orgSet[String(o)] = true; });
    var orgs = Object.keys(orgSet).sort(), cross = inOrg(((p.bank || {}).cross_check || {}).both || [], v), est = inOrg((p.est || {}).org_summary || [], v);
    var ar = inOrg((p.arhx || {}).recon || [], v), ap = inOrg((p.aphx || {}).recon || [], v);
    var h = head(v, d, "");
    h += '<div class="a-metrics">' + metric("头寸核对差额", money(cross.length ? sum(cross, "delta", true) : null), cross.length ? cross.filter(function (r) { return Math.abs(n(r.delta)) > .5; }).length + " 个差异组织 · 绝对值合计" : "无可对照组织明细", "blue") + metric("应付与暂估差额", money(est.length && est.every(function (r) { return present(r.diff); }) ? sum(est, "diff") : null), "按组织汇总 · 净差额", "amber") + metric("应收核销差异", money(ar.length && ar.every(function (r) { return present(r.diff); }) ? sum(ar, "diff", true) : null), "已提取对账明细 · 绝对值合计", "teal") + metric("应付核销差异", money(ap.length && ap.every(function (r) { return present(r.diff); }) ? sum(ap, "diff", true) : null), "已提取对账明细 · 绝对值合计", "violet") + '</div>';
    h += capitalPanel(d);
    var swings = inOrg((p.subject || {}).swings || [], v), types = group(swings, "type", "delta");
    h += '<div class="a-grid a-wide-left">' + panel("01", "组织差异矩阵", matrix(d, orgs), "已提取数据 · 净差额") + panel("02", "科目余额变动", bars(types, "type", s.type, { semantic: true }) + '<div class="a-panel-bottom">' + act("drill", "subject", num(swings.length, 0) + ' 个波动科目 <span>查看明细 ↗</span>', "a-link-row") + '</div>', "科目类型 · 元") + '</div>';
    var goods = inOrg(mgmtRows(d, "goods"), v), expenseRows = inOrg(mgmtRows(d, "expense"), v);
    var risks = [{ label: "有货未回票", key: "goods", v: sum(goods, "amount", true) }, { label: "非货未回票", key: "expense", v: sum(expenseRows, "amount", true) }].filter(function (r) { return !!p[r.key]; });
    var reconSource = s.source === "aphx" ? "aphx" : "arhx", reconRows = inOrg(mgmtRows(d, reconSource), v);
    h += '<div class="a-grid">' + panel("03", "核销差异 · 往来方", bars(reconRows.slice().sort(function (a, b) { return Math.abs(b.amount) - Math.abs(a.amount); }).map(function (r) { return { label: r.name, key: r.name, v: r.amount, note: orgName(r.org, v) }; }), "party", s.party, { semantic: true, limit: 6 }), select("recon", "核销方向", [["arhx", "应收"], ["aphx", "应付"]], reconSource)) + panel("04", "票据与存货关注", bars(risks, "source", s.source, { multicolor: true }) + '<div class="a-attention-grid">' + [["invoice", "待审核发票"], ["anom", "存货异常"], ["interdim", "跨科目往来"]].map(function (sp) { var cards = (p[sp[0]] || {}).cards || []; if (((p[sp[0]] || {}).errors || []).length) cards = []; return act("drill", sp[0], '<span>' + sp[1] + '</span><b>' + (cards.length ? num(cards[0].value, 0) : "—") + '</b>', "a-attention", !p[sp[0]] ? " disabled" : ""); }).join("") + '</div><div class="a-scope-note">上方三项计数 · 全部已选组织</div>', "已提取明细金额绝对值 · 元") + '</div>';
    h += panel("SOURCES", "数据源工作台", '<div class="a-source-grid">' + known.map(function (key, i) { var page = p[key], issues = page && ((page.errors || []).length || (page.dropped || []).length); return act("drill", key, '<span class="a-source-num">' + String(i + 1).padStart(2, "0") + '</span><span>' + sources[key] + '</span><i class="' + (!page ? "bad" : issues ? "warn" : "") + '"></i><small>' + (!page ? ((d.deferred_sources || []).indexOf(key) >= 0 ? "后台待加载" : "未取得") : issues ? "有告警" : "已提取") + '</small>', "a-source", !page ? " disabled" : ""); }).join("") + '</div>', Object.keys(p).filter(function (key) { return known.indexOf(key) >= 0; }).length + " / 12 已提取");
    var rows = inOrg(mgmtRows(d, s.source), v).filter(function (r) { return (!s.type || s.source !== "subject" || r.item === s.type) && (!s.party || r.name === s.party); });
    var cols = [col("org_name", "组织"), col("name", "对象 / 往来方"), col("bill", "单号 / 科目"), col("item", "类别")];
    if (s.source === "invoice") cols.push(col("count", "笔数", "number"));
    else { if (["bank", "subject", "est", "arhx", "aphx", "goods"].indexOf(s.source) >= 0) { var labs = { bank: ["头寸期末", "科目余额"], subject: ["本期末", "上期末"], est: ["累计不含税", "累计暂估"], goods: ["已结算", "开票"] }[s.source] || ["单据未结算", "收付款未核销"]; cols.push(col("base", labs[0], "money"), col("compare", labs[1], "money")); } cols.push(col("amount", "金额 / 差额", "money")); }
    var sourceRows = inOrg(mgmtRows(d, s.source), v).filter(function (r) { return !s.type || s.source !== "subject" || r.item === s.type; });
    var sourceBars = group(sourceRows, "name", s.source === "invoice" ? "count" : "amount");
    var sourceChart = '<div class="a-source-visual"><div><span class="a-eyebrow">FOCUS / ' + esc(s.source.toUpperCase()) + '</span><h4>' + esc(sources[s.source] || "分析主题") + ' · 对象排行</h4><p>' + (s.source === "invoice" ? "按笔数汇总" : "已提取明细 · 按金额或净差额汇总") + '</p></div>' + bars(sourceBars, "object", s.party, { semantic: true, count: s.source === "invoice", limit: 5 }) + '</div>';
    h += detail(v, "财务健康分析工作表", cols, rows, select("source", "分析主题", known.map(function (key) { return [key, sources[key]]; }), s.source), "已提取明细；对账/供应商最多 200 条，存货每类最多 100 条", sourceChart);
    var current = p[s.source];
    if (current && current.note) h += '<details class="a-method"><summary>当前工作表口径</summary><p>' + esc(current.note) + '</p></details>';
    var warnings = []; known.forEach(function (key) { var pp = p[key] || {}; (pp.errors || []).concat(pp.dropped || []).forEach(function (e) { warnings.push(sources[key] + "：" + e); }); });
    if (warnings.length) h += '<details class="a-method a-warning"><summary>数据源告警 · ' + warnings.length + '</summary>' + warnings.map(function (e) { return '<p>' + esc(e) + '</p>'; }).join("") + '</details>';
    return h + '</div>';
  }

  /* 排行维度的月度轨迹：复用客户贡献排行的同一批对象与同一套顺序。
     返回 [{name, values:[按月], color}]，values 的度量由 metric 决定。 */
  function trendByDim(d, dim, metric, months) {
    var trend = d.dimension_trend || [];
    var names = [], map = {};
    trend.forEach(function (r) {
      if (r.dim !== dim) return;
      if (!map[r.name]) { map[r.name] = {}; names.push(r.name); }
      map[r.name][r.month] = r;
    });
    if (!names.length) return [];
    var order = (d[dim + "_summary"] || []).map(function (r) { return r.name; });
    names.sort(function (a, b) {
      var ia = order.indexOf(a), ib = order.indexOf(b);
      return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib);
    });
    return names.slice(0, 6).map(function (nm, i) {
      return { name: nm, key: "trend-" + dim + "-" + nm,
        values: months.map(function (mo) { var r = map[nm][mo]; return r ? r[metric] : null; }),
        color: color(i) };
    });
  }

  function sales(d) {
    var v = "sales", s = state(v), p = d.periods || {}, tm = p.this_month || {}, ytd = p.ytd || {}, summary = d.summary || {};
    var months = periodMonths((d.monthly_trend || []).map(function (r) { return r.month; }), s), monthRows = (d.monthly_trend || []).filter(function (r) { return months.indexOf(r.month) >= 0; });
    var dims = { customer: ["customer_summary", "客户"], salesperson: ["salesperson_summary", "销售员"], material: ["material_summary", "物料"], billtype: ["bill_type_summary", "单据类型"] };
    // 排行维度：轨迹与贡献排行共用一套对象与顺序（客户贡献排行的销售员 / 物料直接复用）。
    var rankDim = dims[s.source] ? s.source : "all", rankLabel = dims[rankDim] ? dims[rankDim][1] : "汇总";
    var dim = dims[s.source] ? s.source : "customer", rows = d[dims[dim][0]] || [];
    var hitMonth = (d.monthly_trend || []).find(function (r) { return r.month === s.month; });
    var hitParty = rows.find(function (r) { return r.name === s.party; });
    var main = hitParty || hitMonth || ytd, scope = hitParty ? s.party : hitMonth ? s.month : "本年累计";
    var metricOptions = [["amount", "出库金额"], ["receipt_settlement_amount", "已结算金额"], ["unsettled_amount", "未结算金额"]];
    var dimItems = [["trend", "月度趋势"], ["customer", "客户"], ["salesperson", "销售员"], ["material", "物料"], ["billtype", "单据类型"], ["unsettled", "未结清单"], ["method", "口径"]];
    var rankOptions = [["all", "汇总"]].concat(Object.keys(dims).map(function (key) { return [key, dims[key][1]]; }));
    var h = head(v, d, select("metric", "分析指标", metricOptions, s.metric)
      + select("source", "排行维度", rankOptions, rankDim)
      + dimNav(dimItems, s.dim));
    var comparisons = [["mom_amount", "环比"], ["yoy_amount", "同比"]].filter(function (sp) { return present(tm[sp[0]]); }).map(function (sp) { return sp[1] + " " + (n(tm[sp[0]]) >= 0 ? "+" : "") + num(tm[sp[0]], 1) + "%"; }).join(" · ");
    h += '<div class="a-metrics">' + metric(s.month || s.party ? "当前出库金额" : "本月出库金额", money(s.month || s.party ? main.amount : tm.amount), s.month || s.party ? scope : comparisons || "暂无同期对比基数", "blue", monthRows.map(function (r) { return n(r.amount); })) + metric("出库金额 · " + scope, money(main.amount == null ? summary.amount : main.amount), scope, "slate") + metric("已结算 · " + scope, money(main.receipt_settlement_amount), "已收款结算口径", "teal") + metric("未结算 · " + scope, money(main.unsettled_amount), "已提取汇总口径", "amber") + '</div>';
    // 维度子表：整屏展示（独占内容区），不再挤在两栏网格里。
    if (s.dim) return h + salesDimView(d, s, dims) + '</div>';
    // 轨迹图：默认总额三条线；选定排行维度后叠加该维度 Top 对象的分月走势。
    var baseSeries = [{ name: "出库金额", key: "amount", values: monthRows.map(function (r) { return r.amount; }) }, { name: "已结算", key: "receipt_settlement_amount", values: monthRows.map(function (r) { return r.receipt_settlement_amount; }) }, { name: "未结算", key: "unsettled_amount", values: monthRows.map(function (r) { return r.unsettled_amount; }), color: "var(--a-amber)" }];
    var dimSeries = rankDim === "all" ? [] : trendByDim(d, rankDim, s.metric, months);
    var trajSeries = dimSeries.length
      ? baseSeries.map(function (r) { return Object.assign({}, r, { context: true }); }).concat(dimSeries)
      : baseSeries;
    var trajChart = dimSeries.length
      ? tline(months, trajSeries, "sales-trend", v, "month", s.month)
      : line(months, baseSeries, "sales-trend", v, "month", s.month);
    var trajNote = dimSeries.length ? "元 · 月度 · " + rankLabel + " Top " + dimSeries.length + " 分月走势" : "元 · 月度";
    h += '<div class="a-grid a-wide-left">' + panel("01", "销售与回款轨迹", trajChart, trajNote) + panel("02", rankLabel + "贡献排行", tbars(rows.map(function (r) { return { label: r.name, v: r[s.metric], key: r.name }; }), "party", s.party, { limit: 7 }), '<div class="a-panel-bottom">' + esc(rankDim === "all" ? "选择上方「排行维度」查看对象排行" : "按未结算金额绝对值 + 应收金额降序") + "</div>") + '</div>';
    // Same denominator as metrics_sales._summary/_group: receivable amount, not shipment amount.
    var rate = function (key) { var rateKey = key === "invoice_amount" ? "invoice_rate" : "settlement_rate"; return present(main[rateKey]) ? Number(main[rateKey]) : present(main[key]) && n(main.receivable_amount) !== 0 ? Number(main[key]) / Number(main.receivable_amount) * 100 : null; };
    h += '<div class="a-grid a-wide-right">' + panel("03", "开票与回款进度", progress("开票率", rate("invoice_amount"), "blue") + progress("结算率", rate("receipt_settlement_amount"), "teal") + '<div class="a-selection-summary"><span>当前范围</span><b>' + esc(scope) + '</b></div>', esc(scope)) + panel("04", "客户集中度", concentration(p), "累计客户结构") + '</div>';
    var bills = (d.unsettled_bills || []).filter(function (r) { return (!s.month || String(r.bill_date).slice(0, 7) === s.month) && (!s.party || (dim === "customer" ? r.customer === s.party : dim === "salesperson" ? r.salesperson === s.party : dim === "billtype" ? r.bill_type === s.party : true)); })
      .filter(function (r) { return Math.abs(n(r.unsettled_amount)) >= 0.01; });   // 已全额结清的不是未结单据
    // Only filter bill-level rows on dimensions actually returned by the API.
    if (s.party && dim === "material") { h += detail(v, dims[dim][1] + "汇总工作表", [col("name", dims[dim][1]), col("amount", "出库金额", "money"), col("invoice_amount", "已开票", "money"), col("receipt_settlement_amount", "已结算", "money"), col("unsettled_amount", "未结算", "money")], rows.filter(function (r) { return r.name === s.party; }), "", "当前维度汇总；月份仅联动月度汇总"); }
    else h += detail(v, "未结销售单据", [col("bill_no", "单号"), col("bill_date", "日期"), col("customer", "客户"), col("salesperson", "销售员"), col("amount", "出库金额", "money"), col("receipt_settlement_amount", "已结算", "money"), col("unsettled_amount", "未结算", "money")], bills, "", "已提取未结单据 Top100；月份、客户/销售员/单据类型联动");
    return h + '</div>';
  }

  /* 销售看板的维度子表：整屏 + Tableau 风格图表。 */
  function salesDimView(d, s, dims) {
    var v = "sales", id = s.dim, months = (d.monthly_trend || []).map(function (r) { return r.month; });    if (id === "trend") {
      var mt = d.monthly_trend || [];
      return '<div class="a-dimview">' + dimHead("月度趋势", "出库 / 已结算 / 未结算的逐月走势")
        + '<div class="a-panel a-panel-full">' + tline(mt.map(function (r) { return r.month; }), [{ name: "出库金额", key: "amount", values: mt.map(function (r) { return r.amount; }) }, { name: "已结算", key: "receipt_settlement_amount", values: mt.map(function (r) { return r.receipt_settlement_amount; }) }, { name: "未结算", key: "unsettled_amount", values: mt.map(function (r) { return r.unsettled_amount; }), color: "var(--a-amber)" }], "sales-trend-dim", v, "month", s.month) + "</div>"
        + detail(v, "月度明细", [col("month", "月份"), col("qty", "数量", "number"), col("amount", "出库金额", "money"), col("receivable_amount", "应收金额", "money"), col("invoice_amount", "开票金额", "money"), col("receipt_settlement_amount", "结算金额", "money"), col("unsettled_amount", "未结算", "money")], mt, "", "已提取月度汇总")
        + "</div>";
    }
    if (id === "unsettled") {
      var bills = (d.unsettled_bills || []).filter(function (r) { return Math.abs(n(r.unsettled_amount)) >= 0.01; });
      return '<div class="a-dimview">' + dimHead("未结销售单据", "仅显示仍有未结算金额的单据（已全额结清的不计入）")
        + detail(v, "未结销售单据", [col("bill_no", "单号"), col("bill_date", "日期"), col("customer", "客户"), col("salesperson", "销售员"), col("bill_type", "单据类型"), col("amount", "出库金额", "money"), col("receivable_amount", "应收金额", "money"), col("invoice_amount", "已开票", "money"), col("receipt_settlement_amount", "已结算", "money"), col("unsettled_amount", "未结算", "money")], bills, "", "已提取未结单据 Top100")
        + "</div>";
    }
    if (id === "method") {
      return '<div class="a-dimview">' + dimHead("口径说明", "销售与回款数据的取数与计算口径")
        + '<div class="a-panel a-panel-full"><ul class="a-notes">' + (d.method_notes || []).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") + "</ul></div></div>";
    }
    var key = dims[id] ? dims[id][0] : null, label = dims[id] ? dims[id][1] : "维度", rows = key ? (d[key] || []) : [];
    var metricKey = s.metric;
    var trend = trendByDim(d, id, metricKey, months);
    var chart = trend.length
      ? '<div class="a-panel a-panel-full">' + tline(months, trend, "sales-dim-" + id, v, "month", s.month) + "</div>"
      : '<div class="a-panel a-panel-full">' + empty("该维度暂无分月轨迹（仅按维度汇总金额）") + "</div>";
    var rank = tbars(rows.map(function (r) { return { label: r.name, v: r[metricKey], key: r.name }; }), "party", s.party, { limit: 12 });    return '<div class="a-dimview">' + dimHead(label + "维度分析", "排行与分月走势（对象顺序与「客户贡献排行」一致）")
      + '<div class="a-dimgrid">' + '<div class="a-panel">' + rank + "</div>" + chart + "</div>"
      + detail(v, label + "明细", [col("name", label), col("bill_count", "单据", "number"), col("amount", "出库金额", "money"), col("receivable_amount", "应收金额", "money"), col("invoice_amount", "已开票", "money"), col("receipt_settlement_amount", "已结算", "money"), col("unsettled_amount", "未结算", "money")], rows, "", "按未结算金额绝对值 + 应收金额降序")
      + "</div>";
  }
  function progress(label, value, tone) { return '<div class="a-progress"><div><span>' + label + '</span><b>' + (value == null ? "—" : num(value, 1) + "%") + '</b></div><div class="a-progress-track"><i style="width:' + Math.max(0, Math.min(100, n(value))) + '%;background:var(--a-' + tone + ')"></i></div></div>'; }
  function concentration(p) {
    var rows = p.top_customers || [], positive = rows.filter(function (r) { return n(r.amount) > 0; }), top = positive.slice(0, 5), totalShare = sum(top, "share");
    if (!rows.length) return empty();
    // A share above 100% is valid with returns; don't force it into a part-to-whole strip.
    var strip = totalShare >= 0 && totalShare <= 100 ? '<div class="a-concentration-strip">' + top.map(function (r, i) { return act("customer", r.name, "", "a-concentration-segment", ' style="width:' + Math.max(0, n(r.share)) + '%;background:' + color(i) + '" aria-label="' + esc(r.name + " " + num(r.share, 1) + "%") + '"' + tips(r.name + "\n" + money(r.amount))); }).join("") + '</div>' : "";
    return '<div class="a-concentration-head"><strong>' + num(p.top5_share, 1) + '<small>%</small></strong><span>Top 5 客户贡献</span><b>最大客户 ' + num(p.top1_share, 1) + '%</b></div>' + strip + '<div class="a-top-customers">' + top.slice(0, 3).map(function (r, i) { return act("customer", r.name, '<i style="background:' + color(i) + '"></i><span>' + esc(r.name) + '</span><b>' + num(r.share, 1) + '%</b>', "a-customer"); }).join("") + '</div>';
  }

  function cash(d) {
    var v = "cash", s = state(v), k = d.kpi || {}, curve = d.curve || {}, orgCurves = curve.orgs || {}, labels = d.week_labels || [], total = curve.total || [], selected = s.org ? orgCurves[s.org] || [] : total;
    var valid = selected.filter(present).map(Number), complete = labels.length > 0 && selected.length === labels.length && valid.length === labels.length && !(d.fund_errors || []).length;
    var min = complete && valid.length ? Math.min.apply(null, valid) : null, end = complete && labels.length ? selected[labels.length - 1] : null, weekIndex = labels.indexOf(s.month);
    var position = (d.fund_errors || []).length ? null : s.org ? (d.position || {})[s.org] : k.today_position;
    var h = head(v, d, dimNav([["curve", "逐周曲线"], ["ar", "应收"], ["ap", "应付"], ["risk", "风险榜"], ["breach", "组织穿底"], ["pipe", "补充预测"], ["method", "预测口径"]], s.dim));
    h += '<div class="a-metrics">' + metric("今日实际头寸", money(position), s.org ? orgName(s.org, v) : "全部已选组织", "blue") + metric("12 周预测谷值", money(min), !complete ? "曲线覆盖不完整" : (min < 0 ? "存在资金缺口" : "预测期内保持非负"), min < 0 ? "amber" : "teal", complete ? valid : null) + metric("第 12 周末头寸", money(end), "概率加权预测", "slate") + metric(s.month ? s.month + "末头寸" : "首次预计穿底", s.month ? money(selected[weekIndex]) : !complete ? "数据不完整" : (selected.findIndex(function (x) { return present(x) && Number(x) < 0; }) >= 0 ? "第 " + (selected.findIndex(function (x) { return present(x) && Number(x) < 0; }) + 1) + " 周" : "未穿底"), s.org ? orgName(s.org, v) : "合计口径", "amber") + '</div>';
    // 维度子表：整屏展示（独占内容区），不再挤在两栏网格里。
    if (s.dim) return h + cashDimView(d, s, labels, orgCurves, total) + '</div>';
    var series = [{ name: s.org ? orgName(s.org, v) : "合计", key: s.org || "total", values: selected }];
    if (!s.org) Object.keys(orgCurves).forEach(function (o, i) { series.push({ name: orgName(o, v), key: o, values: orgCurves[o], color: color(i + 1), dash: true }); });
    h += '<div class="a-grid a-wide-left">' + panel("01", "12 周资金头寸轨迹", line(labels, series, "cash-curve", v, "month", s.month), "元 · 零轴为资金警戒线") + panel("02", "组织期末头寸", bars(Object.keys(orgCurves).map(function (o) { var vals = orgCurves[o]; return { label: orgName(o, v), key: o, v: vals[weekIndex >= 0 ? weekIndex : vals.length - 1], note: s.month || "第 12 周末" }; }).filter(function (r) { return present(r.v); }).sort(function (a, b) { return a.v - b.v; }), "org", s.org, { semantic: true }), esc(s.month || "第 12 周末") + " · 元") + '</div>';
    var allCurvesComplete = labels.length > 0 && Object.keys(orgCurves).length > 0 && Object.keys(orgCurves).every(function (o) { return orgCurves[o].length === labels.length && orgCurves[o].every(present); }) && !(d.fund_errors || []).length;
    h += '<div class="a-grid">' + panel("03", "期望流入与流出", cashExpect(k), "与 12 周曲线同口径 · 元") + panel("04", "组织穿底关注", breachTable(d.org_breaches || [], s.org, allCurvesComplete), "12 周预测") + '</div>';
    if (s.month) { var weekRows = Object.keys(orgCurves).filter(function (o) { return !s.org || o === s.org; }).map(function (o) { return { org: orgName(o, v), date: (d.week_ends || [])[weekIndex], amount: orgCurves[o][weekIndex] }; }); h += detail(v, s.month + "头寸工作表", [col("org", "组织"), col("date", "周末日期"), col("amount", "预测头寸", "money")], weekRows, "", "当前周组织预测头寸"); }
    else { var side = s.source === "ap" ? "ap" : "ar", details = inOrg(d[side + "_detail"] || [], v).map(function (r) { return Object.assign({}, r, { probability: present(r.prob) ? Number(r.prob) * 100 : null }); });
      h += detail(v, "往来收付工作表", [col("org_name", "组织"), col("party", side === "ar" ? "客户" : "供应商"), col("due", "到期日"), col("nominal", "名义未结", "money"), col("probability", "概率", "percent"), col("expect", "期望金额", "money")], details, select("source", "收付方向", [["ar", "应收流入"], ["ap", "应付流出"]], side), "已提取 Top50；选择周后查看该周头寸"); }
    // 每个组织的流入流出构成：解释「曲线向下」到底来自哪一项。
    var flowRows = orgFlowRows(d, s, v);
    if (flowRows.length) {
      h += detail(v, "组织期望流入流出构成", [col("name", "组织"), col("ar_in", "期望流入", "money"),
        col("ap_out", "期望流出", "money"), col("net", "期望净流入", "money"),
        col("ap", "其中未结应付", "money"), col("pipe_ap", "其中预计新增应付", "money"),
        col("periodic", "其中周期付款", "money")], flowRows, "",
        "流出 = 未结应付期望 + 预计新增应付 + 周期性付款（工资/社保/公积金）；与 12 周曲线逐周变化同口径");
    }
    return h + '</div>';

  }
  /* 期望流入 / 流出：与 12 周曲线逐周变化同口径。
     流入 = 未结应收期望 + 预计新增应收；流出 = 未结应付期望 + 预计新增应付 + 周期性付款。
     ⚠️ 只显示 ap_expect 会让「曲线向下但没有应付流出」看起来像缺数据。 */
  function cashExpect(k) {
    var inFlow = present(k.ar_in_expect) ? k.ar_in_expect
      : (present(k.ar_expect) ? n(k.ar_expect) + n(k.pipe_ar) : null);
    var outFlow = present(k.ap_out_expect) ? k.ap_out_expect
      : (present(k.ap_expect) ? n(k.ap_expect) + n(k.pipe_ap) + n(k.periodic_total) : null);
    var max = Math.max(Math.abs(n(inFlow)), Math.abs(n(outFlow))) || 1;
    var rows = [
      { label: "期望流入", v: inFlow, tone: "teal", note: "未结应收 " + money(k.ar_expect) + " ＋ 预计新增 " + money(k.pipe_ar) },
      { label: "期望流出", v: outFlow, tone: "amber", note: "未结应付 " + money(k.ap_expect) + " ＋ 预计新增 " + money(k.pipe_ap) + " ＋ 周期付款 " + money(k.periodic_total) }
    ];
    var net = present(k.net_flow_expect) ? k.net_flow_expect
      : (present(inFlow) && present(outFlow) ? inFlow - outFlow : null);
    return '<div class="a-expect">' + rows.map(function (r) {
      return '<div class="a-expect-row"><div><b>' + r.label + '</b><span>' + r.note + '</span><strong>' + money(r.v) + '</strong></div>'
        + '<div class="a-expect-track"><i style="width:' + (Math.abs(n(r.v)) / max * 100).toFixed(2) + '%;background:var(--a-' + r.tone + ')"></i></div></div>';
    }).join("") + '<div class="a-selection-summary"><span>期望净流入（与曲线同口径）</span><b>' + money(net) + '</b></div></div>';
  }
  /* 单组织的流入流出构成：解释「该组织曲线为何向下」。 */
  function orgFlowRows(d, s, v) {
    var byOrg = d.kpi_by_org || {}, rows = [];
    Object.keys(byOrg).forEach(function (o) {
      var r = byOrg[o];
      rows.push({ org: o, name: orgName(o, v), ar_in: r.ar_in_expect, ap_out: r.ap_out_expect,
        net: r.net_flow_expect, ar: r.ar_expect, ap: r.ap_expect,
        pipe_ar: r.pipe_ar, pipe_ap: r.pipe_ap, periodic: r.periodic_total });
    });
    if (!rows.length) return [];
    rows.sort(function (a, b) { return n(a.net) - n(b.net); });
    return rows;
  }
  function breachTable(rows, org, complete) {
    if (!rows.length && !complete) return empty("预测或头寸数据不完整，无法判断组织穿底风险");
    if (!rows.length) return '<div class="a-safe-state"><span>✓</span><b>各组织未见穿底</b></div>';
    return '<div class="a-breaches">' + rows.map(function (r) { return act("org", r.org, '<span><b>' + esc(r.org_name || r.org) + '</b><small>首次第 ' + num(r.week, 0) + ' 周</small></span><strong>' + money(r.min) + '</strong><em>' + (r.alert ? "重点关注" : "关注") + '</em>', "a-breach" + (org === String(r.org) ? " selected" : "")); }).join("") + '</div>';
  }

  /* 现金流看板的维度子表：整屏 + Tableau 风格图表。 */
  function cashDimView(d, s, labels, orgCurves, total) {
    var v = "cash", id = s.dim, ends = d.week_ends || [];
    if (id === "curve") {
      var series = [{ name: "合计", key: "total", values: total }];
      Object.keys(orgCurves).forEach(function (o, i) { series.push({ name: orgName(o, v), key: o, values: orgCurves[o], color: color(i + 1), dash: true }); });
      return '<div class="a-dimview">' + dimHead("12 周头寸预测", "实际头寸 + 累计期望净流（与曲线同口径）")
        + '<div class="a-panel a-panel-full">' + tline(labels, series, "cash-curve-dim", v, "month", s.month) + "</div>"
        + detail(v, "逐周头寸", [col("org", "组织"), col("date", "周末日期"), col("amount", "预测头寸", "money")], labels.map(function (lab, i) { return { org: "合计", date: ends[i], amount: total[i] }; }), "", "合计口径逐周预测头寸")
        + "</div>";
    }
    if (id === "method") {
      return '<div class="a-dimview">' + dimHead("预测口径", "12 周现金流预测的取数与计算口径")
        + '<div class="a-panel a-panel-full"><ul class="a-notes">' + (d.method_notes || []).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") + "</ul></div></div>";
    }
    if (id === "risk" || id === "breach" || id === "pipe") {
      var body = "";
      if (id === "risk") {
        body = detail(v, "应收风险 Top10", [col("org_name", "组织"), col("party", "客户"), col("due", "到期日"), col("nominal", "名义未结", "money"), col("prob", "概率", "percent"), col("expect", "期望金额", "money")], inOrg(d.ar_risk || [], v).map(function (r) { return Object.assign({}, r, { prob: present(r.prob) ? Number(r.prob) * 100 : null }); }), "", "按期望金额降序")
          + detail(v, "应付风险 Top10", [col("org_name", "组织"), col("party", "供应商"), col("due", "到期日"), col("nominal", "名义未结", "money"), col("prob", "概率", "percent"), col("expect", "期望金额", "money")], inOrg(d.ap_risk || [], v).map(function (r) { return Object.assign({}, r, { prob: present(r.prob) ? Number(r.prob) * 100 : null }); }), "", "按期望金额降序");
      } else if (id === "breach") {
        body = detail(v, "组织穿底", [col("org_name", "组织"), col("week", "首次穿底周", "number"), col("min", "谷值", "money"), col("end", "第 12 周末", "money")], (d.org_breaches || []).map(function (r) { return { org_name: r.org_name || r.org, week: r.week, min: r.min, end: r.end }; }), "", "谷值 ≤ -1 万升级重点关注");
      } else {
        body = detail(v, "周期性付款", [col("org", "组织"), col("category", "类别"), col("next_date", "下次付款日"), col("amount", "单次金额", "money"), col("gap", "平均间隔(天)", "number"), col("total", "预测流出", "money")], d.periodic || [], "", "工资 / 社保公积金付款单")
          + detail(v, "预计新增 · 应付", [col("party", "往来方"), col("base", "周均基数", "money"), col("trend", "趋势", "percent"), col("expect_total", "预测期望", "money")], d.pipe_ap || [], "", "近 12 周新单趋势外推");
      }
      return '<div class="a-dimview">' + dimHead(id === "risk" ? "风险榜" : id === "breach" ? "组织穿底" : "补充预测", "整屏明细") + body + "</div>";
    }
    var side = id === "ap" ? "ap" : "ar";
    var rows = inOrg(d[side + "_detail"] || [], v).map(function (r) { return Object.assign({}, r, { probability: present(r.prob) ? Number(r.prob) * 100 : null }); });
    return '<div class="a-dimview">' + dimHead(side === "ap" ? "应付未结明细" : "应收未结明细", "期望金额 = 名义 × 概率；概率来源：行为曲线或全局阶梯")
      + detail(v, side === "ap" ? "应付未结明细" : "应收未结明细", [col("org_name", "组织"), col("party", side === "ap" ? "供应商" : "客户"), col("due", "到期日"), col("age_days", "账龄(天)", "number"), col("nominal", "名义未结", "money"), col("probability", "概率", "percent"), col("expect", "期望金额", "money")], rows, "", "已提取 Top50")
      + "</div>";
  }

  function exportTable(v) {
    var t = tables[v]; if (!t) return;
    function cell(x) { var text = String(x == null ? "" : x); if (/^[\s]*[=+@-]/.test(text) && typeof x !== "number") text = "'" + text; return '"' + text.replace(/"/g, '""') + '"'; }
    var csv = [t.columns.map(function (c) { return cell(c.label); }).join(",")].concat(t.rows.map(function (r) { return t.columns.map(function (c) { return cell(r[c.key]); }).join(","); })).join("\r\n");
    var url = URL.createObjectURL(new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8;" })), a = document.createElement("a"); a.href = url; a.download = t.title + ".csv"; a.click(); setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }
  var tooltip;
  function hideTip() { if (tooltip) tooltip.hidden = true; }
  function wire(root, v, rerender, drill) {
    var box = root.querySelector(".analytics"); if (!box) return;
    function change(el) {
      var field = el.dataset.a, value = el.dataset.value == null ? el.value : el.dataset.value, s = state(v);
      hideTip();
      if (field === "org") { if (contexts[v].onOrg) contexts[v].onOrg(value); return; }
      if (field === "drill") { drill(value); return; }
      if (field === "export") { exportTable(v); return; }
      if (field === "reset") { delete states[v]; }
      else if (field === "legend") { var split = value.indexOf(":"), chart = value.slice(0, split), key = value.slice(split + 1), hidden = s.hidden[chart] || []; s.hidden[chart] = hidden.indexOf(key) >= 0 ? hidden.filter(function (x) { return x !== key; }) : hidden.concat(key); }
      else if (field === "sort") { s.desc = s.sort === value ? !s.desc : true; s.sort = value; s.page = 0; }
      else if (field === "page") { s.page = Number(value); }
      else if (field === "cell") { var parts = value.split(":"); s.source = parts[1]; s.party = ""; s.type = ""; s.page = 0; if (contexts[v].onOrg) { contexts[v].onOrg(parts[0]); return; } }
      else if (field === "recon") { s.source = value; s.party = ""; s.page = 0; }
      else if (field === "object") { s.party = s.party === value ? "" : value; s.page = 0; }
      else if (field === "customer") { s.source = "customer"; s.party = s.party === value ? "" : value; s.month = ""; s.page = 0; }
      else if (field === "dim") {
        s.dim = s.dim === value ? "" : value;
        s.page = 0; s.sort = ""; s.party = "";
        // 维度子表与「排行维度 / 收付方向」保持同一口径，避免两处指向不同对象。
        if (v === "sales" && ["customer", "salesperson", "material", "billtype"].indexOf(value) >= 0) s.source = value;
        if (v === "cash" && ["ar", "ap"].indexOf(value) >= 0) s.source = value;
      }
      else {
        var old = s[field]; s[field] = el.tagName === "SELECT" || el.tagName === "INPUT" ? value : (old === value ? "" : value); s.page = 0;
        if (field === "source") { s.party = ""; s.type = ""; s.sort = ""; }
        if (v === "mgmt" && field === "type") { s.source = "subject"; s.party = ""; }
        if (v === "mgmt" && field === "party" && ["arhx", "aphx"].indexOf(s.source) < 0) s.source = "arhx";
        if (v === "sales" && field === "month") s.party = "";
        if (v === "sales" && field === "party") s.month = "";
      }
      var scrollPositions = Array.prototype.map.call(box.querySelectorAll(".a-chart-scroll,.a-matrix-scroll,.a-table-scroll"), function (e) { return { left: e.scrollLeft, top: e.scrollTop }; });
      var focusValue = el.dataset.value, cursor = typeof el.selectionStart === "number" ? el.selectionStart : null;
      rerender();
      Array.prototype.forEach.call(root.querySelectorAll(".a-chart-scroll,.a-matrix-scroll,.a-table-scroll"), function (e, i) { if (scrollPositions[i]) { e.scrollLeft = scrollPositions[i].left; e.scrollTop = scrollPositions[i].top; } });
      var candidates = root.querySelectorAll('[data-a="' + field + '"]'), focus = Array.prototype.find.call(candidates, function (x) { return focusValue == null || x.dataset.value === focusValue; });
      if (focus) { focus.focus({ preventScroll: true }); if (cursor != null && focus.setSelectionRange) focus.setSelectionRange(cursor, cursor); }
    }
    function zoomChart(el, delta, pan, anchor) {
      var id = el.dataset.zoomChart, total = Number(el.dataset.total), z = state(v).zoom[id];
      if (!z || total < 3) return false;
      var count = z.count, start = z.start;
      if (pan) z.start = Math.max(0, Math.min(total - z.count, z.start + (delta > 0 ? 1 : -1)));
      else { z.count = Math.max(2, Math.min(total, z.count + (delta > 0 ? 1 : -1) * Math.max(1, Math.round(z.count * .18)))); z.start = Math.max(0, Math.min(total - z.count, Math.round(start + (count - z.count) * anchor))); }
      if (count === z.count && start === z.start) return false;
      hideTip(); rerender(); return true;
    }
    box.addEventListener("wheel", function (e) {
      var el = e.target.closest("[data-zoom-chart]"); if (!el) return;
      var rect = el.getBoundingClientRect(), delta = e.deltaY || e.deltaX;
      if (zoomChart(el, delta, e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY), Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)))) e.preventDefault();
    }, { passive: false });
    box.addEventListener("keydown", function (e) {
      var el = e.target.closest("[data-zoom-chart]"); if (!el || !["+", "=", "-", "ArrowLeft", "ArrowRight"].includes(e.key)) return;
      if (zoomChart(el, ["-", "ArrowRight"].includes(e.key) ? 1 : -1, e.key.indexOf("Arrow") === 0, .5)) { e.preventDefault(); var next = root.querySelector('[data-zoom-chart="' + el.dataset.zoomChart + '"]'); if (next) next.focus({ preventScroll: true }); }
    });
    box.addEventListener("click", function (e) { var el = e.target.closest("[data-a]"); if (!el || el.tagName === "SELECT" || el.tagName === "INPUT") return; e.preventDefault(); change(el); });
    box.addEventListener("change", function (e) { if (e.target.matches("select[data-a]")) change(e.target); });
    box.addEventListener("keydown", function (e) { var el = e.target.closest('[role="button"][data-a]'); if (el && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); change(el); } });
    box.addEventListener("input", function (e) { if (e.target.dataset.a === "search") change(e.target); });
    function tip(e) {
      var el = e.target.closest("[data-tip]"); if (!el) { hideTip(); return; }
      if (!tooltip) { tooltip = document.createElement("div"); tooltip.className = "a-tooltip"; tooltip.setAttribute("role", "tooltip"); tooltip.id = "analytics-tooltip"; document.body.appendChild(tooltip); }
      tooltip.textContent = el.dataset.tip; tooltip.hidden = false;
      var rect = el.getBoundingClientRect(), px = e.clientX == null ? rect.left + rect.width / 2 : e.clientX, py = e.clientY == null ? rect.bottom : e.clientY;
      tooltip.style.left = Math.max(8, Math.min(window.innerWidth - tooltip.offsetWidth - 12, px + 12)) + "px";
      tooltip.style.top = Math.max(8, Math.min(window.innerHeight - tooltip.offsetHeight - 12, py + 14)) + "px";
      el.setAttribute("aria-describedby", tooltip.id);
    }
    box.addEventListener("pointermove", tip); box.addEventListener("focusin", tip); box.addEventListener("pointerleave", hideTip); box.addEventListener("focusout", hideTip);
  }
  window.PortalAnalytics = {
    render: function (v, d, context) { contexts[v] = context || { orgs: [] }; return ({ expense: expense, mgmt: mgmt, sales: sales, cash: cash })[v](d); },
    wire: wire,
    reset: function () { states = {}; tables = {}; hideTip(); }
  };
})();
