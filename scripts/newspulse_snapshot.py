import csv, hashlib, io, json, math, os, re, time, urllib.parse, urllib.request, zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone

csv.field_size_limit(10_000_000)

LASTUPDATE = 'https://data.gdeltproject.org/gdeltv2/lastupdate.txt'
STATE_PATH = 'daily-maps/data/newspulse-state.json'
OUT_PATH = 'daily-maps/data/newspulse.json'
USER_AGENT = 'MitchellCo-NewsPulse/5.0 (+https://christophermitchell012.github.io/wildfire-map/)'
KEEP_HOURS = 48
MAX_LOCATIONS_PER_TOPIC = 600
MAX_OUTPUT_PER_TOPIC = 200
PROXIMITY_CHARS = 150
LEDE_CHARS = 1500
STATE_VERSION = 5
ALLOWED_LOCATION_TYPES = {'3', '4'}  # US state/country centroids (1/2) are too coarse.
MENTION_MIN_CONFIDENCE = 60
MAX_MENTION_URLS = 50

# GKG theme names are taxonomy identifiers, not prose. Match token-shaped theme names
# rather than arbitrary substrings so e.g. TAX_TERROR_GROUP_HAMAS does not become an
# event merely because the identifier contains TERROR.
TOPIC_THEME_PATTERNS = {
    'unrest': re.compile(r'(?:^|_)(?:PROTEST[A-Z]*|RIOT[A-Z]*|DEMONSTRAT[A-Z]*|CIVIL_UNREST|LABOR_STRIKE)(?:_|$)'),
    'violence': re.compile(r'(?:^|_)(?:SHOOT[A-Z]*|ARMEDCONFLICT|MASS_VIOLENCE|TERROR_ATTACK[A-Z]*|BOMBING[A-Z]*|EXPLOSION[A-Z]*)(?:_|$)'),
    'transport': re.compile(r'(?:^|_)(?:AVIATION|AIRPORT|AIRLINE|DERAIL[A-Z]*|TRAIN_CRASH|PLANE_CRASH|ROAD_CLOS[A-Z]*|TRAFFIC_ACCIDENT)(?:_|$)'),
    'health': re.compile(r'(?:^|_)(?:DISEASE[A-Z]*|OUTBREAK[A-Z]*|EPIDEMIC[A-Z]*|PANDEMIC[A-Z]*|INFECT[A-Z]*|COMMUNICABLE[A-Z]*|VIRUS[A-Z]*|INFLUENZA|MEASLES|MPOX|CHOLERA|EBOLA|DENGUE|MALARIA|SALMONELLA|LISTERIA|E_COLI)(?:_|$)'),
    'economy': re.compile(r'(?:^|_)(?:LAYOFF[A-Z]*|BANKRUPT[A-Z]*|UNEMPLOY[A-Z]*|RECESSION[A-Z]*|PLANT_CLOS[A-Z]*|FACTORY_CLOS[A-Z]*|JOB_LOSS[A-Z]*)(?:_|$)'),
}
EVENT_ROOT_TOPIC = {'14': 'unrest', '18': 'violence', '19': 'violence', '20': 'violence'}

# URL evidence is the second independent signal for a link. Normalize punctuation to
# spaces before matching, so word boundaries behave as intended: "flu" no longer matches
# influencer/influence/affluent/superfluous/fluke and "viral" is not a health keyword.
TOPIC_URL_RE = {
    'unrest': re.compile(r'\b(?:protest(?:s|er|ers|ing)?|riot(?:s|ing)?|demonstration(?:s)?|strike(?:s|rs)?|unrest)\b', re.I),
    'violence': re.compile(r'\b(?:shooting(?:s)?|shot|gunfire|gunman|attack(?:s|ed)?|bomb(?:ing|ings|s)?|explosion(?:s)?|blast(?:s)?|massacre|terrorism|terrorist(?:s)?)\b', re.I),
    'transport': re.compile(r'\b(?:crash(?:es|ed)?|collision(?:s)?|derail(?:ed|ment|ments)?|airport|airline|flight(?:s)?|aviation|traffic|road closure|train)\b', re.I),
    'health': re.compile(r'\b(?:disease|outbreak(?:s)?|epidemic|pandemic|infection(?:s)?|infected|virus(?:es)?|covid|flu|influenza|measles|mpox|cholera|ebola|dengue|malaria|salmonella|listeria|e coli|rabies|rabid|west nile|bird flu|h5n1|diphtheria|screwworm)\b', re.I),
    'economy': re.compile(r'\b(?:layoff(?:s)?|job cuts?|job losses|bankrupt(?:cy)?|unemployment|recession|plant closure|factory closure)\b', re.I),
}

