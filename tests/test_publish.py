import json

from verda.publish import projection, sync


class FakeFirestore:
    def __init__(self, existing=()):
        self.existing, self.commits = list(existing), []

    def name(self, collection, doc_id):
        return f'projects/p/databases/(default)/documents/{collection}/{doc_id}'

    def _base(self):
        return 'https://example.invalid/v1/projects/p/databases/(default)/documents'

    def list_ids(self, collection):
        return list(self.existing)

    def request(self, method, url, body=None):
        assert method == 'POST' and url.endswith('documents:commit')
        self.commits.append(body['writes'])
        return {}


def docs(*ids, score=90):
    out = [(projection.COLLECTION, i, {'listingId': i, 'score': score, 'updatedAt': 'now'}) for i in ids]
    return out + [('meta', 'v2', {'recordCount': len(ids), 'generatedAt': 'now'})]


def test_values_keep_their_types_and_nested_shapes():
    assert sync.value(3) == {'integerValue': '3'} and sync.value(3.5) == {'doubleValue': 3.5} and sync.value(True) == {'booleanValue': True}
    assert sync.value(None) == {'nullValue': None}
    assert sync.value({'a': [1, 'x']}) == {'mapValue': {'fields': {'a': {'arrayValue': {'values': [{'integerValue': '1'}, {'stringValue': 'x'}]}}}}}


def test_only_changed_documents_are_written_and_stale_overlays_are_deleted(tmp_path, monkeypatch):
    manifest, fs = tmp_path / 'm.json', FakeFirestore(existing=['A', 'B', 'GONE'])
    monkeypatch.setattr(projection, 'build', lambda *a, **k: docs('A', 'B'))
    first = sync.sync(None, fs, manifest_path=manifest)
    assert (first['written'], first['deleted']) == (3, 1)                      # A, B and meta/v2; GONE is removed
    assert fs.commits[0][-1] == {'delete': fs.name(projection.COLLECTION, 'GONE')}
    monkeypatch.setattr(projection, 'build', lambda *a, **k: docs('A', 'B'))
    fs.existing = ['A', 'B']
    again = sync.sync(None, fs, manifest_path=manifest)
    assert again['written'] == 0 and len(fs.commits) == 1                     # nothing changed, nothing sent
    monkeypatch.setattr(projection, 'build', lambda *a, **k: docs('A', 'B', score=95))
    assert sync.sync(None, fs, manifest_path=manifest)['written'] == 2          # A and B changed; meta/v2 did not


def test_a_dry_run_only_counts(tmp_path, monkeypatch):
    fs = FakeFirestore(existing=['GONE'])
    monkeypatch.setattr(projection, 'build', lambda *a, **k: docs('A'))
    result = sync.sync(None, fs, dry_run=True, manifest_path=tmp_path / 'm.json')
    assert result == {'documents': 2, 'to_write': 2, 'to_delete': 0, 'photos_to_write': 0, 'dry_run': True} and not fs.commits and not (tmp_path / 'm.json').exists()


def test_the_dashboards_own_wording_for_how_far_a_conversation_has_come():
    assert projection.contact_of({'received': 1}, []) == 'Yanıt işlendi'
    assert projection.contact_of({'sent': 1, 'received': 0}, [{'state': 'asked'}]) == 'İletişim kuruldu'
    assert projection.contact_of({}, [{'state': 'none'}]) is None



class FakeStore:
    """Just enough of the store for the photo step: a transaction and an asset reader backed by files."""
    def __init__(self, files):
        self.files = files
        outer = self
        class Assets:
            def read(self, con, asset):
                return ('image/jpeg', outer.files[asset]) if asset in outer.files else None
        self.assets = Assets()

    def transaction(self):
        import contextlib
        return contextlib.nullcontext(None)


def test_photos_go_first_in_chunks_once_and_only_when_new(tmp_path, monkeypatch):
    import base64
    one, two = tmp_path / 'a', tmp_path / 'b'
    one.write_bytes(b'\xff\xd8\xff' + b'x' * 150_000)           # 200 KB of base64: two chunks
    two.write_bytes(b'\xff\xd8\xff' + b'y' * 10)
    store, fs, manifest = FakeStore({'A1': one, 'A2': two}), FakeFirestore(), tmp_path / 'm.json'
    def build(*a, **k):
        d = docs('L1')
        d[0][2]['photos'] = ['A1', 'A2', 'MISSING']
        return d
    monkeypatch.setattr(projection, 'build', build)
    first = sync.sync(store, fs, manifest_path=manifest)
    assert first['photos_written'] == 2                                   # the one that is not in the store is skipped
    commits = [[w['update']['name'].rsplit('/', 2)[-2] for w in c] for c in fs.commits]
    assert commits[0] == ['evidenceAssets', 'evidenceAssets'] and 'v2Listings' in commits[1]     # photos before the overlay
    body = fs.commits[0][0]['update']['fields']
    assert len(body['chunks']['arrayValue']['values']) == 3 and body['source']['stringValue'] == 'v2' and body['position']['integerValue'] == '0'
    joined = ''.join(v['stringValue'] for v in body['chunks']['arrayValue']['values'])
    assert base64.b64decode(joined)[:3] == b'\xff\xd8\xff'
    fs.commits.clear()
    again = sync.sync(store, fs, manifest_path=manifest)
    assert again['photos_written'] == 0 and not fs.commits                   # nothing new, nothing sent


