"""Field vocabulary, proposals and verdicts. Pure data, no I/O."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any

SEARCH = 'sahibinden.search'
LISTING = 'sahibinden.read_listing'
THREAD = 'sahibinden.read_thread'
SAYS = 'sit.says'                                # the ministry's sit layer against the parcel outline (agency/says.py)
TEXT_TOOLS = frozenset({LISTING, THREAD})      # tools whose payload carries human text
NATURAL_SIT = ('none', 'first_degree', 'second_degree', 'third_degree', 'archival_exception')
ACCESS = ('direct', 'existing_easement', 'corridor_conditional', 'none')
PARCEL = re.compile(r'^(?:0|[1-9]\d{0,5})/[1-9]\d{0,5}$')     # ada/parsel; ada 0 is a parcel outside any block (TKGM shows an empty ada); 0/0 is a placeholder


@dataclass(frozen=True)
class FieldSpec:
    kind: str                       # number | bool | enum | parcel
    tools: frozenset                # tools that may source the field (users may always)
    values: tuple = ()
    multi: bool = False             # several simultaneous values (route engines)


FIELDS: dict[str, FieldSpec] = {
    'price_tl': FieldSpec('number', frozenset({SEARCH}) | TEXT_TOOLS),
    'sale_area_m2': FieldSpec('number', frozenset({SEARCH, 'tkgm.lookup'}) | TEXT_TOOLS),
    'sale_scope_resolved': FieldSpec('bool', frozenset({'tkgm.lookup'})),
    'parcel_key': FieldSpec('parcel', frozenset({'tkgm.lookup'}) | TEXT_TOOLS),
    'property_type': FieldSpec('text', frozenset({'tkgm.lookup'}) | TEXT_TOOLS),
    'shared_title': FieldSpec('bool', frozenset({'tkgm.lookup'}) | TEXT_TOOLS),
    'archaeological_sit': FieldSpec('bool', TEXT_TOOLS),
    'natural_sit': FieldSpec('enum', TEXT_TOOLS | {SAYS}, NATURAL_SIT),
    'access': FieldSpec('enum', frozenset({'tkgm.lookup', 'tkgm.corridor', 'tkgm.neighbors'}) | TEXT_TOOLS, ACCESS),
    'route_minutes': FieldSpec('number', frozenset({'geo.route'}), multi=True),
    'destination_verified': FieldSpec('bool', frozenset()),
}

# Grade follows the source of the observation, never the proposer.
TOOL_GRADE = {SEARCH: 'rapor', LISTING: 'beyan', THREAD: 'beyan',
              'tkgm.lookup': 'resmi', SAYS: 'resmi', 'tkgm.corridor': 'bagimsiz', 'tkgm.neighbors': 'bagimsiz', 'geo.route': 'bagimsiz', 'geo.elevation': 'bagimsiz'}
SOURCE_KIND = {SEARCH: 'listing', LISTING: 'listing', THREAD: 'thread',
               'tkgm.lookup': 'official', SAYS: 'official', 'tkgm.corridor': 'calc', 'tkgm.neighbors': 'calc', 'geo.route': 'calc', 'geo.elevation': 'calc'}

# A wrong value here removes a listing from the funnel unseen, so the bar is higher.
EXCLUDING = frozenset({('shared_title', True), ('archaeological_sit', True),
                       ('natural_sit', 'first_degree'), ('access', 'none')})


@dataclass(frozen=True)
class Proposal:
    listing_ref: str
    field: str
    value: Any
    proposer: str                    # code:<name>@<n> | llm:<agent> | user
    mode: str = 'local'
    observation_id: str | None = None
    source_ref: str | None = None    # message id, "description", "attributes.<label>" ...
    quote: str | None = None         # verbatim text; for users, the note
    subject: str = 'this_parcel'     # this_parcel | neighbor | unclear (declared by the proposer)
    key: str | None = None           # source key for multi-valued fields (engine name)
    resolves: str | None = None      # review item this proposal answers
    basis: str = 'statement'         # statement | absence ("the listing does not say so"; code only)


@dataclass(frozen=True)
class Verdict:
    status: str                      # accepted | rejected | needs_review
    reason: str
    grade: str | None = None


def accept(grade, reason='verified'):
    return Verdict('accepted', reason, grade)


def reject(reason):
    return Verdict('rejected', reason)


def review(reason):
    return Verdict('needs_review', reason)


def validate_value(spec: FieldSpec, value) -> str | None:
    """Return a rejection reason, or None when the value is well formed."""
    if spec.kind == 'bool':
        return None if type(value) is bool else 'value_not_boolean'
    if spec.kind == 'number':
        if type(value) in (int, float) and math.isfinite(value) and value > 0:
            return None
        return 'value_not_positive_number'
    if spec.kind == 'enum':
        return None if value in spec.values else 'value_not_in_vocabulary'
    if spec.kind == 'text':
        return None if isinstance(value, str) and 2 <= len(value.strip()) <= 80 else 'value_not_short_text'
    if spec.kind == 'parcel':
        return None if isinstance(value, str) and PARCEL.match(value) else 'parcel_key_malformed'
    return 'unknown_kind'
