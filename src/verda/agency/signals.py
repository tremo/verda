"""What the records say beyond today's price: how the asking price has moved, who is selling, and what the owner marked on the dashboard.

Pure reads over what was already observed; nothing here opens a page. A seller who keeps cutting the price tells more than the price itself:
the last figure is probably not the final one. That earns a small, visible addition to the opportunity score, never more than MAX_BONUS points.
"""
import json
import re
import sqlite3

from verda.facts.text import fold

WINDOW_DAYS = 45                # how far back a price cut still says something about the seller today
MIN_DROP_PCT = 5                # smaller moves are rounding, not a signal
MAX_BONUS = 6
RECENT_DAYS = 7                 # "just changed": worth telling the owner about


def steps(points):
    """The price each time it changed, oldest first: [(first seen at, price)] from every sighting [(at, price)]."""
    out = []
    for at, price in sorted(points):
        if not out or out[-1][1] != price:
            out.append((at, price))
    return out


def price_history(store, mode='local'):
    """{listing id: [(at, price)]} from every search row and every listing read this system recorded."""
    seen = {}
    with store.transaction() as con:
        for row in con.execute("SELECT captured_at, payload FROM observations WHERE tool='sahibinden.search' AND mode=? ORDER BY captured_at, id", (mode,)):
            try:
                listings = json.loads(row['payload']).get('listings') or []
            except (ValueError, AttributeError):
                continue
            for item in listings:
                if isinstance(item, dict) and item.get('listing_id') and isinstance(item.get('price_tl'), (int, float)) and item['price_tl'] > 0:
                    seen.setdefault(str(item['listing_id']), []).append((row['captured_at'], float(item['price_tl'])))
        for row in con.execute("SELECT listing_ref, captured_at, json_extract(payload, '$.price_tl') AS price FROM observations "
                               "WHERE tool='sahibinden.read_listing' AND mode=? AND listing_ref IS NOT NULL ORDER BY captured_at, id", (mode,)):
            if isinstance(row['price'], (int, float)) and row['price'] > 0:
                seen.setdefault(row['listing_ref'], []).append((row['captured_at'], float(row['price'])))
    return {listing: steps(points) for listing, points in seen.items()}


def trend(history, now, window_days=WINDOW_DAYS):
    """How the asking price moved inside the window, or None when it did not move there.
    -> {from, to, change_pct (negative: cheaper), drops, rises, last_move, last_change_at, since_days, steps}"""
    if not history or len(history) < 2:
        return None
    opened = now - window_days * 86400
    before = [(at, price) for at, price in history if at <= opened]
    inside = [(at, price) for at, price in history if at > opened]
    start = before[-1] if before else inside[0]
    moves = inside if before else inside[1:]
    if not moves:
        return None
    path = [start] + moves
    prices = [price for _, price in path]
    pairs = list(zip(prices, prices[1:]))
    # Measured from the highest price of the window; a price that only went up is measured from where the window found it.
    base_at, base = max(path, key=lambda step: step[1])
    if prices[-1] >= base:
        base_at, base = path[0]
    return {'from': base, 'to': prices[-1], 'change_pct': round(100 * (prices[-1] - base) / base, 1),
            'drops': sum(b < a for a, b in pairs), 'rises': sum(b > a for a, b in pairs), 'last_move': 'down' if prices[-1] < prices[-2] else 'up',
            'last_change_at': moves[-1][0], 'since_days': max(1, round((now - max(base_at, opened)) / 86400)), 'steps': [[at, price] for at, price in path[-6:]]}


def bonus(moved):
    """Points for a price that keeps coming down. 0 for a move too small to mean anything, for a price that went back up, and for no move."""
    if not moved or moved['last_move'] != 'down' or -moved['change_pct'] < MIN_DROP_PCT:
        return 0
    return min(MAX_BONUS, int(-moved['change_pct'] // 5) + (1 if moved['drops'] >= 2 else 0))


def describe(moved):
    """'34 günde %23 indi (3 indirim)'; None when there is nothing to say."""
    if not moved or abs(moved['change_pct']) < 1:
        return None
    pct = f"%{abs(moved['change_pct']):g}".replace('.', ',')
    if moved['change_pct'] < 0:
        return f"{moved['since_days']} günde {pct} indi" + (f" ({moved['drops']} indirim)" if moved['drops'] > 1 else '')
    return f"{moved['since_days']} günde {pct} arttı"


_YEARS = re.compile(r'\d{1,2}\.?')


def seller_of(payload):
    """Who is selling, as the listing page shows it: {'kind': 'owner' | 'agency', 'store': the office's name or None}. None when the page does not say.
    An owner's name is not on the page (only the month the account was opened), so owners are not told apart from one another here."""
    if not isinstance(payload, dict):
        return None
    said = fold(str((payload.get('attributes') or {}).get('kimden') or '').strip())
    if not said:
        return None
    if said == 'sahibinden':
        return {'kind': 'owner', 'store': None}
    lines = [line.strip() for line in str(payload.get('page_text') or '').splitlines() if line.strip()]
    store = None
    for i in range(1, len(lines) - 1):
        if lines[i] == 'YIL' and _YEARS.fullmatch(lines[i - 1]):           # the office's card: "<n>." / "YIL" / the office's name
            store = lines[i + 1][:80]
            break
    return {'kind': 'agency', 'store': store}


def sellers(store, mode='local'):
    """{listing id: seller_of(newest read)}"""
    out = {}
    with store.transaction() as con:
        for row in con.execute("SELECT listing_ref, payload FROM observations WHERE tool='sahibinden.read_listing' AND mode=? AND listing_ref IS NOT NULL ORDER BY captured_at, id", (mode,)):
            try:
                found = seller_of(json.loads(row['payload']))
            except ValueError:
                continue
            if found:
                out[row['listing_ref']] = found
    return out


MARKS = ('shortlist', 'maybe', 'excluded')


def user_marks(store):
    """The owner's own marks from the dashboard, the newest per listing: {listing id: 'shortlist' | 'maybe' | 'excluded'}. A mark taken back is gone."""
    try:
        with store.transaction() as con:
            row = con.execute("SELECT value FROM runtime_settings WHERE key='feedback:imported'").fetchone()
    except sqlite3.OperationalError:                       # a store the dashboard's feedback has never been read into
        return {}
    newest = {}
    for entry in json.loads(row[0]) if row else []:
        listing, _, signature = str(entry).partition(':')
        if signature.startswith('decision='):
            mark, _, at = signature[len('decision='):].partition('@')
            if at >= newest.get(listing, ('', ''))[1]:
                newest[listing] = (mark, at)
    return {listing: mark for listing, (mark, _) in newest.items() if mark in MARKS}
