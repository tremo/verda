"""Official parcel lookup (TKGM), ported from the legacy `tkgm_parcel_batch.py`.

Read-only and quota-conscious: names must match exactly, the returned parcel must be the
one asked for, 403/429 stop the shared source. The network is injected so tests never call it.
"""
import json
import os
from pathlib import Path
import re
import time
import unicodedata
import urllib.error
import urllib.request

from verda.agency.registry import ResourceBlocked, TransientToolError

BASE = 'https://cbsapi.tkgm.gov.tr/megsiswebapi.v3.1/api'
RATE_CODES = {403, 429}
MAX_RING = 200
_SUFFIX = re.compile(r'\s+(mh\.?|mah\.?|mahallesi)$', re.I)


def fold(value) -> str:
    text = str(value or '').strip().replace('ı', 'i').replace('İ', 'I')
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(c))


def clean_neighborhood(value) -> str:
    """"Yaka Mh." / "Köyler\\nYaka Mahallesi" -> "Yaka"."""
    last = str(value or '').strip().splitlines()[-1] if str(value or '').strip() else ''
    return _SUFFIX.sub('', last.strip()).strip()


def split_parcel(value) -> tuple[str, str]:
    match = re.fullmatch(r'\s*(0|[1-9][0-9]*)\s*/\s*([1-9][0-9]*)\s*', str(value or ''))
    if not match:
        raise ValueError('parcel must be ada/parsel: ada 0 or positive, parsel positive')
    return match[1], match[2]


def ada_no(props) -> str:
    """TKGM shows a parcel outside any block with an empty ada; the query and our keys call it 0 (Karaköy 0/1238, checked 14 Oct 2026)."""
    return str(props.get('adaNo') or '0')


