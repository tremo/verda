"""Acceptance rules. The model proposes; this module decides, with plain code.

A claim becomes a fact only if its source exists, belongs to the same listing and
mode, says what is claimed (verbatim quote), is not hedged or truncated, and comes
from the right speaker. Grades follow the source, not the proposer.
"""
from __future__ import annotations

import re
from typing import Any

from verda.facts.model import (EXCLUDING, FIELDS, LISTING, SEARCH, SOURCE_KIND, TEXT_TOOLS, THREAD,
                               TOOL_GRADE, Proposal, Verdict, accept, reject, review, validate_value)
from verda.facts.text import MAX_QUOTE, MIN_QUOTE, fold, has_ellipsis, is_hedged, locate

VERIFIER = 'v1'


def payload_listing_ids(tool: str, payload: Any):
    """Listings an observation is actually about, read from its content, not its label."""
    if not isinstance(payload, dict):
        return None
    if tool in {LISTING, THREAD}:
        value = payload.get('listing_id')
        return {value} if isinstance(value, str) else set()
    if tool == 'sit.says':
        value = payload.get('listing_id')
        return {value} if isinstance(value, str) else set()
    if tool == SEARCH:
        return {r['listing_id'] for r in payload.get('listings', []) if isinstance(r, dict) and isinstance(r.get('listing_id'), str)}
    return None


def source_texts(tool: str, payload: Any) -> list[dict]:
    """Quotable texts of an observation: [{ref, role, text, truncated}]."""
    if not isinstance(payload, dict):
        return []
    texts = []
    if tool == LISTING:
        for ref in ('title', 'description', 'page_text'):
            if isinstance(payload.get(ref), str):
                texts.append({'ref': ref, 'role': 'listing', 'text': payload[ref], 'truncated': False})
        for label, value in (payload.get('attributes') or {}).items():
            if isinstance(label, str) and isinstance(value, str):
                texts.append({'ref': 'attributes.' + label, 'role': 'listing', 'text': f'{label}: {value}', 'truncated': False})
    elif tool == THREAD:
        # Contract for the thread reader: messages=[{id, role: seller|user, text, truncated?}].
        for message in payload.get('messages', []):
            if isinstance(message, dict) and isinstance(message.get('text'), str):
                texts.append({'ref': 'message:' + str(message.get('id')), 'role': message.get('role'),
                              'text': message['text'], 'truncated': bool(message.get('truncated'))})
    elif tool == SEARCH:
        for row in payload.get('listings', []):
            if isinstance(row, dict) and isinstance(row.get('title'), str):
                texts.append({'ref': 'row:' + str(row.get('listing_id')), 'role': 'listing', 'text': row['title'], 'truncated': False})
    return texts


def _check_quote(p: Proposal, observation: dict) -> tuple[Verdict | None, str | None]:
    """Returns (verdict-if-failed, folded sentence containing the quote)."""
    quote = p.quote or ''
    if len(quote) > MAX_QUOTE:
        return reject('quote_too_long'), None
    if has_ellipsis(quote):
        return reject('quote_truncated'), None
    if len(fold(quote)) < MIN_QUOTE:
        return review('quote_too_short'), None
    tool = observation['tool']
    texts = source_texts(tool, observation['payload'])
    if tool == THREAD and not p.source_ref:
        return reject('source_ref_required'), None
    if p.source_ref:
        named = [t for t in texts if t['ref'] == p.source_ref]
        # A wrong message id is the proposer's slip, not a lie about the text: the quote is still looked up, word for word, in the seller's
        # messages of this very observation. If it is not there the proposal fails as before.
        texts = named or texts
        if not texts:
            return reject('source_not_found'), None
    for source in texts:
        found = locate(quote, source['text'])
        if found is None:
            continue
        if tool == THREAD and source['role'] != 'seller':
            return reject('quote_not_from_seller'), None
        raw, folded = found
        if source['truncated'] or has_ellipsis(raw[-3:]):
            return reject('source_truncated'), None
        return None, folded
    return reject('quote_not_in_source'), None


def verify(p: Proposal, observation: dict | None) -> Verdict:
    spec = FIELDS.get(p.field)
    if spec is None:
        return reject('unknown_field')
    problem = validate_value(spec, p.value)
    if problem:
        return reject(problem)
    if p.proposer == 'user':
        return accept('kullanici', 'user_decision')
    if observation is None:
        return reject('observation_missing')
    if observation['mode'] != p.mode:
        return reject('mode_mismatch')
    tool = observation['tool']
    if tool not in spec.tools:
        return reject('tool_not_allowed_for_field')
    about = payload_listing_ids(tool, observation['payload'])
    if (p.listing_ref not in about) if about is not None else (observation.get('listing_ref') != p.listing_ref):
        return reject('listing_mismatch')
    model = p.proposer.startswith('llm:')
    if p.basis == 'absence':
        # "The listing does not say hisseli, so it is not hisseli": a screening assumption.
        # Only code may make it, only for the clearing value, only from a full listing read,
        # and it ranks below any explicit statement.
        if model or p.field != 'shared_title' or p.value is not False or p.quote:
            return reject('absence_not_allowed')
        return accept('rapor', 'no_disclosure_assumed') if tool == LISTING else reject('absence_needs_full_listing')
    if model and tool not in TEXT_TOOLS:
        return reject('model_not_allowed_for_tool')   # numbers from tools are extracted by code
    if model and not p.quote:
        return reject('quote_required')
    sentence = None
    if p.quote:
        failed, sentence = _check_quote(p, observation)
        if failed:
            return failed
        quoted = fold(p.quote)
        # "doğal" counts only next to "sit" ("doğal sit", "doğal ve arkeolojik sit"): in "doğal konumunda zeytinlik" it is just an adjective.
        natural = bool(re.search(r'doğal(?:\s+\S+){0,3}\s+sit', quoted))
        archaeological = bool(re.search(r'arkeolojik(?:\s+\S+){0,3}\s+sit|arkeolojik', quoted))
        if (p.field == 'archaeological_sit' and natural and not archaeological) or \
           (p.field == 'natural_sit' and archaeological and not natural):
            return review('sit_type_unclear')      # the words speak of the other kind (or a general "sit"): a person decides, the model is not simply refused
        if is_hedged(sentence):
            return review('hedged')
    if model and (p.field, p.value) in EXCLUDING and p.subject != 'this_parcel':
        return review('subject_not_this_parcel')
    return accept(TOOL_GRADE[tool])


def source_kind(tool: str | None, proposer: str) -> str:
    return 'user' if proposer == 'user' else SOURCE_KIND.get(tool or '', 'listing')
