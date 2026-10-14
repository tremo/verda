"""The browser side of a seller message, in the user's visible Chrome.

Order, and why: read the live listing (same listing, still live, the right account, text unchanged since the facts were read) ->
open Sahibinden's own new-message form -> type the approved text and read it back -> dry run: clear it and stop; live: record the
click intent, click Send once, read the conversation to see the exact text. All decisions are made here in code; the browser is
only asked to look and to do the few fixed things below. Nothing a page says is followed as an instruction.
"""
import random
import re
import time
from urllib.parse import urlparse

from verda.agency import message as M
from verda.agency.access import account_matches, classify_url

FORM_URL = 'https://banaozel.sahibinden.com/mesajlarim/detay/{id}/yeni'
EDITOR = 'textarea[placeholder="Mesajınızı yazınız"]'
SEND = 'button.send-button[title="Gönder"]'
KVKK = 'KVKK Aydınlatma Metni Güncellemesi'
WAITS = {'listing': (4, 8), 'form': (3, 6), 'after_send': (3, 6), 'pace': (0.5, 2)}


class NotSent(Exception):
    """A safe stop: nothing was clicked. `reason` is a short code."""
    def __init__(self, reason, **details):
        super().__init__(reason)
        self.reason, self.details = reason, details


class Stopped(Exception):
    """The site raised a security control or the session is gone: stop everything (code from the access signals)."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---- what the real page is asked ------------------------------------------------------------------------------------
FORM_JS = """(() => ({url: location.href, title: document.title, text: (document.body ? document.body.innerText : '').slice(0, 20000),
  head: (document.body ? document.body.innerText : '').slice(0, 3000), editor: !!document.querySelector('%s')}))()""" % EDITOR
EDITOR_JS = """(() => { const e=document.querySelector('%s')||document.querySelector('textarea');
  if(!e) return {found:false,value:'',is_filled:false};
  return {found:true,value:e.value,is_filled:e.classList.contains('is-filled')}; })()""" % EDITOR
SEND_JS = """(() => { const b=document.querySelector('%s'); if(!b) return {found:false,disabled:true,name:''};
  return {found:true,disabled:Boolean(b.disabled),name:b.getAttribute('title')||''}; })()""" % SEND
POINT_JS = """(selector => { const e=document.querySelector(selector); if(!e) return null;
  e.scrollIntoView({block:'center'}); const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
  if(!r.width||!r.height||!e.contains(document.elementFromPoint(x,y))) return null; return {x,y}; })"""
MODAL_CLOSE_JS = """(() => { const b=[...document.querySelectorAll('button')].find(e=>/^kapat$/i.test((e.innerText||e.getAttribute('aria-label')||'').trim()) && e.getClientRects().length);
  if(!b) return null; const r=b.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()"""


class CdpPage:
    """The few fixed actions, over the Jev browser's CDP session. Selectors are code-owned; none comes from a model or a page."""

    def __init__(self, browser, read_state, thread_read):
        self.browser, self.read_state, self.thread_read = browser, read_state, thread_read

    def _wait_ready(self, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.browser.evaluate('document.readyState') == 'complete':
                return
            time.sleep(0.2)

    def listing_state(self):
        deadline = time.monotonic() + 20
        raw = self.browser.evaluate(self.read_state)
        while not (raw.get('detail') or raw.get('removed') or raw.get('blocker')) and time.monotonic() < deadline:
            time.sleep(0.5)
            raw = self.browser.evaluate(self.read_state)
        raw['head'] = self.browser.evaluate("(document.body ? document.body.innerText : '').slice(0, 3000)")
        return raw

    def goto(self, url):
        self.browser.call('Page.navigate', url=url)
        time.sleep(0.5)
        self._wait_ready()

    def form_state(self):
        deadline = time.monotonic() + 20
        state = self.browser.evaluate(FORM_JS)
        while not state['editor'] and not M.unavailable_reason(state['text']) and time.monotonic() < deadline:
            time.sleep(0.5)
            state = self.browser.evaluate(FORM_JS)
        state['blocker'] = (self.browser.evaluate(self.read_state) or {}).get('blocker')
        return state

    def editor_state(self):
        return self.browser.evaluate(EDITOR_JS)

    def send_state(self):
        return self.browser.evaluate(SEND_JS)

    def _click_point(self, point):
        for kind in ('mousePressed', 'mouseReleased'):
            self.browser.call('Input.dispatchMouseEvent', type=kind, x=point['x'], y=point['y'], button='left', clickCount=1)

    def click(self, selector):
        import json
        point = self.browser.evaluate(POINT_JS + '(' + json.dumps(selector) + ')')
        if point is None:
            raise NotSent('target_not_clickable', selector=selector)
        self._click_point(point)

    def close_known_modal(self):
        point = self.browser.evaluate(MODAL_CLOSE_JS)
        if point is None:
            return False
        self._click_point(point)
        time.sleep(0.6)
        return True

    def _select_all(self):
        import sys
        mod = 4 if sys.platform == 'darwin' else 2
        self.browser.call('Input.dispatchKeyEvent', type='keyDown', key='a', code='KeyA', modifiers=mod, commands=['selectAll'])
        self.browser.call('Input.dispatchKeyEvent', type='keyUp', key='a', code='KeyA', modifiers=mod)

    def clear_editor(self):
        self.click(EDITOR)
        self._select_all()
        self.browser.call('Input.dispatchKeyEvent', type='keyDown', key='Backspace', code='Backspace', windowsVirtualKeyCode=8)
        self.browser.call('Input.dispatchKeyEvent', type='keyUp', key='Backspace', code='Backspace', windowsVirtualKeyCode=8)
        time.sleep(0.4)

    def insert(self, text):
        self.click(EDITOR)
        self._select_all()
        self.browser.call('Input.insertText', text=text)
        time.sleep(0.4)

    def thread_messages(self):
        raw = self.browser.evaluate(self.thread_read)
        return [{'who': 'sen' if m.get('role') == 'user' else 'satıcı', 'text': m.get('text') or ''} for m in raw.get('messages', [])]

    def url(self):
        return self.browser.evaluate('location.href')

    def close(self):
        try:
            self.browser.close()
        except Exception:
            pass


# ---- the decisions -------------------------------------------------------------------------------------------------
def page_path(url):
    return urlparse(str(url or '')).path


def is_form_path(url, listing_id):
    return page_path(url) in (f'/mesajlarim/detay/{listing_id}/yeni', f'/mesajlarim/{listing_id}/yeni')


def is_conversation_path(url, listing_id):
    return bool(re.fullmatch(rf'/mesajlarim/{re.escape(listing_id)}/[^/]+/[^/]+', page_path(url)))


def head_name(text):
    from verda.agency.login import shown_name
    return shown_name(text)


class MessageSender:
    """Runs one outbox row to its end. `open_page(listing_url)` gives a page adapter (CdpPage or a test double)."""

    def __init__(self, open_page, sleep=time.sleep, waits=None, now=time.time):
        self.open_page, self.sleep, self.waits, self.now = open_page, sleep, {**WAITS, **(waits or {})}, now

    def _wait(self, kind):
        self.sleep(random.uniform(*self.waits[kind]))

    def _account(self, head, label):
        shown = head_name(head)
        if shown is None:
            raise NotSent('account_unverified', page=label)
        if not account_matches(shown):
            raise NotSent('account_mismatch', page=label)

    def run(self, job, progress, checkpoint):
        """Returns the receipt dict. Raises Stopped for a security control or a lost session (the caller records it)."""
        args = job['arguments']
        listing_id, message_text, dry = args['listing_id'], args['text'], bool(args['dry_run'])
        page = self.open_page(args['listing_url'])
        evidence = {'dry_run': dry, 'phone_banner': 'none'}
        intent = False
        try:
            progress('connected')
            self._wait('listing')
            live = page.listing_state()
            if live.get('blocker') or classify_url(live.get('url')):
                raise Stopped(live.get('blocker') or classify_url(live.get('url')))
            if live.get('removed') and not live.get('detail'):
                return self._receipt(args, 'cancelled', 'listing_not_live', evidence)
            detail = live.get('detail') or {}
            if detail.get('listing_id') != listing_id or listing_id not in page_path(live.get('url')):
                raise NotSent('unexpected_listing_page')
            self._account(live.get('head'), 'listing')
            if args.get('listing_digest') and live_digest(detail) != args['listing_digest']:
                raise NotSent('listing_changed')
            evidence['listing_live'] = True
            progress('observed')

            self._wait('pace')
            page.goto(FORM_URL.format(id=listing_id))
            self._wait('form')
            form = page.form_state()
            if form.get('blocker') or classify_url(form.get('url')):
                raise Stopped(form.get('blocker') or classify_url(form.get('url')))
            if not (is_form_path(form['url'], listing_id) or is_conversation_path(form['url'], listing_id)):
                raise NotSent('unexpected_message_page')
            self._account(form.get('head'), 'form')
            if KVKK in form['text']:
                if not page.close_known_modal():
                    raise NotSent('known_modal_close_missing')
                form = page.form_state()
                if KVKK in form['text']:
                    raise NotSent('known_modal_dismiss_failed')
            gone = M.unavailable_reason(form['text'])
            if gone:
                return self._receipt(args, 'cancelled', gone, evidence)
            banner = M.phone_banner(form['text'])
            evidence['phone_banner'] = banner
            if banner == 'other':
                raise NotSent('phone_number_disclosure_warning')
            if is_conversation_path(form['url'], listing_id):
                earlier = page.thread_messages()
                evidence['earlier_messages'] = len(earlier)
                if any(m['who'] == 'sen' and M.exact_in(message_text, m['text']) for m in earlier):
                    return self._receipt(args, 'confirmed', 'identical_message_already_in_thread', evidence, thread_url=form['url'])
                if earlier and earlier[-1]['who'] == 'sen':
                    raise NotSent('waiting_for_reply')
                covered = M.overlap(args.get('fields') or [], earlier)
                if covered['asked'] or covered['seller_wrote']:
                    evidence['covered'] = covered
                    raise NotSent('asked_before_in_thread')    # the live page itself, the third check: what is on screen beats every record
            if not form['editor']:
                raise NotSent('message_textbox_missing')
            progress('verified')

            page.insert(message_text)
            typed = page.editor_state()
            if not typed.get('found') or typed.get('value') != message_text:
                self._tidy(page)
                raise NotSent('message_text_mismatch')
            evidence['typed_exact'] = True
            button = page.send_state()
            if not button.get('found') or button.get('disabled') or M.normalize(button.get('name')) != 'Gönder':
                self._tidy(page)
                raise NotSent('enabled_send_button_missing')
            evidence['send_enabled'] = True
            progress('filled')

            if dry:
                page.clear_editor()
                cleared = page.editor_state()
                evidence['draft_cleared'] = cleared.get('found') and cleared.get('value') == ''
                if not evidence['draft_cleared']:
                    raise NotSent('draft_clear_failed')
                return self._receipt(args, 'dry_run', 'typed_and_verified_not_sent', evidence)

            self._wait('pace')
            again = page.editor_state()
            if again.get('value') != message_text:
                raise NotSent('message_text_changed')
            if not M.within_window(self.now()):
                self._tidy(page)
                raise NotSent('outside_window')
            granted = checkpoint(args['text_hash'], page.url())
            if granted is not True:
                self._tidy(page)
                raise NotSent(granted if isinstance(granted, str) else 'checkpoint_refused')
            intent = True                                   # from here on the click may have happened
            page.click(SEND)
            progress('clicked')
            self._wait('after_send')
            after = page.form_state()
            if after.get('blocker') or classify_url(after.get('url')):
                raise Stopped(after.get('blocker') or classify_url(after.get('url')))
            if 'ilan sahibi ilanıyla ilgili mesaj almamayı tercih ediyor' in M.tr_lower(after['text']):
                return self._receipt(args, 'cancelled', 'seller_messages_disabled', evidence)
            url_ok = is_conversation_path(after['url'], listing_id)
            seen = page.thread_messages() if url_ok else []
            text_ok = any(m['who'] == 'sen' and M.exact_in(message_text, m['text']) for m in seen[-3:])
            evidence.update(conversation_url=url_ok, exact_text_in_thread=text_ok)
            if url_ok and text_ok:
                return self._receipt(args, 'confirmed', 'delivery_verified', evidence, thread_url=after['url'])
            return self._receipt(args, 'delivery_unknown', 'delivery_not_fully_verified', evidence, thread_url=after['url'] if url_ok else None)
        except NotSent as stop:
            if intent:
                return self._receipt(args, 'delivery_unknown', 'stopped_after_click:' + stop.reason, evidence)
            return self._receipt(args, 'not_sent', stop.reason, evidence)
        finally:
            page.close()

    @staticmethod
    def _tidy(page):
        """After a failed typing step: leave no half-written draft in the form."""
        try:
            page.clear_editor()
        except Exception:
            pass

    @staticmethod
    def _receipt(args, status, reason, evidence, thread_url=None):
        return {'outbox_id': args['outbox_id'], 'listing_id': args['listing_id'], 'status': status, 'reason': reason[:80],
                'thread_url': thread_url, 'evidence': {k: v for k, v in evidence.items()}}


def live_digest(detail):
    """What the listing said when the facts were read: title, price, area and text. Same function at both ends."""
    from verda.legacy import digest
    num = lambda v: None if v is None else round(float(v), 2)             # 3000000 and 3000000.0 are the same price
    return digest({'title': M.normalize(detail.get('title')), 'price': num(detail.get('price_tl')), 'area': num(detail.get('area_m2')),
                   'description': M.normalize(detail.get('description'))})[:32]
