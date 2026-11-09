"""Merge edits without deleting authorizations outside the submitted organization scope."""
import copy
import secrets


def org_codes(value):
    return [x.strip() for x in str(value or '').replace('，', ',').split(',') if x.strip()]


def merge_org_rows(companies, rows):
    result = copy.deepcopy(companies)
    for row in rows:
        no, client_id = str(row.get('org_no') or '').strip(), str(row.get('client_id') or '').strip()
        if not no or not client_id:
            continue  # Empty cells never mean deletion; deletion is a separate explicit action.
        matches = [co for co in result if no in org_codes(co.get('kingdee_orgs'))]
        old = next((co for co in matches if co.get('id') == row.get('id')), None) or (matches[0] if matches else None)
        same = next((co for co in result if co.get('client_id') == client_id), None)
        if same:
            target = same
        elif old and len(org_codes(old.get('kingdee_orgs'))) == 1:
            target = old
        else:
            target = copy.deepcopy(old) if old else {}
            target.update(id='c-' + secrets.token_hex(6), kingdee_orgs='')
            result.append(target)
        codes = org_codes(target.get('kingdee_orgs'))
        for co in matches:
            if co is not target:
                co['kingdee_orgs'] = ','.join(x for x in org_codes(co.get('kingdee_orgs')) if x != no)
        if no not in codes:
            codes.append(no)
        target.update(client_id=client_id, kingdee_orgs=','.join(codes))
        for field in ('ghf_mc', 'tax_no', 'client_secret', 'eid'):
            value = str(row.get(field) or '').strip()
            if value:
                target[field] = value
    return result
