# -*- coding: utf-8 -*-
"""现金流预测看板 · 指标口径（移植自「现金流预测」定时任务 recipe）

数据来源（全部动态从金蝶取，不读任何本地文件）
------------------------------------------------------
1. 实际头寸    —— 报表 CN_FundPositionReport（按组织逐个查，本位币本日余额 FLocalTodayBal）
2. 应收未结    —— AR_receivable（9 字段，FNORECEIVEAMOUNT 口径，已校准部分核销高估）
3. 应付未结    —— AP_Payable（同 9 字段）
4. 周期付款    —— AP_PayBill（工资发放/社保公积金付款单，回溯 470 天）
5. 预计新增    —— 复用 2/3 的全量数据里 FCREATEDATE 近 12 周的新单（不额外调用）

核心模型（与 recipe 一致，用户确证口径）
------------------------------------------------------
L0 过滤：已审核(C) + 未作废(A) + 未完全核销 + FNORECEIVEAMOUNT>0
L1 全局概率（账龄分段阶梯）：应收 95/85/70/45/25/5%，应付 98/95/90/80/45/10%
L2 单位行为曲线：每往来单位按账龄桶算金额加权结算率（历史额≥5万才启用）；
   三重修正：空桶前瞻填充 / 相邻桶单调不增 / 365+ 桶回落全局；
   慢性拖欠：未结>10万 且 >365天未结占比>50%，或 365+桶结算率<0.25 且 未结>5万
12 周曲线：week_ends 按基准日滚动 7 天；概率按今日账龄冻结只算一次；
   未到期单落入到期日所在周；已逾期单按近因加权衰减 [0.6^i] 归一化分摊
补充预测①：近 12 周新单趋势外推（trend 封顶 ±30%），合成单账期 45 天
补充预测②：周期性付款（工资/社保/公积金），间隔夹取 [15,45] 天
穿底：合计列首次 <0 的周；单组织谷值 ≤ -1 万升级【重点关注】

组织范围：门户登录者有权组织（动态发现，不写死 101/102/104/105）。
内部往来剔除：按组织名集合匹配客户/供应商名，命中即整单不计入。
"""
from __future__ import annotations

import collections
import datetime
import time

WEEKS = 12
HISTORY_YEARS = 3
PIPELINE_TRAIL_WEEKS = 12
PIPELINE_LAG_DAYS = 45
PIPE_PARTY_MIN_EVENTS = 3
DECAY = 0.6
TREND_CAP = 0.30
CURVE_MIN_HISTORY = 50000.0            # L2 曲线启用阈值（历史额）
CHRONIC_UNSETTLED = 100000.0           # 慢性拖欠：未结金额阈值
CHRONIC_365_RATIO = 0.50               # 慢性拖欠：>365 天未结占比阈值
CHRONIC_BUCKET_RATE = 0.25             # 慢性拖欠：365+ 桶结算率阈值
CHRONIC_BUCKET_MIN = 50000.0           # 慢性拖欠：365+ 桶时的未结金额阈值
ORG_ALERT_THRESHOLD = -10000.0         # 单组织告警分级（谷值）
PERIODIC_TYPES = ("工资发放付款单", "社保公积金付款单")
PERIODIC_LOOKBACK_DAYS = 470
PERIODIC_RECENT = 6                    # 单次金额取近 N 笔平均
PERIODIC_GAP = (15, 45)
PERIODIC_GAP_DEFAULT = 30

FUND_FIELDS = ",".join([
    "FRowTypeName", "FBankName", "FBankAcctName", "FBankAcctNo", "FPAYORGNAME",
    "FINNERACCTNAME", "FINNERACCTNO", "FForCurrencyName", "FForLastBal",
    "FForTodayIn", "FForTodayOut", "FForTodayBal", "FLocalCurrencyName",
    "FLocalLastBal", "FLocalTodayIn", "FLocalTodayOut", "FLocalTodayBal",
])
AR_FIELDS = ("FID,FBillNo,FCUSTOMERID.FName,FSETTLEORGID.FNumber,FSETTLEORGID.FName,"
             "FENDDATE_H,FALLAMOUNT_D,FWRITTENOFFSTATUS,FNORECEIVEAMOUNT,FCREATEDATE")
AP_FIELDS = ("FID,FBillNo,FSUPPLIERID.FName,FSETTLEORGID.FNumber,FSETTLEORGID.FName,"
             "FENDDATE_H,FALLAMOUNT_D,FWRITTENOFFSTATUS,FNORECEIVEAMOUNT,FCREATEDATE")
