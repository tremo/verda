from verda.agency.jev import own_tab


def test_verda_reuses_its_own_tab_and_opens_a_window_only_when_that_tab_is_gone(tmp_path):
    # 25 Nov 2026: a new tab per job landed in the owner's working window. Verda keeps one tab in a window of its own.
    calls, pages, made = [], [{'targetId': 'USER', 'type': 'page'}], []

    def cdp(method, **params):
        calls.append((method, params))
        if method == 'Target.getTargets':
            return {'targetInfos': list(pages)}
        if method == 'Target.createTarget':
            made.append(1)
            pages.append({'targetId': f'V{len(made)}', 'type': 'page'})
            return {'targetId': pages[-1]['targetId']}
    path = tmp_path / 'tab.json'
    first = own_tab(cdp, path)
    assert first == 'V1' and ('Target.createTarget', {'url': 'about:blank', 'newWindow': True}) in calls
    calls.clear()
    assert own_tab(cdp, path) == 'V1' and not any(m == 'Target.createTarget' for m, _ in calls)      # still open: the same tab, no new window
    pages[:] = [p for p in pages if p['targetId'] != 'V1']                                          # the owner closed Verda's window
    assert own_tab(cdp, path) == 'V2'
    path.write_text('not json')
    assert own_tab(cdp, path) == 'V3'                                                               # a broken record makes a new tab, never fails
