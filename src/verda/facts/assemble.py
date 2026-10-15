"""Accepted claims -> the facts `evaluate` may use. Pure; deterministic."""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from verda.facts.model import FIELDS
from verda.facts.text import fold
from verda.policy import GRADES, Policy

IDENTITY = 'parcel_identity'
SCOPE = 'sale_scope'
SCOPE_MISMATCH = 0.20       # official area this far from the advertised one: the parcel looked up is not (all of) what is on sale


@dataclass
class Assembled:
    values: dict = field(default_factory=dict)          # field -> value (list for multi-valued)
    chosen: dict = field(default_factory=dict)          # field -> [claim rows used]
    conflicts: list = field(default_factory=list)       # subjects withheld from evaluation
    leads: dict = field(default_factory=dict)           # field -> claims too weak to establish it
    contradictions: list = field(default_factory=list)  # the user's decision against official evidence: both stay visible
    errors: list = field(default_factory=list)          # what a listing or seller stated that the official record corrects
    parcels: list = field(default_factory=list)         # the parcels this sale is (the main one first); empty while unknown
    unchecked_parcels: list = field(default_factory=list)   # parcels of the sale with no official record yet: to look up

    def basis(self) -> dict:
        return {f: min((c['grade'] for c in cs), key=GRADES.index) for f, cs in self.chosen.items()}


def _same(a, b, field='') -> bool:
    if field == 'sale_area_m2' and type(a) in (int, float) and type(b) in (int, float):
        return abs(a - b) <= max(2.0, 0.02 * max(a, b))      # "1.053" and 1053.41 are the same parcel
    if field == 'property_type' and isinstance(a, str) and isinstance(b, str):
        return fold(a) == fold(b)
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _pick(field_name, claims, allowed, withhold_parcel):
    name = field_name
    """Returns (values, rows, conflict, leads, contradictions) for one field."""
    spec = FIELDS[field_name]
    if withhold_parcel:
        claims = [c for c in claims if c['scope'] != 'parcel']
    usable = [c for c in claims if c['grade'] in allowed]
    leads = [c for c in claims if c['grade'] not in allowed]
    # A newer claim of the same source and grade replaces an older one (price changes
    # are versions, not conflicts); older rows stay in the log.
    groups: dict = {}
    for c in sorted(usable, key=lambda c: c['seq']):
        groups.setdefault((c['source_kind'], c['grade'], c['key'] if spec.multi else None), []).append(c)
    # The newest observation of a source is current. Two parts of that same observation that
    # disagree (a title row against the description) are a conflict, not a newer version.
    current = []
    for group in groups.values():
        newest = group[-1]
        current.extend([c for c in group if c.get('observation_id') == newest['observation_id']]
                       if newest.get('observation_id') else [newest])
    if not current:
        return None, [], False, leads, []
    if spec.multi:
        rows = sorted(current, key=lambda c: (c['key'] or '', c['seq']))
        return [c['value'] for c in rows], rows, False, leads, []
    best = min(GRADES.index(c['grade']) for c in current)
    top = [c for c in current if GRADES.index(c['grade']) == best]
    if any(not _same(c['value'], top[0]['value'], name) for c in top):
        return None, [], True, leads, []
    winner = max(top, key=lambda c: c['seq'])
    # Card data (`rapor`) is provisional by definition; differing from it is not a contradiction.
    contradictions = [c for c in current if c['grade'] != 'rapor' and not _same(c['value'], winner['value'], name)]
    return winner['value'], [winner], False, leads, contradictions


def assemble(claims: list[dict], policy: Policy | None = None) -> Assembled:
    """`claims` are accepted claim rows with parsed `value`, ordered or not."""
    policy = policy or Policy()
    by_field: dict[str, list[dict]] = {}
    for c in claims:
        if c['field'] in FIELDS:
            by_field.setdefault(c['field'], []).append(c)
    dest = getattr(policy, 'destination', None)
    if dest and dest.get('verified_by') == 'user':
        # Policy-level user decision, shown like any other claim so the basis stays visible.
        by_field.setdefault('destination_verified', []).append({
            'id': 'policy-destination', 'seq': 0, 'field': 'destination_verified', 'value': True, 'grade': 'kullanici',
            'source_kind': 'user', 'key': None, 'scope': 'listing', 'observation_id': None, 'proposer': 'policy',
            'source_ref': 'policy.destination', 'quote': f"{dest['name']} {dest['lat']},{dest['lon']} (kullanıcı verdi {dest.get('given_at', '')})"})
    out = Assembled()
    _scope_to_sale(by_field, policy, out)
    # Identity first: a disputed parcel withholds everything bound to the parcel,
    # but not what the listing itself says (price, its own statements).
    withhold = False
    for name in ['parcel_key'] + [f for f in FIELDS if f != 'parcel_key']:
        allowed = policy.field_grades.get(name, ())
        value, rows, conflict, leads, contradictions = _pick(name, by_field.get(name, []), allowed, withhold)
        if name == 'parcel_key' and conflict:
            withhold = True
        if conflict:
            out.conflicts.append(IDENTITY if name == 'parcel_key' else name)
        if rows:
            out.values[name], out.chosen[name] = value, rows
        if leads:
            out.leads[name] = leads
        for other in contradictions:
            winner = rows[0]
            entry = {'field': name, 'chosen': {'claim_id': winner['id'], 'grade': winner['grade'], 'value': winner['value']},
                     'other': {'claim_id': other['id'], 'grade': other['grade'], 'value': other['value'], 'quote': other['quote']}}
            if winner['grade'] in ('resmi', 'bagimsiz'):
                out.errors.append(entry)            # the official record is right; the statement was wrong
            else:
                out.contradictions.append({**entry, 'reopen': winner['grade'] == 'kullanici' and other['grade'] in ('resmi', 'bagimsiz')})
    _sum_parcels(out)
    _check_scope(out)
    return out