PAYBILL_FIELDS = ("FID,FDate,FSETTLEORGID.FNumber,FBillTypeID.FName,"
                  "FCOSTID.FName,FPAYTOTALAMOUNTFOR")

# 账龄桶 → 全局概率（pre=未到期）
AGE_BUCKETS = ("pre", "0_30", "31_90", "91_180", "181_365", "365p")
GLOBAL_PROB = {
    "AR": {"pre": 0.95, "0_30": 0.85, "31_90": 0.70, "91_180": 0.45, "181_365": 0.25, "365p": 0.05},
    "AP": {"pre": 0.98, "0_30": 0.95, "31_90": 0.90, "91_180": 0.80, "181_365": 0.45, "365p": 0.10},
}
BUCKET_DAYS = {"0_30": (0, 30), "31_90": (31, 90), "91_180": (91, 180),
               "181_365": (181, 365), "365p": (366, 10 ** 9)}


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


def _d(v):
    s = s0(v)[:10]
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def age_bucket(age_days: int) -> str:
    if age_days < 0:
        return "pre"
    for name, (lo, hi) in BUCKET_DAYS.items():
        if lo <= age_days <= hi:
            return name
    return "365p"


def _org_filter(field, nums):
    if not nums:
        return ""
    parts = [f"{field}='{n}'" for n in nums]
    return "(" + " OR ".join(parts) + ")"


class _Rec:
    __slots__ = ("billno", "party", "org", "org_name", "due", "orig", "left", "created")

    def __init__(self, billno, party, org, org_name, due, orig, left, created):
        self.billno, self.party, self.org = billno, party, org
        self.org_name, self.due, self.orig = org_name, due, orig
        self.left, self.created = left, created


def _fetch_side(client, form_id, fields, party_field, kind, org_nums, floor, progress):
    """拉一类往来单（应收/应付），L0 过滤后返回 _Rec 列表。"""
    filt = _org_filter("FSETTLEORGID.FNumber", org_nums)
    conds = ["FDOCUMENTSTATUS='C'", "FCancelStatus='A'"]
    if filt:
        conds.append(filt)
    if floor:
        conds.append(f"FENDDATE_H>='{floor}'")
    rows = client.query_all(form_id, fields, " AND ".join(conds), page=5000,
                            max_rows=200000, order="FID")
    out = []
    for r in rows:
        if not isinstance(r, (list, tuple)):
            continue
        left = f2(r[8])
        if left <= 0.01:
            continue                                   # 已结清/无未结
        if s0(r[7]) == "C":
            continue                                   # 已完全核销
        out.append(_Rec(s0(r[1]), s0(r[2]), s0(r[3]), s0(r[4]), _d(r[5]),
                        f2(r[6]), left, _d(r[9]) if len(r) > 9 else None))
    progress(f"    {form_id}：{len(rows)} 行 → 未结 {len(out)} 笔")
    return out


def fetch_fund(client, org_nums, today, progress):
    """逐组织查资金头寸（一次传多组织会漏主账户，与看板口径一致）。

    返回 (position, errors)：errors 用于把失败原因暴露到页面，不静默出 0。
    """
    pos, errors = {}, []
    debug = {}
    seen_accounts = set()
    for no in org_nums:
        model = {
            "FSETTLEORGID": [{"FNumber": no}], "FOrgId": [{"FNumber": no}],
            "FStartDate": f"{today} 00:00:00", "FEndDate": f"{today} 23:59:59",
            "FMyCurrency": True, "FInNOut": True, "FNotAudit": False,
            "FAllBankAccount": True, "FAllCashAccount": True,
            "FINCLUDEEMPTY": False, "FIsShowCancelBankAcnt": False,
        }
        try:
            rows = client.sys_report("CN_FundPositionReport", FUND_FIELDS, model, scheme_id="")
        except Exception as exc:  # noqa: BLE001
            msg = f"组织 {no}：{str(getattr(exc, 'message', exc))[:100]}"
            errors.append(msg)
            progress(f"    头寸：{msg}")
            continue
        if not any(isinstance(r, (list, tuple)) and len(r) >= 17 for r in rows):
            errors.append(f"组织 {no}：未返回可用头寸记录")
            continue
        total = 0.0
        for r in rows:
            # ⚠️ 与 metrics_mgmt 的头寸解析保持一致：len<17 跳过，取 13/14/15/16
            #   （本位币期初/收入/支出/本日余额）。早期用 <18 + r[16] 索引，
            #   遇到服务端少返两列（FInCount/FOutCount）时整表被跳过 → 头寸静默变 0。）
            if not isinstance(r, (list, tuple)) or len(r) < 17:
                continue
            account = s0(r[3])
            if account and account in seen_accounts:
                continue
            if account:
                seen_accounts.add(account)
            start_bal, today_in = f2(r[13]), f2(r[14])
            today_out, today_bal = f2(r[15]), f2(r[16])
            if abs(start_bal) + abs(today_in) + abs(today_out) + abs(today_bal) == 0:
                continue
            total += today_bal
        pos[no] = round(total, 2)
        debug[no] = {"rows": len(rows), "total": round(total, 2),
                     "sample": rows[0] if rows else None}
        progress(f"    头寸：组织 {no} → {total:,.2f}（{len(rows)} 行）")
        time.sleep(0.05)
    return pos, errors, debug


