# -*- coding: utf-8 -*-
"""审批明细的「按单据类型自动识别」：只显示实际有内容的字段。

字段中文名来自 kingdee-data-exporter 的 field-docs（本租户真实映射），
随项目放在 docs/field-docs/；找不到文档时退回少量内置映射，字段名仍按原始 Key 显示。
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SKILL_DOCS = Path.home() / ".workbuddy" / "skills" / "kingdee-data-exporter" / "field-docs"

# 内置兜底：没有字段文档时至少认识这几列。
_FALLBACK = {
    "SEQ": "序号", "FSEQ": "序号", "FEXPENSEITEMID": "费用项目", "FEXPID": "费用项目",
    "FCOSTID": "费用项目", "FORGAMOUNT": "申请金额", "FEXPENSEAMOUNT": "申请报销金额",
    "FEXPSUBMITAMOUNT": "核定报销金额", "FREMARK": "备注",
    "F_TJKT_BASE_QTR": "项目", "F_TJKT_TEXT_QTR": "特殊备注",
    "FENTRYCOSTDEPTID": "费用承担部门", "FEXPENSEDEPTENTRYID": "费用承担部门",
}

# 这些字段是系统internal/link信息，金蝶界面也不当费用明细展示。
_SKIP_KEYS = {
    "FID", "ID", "FENTRYID", "FBILLHEAD", "FENTITY", "FENTITY_LINK", "FLINKID",
    "FENTITY_LINK_FFLOWID", "FENTITY_LINK_FFLOWLINEID", "FENTITY_LINK_FRULEID",
    "FENTITY_LINK_FSTABLEID", "FENTITY_LINK_FSTABLENAME", "FENTITY_LINK_FSBILLID",
    "FENTITY_LINK_FSID", "FENTITY_LINK_FORGAMOUNTOLD", "FENTITY_LINK_FORGAMOUNT",
    "FRANDOMCODE", "FCLIENTID", "FCLIENTSECRET", "FUNIQUE", "FISREVISE",
    "FSRCROWID", "SRCROWID", "FSRCENTRYID", "SRCENTRYID",
}
_SKIP_LABELS = {
    "实体主键", "明细信息", "关联关系表", "业务流程图", "推进路线", "转换规则",
    "源单表内码", "源单表", "源单内码", "源单分录内码", "源单行内码", "源单行号",
    "原始携带量", "修改携带量", "状态修改随机码", "秘钥", "AppID", "单据头",
    "单据编号", "单据状态", "行关闭状态", "明细与发票联动处理标识",
    "明细与发票联动处理关联关系",
    # 用户 2026-11-27 明确不要这两列（费用明细里已有目的地口径）。
    "出发地（费控）", "出发地(费控)", "目的地（费控）", "目的地(费控)",
}
# 按用户给的出差申请单截图排序列；其余字段按字段文档顺序排后面。
_PRIORITY = (
    "序号", "费用项目", "开始日期", "结束日期",
    "出发地", "目的地", "项目",
    "机票/火车票", "机票/火车", "机票", "市内交通费", "其他长途交通费", "过路费（路桥）",
    "住宿费", "出差补助", "其他费用", "申请金额", "申请金额(本位币)",
    "申请报销金额", "核定报销金额", "特殊备注", "备注", "费用承担部门",
)
_NUM_RE = re.compile(r"^[-+]?[\d,]+(?:\.\d+)?%?$")
# 带 ¥ 的列（金额口径）；数值但非金额的列（天数/数量/汇率等）只做千分位。
_MONEY_HINT = ("金额", "税额", "价税", "票价", "交通费", "住宿费", "补助",
               "费用", "机票", "火车票", "单价", "价格", "价")
_NUM_HINT = ("天数", "数量", "汇率", "比例", "税率", "次数")
_TEXT_HINT = ("部门", "项目", "备注", "日期", "目的地", "出发地")


def _doc_dirs():
    dirs = []
    env = os.environ.get("PORTAL_FIELD_DOCS_DIR", "").strip()
    if env:
        dirs += [Path(p) for p in env.split(os.pathsep) if p.strip()]
    dirs += [_HERE / "docs" / "field-docs", _SKILL_DOCS]
    return dirs


@lru_cache(maxsize=None)
def labels(form_id: str) -> dict:
    """返回 {字段Key(大写): 中文名}；Key 顺序即字段文档里的业务顺序。"""
    out = {}
    fid = str(form_id or "").strip()
    if fid:
        for base in _doc_dirs():
            path = base / (fid + ".txt")
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                if line.startswith("#") or ":" not in line:
                    continue
                label, key = line.split(":", 1)
                key, label = key.strip().upper(), label.strip()
                if key and label and key not in out:
                    out[key] = label
            if out:
                break
    merged = dict(_FALLBACK)
    merged.update(out)
    return merged


def _group(key):
    """Fxxx_Id / Fxxx_Name 归到同一列；大小写归一。"""
    text = str(key or "").strip().upper()
    for suffix in ("_NAME", "_ID"):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            break
    return text


def _cell(value):
    """把 View 单元格压成一行文本；空值/0/False 视为没内容。"""
    if value is None or isinstance(value, bool) and not value:
        return ""
    if isinstance(value, bool):
        return "是"
    if isinstance(value, (int, float)):
        if float(value) == 0:
            return ""
        return ("%.6f" % value).rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in ("false", "null", "none"):
            return ""
        return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.]+", text) else text
    if isinstance(value, dict):
        for key in ("Name", "FName", "Value", "FValue"):
            text = _cell(value.get(key))
            if text:
                return text
        for key in ("Number", "FNumber"):
            text = _cell(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and str(item.get("Key")) == "2052":
                return _cell(item.get("Value"))
        for item in value:
            text = _cell(item)
            if text:
                return text
    return ""


def _numeric(values):
    seen = False
    for text in values:
        if not text:
            continue
        clean = text.replace("￥", "").replace("¥", "").replace("，", ",").strip()
        if not _NUM_RE.fullmatch(clean):
            return False
        seen = True
    return seen


def column_flags(label, values):
    """列是否按数值/金额渲染，返回 (num, money)。供各审批路径共用。"""
    hinted = (any(h in label for h in _MONEY_HINT + _NUM_HINT)
              and not any(h in label for h in _TEXT_HINT))
    num = label != "序号" and (hinted or (_numeric(values)
                                          and any("." in x for x in values if x)))
    return bool(num), bool(num and any(h in label for h in _MONEY_HINT))


def detail_view(rows, form_id: str = "", limit: int = 30, checked=None):
    """从明细行自动识别列：只有至少一个单元格有内容才保留。

    checked 是与 rows 等长/同序的核定金额序列（元素可为 dict{entry_id,checked} 或数值），
    提供时在末尾追加「审核核定金额」列；渲染是否可编辑由调用方按场景决定。
    返回 {"columns":[...], "rows":[[文本,...]], "checked":[...]}；没有内容返回 None。
    """
    sample = [r for r in (rows or []) if isinstance(r, dict)][:200]
    if not sample:
        return None
    names = labels(form_id)
    doc_order = {key: i for i, key in enumerate(names)}
    # 先收键、再按 sample 逐行取值：某行缺这个键时补空串，否则列数组会短一截，
    # 按行号取数就串行（空行被跳过后尤其明显）。
    key_order = {}
    first_seen = {}
    for row in sample:
        for key, value in row.items():
            group = _group(key)
            if group in _SKIP_KEYS or "LINK" in group or group.startswith("FENTITY_"):
                continue
            first_seen.setdefault(group, str(key))
            key_order.setdefault(group, {})[str(key)] = True
    groups = {}
    for group, keys in key_order.items():
        for key in keys:
            groups.setdefault(group, {})[key] = [
                _cell(row[key]) if key in row else "" for row in sample
            ]
    picked = []
    for group, by_key in groups.items():
        label = names.get(group) or names.get("F" + group) or first_seen[group]
        if label in _SKIP_LABELS:
            continue
        # 同一列取命中最多的那个 Key（Fxxx_Name 与 Fxxx_Id 只想留一个）。
        key = max(by_key, key=lambda k: (sum(bool(x) for x in by_key[k]),
                                         k not in ("_Name", "_ID")))
        values = by_key[key]
        if not any(values):
            continue
        rank = next((i for i, name in enumerate(_PRIORITY) if name == label), None)
        if rank is None:
            rank = 100 + doc_order.get(group, doc_order.get("F" + group, 500))
        picked.append((rank, label, values))
    if not picked:
        return None
    picked.sort(key=lambda x: x[0])
    columns, all_values = [], []
    for _, label, values in picked[:max(1, limit)]:
        num, money = column_flags(label, values)
        columns.append({"t": label, "num": num, "money": money})
        all_values.append(values)
    with_checked = checked is not None
    if with_checked:
        columns.append({"t": "审核核定金额", "num": True, "money": True, "edit": True})
    body, check_col = [], []
    for i in range(len(sample)):
        line = [vals[i] if i < len(vals) else "" for vals in all_values]
        if not any(line):
            continue
        body.append(line)
        if with_checked:
            item = checked[i] if i < len(checked) else None
            if isinstance(item, dict):
                check_col.append(dict(item))
            else:
                check_col.append({"entry_id": "", "checked": item})
    if not body:
        return None
    out = {"columns": columns, "rows": body}
    if with_checked:
        for i, line in enumerate(body):
            line.append(str(check_col[i].get("checked", "")))
        out["checked"] = check_col
    return out
