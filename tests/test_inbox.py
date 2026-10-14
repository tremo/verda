from verda.agency import inbox


def test_a_conversation_with_unread_messages_is_read_even_when_its_time_column_is_empty(monkeypatch):
    seen = {(inbox.fold('Arsa A'), inbox.fold('Ali'), None), (inbox.fold('Arsa B'), inbox.fold('Veli'), None)}
    monkeypatch.setattr(inbox, 'read_markers', lambda store: seen)
    monkeypatch.setattr(inbox, 'legacy_lifecycles', lambda store: {})
    from collections import Counter
    monkeypatch.setattr(inbox, 'failed_row_counts', lambda store, now=None: Counter())
    rows = [{'title': 'Arsa A', 'counterpart': 'Ali', 'last_at_text': None, 'unread': 1},      # read before, a new message arrived
            {'title': 'Arsa B', 'counterpart': 'Veli', 'last_at_text': None, 'unread': 0}]     # read before, nothing new
    plan = {r['title']: why for r, why in inbox.plan(None, rows)}
    assert plan == {'Arsa A': 'read', 'Arsa B': 'skip:already_read'}
    settled = inbox.settled_check(None)
    assert settled(rows[0]) is False and settled(rows[1]) is True


def test_a_row_that_failed_twice_today_is_left_alone(monkeypatch):
    from collections import Counter
    monkeypatch.setattr(inbox, 'read_markers', lambda store: set())
    monkeypatch.setattr(inbox, 'legacy_lifecycles', lambda store: {})
    monkeypatch.setattr(inbox, 'failed_row_counts', lambda store, now=None: Counter({(inbox.fold('Arsa A'), inbox.fold('Ali')): 2}))
    rows = [{'title': 'Arsa A', 'counterpart': 'Ali', 'last_at_text': '10:00', 'unread': 1}, {'title': 'Arsa B', 'counterpart': 'Veli', 'last_at_text': '10:05', 'unread': 1}]
    assert {r['title']: why for r, why in inbox.plan(None, rows)} == {'Arsa A': 'skip:stuck', 'Arsa B': 'read'}


class _Store:
    """The two queries inbox.known_titles / found_before make, on a real SQLite table."""
    def __init__(self, path):
        import sqlite3
        self.path = path
        con = sqlite3.connect(path)
        con.execute('CREATE TABLE observations(id TEXT, tool TEXT, mode TEXT, listing_ref TEXT, captured_at REAL, payload TEXT)')
        con.commit()
        con.close()

    def add(self, tool, at, payload, listing_ref=None):
        import json, sqlite3
        con = sqlite3.connect(self.path)
        con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)', (str(at), tool, 'local', listing_ref, at, json.dumps(payload)))
        con.commit()
        con.close()

    def transaction(self):
        import contextlib, sqlite3

        @contextlib.contextmanager
        def tx():
            con = sqlite3.connect(self.path)
            con.row_factory = sqlite3.Row
            try:
                yield con
                con.commit()
            finally:
                con.close()
        return tx()


def test_a_short_list_marked_complete_does_not_hide_conversations_from_a_longer_one(tmp_path):
    # 14 Oct 2026: a recent read stalled at 30 rows and called itself complete; the 216-row list of 11 Oct still held the conversation
    store = _Store(tmp_path / 'a.sqlite')
    rows = lambda *titles: [{'title': t, 'counterpart': None, 'last_at_text': None, 'unread': 0} for t in titles]
    store.add('sahibinden.list_inbox', 100.0, {'complete': True, 'rows': rows(*[f'Arsa {i}' for i in range(216)], 'REST EMLAK- KAÇIRILMAYACAK FIRSAT')})
    store.add('sahibinden.list_inbox', 200.0, {'complete': True, 'rows': rows(*[f'Arsa {i}' for i in range(30)])})
    titles = inbox.known_titles(store, now=300.0)
    assert inbox.fold('REST EMLAK- KAÇIRILMAYACAK FIRSAT') in titles
    store.add('sahibinden.list_inbox', 250.0, {'complete': True, 'rows': rows(*[f'Arsa {i}' for i in range(210)])})     # a genuine full list later: it is the base
    assert inbox.fold('REST EMLAK- KAÇIRILMAYACAK FIRSAT') not in inbox.known_titles(store, now=300.0)


def test_a_conversation_opened_once_is_never_reported_missing_without_looking(tmp_path, monkeypatch):
    import pytest
    from verda.agency import jev
    store = _Store(tmp_path / 'a.sqlite')
    store.add('sahibinden.read_thread', 10.0, {'found': True, 'messages': [{'role': 'seller', 'text': 'x'}]}, listing_ref='1214768369')
    assert inbox.found_before(store, '1214768369') and not inbox.found_before(store, '1000000001')
    monkeypatch.setattr(jev, 'listing_title', lambda store, listing_id: 'REST EMLAK- KAÇIRILMAYACAK FIRSAT')
    monkeypatch.setattr(inbox, 'known_titles', lambda store: set())       # the held list does not name it

    class Inbox:
        """An inbox that shows rows but never the wanted one."""
        url = jev.INBOX_URL
        def __init__(self, url): self.closed = False
        def evaluate(self, script):
            if script == jev.THREAD_READ: return {'url': self.url, 'inbox_rows': 3, 'panel': False, 'messages': []}
            if script == jev.READ_STATE: return {}
            if script == jev.ROW_TEXTS: return ['Arsa 1\nAli', 'Arsa 2\nVeli', 'Arsa 3\nAyşe']
            return False                                                   # the list does not move
        def close(self): self.closed = True

    opened = []
    factory = lambda url: opened.append(url) or Inbox(url)
    job = {'arguments': {'listing_id': '1214768369'}}
    phases = []
    with pytest.raises(jev.JevStopped):                                   # looked, did not find it: a reading failure, not "no conversation"
        jev.ThreadReader(browser_factory=factory, sleep=lambda s: None).read(store, job, lambda p, d=None: phases.append((p, d)))
    assert opened and ('read_failed', 'known_conversation_not_found') in phases
    # a listing never seen in a conversation still takes the shortcut: no inbox opened, "not found"
    opened.clear()
    receipt = jev.ThreadReader(browser_factory=factory, sleep=lambda s: None).read(store, {'arguments': {'listing_id': '1000000001'}}, lambda p, d=None: None)
    assert receipt['found'] is False and not opened
