# -*- coding: utf-8 -*-
"""管理会计看板 · 全貌版（移植自「管理会计仪表盘日报」的卡片导航 + 明细分页模式）

主页 = 指标卡片导航；点击卡片进对应明细分页（前端实现）。
本模块负责**服务端聚合**：每个数据源独立采集、独立降级（失败不拖垮其他源，
与定时任务"单个失败不影响后续"一致），失败原因进 errors，不静默出 0。

数据源（13 个；「未生成凭证 / 组织间往来对账 / 四路核对」三个源依赖外部脚本或
本地核对工作簿，门户侧不做，主页不展示，避免摆一个永远空着的卡片）：
  bank     银行头寸 + 头寸与科目余额核对   CN_FundPositionReport × GL_RPT_AccountBalance
  subject  科目余额看板（波动 Top）         GL_RPT_AccountBalance
  est      应付 vs 暂估差异                 AP_Payable × 科目余额表（暂估应付款核算维度）
  interdim 往来科目核算维度明细             GL_RPT_AccountBalance（7 类往来科目）
  arhx     应收核销（应收单 vs 收款单）     AR_receivable × AR_RECEIVEBILL
  aphx     应付核销（应付单 vs 付款单）     AP_Payable × AP_PAYBILL
  goods    有货未回票（采购订单执行）       PUR_PurchaseOrderDetailRpt
  expense  非货未回票（费用/手工应付单）    AP_Payable（按单据类型自适应）
  dash     带横杠单号                       AR_receivable / AP_Payable
  recv     收票单汇总                       IV_ReceivedInvoice
  anom     存货异常（三类）                 HS_INOUTSTOCKSUMMARYRPT
  invoice  未审核发票                       IV 四类进项发票表单

bank/subject 复用 metrics_mgmt.build()（已验证口径），其余源在本文件实现。
"""
from __future__ import annotations

import collections
import datetime
import os
import re
import financial_reports

import metrics_mgmt

FIELD_MISSING_RE = re.compile(r"标识为([A-Za-z0-9_.]+)的字段不存在")
ROW_LIMIT = 20000

# 未审核发票：四类进项发票表单
IV_FORMS = ("IV_PURCHASEOC", "IV_PURCHASEIC", "IV_PUREXPINV", "IV_PUREXVATIN")

# 往来科目分类（与 inter_dimension.py 一致）
CAT_ORDER = ("1122", "1123", "1221", "2203", "2202ZG", "2202MX", "2241")
CAT_NAMES = {"1122": "应收账款", "1123": "预付账款", "1221": "其他应收款",
             "2203": "预收账款", "2202ZG": "应付暂估", "2202MX": "应付明细",
             "2241": "其他应付款"}


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


def _r(v, nd=2):
    try:
        x = round(float(v or 0), nd)
    except (TypeError, ValueError):
        x = 0.0
    return int(x) if nd == 0 else x


def _cat_of(code, name=""):
    c = s0(code)
    n = s0(name)
    if c.startswith("1122"):
        return "1122"
    if c.startswith("1123"):
        return "1123"
    if c.startswith("1221"):
        return "1221"
    if c.startswith("2203") or c.startswith("2001"):
        return "2203"
    if c.startswith("2202.01") or (c.startswith("2202") and "暂估" in n):
        return "2202ZG"
    if c.startswith("2202.02") or (c.startswith("2202") and "明细" in n):
        return "2202MX"
    if c.startswith("2241"):
        return "2241"
    return None


def query_safe(client, form_id, fields, filter_string="", order="FID"):
    """带「字段不存在自动剔除」的查询（换账套不改代码）。

    fields 可以是逗号字符串或列表。
    """
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",") if f.strip()]
    fields = list(fields)
    for _ in range(len(fields) + 1):
        try:
            return client.query_all(form_id, ",".join(fields), filter_string,
                                    page=5000, max_rows=ROW_LIMIT, order=order), fields, []
        except Exception as exc:  # noqa: BLE001
            msg = str(getattr(exc, "message", exc))
            m = FIELD_MISSING_RE.search(msg)
            if not m or m.group(1) not in fields:
                raise
            fields.remove(m.group(1))
    raise RuntimeError(f"{form_id} 字段全部被剔除")


def _org_filter(field, nums):
    if not nums:
        return ""
    return "(" + " OR ".join(f"{field}='{n}'" for n in nums) + ")"


