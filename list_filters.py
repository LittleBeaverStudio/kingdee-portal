"""Validated list filters translated to Kingdee query conditions before row limits."""
from datetime import date
import re


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def contains(field, value):
    # SQL LIKE metacharacters are literal user text, never executable filters.
    value = str(value).replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
    return field + " like " + quote("%" + value + "%")


def parse(args):
    out = {}
    for key in ("q", "person", "kind", "status", "borrow", "date_from", "date_to"):
        value = str(args.get(key) or "").strip()
        if len(value) > 100:
            raise ValueError("筛选条件过长")
        out[key] = value
    for key in ("date_from", "date_to"):
        if out[key]:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", out[key]):
                raise ValueError("日期格式应为 YYYY-MM-DD")
            date.fromisoformat(out[key])
    if out["date_from"] and out["date_to"] and out["date_from"] > out["date_to"]:
        raise ValueError("开始日期不能晚于结束日期")
    if out["status"] not in ("", "Z", "A", "B", "C", "D", "draft"):
        raise ValueError("单据状态筛选无效")
    if out["kind"] not in ("", "expense", "travel") or out["borrow"] not in ("", "1", "0"):
        raise ValueError("单据类型或借款筛选无效")
    return out


def conditions(filters, *, person_field, date_field, orgs=None, application=False):
    f = filters or {}
    parts = []
    if f.get("q"):
        parts.append(contains("FBillNo", f["q"]))
    if f.get("person"):
        parts.append(contains(person_field, f["person"]))
    if orgs:
        parts.append("FOrgId.FNumber in (" + ",".join(quote(x) for x in orgs) + ")")
    if f.get("date_from"):
        parts.append(date_field + ">=" + quote(f["date_from"]))
    if f.get("date_to"):
        # Date fields can contain times. Include the entire final day.
        from datetime import timedelta
        end = date.fromisoformat(f["date_to"]) + timedelta(days=1)
        parts.append(date_field + "<" + quote(end.isoformat()))
    if application:
        if f.get("borrow"):
            parts.append("FIsBorrow=" + f["borrow"])
    elif f.get("status"):
        parts.append("FDocumentStatus in ('Z','A','D')" if f["status"] == "draft"
                     else "FDocumentStatus=" + quote(f["status"]))
    return parts
