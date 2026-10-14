import json
import os

import pytest
from fastapi.testclient import TestClient

from verda.agency.browser import BrowserBridge
from verda.agency.jev import JevReader, JevStopped, load_env, run_worker, verify_receipt, same_target
from verda.agency.store import AgencyStore
from verda.api import create_app
from test_management import setup, search, receipt


def raw_receipt():
    value = receipt()
    return {'url':value['source_url'],'blocker':None,'listings':value['listings'],
            'total_reported':value['total_reported'],'detail':None}


def job():
    return {'tool':'sahibinden.search','arguments':{'query':'Datça'}}


def test_driver_routing_survives_restart_and_does_not_steal_old_jobs(tmp_path):
    store, registry = setup(tmp_path)
    bridge = BrowserBridge(store)
    bridge.select('jev')
    task, engine = search(store,registry)
    bridge.attach('codex-test',now=3)
    assert bridge.claim('codex-test',now=4) is None
    with pytest.raises(ValueError,match='Bekleyen'):
        bridge.select('codex')
    reopened = AgencyStore(store.path,store.config);reopened.initialize()
    bridge = BrowserBridge(reopened)
    assert bridge.overview(now=4)['selected_driver'] == 'jev'
    assert not bridge.overview(now=4)['active']
    bridge.attach('jev-test',driver='jev',now=3)
    owned = bridge.claim('jev-test',now=4)
    bridge.progress(owned,'jev-test','observed',now=5)
    with pytest.raises(ValueError):bridge.progress(owned,'other','observed',now=5)
    bridge.finish(owned['id'],'jev-test',owned['token'],result=receipt(),now=6)
    assert engine.step(now=7)
    view=store.overview()
    assert next(t for t in view['tasks'] if t['id']==task)['state']=='complete'
    observation=store.record_detail('observation',view['records']['observations'][0]['id'])
    assert observation['payload']['browser_driver']=='jev_ultrafast'


def test_v6_upgrade_preserves_browser_jobs(tmp_path):
    store,registry=setup(tmp_path);search(store,registry)
    with store.transaction() as con:
        con.execute('ALTER TABLE browser_jobs DROP COLUMN driver')
        con.execute('ALTER TABLE browser_sessions DROP COLUMN driver')
        con.execute('DROP TABLE browser_config')
        con.execute('UPDATE agency_meta SET version=6')
    store.initialize()
    assert BrowserBridge(store).overview()['jobs'][0]['driver']=='codex'


def test_worker_setup_failure_does_not_claim_or_attach(tmp_path):
    store,registry=setup(tmp_path);BrowserBridge(store).select('jev');search(store,registry)
    class Missing:
        def ready(self):return 'typesafe_key_missing'
    result=run_worker(store,reader=Missing())
    assert result['state']=='waiting_setup'
    status=BrowserBridge(store).overview()
    assert not status['active'] and status['jobs'][0]['state']=='queued'
    assert status['jev']['last_check']['reason']=='typesafe_key_missing'


def test_worker_receipt_reaches_common_records(tmp_path):
    store,registry=setup(tmp_path);bridge=BrowserBridge(store);bridge.select('jev')
    # Real-time task clocks are needed by the standalone worker.
    task=store.submit(agent='sahibinden',objective='Fixture read',inputs={'query':'Datça'},request_key='jev-test',executor_tool='sahibinden.search')
    from verda.agency.engine import AgentEngine
    engine=AgentEngine(store,registry,providers={});assert engine.step()
    class Reader:
        def ready(self):return None
        def read(self,url,job,report):
            assert url.startswith('https://www.sahibinden.com/satilik-arsa/mugla-datca?')
            report('observed');report('DONE');report('verified');return receipt()
    assert run_worker(store,reader=Reader())['completed']==1
    assert engine.step()
    view=store.overview()
    assert next(t for t in view['tasks'] if t['id']==task)['state']=='complete'
    assert not bridge.overview()['active']
    assert any(e['kind']=='browser_progress' for e in view['events'])


class Browser:
    def __init__(self,url,raw=None):
        self.url=url;self.raw=raw or raw_receipt();self.closed=False;self.acts=[]
    def observe(self,**kwargs):
        return {'url':self.url,'title':'Fixture','text':'One land listing','actions':[
            {'id':'send','kind':'click','label':'Send message'},
            {'id':'wait','kind':'wait','label':'Wait'},
            {'id':'scroll_down','kind':'scroll','label':'Scroll','delta':560}]}
    def evaluate(self,script):return self.raw
    def fresh(self,page):return True
    def act(self,action,page):self.acts.append(action)
    def close(self):self.closed=True


