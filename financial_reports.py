"""Direct KDS financial statements, using the employee's existing Kingdee session.

KDS request and kdspread decoding adapted from kingdee-data-exporter 2.4.3,
LittleBeaverStudio (https://littlebeaver.top). See vendor/kingdee-data-exporter-LICENSE.md.
No account-balance/voucher fallback: the dashboard must reconcile to the statements.
"""
import calendar
import datetime
import json
import math
import re
import unicodedata

REPORT_PATH = 'Kingdee.BOS.KDS.ServiceFacade.ServicesStub.KDSReportAPIStub.GetReportData.common.kdsvc'
DEFAULT_MODEL = {'ReportType': 1, 'ReportNumber': 'BBMB0001', 'AcctSystemNumber': 'KJHSTX01_SYS',
                 'AcctPolicyNumber': 'KJZC01_SYS', 'CurrencyNumber': 'PRE001', 'CurrUnitNumber': 'JEDW01_SYS',
                 'CycleType': 4, 'DataType': 'Json', 'ResultType': '0'}
BALANCE_ITEMS = {'ar': '应收账款', 'inventory': '存货', 'ap': '应付账款'}
FLOW_ITEMS = {'revenue': '营业收入', 'cost': '营业成本'}


class StatementError(ValueError):
    pass


def number(value):
    if value is None or isinstance(value, bool) or str(value).strip() in ('', '-', '—', '***'):
        return None
    text = unicodedata.normalize('NFKC', str(value)).strip().replace(',', '').replace(' ', '')
    if text.startswith('(') and text.endswith(')'):
        text = '-'+text[1:-1]
    try:
        result = float(text)
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def text(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or '')))


def decode_sheets(result):
    for _ in range(6):
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (TypeError, ValueError):
                raise StatementError('财报返回格式无法解析') from None
        elif isinstance(result, dict) and 'status' in result:
            if str(result['status']).lower() not in ('0', 'true', 'success'):
                raise StatementError('金蝶财报接口未授权、模板不可用或尚未生成')
            result = result.get('result')
        else:
            break
    sheets = []
    def walk(node):
        if isinstance(node, dict):
            if node.get('xtype') == 'kdspread' and isinstance(node.get('data'), dict):
                for cells in node['data'].get('data') or []:
                    grid = {}
                    for cell in cells:
                        if not isinstance(cell, list) or len(cell) < 3:
                            continue
                        row, col = int(cell[0]), int(cell[1])
                        if not (0 <= row <= 4096 and 0 <= col <= 200):
                            raise StatementError('财报单元格范围异常')
                        grid.setdefault(row, {})[col] = cell[2]
                    if grid:
                        sheets.append(grid)
                return
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(result)
    if not sheets:
        raise StatementError('未取得财报工作表')
    return sheets


def statement(sheets, title, org_name, year, month):
    matches = [g for g in sheets if any(text(v) == title for row, cols in g.items() if row < 3 for v in cols.values())]
    if len(matches) != 1:
        raise StatementError(title+'未取得唯一工作表')
    grid = matches[0]
    head = [str(v or '') for row, cols in grid.items() if row < 4 for v in cols.values()]
    dates = [tuple(map(int, m)) for value in head for m in re.findall(r'(\d{4})年\s*(\d{1,2})月', value)]
    if not dates or any(d != (year, month) for d in dates):
        raise StatementError(title+'的期间与请求月份不一致')
    names = [re.sub(r'^.*?编制单位\s*[:：]\s*', '', v).strip() for v in head if '编制单位' in v]
    if not org_name or len(names) != 1 or text(names[0]) != text(org_name):
        raise StatementError(title+'的编制单位与组织不一致或无法核实')
    units = [text(v).split('单位:')[-1] for v in head if text(v).startswith('单位:')]
    scale = {'元': 1, '千元': 1000, '万元': 10000, '亿元': 100000000}
    if len(units) != 1 or units[0] not in scale:
        raise StatementError(title+'的金额单位无法核实')
    return grid, scale[units[0]]


def extract(grid, item, headers, scale):
    hits = []
    for row, cols in grid.items():
        for col, value in cols.items():
            label = re.sub(r'^[一二三四五六七八九十\d]+[、.．]', '', text(value))
            label = re.sub(r'^(加|减)[:：]', '', label)
            if label != item:
                continue
            # Locate the column by its header inside this asset/liability block.
            choices = sorted({c for r, cs in grid.items() if r < row and r < 8 for c, v in cs.items()
                              if col < c <= col+3 and text(v) in headers})
            if len(choices) != 1:
                hits.append(None)
            else:
                val = number(cols.get(choices[0]))
                hits.append(val*scale if val is not None else None)
    return hits[0] if len(hits) == 1 else None


