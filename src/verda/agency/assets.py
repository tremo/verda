"""Local, content-addressed images for a listing (photos). Private: never in git, never in model input."""
import hashlib
import os
from pathlib import Path
import time

MAX_BYTES = 6_000_000
MAGIC = ((b'\xff\xd8\xff', 'image/jpeg'), (b'\x89PNG\r\n\x1a\n', 'image/png'))


def sniff(data: bytes) -> str | None:
    """The real type from the bytes, never from what a server claims."""
    for prefix, mime in MAGIC:
        if data.startswith(prefix):
            return mime
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/avif' if data[4:8] == b'ftyp' and data[8:12] in (b'avif', b'avis') else None       # the site's cover photo is an AVIF


def install(con):
    con.execute('''CREATE TABLE IF NOT EXISTS evidence_assets(id TEXT PRIMARY KEY, listing_ref TEXT NOT NULL, mode TEXT NOT NULL,
        observation_id TEXT REFERENCES observations(id), kind TEXT NOT NULL, mime TEXT NOT NULL, bytes INTEGER NOT NULL,
        source_url TEXT NOT NULL, position INTEGER NOT NULL, created_at REAL NOT NULL, UNIQUE(listing_ref, mode, id))''')
    con.execute('CREATE INDEX IF NOT EXISTS assets_listing ON evidence_assets(listing_ref,mode,position)')
    for action in ('UPDATE', 'DELETE'):
        con.execute(f"CREATE TRIGGER IF NOT EXISTS evidence_assets_no_{action.lower()} BEFORE {action} ON evidence_assets "
                    "BEGIN SELECT RAISE(ABORT,'evidence_assets_are_append_only'); END")


def migrate_v9(con):
    install(con)
    con.execute('UPDATE agency_meta SET version=9')


class AssetStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, asset_id: str) -> Path:
        return self.root / asset_id[:2] / asset_id

    def save(self, con, *, listing_ref, mode, observation_id, source_url, position, data: bytes, kind='photo', now=None) -> dict:
        if not data or len(data) > MAX_BYTES:
            raise ValueError('image_size_out_of_bounds')
        mime = sniff(data)
        if mime is None:
            raise ValueError('not_an_image')
        asset_id = hashlib.sha256(data).hexdigest()
        target = self.path(asset_id)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = target.with_suffix('.tmp')
            fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
            os.replace(tmp, target)
        cursor = con.execute('INSERT OR IGNORE INTO evidence_assets VALUES(?,?,?,?,?,?,?,?,?,?)',
                             (asset_id, listing_ref, mode, observation_id, kind, mime, len(data), source_url, position,
                              time.time() if now is None else now))
        return {'id': asset_id, 'mime': mime, 'bytes': len(data), 'new': cursor.rowcount == 1}

    @staticmethod
    def listing(con, listing_ref, mode):
        return [dict(r) for r in con.execute('SELECT id,mime,bytes,position,kind FROM evidence_assets WHERE listing_ref=? AND mode=? ORDER BY position,created_at',
                                             (listing_ref, mode))]

    def read(self, con, asset_id):
        row = con.execute('SELECT mime FROM evidence_assets WHERE id=? LIMIT 1', (asset_id,)).fetchone()
        path = self.path(asset_id)
        if not row or not path.is_file():
            return None
        return row['mime'], path
