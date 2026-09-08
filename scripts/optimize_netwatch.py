from pathlib import Path

PATH = Path('daily-maps/11-internet-outage-watch.html')
text = PATH.read_text(encoding='utf-8')

# Idempotence marker.
if 'data-netwatch-performance="v2"' in text:
    print('NetWatch v2 already applied.')
    raise SystemExit(0)

text = text.replace(
    '<meta name="mitchellco-map-standard" content="evacwatch-v2" />',
    '<meta name="mitchellco-map-standard" content="evacwatch-v2" />\n    <meta data-netwatch-performance="v2" content="chunked-ripe-throttled-bgp" />',
    1,
)

text = text.replace(
    '<div class="metric"><b id="bgp5">–</b><span>BGP msgs / 5m</span></div>',
    '<div class="metric"><b id="bgp5">–</b><span>BGP UPDATE msgs / 5m</span></div>',
    1,
)

text = text.replace(
    'keeps only a rolling <b>5-minute message count</b> as routing\n            context and reconnects with exponential backoff.',
    'keeps only a rolling <b>5-minute UPDATE-message count</b> as routing\n            context. The displayed count refreshes at most every <b>10 seconds</b>\n            so the live stream does not cause UI churn, and reconnects with\n            exponential backoff.',
    1,
)

text = text.replace(
    '        bgp: [],\n        ws: null,',
    '        bgp: [],\n        bgpHead: 0,\n        bgpUiTimer: null,\n        ws: null,',
    1,
)

text = text.replace(
    '      async function getJSON(url, gen) {\n        const ctl = new AbortController(),\n          timer = setTimeout(() => ctl.abort(), 12000);',
    '      async function getJSON(url, gen) {\n        const ctl = new AbortController(),\n          timer = setTimeout(() => ctl.abort(), 25000);',
    1,
)

old_probe = '''      function probeURL(status, b, page = 1) {
        const q = new URLSearchParams({
          status: String(status),
          latitude__gte: b.s,
          latitude__lte: b.n,
          longitude__gte: b.w,
          longitude__lte: b.e,
          page_size: "500",
          page: String(page),
          fields:
            "id,geometry,status,status_since,last_connected,asn_v4,asn_v6,is_anchor,total_uptime",
        });
        return "https://atlas.ripe.net/api/v2/probes/?" + q;
      }
      async function loadStatus(status, b, gen) {
        let url = probeURL(status, b, 1),
          out = [],
          pages = 0;
        while (url && pages < 4) {
          const j = await getJSON(url, gen);
          if (!j) return null;
          out.push(...(j.results || []));
          url = j.next;
          pages++;
        }
        return out;
      }
'''
new_probe = '''      function probeURL(status, b, page = 1) {
        const q = new URLSearchParams({
          status: String(status),
          latitude__gte: b.s,
          latitude__lte: b.n,
          longitude__gte: b.w,
          longitude__lte: b.e,
          page_size: "300",
          page: String(page),
          fields:
            "id,geometry,status,status_since,last_connected,asn_v4,asn_v6,is_anchor,total_uptime",
        });
        return "https://atlas.ripe.net/api/v2/probes/?" + q;
      }
      function splitProbeBox(b) {
        const my = (b.s + b.n) / 2,
          mx = (b.w + b.e) / 2;
        return [
          { s: b.s, w: b.w, n: my, e: mx },
          { s: b.s, w: mx, n: my, e: b.e },
          { s: my, w: b.w, n: b.n, e: mx },
          { s: my, w: mx, n: b.n, e: b.e },
        ];
      }
      async function loadProbeChunk(status, b, gen) {
        let url = probeURL(status, b, 1),
          out = [],
          pages = 0;
        while (url && pages < 4) {
          const j = await getJSON(url, gen);
          if (!j) return null;
          out.push(...(j.results || []));
          url = j.next;
          pages++;
          await (window.MCMap?.yieldControl?.() || Promise.resolve());
        }
        return out;
      }
      async function loadStatus(status, b, gen) {
        const chunks = splitProbeBox(b),
          parts = await (window.MCMap?.mapLimit
            ? window.MCMap.mapLimit(chunks, 2, (chunk) =>
                loadProbeChunk(status, chunk, gen),
              )
            : Promise.all(chunks.map((chunk) => loadProbeChunk(status, chunk, gen))));
        if (gen !== S.gen || parts.some((x) => !x)) return null;
        const seen = new Set(),
          out = [];
        for (const part of parts) {
          for (const p of part || []) {
            if (seen.has(p.id)) continue;
            seen.add(p.id);
            out.push(p);
          }
          await (window.MCMap?.yieldControl?.() || Promise.resolve());
        }
        return out;
      }
'''
if old_probe not in text:
    raise SystemExit('Probe loader pattern not found; refusing partial patch.')
text = text.replace(old_probe, new_probe, 1)

text = text.replace(
    '''      async function loadProbes() {
        if (S.busy) return;
        S.busy = true;
        const gen = ++S.gen,
          b = box();
        h("probeStatus", "Loading");''',
    '''      async function loadProbes() {
        const gen = ++S.gen,
          b = box();
        h("probeStatus", "Loading", "4 spatial chunks");''',
    1,
)
text = text.replace(
    '''        } finally {
          S.busy = false;
        }
      }
      function pt(p) {''',
    '''        }
      }
      function pt(p) {''',
    1,
)

old_ws = '''        ws.onmessage = (e) => {
          try {
            const m = JSON.parse(e.data);
            if (m.type === "ris_message") {
              const now = Date.now();
              S.bgp.push(now);
              S.bgp = S.bgp.filter((t) => now - t <= 300000);
              $("bgp5").textContent = S.bgp.length.toLocaleString();
            }
          } catch {}
        };
'''
new_ws = '''        ws.onmessage = (e) => {
          try {
            const m = JSON.parse(e.data);
            if (m.type === "ris_message") S.bgp.push(Date.now());
          } catch {}
        };
'''
if old_ws not in text:
    raise SystemExit('RIS message pattern not found; refusing partial patch.')
text = text.replace(old_ws, new_ws, 1)

insert_before = '      function connectRIS() {'
bgp_helper = '''      function refreshBGPMetric() {
        const now = Date.now(),
          cutoff = now - 300000;
        while (S.bgpHead < S.bgp.length && S.bgp[S.bgpHead] < cutoff)
          S.bgpHead++;
        const count = S.bgp.length - S.bgpHead;
        $("bgp5").textContent = count.toLocaleString();
        if (S.bgpHead > 5000 && S.bgpHead > S.bgp.length / 2) {
          S.bgp = S.bgp.slice(S.bgpHead);
          S.bgpHead = 0;
        }
      }
      function startBGPMetricTimer() {
        if (S.bgpUiTimer) return;
        refreshBGPMetric();
        S.bgpUiTimer = setInterval(() => {
          if (!document.hidden) refreshBGPMetric();
        }, 10000);
      }
'''
if insert_before not in text:
    raise SystemExit('connectRIS insertion point not found.')
text = text.replace(insert_before, bgp_helper + insert_before, 1)

text = text.replace(
    '          h("bgpStatus", "Live", "rolling 5m");',
    '          h("bgpStatus", "Live", "rolling 5m · UI 10s");\n          startBGPMetricTimer();',
    1,
)

text = text.replace(
    '      loadProbes();\n      connectRIS();',
    '      loadProbes();\n      startBGPMetricTimer();\n      connectRIS();',
    1,
)

PATH.write_text(text, encoding='utf-8', newline='\n')
print('Applied NetWatch v2: chunked RIPE Atlas loading, stale-load fix, 25s timeout, and 10s BGP UI throttling.')