def test_dashboard_corrections_map_back_to_fact_values_and_are_applied_once(tmp_path):
    from verda.agency.registry import AgencyConfig
    from verda.agency.store import AgencyStore
    from verda.publish import feedback
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    calls = []
    store.facts.decide = lambda con, listing, mode, field, value, note, now: calls.append((listing, field, value))
    class Fs:
        def list_docs(self, collection):
            return [('L1', {'userFacts': {'natural': {'value': 'none', 'basis': 'seller', 'at': 't1'}, 'archaeological': {'value': 'present', 'basis': 'site', 'at': 't1'},
                                          'road': {'value': 'rightOfWay', 'basis': 'official', 'at': 't1'}, 'parcelId': {'ada': '202', 'parsel': '20', 'basis': 'official', 'at': 't1'},
                                          'natural2': {'value': 'x'}}})]
    assert feedback.import_feedback(store, Fs())['user_facts_applied'] == 4
    assert sorted(calls) == [('L1', 'access', 'existing_easement'), ('L1', 'archaeological_sit', True), ('L1', 'natural_sit', 'none'), ('L1', 'parcel_key', '202/20')]
    calls.clear()
    assert feedback.import_feedback(store, Fs())['user_facts_applied'] == 0 and not calls            # the same corrections are not applied again


class SqlStore:
    """A real sqlite connection with the two tables closed_documents reads."""
    def __init__(self):
        import sqlite3
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        self.con.execute('CREATE TABLE assessments(listing_ref TEXT, mode TEXT)')
        self.con.execute('CREATE TABLE observations(id INTEGER PRIMARY KEY, listing_ref TEXT, mode TEXT, tool TEXT, payload TEXT, captured_at REAL)')

    def transaction(self):
        import contextlib
        return contextlib.nullcontext(self.con)


def test_listings_that_left_the_running_are_published_with_the_reason(monkeypatch):
    from verda.agency import score, queue, listing_dates
    store = SqlStore()
    for listing in ('RANKED', 'GONE', 'FAR', 'DEAR', 'OLDONLY', 'OPEN'):
        store.con.execute("INSERT INTO assessments VALUES(?, 'local')", (listing,))
    store.con.execute("INSERT INTO observations(listing_ref, mode, tool, payload, captured_at) VALUES('GONE', 'local', 'sahibinden.read_listing', ?, 1791550003.0)", ('{"unavailable":true}',))
    store.con.execute("INSERT INTO observations(listing_ref, mode, tool, payload, captured_at) VALUES('NEVERASSESSED', 'local', 'sahibinden.read_listing', ?, 1791550004.0)", ('{"unavailable":true}',))
    status = {
        'RANKED': {'decision': {'state': 'excluded', 'reasons': ['price_over_limit']}},
        'GONE': {'decision': {'state': 'research_required', 'reasons': ['price']}, 'seen': {'listing_removed': True}},
        'NEVERASSESSED': {'decision': {}, 'seen': {'listing_removed': True}},
        'FAR': {'decision': {'state': 'outside_search', 'reasons': ['route_over_limit']}, 'facts': {'price_tl': {'value': 14650000.0}, 'sale_area_m2': {'value': 1145.5}, 'property_type': {'value': 'Tarla'}},
                'links': {'tkgm': 'https://parselsorgu.tkgm.gov.tr/#ara/idari/1/2/3'}},
        'DEAR': {'decision': {'state': 'excluded', 'reasons': ['price_over_limit']}},
        'OLDONLY': {'decision': {'state': 'excluded', 'reasons': ['legacy_excluded']}},
        'OPEN': {'decision': {'state': 'conditional', 'reasons': ['screening_complete']}},
    }
    monkeypatch.setattr(projection, 'fact_status', lambda s, listing, mode='local': status[listing])
    monkeypatch.setattr(score, '_legacy_rows', lambda s: [{'id': 'DEAR', 'price': 20_000_000, 'area': 3000, 'type': 'Tarla', 'neighborhood': 'Yaka'}])
    monkeypatch.setattr(score, '_titles', lambda s, mode: {'FAR': 'Denize sıfır tarla'})
    monkeypatch.setattr(listing_dates, 'legacy_titles', lambda s: {})
    monkeypatch.setattr(queue, 'neighborhood_of', lambda s, listing: 'Karaköy Mh.' if listing == 'FAR' else None)
    out = {listing: doc for _, listing, doc in projection.closed_documents(store, {'RANKED'}, 'now')}
    assert set(out) == {'GONE', 'NEVERASSESSED', 'FAR', 'DEAR'}                 # ranked, only-legacy-excluded and still-open listings are left alone
    assert out['GONE']['state'] == 'removed' and out['GONE']['decisionReasons'] == [] and out['GONE']['removedAt'].startswith('2026-10-')
    assert out['NEVERASSESSED']['state'] == 'removed'                             # a removed listing needs no assessment to be reported
    assert out['FAR'] == {'listingId': 'FAR', 'state': 'outside_search', 'decisionReasons': ['route_over_limit'], 'title': 'Denize sıfır tarla', 'priceTl': 14650000.0,
                          'areaM2': 1145.5, 'type': 'Tarla', 'neighborhood': 'Karaköy Mh.', 'links': {'listing': 'https://www.sahibinden.com/ilan/FAR/detay', 'tkgm': 'https://parselsorgu.tkgm.gov.tr/#ara/idari/1/2/3'}, 'updatedAt': 'now'}
    assert out['DEAR']['decisionReasons'] == ['price_over_limit'] and out['DEAR']['priceTl'] == 20_000_000      # falls back to the old record's numbers