# ---------------------------------------------------------------- 各数据源
def src_bank(client, orgs, org_numbers, year, period, scheme_id, progress):
    """银行头寸 + 科目余额 + 波动 Top + 交叉验证（复用已验证口径）。"""
    d = metrics_mgmt.build(client, orgs=orgs, year=year, period=period,
                           scheme_id=scheme_id, progress=progress,
                           org_filter=org_numbers)
    fund = d.get("fund") or {}
    subj = d.get("subject") or {}
    cross = d.get("cross_check") or {}
    diff_orgs = [x for x in (cross.get("both") or []) if abs(f2(x.get("delta"))) > 0.5]
    diff_sum = sum(abs(f2(x.get("delta"))) for x in diff_orgs)
    return {
        "fund": fund, "subject": subj, "swings": d.get("swings") or [],
        "cross_check": cross, "notes": d.get("notes") or [],
        "errors": d.get("notes") or [],
        "working_capital": d.get("working_capital") or [],
        "cards": [
            # 用户要求（2026-10-17）：这一页**只保留差异统计**，头寸余额归现金流看板
            {"value": len(diff_orgs), "unit": "个",
             "sub": f"头寸 vs 科目余额有差异的组织（>0.5 元）"},
            {"value": _r(diff_sum), "unit": "元", "sub": "差额绝对值合计"},
        ],
    }


def _organization_book(client, no, books=None):
    """A GL report requires an explicit book; an unscoped query cannot be relabeled per org."""
    if books is None:
        books = client.discover_account_books()
    matches = [b for b in books if str(b.get("org_number") or "") == str(no)]
    if len(matches) != 1:
        raise ValueError(f"组织 {no} 未取得唯一账簿，暂不计算科目对照")
    return matches[0].get("book_number") or matches[0].get("number")


def src_est(client, org_numbers, today, scheme_id, progress, books=None):
    """应付 vs 暂估：AP 未开票核销（不含税）vs 科目余额表暂估应付款。"""
    cum_start = (today - datetime.timedelta(days=365 * 3)).isoformat()
    month_start = f"{today.year:04d}-{today.month:02d}-01"
    ap_fields = ("FDATE,FSETTLEORGID.FNumber,FSETTLEORGID.FName,FSUPPLIERID.FName,"
                 "FNOINVOICEAMOUNT,FTAXAMOUNT_D,FALLAMOUNTFOR_D")
    filt = _org_filter("FSETTLEORGID.FNumber", org_numbers)
    conds = ["FDOCUMENTSTATUS='C'", "FCancelStatus='A'", f"FDATE>='{cum_start}'"]
    if filt:
        conds.append(filt)
    rows, _, _ = query_safe(client, "AP_Payable", ap_fields, " AND ".join(conds))
    agg = collections.defaultdict(lambda: {"cum": 0.0, "cur": 0.0})
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 7:
            continue
        noinv = f2(r[4])
        if abs(noinv) < 0.01:
            continue
        tax, total = f2(r[5]), f2(r[6])
        tax_part = tax * noinv / total if total else 0.0
        notax = noinv - tax_part
        key = (s0(r[1]), s0(r[3]) or "未知供应商")   # ⚠️ 必须是二元组：与科目余额侧 (组织, 供应商) 对齐
        agg[key]["cum"] += notax
        if s0(r[0])[:10] >= month_start:
            agg[key]["cur"] += notax
    # 科目余额表：暂估应付款核算维度（供应商）明细
    bal_fields = ("FBALANCEID,FBALANCENAME,FDETAILNUMBER,FDETAILNAME,"
                  "FBEGINDEBITLOCAL,FBEGINCREDITLOCAL,FDEBITLOCAL,FCREDITLOCAL,"
                  "FYTDDEBITLOCAL,FYTDCREDITLOCAL,FENDDEBITLOCAL,FENDCREDITLOCAL")
    org_names = {n: "" for n in org_numbers}
    errors, failed = [], set()
    bal_agg = collections.defaultdict(lambda: {"cum": 0.0, "cur": 0.0})
    org_rows = collections.defaultdict(lambda: {"notax": 0.0, "est": 0.0, "n": set()})
    for no in (org_numbers or [""]):
        model = metrics_mgmt._bal_model_local(no, today.year, today.month) \
            if hasattr(metrics_mgmt, "_bal_model_local") else None
        if model is None:                    # 复用 build 内的 model 构造
            model = {
                "FSTARTYEAR": str(today.year), "FSTARTPERIOD": "1",
                "FENDYEAR": str(today.year), "FENDPERIOD": str(today.month),
                "FBALANCELEVEL": "3", "FSHOWDETAIL": True, "FFORBIDBALANCE": True,
                "FNOTPOSTVOUCHER": True, "FDEBITORCREDIT": False, "FBALANCEZERO": True,
                "FNOBUSINESS": False, "FPERIODNOBALANCE": True, "FYEARNOBALANCE": True,
                "FSHOWFULLNAME": True, "FDETAILSHOWACCT": True, "FSHOWDETAILONLY": False,
                "FEXCLUDEADJUSTVCH": False, "FFLEXDEBITORCREDIT": False,
                "FSHOWFLEXBYCOL": False,
            }
        try:
            model["FACCTBOOKID"] = {"FNumber": _organization_book(client, no, books)}
            model["FCURRENCY"] = "0"
            brows = client.sys_report("GL_RPT_AccountBalance", bal_fields, model,
                                      scheme_id=scheme_id)
        except Exception as exc:  # noqa: BLE001
            failed.add(no)
            errors.append(f"组织 {no}：{str(getattr(exc, 'message', exc))[:100]}")
            progress(f"    暂估差异：组织 {no or '全部'} 科目余额取数失败"
                     f"（{str(getattr(exc, 'message', exc))[:60]}）")
            continue
        for r in brows:
            if not isinstance(r, (list, tuple)) or len(r) < 12:
                continue
            code, name = s0(r[1]), s0(r[1])
            if "暂估应付款" not in name:
                continue
            dim = s0(r[3])
            if not dim:
                continue
            end_credit, end_debit = f2(r[11]), f2(r[10])
            ytd_credit, ytd_debit = f2(r[9]), f2(r[8])
            cum = end_credit - end_debit
            cur = ytd_credit - ytd_debit
            bal_agg[(no, dim)]["cum"] += cum
            bal_agg[(no, dim)]["cur"] += cur
            org_rows[no]["est"] += cum
            org_rows[no]["n"].add(dim)
    # 两侧并集对齐
    keys = set(agg) | set(bal_agg)
    detail = []
    for (org, sup) in sorted(keys):
        a = agg.get((org, sup), {"cum": 0.0, "cur": 0.0})
        b = bal_agg.get((org, sup), {"cum": 0.0, "cur": 0.0})
        d_cum = a["cum"] - b["cum"]
        d_cur = a["cur"] - b["cur"]
        org_rows[org]["notax"] += a["cum"]
        if abs(d_cum) < 0.01 and abs(d_cur) < 0.01:
            continue
        detail.append({"org": org, "supplier": sup,
                       "notax": _r(a["cum"]), "est_cum": (_r(b["cum"]) if org not in failed else None),
                       "diff_cum": (_r(d_cum) if org not in failed else None),
                       "diff_cur": (_r(d_cur) if org not in failed else None)})
    detail.sort(key=lambda x: -abs(x["diff_cum"] or 0))
    org_summary = [{"org": o, "suppliers": len(v["n"]), "notax": _r(v["notax"]),
                    "est": (_r(v["est"]) if o not in failed else None),
                    "diff": (_r(v["notax"] - v["est"]) if o not in failed else None)}
                   for o, v in sorted(org_rows.items())]
    cum_total = sum(x["diff_cum"] or 0 for x in detail)
    return {
        "org_summary": org_summary, "detail": detail[:200], "errors": errors,
        "cards": [
            {"value": (_r(cum_total) if not errors else None), "unit": "元",
             "sub": f"累计差额合计 · {len(detail)} 家供应商有差异"},
            {"value": len(org_summary), "unit": "个", "sub": "覆盖组织"},
        ],
        "note": "对应税额 = 税额 × 未开票核销 ÷ 价税合计；不含税 = 未开票核销 − 对应税额；"
                "累计差额 = 累计不含税 −（暂估应付款期末贷 − 借）；"
                "科目余额表 FNOTPOSTVOUCHER=True（含未过账凭证），与金蝶界面默认口径不同属正常。",
    }


