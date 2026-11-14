"""Verda over Telegram: the owner's questions in, answers and the day's decisions out.

Outbound only: the service asks Telegram for new messages (long polling); nothing listens for a connection. Exactly one chat is paired, by a code
the owner confirms on this Mac; every other chat gets no answer. The bot's token is read from an env file the owner fills in; this code never
writes it anywhere and never puts it in a log or an error. Message texts pass through Telegram's servers: that was the owner's choice.

Commands answer from code (no model): /bugun, /liste, /ilan <no>, /durum, /ozet. Anything else is a question for the chat agent (agency/chat.py).
Nothing here changes a record, sends a seller a message or resumes a source.
"""
import fcntl
import http.client
import json
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from verda.agency import chat
from verda.agency.decisions import money

TOKEN_KEY = 'VERDA_TELEGRAM_TOKEN'
DEFAULT_ENV = Path('~/Library/Application Support/Verda/secrets/telegram.env')
API = 'https://api.telegram.org/bot{token}/{method}'
PAIR_SECONDS = 600              # a pairing code lives this long
MAX_PENDING = 3                 # chats that may hold a pairing code at once
ANSWER_SECONDS = 300            # a question waits this long for the agent before the owner is told it is late
CONFIRM_SECONDS = 600           # a proposed change waits this long for the owner's yes
LATE_SECONDS = 1800             # a late answer is still delivered within this time, then forgotten
CHUNK = 3900                    # Telegram takes 4096 characters per message
BRIEF_HOUR = 9                  # local hour from which the day's decisions are sent, once a day
STALE_SECONDS = 900             # a bridge that has not reported for this long counts as down
HELP = ('Verda. Soru sorabilirsin ya da:\n/bugun: bugün senden beklenenler\n/liste: en yüksek puanlı adaylar (/liste 10)\n'
        '/ilan <ilan no>: bir ilanın özeti\n/durum: sistemin durumu\n/ozet kapat ya da /ozet ac: sabah özetini kapatır, açar\n\n'
        'Bir ilanın bağlantısını ya da numarasını gönderirsen kayıtlardan değerlendiririm. Bir düzeltme ya da eleme istersen önce sana sorar, "evet" dersen senin kararın olarak kaydederim. Satıcıya mesaj göndermem.')
STATE_TR = {'eligible': 'taraması bitti, uygun', 'conditional': 'taraması bitti, koşullu', 'research_required': 'araştırılıyor (puan geçici)'}
MARK_TR = {'shortlist': 'kısa liste', 'maybe': 'belki', 'excluded': 'elendi'}
NO_TKGM = {True: 'TKGM: resmî sorgu henüz yapılmadı', False: 'TKGM: parsel henüz belli değil'}       # by whether the listing names a parcel; a link is never made up


class BadToken(Exception):
    """Telegram does not know this token."""


class Busy(Exception):
    """Another reader holds the bot's updates, or Telegram asks us to slow down."""

    def __init__(self, retry_after=None):
        super().__init__('busy')
        self.retry_after = retry_after


class Unreachable(Exception):
    """No usable answer from Telegram."""


def read_token(env_file):
    """The token from the owner's env file (`VERDA_TELEGRAM_TOKEN=...`); None when the file or the line is missing. Nothing else is read from it."""
    try:
        lines = Path(env_file).expanduser().read_text().splitlines()
    except (OSError, ValueError):
        return None
    for entry in lines:
        key, sep, value = entry.strip().removeprefix('export ').partition('=')
        if sep and key.strip() == TOKEN_KEY:
            value = value.strip()
            if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
                value = value[1:-1]
            return value or None
    return None


