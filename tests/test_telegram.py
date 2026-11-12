"""Verda over Telegram: one paired chat, commands answered from code, questions handed to the chat agent. Telegram itself is faked; all data is synthetic."""
import io
import json
import stat
import time
import urllib.error

import pytest

from test_chat import ROWS, make_store
from verda.agency import chat, telegram

OWNER, STRANGER = 4242, 9191
TOKEN = '123456:synthetic-token-for-tests'
MORNING = time.mktime((2026, 11, 12, 7, 0, 0, 0, 0, -1))              # 07:00 local: before the daily brief


class FakeApi:
    def __init__(self):
        self.sent, self.updates, self.calls, self.fail_send = [], [], [], None

    def call(self, method, params=None, timeout=20.0):
        self.calls.append((method, params))
        if method == 'getUpdates':
            out = [u for u in self.updates if u['update_id'] >= params['offset']]
            self.updates = []
            return out
        if method == 'sendMessage':
            if self.fail_send:
                raise self.fail_send
            self.sent.append((params['chat_id'], params['text']))
        return True

    def say(self, update_id, text, user=OWNER, kind='private', **extra):
        self.updates.append({'update_id': update_id, 'message': {'chat': {'id': user, 'type': kind}, 'from': {'id': user, 'first_name': 'Deniz', **extra}, 'text': text}})

    def texts(self, user=OWNER):
        return [text for chat_id, text in self.sent if chat_id == user]


class Clock:
    def __init__(self, at=MORNING):
        self.at = at

    def __call__(self):
        return self.at


def paired(tmp_path, monkeypatch):
    store, config = make_store(tmp_path, monkeypatch)
    api, clock = FakeApi(), Clock()
    bridge = telegram.Bridge(store, api, now=clock)
    api.say(1, '/start')
    bridge.poll()
    code = api.texts()[-1].split('Eşleştirme kodu: ')[1].split('\n')[0]
    assert telegram.pair(store, code, now=clock()) == 'Deniz'
    api.sent.clear()
    return store, config, api, clock, bridge


def test_only_the_token_line_is_read_from_the_owners_file(tmp_path):
    env = tmp_path / 'telegram.env'
    assert telegram.read_token(env) is None                                                # no file yet: the bridge waits
    env.write_text(f'# bot\nOTHER_SECRET=never-read\nexport VERDA_TELEGRAM_TOKEN="{TOKEN}"\n')
    assert telegram.read_token(env) == TOKEN
    env.write_text('VERDA_TELEGRAM_TOKEN=\n')
    assert telegram.read_token(env) is None


def test_a_chat_becomes_the_owners_only_when_its_code_is_confirmed_on_the_mac(tmp_path, monkeypatch):
    store, _ = make_store(tmp_path, monkeypatch)
    api, clock = FakeApi(), Clock()
    bridge = telegram.Bridge(store, api, now=clock)
    api.say(1, 'merhaba')                                                                  # not paired and not /start: silence
    api.say(2, '/start', kind='group')                                                     # never in a group
    api.say(3, '/start', is_bot=True)
    bridge.poll()
    assert api.sent == [] and telegram.load_state(store)['offset'] == 4
    api.say(4, '/start')
    api.say(5, '/start', user=STRANGER)
    bridge.poll()
    mine, theirs = api.texts(OWNER)[0], api.texts(STRANGER)[0]
    code, other = (t.split('Eşleştirme kodu: ')[1].split('\n')[0] for t in (mine, theirs))
    assert code != other and len(code) == 6 and f'verda agency-telegram-pair {code}' in mine
    api.say(6, '/start')
    bridge.poll()
    assert api.texts(OWNER)[1] == mine                                                     # asking again shows the same code, not a new one
    api.say(7, '/bugun')
    bridge.poll()
    assert len(api.texts(OWNER)) == 2                                                      # a code alone opens nothing
    assert telegram.pair(store, '000000', now=clock()) is None and telegram.load_state(store)['chat_id'] is None
    assert telegram.pair(store, code, now=clock() + telegram.PAIR_SECONDS + 1) is None     # an old code is worth nothing
    assert telegram.pair(store, code, now=clock()) == 'Deniz' and telegram.load_state(store)['chat_id'] == OWNER
    assert telegram.pair(store, other, now=clock()) is None                                # the stranger's code died with the pairing
    api.sent.clear()
    api.say(8, '/bugun', user=STRANGER)
    api.say(9, '/start', user=STRANGER)
    api.say(10, '/yardim')
    bridge.poll()
    assert api.texts(STRANGER) == [] and api.texts(OWNER) == [telegram.HELP]               # everyone else gets no answer at all
    assert telegram.forget(store) is True and telegram.load_state(store)['chat_id'] is None and telegram.forget(store) is False


