import pytest

from verda.agency import message as M
from verda.agency.send import MessageSender, Stopped, live_digest

ID = '1344169039'
TEXT = M.render(ID, ['natural_sit', 'archaeological_sit'])
DETAIL = {'listing_id': ID, 'title': 'Arsa', 'price_tl': 3000000.0, 'area_m2': 1053.0, 'description': 'Yola cepheli.'}
HEAD = 'sahibinden.com\nKaan Y.\nÜcretsiz İlan Ver\n'


class FakePage:
    """A scripted Sahibinden: the listing, then the form, then (after Send) the conversation."""

    def __init__(self, listing=None, form=None, after=None, name_head=HEAD, accepts_text=True):
        self.listing = {'url': f'https://www.sahibinden.com/ilan/arsa-{ID}/detay', 'blocker': None, 'removed': False, 'detail': DETAIL, 'head': name_head, **(listing or {})}
        self.form = {'url': f'https://banaozel.sahibinden.com/mesajlarim/detay/{ID}/yeni', 'text': 'Mesajınızı yazınız', 'head': name_head, 'editor': True, 'blocker': None, **(form or {})}
        self.after = after
        self.value, self.log, self.closed, self.accepts, self.on_form = '', [], False, accepts_text, False
        self.sent = False

    def listing_state(self): self.log.append('listing'); return dict(self.listing)
    def goto(self, url): self.log.append('goto:' + url.rsplit('/', 2)[-2]); self.on_form = True
    def form_state(self): return dict(self.after if self.sent and self.after else self.form)
    def editor_state(self): return {'found': True, 'value': self.value, 'is_filled': bool(self.value)}
    def send_state(self): return {'found': True, 'disabled': not self.value, 'name': 'Gönder'}
    def insert(self, text): self.log.append('insert'); self.value = text if self.accepts else text[:10]
    def clear_editor(self): self.log.append('clear'); self.value = ''
    def close_known_modal(self): return False
    def click(self, selector): self.log.append('click'); self.sent = True
    def thread_messages(self): return list((self.after if self.sent and self.after else self.form).get('thread', []))
    def url(self): return self.form['url']
    def close(self): self.closed = True


def job(dry, **kw):
    return {'arguments': {'outbox_id': 'o1', 'listing_id': ID, 'text': TEXT, 'text_hash': 'h', 'dry_run': dry, 'listing_url': f'https://www.sahibinden.com/ilan/arsa-{ID}/detay',
                          'listing_digest': live_digest(DETAIL), **kw}}


def run(page, dry, checkpoint=lambda h, u: True, **kw):
    phases = []
    sender = MessageSender(lambda url: page, sleep=lambda s: None)
    receipt = sender.run(job(dry, **kw), lambda p, d=None: phases.append(p), checkpoint)
    return receipt, phases


def test_the_listing_digest_ignores_how_a_number_is_written():
    assert live_digest({**DETAIL, 'price_tl': 3000000}) == live_digest({**DETAIL, 'price_tl': 3000000.0})
    assert live_digest({**DETAIL, 'price_tl': 2900000}) != live_digest(DETAIL)


def test_a_dry_run_types_reads_back_clears_and_never_clicks():
    page = FakePage()
    receipt, phases = run(page, True)
    assert receipt['status'] == 'dry_run' and receipt['evidence']['typed_exact'] and receipt['evidence']['draft_cleared'] and receipt['evidence']['send_enabled']
    assert 'click' not in page.log and page.value == '' and page.closed
    assert phases == ['connected', 'observed', 'verified', 'filled']


def test_a_live_send_records_the_intent_before_the_click_and_confirms_from_the_conversation():
    order = []
    after = {'url': f'https://banaozel.sahibinden.com/mesajlarim/{ID}/a1/b2', 'text': TEXT, 'head': HEAD, 'editor': True, 'blocker': None, 'thread': [{'who': 'sen', 'text': TEXT}]}
    page = FakePage(after=after)
    original_click = page.click
    page.click = lambda sel: (order.append('click'), original_click(sel))
    sender = MessageSender(lambda url: page, sleep=lambda s: None, now=lambda: M.local.__globals__['datetime'](2026, 10, 8, 12, 0, tzinfo=M.TZ).timestamp())
    receipt = sender.run(job(False), lambda p, d=None: None, lambda h, u: (order.append('intent'), True)[1])
    assert order == ['intent', 'click']                               # the intent is durable before the click, never after
    assert receipt['status'] == 'confirmed' and receipt['thread_url'].endswith('/a1/b2') and receipt['evidence']['exact_text_in_thread']


def test_a_refused_checkpoint_means_no_click_and_a_wrong_conversation_after_the_click_is_unknown():
    noon = lambda: M.local.__globals__['datetime'](2026, 10, 8, 12, 0, tzinfo=M.TZ).timestamp()
    page = FakePage()
    receipt = MessageSender(lambda url: page, sleep=lambda s: None, now=noon).run(job(False), lambda p, d=None: None, lambda h, u: False)
    assert receipt['status'] == 'not_sent' and receipt['reason'] == 'checkpoint_refused' and 'click' not in page.log and page.value == ''
    after = {'url': f'https://banaozel.sahibinden.com/mesajlarim/{ID}/a1/b2', 'text': 'başka', 'head': HEAD, 'editor': True, 'blocker': None, 'thread': [{'who': 'sen', 'text': 'başka bir mesaj'}]}
    page = FakePage(after=after)
    receipt = MessageSender(lambda url: page, sleep=lambda s: None, now=noon).run(job(False), lambda p, d=None: None, lambda h, u: True)
    assert receipt['status'] == 'delivery_unknown'


