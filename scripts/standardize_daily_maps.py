#!/usr/bin/env python3
"""Apply the MitchellCo EvacWatch v2 UX/reliability standard to daily maps.

This script is deliberately conservative: it adds a shared resilient runtime, a
standard data-health panel when one is missing, and map-specific fusion framing
for maps 01-13. It does not rewrite each map's analytical model.

Run with --check to fail when a daily map is not standardized.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAP_DIR = ROOT / "daily-maps"
STANDARD = "evacwatch-v2"

QUESTIONS = {
    "01": "Where are flood conditions overlapping with exposed and vulnerable communities?",
    "02": "Where is dangerous heat overlapping with large and socially vulnerable populations?",
    "03": "Where is wildfire smoke exposure most consequential for people?",
    "04": "Where could tropical-cyclone hazards create the greatest human and infrastructure impacts?",
    "05": "Where are breaking events accelerating beyond normal news activity?",
    "06": "Where do severe-storm hazards overlap with populated, vulnerable communities?",
    "07": "Where are populated, vulnerable communities closest to active wildfire perimeters?",
    "08": "Where are drought conditions translating into water-supply stress for people and agriculture?",
    "09": "Where is extreme weather coinciding with grid constraints and population exposure?",
    "10": "Where do drinking-water compliance signals overlap with large, vulnerable populations?",
    "11": "Where do independent network signals suggest a potentially meaningful internet disruption?",
    "12": "Where are coastal-flood signals overlapping with exposed, vulnerable communities?",
    "13": "Where are recent earthquakes most likely to matter for nearby populations?",
}

FUSION = {
    "01": ("Flood alerts / river conditions", "Population and places", "Social vulnerability and access consequences"),
    "02": ("Extreme-heat conditions", "Population exposure", "Social vulnerability / health sensitivity"),
    "03": ("Smoke / fire-related air conditions", "Population exposure", "Health vulnerability"),
    "04": ("Tropical-cyclone / surge hazard", "People and infrastructure", "Potential human and access consequences"),
    "05": ("Current event / news acceleration", "Geographic concentration and distinct sources", "Attention signal, not event severity"),
    "06": ("Severe-weather / tornado signals", "Population exposure", "Social vulnerability"),
    "07": ("Wildfire proximity and condition", "Population", "SVI, fire-weather and access context"),
    "08": ("Meteorological / hydrological drought", "Population and water demand", "Water-supply / agricultural stress"),
    "09": ("Extreme weather", "Grid geography and population", "Derived grid-weather stress"),
    "10": ("Drinking-water compliance signals", "Population served", "Community social vulnerability"),
    "11": ("Reachability / routing signals", "Networks and geography", "Derived outage confidence, never cause"),
    "12": ("High-tide / coastal-flood signals", "Coastal population", "Social vulnerability"),
    "13": ("Recent earthquake hazard", "Nearby population", "Social vulnerability / official impact products when available"),
}

CSS = r"""
/* MitchellCo EvacWatch v2 shared UX standard */
.mc-question{font-size:13px;font-weight:700;margin:0 0 10px;color:#fff}.mc-health-card{background:var(--card,#20262a);border:1px solid var(--line,#394148);border-radius:8px;padding:9px;margin:8px 0}.mc-health-row{display:grid;grid-template-columns:1fr auto;gap:8px;padding:3px 0;font-size:11.5px}.mc-pill{font-size:10px;border:1px solid var(--line,#394148);border-radius:999px;padding:1px 6px;white-space:nowrap}.mc-live{color:#b8efc4}.mc-retrying{color:#ffbf3f}.mc-unavailable{color:#ff9c9c}.mc-cached{color:#9ccaff}.mc-standard-details{font-size:11.5px;color:var(--muted,#9eabb3)}.mc-standard-details summary{cursor:pointer;color:var(--text,#f6f8f9)}
""".strip()

RUNTIME = r"""
<script data-mitchellco-runtime="evacwatch-v2">
(() => {
  'use strict';
  if (window.MCMap?.version === 'evacwatch-v2') return;
  const nativeFetch = window.fetch.bind(window);
  const inflight = new Map();
  const sourceState = new Map();
  const friendly = host => ({
    'api.weather.gov':'NWS',
    'waterservices.usgs.gov':'USGS',
    'earthquake.usgs.gov':'USGS Earthquakes',
    'services3.arcgis.com':'ArcGIS',
    'onemap.cdc.gov':'CDC/ATSDR',
    'tigerweb.geo.census.gov':'U.S. Census',
    'api.open-meteo.com':'Open-Meteo',
    'api.tidesandcurrents.noaa.gov':'NOAA CO-OPS'
  }[host] || host);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const retryable = status => [429, 502, 503, 504].includes(status);
  const setState = (url, state, detail='') => {
    try {
      const host = new URL(url, location.href).host;
      if (!host || host === location.host) return;
      sourceState.set(host, {state, detail, time:Date.now()});
      renderHealth();
    } catch (_) {}
  };
  const renderHealth = () => {
    const root = document.getElementById('mc-runtime-health-list');
    if (!root) return;
    const rows = [...sourceState.entries()].sort((a,b)=>a[0].localeCompare(b[0]));
    root.innerHTML = rows.length ? rows.map(([host,s]) => {
      const cls = s.state === 'Live' ? 'mc-live' : s.state === 'Retrying' ? 'mc-retrying' : s.state === 'Cached' ? 'mc-cached' : 'mc-unavailable';
      const age = Math.max(0, Math.floor((Date.now()-s.time)/60000));
      return `<div class="mc-health-row"><span>${friendly(host)}</span><span class="mc-pill ${cls}">${s.state}${s.detail ? ' · '+s.detail : ''}${s.state==='Live' ? ` · ${age<1?'&lt;1':age}m` : ''}</span></div>`;
    }).join('') : '<div class="mc-standard-details">Waiting for live sources…</div>';
  };
  async function resilientFetch(input, init={}) {
    const req = input instanceof Request ? input : new Request(input, init);
    const method = (req.method || 'GET').toUpperCase();
    if (method !== 'GET') return nativeFetch(input, init);
    const url = req.url;
    if (inflight.has(url)) {
      const r = await inflight.get(url);
      return r.clone();
    }
    const task = (async () => {
      let lastErr;
      for (let attempt=0; attempt<3; attempt++) {
        const ctl = new AbortController();
        const timer = setTimeout(() => ctl.abort(), 15000);
        try {
          if (attempt) {
            setState(url, 'Retrying', `attempt ${attempt+1}/3`);
            await sleep(750 * (2 ** (attempt-1)));
          }
          const headers = new Headers(req.headers);
          const r = await nativeFetch(new Request(req, {signal:ctl.signal, headers}));
          clearTimeout(timer);
          if (r.ok) {
            setState(url, 'Live');
            return r;
          }
          lastErr = new Error(`HTTP ${r.status}`);
          if (!retryable(r.status)) {
            setState(url, 'Unavailable', `HTTP ${r.status}`);
            return r;
          }
        } catch (e) {
          clearTimeout(timer);
          lastErr = e;
        }
      }
      setState(url, 'Unavailable', lastErr?.name === 'AbortError' ? 'timeout' : 'request failed');
      throw lastErr || new Error('request failed');
    })();
    inflight.set(url, task);
    try {
      const r = await task;
      return r.clone();
    } finally {
      inflight.delete(url);
    }
  }
  async function openPopup(layer, map, latlng, zoom=8) {
    if (!layer || !map) return;
    map.closePopup();
    const target = typeof layer.getLayers === 'function' && !layer.getPopup?.() ? layer.getLayers().find(x => x.getPopup?.()) : layer;
    if (!target?.openPopup) return;
    const open = () => { try { target.openPopup(); target.bringToFront?.(); } catch (_) {} };
    if (latlng && Number.isFinite(latlng[0]) && Number.isFinite(latlng[1])) {
      map.once('moveend', open);
      map.flyTo(latlng, zoom, {duration:.65});
      setTimeout(open, 900);
    } else open();
  }
  function makeGenerationGuard(state, key='generation') {
    state[key] = Number(state[key] || 0);
    return {
      next(){ state[key] += 1; return state[key]; },
      current(){ return state[key]; },
      valid(token){ return token === state[key]; }
    };
  }
  window.MCMap = {version:'evacwatch-v2', nativeFetch, fetch:resilientFetch, openPopup, makeGenerationGuard, sourceState};
  window.fetch = resilientFetch;
  document.addEventListener('DOMContentLoaded', () => { renderHealth(); setInterval(renderHealth, 30000); });
})();
</script>
""".strip()


def insert_after_opening_aside(text: str, block: str) -> str:
    m = re.search(r"<aside(?:\s[^>]*)?>", text, flags=re.I)
    if m:
        return text[:m.end()] + "\n" + block + "\n" + text[m.end():]
    m = re.search(r"<body(?:\s[^>]*)?>", text, flags=re.I)
    if m:
        return text[:m.end()] + "\n" + block + "\n" + text[m.end():]
    return text


def standard_block(num: str, text: str) -> str:
    parts = []
    if 'class="question"' not in text and 'class="mc-question"' not in text and num in QUESTIONS:
        parts.append(f'<div class="mc-question">{QUESTIONS[num]}</div>')
    if 'id="mc-runtime-health"' not in text and re.search(r">\s*Data health\s*<", text, re.I) is None:
        parts.append('<div class="mc-health-card" id="mc-runtime-health"><b>Data health</b><div id="mc-runtime-health-list"><div class="mc-standard-details">Waiting for live sources…</div></div></div>')
    if 'data-mitchellco-fusion=' not in text and num in FUSION:
        h,e,c = FUSION[num]
        parts.append(
            '<details class="mc-health-card mc-standard-details" data-mitchellco-fusion="hazard-exposure-consequence">'
            '<summary><b>How to read this map</b></summary>'
            f'<div style="margin-top:7px"><b>Hazard / signal:</b> {h}<br><b>Exposure:</b> {e}<br><b>Consequence / vulnerability:</b> {c}<br><br>'
            'Official/source values remain distinct from MitchellCo derived screening scores or estimates. Derived values are for prioritization and interpretation, not official warnings or predictions.</div></details>'
        )
    return "\n".join(parts)


def transform(path: Path) -> tuple[str, bool]:
    text = path.read_text(encoding="utf-8")
    original = text
    if f'content="{STANDARD}"' not in text:
        marker = f'<meta name="mitchellco-map-standard" content="{STANDARD}">'
        if '<meta charset' in text.lower():
            text = re.sub(r'(<meta\s+charset=[^>]+>)', r'\1\n' + marker, text, count=1, flags=re.I)
        else:
            text = text.replace('</head>', marker + '\n</head>', 1)
    if 'MitchellCo EvacWatch v2 shared UX standard' not in text:
        if '</style>' in text:
            text = text.replace('</style>', '\n' + CSS + '\n</style>', 1)
        else:
            text = text.replace('</head>', '<style>\n' + CSS + '\n</style>\n</head>', 1)
    if 'data-mitchellco-runtime="evacwatch-v2"' not in text:
        text = text.replace('</head>', RUNTIME + '\n</head>', 1)
    num = path.name[:2]
    block = standard_block(num, text)
    if block:
        text = insert_after_opening_aside(text, block)
    return text, text != original


def check(path: Path, text: str) -> list[str]:
    errs = []
    if f'content="{STANDARD}"' not in text:
        errs.append('missing map-standard meta')
    if 'data-mitchellco-runtime="evacwatch-v2"' not in text:
        errs.append('missing resilient runtime')
    num = path.name[:2]
    if num in QUESTIONS and 'class="question"' not in text and 'class="mc-question"' not in text:
        errs.append('missing map question')
    if num in FUSION and 'data-mitchellco-fusion="hazard-exposure-consequence"' not in text:
        errs.append('missing fusion explanation')
    if 'googletagmanager.com/gtag/js?id=G-8SVEH8WD1R' not in text:
        errs.append('missing required GA4 tag')
    if '© 2026 MitchellCo Inc.' not in text:
        errs.append('missing MitchellCo copyright')
    # Safeguard from Map 07 bug #1: NWS alerts often have null geometry.
    if 'api.weather.gov/alerts' in text and ('Red Flag Warning' in text or 'Fire Weather Watch' in text):
        if 'affectedZones' not in text:
            errs.append('NWS fire-weather alerts used without affectedZones geometry resolution safeguard')
    # Safeguard from Map 07 bug #2: ranked rows must open the popup-owning layer after movement.
    if re.search(r'rank|highest', text, re.I) and 'openPopup' in text and ('flyTo' in text or 'fitBounds' in text):
        if 'MCMap.openPopup' not in text and 'moveend' not in text:
            errs.append('ranked popup navigation lacks moveend/MCMap.openPopup safeguard')
    # Region changes can leave stale async results unless generation/abort semantics exist.
    if re.search(r'id=["\']region["\']', text, re.I) and 'onchange' in text:
        if not re.search(r'generation|AbortController|requestToken|loadToken', text, re.I):
            errs.append('region selector lacks stale-request generation/abort safeguard')
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    files = sorted(MAP_DIR.glob('[0-9][0-9]-*.html'))
    failures = []
    changed = []
    for path in files:
        if args.check:
            text = path.read_text(encoding='utf-8')
        else:
            text, did = transform(path)
            if did:
                path.write_text(text, encoding='utf-8', newline='\n')
                changed.append(path.name)
        errs = check(path, text)
        if errs:
            failures.append((path.name, errs))
    if changed:
        print('Standardized:', ', '.join(changed))
    if failures:
        for name, errs in failures:
            print(f'{name}:')
            for e in errs:
                print(f'  - {e}')
        return 1
    print(f'Validated {len(files)} daily maps against {STANDARD}.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