def fetch_json(url, timeout=20.0):
    request = urllib.request.Request(url, headers={'Accept': 'application/json, application/geo+json',
                                                   'User-Agent': 'Verda-TKGM-Parcel-Verifier/2.0'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def _features(payload):
    if isinstance(payload, dict) and isinstance(payload.get('features'), list):
        return [f for f in payload['features'] if isinstance(f, dict)]
    return [f for f in payload if isinstance(f, dict)] if isinstance(payload, list) else []


def exact_admin_id(payload, requested) -> int:
    hits = []
    for feature in _features(payload):
        props = feature.get('properties') if isinstance(feature.get('properties'), dict) else feature
        if props.get('text') is not None and props.get('id') is not None and fold(props['text']) == fold(requested):
            hits.append(int(props['id']))
    if len(hits) != 1:
        names = sorted({str((f.get('properties') or f).get('text')) for f in _features(payload)
                        if fold(requested)[:4] in fold((f.get('properties') or f).get('text'))})[:8]
        raise ValueError(f'no single exact administrative match for {requested!r}; similar: {names}')
    return hits[0]


def parse_area(value) -> float:
    """TKGM prints areas in two styles depending on the endpoint: "1.053,41" and "1,053.41".
    With both separators the last one is the decimal point; a lone separator followed by exactly
    three digits is a thousands separator, otherwise it is the decimal point."""
    text = str(value).strip()
    if not re.fullmatch(r'\d{1,3}([.,]\d{3})*([.,]\d+)?|\d+([.,]\d+)?', text):
        raise ValueError('official area is not a number')
    seps = [c for c in text if c in '.,']
    if not seps:
        return float(text)
    last = text.rfind(seps[-1])
    decimal = len(set(seps)) == 2 or (len(seps) == 1 and len(text) - last - 1 != 3)
    whole, frac = (text[:last], text[last + 1:]) if decimal else (text, '')
    return float(whole.replace('.', '').replace(',', '') + ('.' + frac if frac else ''))


def validate_feature(payload, neighborhood, neighborhood_id, ada, parcel) -> dict:
    if not isinstance(payload, dict) or payload.get('type') != 'Feature':
        raise ValueError('response is not a GeoJSON Feature')
    props, geometry = payload.get('properties'), payload.get('geometry')
    if not isinstance(props, dict) or not isinstance(geometry, dict):
        raise ValueError('feature lacks properties or geometry')
    if not (fold(props.get('mahalleAd')) == fold(neighborhood) and str(props.get('mahalleId')) == str(neighborhood_id)
            and ada_no(props) == ada and str(props.get('parselNo')) == parcel):
        raise ValueError('returned parcel identity does not match the request')
    ring = (geometry.get('coordinates') or [[]])[0] if geometry.get('type') == 'Polygon' else None
    if not ring or len(ring) < 4 or ring[0] != ring[-1]:
        raise ValueError('geometry is not a closed Polygon')
    area = parse_area(props.get('alan'))
    if not area > 0:
        raise ValueError('official area is not positive')
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    return {'official_area_m2': area, 'property_type': props.get('nitelik'), 'raw_status': props.get('durum'),
            'geometry': {'type': 'Polygon', 'ring_points': len(ring), 'centroid': [round(sum(xs) / len(xs), 6), round(sum(ys) / len(ys), 6)],
                         'bbox': [min(xs), min(ys), max(xs), max(ys)],
                         **({'ring': [[round(x, 6), round(y, 6)] for x, y in ring]} if len(ring) <= MAX_RING else {'ring_omitted': True})}}


class TkgmClient:
    def __init__(self, cache_path=None, fetch=fetch_json, sleep=time.sleep, province='Muğla', district='Datça', delay=1.0):
        self.cache_path, self.fetch, self.sleep = Path(cache_path) if cache_path else None, fetch, sleep
        self.province, self.district, self.delay = province, district, delay

    def _get(self, url):
        try:
            return self.fetch(url)
        except urllib.error.HTTPError as error:
            if error.code in RATE_CODES:
                raise ResourceBlocked(f'tkgm_http_{error.code}') from None
            if error.code >= 500:
                raise TransientToolError('tkgm_server_error') from None
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise TransientToolError('tkgm_unreachable') from None

    def _neighborhood_id(self, name):
        cache = {}
        if self.cache_path and self.cache_path.exists():
            cache = json.loads(self.cache_path.read_text())
        known = {fold(k): v for k, v in cache.get('neighborhoods', {}).items()}
        if fold(name) in known:
            return int(known[fold(name)])
        province = exact_admin_id(self._get(BASE + '/idariYapi/ilListe'), self.province)
        self.sleep(self.delay)
        district = exact_admin_id(self._get(f'{BASE}/idariYapi/ilceListe/{province}'), self.district)
        self.sleep(self.delay)
        ident = exact_admin_id(self._get(f'{BASE}/idariYapi/mahalleListe/{district}'), name)
        self.sleep(self.delay)
        if self.cache_path:
            cache.setdefault('neighborhoods', {})[name] = ident
            tmp = self.cache_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(cache, ensure_ascii=False))
            os.replace(tmp, self.cache_path)
        return ident

    def lookup(self, args: dict) -> dict:
        neighborhood = clean_neighborhood(args['neighborhood'])
        ada, parcel = split_parcel(args['parcel_key'])
        ident = self._neighborhood_id(neighborhood)
        url = f'{BASE}/parsel/{ident}/{ada}/{parcel}'
        base = {'parcel_key': f'{ada}/{parcel}', 'neighborhood': neighborhood, 'neighborhood_id': ident, 'source_url': url,
                'parcel_url': f'https://parselsorgu.tkgm.gov.tr/#ara/idari/{ident}/{ada}/{parcel}', 'observed_at': time.time()}
        try:
            payload = self._get(url)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {**base, 'found': False}
            raise RuntimeError(f'tkgm_http_{error.code}') from None
        return {**base, 'found': True, **validate_feature(payload, neighborhood, ident, ada, parcel)}