def src_interdim(client, org_numbers, today, scheme_id, progress):
    """往来科目核算维度明细（7 类往来科目，仅列跨 ≥2 科目有余额的维度）。"""
    bal_fields = ("FBALANCEID,FBALANCENAME,FDETAILNUMBER,FDETAILNAME,"
                  "FBEGINDEBITLOCAL,FBEGINCREDITLOCAL,FDEBITLOCAL,FCREDITLOCAL,"
                  "FYTDDEBITLOCAL,FYTDCREDITLOCAL,FENDDEBITLOCAL,FENDCREDITLOCAL")
    per_org = collections.defaultdict(lambda: collections.defaultdict(
        lambda: collections.defaultdict(float)))
    errors = []
    books = client.discover_account_books()
    for no in (org_numbers or [""]):
        model = {
            "FSTARTYEAR": str(today.year), "FSTARTPERIOD": "1",
            "FENDYEAR": str(today.year), "FENDPERIOD": str(today.month),
            "FBALANCELEVEL": "3", "FSHOWDETAIL": True, "FFORBIDBALANCE": True,
            "FNOTPOSTVOUCHER": True, "FDEBITORCREDIT": False, "FBALANCEZERO": True,
            "FNOBUSINESS": False, "FPERIODNOBALANCE": True, "FYEARNOBALANCE": True,
            "FSHOWFULLNAME": True, "FDETAILSHOWACCT": True, "FSHOWDETAILONLY": False,
            "FEXCLUDEADJUSTVCH": False, "FFLEXDEBITORCREDIT": False,
            "FSHOWFLEXBYCOL": False,
        }
        try:
            model["FACCTBOOKID"] = {"FNumber": _organization_book(client, no, books)}
            model["FCURRENCY"] = "0"
            rows = client.sys_report("GL_RPT_AccountBalance", bal_fields, model,
                                     scheme_id=scheme_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"组织 {no}：{str(getattr(exc, 'message', exc))[:100]}")
            progress(f"    核算维度：组织 {no or '全部'} 取数失败"
                     f"（{str(getattr(exc, 'message', exc))[:60]}）")
            continue
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 12:
                continue
            cat = _cat_of(r[0], r[1])
            if not cat:
                continue
            dim = s0(r[3])
            if not dim:
                continue
            net = f2(r[10]) - f2(r[11])     # 期末借 − 期末贷
            per_org[no][dim][cat] += net
    out_orgs = []
    n_cross = 0
    for no, dims in per_org.items():
        rows_out = []
        for dim, cats in sorted(dims.items()):
            live = {c: v for c, v in cats.items() if abs(v) > 0.01}
            if len(live) < 2:
                continue
            n_cross += 1
            rows_out.append({"dim": dim, "cats": live,
                             "net": _r(sum(live.values()))})
        rows_out.sort(key=lambda x: -abs(x["net"]))
        summary = []
        for c in CAT_ORDER:
            tot = sum(cats.get(c, 0.0) for cats in dims.values())
            if abs(tot) > 0.01:
                summary.append({"cat": c, "name": CAT_NAMES[c], "net": _r(tot)})
        out_orgs.append({"org": no, "summary": summary,
                         "rows": rows_out[:200]})
    return {
        "orgs": out_orgs, "errors": errors,
        "cards": [
            {"value": (n_cross if not errors else None), "unit": "个",
             "sub": f"跨 ≥2 个往来科目有余额的核算维度 · {len(out_orgs)} 组织"},
        ],
        "note": "科目：1122 应收 / 1123 预付 / 1221 其他应收 / 2203·2001 预收 / "
                "2202.01 应付暂估 / 2202.02 应付明细 / 2241 其他应付；"
                "净额 = 期末借 − 期末贷；含未过账凭证。",
    }