def test_jev_done_independently_verified_and_only_read_actions_offered():
    pytest.importorskip('jev_ultrafast')
    b=Browser(receipt()['source_url']);calls=[];progress=[]
    def choose(page,goal,history):
        calls.append(page)
        assert {a['kind'] for a in page['actions']}=={'scroll','wait'}
        return {'choice':'DONE'}
    result=JevReader(lambda url:b,choose).read(b.url,job(),progress.append)
    assert result['listings'][0]['listing_id']=='9000000001'
    # The page already verifies statically, so the navigation model is never consulted.
    assert not calls and progress[-1]=='verified' and b.closed and not b.acts


def test_the_navigation_model_is_asked_only_when_the_page_is_not_ready_and_may_only_scroll_or_wait():
    pytest.importorskip('jev_ultrafast')
    empty={**raw_receipt(),'listings':[],'total_reported':30}
    class Lazy(Browser):
        def __init__(self,url):super().__init__(url,empty)
        def act(self,action,page):
            super().act(action,page);self.raw=raw_receipt()      # scrolling loads the results
    b=Lazy(receipt()['source_url']);offered=[]
    def choose(page,goal,history):
        offered.append({a['kind'] for a in page['actions']});return {'choice':'scroll_down' if len(offered)==1 else 'DONE'}
    result=JevReader(lambda url:b,choose,sleep=lambda s:None).read(b.url,job(),lambda *a:None)
    assert result['listings'] and offered[0]=={'scroll','wait'} and len(b.acts)==1


@pytest.mark.parametrize('raw,choice,code',[
    ({**raw_receipt(),'blocker':'captcha'},'DONE','captcha'),
    ({**raw_receipt(),'total_reported':30,'listings':[]},'DONE',None),
    ({**raw_receipt(),'total_reported':30,'listings':[]},'send','read_failed'),
    ({**raw_receipt(),'url':'https://www.sahibinden.com/cs/checkLoading?returnUrl=https%3A%2F%2Fwww.sahibinden.com%2Filan%2Fx-1%2Fdetay'},'DONE','unusual_access'),
    ({**raw_receipt(),'blocker':'unusual_access'},'DONE','unusual_access'),
])
def test_jev_blocks_challenges_false_done_and_out_of_scope_actions(raw,choice,code):
    pytest.importorskip('jev_ultrafast')
    b=Browser(receipt()['source_url'],raw);calls=[]
    def choose(*args):calls.append(1);return {'choice':choice}
    with pytest.raises((JevStopped,ValueError)) as error:
        JevReader(lambda url:b,choose).read(b.url,job(),lambda phase,detail=None:None)
    if code:assert str(error.value)==code
    if code in {'captcha','unusual_access'}:assert not calls     # a security control never reaches the model
    assert b.closed and not b.acts


def test_receipt_rejects_wrong_filters_duplicate_rows_or_wrong_listing():
    raw=raw_receipt()
    for modified in [{**raw,'url':raw['url'].replace('15000000','999999')},
                     {**raw,'listings':raw['listings']*2,'total_reported':2},
                     {**raw,'url':'https://example.com/'}]:
        with pytest.raises(ValueError):verify_receipt(job(),modified)
    with pytest.raises(ValueError):
        verify_receipt({'tool':'sahibinden.read_listing','arguments':{'listing_id':'9999999999'}},
                       {**raw,'url':raw['listings'][0]['url'],'detail':raw['listings'][0]})


def test_target_accepts_filter_reordering_but_not_another_page():
    expected=receipt()['source_url']
    assert same_target('https://www.sahibinden.com/satilik-arsa/mugla-datca?a507_min=1000.0&price_max=15000000',expected)
    assert not same_target(expected+'&pagingOffset=20',expected)
    assert not same_target(expected.replace('15000000','999'),expected)
    assert not same_target(expected.replace('mugla-datca','mugla-bodrum'),expected)