def test_no_more_than_a_few_chats_can_hold_a_code_at_once(tmp_path, monkeypatch):
    store, _ = make_store(tmp_path, monkeypatch)
    api = FakeApi()
    bridge = telegram.Bridge(store, api, now=Clock())
    for n in range(telegram.MAX_PENDING + 2):
        api.say(n + 1, '/start', user=1000 + n)
    bridge.poll()
    assert len(api.sent) == telegram.MAX_PENDING and len(telegram.load_state(store)['pending']) == telegram.MAX_PENDING


def test_the_state_file_is_private_and_holds_neither_the_token_nor_a_message(tmp_path, monkeypatch):
    store, _, api, clock, bridge = paired(tmp_path, monkeypatch)
    api.say(2, 'çok özel bir soru')
    bridge.poll()
    path = telegram.state_path(store)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_text()
    assert 'özel' not in raw and TOKEN not in raw and json.loads(raw)['chat_id'] == OWNER and len(json.loads(raw)['waiting']) == 1


def test_commands_are_answered_from_the_records_without_a_model(tmp_path, monkeypatch):
    store, _, api, clock, bridge = paired(tmp_path, monkeypatch)
    monkeypatch.setattr(chat, 'system', lambda store, args=None, mode='local': {
        'health': [{'level': 'ok', 'text': 'Panel: çalışıyor'}, {'level': 'bad', 'text': 'Kaynak engelli: tkgm · http_403 (açılalı 2 sa)'}],
        'listings_by_state': {'Uygun': 3, 'Koşullu': 12, 'Araştırılıyor': 140}, 'waiting_work': [{'action': 'tkgm.lookup', 'label': 'TKGM parsel sorgusu', 'waiting': 17}],
        'waiting_total': 17, 'skipped': {}, 'stuck_last_6h': [], 'done_today': [{'tool': 'geo.route', 'label': 'Rota', 'done': 9, 'failed': 0}], 'sender_mode': 'off', 'generated_at': 'x'})
    monkeypatch.setattr(chat, 'today', lambda store, args=None, mode='local': {'items': [{'title': 'x'}], 'counts': {}, 'text': '1. Taraması bitti, puanı yüksek\n   #1000000001'})
    for n, text in enumerate(['/liste 2', '/ilan 1000000004', '/ilan 1000000003', '/ilan 5555555555', '/durum', '/bugun', '/bilinmeyen', '/liste@VerdaBot'], start=2):
        api.say(n, text)
    bridge.poll()
    top, cut, unsure, missing, system, today, unknown, mention = api.texts()
    first = top.split('\n\n')[0].splitlines()
    assert len(top.split('\n\n')) == 2 and first[0] == ('#1000000001 · Yaka Mh. · 153/1 · 5 M ₺ · 5.000 m² · puan 78 · TKGM: https://parselsorgu.tkgm.gov.tr/#ara/idari/100/153/1 '
                                                        '· Sahibinden: https://www.sahibinden.com/1000000001')          # number, parcel and both links on one line
    assert first[1] == 'Olası fırsat · beklenenin %21,5 altında · %8 eğim'
    assert 'Olası fırsat: ilan 1.333 ₺/m², beklenen ≈ 1.275 ₺/m² (%21,5 altında)' in cut and 'Fiyat: 2,4 M ₺ → 2 M ₺ (12 günde %16,7 indi)' in cut
    assert 'Arazi: %8 eğim (hafif eğimli), güney yönüne bakıyor, rakım 140 m, denize 2,5 km' in cut and 'Arazi kalitesi 72 · yol: var · Mertur 15 dk' in cut
    assert 'TKGM: resmî sorgu henüz yapılmadı' in unsure and 'Durum: araştırılıyor (puan geçici) · eksik: yol' in unsure and 'Arazi: %24 eğim (dik)' in unsure
    assert missing.startswith('Bu numara adaylar arasında yok')
    assert system.splitlines()[:2] == ['Dikkat isteyen:', '- Kaynak engelli: tkgm · http_403 (açılalı 2 sa)'] and 'Panel: çalışıyor' not in system
    assert 'İlanlar: Uygun 3 · Koşullu 12 · Araştırılıyor 140' in system and 'Bekleyen iş: TKGM parsel sorgusu 17' in system and system.endswith('Mesaj gönderimi: off')
    assert telegram.line({**chat.listings(store, {'ids': ['1000000003']})['listings'][0], 'parcel': None}).count('TKGM: parsel henüz belli değil') == 1
    assert today.startswith('1. Taraması bitti') and unknown == telegram.HELP and len(mention.split('\n\n')) == 4
    with store.transaction() as con:
        assert con.execute('SELECT count(*) FROM inbox').fetchone()[0] == 0                # no command opened a task


