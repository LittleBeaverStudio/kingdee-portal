# -*- coding: utf-8 -*-
"""销售出库看板 · 指标口径（移植自「销售出库分析报告」定时任务）

数据来源
------------------------------------------------------
报表 `SAL_OutStockInvoiceRpt`（销售出库开票跟踪表，GetSysReportData 接口），
字段与导出口径完全一致（FSALEORGNAME/FBILLNO/FBILLTYPENAME/FDate/FSALESNAME/
FCUSTOMERNAME/FMATERIALNAME/FREALQTY/FPrice/FALLAMOUNT/FISFREE/FRECQTY/FRECAMOUNT/
FWriteOffAmount/FINVOECEQTY/FINVOECEAMOUNT/FRECEIPTAMOUNT/FJSWRITEOFFAMOUNT/FChargeOffAmount）。

与定时任务的差异（门户侧有意的微调）：
  · 时间范围默认「当年 1/1 ~ 今天」（定时任务是当月 1 号~今天）—— 看板要有月度趋势；
  · 按组织逐个查报表再合并（与资金头寸同思路：一次传多组织会漏主账户/口径变化）；
  · 纯 Python 聚合，不依赖 pandas（门户运行环境只有标准库 + requests）。

指标口径（与分析器一致，用户确证口径）
------------------------------------------------------
  未结算金额 = 应收金额 − 收款结算金额（按原始差额保留，可能为负数）
  未开票金额 = 应收金额 − 开票金额（下限 0）
  开票率 = 开票金额 / 应收金额；结算率 = 收款结算金额 / 应收金额
  未结算占比 = 未结算金额 / 应收金额
  维度汇总默认按「未结算金额绝对值 + 应收金额」降序（同时识别待结算与超结算/退货）
"""
from __future__ import annotations

import collections
import datetime
import re
import time

REPORT_ID = "SAL_OutStockInvoiceRpt"
REPORT_NAME = "销售出库开票跟踪表"

FIELDS = ("FSALEORGNAME,FBILLNO,FBILLTYPENAME,FDate,FSALESNAME,FCUSTOMERNAME,"
          "FMATERIALNAME,FREALQTY,FPrice,FALLAMOUNT,FISFREE,FRECQTY,FRECAMOUNT,"
          "FWriteOffAmount,FINVOECEQTY,FINVOECEAMOUNT,FRECEIPTAMOUNT,"
          "FJSWRITEOFFAMOUNT,FChargeOffAmount")

# 与 analyzer 的 FIELD_ALIASES 对齐（报表列名即金蝶字段名）
COLS = ["sale_org", "bill_no", "bill_type", "bill_date", "salesperson", "customer",
        "material", "qty", "price", "amount", "is_free", "receivable_qty",
        "receivable_amount", "adjustment_amount", "invoice_qty", "invoice_amount",
        "receipt_settlement_amount", "settlement_adjustment", "special_writeoff"]
NUM_COLS = {"qty", "price", "amount", "receivable_qty", "receivable_amount",
            "adjustment_amount", "invoice_qty", "invoice_amount",
            "receipt_settlement_amount", "settlement_adjustment", "special_writeoff"}
TEXT_COLS = {"sale_org", "bill_no", "bill_type", "salesperson", "customer",
             "material", "is_free"}

ROW_LIMIT = 60000


def f2(v):
    try:
        if isinstance(v, str):
            v = v.replace(",", "").strip()
            if v == "":
                return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def s0(v):
    return "" if v is None else str(v).strip()


def _round(v, nd=2):
    try:
        r = round(float(v or 0), nd)
    except (TypeError, ValueError):
        r = 0.0
    return int(r) if nd == 0 else r


def _safe_div(a, b):
    return (a / b) if b else 0.0


FIELD_MISSING_RE_SALES = re.compile(r"标识为([A-Za-z0-9_.]+)的字段不存在")


def query_safe(client, form_id, fields, filter_string="", order="FID"):
    """带「字段不存在自动剔除」的查询（换账套不改代码）。fields 支持逗号字符串。"""
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",") if f.strip()]
    fields = list(fields)
    for _ in range(len(fields) + 1):
        try:
            return client.query_all(form_id, ",".join(fields), filter_string,
                                    page=5000, max_rows=ROW_LIMIT, order=order)
        except Exception as exc:  # noqa: BLE001
            msg = str(getattr(exc, "message", exc))
            m = FIELD_MISSING_RE_SALES.search(msg)
            if not m or m.group(1) not in fields:
                raise
            fields.remove(m.group(1))
    raise RuntimeError(f"{form_id} 字段全部被剔除")


