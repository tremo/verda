"""Writes the projection to Firestore: only documents that changed, stale overlays removed, every write counted. A dry run only counts."""
import base64
import json
import time
from pathlib import Path

from verda.publish import projection

MANIFEST = Path('.local/firestore-manifest.json')
RECONCILED = '_reconciled_at'          # manifest entry: when the remote document list was last read
RECONCILE_EVERY = 86400
BATCH = 200
CHUNK = 100_000                  # characters per base64 piece
ASSET_BATCH_BYTES = 4_000_000   # one commit stays well under Firestore's 10 MiB request limit
OWNED = (projection.COLLECTION,)                 # collections this publisher may delete from; `meta/v2` is replaced, never deleted


def value(v):
    if v is None:
        return {'nullValue': None}
    if isinstance(v, bool):
        return {'booleanValue': v}
    if isinstance(v, int):
        return {'integerValue': str(v)}
    if isinstance(v, float):
        return {'doubleValue': v}
    if isinstance(v, str):
        return {'stringValue': v}
    if isinstance(v, (list, tuple)):
        return {'arrayValue': {'values': [value(x) for x in v]}}
    if isinstance(v, dict):
        return {'mapValue': {'fields': {str(k): value(x) for k, x in v.items()}}}
    raise TypeError(type(v))


def fields(data):
    return {str(k): value(v) for k, v in data.items()}


def load_manifest(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def asset_writes(store, firestore, docs, manifest, mode='local'):
    """Photos the overlays point to and that are not published yet: [(key, write)]. Read from the private store only when needed."""
    wanted = {}
    for collection, doc_id, data in docs:
        if collection == projection.COLLECTION:
            for position, asset in enumerate(data.get('photos') or []):
                wanted[asset] = (doc_id, position)
    out = []
    for asset, (listing, position) in wanted.items():
        key = f'{projection.ASSETS}/{asset}'
        if manifest.get(key):
            continue
        with store.transaction() as con:
            found = store.assets.read(con, asset)
        if found is None:
            continue
        mime, path = found
        data = base64.b64encode(Path(path).read_bytes()).decode()
        body = {'mime': mime, 'chunks': [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)], 'listingId': listing, 'position': position, 'source': 'v2'}
        out.append((key, {'update': {'name': firestore.name(projection.ASSETS, asset), 'fields': fields(body)}}))
    return out


def commit(firestore, writes):
    firestore.request('POST', firestore._base().rsplit('/documents', 1)[0] + '/documents:commit', {'writes': writes})


def sync(store, firestore, *, dry_run=False, manifest_path=MANIFEST, mode='local', now=None):
    docs = projection.build(store, mode=mode, now=now)
    manifest = load_manifest(manifest_path)
    writes, new_manifest = [], {}
    for collection, doc_id, data in docs:
        key = f'{collection}/{doc_id}'
        fingerprint = projection.digest(data)
        new_manifest[key] = fingerprint
        if manifest.get(key) != fingerprint:
            writes.append({'update': {'name': firestore.name(collection, doc_id), 'fields': fields(data)}})
    expected = {doc_id for collection, doc_id, _ in docs if collection == projection.COLLECTION}
    # What to delete comes from our own record of what we published. Listing the collection costs one read per document: every five minutes
    # that was ~60k reads a day, over Firestore's free 50k, and on 14-15 Oct 2026 the quota ran out for the publisher and the dashboard alike.
    # The remote list is read once a day only, to catch documents written by anything else.
    now = time.time() if now is None else now
    reconcile = manifest.get(RECONCILED) is None or now - float(manifest[RECONCILED]) > RECONCILE_EVERY
    published = {k.split('/', 1)[1] for k in manifest if k.startswith(projection.COLLECTION + '/')}
    stale = [] if dry_run else sorted((set(firestore.list_ids(projection.COLLECTION)) if reconcile else published) - expected)
    deletes = [{'delete': firestore.name(projection.COLLECTION, i)} for i in stale]
    assets = asset_writes(store, firestore, docs, manifest, mode)
    summary = {'documents': len(docs), 'to_write': len(writes), 'to_delete': len(deletes), 'photos_to_write': len(assets), 'dry_run': dry_run}
    if dry_run:
        return summary
    try:                                           # a problem that has stayed on the status page is told on this Mac too; failing to tell never stops the publish
        from verda.publish import notify
        page = next((data for collection, doc_id, data in docs if (collection, doc_id) == ('meta', 'v2status')), None)
        summary['notified'] = len(notify.notify(store, (page or {}).get('health') or [], now))
    except Exception:
        summary['notified'] = 0
    group, size = [], 0
    for key, write in assets:                      # photos first, so an overlay never names a photo that is not there yet
        piece = len(json.dumps(write, ensure_ascii=False))
        if group and size + piece > ASSET_BATCH_BYTES:
            commit(firestore, [w for _, w in group])
            new_manifest.update({k: 'v2' for k, _ in group})
            group, size = [], 0
        group.append((key, write)); size += piece
    if group:
        commit(firestore, [w for _, w in group])
        new_manifest.update({k: 'v2' for k, _ in group})
    new_manifest.update({k: v for k, v in manifest.items() if k.startswith(projection.ASSETS + '/') and k not in new_manifest})
    new_manifest[RECONCILED] = now if reconcile else manifest[RECONCILED]
    batch = writes + deletes
    for start in range(0, len(batch), BATCH):
        commit(firestore, batch[start:start + BATCH])
    Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
    Path(manifest_path).write_text(json.dumps(new_manifest, ensure_ascii=False), encoding='utf-8')
    return {**summary, 'written': len(writes), 'deleted': len(deletes), 'photos_written': len(assets)}


def status_path(store):
    return Path(store.path).parent / 'publisher-status.json'


def record_status(store, result, *, wait, now=None):
    """The publisher's last round for the watchdog: when, ok or the error, and how long until the next try. Local, tiny, overwritten."""
    now = time.time() if now is None else now
    path = status_path(store)
    data = {'at': now, 'ok': 'error' not in result, 'error': result.get('error'), 'next_at': now + wait}
    if data['ok']:
        data['last_ok_at'] = now
    else:
        try:
            data['last_ok_at'] = json.loads(path.read_text()).get('last_ok_at')
        except (OSError, ValueError):
            data['last_ok_at'] = None
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data))
    tmp.replace(path)