def test_a_parcels_outline_travels_wrapped_because_firestore_takes_no_array_in_an_array():
    store = SqlStore()
    ring = [[27.1, 36.1], [27.2, 36.1], [27.2, 36.2], [27.1, 36.1]]
    lookup = {'found': True, 'geometry': {'type': 'Polygon', 'ring': ring, 'centroid': [27.15, 36.13]}}
    store.con.execute("INSERT INTO observations(listing_ref, mode, tool, payload, captured_at) VALUES('L1', 'local', 'tkgm.lookup', ?, 1.0)", (json.dumps(lookup),))
    store.con.execute("INSERT INTO observations(listing_ref, mode, tool, payload, captured_at) VALUES('L1', 'local', 'tkgm.lookup', ?, 2.0)", (json.dumps({'found': False}),))     # a newer lookup without an outline does not hide the older one
    store.con.execute("INSERT INTO observations(listing_ref, mode, tool, payload, captured_at) VALUES('L2', 'local', 'tkgm.lookup', ?, 1.0)", (json.dumps({'found': True, 'geometry': {'type': 'Polygon', 'ring': [[1, 2]], 'centroid': [1, 2]}}),))
    shape = projection.parcel_shape(store, 'L1')
    assert shape['centroid'] == [27.15, 36.13]
    points = shape['geometry']['coordinates'][0]['items']
    assert [p['items'] for p in points] == ring and shape['geometry']['type'] == 'Polygon'
    assert projection.parcel_shape(store, 'L2') is None and projection.parcel_shape(store, 'NONE') is None      # too few points / nothing looked up


def test_the_remote_document_list_is_read_once_a_day_and_deletions_come_from_what_was_published(tmp_path, monkeypatch):
    # Listing the collection every five minutes cost ~60k reads a day, over Firestore's free quota (14-15 Oct 2026).
    calls = []
    manifest, fs = tmp_path / 'm.json', FakeFirestore(existing=['A', 'B'])
    fs.list_ids = lambda collection: calls.append(collection) or list(fs.existing)
    monkeypatch.setattr(projection, 'build', lambda *a, **k: docs('A', 'B'))
    sync.sync(None, fs, manifest_path=manifest, now=1000.0)
    assert len(calls) == 1                                                      # first round: reconcile with the remote list
    monkeypatch.setattr(projection, 'build', lambda *a, **k: docs('A'))
    second = sync.sync(None, fs, manifest_path=manifest, now=1300.0)
    assert len(calls) == 1 and second['deleted'] == 1                          # B, which we published, is gone: deleted without listing
    sync.sync(None, fs, manifest_path=manifest, now=1000.0 + 86401)
    assert len(calls) == 2                                                      # a day later the remote list is read again


def test_a_quota_stop_reaches_the_watchdog_with_its_reason(tmp_path):
    from verda.agency import watchdog

    class Store:
        path = tmp_path / 'agency.sqlite'
    sync.record_status(Store, {'documents': 1}, wait=300, now=1000.0)
    sync.record_status(Store, {'error': 'RuntimeError:firestore_http_429'}, wait=600, now=4000.0)
    found = watchdog.publisher_silent(Store, 'local', 4000.0)
    assert found[0]['severity'] == 'action' and found[0]['title'] == 'Pano 50 dakikadır güncellenmedi' and 'kotası doldu' in found[0]['detail']
    sync.record_status(Store, {'documents': 1}, wait=300, now=4300.0)
    assert watchdog.publisher_silent(Store, 'local', 4300.0) == []


def test_an_area_in_dispute_is_published_with_both_numbers():
    current = {'conflicts': ['sale_scope'], 'listing_errors': [{'field': 'sale_area_m2', 'chosen': {'value': 678.02, 'grade': 'resmi'}, 'other': {'value': 3260.0, 'grade': 'beyan'}}]}
    assert projection.area_conflict(current) == {'statedM2': 3260.0, 'officialM2': 678.02}
    assert projection.area_conflict({'conflicts': [], 'listing_errors': current['listing_errors']}) is None