def parse_statement(result, org, org_name, year, month):
    sheets = decode_sheets(result)
    balance, bs_scale = statement(sheets, '资产负债表', org_name, year, month)
    profit, pl_scale = statement(sheets, '利润表', org_name, year, month)
    row = {'org': str(org), 'year': year, 'month': month, 'period': f'{year}-{month:02}',
           'days': calendar.monthrange(year, month)[1], 'basis': 'month'}
    for key, item in BALANCE_ITEMS.items():
        row[key+'_end'] = extract(balance, item, ('期末余额', '期末数'), bs_scale)
        row[key+'_year_begin'] = extract(balance, item, ('上年年末余额', '年初余额', '年初数'), bs_scale)
    for key, item in FLOW_ITEMS.items():
        row[key] = extract(profit, item, ('本期金额', '本月金额', '本月数', '本期数'), pl_scale)
        row[key+'_ytd'] = extract(profit, item, ('本年金额', '本年累计金额', '本年累计数', '本年累计'), pl_scale)
    return row


def periods_before(year, month, today=None, count=12):
    today = today or datetime.date.today()
    anchor = min(datetime.date(int(year), int(month), 1), today.replace(day=1))
    last = anchor - datetime.timedelta(days=1)
    result = []
    for _ in range(count):
        result.append((last.year, last.month))
        last = last.replace(day=1)-datetime.timedelta(days=1)
    return list(reversed(result))


def assemble(raw_rows, periods):
    """Month opening = preceding balance sheet close; YTD uses year opening."""
    unique = {}
    for row in raw_rows:
        key = (row['org'], row['year'], row['month'])
        if key in unique:
            raise StatementError('同一组织同月出现多份财报')
        unique[key] = row
    history, ytd = [], []
    for row in raw_rows:
        year, month = row['year'], row['month']
        if (year, month) not in periods:
            continue
        previous = datetime.date(year, month, 1)-datetime.timedelta(days=1)
        prior = unique.get((row['org'], previous.year, previous.month)) or {}
        monthly = dict(row)
        annual = {k: row[k] for k in ('org', 'year', 'month', 'period')}
        annual.update(basis='ytd', days=(datetime.date(year, month, calendar.monthrange(year, month)[1])-datetime.date(year, 1, 1)).days+1)
        for key in BALANCE_ITEMS:
            opening = row.get(key+'_year_begin') if month == 1 else prior.get(key+'_end')
            closing = row.get(key+'_end')
            monthly[key+'_begin'] = opening
            monthly[key] = (opening+closing)/2 if opening is not None and closing is not None else None
            annual[key] = (row[key+'_year_begin']+closing)/2 if row.get(key+'_year_begin') is not None and closing is not None else None
        for key in FLOW_ITEMS:
            annual[key] = row.get(key+'_ytd')
        history.append(monthly)
        ytd.append(annual)
    history.sort(key=lambda r: (r['period'], r['org']))
    ytd.sort(key=lambda r: (r['period'], r['org']))
    return history, ytd


def build(client, orgs, org_numbers, year, month, progress=None):
    p = progress or (lambda *_: None)
    periods = periods_before(year, month)
    extra = periods_before(year, month, count=13)
    names = {str(o['number']): o.get('name') for o in orgs if o.get('number')}
    ids = list(dict.fromkeys(map(str, org_numbers)))
    raw_rows, errors = [], []
    for i, org in enumerate(ids):
        p(f'财报营运资金：{i+1}/{len(ids)} 个组织 · 月度报表后台加载')
        for y, m in reversed(extra):
            if not names.get(org):
                errors.append({'org': org, 'reason': '组织名称未知，无法核实财报编制单位'})
                break
            model = {**DEFAULT_MODEL, 'OrgNumber': org, 'Year': y, 'Period': m}
            try:
                raw = client._post(REPORT_PATH, {'parameters': [json.dumps(model, ensure_ascii=False)]}, timeout=90)
                raw_rows.append(parse_statement(raw, org, names[org], y, m))
            except Exception as exc:
                reason = str(exc) if isinstance(exc, StatementError) else '财报读取失败，请核对本人权限、报表模板与期间'
                errors.append({'org': org, 'period': f'{y}-{m:02}', 'reason': reason})
    history, ytd = assemble(raw_rows, set(periods))
    grouped_errors = {}
    for error in errors:
        key = (error['org'], error['reason'])
        group = grouped_errors.setdefault(key, {**error, 'missing_periods': []})
        if error.get('period'):
            group['missing_periods'].append(error['period'])
    return {'working_capital_history': history, 'working_capital_ytd': ytd,
            'capital_errors': list(grouped_errors.values()), 'capital_pending': False, 'capital_periods': [f'{y}-{m:02}' for y, m in periods],
            'capital_source': '金蝶财报 · 资产负债表 + 利润表', 'capital_report_number': DEFAULT_MODEL['ReportNumber']}