def _recon_side(client, bill_form, bill_fields, pay_form, pay_fields,
                org_numbers, progress, label):
    """核销对账：单据未结算 vs 收/付款单未核销，按 (组织, 单位) 对齐。"""
    filt = _org_filter("FSETTLEORGID.FNumber", org_numbers)
    conds = ["FDOCUMENTSTATUS='C'", "FCancelStatus='A'"]
    if filt:
        conds.append(filt)
    brows, bf, bdrop = query_safe(client, bill_form, bill_fields, " AND ".join(conds))
    try:
        prows, pf, pdrop = query_safe(client, pay_form, pay_fields, " AND ".join(conds))
    except Exception as exc:  # noqa: BLE001
        prows, pf, pdrop = [], [], [f"{pay_form} 查询失败：{str(exc)[:80]}"]
    bill_agg = collections.defaultdict(float)
    for r in brows:
        if not isinstance(r, (list, tuple)) or len(r) < 4:
            continue
        v = f2(r[3])
        if abs(v) > 0.01:
            bill_agg[(s0(r[1]), s0(r[2]) or "未知")] += v
    pay_agg = collections.defaultdict(float)
    for r in prows:
        if not isinstance(r, (list, tuple)) or len(r) < 4:
            continue
        v = f2(r[3])
        if abs(v) > 0.01:
            pay_agg[(s0(r[1]), s0(r[2]) or "未知")] += v
    keys = set(bill_agg) | set(pay_agg)
    rows = []
    for k in sorted(keys):
        a, b = bill_agg.get(k, 0.0), pay_agg.get(k, 0.0)
        if abs(a) < 0.01 and abs(b) < 0.01:
            continue
        rows.append({"org": k[0], "party": k[1], "bill": _r(a), "pay": (_r(b) if pf else None),
                     "diff": (_r(a - b) if pf else None)})
    rows.sort(key=lambda x: -abs(x["diff"] or 0))
    # 正负并存：同 (组织,单位) 单据里同时存在正负金额
    pn = collections.defaultdict(lambda: {"pos": 0.0, "neg": 0.0})
    for r in brows:
        if not isinstance(r, (list, tuple)) or len(r) < 4:
            continue
        v = f2(r[3])
        cell = pn[(s0(r[1]), s0(r[2]) or "未知")]
        if v > 0:
            cell["pos"] += v
        elif v < 0:
            cell["neg"] += v
    pn_rows = [{"org": k[0], "party": k[1], "pos": _r(v["pos"]), "neg": _r(v["neg"])}
               for k, v in pn.items() if v["pos"] > 0.01 and v["neg"] < -0.01]
    pn_rows.sort(key=lambda x: -abs(x["pos"] + x["neg"]))
    progress(f"    {label}：单据未结 {len(bill_agg)} 家 / 收付未核 {len(pay_agg)} 家")
    return {
        "recon": rows[:200], "posneg": pn_rows[:100],
        "cards": [
            {"value": (len([x for x in rows if x["diff"] is not None and abs(x["diff"]) > 0.5]) if pf else None), "unit": "项",
             "sub": f"{label}：未结算 vs 未核销有差异（>0.5 元）"},
            {"value": len(pn_rows), "unit": "项", "sub": "正负并存"},
        ],
        "dropped": bdrop + pdrop,
    }


