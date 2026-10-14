"""When a listing was published and when this system first saw it. Read-only, from the records.

Published: the date Sahibinden shows on the ad ("05 Ekim 2026"), from a page read or a search row.
Discovered: the earliest of the old system's first record of the listing, the first time a search page listed it,
and the first record any tool made about it. The source is named, so "keşif" is never a guess.
"""
import json
from datetime import datetime, timezone

MONTHS = {'ocak': 1, 'şubat': 2, 'mart': 3, 'nisan': 4, 'mayıs': 5, 'haziran': 6, 'temmuz': 7, 'ağustos': 8, 'eylül': 9, 'ekim': 10, 'kasım': 11, 'aralık': 12}


def parse_tr_date(text):
    """Seconds since the epoch (midnight, UTC) of '05 Ekim 2026'; None when it is not such a date."""
    parts = str(text or '').replace('İ', 'i').replace('I', 'ı').lower().split()
    if len(parts) != 3 or parts[1] not in MONTHS or not parts[0].isdigit() or not parts[2].isdigit():
        return None
    try:
        return datetime(int(parts[2]), MONTHS[parts[1]], int(parts[0]), tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _iso(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def collect(store, mode='local'):
    """{listing id: {'published_at', 'discovered_at', 'discovered_by'}} for every listing the records mention."""
    from verda.agency.research import archive_connection
    published, found = {}, {}

    def see(listing, at, by):
        if listing and at is not None and (listing not in found or at < found[listing][0]):
            found[listing] = (at, by)

    with store.transaction() as con:
        for row in con.execute("SELECT payload, captured_at FROM observations WHERE mode=? AND tool='sahibinden.search' ORDER BY captured_at", (mode,)):
            try:
                listings = json.loads(row['payload']).get('listings', [])
            except ValueError:
                continue
            for item in listings:
                if isinstance(item, dict) and item.get('listing_id'):
                    see(item['listing_id'], row['captured_at'], 'arama')
                    if parse_tr_date(item.get('listed_at_text')):
                        published[item['listing_id']] = parse_tr_date(item['listed_at_text'])
        for row in con.execute("SELECT listing_ref, MIN(captured_at) AS at FROM observations WHERE mode=? AND listing_ref IS NOT NULL GROUP BY listing_ref", (mode,)):
            see(row['listing_ref'], row['at'], 'v2 kaydı')
        for row in con.execute("""SELECT listing_ref, json_extract(payload, '$.attributes."ilan tarihi"') AS d FROM observations
                WHERE mode=? AND tool='sahibinden.read_listing' AND listing_ref IS NOT NULL ORDER BY captured_at""", (mode,)):
            if parse_tr_date(row['d']):
                published[row['listing_ref']] = parse_tr_date(row['d'])
    archive = archive_connection(store) if mode == 'local' else None
    if archive:
        try:
            snapshot = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
            for row in archive.execute("SELECT subject_id, payload FROM legacy_rows WHERE snapshot_id=? AND table_name='items'", (snapshot['id'],)) if snapshot else []:
                see(row['subject_id'], _iso(json.loads(row['payload']).get('created_at')), 'eski sistem')
        finally:
            archive.close()
    return {i: {'published_at': published.get(i), 'discovered_at': found[i][0] if i in found else None, 'discovered_by': found[i][1] if i in found else None}
            for i in set(published) | set(found)}


def legacy_titles(store):
    """The old system's title of each listing, for listings this system has not read itself yet."""
    from verda.agency.research import archive_connection
    archive = archive_connection(store)
    if not archive:
        return {}
    try:
        snapshot = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
        return {r['listing_id']: r['title'] for r in archive.execute('SELECT listing_id, title FROM cases WHERE snapshot_id=?', (snapshot['id'],)) if r['title']} if snapshot else {}
    finally:
        archive.close()