def test_env_file_only_reads_expected_settings_without_shell_execution(tmp_path,monkeypatch):
    monkeypatch.delenv('TYPESAFE_API_KEY',raising=False)
    path=tmp_path/'credentials.env'
    path.write_text('TYPESAFE_API_KEY="fixture-private-key"\nBU_CDP_URL=https://evil.example\nIGNORED=$(touch surprise)\n')
    previous=os.environ.get('BU_CDP_URL')
    load_env(path)
    assert os.environ['TYPESAFE_API_KEY']=='fixture-private-key'
    assert os.environ.get('BU_CDP_URL')==previous
    assert not (tmp_path/'surprise').exists()


def test_browser_selection_requires_existing_editor_auth(engine,tmp_path):
    store,_=setup(tmp_path)
    with TestClient(create_app(engine,'long-local-api-token-with-32-characters',viewer_nonce='open',agency_store=store)) as client:
        path='/control/private/browser'
        assert client.put(path,json={'driver':'jev'}).status_code==401
        client.get('/control/unlock/open')
        data=client.get('/control/private/agency').json()
        assert client.put(path,json={'driver':'jev'}).status_code==403
        headers={'Origin':'http://testserver','X-Verda-CSRF':data['csrf_token']}
        assert client.put(path,json={'driver':'jev','command':'evil'},headers=headers).status_code==422
        result=client.put(path,json={'driver':'jev'},headers=headers)
        assert result.status_code==200 and result.json()['selected_driver']=='jev'


def test_a_security_check_page_is_named_before_anything_reads_it():
    pytest.importorskip('jev_ultrafast')
    check='https://www.sahibinden.com/cs/checkLoading?returnUrl=https%3A%2F%2Fwww.sahibinden.com%2Filan%2Fx-9000000001%2Fdetay'
    b=Browser(check);calls=[];progress=[]
    with pytest.raises(JevStopped) as error:
        JevReader(lambda url:b,lambda *a:calls.append(1)).read(b.url,job(),progress.append)
    assert error.value.code=='unusual_access' and not calls and b.closed and not b.acts
    assert 'observed' not in progress                         # it never counted as an observed target page


def test_a_security_check_halts_the_shared_source_and_the_next_job_is_not_claimed(tmp_path):
    store,registry=setup(tmp_path);bridge=BrowserBridge(store);bridge.select('jev')
    task,engine=search(store,registry);bridge.attach('jev-test',driver='jev',now=3)
    owned=bridge.claim('jev-test',now=4)
    bridge.finish(owned['id'],'jev-test',owned['token'],error='unusual_access',now=5)
    view=store.overview()
    assert view['incidents'][0]['reason']=='unusual_access' and view['incidents'][0]['state']=='open'
    assert next(r for r in view['resources'] if r['key']=='sahibinden')['halted']=='unusual_access'
    store.submit(agent='sahibinden',objective='Next read',inputs={'query':'Datça'},request_key='next',executor_tool='sahibinden.search',now=6)
    assert engine.step(now=7) is True                          # the task is deferred, never sent to a browser
    bridge.attach('jev-test',driver='jev',now=8)
    assert bridge.claim('jev-test',now=9) is None


def test_classification_by_address():
    from verda.agency.access import classify_url
    assert classify_url('https://www.sahibinden.com/cs/hloading') == 'unusual_access'
    assert classify_url('https://www.sahibinden.com/cs/tLoading?x=1') == 'unusual_access'
    assert classify_url('https://secure.sahibinden.com/login') == 'session_lost'
    assert classify_url('https://www.sahibinden.com/ilan/emlak-arsa-satilik-x-9000000001/detay') is None
    assert classify_url(None) is None


def test_a_person_may_pass_a_security_check_while_the_reader_only_waits():
    pytest.importorskip('jev_ultrafast')
    check='https://www.sahibinden.com/cs/checkLoading?returnUrl=x'
    class Passing(Browser):
        def __init__(self):
            super().__init__(check);self.hrefs=iter([check,check,receipt()['source_url']])
        def observe(self,**kw):
            return {**super().observe(),'url':self.url}
        def evaluate(self,script):
            if script=='location.href':
                self.url=next(self.hrefs);return self.url
            return {**self.raw,'url':self.url}
    b=Passing();progress=[];slept=[]
    reader=JevReader(lambda url:b,lambda *a:{'choice':'DONE'},sleep=slept.append,patience=30)
    b.url=check
    result=reader.read(receipt()['source_url'],job(),progress.append)
    assert result['listings'] and progress.count('WAIT')==1 and not b.acts    # it never touched the page