def src_arhx(client, org_numbers, progress):
    return _recon_side(
        client, "AR_receivable",
        "FID,FBillNo,FSETTLEORGID.FNumber,FCUSTOMERID.FName,FNORECEIVEAMOUNT",
        "AR_RECEIVEBILL",
        "FID,FBillNo,FSETTLEORGID.FNumber,FCUSTOMERID.FName,FUNVERIFIEDAMOUNT",
        org_numbers, progress, "应收核销")


def src_aphx(client, org_numbers, progress):
    return _recon_side(
        client, "AP_Payable",
        "FID,FBillNo,FSETTLEORGID.FNumber,FSUPPLIERID.FName,FNORECEIVEAMOUNT",
        "AP_PAYBILL",
        "FID,FBillNo,FSETTLEORGID.FNumber,FSUPPLIERID.FName,FUNVERIFIEDAMOUNT",
        org_numbers, progress, "应付核销")


def src_goods(client, org_numbers, start, end, progress):
    """有货未回票：采购订单执行明细（差额 = 已结算 − 开票）。"""
    fields = ("FPurchaseOrgId,FBillNo,FDate,FSUPPLIERNAME,FMATERIALNAME,"
              "FOrderQty,FOrderAmount,FReceiveQty,FReceiveAmount,FImportQty,FImportAmount,"
              "FRETURNQTY,FRETURNAMOUNT,FPAYQTY,FPAYAMOUNT,FPREINVOICEQTY,FPREINVOICEAMOUNT,"
              "FINVOICEQTY,FINVOICEAMOUNT,FRECPAYBILLAMOUNT,FPAYBILLAMOUNT,"
              "FSETADJAMOUNT,FPAYWRITOFFAMOUNT")
    rows_all, errs = [], []
    for no in (org_numbers or [""]):
        model = {
            "FPurchaseOrgIdList": [{"FNumber": no}] if no else "",
            "FOrderStartDate": start, "FOrderEndDate": end,
            "FBeginSupplierId": {"FNumber": ""}, "FEndSupplierId": {"FNumber": ""},
            "FBeginBillNumber": "", "FEndBillNumber": "",
            "FBeginMaterialId": {"FNumber": ""}, "FEndMaterialId": {"FNumber": ""},
            "FBeginPurchaser": {"FNumber": ""}, "FEndFPurchaser": {"FNumber": ""},
            "FBusinessType": "", "FFormStatus": "C",
        }
        try:
            got = client.sys_report("PUR_PurchaseOrderDetailRpt", fields, model,
                                    scheme_id="")
            rows_all.extend(got)
        except Exception as exc:  # noqa: BLE001
            errs.append(f"组织 {no or '全部'}：{str(getattr(exc, 'message', exc))[:80]}")
        import time as _t
        _t.sleep(0.05)
    detail = []
    for r in rows_all:
        if not isinstance(r, (list, tuple)) or len(r) < 20:
            continue
        recv, inv = f2(r[9]), f2(r[18])     # 入库金额 / 开票金额
        settled = f2(r[19])                 # 已结算（收付款单金额）
        diff = settled - inv
        if abs(diff) < 0.5:
            continue
        detail.append({"org": s0(r[0]), "bill_no": s0(r[1]), "date": s0(r[2])[:10],
                       "supplier": s0(r[3]), "material": s0(r[4]),
                       "receive": _r(recv), "settled": _r(settled),
                       "invoice": _r(inv), "diff": _r(diff)})
    detail.sort(key=lambda x: -abs(x["diff"]))
    by_sup = collections.defaultdict(lambda: {"n": 0, "diff": 0.0})
    for x in detail:
        by_sup[x["supplier"] or "未知"]["n"] += 1
        by_sup[x["supplier"] or "未知"]["diff"] += x["diff"]
    sup_rows = sorted(({"supplier": k, "n": v["n"], "diff": _r(v["diff"])}
                       for k, v in by_sup.items()), key=lambda x: -abs(x["diff"]))
    return {
        "detail": detail[:200], "by_supplier": sup_rows[:50], "errors": errs,
        "cards": [
            {"value": _r(sum(x["diff"] for x in detail)), "unit": "元",
             "sub": f"已结算 − 开票差额 · {len(detail)} 行订单"},
        ],
        "note": "差额 = 已结算金额 − 开票金额；已结算取收付款单金额口径。",
    }