def _org_fid_map(client, org_numbers):
    """组织编码 → 组织内码（FID）映射字典。返回 (fid, err_reason)。

    ⚠️ 销售出库开票跟踪表的 `FSaleOrgId` 要的是**组织内码字符串**（逗号分隔），
    不是 {"FNumber": ...}（实测报 ResolveFiled_InnerEx 解析异常）。
    ⚠️ `ORG_Organizations` 本账套无查看权限（实测被拒），所以主用
    **AP_Payable 反查**（与导出器 resolve_settle_org_ids_by_numbers 同款）。
    """
    fid, why = {}, []
    for form, fields in (("AP_Payable", "FSETTLEORGID,FSETTLEORGID.FNumber,FSETTLEORGID.FName"),
                         ("ORG_Organizations", "FOrgId,FNumber,FName")):
        try:
            rows = query_safe(client, form, fields, "")
        except Exception as exc:  # noqa: BLE001
            why.append(f"{form}：{str(getattr(exc, 'message', exc))[:80]}")
            continue
        got = 0
        for r in rows:
            if isinstance(r, (list, tuple)) and len(r) >= 2 and s0(r[1]):
                if s0(r[1]) not in fid:
                    fid[s0(r[1])] = s0(r[0])
                    got += 1
        if got:
            return fid, ""
    return fid, "；".join(why) or "两个来源都没有返回组织"


def fetch_rows(client, org_numbers, start_date, end_date, progress=None):
    """查销售出库开票跟踪表（组织内码字符串；实测逐个组织传 FNumber 会解析失败，
    不过滤（空串）则返回 0 行 —— 所以必须解析出 FID 再查）。"""
    p = progress or (lambda *_: None)
    rows, errors = [], []
    fid_map, fid_why = _org_fid_map(client, org_numbers)
    fids = [fid_map[n] for n in (org_numbers or []) if fid_map.get(n)]
    if not fids:
        fids = list(dict.fromkeys(fid_map.values()))   # 未指定组织 = 反查到的全部
    if not fids:
        return [], ["组织内码解析失败（%s）" % fid_why]
    model = {
        "FSaleOrgId": ",".join(fids),
        "FMoneyType": {"FNumber": ""},
        "FStartDate": start_date, "FEndDate": end_date,
        "FCustomerFrom": {"FNumber": ""}, "FCustomerTo": {"FNumber": ""},
        "FSaleDeptFrom": {"FNUMBER": ""}, "FSaleDeptTo": {"FNUMBER": ""},
        "FMaterialFrom": {"FNumber": ""}, "FMaterialTo": {"FNumber": ""},
        "FFormStatus": "C", "FIsIncludeSerMat": "false",
        "FSuite": "", "FSettleOrgList": "",
    }
    try:
        rows = client.sys_report(REPORT_ID, FIELDS, model, scheme_id="")
    except Exception as exc:  # noqa: BLE001
        errors.append(str(getattr(exc, "message", exc))[:160])
        p(f"    销售出库报表查询失败：{errors[-1]}")
    p(f"    销售出库报表：{len(rows)} 行（组织内码 {len(fids)} 个）")
    return rows, errors


def normalize(rows):
    """报表行 → 规范明细（含派生指标）。"""
    out = []
    for r in rows:
        if not isinstance(r, (list, tuple)):
            continue
        rec = {}
        for i, key in enumerate(COLS):
            rec[key] = r[i] if i < len(r) else ("" if key in TEXT_COLS else 0)
        for k in NUM_COLS:
            rec[k] = f2(rec.get(k))
        for k in TEXT_COLS:
            rec[k] = s0(rec.get(k))
        # 金蝶报表日期可能是 "2026/9/1" → 归一成 "2026-09-01"，月份 "2026-09"（保证排序正确）
        raw = s0(rec.get("bill_date"))[:10].replace("/", "-")
        bits = [b for b in raw.split("-") if b != ""]
        if len(bits) >= 3:
            try:
                rec["bill_date"] = "%s-%02d-%02d" % (bits[0], int(bits[1]), int(bits[2]))
                rec["bill_month"] = "%s-%02d" % (bits[0], int(bits[1]))
            except ValueError:
                rec["bill_date"] = raw
                rec["bill_month"] = raw[:7] or "未识别"
        else:
            rec["bill_date"] = raw
            rec["bill_month"] = raw[:7] or "未识别"
        rec["unsettled_amount"] = _round(rec["receivable_amount"] - rec["receipt_settlement_amount"])
        rec["uninvoiced_amount"] = _round(max(0.0, rec["receivable_amount"] - rec["invoice_amount"]))
        rec["unsettled_qty"] = _round(max(0.0, rec["receivable_qty"] - rec["invoice_qty"]))
        rec["invoice_rate"] = _safe_div(rec["invoice_amount"], rec["receivable_amount"])
        rec["settlement_rate"] = _safe_div(rec["receipt_settlement_amount"], rec["receivable_amount"])
        rec["avg_price"] = _safe_div(rec["amount"], rec["qty"])
        out.append(rec)
    return out


