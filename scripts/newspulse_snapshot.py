import csv, io, json, math, os, re, time, urllib.parse, urllib.request, zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# GDELT GKG rows occasionally contain individual tab-delimited fields larger than
# Python's default 128 KiB CSV parser limit. Keep a generous but bounded ceiling.
csv.field_size_limit(10_000_000)

LASTUPDATE = 'https://data.gdeltproject.org/gdeltv2/lastupdate.txt'
STATE_PATH = 'daily-maps/data/newspulse-state.json'
OUT_PATH = 'daily-maps/data/newspulse.json'
USER_AGENT = 'MitchellCo-NewsPulse/4.1 (+https://christophermitchell012.github.io/wildfire-map/)'
KEEP_HOURS = 48
MAX_LOCATIONS_PER_TOPIC = 600
MAX_OUTPUT_PER_TOPIC = 200
PROXIMITY_CHARS = 300
STATE_VERSION = 4

# Deliberately avoid generic terms such as HEALTH, MEDICAL, ECON and BUSINESS.
# Those broad GKG themes caused unrelated articles to be attached to topic/location dots.
TOPIC_THEME_TERMS = {
    'unrest': ('PROTEST', 'RIOT', 'DEMONSTRAT', 'CIVIL_UNREST', 'LABOR_STRIKE'),
    'violence': ('TERROR', 'SHOOT', 'BOMB', 'EXPLOS', 'ARMEDCONFLICT', 'MASS_VIOLENCE'),
    'transport': ('AVIATION', 'AIRPORT', 'AIRLINE', 'DERAIL', 'TRAIN_CRASH', 'PLANE_CRASH', 'ROAD_CLOS', 'TRAFFIC_ACCIDENT'),
    'health': ('DISEASE', 'OUTBREAK', 'EPIDEMIC', 'PANDEMIC', 'INFECT', 'COMMUNICABLE', 'VIRUS', 'INFLUENZA', 'MEASLES', 'MPOX', 'CHOLERA', 'EBOLA', 'DENGUE', 'MALARIA', 'SALMONELLA', 'LISTERIA', 'E_COLI'),
    'economy': ('LAYOFF', 'BANKRUPT', 'UNEMPLOY', 'RECESSION', 'PLANT_CLOS', 'FACTORY_CLOS', 'JOB_LOSS'),
}
EVENT_ROOT_TOPIC = {'14': 'unrest', '18': 'violence', '19': 'violence', '20': 'violence'}

# Disease/outbreak is intentionally high precision. GKG theme proximity alone can still
# associate a secondary disease theme with an unrelated location/article. Require the
# article URL itself to carry disease/outbreak evidence before exposing it on this layer.
HEALTH_URL_RE = re.compile(
    r'(?:disease|outbreak|epidemic|pandemic|infect(?:ion|ed|ious)?|virus|viral|covid|'
    r'flu|influenza|measles|mpox|cholera|ebola|dengue|malaria|salmonella|listeria|'
    r'e[-_]?coli|rabies|rabid|west[-_]?nile|bird[-_]?flu|h5n1|diphtheria|screwworm)',
    re.I,
)


def request_bytes(url, attempts=3, timeout=45):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(2 ** i)
    raise last


def request_text(url):
    return request_bytes(url).decode('utf-8', errors='replace')