def test_a_question_goes_to_the_chat_agent_and_its_answer_comes_back_once(tmp_path, monkeypatch):
    store, _, api, clock, bridge = paired(tmp_path, monkeypatch)
    api.say(2, 'En düz arazi hangisi?')
    api.say(3, 'bir soru daha')
    bridge.poll()
    assert api.texts() == ['Önceki sorunu cevaplıyorum; bitince bunu yeniden yaz.']        # one question at a time
    with store.transaction() as con:
        task = con.execute('SELECT * FROM inbox').fetchone()
    assert (task['agent'], task['source'], task['priority'], task['state']) == ('verda', 'chat', 90, 'queued') and json.loads(task['inputs'])['message'] == 'En düz arazi hangisi?'
    bridge.tick()
    assert api.texts() == api.texts()[:1] and ('sendChatAction', {'chat_id': OWNER, 'action': 'typing'}) in api.calls
    with store.transaction() as con:
        con.execute("UPDATE inbox SET state='complete', result=? WHERE id=?", (json.dumps({'summary': 'özet', 'data': {'answer': 'En düzü #1000000001.'}}), task['id']))
    bridge.tick()
    bridge.tick()
    assert api.texts()[1:] == ['En düzü #1000000001.'] and telegram.load_state(store)['waiting'] == {}
    api.say(4, 'peki ikincisi?')
    bridge.poll()
    with store.transaction() as con:
        second = con.execute("SELECT inputs FROM inbox WHERE id!=?", (task['id'],)).fetchone()
    assert json.loads(second['inputs'])['history'] == [{'question': 'En düz arazi hangisi?', 'answer': 'En düzü #1000000001.'}]      # the thread of the talk goes with it
    api.say(5, 'x' * (chat.QUESTION_MAX + 1))
    with store.transaction() as con:
        con.execute("UPDATE inbox SET state='blocked', reason='turn_error:ValueError' WHERE state='queued'")
    bridge.tick()
    bridge.poll()
    assert api.texts()[-2].startswith('Bu soruyu cevaplayamadım: cevap hazırlanırken hata oldu') and api.texts()[-1].startswith('Soru çok uzun')