def _summary(df):
    receivable = sum(x["receivable_amount"] for x in df)
    invoice = sum(x["invoice_amount"] for x in df)
    settled = sum(x["receipt_settlement_amount"] for x in df)
    unsettled = sum(x["unsettled_amount"] for x in df)
    amount = sum(x["amount"] for x in df)
    qty = sum(x["qty"] for x in df)
    return {
        "line_count": len(df),
        "bill_count": len({x["bill_no"] for x in df if x["bill_no"]}),
        "customer_count": len({x["customer"] for x in df if x["customer"]}),
        "salesperson_count": len({x["salesperson"] for x in df if x["salesperson"]}),
        "material_count": len({x["material"] for x in df if x["material"]}),
        "qty": _round(qty),
        "amount": _round(amount),
        "receivable_qty": _round(sum(x["receivable_qty"] for x in df)),
        "receivable_amount": _round(receivable),
        "invoice_amount": _round(invoice),
        "receipt_settlement_amount": _round(settled),
        "unsettled_amount": _round(unsettled),
        "uninvoiced_amount": _round(sum(x["uninvoiced_amount"] for x in df)),
        "settlement_adjustment": _round(sum(x["settlement_adjustment"] for x in df)),
        "special_writeoff": _round(sum(x["special_writeoff"] for x in df)),
        "invoice_rate": _round(_safe_div(invoice, receivable) * 100, 1),
        "settlement_rate": _round(_safe_div(settled, receivable) * 100, 1),
        "unsettled_ratio": _round(_safe_div(unsettled, receivable) * 100, 1),
        "avg_price": _round(_safe_div(amount, qty)),
    }


def _monthly(df):
    g = collections.defaultdict(lambda: collections.defaultdict(float))
    for x in df:
        m = g[x["bill_month"]]
        for k in ("qty", "amount", "receivable_amount", "invoice_amount",
                  "receipt_settlement_amount", "unsettled_amount"):
            m[k] += x[k]
    return [{"month": m, **{k: _round(v) for k, v in row.items()}}
            for m, row in sorted(g.items())]


def _group(df, key, fallback, top=50):
    """按维度汇总（bill_no 去重计数；按未结算绝对值+应收金额降序）。"""
    g = {}
    for x in df:
        name = x.get(key) or f"未识别{fallback}"
        r = g.setdefault(name, {"bill_count": set(), "line_count": 0,
                                "qty": 0.0, "amount": 0.0, "receivable_amount": 0.0,
                                "invoice_amount": 0.0, "receipt_settlement_amount": 0.0,
                                "unsettled_amount": 0.0, "uninvoiced_amount": 0.0})
        if x.get("bill_no"):
            r["bill_count"].add(x["bill_no"])
        r["line_count"] += 1
        for k in ("qty", "amount", "receivable_amount", "invoice_amount",
                  "receipt_settlement_amount", "unsettled_amount", "uninvoiced_amount"):
            r[k] += x[k]
    rows = []
    for name, r in sorted(g.items(),
                          key=lambda kv: (-abs(kv[1]["unsettled_amount"]),
                                          -kv[1]["receivable_amount"]))[:top]:
        rec = r["receivable_amount"]
        rows.append({
            "name": name, "bill_count": len(r["bill_count"]), "line_count": r["line_count"],
            "qty": _round(r["qty"]), "amount": _round(r["amount"]),
            "receivable_amount": _round(rec), "invoice_amount": _round(r["invoice_amount"]),
            "receipt_settlement_amount": _round(r["receipt_settlement_amount"]),
            "unsettled_amount": _round(r["unsettled_amount"]),
            "uninvoiced_amount": _round(r["uninvoiced_amount"]),
            "invoice_rate": _round(_safe_div(r["invoice_amount"], rec) * 100, 1),
            "settlement_rate": _round(_safe_div(r["receipt_settlement_amount"], rec) * 100, 1),
            "unsettled_ratio": _round(_safe_div(r["unsettled_amount"], rec) * 100, 1),
        })
    return rows