def src_expense(client, org_numbers, start, progress):
    """非货未回票：费用应付单 / 手工标准应付单（单据类型自适应，不写死）。"""
    fields = ("FID,FBillNo,FDATE,FSETTLEORGID.FNumber,FSETTLEORGID.FName,"
              "FSUPPLIERID.FName,FBillTypeID.FName,FALLAMOUNTFOR_D,"
              "FNOINVOICEAMOUNT,FTAXAMOUNT_D,FCreatorId.FName")
    filt = _org_filter("FSETTLEORGID.FNumber", org_numbers)
    conds = ["FDOCUMENTSTATUS='C'", "FCancelStatus='A'", f"FDATE>='{start}'"]
    if filt:
        conds.append(filt)
    rows, _, _ = query_safe(client, "AP_Payable", fields, " AND ".join(conds))
    want = re.compile(r"费用|手工|非货")
    by_bill = collections.defaultdict(lambda: {"date": "", "org": "", "supplier": "",
                                               "type": "", "total": 0.0, "noinv": 0.0,
                                               "creator": "", "n": 0})
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 10:
            continue
        bt = s0(r[6])
        if not want.search(bt):
            continue
        no = s0(r[1])
        cell = by_bill[no]
        cell["date"] = s0(r[2])[:10]
        cell["org"] = s0(r[4])
        cell["supplier"] = s0(r[5])
        cell["type"] = bt
        cell["total"] += f2(r[7])
        cell["noinv"] += f2(r[8])
        cell["creator"] = s0(r[10])
        cell["n"] += 1
    detail = sorted(({"bill_no": k, **v, "total": _r(v["total"]),
                      "noinv": _r(v["noinv"]),
                      "settled": _r(v["total"] - v["noinv"])}
                     for k, v in by_bill.items() if abs(v["noinv"]) > 0.01),
                    key=lambda x: -abs(x["noinv"]))
    return {
        "detail": detail[:200],
        "cards": [
            {"value": _r(sum(x["noinv"] for x in detail)), "unit": "元",
             "sub": f"未开票核销合计 · {len(detail)} 张单据"},
        ],
        "note": "已结算金额 = 价税合计 − 未开票核销金额（近似口径）；"
                "单据类型按名称含「费用/手工/非货」自适应匹配。",
    }


def src_dash(client, org_numbers, start, progress):
    """带横杠单号（应收 + 应付，已审核）。"""
    out, total = [], 0.0
    for form, party_field, party_name in (
            ("AR_receivable", "FCUSTOMERID.FName", "客户"),
            ("AP_Payable", "FSUPPLIERID.FName", "供应商")):
        fields = f"FID,FBillNo,FSETTLEORGID.FNumber,FSETTLEORGID.FName,{party_field},FALLAMOUNTFOR_D"
        filt = _org_filter("FSETTLEORGID.FNumber", org_numbers)
        conds = ["FDOCUMENTSTATUS='C'", "FCancelStatus='A'", f"FDATE>='{start}'"]
        if filt:
            conds.append(filt)
        try:
            rows, _, _ = query_safe(client, form, fields, " AND ".join(conds))
        except Exception as exc:  # noqa: BLE001
            progress(f"    横杠单号：{form} 失败（{str(getattr(exc, 'message', exc))[:60]}）")
            continue
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 6:
                continue
            no = s0(r[1])
            if "-" not in no:
                continue
            amt = f2(r[5])
            total += amt
            out.append({"bill_no": no, "side": party_name, "org": s0(r[3]),
                        "party": s0(r[4]), "amount": _r(amt)})
    out.sort(key=lambda x: -abs(x["amount"]))
    return {
        "detail": out[:300],
        "cards": [
            {"value": len(out), "unit": "行", "sub": f"带横杠单号 · 金额合计 {_r(total):,.0f} 元"},
        ],
        "note": "单号中含「-」的已审核单据（应收 + 应付两侧）。",
    }


def src_recv(client, org_numbers, start, progress):
    """收票单汇总（按 组织/发票类别/状态）。"""
    fields = ("FID,FBillNo,FIVCODE,FIVNUMBER,FPURNAME,FSALENAME,FSUMAMOUNT,"
              "FSUMTAXAMOUNT,FSUMALLAMOUNT,FOPENDATE,FSTATUS,FLINKBILLTYPE,"
              "FSETTLEORGID.FNumber,FSETTLEORGID.FName")
    filt = _org_filter("FSETTLEORGID.FNumber", org_numbers)
    conds = [f"FOPENDATE>='{start}'"]
    if filt:
        conds.append(filt)
    rows, _, dropped = query_safe(client, "IV_ReceivedInvoice", fields,
                                  " AND ".join(conds))
    agg = collections.defaultdict(lambda: {"n": 0, "amount": 0.0, "total": 0.0})
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 12:
            continue
        org = s0(r[13]) or s0(r[12]) or "未知组织"
        key = (org, s0(r[11]) or "未关联", s0(r[10]) or "未知状态")
        agg[key]["n"] += 1
        agg[key]["amount"] += f2(r[6])
        agg[key]["total"] += f2(r[8])
    rows_out = sorted(({"org": k[0], "link_type": k[1], "status": k[2],
                        "n": v["n"], "amount": _r(v["amount"]),
                        "total": _r(v["total"])}
                       for k, v in agg.items()), key=lambda x: -x["total"])
    month_start = f"{datetime.date.today().year:04d}-{datetime.date.today().month:02d}-01"
    month_total = sum(f2(r[8]) for r in rows
                      if isinstance(r, (list, tuple)) and len(r) >= 9
                      and s0(r[9])[:10] >= month_start)
    return {
        "detail": rows_out,
        "cards": [
            {"value": _r(month_total), "unit": "元",
             "sub": f"本月价税合计 · 范围 {len(rows)} 张"},
        ],
        "dropped": dropped,
        "note": "价税合计口径；「未关联」= 收票单尚未挂到采购/费用单据。",
    }