class Api:
    def __init__(self, token):
        self._token = token

    def call(self, method, params=None, timeout=20.0):
        request = urllib.request.Request(API.format(token=self._token, method=method), data=json.dumps(params or {}).encode(),
                                         headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:        # never re-raised as it is: its url carries the token
            code = error.code
            if code in (401, 404):
                raise BadToken() from None
            if code in (409, 429):
                wait = None
                try:
                    wait = (json.loads(error.read().decode('utf-8')).get('parameters') or {}).get('retry_after')
                except (OSError, ValueError, AttributeError):
                    pass
                raise Busy(wait if isinstance(wait, (int, float)) else None) from None
            raise Unreachable(f'http_{code}') from None
        except (OSError, http.client.HTTPException, ValueError):
            raise Unreachable('network') from None
        if not isinstance(data, dict) or not data.get('ok'):
            raise Unreachable('not_ok')
        return data.get('result')


# ---- state on disk: the pairing, the read position, the questions still waiting (never the token, never a message's text) ----
def state_path(store):
    return store.path.parent / 'telegram.json'


def status_path(store):
    return store.path.parent / 'telegram-status.json'


def _write(path, data):
    """Whole or not at all, readable by the owner only: a reader never sees half a file (a half-read state would look like "not paired")."""
    draft = path.with_name(path.name + '.tmp')
    fd = os.open(draft, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out)
    os.replace(draft, path)


def load_state(store):
    try:
        data = json.loads(state_path(store).read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {'chat_id': data.get('chat_id'), 'pending': data.get('pending') or {}, 'offset': int(data.get('offset') or 0), 'brief_day': data.get('brief_day'),
            'brief': data.get('brief', True) is not False, 'waiting': data.get('waiting') or {}, 'confirm': data.get('confirm')}


@contextmanager
def changing(store):
    """Read, change, write as one step. The bridge and the pairing command are two processes on one file: without this a pairing confirmed
    while the bridge saves its read position could be overwritten. Never held across a network call."""
    with open(state_path(store).with_name('telegram.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(store)
        yield state
        _write(state_path(store), state)


def pair(store, code, now=None):
    """The owner confirms on this Mac the code the bot showed: that chat becomes the one chat Verda answers. -> the chat's name, or None for a wrong or old code."""
    now = time.time() if now is None else now
    with changing(store) as state:
        waiting = state['pending'].get(str(code).strip())
        if not waiting or now - waiting['at'] > PAIR_SECONDS:
            return None
        state.update(chat_id=waiting['chat_id'], pending={}, waiting={})
        return waiting.get('name') or 'sohbet'


def forget(store):
    """Ends the pairing: the bot answers nobody until a new code is confirmed. -> True when there was a pairing."""
    with changing(store) as state:
        had = state['chat_id'] is not None
        state.update(chat_id=None, pending={}, waiting={})
        return had


def write_status(store, text, now=None):
    _write(status_path(store), {'state': text, 'checked_at': time.time() if now is None else now})


def health(store, now=None):
    """One line for the status page, or None when the bridge was never started on this machine."""
    now = time.time() if now is None else now
    try:
        data = json.loads(status_path(store).read_text())
        state, age = data['state'], now - float(data['checked_at'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if age > STALE_SECONDS:
        return {'level': 'bad', 'text': f'Telegram köprüsü {int(age // 60)} dakikadır haber vermiyor (servis kapalı olabilir)'}
    return {'running': {'level': 'ok', 'text': 'Telegram: bağlı'},
            'waiting_pairing': {'level': 'warn', 'text': "Telegram: eşleştirme bekliyor (bota /start yaz, kodu Mac'te onayla)"},
            'no_token': {'level': 'warn', 'text': 'Telegram: bot anahtarı girilmemiş'},
            'bad_token': {'level': 'bad', 'text': "Telegram: bot anahtarı geçersiz (BotFather'dan yenisini al)"},
            'busy': {'level': 'warn', 'text': 'Telegram: bot başka bir yerde de okunuyor ya da Telegram yavaşlamamızı istedi'},
            'offline': {'level': 'warn', 'text': "Telegram'a ulaşılamıyor (ağ)"},
            'storage_busy': {'level': 'warn', 'text': 'Telegram köprüsü kayıt deposunu bekliyor'}}.get(state, {'level': 'warn', 'text': f'Telegram: {state}'})


# ---- texts ----
def thousands(value):
    return f'{round(value):,}'.replace(',', '.')


def decimal(value, digits=1):
    return f'{value:.{digits}f}'.rstrip('0').rstrip('.').replace('.', ',')


def line(row):
    """One listing on one line: number, place, parcel, price, area, score and both links."""
    parts = [f"#{row['listing_id']}", row.get('neighborhood'), row.get('parcel'), money(row['price_tl']) if row.get('price_tl') else None,
             f"{thousands(row['area_m2'])} m²" if row.get('area_m2') else None, f"puan {row['score']}" if row.get('score') is not None else None,
             f"TKGM: {row['tkgm']}" if row.get('tkgm') else NO_TKGM[bool(row.get('parcel'))], f"Sahibinden: {row['sahibinden']}"]
    return ' · '.join(str(p) for p in parts if p)


def land_text(land):
    parts = [f"%{decimal(land['slope_pct'])} eğim" + (f" ({land['slope_class']})" if land.get('slope_class') else '')]
    if land.get('faces'):
        parts.append(f"{land['faces']} yönüne bakıyor")
    if land.get('elevation_m') is not None:
        parts.append(f"rakım {round(land['elevation_m'])} m")
    if land.get('coast_m') is not None:
        parts.append(f"denize {decimal(land['coast_m'] / 1000)} km")
    return ', '.join(parts)


def brief_line(row):
    """A listing in two lines, for /liste."""
    second = [str(row['label'])] if row.get('label') else []
    if row.get('gap_pct') is not None:
        second.append(f"beklenenin %{decimal(abs(row['gap_pct']))} {'altında' if row['gap_pct'] >= 0 else 'üstünde'}")
    if row.get('land'):
        second.append(f"%{decimal(row['land']['slope_pct'])} eğim")
    if row.get('state') == 'research_required':
        second.append('puan geçici')
    return line(row) + ('\n' + ' · '.join(second) if second else '')


def card(row):
    """A listing in a few lines, for /ilan."""
    out = [line(row)]
    if row.get('expected_unit_price') and row.get('unit_price') and row.get('gap_pct') is not None:
        gap = row['gap_pct']
        out.append(f"{row.get('label') or 'Fiyat'}: ilan {thousands(row['unit_price'])} ₺/m², beklenen ≈ {thousands(row['expected_unit_price'])} ₺/m² "
                   f"(%{decimal(abs(gap))} {'altında' if gap >= 0 else 'üstünde'})")
    elif row.get('label'):
        out.append(str(row['label']))
    if row.get('land'):
        out.append('Arazi: ' + land_text(row['land']))
    facts = [f"Arazi kalitesi {row['quality']}" if row.get('quality') is not None else None, f"yol: {row['road']}" if row.get('road') else None,
             f"Mertur {decimal(row['mertur_minutes'])} dk" if row.get('mertur_minutes') is not None else None]
    if any(facts):
        out.append(' · '.join(f for f in facts if f))
    move = row.get('price_move')
    if move:
        out.append(f"Fiyat: {money(move['from'])} → {money(move['to'])} ({move['since_days']} günde %{decimal(abs(move['change_pct']))} {'indi' if move['change_pct'] < 0 else 'arttı'})")
    status = [f"Durum: {STATE_TR.get(row.get('state'), row.get('state'))}"]
    if row.get('missing'):
        status.append('eksik: ' + ', '.join(str(m) for m in row['missing']))
    if row.get('owner_mark'):
        status.append('senin işaretin: ' + MARK_TR.get(row['owner_mark'], row['owner_mark']))
    out.append(' · '.join(status))
    out.extend(str(cap) for cap in row.get('caps') or [])
    return '\n'.join(out)


def system_text(view):
    bad = [h['text'] for h in view['health'] if h['level'] in ('bad', 'warn')]
    out = ['Dikkat isteyen:\n' + '\n'.join('- ' + b for b in bad) if bad else 'Dikkat isteyen bir şey yok.']
    out.append('İlanlar: ' + ' · '.join(f'{label} {n}' for label, n in view['listings_by_state'].items()))
    waits = [f"{w['label']} {w['waiting']}" for w in view['waiting_work'] if w.get('waiting')]
    out.append('Bekleyen iş: ' + (' · '.join(waits[:8]) if waits else 'yok'))
    done = [f"{d['label']} {d['done']}" for d in view['done_today'] if d.get('done')]
    if done:
        out.append('Bugün yapılan: ' + ' · '.join(done[:8]))
    if not any(b.startswith('Mesaj gönderimi') for b in bad):
        out.append(f"Mesaj gönderimi: {view['sender_mode']}")
    return '\n'.join(out)


class Bridge:
    def __init__(self, store, api, *, now=time.time):
        self.store, self.api, self.now = store, api, now
        self.questions = {}                     # task id -> the question's text (memory only; the record keeps it anyway)
        self.history = []                       # (question, answer) pairs of this run, oldest first

    # ---- out ----
    def send(self, chat_id, text):
        text = str(text or '').strip() or '(boş cevap)'
        while text:
            cut = len(text)
            if cut > CHUNK:
                at = text.rfind('\n', 0, CHUNK)
                cut = at if at > CHUNK // 2 else CHUNK
            self.api.call('sendMessage', {'chat_id': chat_id, 'text': text[:cut], 'disable_web_page_preview': True})
            text = text[cut:].lstrip('\n')

    def _typing(self, chat_id):
        try:
            self.api.call('sendChatAction', {'chat_id': chat_id, 'action': 'typing'})
        except (Unreachable, Busy):
            pass

    # ---- in ----
    def poll(self, timeout=50):
        state = load_state(self.store)
        updates = self.api.call('getUpdates', {'offset': state['offset'], 'timeout': timeout, 'allowed_updates': ['message']}, timeout=timeout + 15) or []
        for update in updates:
            try:
                self.handle(update)
            finally:                            # an update is never handled twice, whatever it did
                with changing(self.store) as state:
                    state['offset'] = max(state['offset'], int(update.get('update_id', 0)) + 1)
        return len(updates)

    def handle(self, update):
        message = update.get('message') or {}
        chat_info, sender, text = message.get('chat') or {}, message.get('from') or {}, message.get('text')
        if chat_info.get('type') != 'private' or not isinstance(text, str) or sender.get('is_bot'):
            return
        chat_id, text = chat_info.get('id'), text.strip()
        state = load_state(self.store)
        if state['chat_id'] is None:
            return self._offer_pairing(state, chat_id, sender, text)
        if chat_id != state['chat_id'] or sender.get('id') != chat_id:
            return                              # not the owner's chat: no answer at all
        try:
            self._owner(state, chat_id, text, update)
        except (BadToken, Busy, Unreachable):
            raise
        except Exception as error:              # one message that cannot be handled never stops the bridge; the owner is told, without details
            print(json.dumps({'telegram': 'handle_failed', 'error': type(error).__name__}), flush=True)
            self.send(chat_id, 'Bunu işleyemedim (içeride bir hata oldu). /durum ile sisteme bakabilirsin.')

    def _owner(self, state, chat_id, text, update):
        command, _, rest = text.partition(' ')
        command, rest = command.split('@')[0].lower(), rest.strip()
        if command in ('/start', '/yardim', '/help'):
            return self.send(chat_id, HELP)
        if command == '/bugun':
            return self.send(chat_id, chat.today(self.store)['text'])
        if command == '/durum':
            view = chat.system(self.store)
            mine = health(self.store, self.now())
            return self.send(chat_id, system_text(view) + (f"\n{mine['text']}" if mine and mine['level'] != 'ok' else ''))
        if command == '/liste':
            limit = max(1, min(20, int(rest))) if rest.isdigit() else 5
            rows = chat.listings(self.store, {'limit': limit})['listings']
            return self.send(chat_id, '\n\n'.join(brief_line(r) for r in rows) or 'Aday yok.')
        if command == '/ilan':
            wanted = ''.join(ch for ch in rest if ch.isdigit())
            rows = chat.listings(self.store, {'ids': [wanted], 'limit': 1})['listings'] if wanted else []
            return self.send(chat_id, card(rows[0]) if rows else 'Bu numara adaylar arasında yok (elenmiş, yayından kalkmış ya da hiç görülmemiş olabilir). '
                                                                'Soru olarak yazarsan kayıtlarına bakarım.')
        if command == '/ozet':
            wanted = {'kapat': False, 'ac': True, 'aç': True}.get(rest.lower())
            with changing(self.store) as state:
                if wanted is not None:
                    state['brief'] = wanted
            return self.send(chat_id, f"Sabah özeti {'açık' if state['brief'] else 'kapalı'}." + ('' if wanted is not None else ' Değiştirmek için: /ozet kapat ya da /ozet ac'))
        if text.startswith('/'):
            return self.send(chat_id, HELP)
        asked = state.get('confirm')
        if asked:                               # a change waits for a yes: only a plain yes records it, anything else drops it
            with changing(self.store) as fresh:
                fresh['confirm'] = None
            word = text.lower().strip(' .!')
            if word in ('evet', 'e', 'onay', 'onaylıyorum', 'tamam', 'kaydet') and self.now() - asked['at'] <= CONFIRM_SECONDS:
                return self.send(chat_id, '\n'.join(chat.apply(self.store, asked['changes'], self.now())))
            if word in ('evet', 'e', 'onay', 'onaylıyorum', 'tamam', 'kaydet'):
                return self.send(chat_id, 'Onay süresi doldu; değişikliği yeniden iste.')
            if word in ('hayır', 'hayir', 'h', 'vazgeç', 'vazgec', 'iptal'):
                return self.send(chat_id, 'Vazgeçildi, hiçbir şey değişmedi.')
        if any(not w.get('late') for w in state['waiting'].values()):
            return self.send(chat_id, 'Önceki sorunu cevaplıyorum; bitince bunu yeniden yaz.')
        if len(text) > chat.QUESTION_MAX:
            return self.send(chat_id, f'Soru çok uzun ({len(text)} karakter); {chat.QUESTION_MAX} karaktere sığdır.')
        now = self.now()
        task = chat.ask(self.store, text, self.history, key=f"telegram:{chat_id}:{update.get('update_id')}", now=now)
        with changing(self.store) as state:
            state['waiting'][task] = {'chat': chat_id, 'at': now, 'late': False}
        self.questions[task] = text
        self._typing(chat_id)

    def _offer_pairing(self, state, chat_id, sender, text):
        """An unpaired bot answers /start with a code; the chat becomes the owner's only when that code is confirmed on this Mac."""
        if not text.startswith('/start') or chat_id is None:
            return
        now = self.now()
        with changing(self.store) as state:
            if state['chat_id'] is not None:
                return                              # paired in the meantime
            pending = {code: p for code, p in state['pending'].items() if now - p['at'] <= PAIR_SECONDS}
            code = next((c for c, p in pending.items() if p['chat_id'] == chat_id), None)
            if code is None:
                if len(pending) >= MAX_PENDING:
                    return
                code = f'{secrets.randbelow(900000) + 100000}'
                while code in pending:
                    code = f'{secrets.randbelow(900000) + 100000}'
                pending[code] = {'chat_id': chat_id, 'name': str(sender.get('first_name') or '')[:40], 'at': now}
            state['pending'] = pending
        self.send(chat_id, f"Bu bot tek bir kişiye cevap verir. Eşleştirme kodu: {code}\nKodu 10 dakika içinde Mac'te onayla:\nverda agency-telegram-pair {code}")

    # ---- between polls ----
    def tick(self):
        """Delivers the answers that are ready and, once a day, the day's decisions."""
        now = self.now()
        for task, entry in list(load_state(self.store)['waiting'].items()):
            outcome, text = chat.answer(self.store, task)
            note = None
            if outcome == 'done':
                note = text
                changes = chat.proposed(self.store, task)
                if changes:
                    with changing(self.store) as state:
                        state['confirm'] = {'changes': changes, 'at': now}
                    note += '\n\nKaydedeyim mi?\n' + '\n'.join('- ' + chat.describe(c) for c in changes) + '\n"evet" yazarsan senin kararın olarak kaydederim; başka bir şey yazarsan vazgeçerim.'
            elif outcome == 'stopped':
                note = f'Bu soruyu cevaplayamadım: {text}. /durum ile sisteme bakabilirsin.'
            elif entry.get('late'):
                if now - entry['at'] <= LATE_SECONDS:
                    continue                                    # still being worked on; said so already
            elif now - entry['at'] <= ANSWER_SECONDS:
                self._typing(entry['chat'])
                continue
            elif chat.give_up(self.store, task, now):
                note = 'Sorunu cevaplayamadım: ajan işçisi meşgul ya da kapalı, soru sıradan çıkarıldı. /durum ile bakabilirsin; sonra yeniden yaz.'
            else:
                entry['late'] = True                            # the agent is on it: the answer comes when it is done, new questions are welcome meanwhile
                self._keep(task, entry)
                self.send(entry['chat'], 'Cevap uzun sürdü; bitince gönderirim. Bu arada başka soru yazabilirsin.')
                continue
            try:
                if note:
                    self.send(entry['chat'], note)
            except Unreachable:
                if now - entry['at'] <= LATE_SECONDS:
                    raise                                       # kept on the list: delivered on a later round rather than lost
            self._keep(task, None)
            question = self.questions.pop(task, None)
            if outcome == 'done' and question:
                self.history = (self.history + [(question, text)])[-chat.HISTORY_TURNS:]
        self._brief(now)

    def _keep(self, task, entry):
        with changing(self.store) as state:
            if entry is None:
                state['waiting'].pop(task, None)
            elif task in state['waiting']:
                state['waiting'][task] = entry

    def _brief(self, now):
        state = load_state(self.store)
        local = time.localtime(now)
        day = time.strftime('%Y-%m-%d', local)
        if state['chat_id'] is None or not state['brief'] or state['brief_day'] == day or local.tm_hour < BRIEF_HOUR:
            return
        picked = chat.today(self.store)
        if picked['items']:                           # a send that fails leaves the day unmarked: tried again on a later round
            self.send(state['chat_id'], f"Günaydın. Bugün {len(picked['items'])} konu var:\n\n{picked['text']}\n\nSabah özetini kapatmak için: /ozet kapat")
        with changing(self.store) as state:
            state['brief_day'] = day


def run(store, *, env_file=DEFAULT_ENV, continuous=False, api_factory=Api, sleep=time.sleep, now=time.time):
    """The service loop. Without a token it waits for the owner to put one in the env file; a network fault never stops it."""
    bridge, token_seen, outcome = None, None, 'no_token'
    while True:
        token = read_token(env_file)
        if not token:
            bridge, token_seen, outcome, wait = None, None, 'no_token', 60
        else:
            if bridge is None or token != token_seen:
                bridge, token_seen = Bridge(store, api_factory(token), now=now), token
            try:
                waiting = bool(load_state(store)['waiting'])
                bridge.poll(timeout=(8 if waiting else 50) if continuous else 0)
                bridge.tick()
                outcome, wait = ('running' if load_state(store)['chat_id'] is not None else 'waiting_pairing'), 0
            except BadToken:
                outcome, wait = 'bad_token', 300
                bridge = None
            except Busy as busy:
                outcome, wait = 'busy', max(30, min(600, int(busy.retry_after or 0)))
            except Unreachable:
                outcome, wait = 'offline', 30
            except sqlite3.Error:
                outcome, wait = 'storage_busy', 15
        try:
            write_status(store, outcome, now())
        except OSError:
            pass
        if not continuous:
            return {'state': outcome}
        waited = 0
        while waited < wait:                    # a corrected token is tried at once, not after the whole wait
            sleep(min(5, wait - waited))
            waited += 5
            if outcome in ('no_token', 'bad_token') and read_token(env_file) != token:
                break