def fetch_paybills(client, org_nums, today, progress):
    """周期性付款单（工资/社保公积金），回溯 470 天。"""
    since = today - datetime.timedelta(days=PERIODIC_LOOKBACK_DAYS)
    filt = _org_filter("FSETTLEORGID.FNumber", org_nums)
    conds = [f"FDate>='{since}'", "FDOCUMENTSTATUS='C'", "FCancelStatus='A'"]
    if filt:
        conds.append(filt)
    types = "(" + " OR ".join(f"FBillTypeID.FName='{t}'" for t in PERIODIC_TYPES) + ")"
    conds.append(types)
    rows = client.query_all("AP_PayBill", PAYBILL_FIELDS, " AND ".join(conds),
                            page=5000, max_rows=100000, order="FID")
    out = []
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            continue
        bt = s0(r[3])
        cat = "工资" if bt == "工资发放付款单" else (s0(r[4]) or "社保公积金")
        out.append({"date": _d(r[1]), "org": s0(r[2]), "bill_type": bt,
                    "category": cat, "amount": f2(r[5])})
    progress(f"    周期付款单：{len(rows)} 行 → 事件 {len(out)} 笔")
    return out


def merge_records(recs):
    """同 (组织, 往来单位, 到期日) 合并为一行。"""
    g = collections.defaultdict(lambda: {"orig": 0.0, "left": 0.0, "count": 0})
    for r in recs:
        key = (r.org, r.party, r.due.isoformat() if r.due else "")
        g[key]["orig"] += r.orig
        g[key]["left"] += r.left
        g[key]["count"] += 1
    return g


def global_prob(kind, bucket):
    return GLOBAL_PROB[kind][bucket]


def build_curves(recs, kind, today):
    """L2 单位行为曲线：每单位每账龄桶的金额加权结算率。

    已结清单（orig 全额）计入分子；未结清单按 orig−left 计入分母。
    返回 {party: {bucket: rate}}；历史额 < CURVE_MIN_HISTORY 的单位不启用。
    """
    agg = collections.defaultdict(lambda: collections.defaultdict(
        lambda: {"settled": 0.0, "unsettled": 0.0}))
    hist_total = collections.defaultdict(float)
    for r in recs:
        if not r.due:
            continue
        age = (today - r.due).days
        b = age_bucket(age)
        cell = agg[r.party][b]
        if r.left <= 0.01:
            cell["settled"] += r.orig
        else:
            cell["settled"] += max(0.0, r.orig - r.left)
            cell["unsettled"] += r.left
        hist_total[r.party] += r.orig
    curves = {}
    for party, buckets in agg.items():
        if hist_total[party] < CURVE_MIN_HISTORY:
            continue
        raw = {}
        for b, cell in buckets.items():
            tot = cell["settled"] + cell["unsettled"]
            if tot > 0:
                raw[b] = cell["settled"] / tot
        if not raw:
            continue
        order = [b for b in AGE_BUCKETS if b in raw]
        # 修正 1：空桶前瞻填充（沿用更年轻桶的实测值）
        filled = {}
        last = None
        for b in AGE_BUCKETS:
            if b in raw:
                last = raw[b]
            filled[b] = raw[b] if b in raw else (last if last is not None else None)
        # 修正 2：相邻桶单调不增（账龄越大结算率越低）
        prev = 1.0
        mono = {}
        for b in AGE_BUCKETS:
            v = filled[b]
            if v is None:
                mono[b] = None
                continue
            v = min(v, prev)
            mono[b] = v
            prev = v
        # 修正 3：365+ 桶回落全局
        if mono.get("365p") is None:
            mono["365p"] = global_prob(kind, "365p")
        curves[party] = mono
    return curves


