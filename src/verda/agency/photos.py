"""Saves a listing's photos into the private evidence store. Read-only towards the site's CDN."""
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from verda.agency.assets import MAX_BYTES
from verda.agency.registry import ResourceBlocked, TransientToolError

MAX_PHOTOS = 5                 # the first five are enough to judge a listing (user decision, 11 Oct 2026)
UA = 'Mozilla/5.0 (compatible; Verda-photo-saver/2.0; personal land research)'


def allowed(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ''
    return (parsed.scheme == 'https' and not parsed.username and not parsed.port
            and (host.endswith('.shbdn.com') or host == 'shbdn.com' or host.endswith('sahibinden.com')))


def download(url, timeout=25.0) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'image/avif,image/webp,image/jpeg,image/png,*/*'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_BYTES + 1)


def save_listing_photos(store, args, *, mode, fetch=download, sleep=time.sleep, delay=0.8) -> dict:
    listing = args['listing_id']
    with store.transaction() as con:
        row = con.execute("SELECT id,payload FROM observations WHERE tool='sahibinden.read_listing' AND mode=? AND listing_ref=? ORDER BY captured_at DESC LIMIT 1",
                          (mode, listing)).fetchone()
        have = {r['source_url'] for r in con.execute('SELECT source_url FROM evidence_assets WHERE listing_ref=? AND mode=?', (listing, mode))}
    if not row:
        return {'listing_id': listing, 'saved': 0, 'reason': 'listing_not_read'}
    urls = [u for u in (json.loads(row['payload']).get('photos') or []) if isinstance(u, str) and allowed(u)][:MAX_PHOTOS]
    summary = {'listing_id': listing, 'saved': 0, 'already': 0, 'failed': [], 'photos_listed': len(urls)}
    for position, url in enumerate(urls):
        if url in have:
            summary['already'] += 1
            continue
        if summary['saved'] or summary['failed']:
            sleep(delay)
        try:
            data = fetch(url)
            with store.transaction() as con:
                store.assets.save(con, listing_ref=listing, mode=mode, observation_id=row['id'], source_url=url, position=position, data=data)
            summary['saved'] += 1
        except urllib.error.HTTPError as error:
            if error.code in {403, 429}:
                raise ResourceBlocked(f'media_http_{error.code}') from None
            summary['failed'].append({'position': position, 'error': f'http_{error.code}'})
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            summary['failed'].append({'position': position, 'error': 'unreachable'})
        except ValueError as error:
            summary['failed'].append({'position': position, 'error': str(error)[:60]})
    if summary['failed'] and not summary['saved'] and not summary['already'] and all(f['error'] == 'unreachable' for f in summary['failed']):
        raise TransientToolError('media_unreachable')
    return summary