def test_a_check_that_nobody_passes_stops_and_leaves_the_tab_for_the_user():
    pytest.importorskip('jev_ultrafast')
    check='https://www.sahibinden.com/cs/checkLoading?returnUrl=x'
    class Stuck(Browser):
        def evaluate(self,script):return check if script=='location.href' else {**self.raw,'url':check}
    b=Stuck(check);slept=[]
    with pytest.raises(JevStopped) as error:
        JevReader(lambda url:b,lambda *a:None,sleep=slept.append,patience=9,keep_open=True).read(check,job(),lambda p:None)
    assert error.value.code=='unusual_access' and len(slept)==3 and not b.closed and not b.acts


def test_the_account_is_matched_by_name_with_initials_and_the_login_state_is_read_from_the_header():
    from verda.agency.access import account_matches, login_state
    for shown in ('Deniz Kaan Yıldırım', 'DENİZ KAAN Y.', 'Deniz Kaan Yıldırım ', 'Kaan Y.'):   # the site drops the first name
        assert account_matches(shown)
    for shown in ('Ali Y.', 'Kaan A.', 'Deniz Ali Y.', 'Deniz Kaan Yılmaz', '', 'Deniz Kaan Yıldırım Jr Extra Name'):
        assert not account_matches(shown)
    assert login_state('sahibinden.com anasayfasına dön\n\nDetaylı Arama\nGiriş Yap\nHesap Aç\nÜcretsiz* İlan Ver') == 'logged_out'
    assert login_state('Detaylı Arama\nDeniz Kaan Y.\nMesajlarım') == 'logged_in_or_unknown' and login_state('') == 'unknown'


def test_the_worker_opens_chrome_first_and_waits_for_the_users_approval_without_claiming_anything(tmp_path):
    store,registry=setup(tmp_path);BrowserBridge(store).select('jev');search(store,registry)
    asked=[]
    class Reader(JevReader):
        def ready(self):
            asked.append(self.connect());return self.connect() or None
    reader=Reader(connect=lambda:'chrome_connection_missing')
    result=run_worker(store,reader=reader)
    assert result['state']=='waiting_setup' and result['reason']=='chrome_connection_missing' and asked
    assert BrowserBridge(store).overview()['jobs'][0]['state']=='queued'              # nothing was claimed


def test_the_bridge_reports_the_sahibinden_session_from_the_last_page_it_read(tmp_path):
    store,registry=setup(tmp_path)
    assert BrowserBridge(store).overview()['sahibinden_session']['state']=='unknown'
    assert BrowserBridge(store).overview()['sahibinden_session']['expected_account']=='Deniz Kaan Yıldırım'


def test_chrome_is_attached_and_the_debugging_prompt_is_approved_only_while_the_worker_waits():
    import threading
    from verda.agency.jev import connect_chrome
    approved = threading.Event()
    def attach(wait, name):
        assert approved.wait(5) and name == 'verda'          # the harness blocks until the sheet is answered
    def approve():
        approved.set();return 'ready', None
    assert connect_chrome(wait=5, attach=attach, approve=approve) is None


def test_missing_macos_permission_or_a_switched_off_chrome_is_reported_not_worked_around():
    import threading
    from verda.agency.jev import connect_chrome
    never = threading.Event()
    blocked = lambda wait, name: never.wait(5)
    assert connect_chrome(wait=5, attach=blocked, approve=lambda: ('accessibility-required', 'x')) == 'accessibility_required'
    assert connect_chrome(wait=5, attach=blocked, approve=lambda: ('setup-required', 'x')) == 'chrome_remote_debugging_switch_off'
    def failing(wait, name):
        raise RuntimeError('chrome not reachable')
    assert connect_chrome(wait=5, attach=failing, approve=lambda: ('not-found', None)) == 'chrome_connection_missing'


