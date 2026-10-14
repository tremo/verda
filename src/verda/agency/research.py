"""Bounded database views for the manager. No model-generated SQL or source calls."""
import json
from pathlib import Path
import sqlite3
import time

from verda.legacy import decoded, canonical


def archive_connection(store):
    path = getattr(store, 'archive_path', None)
    if not path or not Path(path).is_file():
        return None
    con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    return con


def audit(store, *, mode='local', offset=0, limit=20, exclude_task_id=None):
    known = {}
    archive = archive_connection(store) if mode == 'local' else None
    if archive:
        try:
            snapshot = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
            if snapshot:
                for row in archive.execute("SELECT listing_id,title,parcel_key,lifecycle FROM cases WHERE snapshot_id=?", (snapshot['id'],)):
                    known[row['listing_id']] = {**dict(row), 'source': 'legacy_archive', 'archive_review_required': True,
                                               'missing': [], 'observed': {}, 'record_ids': []}
        finally:
            archive.close()
    with store.transaction() as con:
        # Scan structured source observations; calculations cannot overwrite source facts.
        for row in con.execute("SELECT * FROM observations WHERE mode=? AND tool IN ('sahibinden.search','sahibinden.read_listing','sahibinden.read_thread','tkgm.lookup','geo.route') ORDER BY captured_at,id", (mode,)):
            payload = json.loads(row['payload'])
            if not isinstance(payload, dict):
                continue
            listings = payload.get('listings', []) if row['tool'] == 'sahibinden.search' else [payload]
            for item in listings:
                if not isinstance(item, dict):
                    continue
                key = item.get('listing_id') or row['listing_ref']
                if not isinstance(key, str) or not key:
                    continue
                entry = known.setdefault(key, {'listing_id': key, 'source': 'live_records' if mode == 'local' else 'synthetic',
                                               'archive_review_required': False, 'observed': {}, 'record_ids': []})
                entry['title'] = item.get('title') or entry.get('title')
                entry['record_ids'] = (entry['record_ids'] + [row['id']])[-12:]
                entry['source_task_id'] = row['task_id']
                entry['trace_id'] = row['trace_id']
                for field in ('price_tl', 'area_m2', 'parcel_key', 'natural_sit', 'archaeological_sit', 'route_minutes', 'access'):
                    if item.get(field) is not None and item[field] not in ('', 'unknown', []):
                        entry['observed'][field] = item[field]
                if row['tool'] == 'sahibinden.read_listing':
                    entry['listing_read'] = True
                if item.get('url'):
                    from verda.agency.browser import source_url
                    try:
                        entry['url'] = source_url(item['url'])
                    except (ValueError, TypeError):
                        pass
        tasks = [dict(r) for r in con.execute("SELECT id,agent,listing_ref,state,reason FROM inbox WHERE mode=? AND state NOT IN ('complete','cancelled')", (mode,))]
    for item in known.values():
        if not item['archive_review_required']:
            item['missing'] = [k for k in ('price_tl', 'area_m2', 'parcel_key', 'natural_sit', 'archaeological_sit', 'route_minutes', 'access') if k not in item['observed']]
        item['open_tasks'] = [t for t in tasks if t['listing_ref'] == item['listing_id'] and t['id'] != exclude_task_id]
        item['suggested_next'] = ('excluded_no_retry' if item.get('lifecycle') == 'excluded' else 'review_saved_history' if item['archive_review_required'] else 'wait_for_existing_task' if item['open_tasks']
                                  else 'read_listing' if not item.get('listing_read') else 'manager_review')
    items = sorted((item for item in known.values() if item.get('lifecycle') != 'excluded' or item['record_ids']), key=lambda x: (x['archive_review_required'], x['listing_id']))
    return {'total': len(items), 'offset': offset, 'has_more': offset + limit < len(items),
            'listings': items[offset:offset + limit],
            'errors': [t for t in tasks if t['state'] in {'blocked', 'uncertain', 'failed'}][:20],
            'note': 'Eksikler yeni yapılandırılmış kaynak alanlarını gösterir. Eski kayıtlar boş sayılmaz; yazışma ve kontrol geçmişi okunmadan yeniden soru sorulmaz. Satıcı beyanı için agent sonuçlarını da incele.'}


