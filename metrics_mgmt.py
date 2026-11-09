# -*- coding: utf-8 -*-
"""管理会计看板 · 指标口径（移植自 管理会计仪表盘 的三个核心模块）

本期先上三个模块（后续再补应收/应付/存货/组织间往来）：
  A. 资金头寸         —— 报表 CN_FundPositionReport（银行+现金账户，按组织）
  B. 货币资金科目余额  —— 报表 GL_RPT_AccountBalance（1001/1002/1012，按账簿/组织）
  C. 科目余额波动 Top  —— 同一报表的本月 vs 上月发生额，按科目

与旧脚本的差异：组织编码、账簿编码、组织名全部动态获取，不内置。
旧脚本 bank_position.py 里 org_of() 是按「示例公司丙/示例公司乙/示例公司丁/示例公司甲」写死关键字匹配，
这里改成对"登录后自动发现的组织名"做最长公共子串匹配 —— 换一家公司不用改代码。
"""
from __future__ import annotations

import datetime
from financial_health import working_capital

FUND_CODES = ("1001", "1002", "1012")
FUND_KEYWORDS = ("货币资金", "库存现金", "银行存款", "其他货币")

FUND_FIELDS = ",".join([
    "FRowTypeName", "FBankName", "FBankAcctName", "FBankAcctNo", "FPAYORGNAME",
    "FINNERACCTNAME", "FINNERACCTNO", "FForCurrencyName", "FForLastBal",
    "FForTodayIn", "FForTodayOut", "FForTodayBal", "FLocalCurrencyName",
    "FLocalLastBal", "FLocalTodayIn", "FLocalTodayOut", "FLocalTodayBal",
    "FInCount", "FOutCount",
])

BAL_FIELDS = ("FBALANCEID,FBALANCENAME,FDETAILNUMBER,FDETAILNAME,"
              "FBEGINDEBITLOCAL,FBEGINCREDITLOCAL,FDEBITLOCAL,FCREDITLOCAL,"
              "FYTDDEBITLOCAL,FYTDCREDITLOCAL,FENDDEBITLOCAL,FENDCREDITLOCAL")

# 科目余额表报表方案 ID：环境相关，可用门户配置覆盖
DEFAULT_SCHEME_ID = "69c396dfde2072"


def f2(v):
    try:
        if isinstance(v, str):
            v = v.replace(",", "").strip()
            if v == "":
                return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _lcs_in(hay: str, needle: str) -> int:
    """needle 在 hay 中出现的最长子串长度（>=2 才算命中）"""
    if not hay or not needle:
        return 0
    best = 0
    n = len(needle)
    for size in range(n, 1, -1):
        for i in range(0, n - size + 1):
            if needle[i:i + size] in hay:
                return size
    return best


def match_org(text, orgs):
    """把一段文本归属到某个组织：对发现出来的组织名做最长公共子串匹配，不写死关键字"""
    text = str(text or "")
    if not text:
        return None
    best, best_len = None, 1
    for o in orgs or []:
        nm = str(o.get("name") or "").strip()
        if not nm:
            continue
        score = _lcs_in(text, nm)
        if score > best_len:
            best, best_len = o, score
        if str(o.get("number") or "") and str(o["number"]) in text:
            return o
    return best


def subject_side(code, name=""):
    """按科目编码判断类型与余额方向（金蝶：1 资产 / 2 负债 / 3 共同 / 4 权益 / 5 成本 / 6 损益）。

    用户确证口径（2026-10-17）：
      资产类 = 借方 − 贷方；负债类 = 贷方 − 借方；收入类 = 贷方；
      成本/费用类 = 借方；权益类 = 贷方 − 借方。
    返回 (类型名, "debit"/"credit")，方向决定净额算法，**正数统一表示"余额增加"**，
    这样不同科目的波动可以放在一起比较。
    """
    c = str(code or "").strip()
    n = str(name or "")
    if not c:
        return ("其他", "debit")
    head = c[0]
    if head == "1":
        return ("资产", "debit")
    if head == "2":
        return ("负债", "credit")
    if head == "3":
        return ("共同", "debit")          # 共同类多为资产负债混合，按借方口径
    if head == "4":
        return ("权益", "credit")
    if head == "5":
        return ("成本", "debit")
    if head == "6":
        # 收入类：主营/其他业务收入、投资收益、营业外收入等（名称含"收入/收益"兜底）
        if c[:4] in ("6001", "6051", "6111", "6117", "6301") or "收入" in n or "收益" in n:
            return ("收入", "credit")
        return ("费用", "debit")
    return ("其他", "debit")