def src_anom(client, org_numbers, today, progress):
    """存货异常三类：零数量有金额 / 有数量负金额 / 异常价格。"""
    fields = ("FMATERIALBASEID,FMATERIALNAME,FMATERIALGROUP,FSTOCKId,"
              "FINITQty,FRECEIVEQty,FSENDQty,FENDQty,FRECEIVEAmount,FENDAmount")
    org_label = {n: n for n in org_numbers}
    anom = {"anom1": [], "anom2": [], "anom3": []}
    errors = []
    stats = {"rows": 0, "materials": 0}
    seen_mat = set()
    for no in (org_numbers or [""]):
        model = {
            "FACCTGSYSTEMID": {"FNumber": "KJHSTX01_SYS"},
            "FACCTGORGID": {"FNumber": no} if no else {"FNumber": ""},
            "FACCTPOLICYID": {"FNumber": "KJZC01_SYS"},
            "FYear": str(today.year), "FPeriod": str(today.month),
            "FENDYEAR": str(today.year), "FEndPeriod": str(today.month),
            "FCOMBOTotalType": "不汇总", "FDimType": "FMATERIALID,FSTOCKID",
            "FIsDisplayPeriod": True,
        }
        try:
            rows = client.sys_report("HS_INOUTSTOCKSUMMARYRPT", fields, model,
                                     scheme_id="")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"组织 {no or '全部'}：{str(getattr(exc, 'message', exc))[:100]}")
            progress(f"    存货异常：组织 {no or '全部'} 取数失败"
                     f"（{str(getattr(exc, 'message', exc))[:60]}）")
            continue
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 10:
                continue
            end_qty, end_amt = f2(r[7]), f2(r[9])
            recv_amt = f2(r[8])
            code, name, stock = s0(r[0]), s0(r[1]), s0(r[3])
            stats["rows"] += 1
            if code:
                seen_mat.add(code)
            item = {"org": org_label.get(no, no), "code": code, "name": name,
                    "stock": stock, "end_qty": _r(end_qty), "end_amount": _r(end_amt)}
            if abs(end_qty) < 0.0001 and abs(end_amt) > 0.01:
                anom["anom1"].append({**item, "reason": "期末数量为 0 但有金额"})
            elif end_qty > 0 and end_amt < -0.01:
                anom["anom2"].append({**item, "reason": "有数量但金额为负"})
            elif end_qty > 0 and end_amt > 0:
                price = end_amt / end_qty
                ref = recv_amt / f2(r[5]) if abs(f2(r[5])) > 0.0001 else 0.0
                if ref > 0 and (price > ref * 10 or price < ref / 10):
                    anom["anom3"].append({**item, "reason":
                                          f"期末单价 {price:,.2f} 与本期收入均价 {ref:,.2f} 偏离超 10 倍"})
        import time as _t
        _t.sleep(0.05)
    stats["materials"] = len(seen_mat)
    n1, n2, n3 = len(anom["anom1"]), len(anom["anom2"]), len(anom["anom3"])
    for k in anom:
        anom[k].sort(key=lambda x: -abs(x["end_amount"]))
        anom[k] = anom[k][:100]
    return {
        "anom": anom, "stats": stats, "errors": errors,
        "cards": [
            {"value": (n1 + n2 + n3 if not errors else None), "unit": "项",
             "sub": f"零数量有金额 {n1} · 数量负金额 {n2} · 异常价格 {n3}"},
        ],
        "note": "存货收发存汇总表（HS_INOUTSTOCKSUMMARYRPT）当期口径；"
                "异常价格 = 期末单价与本期收入均价偏离超 10 倍。",
    }


def src_invoice(client, org_numbers, start, progress):
    """未审核发票：四类进项发票表单，未审核笔数。"""
    errors = []
    agg = collections.defaultdict(lambda: {"n": 0, "suppliers": set()})
    form_names = {"IV_PURCHASEOC": "采购普通发票", "IV_PURCHASEIC": "采购增值税专用发票",
                  "IV_PUREXPINV": "进项费用普通发票", "IV_PUREXVATIN": "进项费用增值税发票"}
    for form in IV_FORMS:
        fields = "FID,FBillNo,FSupplierID.FName,FDocumentStatus,FCancelStatus,FSETTLEORGID.FNumber"
        filt = _org_filter("FSETTLEORGID.FNumber", org_numbers)
        conds = [f"FDATE>='{start}'"]
        if filt:
            conds.append(filt)
        try:
            rows, _, _ = query_safe(client, form, fields, " AND ".join(conds))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{form}：{str(getattr(exc, 'message', exc))[:100]}")
            progress(f"    未审核发票：{form} 失败"
                     f"（{str(getattr(exc, 'message', exc))[:60]}）")
            continue
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 5:
                continue
            if s0(r[3]) == "C" or s0(r[4]) == "B":
                continue
            key = (form_names.get(form, form), s0(r[2]) or "未知供应商", s0(r[3]), s0(r[5]) if len(r) > 5 else "")
            agg[key]["n"] += 1
            agg[key]["suppliers"].add(s0(r[2]) or "未知供应商")
    rows_out = sorted(({"form": k[0], "supplier": k[1], "status": k[2],
                        "n": v["n"], "org": k[3]} for k, v in agg.items()),
                      key=lambda x: -x["n"])
    return {
        "detail": rows_out[:200], "errors": errors,
        "cards": [
            {"value": (sum(x["n"] for x in rows_out) if not errors else None), "unit": "笔",
             "sub": f"四类进项发票未审核 · {len(rows_out)} 组"},
        ],
        "note": "未审核 = 单据状态非已审核(C) 且未作废；按 表单/供应商/状态 聚合。",
    }