def listing(store, listing_id, *, mode='local', offset=0):
    rows = []
    with store.transaction() as con:
        for row in con.execute('SELECT * FROM observations WHERE mode=? ORDER BY captured_at,id', (mode,)):
            payload = json.loads(row['payload'])
            if row['listing_ref'] == listing_id:
                rows.append({'kind': 'observation', 'id': row['id'], 'tool': row['tool'], 'captured_at': row['captured_at'], 'payload': payload})
            elif row['tool'] == 'sahibinden.search' and isinstance(payload, dict):
                for item in payload.get('listings', []):
                    if isinstance(item, dict) and item.get('listing_id') == listing_id:
                        rows.append({'kind': 'search_observation', 'id': row['id'], 'captured_at': row['captured_at'], 'payload': item})
        for row in con.execute('SELECT id,state,result,recorded_at FROM record_outcomes WHERE mode=? AND listing_ref=? ORDER BY recorded_at,id', (mode, listing_id)):
            rows.append({**dict(row), 'kind': 'agent_result', 'result': decoded(row['result'])})
    archive = archive_connection(store) if mode == 'local' else None
    if archive:
        try:
            snapshot = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
            if snapshot:
                for row in archive.execute("SELECT table_name,row_key,payload FROM legacy_rows WHERE snapshot_id=? AND subject_id=? AND table_name IN ('items','checks','outbox','listing_feedback') ORDER BY table_name,row_key", (snapshot['id'], listing_id)):
                    rows.append({'kind': 'legacy', 'table': row['table_name'], 'id': row['row_key'], 'payload': decoded(row['payload'])})
        finally:
            archive.close()
    # Page by characters so large archived messages never overflow the model context.
    body = canonical(rows)
    return {'listing_id': listing_id, 'offset': offset, 'text': body[offset:offset+12000],
            'next_offset': offset + 12000 if offset + 12000 < len(body) else None,
            'total_characters': len(body), 'source_is_untrusted_data': True}


def request_review(store, listing_id, request_key):
    archive = archive_connection(store) if listing_id else None
    if archive:
        try:
            excluded = archive.execute("SELECT 1 FROM cases WHERE listing_id=? AND lifecycle='excluded' AND snapshot_id=(SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1)", (listing_id,)).fetchone()
            if excluded:
                raise ValueError('Bu ilan daha önce elenmiş. Yeniden değerlendirme kararı olmadan araştırma tekrarlanmaz.')
        finally:
            archive.close()
    objective = ('Kayıt deposundaki eksikleri ve hata durumlarını incele. Önce records.audit ve ilgili records.listing kayıtlarını oku. '
                 'inputs.listing_id verilmişse yalnız o ilanı ele al. Eski yazışmalarla yanıtlanmış soruyu tekrar sorma. '
                 'Aynı ilan için başka açık görev varsa yenisini üretme. Bu incelemede en fazla bir ilan için bağlı bir okuma aracını '
                 'kullanacak operatöre görev ver. CAPTCHA, erişim engeli ve belirsiz gönderimde tekrar yaptırma; durumu bildir. '
                 'Bağlantısız araçlar için tekrar döngüsü kurma.')
    with store.transaction() as con:
        existing = con.execute("""SELECT id FROM inbox WHERE agent=? AND source='control:review' AND listing_ref IS ?
            AND mode='local' AND state IN ('queued','running','waiting_children','waiting_browser') LIMIT 1""",
                               (store.supervisor_agent, listing_id)).fetchone()
        if existing:
            return existing['id']
        return store._submit(con, agent=store.supervisor_agent, objective=objective,
                             inputs={'listing_id': listing_id, 'max_followups': 1}, listing_ref=listing_id,
                             request_key='review:' + request_key, source='control:review', mode='local', priority=40, now=time.time())


def legacy_lifecycle(store, listing_id):
    """The old system's verdict for a listing, read-only. None when unknown."""
    archive = archive_connection(store)
    if not archive:
        return None
    try:
        snapshot = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
        row = snapshot and archive.execute('SELECT lifecycle FROM cases WHERE listing_id=? AND snapshot_id=?', (listing_id, snapshot['id'])).fetchone()
        return row['lifecycle'] if row else None
    finally:
        archive.close()


LEGACY_REASONS = {'listing_area_under_1000': 'alan 1.000 m² altında', 'price_over_15m': 'fiyat 15 milyon ₺ üstünde', 'shared_title_excluded': 'hisseli / paylı tapu',
                  'direct_cadastral_road_absent': 'doğrudan kadastral yol yok', 'first_degree_natural_sit': '1. derece doğal sit',
                  'invalid_listing_identity_non_land_decoy': 'arsa ilanı değil ya da ilan kimliği geçersiz'}


def legacy_exclusion_reason(store, listing_id):
    """Why the old system excluded a listing, in words; None when it did not or no reason was kept."""
    archive = archive_connection(store)
    if not archive:
        return None
    try:
        snapshot = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
        row = snapshot and archive.execute("SELECT payload FROM legacy_rows WHERE snapshot_id=? AND table_name='items' AND subject_id=?", (snapshot['id'], listing_id)).fetchone()
        if not row:
            return None
        item = json.loads(row['payload'])
        if item.get('lifecycle') != 'excluded':
            return None
        text = str(json.loads(item.get('legacy_json') or '{}').get('exclusion_reason') or '').strip()
    finally:
        archive.close()
    if not text or text == 'None':
        return None
    parts = [p.strip() for p in text.split(';') if p.strip()]
    return '; '.join(LEGACY_REASONS.get(p, p) for p in parts)[:400]


def fact_status(store, listing_id, *, mode='local'):
    """Verified facts, the rule engine's decision and the next real action for one listing."""
    lifecycle = legacy_lifecycle(store, listing_id) if mode == 'local' else None
    with store.transaction() as con:
        return store.facts.status(con, listing_id, mode, legacy_lifecycle=lifecycle)


