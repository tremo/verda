import sqlite3
from datetime import datetime

import pytest

from verda.agency import message as M
from verda.agency.learning import build

NOON = datetime(2026, 10, 8, 12, 0, tzinfo=M.TZ).timestamp()


def current(**kw):
    base = {'decision': {'state': 'research_required'}, 'next_action': {'action': 'ask:sit'}, 'legacy_lifecycle': None,
            'seen': {'listing_read': True, 'thread_read': True, 'tkgm_looked_up': True, 'corridor_checked': True}}
    base.update(kw)
    return base


def built_for(unknown, plan='ask:sit', **data):
    d = {'thread': None, 'checks': {}, 'tasks': [], 'incidents': [], 'flags': [], 'outbox': []}
    d.update(data)
    return build(d, unknown, {'action': plan})


def plan_for(unknown=('natural_sit', 'archaeological_sit'), cur=None, convo=None, outbox=(), now=NOON, mode='dry_run', **kw):
    cur = cur or current()
    return M.plan(listing_id='1344169039', current=cur, built=built_for(list(unknown), cur['next_action']['action']), convo=convo, outbox=list(outbox), mode=mode, now=now, **kw)


def test_the_message_is_one_text_from_a_fixed_template_in_a_fixed_order():
    assert M.render('1344169039', ['archaeological_sit', 'natural_sit']) == \
        'Merhaba, 1344169039 numaralı taşınmaz için parsel herhangi bir doğal sit statüsünde mi? Parsel herhangi bir arkeolojik sit alanında mı? Teşekkürler.'
    text = M.render('1344169039', ['natural_sit', 'archaeological_sit', 'access', 'parcel_key'])
    assert text.startswith('Merhaba, 1344169039 numaralı taşınmaz için ada/parsel bilgisini paylaşabilir misiniz? Parselin doğrudan kadastral yol cephesi var mı?')
    assert M.check_text(text, '1344169039') == text
    with pytest.raises(ValueError):
        M.render('1344169039', ['route_minutes'])
    for bad in ('Beni arayın 05321234567', 'Ben bir yapay zeka asistanıyım', 'Karavan koyabilir miyim?'):
        with pytest.raises(ValueError):
            M.check_text(bad, '1344169039')


def test_messages_go_out_only_between_0830_and_2100_istanbul_time():
    at = lambda h, m: datetime(2026, 10, 8, h, m, tzinfo=M.TZ).timestamp()
    assert not M.within_window(at(8, 29)) and M.within_window(at(8, 30)) and M.within_window(at(20, 59)) and not M.within_window(at(21, 0))
    assert plan_for(now=at(22, 0), mode='live')['reason'] == 'outside_window'
    assert plan_for(now=at(22, 0), mode='dry_run')['ok'] is True


def test_page_signals_the_standard_phone_banner_is_accepted_any_other_disclosure_stops():
    assert M.phone_banner('Mesaj gönderdiğinizde 0 532 000 00 00 numaralı telefonunuz karşı tarafla paylaşılacaktır.') == 'standard'
    assert M.phone_banner('Telefon numaranız ilan sahibine gösterilecek') == 'other'
    assert M.phone_banner('Mesajınızı yazınız') == 'none'
    assert M.unavailable_reason('Bu ilan yayında değil') == 'listing_not_live'
    assert M.unavailable_reason('İlan sahibi ilanıyla ilgili mesaj almamayı tercih ediyor') == 'seller_messages_disabled'


def test_the_gates_in_order_off_candidate_step_waiting_unresolved_nothing_dry_run():
    assert plan_for(mode='off')['reason'] == 'sender_off'
    assert plan_for(sent_this_run=1)['reason'] == 'one_message_per_run'
    assert plan_for(cur=current(decision={'state': 'excluded'}))['reason'] == 'not_a_candidate'
    assert plan_for(cur=current(legacy_lifecycle='excluded'))['reason'] == 'not_a_candidate'
    assert plan_for(cur=current(next_action={'action': 'sahibinden.read_thread'}))['reason'] == 'no_ask_step'
    asked = {'found': True, 'messages': [{'who': 'sen', 'text': 'x', 'at': None}]}
    assert plan_for(convo=asked)['reason'] == 'waiting_for_reply'
    answered = {'found': True, 'messages': [{'who': 'sen', 'text': 'x', 'at': None}, {'who': 'satıcı', 'text': 'y', 'at': None}]}
    assert plan_for(convo=answered)['ok'] is True
    assert plan_for(outbox=[{'state': 'delivery_unknown', 'fingerprint': 'z'}])['reason'] == 'delivery_unresolved'
    assert plan_for(unknown=())['reason'] == 'nothing_to_ask'
    ok = plan_for()
    assert ok['ok'] and ok['fields'] == ['natural_sit', 'archaeological_sit'] and ok['dry_run'] is True
    assert plan_for(outbox=[{'state': 'dry_run', 'fingerprint': ok['fingerprint']}])['reason'] == 'dry_run_done'
    assert plan_for(dry_runs=3)['reason'] == 'dry_run_limit'
    assert plan_for(mode='live', outbox=[{'state': 'dry_run', 'fingerprint': ok['fingerprint']}])['ok'] is True        # a dry run does not block the real one
    assert plan_for(mode='live', outbox=[{'state': 'confirmed', 'fingerprint': ok['fingerprint']}])['reason'] == 'already_asked'