def _scope_to_sale(by_field, policy, out):
    """Which parcels this sale is, before anything else is picked: the user's parcel or parcel list first, then a stated list, else the
    chosen parcel. Records about any other parcel are set aside: 1336424878 named a road parcel (894/3, 222 m2); once the user gave the
    real one, the road parcel's area, sit check and corridor must not decide the listing."""
    def pick(name):
        value, rows, conflict, _, _ = _pick(name, by_field.get(name, []), policy.field_grades.get(name, ()), False)
        return (value, rows[0]['grade']) if rows and not conflict else (None, None)
    main, main_grade = pick('parcel_key')
    listed, listed_grade = pick('sale_parcels')
    if listed and main_grade == 'kullanici' and listed_grade != 'kullanici' and main not in listed:
        listed = None                                   # the user's own parcel outranks a stated list that does not contain it
    parcels = ([main] if main and (not listed or main in listed) else []) + [p for p in (listed or []) if p != main]
    if listed and not main:
        parcels = list(listed)
    out.parcels = parcels
    if not parcels:
        return
    for name, claims in by_field.items():
        by_field[name] = [c for c in claims if c.get('parcel') is None or c.get('parcel') in parcels]


def _sum_parcels(out):
    """The sale area of a several-deed sale is the sum of its parcels' official areas; a parcel not looked up yet leaves it open.
    For a single parcel, a parcel with no official record yet (a corrected number) is named so it is looked up."""
    areas = {c.get('key'): c for c in out.chosen.get('parcel_area_m2', []) if c.get('key')}
    official = (out.chosen.get('sale_area_m2') or [{}])[0]
    if official.get('grade') == 'resmi' and official.get('parcel') and official['parcel'] not in areas:
        areas[official['parcel']] = official
    out.unchecked_parcels = [p for p in out.parcels if p not in areas]
    if len(out.parcels) < 2:
        return
    stated = [e['other']['value'] for e in out.errors if e['field'] == 'sale_area_m2']
    stated += [c['value'] for c in out.contradictions if c['field'] == 'sale_area_m2']
    out.errors = [e for e in out.errors if e['field'] != 'sale_area_m2']
    out.contradictions = [c for c in out.contradictions if c['field'] != 'sale_area_m2']
    if out.unchecked_parcels:
        for name in ('sale_area_m2', 'sale_scope_resolved'):       # one deed's area is not the sale's: wait for the others
            out.values.pop(name, None)
            out.chosen.pop(name, None)
        return
    rows = [areas[p] for p in out.parcels]
    total = round(sum(float(r['value']) for r in rows), 2)
    out.values['sale_area_m2'], out.chosen['sale_area_m2'] = total, rows
    for value in stated:
        if not _same(value, total, 'sale_area_m2'):
            out.errors.append({'field': 'sale_area_m2', 'chosen': {'claim_id': rows[0]['id'], 'grade': 'resmi', 'value': total},
                               'other': {'claim_id': None, 'grade': 'beyan', 'value': value, 'quote': None}})


def _check_scope(out: Assembled) -> None:
    """The TKGM record says "this parcel is what is on sale" whenever the parcel is found. That holds only when the areas roughly agree: a
    listing of two title deeds (1252932673: 2582 + 678 m2, only 678 looked up), a wrong parcel number (1336424878: 4540 m2 advertised, 222 m2
    official) or a share sale (1216510607: 1318 of 2606 m2) is not settled by it, and must never be excluded or priced on the official area.
    Then the scope is withheld as a conflict: a person decides, the minimum-area rule does not fire."""
    if out.values.get('sale_scope_resolved') is not True or (out.chosen.get('sale_scope_resolved') or [{}])[0].get('grade') == 'kullanici':
        return
    official = out.values.get('sale_area_m2')
    stated = [e['other']['value'] for e in out.errors if e['field'] == 'sale_area_m2']
    if type(official) in (int, float) and any(type(s) in (int, float) and s > 0 and abs(official - s) / max(official, s) > SCOPE_MISMATCH for s in stated):
        out.values.pop('sale_scope_resolved')
        out.chosen.pop('sale_scope_resolved', None)
        out.conflicts.append(SCOPE)