def resolve_prob(kind, rec, today, curves):
    """一笔未结单的收回/支付概率：优先 L2 单位曲线，否则 L1 全局。"""
    if not rec.due:
        return global_prob(kind, "pre"), "全局L1"
    age = (today - rec.due).days
    b = age_bucket(age)
    curve = curves.get(rec.party)
    if curve:
        v = curve.get(b)
        if v is None and b != "pre":
            # 该桶无实测：用更年轻桶（前瞻填充已做，双保险）
            for bb in reversed(AGE_BUCKETS[:AGE_BUCKETS.index(b)]):
                if curve.get(bb) is not None:
                    v = curve[bb]
                    break
        if v is None:
            v = curve.get("pre")
        if v is not None:
            if b == "pre":
                v = min(v, global_prob(kind, "pre"))
            return v, "行为曲线"
    return global_prob(kind, b), "全局L1"


def chronic_flags(recs, today):
    """慢性拖欠单位判定（用于风险榜标记）。"""
    by_party = collections.defaultdict(lambda: {"unsettled": 0.0, "over365": 0.0,
                                                "bucket365_rate": None})
    for r in recs:
        if r.left <= 0.01 or not r.due:
            continue
        age = (today - r.due).days
        cell = by_party[r.party]
        cell["unsettled"] += r.left
        if age > 365:
            cell["over365"] += r.left
    # 365+ 桶结算率（与 build_curves 同口径）
    agg = collections.defaultdict(lambda: {"settled": 0.0, "unsettled": 0.0})
    for r in recs:
        if not r.due:
            continue
        if age_bucket((today - r.due).days) == "365p":
            cell = agg[r.party]
            if r.left <= 0.01:
                cell["settled"] += r.orig
            else:
                cell["settled"] += max(0.0, r.orig - r.left)
                cell["unsettled"] += r.left
    out = {}
    for party, cell in by_party.items():
        a = agg.get(party)
        rate = None
        if a and (a["settled"] + a["unsettled"]) > 0:
            rate = a["settled"] / (a["settled"] + a["unsettled"])
        chronic = False
        if cell["unsettled"] > CHRONIC_UNSETTLED and cell["unsettled"] > 0 \
                and cell["over365"] / cell["unsettled"] > CHRONIC_365_RATIO:
            chronic = True
        if rate is not None and rate < CHRONIC_BUCKET_RATE \
                and cell["unsettled"] > CHRONIC_BUCKET_MIN:
            chronic = True
        if chronic:
            out[party] = {"unsettled": round(cell["unsettled"], 2),
                          "over365": round(cell["over365"], 2),
                          "bucket365_rate": round(rate, 4) if rate is not None else None}
    return out


def allocate_weeks(rec, prob, week_ends, today):
    """把一笔未结单的期望金额分摊到 12 周。返回 [(week_index, amount), ...]。"""
    exp = rec.left * prob
    if exp <= 0:
        return []
    if rec.due and rec.due > today:
        # 未到期：落入到期日所在周（超窗则最后一周），一笔到位
        for i, we in enumerate(week_ends):
            if rec.due <= we:
                return [(i, exp)]
        return [(len(week_ends) - 1, exp)]
    # 已逾期：近因加权衰减 [0.6^i] 归一化
    w = [DECAY ** i for i in range(len(week_ends))]
    tot = sum(w) or 1.0
    return [(i, exp * wi / tot) for i, wi in enumerate(w)]