def _period_pair(year, period):
    if period <= 1:
        return (year - 1, 12), (year, period)
    return (year, period - 1), (year, period)


def build(client, orgs=None, account_books=None, year=None, period=None,
          scheme_id=DEFAULT_SCHEME_ID, max_books=12, progress=None,
          fund_period_note=None, org_filter=None):
    """orgs        登录后自动发现的全量组织（用于组织名匹配、账簿归属）
    org_filter  门户里用户勾选的组织编码；给了就只取这些组织，空/None 表示全部有权组织
    """
    p = progress or (lambda *_: None)
    today = datetime.date.today()
    year = int(year or today.year)
    period = int(period or today.month)
    (py, pp), (cy, cp) = _period_pair(year, period)
    notes = []

    # 组织编码：优先用登录后自动发现的组织；没有就交给金蝶按账号权限返回
    org_numbers = [str(o.get("number")) for o in (orgs or []) if str(o.get("number") or "").strip()]
    org_numbers = [n for n in dict.fromkeys(org_numbers)]
    # 用户在门户里勾选的组织：只影响"取哪些组织"，不影响组织名匹配用的全量清单
    allowed = {str(x).strip() for x in (org_filter or []) if str(x).strip()}
    if allowed:
        org_numbers = [n for n in org_numbers if n in allowed] if org_numbers else sorted(allowed)

    # ---------- A. 资金头寸 ----------
    # 实测结论：把多个组织一次性塞进 FSETTLEORGID / FOrgId，报表口径会变（会漏掉主账户），
    # 所以这里改成**按组织逐个取数**，再按「银行账号」去重 —— 顺带解决两件事：
    #   ① 组织归属天然准确（不再靠名字匹配）；
    #   ② 若某个组织（如集团）返回了包含下级账户的全量，去重后不会重复相加。
    p("①/③ 拉取资金头寸（银行 / 现金账户，按组织逐个取）")

    def fund_model(nums):
        return {
            "FSETTLEORGID": [{"FNumber": n} for n in nums],
            "FOrgId": [{"FNumber": n} for n in nums],
            "FStartDate": f"{today} 00:00:00", "FEndDate": f"{today} 23:59:59",
            "FMyCurrency": True, "FInNOut": True, "FNotAudit": False,
            "FAllBankAccount": True, "FAllCashAccount": True,
            "FINCLUDEEMPTY": False, "FIsShowCancelBankAcnt": False,
        }

    budget = []
    seen_accts = set()
    dup_accts = 0
    org_name_by_number = {str(o.get("number")): o.get("name") or ""
                          for o in (orgs or []) if str(o.get("number") or "").strip()}
    targets = org_numbers if org_numbers else [""]
    for n in targets:
        nums = [n] if n else []
        try:
            rows = client.sys_report("CN_FundPositionReport", FUND_FIELDS, fund_model(nums), "")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"资金头寸（组织 {n or '全部'}）取数失败："
                         f"{str(getattr(exc, 'message', exc))[:110]}")
            continue
        for row in rows:
            if not isinstance(row, list) or len(row) < 17:
                continue
            start_bal = f2(row[13]); today_in = f2(row[14])
            today_out = f2(row[15]); today_bal = f2(row[16])
            if abs(start_bal) + abs(today_in) + abs(today_out) + abs(today_bal) == 0:
                continue
            bank_name, acct_name = str(row[1] or ""), str(row[2] or "")
            acct_no = str(row[3] or "").strip()
            key = acct_no or f"{bank_name}|{acct_name}"
            if key in seen_accts:
                dup_accts += 1
                continue
            seen_accts.add(key)
            if n:
                org_number, org_name = n, (org_name_by_number.get(n) or n)
            else:
                pay_org = str(row[4] or "").strip()
                hit = match_org(pay_org, orgs) or match_org(f"{acct_name}|{bank_name}", orgs)
                org_number = (hit or {}).get("number", "")
                org_name = (hit or {}).get("name") or (pay_org or "未归属组织")
            budget.append({
                "org_number": org_number, "org_name": org_name,
                "bank_name": bank_name, "acct_name": acct_name, "acct_no": acct_no,
                "start": start_bal, "income": today_in, "outcome": today_out, "end": today_bal,
            })
    if dup_accts:
        notes.append(f"资金头寸有 {dup_accts} 行账户在不同组织维度下重复出现，已按银行账号去重，未重复相加。")

    grouped = {}
    for a in budget:
        key = a["org_number"] or a["org_name"]
        g = grouped.setdefault(key, {"org_number": a["org_number"], "org_name": a["org_name"],
                                     "accounts": [], "start": 0.0, "income": 0.0,
                                     "outcome": 0.0, "end": 0.0, "subject": 0.0})
        g["accounts"].append(a)
        for k in ("start", "income", "outcome", "end"):
            g[k] += a[k]
    budget_orgs = sorted(grouped.values(), key=lambda x: -abs(x["end"]))
    for o in budget_orgs:
        if o["end"] < -0.01:
            notes.append(f"{o['org_name']} 今日期末头寸为负（{o['end']:,.2f}），"
                         "请确认是内部资金归集账户、透支，还是数据异常。")

    # ---------- B/C. 科目余额（账簿列表动态取） ----------
    books = account_books or []
    if not books:
        p("②/③ 读取账簿列表（科目余额表参数）")
        books = client.discover_account_books()
    # 账簿 → 组织：优先用账簿自带的组织字段，其次用组织名匹配账簿名
    book_org = {}
    for b in books:
        hit = None
        if b.get("org_number") or b.get("org_name"):
            hit = {"number": b.get("org_number") or "", "name": b.get("org_name") or ""}
        if not (hit and hit.get("number")):
            hit = match_org(f"{b.get('name','')} {b.get('number','')}", orgs)
        book_org[b["number"]] = hit or {}
    # 跳过集团账簿（名称含"集团"），除非只有它
    usable = [b for b in books if "集团" not in str(b.get("name") or "")]
    if not usable:
        usable = books
    usable = usable[:max_books]
    # 按用户勾选的组织筛选账簿（账簿自带所属组织编码，可靠）
    if allowed:
        kept = [b for b in usable
                if str(((book_org.get(b["number"]) or {}).get("number")) or "") in allowed]
        if kept:
            if len(kept) != len(usable):
                notes.append(f"科目余额已按所选组织筛选：统计 {len(kept)} 个账簿，"
                             f"排除 {len(usable) - len(kept)} 个不属于所选组织的账簿。")
            usable = kept
        else:
            usable = []
            notes.append("所选组织未匹配到可用账簿，不展示其他组织的余额。")

    def bal_model(y, pd_):
        return {
            "FACCTBOOKID": {"FNumber": ""}, "FCURRENCY": "0",
            "FSTARTYEAR": str(y), "FSTARTPERIOD": str(pd_),
            "FENDYEAR": str(y), "FENDPERIOD": str(pd_),
            "FBALANCELEVEL": "3", "FSHOWDETAIL": True, "FFORBIDBALANCE": True,
            "FNOTPOSTVOUCHER": True, "FDEBITORCREDIT": False, "FBALANCEZERO": True,
            "FNOBUSINESS": False, "FPERIODNOBALANCE": True, "FYEARNOBALANCE": True,
            "FSHOWFULLNAME": True, "FDETAILSHOWACCT": True, "FSHOWDETAILONLY": False,
            "FEXCLUDEADJUSTVCH": False, "FFLEXDEBITORCREDIT": False, "FSHOWFLEXBYCOL": False,
        }

    def fetch_balance(book_no, y, pd_, label):
        model = bal_model(y, pd_)
        model["FACCTBOOKID"] = {"FNumber": book_no}
        try:
            return client.sys_report("GL_RPT_AccountBalance", BAL_FIELDS, model,
                                     scheme_id=scheme_id)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"科目余额表（{label} 账簿 {book_no}）取数失败："
                         f"{str(getattr(exc, 'message', exc))[:100]}")
            return []

    subject_rows = []
    prev_rows = {}

    def split_levels(rows):
        """把报表行拆成「科目级」与「核算维度级」两类。

        实测：FDETAILNUMBER 非空的行是科目的**核算维度明细**（如银行账户），
        不是子科目；科目本身的行 FDETAILNUMBER 为空。所以科目余额要取科目级行，
        再看「哪些科目编码不是别的编码的前缀」来定位叶子科目，避免父子科目重复相加。
        """
        level, dims = {}, []
        for r in rows:
            if not isinstance(r, list) or len(r) < 12:
                continue
            code = str(r[0] or "").strip()
            name = str(r[1] or "").strip()
            dim_no = str(r[2] or "").strip()
            debit, credit = f2(r[6]), f2(r[7])
            occ = debit + credit                  # 本期发生 借 + 贷（发生额总额，保留兼容）
            end = f2(r[10]) - f2(r[11])          # 期末余额 借 − 贷
            if dim_no:
                dims.append({"code": code, "name": name, "dim": dim_no,
                             "dim_name": str(r[3] or "").strip(), "end": end})
            elif code:
                level[code] = {"name": name, "occ": occ, "debit": debit,
                               "credit": credit, "end": end,
                               "begin": f2(r[4]) - f2(r[5]),
                               "valid_balance": all(v is not None and str(v).strip() != "" for v in (r[4], r[5], r[10], r[11])),
                               "valid_flow": all(v is not None and str(v).strip() != "" for v in (r[6], r[7]))}
        return level, dims

    def leaf_codes(level_map, other_level):
        all_codes = set(level_map) | set(other_level)
        return {c for c in all_codes
                if not any(o != c and o.startswith(c) for o in all_codes)}

    swings = []
    capital = []
    for i, b in enumerate(usable, 1):
        p(f"③/③ 科目余额表：{i}/{len(usable)} 个账簿")
        cur = fetch_balance(b["number"], cy, cp, "本期")
        if not cur:
            continue
        prev = fetch_balance(b["number"], py, pp, "上月")
        prev_rows[b["number"]] = prev
        cur_level, cur_dims = split_levels(cur)
        prev_level, _ = split_levels(prev)
        leaves = leaf_codes(cur_level, prev_level)
        org = book_org.get(b["number"]) or {}
        capital.append(working_capital(cur_level, org.get("number"), b["number"], cy, cp))
        fund = 0.0
        leaf = []
        fund_items = []
        for code in sorted(leaves):
            info = cur_level.get(code) or {}
            end = info.get("end", 0.0)
            if code.startswith(FUND_CODES) or any(k in (info.get("name") or "")
                                                 for k in FUND_KEYWORDS):
                fund += end
                fund_items.append({"code": code, "name": info.get("name") or "",
                                   "end": round(end, 2)})
            leaf.append((code, info.get("name", ""), info.get("occ", 0.0), end))
            # 科目波动：**期末余额 vs 上月末余额**，按科目方向语义化
            #   （正 = 余额增加、负 = 余额减少）。
            #   ⚠️ 早期版本用「发生额合计」对比，会出现"本月 0 / 上月 57 万 → 增加 57 万"
            #   这种读不通的语义（本月没做账不代表余额增加了），已改为余额口径。
            pinfo = prev_level.get(code) or {}
            stype, side = subject_side(code, info.get("name") or pinfo.get("name") or "")
            if side == "debit":
                net_cur = info.get("end", 0.0)            # 资产/成本/费用：借−贷
                net_prev = pinfo.get("end", 0.0)
            else:
                net_cur = -info.get("end", 0.0)           # 负债/权益/收入：贷−借
                net_prev = -pinfo.get("end", 0.0)
            if abs(net_cur) < 1000 and abs(net_prev) < 1000:
                continue
            delta = net_cur - net_prev
            if abs(delta) < 5000:
                continue
            swings.append({
                "org_number": org.get("number", ""), "org_name": org.get("name", ""),
                "book_name": b["name"], "code": code,
                "name": info.get("name") or pinfo.get("name") or "",
                "type": stype, "side": side,
                "cur": round(net_cur, 2), "prev": round(net_prev, 2),
                "delta": round(delta, 2),
                "rate": round(delta / abs(net_prev), 4) if abs(net_prev) > 0.01 else None,
            })
        org = book_org.get(b["number"]) or {}
        subject_rows.append({
            "book_number": b["number"], "book_name": b["name"],
            "org_number": org.get("number", ""), "org_name": org.get("name", ""),
            "fund_balance": round(fund, 2), "leaf": leaf, "fund_items": fund_items,
            "leaf_count": len(leaves), "dim_count": len(cur_dims),
            "dims": sorted(cur_dims, key=lambda x: -abs(x["end"]))[:60],
        })

    swings.sort(key=lambda x: -abs(x["delta"]))

    subject_total = round(sum(x["fund_balance"] for x in subject_rows), 2)
    budget_end = round(sum(x["end"] for x in budget_orgs), 2)

    # 资金头寸与科目余额的关系（**不能**相减求"总差额"，两者取数范围不同）
    fund_nums = {o["org_number"] for o in budget_orgs if o["org_number"]}
    both, only_subject, only_fund = [], [], []
    for o in budget_orgs:
        hit = next((s for s in subject_rows if s["org_number"] and
                    s["org_number"] == o["org_number"]), None)
        if hit:
            o["subject"] = hit["fund_balance"]
            o["subject_book"] = hit["book_name"]
            both.append({"org_number": o["org_number"], "org_name": o["org_name"],
                         "fund_end": round(o["end"], 2), "subject": hit["fund_balance"],
                         "delta": round(o["end"] - hit["fund_balance"], 2),
                         "accounts": len(o["accounts"])})
        else:
            only_fund.append({"org_number": o["org_number"], "org_name": o["org_name"],
                              "fund_end": round(o["end"], 2), "accounts": len(o["accounts"])})
    for s in subject_rows:
        if not s["org_number"] or s["org_number"] not in fund_nums:
            only_subject.append({"org_number": s["org_number"], "org_name": s["org_name"],
                                 "book_name": s["book_name"],
                                 "fund_balance": s["fund_balance"]})

    return {
        "period": {"year": year, "month": period, "prev": {"year": py, "month": pp}},
        "org_numbers_used": org_numbers,
        "fund": {
            "source": "金蝶原生报表 CN_FundPositionReport（资金头寸表）",
            "rows": budget_orgs,
            "grand": {
                "start": round(sum(o["start"] for o in budget_orgs), 2),
                "income": round(sum(o["income"] for o in budget_orgs), 2),
                "outcome": round(sum(o["outcome"] for o in budget_orgs), 2),
                "end": budget_end,
            },
            "account_count": len(budget),
            "org_count": len(budget_orgs),
        },
        "subject": {
            "source": "金蝶原生报表 GL_RPT_AccountBalance（科目余额表）",
            "rows": [{"book_number": s["book_number"], "book_name": s["book_name"],
                      "org_number": s["org_number"], "org_name": s["org_name"],
                      "fund_balance": s["fund_balance"], "leaf_count": s.get("leaf_count", 0),
                      "dim_count": s.get("dim_count", 0), "fund_items": s.get("fund_items", []),
                      "dims": s.get("dims", [])} for s in subject_rows],
            "total": subject_total,
            "books_used": len(subject_rows),
            "scheme_id": scheme_id,
        },
        "cross_check": {
            "title": "资金头寸 vs 货币资金科目余额（仅作交叉验证，不可相减求总差额）",
            "both": both,
            "only_in_fund": only_fund,
            "only_in_subject": only_subject,
            "why": "头寸表按「已在系统登记的银行 / 现金账户」取数；科目余额表按「会计科目」取数，"
                   "且依赖会计是否已入账（未达账项）。两者的账户集合与入账时点都不同，"
                   "所以只在同一组织内做参考对照，不做整体差额。",
        },
        "working_capital": capital,
        "swings": swings[:40],
        "org_numbers_used_note": "资金头寸按组织逐个取数后按银行账号去重（多组织一次查会漏主账户，已实测）。",
        "notes": notes,
    }
