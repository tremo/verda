"""The next real action for a listing, derived from facts. Advisory; pure."""
from __future__ import annotations

from verda.policy import Decision

# Purchase-time checklist: fields an official record can settle, still resting on
# something weaker (a statement, card data, the user's own assumption).
UNVERIFIED_GRADES = ('beyan', 'rapor', 'kullanici')
# Not `shared_title`: TKGM's public query shows neither owner nor shares, so there is no official
# source to check it against; "the listing does not say hisseli" is the accepted screening rule.
OFFICIALLY_CHECKABLE = ('parcel_key', 'property_type', 'sale_area_m2', 'archaeological_sit', 'natural_sit', 'access')


def _act(action, why, **extra):
    return {'action': action, 'why': why, **extra}


def next_action(decision: Decision | None, assembled, seen: dict, review_open: int, legacy: str | None) -> dict:
    """`ask:*` entries are message intents; they still need the message eligibility gate."""
    if legacy == 'excluded':
        return _act('none', 'legacy_excluded')
    if seen.get('listing_removed'):
        return _act('none', 'listing_removed')
    if decision is not None and decision.state in ('excluded', 'outside_search'):
        return _act('none', decision.state, reasons=decision.reasons)
    if assembled is not None and assembled.conflicts:
        if assembled.conflicts == ['sale_scope'] and assembled.values.get('scope_question') is True:
            # The user chose to ask the seller what exactly is for sale instead of deciding it.
            return _act('ask:sale_scope' if seen.get('thread_read') else 'sahibinden.read_thread', 'sale_scope_question')
        return _act('human_review', 'conflict', fields=assembled.conflicts)
    if review_open:
        return _act('human_review', 'needs_review', count=review_open)
    if not seen.get('listing_read'):
        return _act('sahibinden.read_listing', 'listing_detail_not_read')
    if assembled is not None and 'parcel_key' not in assembled.values and not assembled.parcels:
        return _act('ask:parcel_identity', 'parcel_identity_missing')
    if assembled is not None and assembled.unchecked_parcels and seen.get('tkgm_looked_up'):
        # A parcel of the sale with no official record yet: a further deed, or a number the user corrected.
        first = assembled.unchecked_parcels[0]
        return _act('tkgm.lookup', 'sale_parcel_unchecked', parcel=first, part=first != assembled.parcels[0])
    missing = [r for r in (decision.reasons if decision else []) if decision.state == 'research_required']
    steps = []
    if 'sale_area' in missing:
        steps.append(('human_review', 'official_parcel_not_found_or_unusable') if seen.get('tkgm_looked_up') else ('tkgm.lookup', 'official_area_and_scope_needed'))
    if 'access' in missing and seen.get('tkgm_looked_up') and not seen.get('corridor_checked'):
        steps.append(('tkgm.corridor', 'cadastral_frontage_geometry_check'))      # official geometry before asking anyone
    if {'natural_sit', 'archaeological_sit'} & set(missing):
        steps.append(('ask:sit' if seen.get('thread_read') else 'sahibinden.read_thread', 'sit_status_unknown'))
    if 'access' in missing and (seen.get('corridor_checked') or not seen.get('tkgm_looked_up')):
        steps.append(('ask:access' if seen.get('thread_read') else 'sahibinden.read_thread', 'cadastral_frontage_unknown'))
    if 'route' in missing:
        steps.append(('geo.route', 'route_to_destination_needed'))
    if 'price' in missing:
        steps.append(('sahibinden.read_listing', 'price_unknown'))
    if steps:
        first, others = steps[0], steps[1:]
        return _act(first[0], first[1], also=list(dict.fromkeys(a for a, _ in others if a != first[0])))
    return _act('user_review', decision.state if decision else 'no_decision')
