"""Code extractors: structured fields only. Free text is read by the model, not by regex."""
from __future__ import annotations

import re
from typing import Any

from verda.facts.model import LISTING, PARCEL, SEARCH, Proposal
from verda.facts.text import fold

TKGM = 'tkgm.lookup'
ROUTE = 'geo.route'
CORRIDOR = 'tkgm.corridor'
NEIGHBORS = 'tkgm.neighbors'
SAYS = 'sit.says'
EXTRACTORS = frozenset({LISTING, SEARCH, TKGM, ROUTE, CORRIDOR, SAYS})
SAYS_VALUE = {'none': 'none', 'first': 'first_degree', 'second': 'second_degree', 'third': 'third_degree'}
_ADA_PARSEL = re.compile(r'^\s*(\d+)\s*/\s*(\d+)\s*$')


def _number(value):
    return value if type(value) in (int, float) and value > 0 else None


def shared_from_title_status(text: str) -> bool | None:
    """Sahibinden's own "Tapu Durumu" row. Unknown wording establishes nothing."""
    folded = fold(text)
    if any(word in folded for word in ('hisse', 'payli', 'müşterek')):
        return True
    if 'müstakil' in folded:
        return False
    return None


_SHARED_WORDS = tuple(fold(w) for w in ('hisse', 'paylı', 'müşterek', 'ortak tapu', 'ortaklı'))


def mentions_shared_title(payload: dict) -> bool:
    """Only a gate on the assumption: if any such word appears, code refuses to assume
    and leaves the reading to the model or the user. It never decides anything itself."""
    texts = [payload.get('title'), payload.get('description'), *(payload.get('attributes') or {}).values()]
    folded = ' '.join(fold(t) for t in texts if isinstance(t, str))
    return any(word in folded for word in _SHARED_WORDS)


def parcel_from_listing(payload: dict) -> str | None:
    raw = payload.get('parcel_key')
    attributes = payload.get('attributes') or {}
    if not isinstance(raw, str) and 'ada no' in attributes and 'parsel no' in attributes:
        raw = f"{attributes['ada no']}/{attributes['parsel no']}"
    match = _ADA_PARSEL.match(raw) if isinstance(raw, str) else None
    if not match:
        return None
    key = f'{int(match[1])}/{int(match[2])}'
    return key if PARCEL.match(key) else None      # 0/0 and friends are placeholders


def extract(tool: str, observation_id: str, mode: str, payload: Any, listing_ref: str | None = None,
            listing_area: float | None = None) -> list[Proposal]:
    """Proposals derived from one observation. Tolerates anything a tool returned."""
    if not isinstance(payload, dict) or payload.get('unavailable') is True:
        return []
    if tool == CORRIDOR:
        # Only positive evidence becomes a fact. "No corridor found" is not "no road".
        if payload.get('verdict') != 'corridor_candidate' or not listing_ref:
            return []
        return [Proposal(listing_ref=listing_ref, field='access', value='corridor_conditional', proposer='code:tkgm_corridor@1', mode=mode,
                         observation_id=observation_id, source_ref='corridor:edges=' + ','.join(map(str, (payload.get('corridor') or {}).get('edges', []))))]
    if tool == SAYS:
        # Only a clear answer becomes a fact. A sliver on the boundary or a missing outline concludes nothing.
        value = SAYS_VALUE.get(payload.get('classification'))
        if value is None or not listing_ref:
            return []
        return [Proposal(listing_ref=listing_ref, field='natural_sit', value=value, proposer='code:says_overlay@1', mode=mode, observation_id=observation_id,
                         source_ref='says:' + str(payload.get('layer')))]
    if tool == NEIGHBORS:
        # A numbered neighbour that is itself a road parcel is road frontage. "No road neighbour" is not "no easement": no fact.
        if payload.get('verdict') != 'road_neighbor' or not listing_ref:
            return []
        return [Proposal(listing_ref=listing_ref, field='access', value='corridor_conditional', proposer='code:tkgm_neighbors@1', mode=mode,
                         observation_id=observation_id, source_ref='neighbors:' + ','.join(payload.get('road_neighbors') or []))]
    if tool == ROUTE:
        if not listing_ref:
            return []
        return [Proposal(listing_ref=listing_ref, field='route_minutes', value=e['minutes'], proposer='code:route@1', mode=mode,
                         observation_id=observation_id, source_ref='engine:' + e['engine'], key=e['engine'])
                for e in payload.get('engines', []) if isinstance(e, dict) and e.get('ok') and _number(e.get('minutes'))]
    if tool == TKGM:
        if payload.get('found') is not True or not listing_ref or not _number(payload.get('official_area_m2')):
            return []
        common = dict(listing_ref=listing_ref, proposer='code:tkgm_record@1', mode=mode, observation_id=observation_id)
        out = [Proposal(field='sale_area_m2', value=payload['official_area_m2'], source_ref='official_area_m2', **common)]
        if isinstance(payload.get('property_type'), str) and payload['property_type'].strip():
            out.append(Proposal(field='property_type', value=payload['property_type'].strip(), source_ref='property_type', **common))
        if isinstance(payload.get('parcel_key'), str) and PARCEL.match(payload['parcel_key']):
            out.append(Proposal(field='parcel_key', value=payload['parcel_key'], source_ref='parcel_key', **common))
        # The parcel is named by its ada/parsel, so the parcel is what is on sale. A small difference from the advertised area is a
        # listing error and the official area stands; a large one means it is not (all of) what is on sale (facts.assemble withholds it).
        out.append(Proposal(field='sale_scope_resolved', value=True, source_ref='official_parcel_identified',
                            **{**common, 'proposer': 'code:scope_match@1'}))
        return out
    if tool == SEARCH:
        out = []
        for row in payload.get('listings', []):
            if not isinstance(row, dict) or not isinstance(row.get('listing_id'), str):
                continue
            common = dict(listing_ref=row['listing_id'], proposer='code:search_rows@1', mode=mode,
                          observation_id=observation_id, source_ref='row:' + row['listing_id'])
            for field, key in (('price_tl', 'price_tl'), ('sale_area_m2', 'area_m2')):
                if _number(row.get(key)):
                    out.append(Proposal(field=field, value=row[key], **common))
        return out
    if tool != LISTING or not isinstance(payload.get('listing_id'), str):
        return []
    common = dict(listing_ref=payload['listing_id'], proposer='code:listing_fields@1', mode=mode, observation_id=observation_id)
    out = []
    for field, key in (('price_tl', 'price_tl'), ('sale_area_m2', 'area_m2')):
        if _number(payload.get(key)):
            out.append(Proposal(field=field, value=payload[key], source_ref=key, **common))
    parcel = parcel_from_listing(payload)
    if parcel:
        out.append(Proposal(field='parcel_key', value=parcel, source_ref='parcel_key', **common))
    zoning = (payload.get('attributes') or {}).get('imar durumu')
    if isinstance(zoning, str) and zoning.strip() and fold(zoning) != fold('Belirtilmemiş'):
        out.append(Proposal(field='property_type', value=zoning.strip(), source_ref='attributes.imar durumu', **common))
    title_status = (payload.get('attributes') or {}).get('tapu durumu')
    shared = shared_from_title_status(title_status) if isinstance(title_status, str) else None
    if shared is not None:
        out.append(Proposal(field='shared_title', value=shared, source_ref='attributes.tapu durumu',
                            quote=title_status, **common))
    elif not mentions_shared_title(payload):
        out.append(Proposal(field='shared_title', value=False, source_ref='absence:no_shared_title_words',
                            basis='absence', **{**common, 'proposer': 'code:no_disclosure@1'}))
    return out