def test_the_navigator_only_offers_allowed_clicks_and_succeeds_on_the_address_not_on_the_model():
    from verda.agency.jev import JevNavigator, JevStopped
    class Browser:
        def __init__(self):
            self.url, self.clicked, self.prepared = 'https://banaozel.sahibinden.com/', [], 0
        def evaluate(self, js): self.prepared += 1
        def observe(self, screenshot=False):
            return {'url': self.url, 'actions': [
                {'id': 'e1', 'kind': 'click', 'label': 'Mesajlar'},
                {'id': 'e2', 'kind': 'click', 'label': 'Hesabımı sil'},
                {'id': 'e3', 'kind': 'fill', 'label': 'Mesajlar'},
                {'id': 'e4', 'kind': 'click', 'label': 'Mesajlaşma'}]}
        def act(self, action, page):
            self.clicked.append(action['id'])
            self.url = 'https://banaozel.sahibinden.com/mesajlarim'
    seen = []
    def chooser(page, goal, history):
        seen.append([a['id'] for a in page['actions']])
        return {'choice': 'e1'}
    b = Browser()
    nav = JevNavigator(b, chooser=chooser, sleep=lambda s: None)
    url = nav.go('inbox', reached=lambda u: u.endswith('/mesajlarim'), allow=lambda l: l in {'Mesajlar', 'Mesajlaşma'})
    assert url.endswith('/mesajlarim') and b.clicked == ['e1'] and seen == [['e1', 'e4']] and b.prepared >= 1
    # A model that names something it was never shown clicks nothing.
    b2 = Browser()
    try:
        JevNavigator(b2, chooser=lambda p, g, h: {'choice': 'e2'}, sleep=lambda s: None).go(
            'inbox', reached=lambda u: False, allow=lambda l: l in {'Mesajlar', 'Mesajlaşma'})
        raise AssertionError('should have stopped')
    except JevStopped as stopped:
        assert stopped.code == 'navigation_unavailable' and b2.clicked == []
    # DONE-like claims do not matter: without arrival, after the step budget, it stops.
    b3 = Browser(); b3.act = lambda a, p: b3.clicked.append(a['id'])
    try:
        JevNavigator(b3, chooser=lambda p, g, h: {'choice': 'e1'}, sleep=lambda s: None).go(
            'inbox', reached=lambda u: u.endswith('/mesajlarim'), allow=lambda l: l in {'Mesajlar', 'Mesajlaşma'}, steps=2)
        raise AssertionError('should have stopped')
    except JevStopped as stopped:
        assert stopped.code == 'navigation_unavailable' and b3.clicked == ['e1', 'e1']
    # A security check page is named before any model call.
    b4 = Browser(); b4.url = 'https://www.sahibinden.com/cs/checkLoading?returnUrl=x'
    try:
        JevNavigator(b4, chooser=lambda *a: 1 / 0, sleep=lambda s: None).go('x', reached=lambda u: False, allow=lambda l: True)
        raise AssertionError('should have stopped')
    except JevStopped as stopped:
        assert stopped.code == 'unusual_access'


def test_thread_receipts_bind_a_conversation_to_its_listing_and_never_guess_the_sender():
    import pytest
    from verda.agency.browser import ThreadReceipt, thread_url
    from verda.agency.jev import thread_receipt
    base = 'https://banaozel.sahibinden.com/mesajlarim/1000000001/aa/bb'
    raw = {'url': base, 'messages': [
        {'role': 'user', 'text': 'Merhaba, sit var mı?', 'sent_at_text': '12:37'},
        {'role': 'seller', 'text': 'sit yok', 'sent_at_text': '12:53'},
        {'role': 'seller', 'text': 'sit yok', 'sent_at_text': '12:53'},      # same text, same minute: distinct ids
        {'role': 'seller', 'text': '   ', 'sent_at_text': '12:54'}]}        # an attachment row carries no statement
    receipt = thread_receipt('1000000001', raw)
    assert [m['role'] for m in receipt['messages']] == ['user', 'seller', 'seller']
    assert len({m['id'] for m in receipt['messages']}) == 3 and receipt['found']
    with pytest.raises(ValueError):                                         # a conversation of another listing
        thread_receipt('1000000002', raw)
    with pytest.raises(ValueError):                                         # unknown sender is never defaulted
        thread_receipt('1000000001', {'url': base, 'messages': [{'role': None, 'text': 'x'}]})
    with pytest.raises(ValueError):                                         # not the account's message pages
        thread_url('https://www.sahibinden.com/mesajlarim/1000000001/a/b')
    with pytest.raises(ValueError):
        thread_url('https://banaozel.sahibinden.com/ilanlarim')
    long = thread_receipt('1000000001', {'url': base, 'messages': [{'role': 'seller', 'text': 'a' * 2000}]})
    assert long['messages'][0]['truncated'] and len(long['messages'][0]['text']) == 1500
    many = thread_receipt('1000000001', {'url': base, 'messages': [{'role': 'seller', 'text': f'm{i}'} for i in range(70)]})
    assert many['older_omitted'] and len(many['messages']) == 60 and many['messages'][-1]['text'] == 'm69'
    # "found: false" is only valid on the inbox, carries no messages, and is not a statement of any kind.
    inbox = {'listing_id': '1000000001', 'url': 'https://banaozel.sahibinden.com/mesajlarim', 'found': False, 'messages': []}
    assert ThreadReceipt.model_validate(inbox).found is False
    with pytest.raises(ValueError):
        ThreadReceipt.model_validate({**inbox, 'messages': [{'id': '0123456789ab', 'role': 'seller', 'text': 'x'}]})