def test_an_answer_survives_a_restart_and_a_failed_send(tmp_path, monkeypatch):
    store, _, api, clock, bridge = paired(tmp_path, monkeypatch)
    api.say(2, 'soru')
    bridge.poll()
    task = next(iter(telegram.load_state(store)['waiting']))
    with store.transaction() as con:
        con.execute("UPDATE inbox SET state='complete', result=? WHERE id=?", (json.dumps({'summary': 'Cevap.', 'data': {}}), task))
    restarted = telegram.Bridge(store, api, now=clock)                                     # the service was restarted while the agent worked
    api.fail_send = telegram.Unreachable('network')
    with pytest.raises(telegram.Unreachable):
        restarted.tick()
    assert task in telegram.load_state(store)['waiting']                                   # not delivered: kept
    api.fail_send = None
    restarted.tick()
    assert api.texts() == ['Cevap.'] and telegram.load_state(store)['waiting'] == {}


def test_a_late_answer_is_said_to_be_late_and_a_question_nobody_took_is_withdrawn(tmp_path, monkeypatch):
    store, _, api, clock, bridge = paired(tmp_path, monkeypatch)
    api.say(2, 'işçi kapalıyken')
    bridge.poll()
    clock.at += telegram.ANSWER_SECONDS + 1
    bridge.tick()
    assert api.texts()[-1].startswith('Sorunu cevaplayamadım: ajan işçisi meşgul ya da kapalı') and telegram.load_state(store)['waiting'] == {}
    with store.transaction() as con:
        assert con.execute('SELECT state, reason FROM inbox').fetchone()[:] == ('cancelled', 'chat_timed_out')
    api.say(3, 'uzun süren soru')
    bridge.poll()
    running = store.claim(mode='local', now=clock())
    clock.at += telegram.ANSWER_SECONDS + 1
    bridge.tick()
    bridge.tick()
    assert api.texts()[-1] == 'Cevap uzun sürdü; bitince gönderirim. Bu arada başka soru yazabilirsin.' and api.texts().count(api.texts()[-1]) == 1
    api.say(4, 'bu arada başka soru')                                                      # a late one does not hold the next question back
    bridge.poll()
    assert len(telegram.load_state(store)['waiting']) == 2
    with store.transaction() as con:
        con.execute("UPDATE inbox SET state='complete', token=NULL, result=? WHERE id=?", (json.dumps({'summary': 'Geç gelen cevap.'}), running['id']))
    bridge.tick()
    assert api.texts()[-1] == 'Geç gelen cevap.' and len(telegram.load_state(store)['waiting']) == 1


def test_the_days_decisions_are_sent_once_a_day_after_nine_and_can_be_switched_off(tmp_path, monkeypatch):
    store, _, api, clock, bridge = paired(tmp_path, monkeypatch)
    picked = {'items': [{'title': 'a'}, {'title': 'b'}], 'counts': {}, 'text': '1. a\n2. b'}
    monkeypatch.setattr(chat, 'today', lambda store, args=None, mode='local': picked)
    bridge.tick()
    assert api.sent == []                                                                  # 07:00: too early
    clock.at += 2.5 * 3600
    bridge.tick()
    bridge.tick()
    assert len(api.sent) == 1 and api.texts()[0].startswith('Günaydın. Bugün 2 konu var:\n\n1. a\n2. b')
    clock.at += 86400
    api.fail_send = telegram.Unreachable('network')
    with pytest.raises(telegram.Unreachable):
        bridge.tick()
    api.fail_send = None
    bridge.tick()
    assert len(api.sent) == 2                                                              # a send that failed is tried again, then the day is done
    api.say(2, '/ozet kapat')
    bridge.poll()
    assert api.texts()[-1] == 'Sabah özeti kapalı.'
    clock.at += 86400
    bridge.tick()
    assert len(api.sent) == 3
    api.say(3, '/ozet')
    api.say(4, '/ozet ac')
    bridge.poll()
    assert api.texts()[-2:] == ['Sabah özeti kapalı. Değiştirmek için: /ozet kapat ya da /ozet ac', 'Sabah özeti açık.']
    picked['items'] = []
    clock.at += 86400
    bridge.tick()
    assert len(api.sent) == 5                                                              # nothing to decide: no message