# Hub pages, live blogs, tags and category feeds routinely mention several unrelated
# places/events. They are useful browsing pages but unsafe story identities for map dots.
LOW_QUALITY_PATH_RE = re.compile(
    r'(?:/(?:hub|tag|tags|category|categories|topic|topics|live)(?:/|$)|'
    r'\b(?:live[-_/ ]?updates?|live[-_/ ]?blog|liveblog|breaking[-_/ ]?news[-_/ ]?live)\b)',
    re.I,
)
TRACKING_QUERY_PREFIXES = ('utm_', 'fbclid', 'gclid', 'mc_', 'ref')


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


def canonical_url(url):
    try:
        p = urllib.parse.urlsplit(url.strip())
        if p.scheme not in ('http', 'https') or not p.netloc:
            return ''
        query = []
        for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True):
            kl = k.lower()
            if any(kl == prefix or kl.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
                continue
            query.append((k, v))
        path = re.sub(r'/+', '/', p.path or '/')
        return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urllib.parse.urlencode(query), ''))
    except Exception:
        return ''


def url_story_id(url):
    c = canonical_url(url) or url
    return 'url:' + hashlib.sha1(c.encode('utf-8', errors='ignore')).hexdigest()[:20]


def is_low_quality_url(url):
    try:
        p = urllib.parse.urlsplit(urllib.parse.unquote(url))
        text = (p.path or '/') + ('?' + p.query if p.query else '')
    except Exception:
        text = str(url)
    return bool(LOW_QUALITY_PATH_RE.search(text))


def topic_url_evidence(topic, url):
    if topic not in TOPIC_URL_RE or is_low_quality_url(url):
        return False
    try:
        p = urllib.parse.urlsplit(urllib.parse.unquote(url))
        text = f'{p.path} {p.query}'
    except Exception:
        text = str(url)
    text = re.sub(r'[^a-zA-Z0-9]+', ' ', text).strip().lower()
    return bool(TOPIC_URL_RE[topic].search(text))


def classify_theme(name):
    u = (name or '').strip().upper()
    return [topic for topic, pattern in TOPIC_THEME_PATTERNS.items() if pattern.search(u)]


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
        # GKG V2Locations is LocationType#FullName#CountryCode#ADM1Code#ADM2Code#Lat#Long#FeatureID#CharOffset.
        if len(p) < 9 or p[0].strip() not in ALLOWED_LOCATION_TYPES:
            continue
        lat, lon = safe_float(p[5]), safe_float(p[6])
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        try:
            off = int(p[8])
        except Exception:
            continue
        name = (p[1] or 'Mapped location').strip()
        out.append((name, lat, lon, off, p[0].strip()))
    return out


def loc_key(name, lat, lon, story_id=''):
    # A story/event is the primary identity. This stops a location bucket from pooling
    # alphabetically early links from unrelated stories that merely share a city/topic.
    if story_id:
        return story_id
    return f'loc:{name.lower()}|{lat:.2f}|{lon:.2f}'


def add_obs(bucket, topic, name, lat, lon, url, source_domain, count=1, event_count=0, story_id=''):
    k = loc_key(name, lat, lon, story_id)
    x = bucket[topic].setdefault(k, {
        'story_id': story_id,
        'name': name,
        'lat': round(lat, 5),
        'lon': round(lon, 5),
        'urls': set(),
        'domains': set(),
        'event_count': 0,
    })
    if url:
        x['urls'].add(canonical_url(url) or url)
    if source_domain:
        x['domains'].add(source_domain)
    if not url:
        x.setdefault('extra_count', 0)
        x['extra_count'] += count
    x['event_count'] += event_count


def parse_gkg(url, bucket):
    for row in read_zip_rows(url):
        if len(row) < 11:
            continue
        doc_url = canonical_url(row[4].strip() if len(row) > 4 else '')
        if not doc_url:
            continue
        src = (row[3].strip().lower() if len(row) > 3 else '') or domain(doc_url)
        themes = parse_themes(row[8] if len(row) > 8 else '')
        locs = parse_locations(row[10] if len(row) > 10 else '')
        if not themes or not locs or is_low_quality_url(doc_url):
            continue

        # One document may contain several occurrences of a theme and several places.
        # Pick only the best theme/location pair for each (URL, topic), rather than
        # emitting a dot for every occurrence and spraying one URL across the map.
        best_by_topic = {}
        for topic, toff, theme_name in themes:
            if toff is None or toff < 0 or toff > LEDE_CHARS:
                continue
            if not topic_url_evidence(topic, doc_url):
                continue
            nearest = min(locs, key=lambda l: abs(l[3] - toff))
            distance = abs(nearest[3] - toff)
            if distance > PROXIMITY_CHARS:
                continue
            candidate = (distance, toff, nearest, theme_name)
            if topic not in best_by_topic or candidate[:2] < best_by_topic[topic][:2]:
                best_by_topic[topic] = candidate

        story_id = url_story_id(doc_url)
        for topic, (_distance, _toff, nearest, _theme_name) in best_by_topic.items():
            name, lat, lon, _loff, _loc_type = nearest
            add_obs(bucket, topic, name, lat, lon, doc_url, src, story_id=story_id)