def _rank_names(df, key, fallback):
    """维度名称排序：未结算金额绝对值 + 应收金额 降序（与 _group 完全一致）。

    ⚠️ 两处必须同序：客户/销售员/物料排行的顺序，和「销售与回款轨迹」里
    高亮的对象是同一批，否则排行面板与轨迹图会指向不同对象。
    """
    agg = collections.defaultdict(lambda: [0.0, 0.0])
    for x in df:
        name = x.get(key) or f"未识别{fallback}"
        agg[name][0] += x["unsettled_amount"]
        agg[name][1] += x["receivable_amount"]
    return sorted(agg, key=lambda n: (-abs(agg[n][0]), -agg[n][1]))


def _dimension_trend(df, top=7):
    """按维度（客户 / 销售员 / 物料 / 单据类型）取排行靠前对象的月度轨迹。

    扁平行结构：{dim, name, month, amount, receipt_settlement_amount, unsettled_amount}。
    「销售与回款轨迹」的排行维度直接复用这份数据：与客户贡献排行同一批对象、
    同一套顺序，既能出排行条形图，也能出该对象的月度走势，不必再取数。
    """
    specs = (("customer", "customer", "客户"), ("salesperson", "salesperson", "销售员"),
             ("material", "material", "物料"), ("bill_type", "bill_type", "单据类型"))
    rows = []
    for dim, key, label in specs:
        keep = set(_rank_names(df, key, label)[:top])
        agg = collections.defaultdict(lambda: collections.defaultdict(
            lambda: {"amount": 0.0, "receipt_settlement_amount": 0.0, "unsettled_amount": 0.0}))
        for x in df:
            name = x.get(key) or f"未识别{label}"
            if name not in keep:
                continue
            cell = agg[name][x["bill_month"]]
            cell["amount"] += x["amount"]
            cell["receipt_settlement_amount"] += x["receipt_settlement_amount"]
            cell["unsettled_amount"] += x["unsettled_amount"]
        for name, months in agg.items():
            for month, cell in sorted(months.items()):
                rows.append({"dim": dim, "name": name, "month": month,
                             **{k: _round(v) for k, v in cell.items()}})
    return rows


def _unsettled_bills(df, top=100):
    """按单据汇总的未结清单（未结算金额绝对值降序）。

    ⚠️ 只保留「确实还有未结」的单据：未结算金额为 0 表示该单已全额收付，
    它不属于未结清单（0 值行会让 Top100 里出现一批无意义的空行）。
    负值（超结算 / 退货）保留，仍是需要跟进的异常信号。
    """
    g = {}
    for x in df:
        no = x.get("bill_no") or "未识别单据"
        r = g.setdefault(no, {"bill_date": x["bill_date"], "bill_type": x["bill_type"],
                              "salesperson": x["salesperson"], "customer": x["customer"],
                              "line_count": 0, "qty": 0.0, "amount": 0.0,
                              "receivable_amount": 0.0, "invoice_amount": 0.0,
                              "receipt_settlement_amount": 0.0, "unsettled_amount": 0.0,
                              "uninvoiced_amount": 0.0})
        r["line_count"] += 1
        for k in ("qty", "amount", "receivable_amount", "invoice_amount",
                  "receipt_settlement_amount", "unsettled_amount", "uninvoiced_amount"):
            r[k] += x[k]
        if x["bill_date"] and (not r["bill_date"] or x["bill_date"] < r["bill_date"]):
            r["bill_date"] = x["bill_date"]
    rows = []
    for no, r in sorted(g.items(),
                        key=lambda kv: (-abs(kv[1]["unsettled_amount"]),
                                        -kv[1]["receivable_amount"])):
        if abs(r["unsettled_amount"]) < 0.01:
            continue                                   # 已全额结清，不是未结单据
        rec = r["receivable_amount"]
        rows.append({
            "bill_no": no, "bill_date": r["bill_date"], "bill_type": r["bill_type"],
            "salesperson": r["salesperson"], "customer": r["customer"],
            "line_count": r["line_count"], "qty": _round(r["qty"]),
            "amount": _round(r["amount"]), "receivable_amount": _round(rec),
            "invoice_amount": _round(r["invoice_amount"]),
            "receipt_settlement_amount": _round(r["receipt_settlement_amount"]),
            "unsettled_amount": _round(r["unsettled_amount"]),
            "uninvoiced_amount": _round(r["uninvoiced_amount"]),
            "settlement_rate": _round(_safe_div(r["receipt_settlement_amount"], rec), 4),
        })
        if len(rows) >= top:
            break
    return rows