def test_safe_stops_before_the_click_the_wrong_account_a_changed_listing_a_removed_ad_and_other_phone_text():
    assert run(FakePage(name_head='sahibinden.com\nAli V.\nÜcretsiz İlan Ver\n'), True)[0]['reason'] == 'account_mismatch'
    assert run(FakePage(name_head='Giriş Yap\nHesap Aç'), True)[0]['reason'] == 'account_unverified'
    assert run(FakePage(), True, listing_digest='not-the-same')[0]['reason'] == 'listing_changed'
    gone = run(FakePage(listing={'removed': True, 'detail': None}), True)[0]
    assert gone['status'] == 'cancelled' and gone['reason'] == 'listing_not_live'
    assert run(FakePage(form={'text': 'Telefon numaranız ilan sahibine gösterilecek'}), True)[0]['reason'] == 'phone_number_disclosure_warning'
    ok = run(FakePage(form={'text': 'Mesaj gönderdiğinizde 0 532 000 00 00 numaralı telefonunuz karşı tarafla paylaşılacaktır.'}), True)[0]
    assert ok['status'] == 'dry_run' and ok['evidence']['phone_banner'] == 'standard'
    assert run(FakePage(form={'text': 'Bu ilan yayında değil'}), True)[0]['status'] == 'cancelled'
    assert run(FakePage(form={'editor': False}), True)[0]['reason'] == 'message_textbox_missing'
    assert run(FakePage(form={'url': 'https://banaozel.sahibinden.com/mesajlarim'}), True)[0]['reason'] == 'unexpected_message_page'


def test_a_text_that_did_not_arrive_exactly_is_never_sent_and_leaves_no_draft():
    page = FakePage(accepts_text=False)
    receipt, _ = run(page, False)
    assert receipt['status'] == 'not_sent' and receipt['reason'] == 'message_text_mismatch' and 'click' not in page.log and page.value == ''


def test_security_controls_stop_the_whole_run_and_an_existing_unanswered_message_is_never_repeated():
    with pytest.raises(Stopped) as stop:
        run(FakePage(form={'blocker': 'captcha'}), True)
    assert stop.value.code == 'captcha'
    with pytest.raises(Stopped) as stop:
        run(FakePage(listing={'url': 'https://www.sahibinden.com/cs/hloading'}), True)
    assert stop.value.code == 'unusual_access'
    convo = {'url': f'https://banaozel.sahibinden.com/mesajlarim/{ID}/a1/b2', 'thread': [{'who': 'sen', 'text': 'Merhaba başka bir soru'}]}
    assert run(FakePage(form=convo), True)[0]['reason'] == 'waiting_for_reply'
    same = {**convo, 'thread': [{'who': 'sen', 'text': TEXT}]}
    page = FakePage(form=same, after=same)
    receipt, _ = run(page, False)
    assert receipt['status'] == 'confirmed' and receipt['reason'] == 'identical_message_already_in_thread' and 'click' not in page.log


def test_the_live_conversation_on_screen_stops_a_question_it_already_covers():
    # what the form showed on 14 Oct 2026: our old question and the seller's answer, the road among them
    convo = {'url': f'https://banaozel.sahibinden.com/mesajlarim/{ID}/a1/b2', 'thread': [
        {'who': 'sen', 'text': 'Merhaba, ada/parsel bilgisini paylaşabilir misiniz? Parselin doğrudan kadastral yol cephesi var mı?'},
        {'who': 'satıcı', 'text': 'parsel 51, yola 20 metre cephesi var, sit durumu yok'}]}
    page = FakePage(form=convo)
    receipt, _ = run(page, False, text=M.render(ID, ['access']), fields=['access'])
    assert receipt['status'] == 'not_sent' and receipt['reason'] == 'asked_before_in_thread' and 'click' not in page.log and 'insert' not in page.log
    assert receipt['evidence']['covered'] == {'asked': ['access'], 'seller_wrote': ['access']}
    assert run(FakePage(form=convo), True, fields=['archaeological_sit'])[0]['reason'] == 'asked_before_in_thread'    # "sit durumu yok" covers both kinds
    # a conversation that only settled the parcel does not stop the road question
    parcel_only = {**convo, 'thread': [{'who': 'sen', 'text': 'Merhaba, ada/parsel bilgisini paylaşabilir misiniz?'}, {'who': 'satıcı', 'text': 'Merhaba, ada yok parsel 88'}]}
    receipt, _ = run(FakePage(form=parcel_only), True, fields=['access'])
    assert receipt['status'] == 'dry_run'


def test_a_checkpoint_refusal_code_is_the_receipt_reason_and_nothing_is_clicked():
    noon = lambda: M.local.__globals__['datetime'](2026, 10, 8, 12, 0, tzinfo=M.TZ).timestamp()
    page = FakePage()
    receipt = MessageSender(lambda url: page, sleep=lambda s: None, now=noon).run(job(False), lambda p, d=None: None, lambda h, u: 'asked_before')
    assert receipt['status'] == 'not_sent' and receipt['reason'] == 'asked_before' and 'click' not in page.log and page.value == ''