def parse_events(url):
    events = {}
    for row in read_zip_rows(url):
        # GDELT Events 2.0 export has 61 columns, ending with SOURCEURL at index 60.
        if len(row) < 61:
            continue
        event_id = row[0].strip()
        root = row[28].strip()
        topic = EVENT_ROOT_TOPIC.get(root)
        if not topic:
            continue
        action_geo_type = row[51].strip()
        if action_geo_type not in ALLOWED_LOCATION_TYPES:
            continue
        lat, lon = safe_float(row[56]), safe_float(row[57])
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        name = row[52].strip() or 'Mapped event location'
        source_url = canonical_url(row[60].strip())
        events[event_id] = {
            'topic': topic,
            'name': name,
            'lat': lat,
            'lon': lon,
            'url': source_url,
            'story_id': 'event:' + event_id,
        }
    return events


def parse_mentions(url, events, bucket):
    mention_urls = defaultdict(set)
    for row in read_zip_rows(url):
        # Mentions V2: InRawText=index 10, Confidence=index 11.
        if len(row) < 12:
            continue
        event_id = row[0].strip()
        if event_id not in events:
            continue
        try:
            confidence = int(float(row[11]))
        except Exception:
            continue
        if confidence < MENTION_MIN_CONFIDENCE or row[10].strip() != '1':
            continue
        ident = canonical_url(row[5].strip())
        if not ident:
            continue
        topic = events[event_id]['topic']
        if topic_url_evidence(topic, ident):
            mention_urls[event_id].add(ident)

    for event_id, ev in events.items():
        urls = sorted(mention_urls.get(event_id, set()))
        if not urls and ev['url'] and topic_url_evidence(ev['topic'], ev['url']):
            urls = [ev['url']]
        urls = urls[:MAX_MENTION_URLS]

        if urls:
            # The event is counted once, regardless of how many qualifying mentions link it.
            for i, u in enumerate(urls):
                add_obs(
                    bucket, ev['topic'], ev['name'], ev['lat'], ev['lon'],
                    u, domain(u), event_count=1 if i == 0 else 0,
                    story_id=ev['story_id'],
                )
        else:
            # Keep the structured event without inventing or attaching a weak URL.
            add_obs(
                bucket, ev['topic'], ev['name'], ev['lat'], ev['lon'],
                '', '', count=1, event_count=1, story_id=ev['story_id'],
            )


def load_state():
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            s = json.load(f)
        if s.get('version') == STATE_VERSION:
            return s
    except Exception:
        pass
    return {'version': STATE_VERSION, 'processed': [], 'locations': {k: {} for k in TOPIC_THEME_PATTERNS}}


def append_batch(state, ts, bucket):
    for topic, stories in bucket.items():
        store = state['locations'].setdefault(topic, {})
        for k, x in stories.items():
            rec = store.setdefault(k, {
                'story_id': x.get('story_id', ''),
                'name': x['name'], 'lat': x['lat'], 'lon': x['lon'], 'bins': {}
            })
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
            ranked = sorted(
                store.items(),
                key=lambda kv: sum(b.get('count', 0) for b in kv[1].get('bins', {}).values()),
                reverse=True,
            )
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
                    for u in sorted(b.get('links', [])):
                        if u not in links and len(links) < 5:
                            links.append(u)
            if cur_count <= 0:
                continue
            common = {'name': rec['name'], 'lat': rec['lat'], 'lon': rec['lon']}
            cur_rows.append({
                **common,
                'count': cur_count,
                'domains': sorted(domains)[:20],
                'links': [{'href': u, 'text': domain(u) or 'article'} for u in links],
                'event_count': event_count,
            })
            base_rows.append({**common, 'count': base_count})
        order = sorted(
            range(len(cur_rows)),
            key=lambda i: (cur_rows[i]['count'], len(cur_rows[i]['domains']), cur_rows[i].get('event_count', 0)),
            reverse=True,
        )[:MAX_OUTPUT_PER_TOPIC]
        result[topic] = {'current': [cur_rows[i] for i in order], 'baseline': [base_rows[i] for i in order]}
    return result


def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    latest_ts, templates, feed_timestamps = parse_lastupdate(request_text(LASTUPDATE))
    latest_dt = datetime.strptime(latest_ts, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    state = load_state()
    errors, processed_now = [], []

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
        'method': (
            'Story-scoped GDELT mapping: one best city-level theme/location pair per URL/topic; '
            '<=150-character proximity within the first 1,500 characters; topic evidence required in '
            'article URL; hub/live/tag/category URLs rejected; Events require ActionGeo type 3/4; '
            'Mentions require Confidence>=60 and InRawText=1; each structured event counted once.'
        ),
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