def _periods(df, today, ly_same_summary):
    """本月 / 上月 / 年初至今 + 环比 + 同比（去年同月，来自补充小查询）。

    ⚠️ 为什么不一并查去年全年：2026-10-17 实测，把范围放到"去年 1/1 至今"
    会让金蝶报表服务端计算量翻几倍（2.6 万行 × 19 列），单次取数 >10 分钟。
    改成「当年主查询 + 去年同月小查询」后行数约 1/4，秒级返回。
    """

    def month_range(y, m):
        first = datetime.date(y, m, 1)
        last = (datetime.date(y, 12, 31) if m == 12
                else datetime.date(y, m + 1, 1) - datetime.timedelta(days=1))
        return first.isoformat(), last.isoformat()

    def sub(s, e):
        rows = [x for x in df if s <= (x.get("bill_date") or "") <= e]
        return _summary(rows)

    prev_y, prev_m = ((today.year, today.month - 1) if today.month > 1
                      else (today.year - 1, 12))
    this_m = (month_range(today.year, today.month)[0], today.isoformat())
    prev_m = month_range(prev_y, prev_m)
    ly_same = month_range(today.year - 1, today.month)
    ytd = (f"{today.year}-01-01", today.isoformat())

    def grow(a, b):
        return round((a / b - 1) * 100, 1) if b else None

    ly_same_amt = (ly_same_summary or {}).get("amount") or 0
    out = {
        "this_month": {"label": f"{today.month} 月", "range": this_m, **sub(*this_m)},
        "prev_month": {"label": f"{prev_m} 月", "range": prev_m, **sub(*prev_m)},
        "ly_same_month": {"label": f"去年 {today.month} 月", "range": ly_same,
                          **(ly_same_summary or {})},
        "ytd": {"label": f"{today.year} 年累计", "range": ytd, **sub(*ytd)},
    }
    out["this_month"]["mom_amount"] = grow(out["this_month"]["amount"],
                                           out["prev_month"]["amount"])
    out["this_month"]["yoy_amount"] = grow(out["this_month"]["amount"], ly_same_amt)
    out["ytd"]["yoy_amount"] = None            # 去年累计不再取（太慢），不显示同比
    out["ytd"]["base_amount"] = ly_same_amt     # 对比条参照：去年同月

    # 客户集中度（按出库金额）
    by_cust = collections.defaultdict(float)
    for x in df:
        if x.get("customer"):
            by_cust[x["customer"]] += x["amount"]
    total_amt = sum(by_cust.values())
    top = sorted(by_cust.items(), key=lambda kv: -kv[1])[:8]
    out["top_customers"] = [{"name": k, "amount": _round(v),
                             "share": _round(v / total_amt * 100, 1) if total_amt else 0}
                            for k, v in top]
    top5 = sum(v for _, v in sorted(by_cust.items(), key=lambda kv: -kv[1])[:5])
    out["top5_share"] = _round(top5 / total_amt * 100, 1) if total_amt else 0
    out["top1_share"] = _round(top[0][1] / total_amt * 100, 1) if top and total_amt else 0
    return out


