import json

STATE_PATH = 'daily-maps/data/newspulse-state.json'
OUT_PATH = 'daily-maps/data/newspulse.json'


def has_links_in_bins(rec):
    return any(bool(b.get('links')) for b in rec.get('bins', {}).values())


def prune_state():
    with open(STATE_PATH, 'r', encoding='utf-8') as f:
        state = json.load(f)

    removed = 0
    for topic, store in state.get('locations', {}).items():
        dead = [k for k, rec in store.items() if not has_links_in_bins(rec)]
        for k in dead:
            del store[k]
        removed += len(dead)

    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, separators=(',', ':'))
        f.write('\n')
    return removed


def prune_output():
    with open(OUT_PATH, 'r', encoding='utf-8') as f:
        payload = json.load(f)

    removed = 0
    for window in payload.get('windows', {}).values():
        for topic_data in window.values():
            current = topic_data.get('current', [])
            baseline = topic_data.get('baseline', [])
            kept_current = []
            kept_baseline = []
            for i, cur in enumerate(current):
                if not cur.get('links'):
                    removed += 1
                    continue
                kept_current.append(cur)
                if i < len(baseline):
                    kept_baseline.append(baseline[i])
            topic_data['current'] = kept_current
            topic_data['baseline'] = kept_baseline

    payload['method'] = (
        payload.get('method', '').rstrip('.')
        + '; visible events require at least one verified article URL.'
    )

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
        f.write('\n')
    return removed


def main():
    state_removed = prune_state()
    output_removed = prune_output()
    print('NewsPulse zero-link state records removed:', state_removed)
    print('NewsPulse zero-link visible rows removed:', output_removed)


if __name__ == '__main__':
    main()