# ---------------------------------------------------------------- 编排
def build(client, orgs=None, org_numbers=None, year=None, period=None,
          scheme_id=metrics_mgmt.DEFAULT_SCHEME_ID, progress=None, phase="all", **_ignored):
    p = progress or (lambda *_: None)
    today = datetime.date.today()
    year = int(year or today.year)
    period = int(period or today.month)
    orgs = orgs or []
    nums = [str(x).strip() for x in (org_numbers or []) if str(x).strip()]
    if not nums:
        nums = [str(o.get("number")) for o in orgs if str(o.get("number") or "").strip()]
    nums = list(dict.fromkeys(nums))
    if phase == 'capital':
        return {'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'orgs': nums, 'pages': {}, 'cards': [], 'phase': phase,
                **financial_reports.build(client, orgs, nums, year, period, progress=p)}
    start = f"{today.year}-01-01"

    pages, cards, errors = {}, [], []

    deferred = {"interdim", "dash", "recv", "anom", "invoice"}
    def run(key, title, icon, tone, fn):
        if (phase == "overview" and key in deferred) or (phase == "detail" and key not in deferred):
            return
        try:
            p(f"◆ {title}")
            data = fn()
            pages[key] = data
            for c in (data.get("cards") or []):
                cards.append({"id": key, "title": title, "icon": icon,
                              "tone": tone, **c})
        except Exception as exc:  # noqa: BLE001
            import traceback as _tb
            tb = _tb.extract_tb(exc.__traceback__)[-1]
            msg = f"{title}：{type(exc).__name__}: {str(exc)[:90]}（{os.path.basename(tb.filename)}:{tb.lineno}）"
            errors.append(msg)
            p(f"    ✗ {msg}")

    p("①/⑬ 银行头寸与科目余额核对")
    run("bank", "头寸与科目余额核对", "🏦", "blue",
        lambda: src_bank(client, orgs, nums, year, period, scheme_id, p))
    run("subject", "科目余额波动", "📊", "blue",
        lambda: (pages.get("bank", {}).get("subject") and
                 {"swings": pages["bank"].get("swings"),
                  "subject": pages["bank"].get("subject"),
                  "cards": [{"value": len(pages["bank"].get("swings") or []), "unit": "科",
                             "sub": "按科目类型计的净额变动 Top（正=增加 / 负=减少，|变动|≥5000）"}]})
        or {"swings": [], "subject": {}, "cards": [{"value": 0, "unit": "科", "sub": "无数据"}]})
    run("est", "应付 vs 暂估差异", "⚖️", "amber",
        lambda: src_est(client, nums, today, scheme_id, p,
                        books=((pages.get("bank") or {}).get("subject") or {}).get("rows") or []))
    run("interdim", "往来科目核算维度", "🧩", "blue",
        lambda: src_interdim(client, nums, today, scheme_id, p))
    run("arhx", "应收核销对账", "🔗", "amber",
        lambda: src_arhx(client, nums, p))
    run("aphx", "应付核销对账", "🔩", "amber",
        lambda: src_aphx(client, nums, p))
    run("goods", "有货未回票", "🚚", "amber",
        lambda: src_goods(client, nums, start, today.isoformat(), p))
    run("expense", "非货未回票", "🧾", "amber",
        lambda: src_expense(client, nums, start, p))
    run("dash", "带横杠单号", "➗", "gray",
        lambda: src_dash(client, nums, start, p))
    run("recv", "收票单汇总", "📑", "blue",
        lambda: src_recv(client, nums, start, p))
    run("anom", "存货异常", "📦", "red",
        lambda: src_anom(client, nums, today, p))
    run("invoice", "未审核发票", "🕵️", "amber",
        lambda: src_invoice(client, nums, start, p))

    capital_data = financial_reports.build(client, orgs, nums, year, period, progress=p) if phase == 'all' else {}
    return {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "period": {"year": year, "period": period},
        "cards": cards, "pages": pages, "errors": errors,
        "orgs": nums, "phase": phase,
        "deferred_sources": sorted(deferred) if phase == "overview" else [],
        "working_capital": (pages.get("bank") or {}).get("working_capital") or [],
        "capital_pending": phase == 'overview', **capital_data,
    }