def parse_lastupdate(text):
    files, timestamps = {}, {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        url = parts[-1]
        m = re.search(r'/([0-9]{14})\.(export\.CSV|mentions\.CSV|gkg\.csv)\.zip$', url, re.I)
        if not m:
            continue
        ts, kind = m.group(1), m.group(2).lower()
        files[kind] = url
        timestamps[kind] = ts
    required = {'export.csv', 'mentions.csv', 'gkg.csv'}
    if not required <= files.keys():
        raise RuntimeError('lastupdate.txt did not contain the expected export, mentions, and GKG files')
    # GDELT's three feeds can advance at slightly different times. Use the oldest latest
    # timestamp so we only request 15-minute batches that should exist for all three feeds.
    safe_latest_ts = min(timestamps[k] for k in required)
    return safe_latest_ts, files, timestamps


def sibling_url(template, ts):
    return re.sub(r'/[0-9]{14}(?=\.)', '/' + ts, template)


def read_zip_rows(url):
    raw = request_bytes(url)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        if not names:
            return
        with z.open(names[0]) as f:
            text = io.TextIOWrapper(f, encoding='utf-8', errors='replace', newline='')
            yield from csv.reader(text, delimiter='\t')


def safe_float(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def domain(url):
    try:
        host = urllib.parse.urlparse(url).hostname or ''
        return re.sub(r'^www\.', '', host.lower())
    except Exception:
        return ''


def topic_url_evidence(topic, url):
    if topic != 'health':
        return True
    try:
        text = urllib.parse.unquote(url).lower()
    except Exception:
        text = str(url).lower()
    return bool(HEALTH_URL_RE.search(text))


def classify_theme(name):
    u = name.upper()
    return [topic for topic, needles in TOPIC_THEME_TERMS.items() if any(n in u for n in needles)]


def parse_themes(field):
    out = []
    for block in (field or '').split(';'):
        block = block.strip()
        if not block:
            continue
        if ',' in block:
            name, off = block.rsplit(',', 1)
            try:
                off = int(off)
            except Exception:
                off = None
        else:
            name, off = block, None
        for topic in classify_theme(name):
            out.append((topic, off, name))
    return out


def parse_locations(field):
    out = []
    for block in (field or '').split(';'):
        p = block.split('#')
        if len(p) < 7:
            continue
        lat, lon = safe_float(p[5]), safe_float(p[6])
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        name = (p[1] or 'Mapped location').strip()
        off = None
        if len(p) >= 9:
            try:
                off = int(p[-1])
            except Exception:
                pass
        out.append((name, lat, lon, off))
    return out


def loc_key(name, lat, lon):
    return f'{name.lower()}|{lat:.2f}|{lon:.2f}'


def add_obs(bucket, topic, name, lat, lon, url, source_domain, count=1, event_count=0):
    k = loc_key(name, lat, lon)
    x = bucket[topic].setdefault(k, {
        'name': name, 'lat': round(lat, 5), 'lon': round(lon, 5),
        'urls': set(), 'domains': set(), 'event_count': 0
    })
    if url:
        x['urls'].add(url)
    if source_domain:
        x['domains'].add(source_domain)
    if not url:
        x.setdefault('extra_count', 0)
        x['extra_count'] += count
    x['event_count'] += event_count


def parse_gkg(url, bucket):
    seen = set()
    for row in read_zip_rows(url):
        if len(row) < 11:
            continue
        doc_url = row[4].strip() if len(row) > 4 else ''
        if not doc_url.startswith(('http://', 'https://')):
            continue
        src = (row[3].strip().lower() if len(row) > 3 else '') or domain(doc_url)
        themes = parse_themes(row[8] if len(row) > 8 else '')
        locs = parse_locations(row[10] if len(row) > 10 else '')
        if not themes or not locs:
            continue
        for topic, toff, _theme_name in themes:
            if not topic_url_evidence(topic, doc_url):
                continue
            if toff is None:
                continue
            with_offsets = [l for l in locs if l[3] is not None]
            if not with_offsets:
                continue
            nearest = min(with_offsets, key=lambda l: abs(l[3] - toff))
            distance = abs(nearest[3] - toff)
            if distance > PROXIMITY_CHARS:
                continue
            name, lat, lon, _ = nearest
            dedupe = (topic, doc_url, round(lat, 2), round(lon, 2))
            if dedupe in seen:
                continue
            seen.add(dedupe)
            add_obs(bucket, topic, name, lat, lon, doc_url, src)


def parse_events(url):
    events = {}
    for row in read_zip_rows(url):
        if len(row) < 60:
            continue
        event_id = row[0].strip()
        root = row[28].strip() if len(row) > 28 else ''
        topic = EVENT_ROOT_TOPIC.get(root)
        if not topic:
            continue
        lat, lon = safe_float(row[-5]), safe_float(row[-4])
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        name = (row[-9].strip() if len(row) >= 9 else '') or 'Mapped event location'
        source_url = row[-1].strip()
        events[event_id] = {'topic': topic, 'name': name, 'lat': lat, 'lon': lon, 'url': source_url}
    return events


def parse_mentions(url, events, bucket):
    mention_urls = defaultdict(set)
    for row in read_zip_rows(url):
        if len(row) < 6:
            continue
        event_id = row[0].strip()
        if event_id not in events:
            continue
        ident = row[5].strip()
        if ident.startswith(('http://', 'https://')):
            mention_urls[event_id].add(ident)
    for event_id, ev in events.items():
        urls = mention_urls.get(event_id) or ({ev['url']} if ev['url'] else set())
        if urls:
            for u in list(urls)[:50]:
                add_obs(bucket, ev['topic'], ev['name'], ev['lat'], ev['lon'], u, domain(u), event_count=1)
        else:
            add_obs(bucket, ev['topic'], ev['name'], ev['lat'], ev['lon'], '', '', count=1, event_count=1)


def load_state():
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            s = json.load(f)
        if s.get('version') == STATE_VERSION:
            return s
    except Exception:
        pass
    return {'version': STATE_VERSION, 'processed': [], 'locations': {k: {} for k in TOPIC_THEME_TERMS}}


def append_batch(state, ts, bucket):
    for topic, locs in bucket.items():
        store = state['locations'].setdefault(topic, {})
        for k, x in locs.items():
            rec = store.setdefault(k, {'name': x['name'], 'lat': x['lat'], 'lon': x['lon'], 'bins': {}})
            rec['bins'][ts] = {
                'count': len(x['urls']) + int(x.get('extra_count', 0)),
                'domains': sorted(x['domains'])[:30],
                'links': sorted(x['urls'])[:5],
                'event_count': int(x.get('event_count', 0)),
            }
    if ts not in state['processed']:
        state['processed'].append(ts)


def prune_state(state, latest_dt):
    cutoff = latest_dt - timedelta(hours=KEEP_HOURS)
    keep_processed = []
    for ts in state.get('processed', []):
        try:
            if datetime.strptime(ts, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc) >= cutoff:
                keep_processed.append(ts)
        except Exception:
            pass
    state['processed'] = sorted(set(keep_processed))
    for topic, store in state['locations'].items():
        dead = []
        for k, rec in store.items():
            rec['bins'] = {ts: b for ts, b in rec.get('bins', {}).items() if ts in state['processed']}
            if not rec['bins']:
                dead.append(k)
        for k in dead:
            del store[k]
        if len(store) > MAX_LOCATIONS_PER_TOPIC:
            ranked = sorted(store.items(), key=lambda kv: sum(b.get('count', 0) for b in kv[1].get('bins', {}).values()), reverse=True)
            state['locations'][topic] = dict(ranked[:MAX_LOCATIONS_PER_TOPIC])


def build_window(state, latest_dt, hours):
    result = {}
    current_cut = latest_dt - timedelta(hours=hours)
    full_cut = latest_dt - timedelta(hours=KEEP_HOURS)
    for topic, store in state['locations'].items():
        cur_rows, base_rows = [], []
        for rec in store.values():
            cur_count = base_count = event_count = 0
            domains, links = set(), []
            for ts, b in rec.get('bins', {}).items():
                try:
                    dt = datetime.strptime(ts, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if dt < full_cut:
                    continue
                base_count += b.get('count', 0)
                if dt >= current_cut:
                    cur_count += b.get('count', 0)
                    event_count += b.get('event_count', 0)
                    domains.update(b.get('domains', []))
                    for u in b.get('links', []):
                        if u not in links and len(links) < 5:
                            links.append(u)
            if cur_count <= 0:
                continue
            common = {'name': rec['name'], 'lat': rec['lat'], 'lon': rec['lon']}
            cur_rows.append({**common, 'count': cur_count, 'domains': sorted(domains)[:20], 'links': [{'href': u, 'text': domain(u) or 'article'} for u in links], 'event_count': event_count})
            base_rows.append({**common, 'count': base_count})
        order = sorted(range(len(cur_rows)), key=lambda i: (cur_rows[i]['count'], len(cur_rows[i]['domains']), cur_rows[i].get('event_count', 0)), reverse=True)[:MAX_OUTPUT_PER_TOPIC]
        result[topic] = {'current': [cur_rows[i] for i in order], 'baseline': [base_rows[i] for i in order]}
    return result


def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    latest_ts, templates, feed_timestamps = parse_lastupdate(request_text(LASTUPDATE))
    latest_dt = datetime.strptime(latest_ts, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    state = load_state()
    errors, processed_now = [], []

    # Process the latest four complete 15-minute slots so a delayed 30-minute Action
    # does not miss a batch. "Complete" means all three GDELT feeds have advanced to
    # at least this timestamp, avoiding transient 404s from a faster feed leading the rest.
    for minutes_back in (45, 30, 15, 0):
        dt = latest_dt - timedelta(minutes=minutes_back)
        ts = dt.strftime('%Y%m%d%H%M%S')
        if ts in state.get('processed', []):
            continue
        bucket = defaultdict(dict)
        try:
            parse_gkg(sibling_url(templates['gkg.csv'], ts), bucket)
            events = parse_events(sibling_url(templates['export.csv'], ts))
            parse_mentions(sibling_url(templates['mentions.csv'], ts), events, bucket)
            append_batch(state, ts, bucket)
            processed_now.append(ts)
        except urllib.error.HTTPError as e:
            # A historical slot can occasionally be absent upstream. Keep the prior
            # rolling state and report a real gap, but do not manufacture data.
            errors.append(f'{ts}: HTTP {e.code}')
        except Exception as e:
            errors.append(f'{ts}: {type(e).__name__}: {e}')

    prune_state(state, latest_dt)
    windows = {'6': build_window(state, latest_dt, 6), '12': build_window(state, latest_dt, 12)}
    oldest = min(state.get('processed') or [latest_ts])
    oldest_dt = datetime.strptime(oldest, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    warm_hours = max(0, (latest_dt - oldest_dt).total_seconds() / 3600)
    payload = {
        'generated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'data_through': latest_dt.isoformat().replace('+00:00', 'Z'),
        'source': 'GDELT 2.0 raw 15-minute files via lastupdate.txt',
        'windows': windows,
        'partial': bool(errors),
        'errors': errors[:20],
        'processed_now': processed_now,
        'rolling_history_hours': round(min(KEEP_HOURS, warm_hours), 2),
        'feed_timestamps': feed_timestamps,
        'method': 'Strict GKG topic themes with <=300-character theme/location proximity; disease/outbreak also requires disease evidence in the article URL; structured CAMEO event locations and Mentions URLs; 48h state retained incrementally.'
    }
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, separators=(',', ':'))
        f.write('\n')
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
        f.write('\n')
    print('Safe complete GDELT batch:', latest_ts)
    print('Feed timestamps:', feed_timestamps)
    print('Processed now:', processed_now)
    print('Errors:', errors)
    print('Rolling history hours:', payload['rolling_history_hours'])


if __name__ == '__main__':
    main()