def test_everything_missing_goes_in_one_message_but_the_road_waits_for_the_official_check():
    cur = current(next_action={'action': 'ask:parcel_identity'}, seen={'listing_read': True, 'thread_read': True, 'tkgm_looked_up': False, 'corridor_checked': False})
    full = plan_for(unknown=('parcel_key', 'access', 'natural_sit', 'archaeological_sit'), cur=cur)
    assert full['fields'] == ['parcel_key', 'access', 'natural_sit', 'archaeological_sit']
    cur = current(seen={'listing_read': True, 'thread_read': True, 'tkgm_looked_up': True, 'corridor_checked': False})
    assert plan_for(unknown=('access', 'natural_sit'), cur=cur)['fields'] == ['natural_sit']                  # geometry first, then ask
    cur = current(next_action={'action': 'ask:parcel_identity'}, seen={'listing_read': True, 'thread_read': False})
    assert plan_for(unknown=('parcel_key', 'natural_sit'), cur=cur)['fields'] == ['parcel_key']                # sits only after the conversation was read


@pytest.fixture
def con(tmp_path):
    c = sqlite3.connect(tmp_path / 'o.sqlite')
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_outbox_prepare_is_idempotent_and_claim_checkpoint_finish_follow_the_states(con):
    first = M.prepare(con, listing_ref='1234567890', mode='local', fields=['natural_sit'], text='t', now=1.0)
    again = M.prepare(con, listing_ref='1234567890', mode='local', fields=['natural_sit'], text='t', now=2.0)
    assert first['id'] == again['id'] and first['state'] == 'prepared'
    M.claim(con, first['id'], 'call1', 'task1', 3.0, live=False)
    with pytest.raises(ValueError):
        M.claim(con, first['id'], 'call2', 'task1', 3.5, live=False)                       # one tool call at a time
    assert M.checkpoint(con, first['id'], 'call2', first['text_hash'], 4.0) is False       # not the owner
    assert M.checkpoint(con, first['id'], 'call1', 'other-hash', 4.0) is False             # not the approved text
    assert M.checkpoint(con, first['id'], 'call1', first['text_hash'], 4.0) is True
    assert M.checkpoint(con, first['id'], 'call1', first['text_hash'], 4.1) is False       # intent is recorded once
    assert M.clicked_possibly(con, first['id']) is True
    assert M.finish(con, first['id'], 'call1', 'not_sent', 5.0, reason='after_click_stop') == 'delivery_unknown'   # a click may have happened
    with pytest.raises(ValueError):
        M.finish(con, first['id'], 'call1', 'confirmed', 6.0)
    M.resolve_unknown(con, first['id'], True, 7.0, 'Konuşmada gördüm')
    assert M.rows(con, '1234567890')[0]['state'] == 'confirmed'


def test_before_the_click_a_failure_puts_the_row_back_and_a_dry_run_is_claimable_when_going_live(con):
    row = M.prepare(con, listing_ref='1234567890', mode='local', fields=['access'], text='a', now=1.0)
    M.claim(con, row['id'], 'c1', 't', 2.0, live=False)
    assert M.finish(con, row['id'], 'c1', 'not_sent', 3.0, reason='form_not_found') == 'prepared'
    M.claim(con, row['id'], 'c2', 't', 4.0, live=False)
    assert M.finish(con, row['id'], 'c2', 'dry_run', 5.0, evidence={'typed_exact': True}) == 'dry_run'
    with pytest.raises(ValueError):
        M.claim(con, row['id'], 'c3', 't', 6.0, live=False)                                  # still a dry run in dry-run mode
    M.claim(con, row['id'], 'c3', 't', 6.0, live=True)                                       # live may send the checked draft
    assert M.count(con, 'sending') == 1