def normalise_value(field, value):
    """A plain "no natural sit" said as false means the vocabulary's 'none'. Nothing else is guessed: true says nothing about the degree."""
    return 'none' if field == 'natural_sit' and value is False else value


def vocabulary_hint(field):
    from verda.facts.model import FIELDS
    spec = FIELDS.get(field)
    return list(spec.values) if spec and spec.kind == 'enum' else None


def fact_propose(store, args, *, mode, task_id, proposer=None):
    """The reading agent's proposal. It is verified like any other: the model never writes facts."""
    import time
    from verda.facts.model import Proposal
    with store.transaction() as con:
        agent = con.execute('SELECT agent FROM inbox WHERE id=?', (task_id,)).fetchone()
        who = proposer or 'llm:' + (agent['agent'] if agent else 'unknown')
        result = store.facts.submit(con, Proposal(
            listing_ref=args['listing_id'], field=args['field'], value=normalise_value(args['field'], args['value']), proposer=who, mode=mode,
            observation_id=args['observation_id'], source_ref=args.get('source_ref') or None,
            quote=args.get('quote') or None, subject=args.get('subject', 'this_parcel')), now=time.time())
        if result['status'] == 'accepted' and not result['duplicate']:
            result['assessment'] = store.facts.assess(con, args['listing_id'], mode, time.time())
        if result.get('status') == 'rejected' and result.get('reason') == 'value_not_in_vocabulary':
            result['allowed_values'] = vocabulary_hint(args['field'])           # so the proposer can correct itself instead of giving up
        return result


def flag_for_clarification(store, args, *, mode):
    """The reading agent marks a seller answer that does not settle a field; the quote is checked against the recorded message."""
    from verda.facts.flags import raise_flag
    with store.transaction() as con:
        return raise_flag(con, listing_ref=args['listing_id'], mode=mode, field=args['field'], observation_id=args['observation_id'],
                          source_ref=args['source_ref'], quote=args['quote'], note=args['note'])


def sender_block(store, listing_id, mode, status):
    """The seller message for the listing page: the mode, what would be asked now (or why nothing), and every attempt so far."""
    from verda.agency import message
    now = time.time()
    current_mode = message.mode_of(store)
    with store.transaction() as con:
        attempts = message.rows(con, listing_id, mode)
    return {'mode': current_mode, 'preview': message.preview(store, listing_id, mode, now, current=status),
            'attempts': [{k: a[k] for k in ('id', 'state', 'fields', 'text', 'reason', 'thread_url', 'created_at', 'updated_at', 'sent_at')} | {'evidence': a['evidence']} for a in attempts]}


def listing_file(store, listing_id, *, mode='local'):
    """Everything saved about one listing, for the local panel. Bounded; photos are served separately."""
    status = fact_status(store, listing_id, mode=mode)
    with store.transaction() as con:
        def latest(tool):
            row = con.execute('SELECT id,payload,captured_at FROM observations WHERE tool=? AND mode=? AND listing_ref=? ORDER BY captured_at DESC LIMIT 1',
                              (tool, mode, listing_id)).fetchone()
            return None if row is None else {'id': row['id'], 'captured_at': row['captured_at'], 'payload': json.loads(row['payload'])}
        page, route = latest('sahibinden.read_listing'), latest('geo.route')
        assets = store.assets.listing(con, listing_id, mode)
        from verda.facts.flags import open_flags
        clarifications = open_flags(con, listing_id, mode)
    detail = (page or {}).get('payload') or {}
    from verda.agency.learning import context_of, learning, unknown_fields
    from verda.agency.listing_dates import collect
    dates = collect(store, mode).get(listing_id) or {}
    return {
        'listing_id': listing_id, 'mode': mode, 'status': status,
        'legacy_reason': legacy_exclusion_reason(store, listing_id) if status.get('legacy_lifecycle') == 'excluded' else None,
        'dates': {'published_at': dates.get('published_at'), 'discovered_at': dates.get('discovered_at'), 'discovered_by': dates.get('discovered_by')},
        'learning': learning(store, listing_id, unknown_fields(status), status.get('next_action'), mode=mode, context=context_of(status)),
        'sender': sender_block(store, listing_id, mode, status),
        'clarifications': [{k: f[k] for k in ('id', 'field', 'note', 'quote', 'source_ref', 'created_at')} for f in clarifications],
        'page': None if page is None else {
            'observation_id': page['id'], 'captured_at': page['captured_at'], 'title': detail.get('title'), 'url': detail.get('url'),
            'description': (detail.get('description') or '')[:16000], 'attributes': detail.get('attributes') or {},
            'photos_listed': len(detail.get('photos') or [])},
        'route': None if route is None else {'captured_at': route['captured_at'], **{k: route['payload'].get(k) for k in ('origin', 'destination', 'straight_line_km', 'engines', 'note', 'reason')}},
        'assets': [{'id': a['id'], 'mime': a['mime'], 'bytes': a['bytes']} for a in assets[:40]]}