def test_one_message_that_cannot_be_handled_does_not_stop_the_bridge_or_get_handled_twice(tmp_path, monkeypatch):
    store, _, api, clock, bridge = paired(tmp_path, monkeypatch)
    monkeypatch.setattr(chat, 'today', lambda *a, **k: 1 / 0)
    api.say(2, '/bugun')
    api.say(3, '/yardim')
    bridge.poll()
    assert api.texts() == ['Bunu işleyemedim (içeride bir hata oldu). /durum ile sisteme bakabilirsin.', telegram.HELP] and telegram.load_state(store)['offset'] == 4
    assert bridge.poll() == 0


def test_a_long_answer_is_sent_in_parts_cut_at_line_ends(tmp_path, monkeypatch):
    store, _, api, clock, bridge = paired(tmp_path, monkeypatch)
    text = '\n'.join(f'satır {n} ' + 'x' * 90 for n in range(100))
    bridge.send(OWNER, text)
    parts = api.texts()
    assert len(parts) == 3 and all(len(p) <= telegram.CHUNK for p in parts) and '\n'.join(parts) == text


def test_the_service_waits_for_a_token_says_how_it_is_doing_and_never_leaks_the_token(tmp_path, monkeypatch):
    store, _ = make_store(tmp_path, monkeypatch)
    env = tmp_path / 'telegram.env'
    assert telegram.health(store) is None                                                  # never started here: the status page says nothing
    assert telegram.run(store, env_file=env) == {'state': 'no_token'}
    assert telegram.health(store) == {'level': 'warn', 'text': 'Telegram: bot anahtarı girilmemiş'}
    env.write_text(f'VERDA_TELEGRAM_TOKEN={TOKEN}\n')
    api = FakeApi()
    seen = []
    factory = lambda token: seen.append(token) or api
    assert telegram.run(store, env_file=env, api_factory=factory, now=Clock()) == {'state': 'waiting_pairing'} and seen == [TOKEN]
    assert telegram.health(store, now=MORNING)['level'] == 'warn' and 'eşleştirme bekliyor' in telegram.health(store, now=MORNING)['text']
    assert telegram.health(store, now=MORNING + telegram.STALE_SECONDS + 60)['level'] == 'bad'           # silent for a long time: the service is probably down

    class Refusing:
        def __init__(self, error):
            self.error = error

        def call(self, *a, **k):
            raise self.error
    for error, state in ((telegram.BadToken(), 'bad_token'), (telegram.Busy(), 'busy'), (telegram.Unreachable('network'), 'offline')):
        assert telegram.run(store, env_file=env, api_factory=lambda token, error=error: Refusing(error), now=Clock()) == {'state': state}
    assert telegram.health(store, now=MORNING)['text'] == "Telegram'a ulaşılamıyor (ağ)"

    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'{}'))
    monkeypatch.setattr(telegram.urllib.request, 'urlopen', refuse)
    with pytest.raises(telegram.BadToken) as caught:
        telegram.Api(TOKEN).call('getMe')
    assert TOKEN not in repr(caught.value) and caught.value.__cause__ is None and caught.value.__suppress_context__      # the url with the token goes nowhere

    def slow_down(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 429, 'Too Many Requests', {}, io.BytesIO(b'{"ok":false,"parameters":{"retry_after":45}}'))
    monkeypatch.setattr(telegram.urllib.request, 'urlopen', slow_down)
    with pytest.raises(telegram.Busy) as busy:
        telegram.Api(TOKEN).call('getUpdates')
    assert busy.value.retry_after == 45 and TOKEN not in repr(busy.value)
    assert TOKEN not in telegram.status_path(store).read_text() and not telegram.state_path(store).exists()       # nothing was said to the bot: no state yet


def test_the_status_page_carries_the_bridges_line_only_where_the_bridge_runs(tmp_path, monkeypatch):
    from verda.publish import status
    store, _ = make_store(tmp_path, monkeypatch)
    assert not any('Telegram' in h['text'] for h in status.build(store, now=MORNING, running={})['health'])
    telegram.write_status(store, 'running', MORNING - 30)
    assert {'level': 'ok', 'text': 'Telegram: bağlı'} in status.build(store, now=MORNING, running={})['health']