def test_after_a_dry_run_the_same_message_is_still_to_be_sent_once_live_and_not_repeated_while_in_dry_run_mode():
    first = plan_for()
    dry_row = {'state': 'dry_run', 'fingerprint': first['fingerprint'], 'fields': first['fields'], 'asked': first['fields'], 'text': first['text'], 'updated_at': 5.0, 'sent_at': None, 'url': None, 'prepared_at': 1.0}
    cur = current()
    built = build({'thread': None, 'checks': {}, 'tasks': [], 'incidents': [], 'flags': [], 'outbox': [dry_row]}, ['natural_sit', 'archaeological_sit'], cur['next_action'])
    assert built['fields']['natural_sit']['state'] == 'dry_run'
    again = M.plan(listing_id='1344169039', current=cur, built=built, convo=None, outbox=[dry_row], mode='dry_run', now=NOON)
    assert again['reason'] == 'dry_run_done'
    live = M.plan(listing_id='1344169039', current=cur, built=built, convo=None, outbox=[dry_row], mode='live', now=NOON)
    assert live['ok'] and live['fingerprint'] == first['fingerprint']


def test_a_seller_who_takes_no_messages_is_not_asked_again():
    closed = {'state': 'cancelled', 'reason': 'seller_messages_disabled'}
    assert plan_for(outbox=[closed])['reason'] == 'seller_closed'
    assert plan_for(outbox=[{'state': 'cancelled', 'reason': 'listing_not_live'}])['reason'] == 'seller_closed'
    assert plan_for(outbox=[{'state': 'cancelled', 'reason': 'user_cancelled', 'fingerprint': 'x', 'fields': []}]).get('reason') != 'seller_closed'


def test_a_parcel_enclosed_by_numbered_neighbours_is_asked_about_an_easement_not_about_road_frontage():
    plain = M.render('1344169039', ['access'])
    enclosed = M.render('1344169039', ['access'], enclosed=True)
    assert 'doğrudan kadastral yol cephesi var mı' in plain and 'doğrudan kadastral yol cephesi var mı' not in enclosed
    assert enclosed == 'Merhaba, 1344169039 numaralı taşınmaz için parselin kadastral yol cephesi görünmüyor; yola geçiş hakkınız (irtifak, mahkeme yolu vb.) var mı? Teşekkürler.'
    assert M.check_text(enclosed, '1344169039') == enclosed
    assert M.render('1344169039', ['natural_sit'], enclosed=True) == M.render('1344169039', ['natural_sit'])


def test_a_send_that_found_our_message_last_waits_a_day_even_if_the_thread_read_cannot_confirm_it():
    row = {'state': 'prepared', 'reason': 'waiting_for_reply', 'updated_at': NOON - 600, 'fingerprint': 'x', 'fields': []}
    assert plan_for(outbox=[row], mode='live')['reason'] == 'waiting_for_reply'
    old = {**row, 'updated_at': NOON - 2 * 86400}
    assert plan_for(outbox=[old], mode='live').get('reason') != 'waiting_for_reply'                  # a day later it may try again
    replied = {'found': True, 'messages': [{'who': 'sen', 'text': 'x'}, {'who': 'satıcı', 'text': 'y'}]}
    assert plan_for(outbox=[row], convo=replied, mode='live').get('reason') != 'waiting_for_reply'   # the seller wrote since: the normal gates decide


def test_the_history_check_sees_a_question_asked_before_and_a_topic_the_seller_raised_unasked():
    # one message asked parcel, road and sit; the seller answered all three in one line
    asked = [{'who': 'sen', 'text': 'Merhaba, ada/parsel bilgisini paylaşabilir misiniz? Parselin doğrudan kadastral yol cephesi var mı?'},
             {'who': 'satıcı', 'text': 'parsel 51, yola 20 metre cephesi var, sit durumu yok'}]
    assert M.overlap(['access'], asked) == {'asked': ['access'], 'seller_wrote': ['access']}
    volunteered = [{'who': 'sen', 'text': 'Merhaba, ada/parsel bilgisini paylaşabilir misiniz?'}, {'who': 'satıcı', 'text': 'Parsel 88, kadastral yola cepheli, sit yok'}]
    assert M.overlap(['access', 'natural_sit'], volunteered) == {'asked': [], 'seller_wrote': ['access', 'natural_sit']}
    # a new topic the conversation never touched may be asked (parcel answered, then road and sit)
    answered_parcel = [{'who': 'sen', 'text': 'Merhaba, ada/parsel bilgisini paylaşabilir misiniz?'}, {'who': 'satıcı', 'text': 'Merhaba, ada yok parsel 88'}]
    assert M.overlap(['access', 'natural_sit', 'archaeological_sit'], answered_parcel) == {'asked': [], 'seller_wrote': []}
