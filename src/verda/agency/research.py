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
