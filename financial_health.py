"""Legacy account-level working-capital inputs, retained for old snapshots."""
import calendar
import datetime
import math


def working_capital(level, org, book, year, month):
    groups = {'ar': ('1122',), 'ap': ('2202',),
              'inventory': ('1401', '1402', '1403', '1404', '1405', '1406', '1407', '1408', '1411'),
              'revenue': ('6001', '6051'), 'cost': ('6401', '6402')}
    result = {'org': str(org or ''), 'book': book, 'year': year, 'month': month}
    today = datetime.date.today()
    result['days'] = today.day if (year, month) == (today.year, today.month) else calendar.monthrange(year, month)[1]
    for name, prefixes in groups.items():
        codes = [code for code in level if code.startswith(prefixes)]
        leaves = [code for code in codes if not any(other != code and other.startswith(code) for other in codes)]
        rows = [level[code] for code in leaves]
        required = ('begin', 'end') if name in ('ar', 'ap', 'inventory') else ('debit', 'credit')
        valid_key = 'valid_balance' if name in ('ar', 'ap', 'inventory') else 'valid_flow'
        if not rows or not all(r.get(valid_key, True) for r in rows) or not all(all(isinstance(r.get(k), (int, float)) and math.isfinite(r[k]) for k in required) for r in rows):
            result[name] = None
            continue
        if name in ('ar', 'ap', 'inventory'):
            sign = -1 if name == 'ap' else 1
            result[name] = round(sum((r['begin'] + r['end']) / 2 * sign for r in rows), 2)
        else:
            sign = -1 if name == 'revenue' else 1
            result[name] = round(sum((r['debit'] - r['credit']) * sign for r in rows), 2)
    return result
