"""Keeps an agent's working history small. The full receipts stay in the append-only records and can be read in parts."""
import json

MAX_TEXT = 500
MAX_LIST = 30
MAX_OUTPUT_BYTES = 6000
CONTEXT_BUDGET = 56_000          # the provider refuses more than 64 KiB


def _size(value):
    return len(json.dumps(value, ensure_ascii=False).encode())


SMALL_OUTPUT = 1500
# Tools whose output IS what the agent decides from (queues, plans, statuses): never summarized, only capped when huge.
DECISION_TOOLS = frozenset({'facts.queue', 'scan.delta', 'inbox.plan', 'facts.status', 'listings.rank', 'records.audit', 'records.listing', 'policy.screen', 'facts.propose', 'listing.flag', 'replies.pending'})
DECISION_CAP = 24_000


def _summary(output):
    """What an agent needs from a big output: its own scalars and the size of everything nested. The full output stays in the record."""
    out = {}
    for key, value in output.items():
        if isinstance(value, str):
            out[key] = value if len(value) <= 150 else value[:150] + '…[kısaltıldı]'
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, list):
            out[key] = f'[{len(value)} öğe]'
        elif isinstance(value, dict):
            out[key] = '[nesne: ' + ', '.join(list(value)[:6]) + (', …' if len(value) > 6 else '') + ']'
    out['_özet'] = 'Tam çıktı ortak kayıtta saklandı; bu görünüm kısaltılmıştır, işlem başarıyla tamamlanmıştır.'
    return out


def compact_output(output):
    """A bounded view of one tool output for the agent's working history."""
    if isinstance(output, dict) and isinstance(output.get('listings'), list) and 'total_reported' in output:
        out = {k: output[k] for k in ('page', 'total_reported', 'has_more') if k in output}
        out['listings'] = [{k: row.get(k) for k in ('listing_id', 'price_tl', 'area_m2')} for row in output['listings'][:MAX_LIST]]
        return out
    if isinstance(output, dict) and _size(output) > SMALL_OUTPUT:
        return _summary(output)
    return output


def compact_history(history):
    out = []
    for item in history:
        data = item['data']
        if item['kind'] == 'decision' and isinstance(data, dict) and 'decision' in data:
            # The recorded turn also carries the agent's own instructions and token usage (~7 KB). Those are bookkeeping, not memory:
            # kept, they filled the budget after five turns and pushed the first reads of a batch out of the agent's view.
            data = {'decision': data['decision']}
        elif item['kind'] == 'tool_finished' and isinstance(data, dict) and 'output' in data:
            if data.get('tool') in DECISION_TOOLS and _size(data['output']) <= DECISION_CAP:
                pass
            else:
                data = {**data, 'output': compact_output(data['output'])}
        out.append({**item, 'data': data})
    return out


def batch_progress(inputs, events):
    """Where a batch task stands, from every event of the task, so it never depends on how much history still fits the context.
    `events`: (kind, data) pairs in order, data already decoded. None for a task that is not a batch."""
    if not isinstance(inputs, dict) or inputs.get('routine') != 'batch':
        return None
    items = [str(i['listing_id']) for i in inputs.get('items') or [] if isinstance(i, dict) and i.get('listing_id')]
    if not items:
        return None
    asked, done = {}, set()
    for kind, data in events:
        if kind == 'tool_started' and data.get('tool') == inputs.get('action'):
            asked[data['call_id']] = str((data.get('arguments') or {}).get('listing_id'))
        elif kind == 'tool_finished' and data.get('state') == 'complete' and data.get('call_id') in asked:
            done.add(asked[data['call_id']])
    remaining = [i for i in items if i not in done]
    return {'done': [i for i in items if i in done], 'remaining': remaining, 'next': remaining[0] if remaining else None}


def fit_context(context, budget=CONTEXT_BUDGET):
    """Drop the oldest history entries until the whole context fits the budget; if it still does not fit, the older delivered records and
    record reads go too, each marked `*_omitted` so the agent knows it has not seen them (the protocol tells it not to assume otherwise)."""
    history = list(context.get('history', []))
    while history and _size({**context, 'history': history}) > budget:
        history.pop(0)
    out = {**context, 'history': history}
    for key in ('record_reads', 'received_records'):          # newest first in received_records: the oldest is the last
        items = list(out.get(key) or [])
        while items and _size(out) > budget:
            items.pop(-1 if key == 'received_records' else 0)
            out = {**out, key: items, key + '_omitted': True}
    return out


def budget_for(instructions, limit=65536, margin=1500):
    """What is left for the context once the agent's own instructions are counted (the provider refuses more than 64 KiB in all).
    A fixed budget was right while prompts were short; the manager's prompt grew to 12 KB and two runs failed on 15 Oct 2026."""
    import json
    return min(CONTEXT_BUDGET, limit - len(json.dumps(instructions, ensure_ascii=False).encode()) - margin)