def test_a_single_allowed_target_is_clicked_without_asking_the_model_and_still_needs_arrival():
    from verda.agency.jev import JevNavigator, JevStopped
    class Browser:
        url, clicked = 'https://banaozel.sahibinden.com/mesajlarim', []
        def evaluate(self, js): pass
        def observe(self, screenshot=False):
            return {'url': self.url, 'actions': [{'id': 'e1', 'kind': 'click', 'label': 'Satir A'}, {'id': 'e2', 'kind': 'click', 'label': 'Başka'}]}
        def act(self, action, page):
            self.clicked.append(action['id']); self.url = 'https://banaozel.sahibinden.com/mesajlarim/1000000001/a/b'
    b = Browser()
    url = JevNavigator(b, chooser=lambda *a: 1 / 0, sleep=lambda s: None).go(
        'x', reached=lambda u: u.count('/') > 4, allow=lambda l: l == 'Satir A')
    assert b.clicked == ['e1'] and url.endswith('/a/b')


def test_the_pause_between_browser_actions_is_random_between_half_a_second_and_two():
    from verda.agency.jev import human_pause
    values = [human_pause() for _ in range(200)]
    assert all(0.5 <= v <= 2.0 for v in values) and len(set(values)) > 150


def test_the_wait_between_reads_is_random_within_the_tool_range_and_fixed_otherwise():
    from verda.agency.registry import AgencyConfig
    from verda.agency.store import AgencyStore
    config = AgencyConfig.load(None)
    tools = {t.key: t for t in config.tools}
    for key in ('sahibinden.read_thread', 'sahibinden.list_inbox', 'sahibinden.search', 'sahibinden.read_listing'):
        values = [AgencyStore._pause(tools[key]) for _ in range(300)]
        assert all(0.5 <= v <= 6 for v in values) and len({round(v, 3) for v in values}) > 200
    assert AgencyStore._pause(tools['sahibinden.send_message']) == tools['sahibinden.send_message'].min_interval_seconds


def test_the_navigator_looks_again_when_the_target_has_not_rendered_yet():
    from verda.agency.jev import JevNavigator
    class Browser:
        def __init__(self): self.looks, self.url, self.clicked = 0, 'https://banaozel.sahibinden.com/mesajlarim', []
        def evaluate(self, js): pass
        def observe(self, screenshot=False):
            self.looks += 1
            return {'url': self.url, 'actions': [{'id': 'e1', 'kind': 'click', 'label': 'Satir A'}] if self.looks >= 3 else []}
        def act(self, action, page):
            self.clicked.append(action['id']); self.url = 'https://banaozel.sahibinden.com/mesajlarim/1000000001/a/b'
    b = Browser()
    JevNavigator(b, sleep=lambda s: None).go('x', reached=lambda u: u.count('/') > 4, allow=lambda l: l == 'Satir A')
    assert b.clicked == ['e1'] and b.looks >= 3