def project_pipeline(recs, kind, week_ends, today):
    """补充预测①：近 12 周新单趋势外推（按 (组织,单位) 聚合）。"""
    trail_start = today - datetime.timedelta(weeks=PIPELINE_TRAIL_WEEKS)
    half_start = today - datetime.timedelta(weeks=PIPELINE_TRAIL_WEEKS // 2)
    by_key = collections.defaultdict(lambda: collections.defaultdict(float))
    for r in recs:
        if not r.created or r.created < trail_start:
            continue
        week_idx = (today - r.created).days // 7
        if week_idx >= PIPELINE_TRAIL_WEEKS:
            continue
        by_key[(r.org, r.party)][week_idx] += r.orig
    org_recent = collections.defaultdict(float)
    for (org, _party), weeks in by_key.items():
        for wi, amt in weeks.items():
            if wi < PIPELINE_TRAIL_WEEKS // 2:
                org_recent[org] += amt
    out_rows, out_weeks = [], collections.defaultdict(float)
    for (org, party), weeks in sorted(by_key.items()):
        n_events = sum(1 for v in weeks.values() if v > 0)
        if n_events < PIPE_PARTY_MIN_EVENTS:
            continue                      # 残差并入各组织 __OTHER__ 由 org_recent 体现
        recent = sum(v for wi, v in weeks.items() if wi < PIPELINE_TRAIL_WEEKS // 2)
        prior = sum(v for wi, v in weeks.items() if wi >= PIPELINE_TRAIL_WEEKS // 2)
        base = recent / (PIPELINE_TRAIL_WEEKS // 2)
        trend = ((base - (prior / (PIPELINE_TRAIL_WEEKS - PIPELINE_TRAIL_WEEKS // 2)))
                 / (prior / (PIPELINE_TRAIL_WEEKS - PIPELINE_TRAIL_WEEKS // 2))
                 if prior > 0 else 0.0)
        trend = max(-TREND_CAP, min(TREND_CAP, trend))
        row = {"org": org, "party": party, "kind": kind, "n_events": n_events,
               "base": round(base, 2), "prior": round(prior, 2),
               "trend": round(trend, 4), "weeks": [], "expect_total": 0.0}
        for w_i in range(len(week_ends)):
            proj = base * (1 + trend * w_i / len(week_ends))
            due = week_ends[w_i] + datetime.timedelta(days=PIPELINE_LAG_DAYS)
            # 合成单按今日账龄 45 天 = 未到期 → 概率受 0_30 桶封顶
            prob = min(global_prob(kind, "pre"), global_prob(kind, "0_30"))
            exp = proj * prob
            row["weeks"].append(round(exp, 2))
            row["expect_total"] += exp
            out_weeks[w_i] += exp
        row["expect_total"] = round(row["expect_total"], 2)
        out_rows.append(row)
    # 残差组：近半窗有录入但不满足单列条件的，按组织并入
    listed = {(r["org"], r["party"]) for r in out_rows}
    residual = collections.defaultdict(float)
    for (org, party), weeks in by_key.items():
        if (org, party) in listed:
            continue
        for wi, amt in weeks.items():
            if wi < PIPELINE_TRAIL_WEEKS // 2:
                residual[org] += amt
    for org, amt in residual.items():
        base = amt / (PIPELINE_TRAIL_WEEKS // 2)
        if base <= 0:
            continue
        row = {"org": org, "party": "其他录入（残差）", "kind": kind,
               "n_events": 0, "base": round(base, 2), "prior": 0.0, "trend": 0.0,
               "weeks": [], "expect_total": 0.0}
        for w_i in range(len(week_ends)):
            exp = base * global_prob(kind, "0_30")
            row["weeks"].append(round(exp, 2))
            row["expect_total"] += exp
            out_weeks[w_i] += exp
        row["expect_total"] = round(row["expect_total"], 2)
        out_rows.append(row)
    out_rows.sort(key=lambda x: -x["expect_total"])
    return out_rows[:40], dict(out_weeks)


def project_periodic(events, week_ends, today):
    """补充预测②：周期性付款（工资/社保/公积金），间隔夹取 [15,45] 天。"""
    by_key = collections.defaultdict(list)
    for e in events:
        if not e["date"]:
            continue
        by_key[(e["org"], e["category"])].append(e)
    out_rows, out_weeks = [], collections.defaultdict(float)
    for (org, cat), evs in sorted(by_key.items()):
        evs.sort(key=lambda x: x["date"])
        # 按 (FID) 去重后的"事件"：这里用日期+金额近似（同一天同类多笔会合并视图，金额相加）
        by_day = collections.defaultdict(float)
        for e in evs:
            by_day[e["date"]] += e["amount"]
        days = sorted(by_day)
        if not days:
            continue
        recent = days[-PERIODIC_RECENT:]
        amount = sum(by_day[d] for d in recent) / len(recent)
        if len(days) >= 2:
            gaps = [(days[i + 1] - days[i]).days for i in range(len(days) - 1)]
            gap = sum(gaps) / len(gaps)
            gap = max(PERIODIC_GAP[0], min(PERIODIC_GAP[1], gap))
        else:
            gap = PERIODIC_GAP_DEFAULT
        last = days[-1]
        nxt = last + datetime.timedelta(days=int(round(gap)))
        covered, total = 0, 0.0
        while nxt <= week_ends[-1] and covered < 26:
            for i, we in enumerate(week_ends):
                if nxt <= we:
                    out_weeks[i] += amount
                    total += amount
                    covered += 1
                    break
            nxt = nxt + datetime.timedelta(days=int(round(gap)))
        out_rows.append({
            "org": org, "category": cat, "next_date": nxt.isoformat(),
            "amount": round(amount, 2), "gap": int(round(gap)),
            "covered_weeks": covered, "total": round(total, 2),
        })
    out_rows.sort(key=lambda x: -x["total"])
    return out_rows, dict(out_weeks)


def build(client, orgs=None, org_numbers=None, progress=None, **_ignored):
    """orgs       登录后发现的组织 [{number,name}]（组织名匹配/内部往来剔除用）
    org_numbers 用户勾选的组织编码；空 = 全部有权组织
    """
    p = progress or (lambda *_: None)
    today = datetime.date.today()
    orgs = orgs or []
    name_by_no = {str(o.get("number")): str(o.get("name") or "") for o in orgs}
    nums = [str(x).strip() for x in (org_numbers or []) if str(x).strip()]
    if not nums:
        nums = [n for n in name_by_no if n]
    nums = list(dict.fromkeys(nums))
    org_names = {name_by_no.get(n, "") for n in nums if name_by_no.get(n)}
    week_ends = [today + datetime.timedelta(days=7 * (i + 1)) for i in range(WEEKS)]
    floor = (today - datetime.timedelta(days=365 * HISTORY_YEARS)).isoformat()

    p("①/⑤ 拉取实际头寸（按组织逐个查资金头寸表）")
    position, fund_errors, fund_debug = fetch_fund(client, nums, today, p)
    p("②/⑤ 拉取应收未结")
    ar_recs = _fetch_side(client, "AR_receivable", AR_FIELDS, "FCUSTOMERID.FName",
                          "AR", nums, floor, p)
    p("③/⑤ 拉取应付未结")
    ap_recs = _fetch_side(client, "AP_Payable", AP_FIELDS, "FSUPPLIERID.FName",
                          "AP", nums, floor, p)
    p("④/⑤ 拉取周期性付款单")
    pay_events = fetch_paybills(client, nums, today, p)

    # 内部往来剔除（客户/供应商名命中组织名即整单不计）
    def is_internal(rec):
        for nm in org_names:
            if nm and nm in (rec.party or ""):
                return True
        return False

    internal_ar = sum(r.orig for r in ar_recs if is_internal(r))
    internal_ap = sum(r.orig for r in ap_recs if is_internal(r))
    ar_recs = [r for r in ar_recs if not is_internal(r)]
    ap_recs = [r for r in ap_recs if not is_internal(r)]
    p(f"⑤/⑤ 内部往来剔除：应收 {internal_ar:,.0f} / 应付 {internal_ap:,.0f} 元")

    # 概率与期望
    ar_curves = build_curves(ar_recs, "AR", today)
    ap_curves = build_curves(ap_recs, "AP", today)
    ar_chronic = chronic_flags(ar_recs, today)
    ap_chronic = chronic_flags(ap_recs, today)

    def expected(recs, kind, curves):
        rows, weeks = [], collections.defaultdict(float)
        for r in recs:
            prob, src = resolve_prob(kind, r, today, curves)
            for i, amt in allocate_weeks(r, prob, week_ends, today):
                weeks[i] += amt
            rows.append((r, prob, src))
        return rows, dict(weeks)

    ar_rows, ar_weeks = expected(ar_recs, "AR", ar_curves)
    ap_rows, ap_weeks = expected(ap_recs, "AP", ap_curves)

    pipe_ar_rows, pipe_ar_weeks = project_pipeline(ar_recs, "AR", week_ends, today)
    pipe_ap_rows, pipe_ap_weeks = project_pipeline(ap_recs, "AP", week_ends, today)
    periodic_rows, periodic_weeks = project_periodic(pay_events, week_ends, today)

    # 12 周曲线（按组织 + 合计）
    org_curve, kpi_by_org = {}, {}
    for no in nums:
        ar_local = [x for x in ar_rows if x[0].org == no]
        ap_local = [x for x in ap_rows if x[0].org == no]
        aw, pw = collections.defaultdict(float), collections.defaultdict(float)
        for target, rows in [(aw, ar_local), (pw, ap_local)]:
            for rec, probability, _ in rows:
                for wi, amount in allocate_weeks(rec, probability, week_ends, today):
                    target[wi] += amount
        _, paw = project_pipeline([r for r in ar_recs if r.org == no], "AR", week_ends, today)
        _, ppw = project_pipeline([r for r in ap_recs if r.org == no], "AP", week_ends, today)
        _, periodic = project_periodic([r for r in pay_events if r["org"] == no], week_ends, today)
        value, values = position.get(no, 0.0), []
        for wi in range(WEEKS):
            value += aw.get(wi, 0) - pw.get(wi, 0) + paw.get(wi, 0) - ppw.get(wi, 0) - periodic.get(wi, 0)
            values.append(round(value, 2))
        org_curve[no] = values if no in position else [None] * WEEKS
        # ★ 与 12 周曲线同口径：曲线每周 = 应收期望 + 预计新增应收
        #   − 应付期望 − 预计新增应付 − 周期性付款。
        #   只暴露 ap_expect（未结应付单期望）会让「曲线向下但没有应付流出数据」无法解释。
        org_ar_in = round(sum(r.left * prob for r, prob, _ in ar_local)
                          + sum(paw.values()), 2)
        org_ap_out = round(sum(r.left * prob for r, prob, _ in ap_local)
                           + sum(ppw.values()) + sum(periodic.values()), 2)
        kpi_by_org[no] = {
            "today_position": position.get(no),
            "ar_nominal": round(sum(r.left for r, _, _ in ar_local), 2),
            "ap_nominal": round(sum(r.left for r, _, _ in ap_local), 2),
            "ar_expect": round(sum(r.left * prob for r, prob, _ in ar_local), 2),
            "ap_expect": round(sum(r.left * prob for r, prob, _ in ap_local), 2),
            "pipe_ar": round(sum(paw.values()), 2), "pipe_ap": round(sum(ppw.values()), 2),
            "periodic_total": round(sum(periodic.values()), 2),
            "ar_in_expect": org_ar_in, "ap_out_expect": org_ap_out,
            "net_flow_expect": round(org_ar_in - org_ap_out, 2),
        }
    total_curve = [(round(sum(org_curve[no][wi] for no in nums), 2)
                    if nums and all(org_curve[no][wi] is not None for no in nums) else None)
                   for wi in range(WEEKS)]

    first_negative = None
    for i, v in enumerate(total_curve):
        if v is not None and v < 0:
            first_negative = i
            break
    org_breaches = []
    for n in nums:
        vals = org_curve[n]
        if any(v is None for v in vals):
            continue
        neg = [i for i, v in enumerate(vals) if v < 0]
        if neg:
            org_breaches.append({
                "org": n, "org_name": name_by_no.get(n, n), "week": neg[0] + 1,
                "min": min(vals), "end": vals[-1],
                "alert": min(vals) <= ORG_ALERT_THRESHOLD,
            })

    ar_nominal = sum(r.left for r in ar_recs)
    ap_nominal = sum(r.left for r in ap_recs)
    ar_expect = sum(r.left * prob for r, prob, _ in ar_rows)
    ap_expect = sum(r.left * prob for r, prob, _ in ap_rows)
    pipe_ar_total = sum(pipe_ar_weeks.values())
    pipe_ap_total = sum(pipe_ap_weeks.values())
    periodic_total = sum(periodic_weeks.values())

    # ★ 12 周曲线扣减的是「未结应付期望 + 预计新增应付 + 周期性付款（工资/社保/公积金）」，
    #   只看 ap_expect 会让「曲线向下但应付流出期望为零」看起来像缺数据。
    #   这里给出与曲线同口径的流入 / 流出 / 净额，三项与曲线逐周变化完全一致。
    ar_in_expect = round(ar_expect + pipe_ar_total, 2)
    ap_out_expect = round(ap_expect + pipe_ap_total + periodic_total, 2)
    net_flow_expect = round(ar_in_expect - ap_out_expect, 2)

    def risk_rows(rows, chronic, kind, top=10):
        out = []
        for r, prob, src in rows:
            out.append({
                "org": r.org, "org_name": name_by_no.get(r.org, r.org_name or r.org),
                "party": r.party, "due": r.due.isoformat() if r.due else "",
                "age_days": (today - r.due).days if r.due else 0,
                "nominal": round(r.left, 2), "prob": round(prob, 4), "src": src,
                "expect": round(r.left * prob, 2),
                "chronic": bool(chronic.get(r.party)),
            })
        out.sort(key=lambda x: -x["expect"])
        return out[:top]

    # 明细（Top 50，与定时任务 HTML 一致）
    def detail_rows(rows, chronic, kind, top=50):
        out = []
        for r, prob, src in rows:
            out.append({
                "org": r.org, "org_name": name_by_no.get(r.org, r.org_name or r.org),
                "party": r.party, "due": r.due.isoformat() if r.due else "",
                "age_days": (today - r.due).days if r.due else 0,
                "nominal": round(r.left, 2), "prob": round(prob, 4), "src": src,
                "expect": round(r.left * prob, 2),
                "chronic": bool(chronic.get(r.party)),
            })
        out.sort(key=lambda x: -x["expect"])
        return out[:top]

    curve_covered = sum(1 for r in ar_recs if r.party in ar_curves) + \
        sum(1 for r in ap_recs if r.party in ap_curves)
    total_recs = len(ar_recs) + len(ap_recs)

    return {
        "generated_at": today.isoformat(),
        "orgs": [{"number": n, "name": name_by_no.get(n, n)} for n in nums],
        "weeks": WEEKS,
        "week_labels": [f"第{i + 1}周" for i in range(WEEKS)],
        "week_ends": [d.isoformat() for d in week_ends],
        "kpi": {
            "today_position": round(sum(position.values()), 2),
            "ar_nominal": round(ar_nominal, 2), "ar_expect": round(ar_expect, 2),
            "ap_nominal": round(ap_nominal, 2), "ap_expect": round(ap_expect, 2),
            "net_nominal": round(ar_nominal - ap_nominal, 2),
            "net_expect": round(ar_expect - ap_expect, 2),
            "ar_in_expect": ar_in_expect, "ap_out_expect": ap_out_expect,
            "net_flow_expect": net_flow_expect,
            "first_negative_week": (first_negative + 1) if first_negative is not None else None,
            "curve_covered": curve_covered, "curve_total": total_recs,
            "internal_ar": round(internal_ar, 2), "internal_ap": round(internal_ap, 2),
            "pipe_ar": pipe_ar_total,
            "pipe_ap": pipe_ap_total,
            "periodic_total": periodic_total,
        },
        "kpi_by_org": kpi_by_org,
        "position": position,
        "fund_errors": fund_errors,
        "fund_debug": {k: {"rows": v["rows"], "total": v["total"]} for k, v in fund_debug.items()},
        "fund_sample": next(iter(fund_debug.values())).get("sample") if fund_debug else None,
        "curve": {"orgs": org_curve, "total": total_curve},
        "org_breaches": org_breaches,
        "ar_detail": detail_rows(ar_rows, ar_chronic, "AR"),
        "ap_detail": detail_rows(ap_rows, ap_chronic, "AP"),
        "ar_risk": risk_rows(ar_rows, ar_chronic, "AR"),
        "ap_risk": risk_rows(ap_rows, ap_chronic, "AP"),
        "pipe_ar": pipe_ar_rows, "pipe_ap": pipe_ap_rows,
        "periodic": periodic_rows,
        "chronic_ar": {k: v for k, v in list(ar_chronic.items())[:20]},
        "chronic_ap": {k: v for k, v in list(ap_chronic.items())[:20]},
        "method_notes": [
            "未结净额口径 = 单据「未结算金额」FNORECEIVEAMOUNT（已扣除该单已收/付部分，部分核销不再高估）。",
            "已剔除组织间内部往来（客户/供应商名命中组织名即整单不计）。",
            "L1 全局概率按账龄分段阶梯；L2 单位行为曲线按该单位历史结算率（启用阈值 5 万，三重修正）。",
            "12 周按基准日滚动 7 天；概率按今日账龄冻结；未到期单落入到期日所在周，逾期单近因加权衰减分摊。",
            "补充预测①：近 12 周新单趋势外推（±30% 封顶，账期 45 天）；②：周期性付款（间隔 15–45 天）。",
            "期望流入 = 未结应收期望 + 预计新增应收；期望流出 = 未结应付期望 + 预计新增应付 + 周期性付款；"
            "两项与 12 周曲线逐周变化完全一致，因此曲线向下一定能从流出构成里找到来源。",
            "穿底 = 合计头寸首次 <0 的周；单组织谷值 ≤ -1 万升级重点关注。",
        ],
    }