def build(client, org_numbers=None, start_date=None, end_date=None, progress=None, **_ignored):
    """org_numbers 为空 = 全部有权组织。

    取数范围：**当年 1 月 1 日 ~ 今天**（主查询，覆盖本月/上月/累计/明细/趋势）
    + **去年同月**（补充小查询，只用于本月同比）。
    """
    p = progress or (lambda *_: None)
    today = datetime.date.today()
    start_date = start_date or f"{today.year}-01-01"
    end_date = end_date or today.isoformat()

    p("①/③ 拉取销售出库开票跟踪表（当年）")
    rows, errors = fetch_rows(client, org_numbers, start_date, end_date, progress=p)
    p(f"②/③ 当年共 {len(rows)} 行，开始聚合")
    df = normalize(rows)
    if len(df) >= ROW_LIMIT:
        errors.append(f"返回行数达到单次上限 {ROW_LIMIT}，可能存在遗漏，建议缩短时间范围")

    # 去年同月（小查询，一个月）
    ly = datetime.date(today.year - 1, today.month, 1)
    ly_end = (datetime.date(today.year - 1, 12, 31) if today.month == 12
              else datetime.date(today.year - 1, today.month + 1, 1)
              - datetime.timedelta(days=1))
    p("③/③ 补充拉取去年同月（同比用）")
    ly_rows, ly_err = fetch_rows(client, org_numbers, ly.isoformat(), ly_end.isoformat(),
                                 progress=p)
    errors.extend(ly_err)
    ly_summary = _summary(normalize(ly_rows)) if ly_rows else {"amount": 0}

    orgs_in_data = {}
    for x in df:
        if x["sale_org"]:
            orgs_in_data.setdefault(x["sale_org"], x["sale_org"])

    periods = _periods(df, today, ly_summary)

    result = {
        "report": REPORT_NAME,
        "period": {"start": start_date, "end": end_date,
                   "ly_same": [ly.isoformat(), ly_end.isoformat()]},
        "summary": _summary(df),
        "periods": periods,
        "monthly_trend": _monthly(df),
        "bill_type_summary": _group(df, "bill_type", "单据类型"),
        "salesperson_summary": _group(df, "salesperson", "销售员"),
        "material_summary": _group(df, "material", "物料"),
        "customer_summary": _group(df, "customer", "客户"),
        "dimension_trend": _dimension_trend(df),
        "unsettled_bills": _unsettled_bills(df),
        "orgs_in_data": sorted(orgs_in_data),
        "method_notes": [
            "本月 = 本月 1 日 ~ 今天；累计 = 本年 1 月 1 日 ~ 今天（YTD）。",
            "同比 = 本月 vs 去年同月（补充查询去年同月）；环比 = 本月 vs 上月。",
            "累计不显示同比：去年全年行数过大会拖慢取数（实测跨年查询 >10 分钟），"
            "仅保留去年同月做本月同比。",
            "未结算金额 = 应收金额 − 收款结算金额；按原始差额保留，可能为负数（超结算/退货）。",
            "未开票金额 = 应收金额 − 开票金额（下限 0）；用于观察出库后开票推进。",
            "开票率 = 开票金额/应收金额；结算率 = 收款结算金额/应收金额；未结算占比 = 未结算/应收。",
            "客户集中度 = 该客户出库金额 / 全部出库金额；Top5 占比越高，大客户依赖越强。",
            "「销售与回款轨迹」的排行维度复用同一批明细：客户 / 销售员 / 物料 / 单据类型的排行顺序"
            "与各自贡献排行一致，并附该对象逐月的出库、已结算与未结算。",
            "未结清单只列仍有未结算金额的单据；已全额结清的单据不再出现（负值超结算/退货保留为异常信号）。",
        ],
        "coverage": {
            "rows_fetched": len(rows),
            "rows_normalized": len(df),
            "errors": errors,
        },
    }
    # Keep exact aggregates before TopN truncation for local organization crossfilter.
    previous = normalize(ly_rows)
    result["by_org"] = {}
    for org in sorted(set(orgs_in_data) | {x["sale_org"] for x in previous}):
        subset = [x for x in df if x["sale_org"] == org]
        last = [x for x in previous if x["sale_org"] == org]
        result["by_org"][org] = {
            "summary": _summary(subset), "periods": _periods(subset, today, _summary(last)),
            "monthly_trend": _monthly(subset),
            "bill_type_summary": _group(subset, "bill_type", "单据类型", top=len(subset)),
            "salesperson_summary": _group(subset, "salesperson", "销售员", top=len(subset)),
            "material_summary": _group(subset, "material", "物料", top=len(subset)),
            "customer_summary": _group(subset, "customer", "客户", top=len(subset)),
            "dimension_trend": _dimension_trend(subset),
            "unsettled_bills": _unsettled_bills(subset), "orgs_in_data": [org],
        }
    return result