def test_search_pages_and_sort_are_part_of_the_address_and_must_be_proven_by_it():
    import pytest
    from verda.agency.jev import target_url, verify_receipt
    job = {'tool': 'sahibinden.search', 'arguments': {'query': 'x', 'page': 3, 'sort': 'newest'}}
    url = target_url(None, job)
    assert 'pagingOffset=40' in url and 'sorting=date_desc' in url
    assert 'pagingOffset' not in target_url(None, {'tool': 'sahibinden.search', 'arguments': {'query': 'x'}})
    rows = [{'listing_id': '1000000002', 'title': 'A', 'url': 'https://www.sahibinden.com/ilan/a-1000000002/detay', 'price_tl': 3000000.0, 'area_m2': 1053.0}]
    ok = verify_receipt(job, {'url': url, 'listings': rows, 'total_reported': 295})
    assert ok['page'] == 3 and ok['has_more'] is True
    with pytest.raises(ValueError):                       # a different page than the one assigned
        verify_receipt(job, {'url': url.replace('pagingOffset=40', 'pagingOffset=20'), 'listings': rows, 'total_reported': 295})
    with pytest.raises(ValueError):                       # sort order silently not applied
        verify_receipt(job, {'url': url.replace('&sorting=date_desc', ''), 'listings': rows, 'total_reported': 295})


def test_same_target_accepts_a_reordered_address_for_the_same_page_and_rejects_another_page():
    from verda.agency.jev import same_target
    want = 'https://www.sahibinden.com/satilik-arsa/mugla-datca?price_max=15000000&a507_min=1000&sorting=date_desc&pagingOffset=20'
    assert same_target('https://www.sahibinden.com/satilik-arsa/mugla-datca?pagingOffset=20&a507_min=1000&sorting=date_desc&price_max=15000000', want)
    assert not same_target('https://www.sahibinden.com/satilik-arsa/mugla-datca?pagingOffset=40&a507_min=1000&sorting=date_desc&price_max=15000000', want)
    assert not same_target('https://www.sahibinden.com/satilik-arsa/mugla-datca?a507_min=1000&sorting=date_desc&price_max=15000000', want)


def test_a_retitled_listing_is_the_same_listing_but_another_number_is_not():
    from verda.agency.jev import same_target
    old = 'https://www.sahibinden.com/ilan/emlak-arsa-satilik-ornek-emlakcidan-tarla-1000000005/detay'
    new = 'https://www.sahibinden.com/ilan/emlak-arsa-satilik-datca-ornek-mahalle-yatirimlik-tarla-1000000005/detay'
    assert same_target(new, old)
    assert not same_target(new.replace('1000000005', '1233135673'), old)
    assert not same_target('https://www.sahibinden.com/satilik-arsa/mugla-datca?price_max=15000000&a507_min=1000', old)


def test_a_removed_listing_is_a_receipt_not_a_read_error():
    import pytest
    from verda.agency.jev import verify_receipt
    from verda.agency.browser import ListingGone
    job = {'tool': 'sahibinden.read_listing', 'arguments': {'listing_id': '1000000004'}}
    raw = {'url': 'https://www.sahibinden.com/satilik-arsa/mugla-datca-koyler', 'removed': True, 'detail': None, 'listings': []}
    out = verify_receipt(job, raw)
    assert out['unavailable'] is True and out['listing_id'] == '1000000004' and ListingGone.model_validate(out)
    # a page that shows the listing is never treated as removed, and a search job never gets this receipt
    with pytest.raises(Exception):
        verify_receipt({**job, 'arguments': {'listing_id': '1000000004'}}, {**raw, 'removed': False})


def test_a_removed_listing_leaves_the_queue_and_extracts_no_facts():
    from verda.facts.action import next_action
    from verda.facts.extract import extract
    seen = {'listing_read': True, 'listing_removed': True}
    assert next_action(None, None, seen, 0, None) == {'action': 'none', 'why': 'listing_removed'}
    assert extract('sahibinden.read_listing', 'obs', 'local', {'listing_id': '1000000004', 'unavailable': True, 'notice': 'x'}, '1000000004') == []


def test_the_inbox_card_time_is_read_whether_it_is_a_clock_time_a_weekday_or_a_date():
    from verda.agency.jev import parse_row
    for last in ('16:14', 'Cumartesi', 'Dün', '10.10.2026', '10.10.2026 09:30'):
        assert parse_row(f'Başlık\n2\nSatıcı Adı\nön izleme metni\n{last}') == {'title': 'Başlık', 'counterpart': 'Satıcı Adı', 'last_at_text': last, 'unread': 2}
    assert parse_row('Başlık\nSatıcı Adı\nMerhaba, saat 14:30 gibi')['last_at_text'] is None            # a sentence that mentions a time is not a time
