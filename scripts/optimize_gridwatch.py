#!/usr/bin/env python3
"""Idempotently optimize Map 09 for chunked loading and responsive recomputation."""
from pathlib import Path
import re

P = Path(__file__).resolve().parents[1] / "daily-maps/09-power-grid-stress-extreme-weather.html"
s = P.read_text(encoding="utf-8")


def sub(pattern, repl, flags=re.S):
    global s
    new, n = re.subn(pattern, repl, s, count=1, flags=flags)
    if n != 1:
        raise SystemExit(f"GridWatch optimizer expected 1 match, got {n}: {pattern[:70]}")
    s = new

# State: share in-flight requests, abort stale region work, and cache spatial indexes.
sub(r"      const S = \{.*?\n      \};\n      const CONTROL =",
'''      const S = {
        controls: EMPTY,
        counties: EMPTY,
        alerts: EMPTY,
        countyIndex: [],
        alertIndex: [],
        last: {},
        inflight: new Map(),
        controllers: new Set(),
        rank: new Map(),
        generation: 0,
        recomputeToken: 0,
        recomputeTimer: 0,
        lineRegion: -1,
        forecastCache: new Map(),
        ready: { controls: false, counties: false, alerts: false },
      };
      const CONTROL =''')

# Replace cache/fetch/query helpers. Capture region namespace when a request starts,
# share identical in-flight requests, and use 25s network timeouts. Chunking is 2x2
# for heavy polygon layers with max three concurrent requests.
sub(r"      function cacheKey\(key\) \{.*?\n      async function loadControls\(gen = S\.generation\) \{",
'''      function regionKey() {
        return `${$("region").value || 0}:${region()[0]}`;
      }
      function cacheKey(key, ns = regionKey()) {
        return "gridwatch:v2:" + ns + ":" + key;
      }
      function health(id, state, detail = "") {
        const e = $(id);
        e.textContent = state + (detail ? " · " + detail : "");
        e.className =
          "health " +
          ({ Live: "live", Cached: "cached", Loading: "loading", Retrying: "loading", Unavailable: "error" }[state] || "");
      }
      async function fetchJSON(
        url,
        key,
        { timeout = 25000, retries = 2, cache = true, ttl = 0, gen = S.generation } = {},
      ) {
        const ns = regionKey();
        const requestKey = `${ns}|${key}|${url}`;
        if (S.inflight.has(requestKey)) return S.inflight.get(requestKey);
        const task = (async () => {
          if (cache && ttl) {
            try {
              const c = JSON.parse(localStorage.getItem(cacheKey(key, ns)) || "null");
              if (c?.data && Date.now() - c.time < ttl) {
                S.last[key] = { time: c.time, mode: "Cached" };
                return c.data;
              }
            } catch {}
          }
          let lastError;
          for (let i = 0; i <= retries; i++) {
            if (gen !== S.generation) throw new DOMException("stale region", "AbortError");
            const ctl = new AbortController();
            S.controllers.add(ctl);
            const timer = setTimeout(() => ctl.abort(), timeout);
            try {
              if (i) await sleep(700 * 2 ** (i - 1));
              const r = await fetch(url, {
                headers: { Accept: "application/geo+json, application/json" },
                signal: ctl.signal,
              });
              if (!r.ok) throw new Error("HTTP " + r.status);
              const j = await r.json();
              if (j?.error) throw new Error(j.error.message || "service error");
              if (gen !== S.generation) throw new DOMException("stale region", "AbortError");
              const now = Date.now();
              S.last[key] = { time: now, mode: "Live" };
              if (cache) {
                try { localStorage.setItem(cacheKey(key, ns), JSON.stringify({ time: now, data: j })); } catch {}
              }
              return j;
            } catch (e) {
              lastError = e;
              if (e?.name === "AbortError" || gen !== S.generation) throw e;
              if (i >= retries) break;
            } finally {
              clearTimeout(timer);
              S.controllers.delete(ctl);
            }
          }
          if (cache) {
            try {
              const c = JSON.parse(localStorage.getItem(cacheKey(key, ns)) || "null");
              if (c?.data) {
                S.last[key] = { time: c.time, mode: "Cached" };
                return c.data;
              }
            } catch {}
          }
          throw lastError || new Error("live and cached data unavailable");
        })();
        S.inflight.set(requestKey, task);
        try { return await task; }
        finally { S.inflight.delete(requestKey); }
      }
      function qurl(base, fields, extra = {}, bounds = box()) {
        const geometry = JSON.stringify({
          xmin: bounds.w, ymin: bounds.s, xmax: bounds.e, ymax: bounds.n,
          spatialReference: { wkid: 4326 },
        });
        return base + "?" + new URLSearchParams({
          where: "1=1", outFields: fields, returnGeometry: "true", outSR: "4326",
          geometry, geometryType: "esriGeometryEnvelope",
          spatialRel: "esriSpatialRelIntersects", geometryPrecision: "4", f: "geojson", ...extra,
        });
      }
      function tiles2x2() {
        const b = box(), midLat = (b.s + b.n) / 2, midLon = (b.w + b.e) / 2;
        return [
          { s: b.s, w: b.w, n: midLat, e: midLon },
          { s: b.s, w: midLon, n: midLat, e: b.e },
          { s: midLat, w: b.w, n: b.n, e: midLon },
          { s: midLat, w: midLon, n: b.n, e: b.e },
        ];
      }
      async function mapLimit(items, limit, fn) {
        const out = new Array(items.length); let next = 0;
        async function worker() {
          while (next < items.length) {
            const i = next++;
            out[i] = await fn(items[i], i);
            await sleep0();
          }
        }
        await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
        return out;
      }
      function mergeFeatures(parts, idField) {
        const seen = new Set(), features = [];
        for (const p of parts) for (const f of p?.features || []) {
          const id = String(f.properties?.[idField] ?? f.properties?.OBJECTID ?? JSON.stringify(f.geometry).slice(0, 120));
          if (!seen.has(id)) { seen.add(id); features.push(f); }
        }
        return { type: "FeatureCollection", features };
      }
      async function chunkedArcGIS(base, fields, key, extra, idField, gen) {
        const tiles = tiles2x2();
        health(key === "controls" ? "gridStatus" : "countyStatus", "Loading", `0/${tiles.length} chunks`);
        let done = 0;
        const parts = await mapLimit(tiles, 3, async (tile, i) => {
          const j = await fetchJSON(qurl(base, fields, extra, tile), `${key}-chunk-${i}`, {
            timeout: 25000, retries: 1, cache: true, ttl: 86400000, gen,
          });
          done++;
          if (gen === S.generation)
            health(key === "controls" ? "gridStatus" : "countyStatus", "Loading", `${done}/${tiles.length} chunks`);
          return j;
        });
        return mergeFeatures(parts, idField);
      }
      async function loadControls(gen = S.generation) {''')

# Chunk controls and counties. Build county representative points once instead of
# recomputing pointOnFeature inside every control-area loop.
sub(r"      async function loadControls\(gen = S\.generation\) \{.*?\n      function inBoxFeature\(f\) \{",
'''      async function loadControls(gen = S.generation) {
        health("gridStatus", "Loading", "chunked");
        try {
          const j = await chunkedArcGIS(
            CONTROL,
            "ID,NAME,STATE,YEAR,AVAIL_CAP,TOTAL_CAP,PEAK_LOAD,MIN_LOAD,SOURCEDATE",
            "controls",
            { maxAllowableOffset: "0.02" },
            "ID",
            gen,
          );
          if (gen !== S.generation) return;
          S.controls = j;
          S.ready.controls = true;
          controlLayer.clearLayers().addData(j);
          health("gridStatus", "Live", `${j.features.length} areas · 4 chunks · legacy baseline`);
          scheduleRecompute();
        } catch (e) {
          if (gen === S.generation) health("gridStatus", "Unavailable");
        }
      }
      async function loadCounties(gen = S.generation) {
        health("countyStatus", "Loading", "chunked");
        try {
          const j = await chunkedArcGIS(
            CDC,
            "STCNTY,COUNTY,ST_ABBR,E_TOTPOP,RPL_THEMES",
            "counties",
            { maxAllowableOffset: "0.012" },
            "STCNTY",
            gen,
          );
          if (gen !== S.generation) return;
          S.counties = j;
          S.countyIndex = [];
          let i = 0;
          for (const c of j.features) {
            try {
              const pt = turf.pointOnFeature(c), p = c.properties || {}, bb = turf.bbox(c);
              S.countyIndex.push({
                pt,
                bb,
                pop: Math.max(0, num(p.E_TOTPOP) || 0),
                svi: Math.max(0, Math.min(1, num(p.RPL_THEMES) || 0)),
              });
            } catch {}
            if (++i % 80 === 0) await sleep0();
          }
          S.ready.counties = true;
          health("countyStatus", "Live", `${j.features.length} counties · indexed once · 2022`);
          scheduleRecompute();
        } catch (e) {
          if (gen === S.generation) health("countyStatus", "Unavailable");
        }
      }
      function inBoxFeature(f) {''')

# Resolve NWS zones in small concurrent batches and dedupe zone URLs before fetching.
sub(r"      async function resolveAlertFeature\(alert, gen\) \{.*?\n      async function loadLines\(force = false\) \{",
'''      async function resolveAlertFeature(alert, gen) {
        const p = alert.properties || {};
        if (alert.geometry && inBoxFeature(alert)) return [alert];
        const urls = [...new Set((p.affectedZones || []).filter(zoneInRegion))];
        if (!urls.length) return [];
        const got = await mapLimit(urls, 4, async (u) => {
          if (gen !== S.generation) return null;
          const id = zoneId(u);
          try {
            const z = await fetchJSON(u, `zone-${id}`, { timeout: 20000, retries: 1, cache: true, ttl: 86400000, gen });
            if (!z?.geometry) return null;
            const f = { type: "Feature", geometry: z.geometry, properties: { ...p, zoneId: id, zoneName: z.properties?.name || id } };
            return inBoxFeature(f) ? f : null;
          } catch { return null; }
        });
        return got.filter(Boolean);
      }
      async function loadAlerts(gen = S.generation) {
        health("alertStatus", "Loading");
        try {
          const j = await fetchJSON(ALERTS, "alerts", { timeout: 25000, retries: 1, cache: true, ttl: 120000, gen });
          if (gen !== S.generation || !j) return;
          const local = (j.features || []).filter((f) =>
            WX_WEIGHTS[f.properties?.event] &&
            (f.geometry ? inBoxFeature(f) : (f.properties?.affectedZones || []).some(zoneInRegion)),
          );
          health("alertStatus", "Loading", `${local.length} products · resolving zones`);
          const parts = await mapLimit(local, 3, (a) => resolveAlertFeature(a, gen));
          if (gen !== S.generation) return;
          const resolved = parts.flat();
          S.alerts = { type: "FeatureCollection", features: resolved };
          S.alertIndex = resolved.map((f) => { try { return { f, bb: turf.bbox(f) }; } catch { return null; } }).filter(Boolean);
          S.ready.alerts = true;
          alertLayer.clearLayers().addData(S.alerts);
          health("alertStatus", S.last.alerts?.mode || "Live", `${local.length} products · ${resolved.length} mapped zones`);
          scheduleRecompute();
        } catch (e) {
          if (gen === S.generation) health("alertStatus", "Unavailable");
        }
      }
      async function loadLines(force = false) {''')

# Use bbox prefilters + precomputed county points. These were the dominant repeated
# Turf operations in the original O(control areas x counties) recomputes.
sub(r"      function weatherForArea\(area\) \{.*?\n      function areaPopup\(rec\) \{",
'''      function bboxIntersects(a, b) {
        return !(a[2] < b[0] || a[0] > b[2] || a[3] < b[1] || a[1] > b[3]);
      }
      function weatherForArea(area, areaBB) {
        let best = 0, label = "No mapped extreme-weather alert at area";
        for (const x of S.alertIndex) {
          if (!bboxIntersects(areaBB, x.bb)) continue;
          try {
            if (turf.booleanIntersects(area, x.f)) {
              const ev = x.f.properties?.event || "", w = WX_WEIGHTS[ev] || 0;
              if (w > best) { best = w; label = ev; }
            }
          } catch {}
        }
        return { points: best, label };
      }
      function marginPts(m) {
        if (m == null) return 8;
        if (m <= 0) return 35;
        if (m <= 0.05) return 30;
        if (m <= 0.1) return 24;
        if (m <= 0.2) return 16;
        if (m <= 0.3) return 8;
        return 3;
      }
      function bar(label, v, max) {
        const pct = Math.min(100, (100 * v) / max);
        return `${label}: <b>${v.toFixed(0)}/${max}</b><div class="bar"><i style="width:${pct}%"></i></div>`;
      }
      function countiesForArea(area, areaBB) {
        let pop = 0, weighted = 0;
        for (const c of S.countyIndex) {
          if (!bboxIntersects(areaBB, c.bb)) continue;
          try { if (!turf.booleanPointInPolygon(c.pt, area)) continue; } catch { continue; }
          pop += c.pop;
          weighted += c.pop * c.svi;
        }
        return { pop, svi: pop ? weighted / pop : 0 };
      }
      function areaPopup(rec) {''')

# One responsive, debounced scoring pass. Yield every three areas instead of every
# twelve, and don't run expensive scoring until control + county layers are ready.
sub(r"      async function recompute\(token\) \{.*?\n      function render\(rows\) \{",
'''      async function recompute(token) {
        if (token !== S.recomputeToken || !S.ready.controls || !S.ready.counties || !S.controls.features?.length) return;
        stressLayer.clearLayers();
        S.rank.clear();
        const rows = [];
        let i = 0;
        for (const f of S.controls.features) {
          if (token !== S.recomputeToken) return;
          let areaBB;
          try { areaBB = turf.bbox(f); } catch { continue; }
          const p = f.properties || {}, key = String(p.ID || p.OBJECTID_1 || i), name = p.NAME || `Control area ${key}`,
            avail = num(p.AVAIL_CAP), peak = num(p.PEAK_LOAD),
            margin = avail != null && peak != null && peak > 0 ? (avail - peak) / peak : null,
            marginScore = marginPts(margin), wx = weatherForArea(f, areaBB), exp = countiesForArea(f, areaBB),
            exposure = Math.min(15, exp.pop ? 2.5 * Math.log10(exp.pop) : 0), vuln = 10 * exp.svi,
            score = Math.min(100, wx.points + marginScore + exposure + vuln);
          let pt;
          try { pt = turf.pointOnFeature(f); } catch { continue; }
          const rec = { key, name, avail, peak, year: p.YEAR, margin, marginScore, weather: wx.points, wxLabel: wx.label,
            pop: exp.pop, svi: exp.svi, exposure, vuln, score, lat: pt.geometry.coordinates[1], lon: pt.geometry.coordinates[0], feature: f };
          const fill = score >= 75 ? "#e3342f" : score >= 55 ? "#f27532" : score >= 35 ? "#f2c94c" : "#5aa469";
          const group = L.geoJSON(f, { style: { color: fill, weight: 1.3, fillColor: fill, fillOpacity: 0.18 } }).addTo(stressLayer),
            layer = group.getLayers()[0];
          if (layer) {
            layer.bindPopup(() => areaPopup(rec));
            layer.on("popupopen", () => loadForecast(rec));
            rec.popupLayer = layer;
          }
          S.rank.set(key, rec); rows.push(rec);
          if (++i % 3 === 0) await new Promise((resolve) => requestAnimationFrame(resolve));
        }
        rows.sort((a, b) => b.score - a.score);
        render(rows.slice(0, 15));
      }
      function scheduleRecompute() {
        clearTimeout(S.recomputeTimer);
        const t = ++S.recomputeToken;
        S.recomputeTimer = setTimeout(() => {
          const run = () => recompute(t);
          if ("requestIdleCallback" in window) requestIdleCallback(run, { timeout: 1200 });
          else setTimeout(run, 0);
        }, 260);
      }
      function render(rows) {''')

# Abort stale requests on region change and launch independent sources concurrently.
sub(r"      function clearRegion\(\) \{.*?\n      function toggle\(id, l\) \{",
'''      function clearRegion() {
        S.generation++;
        S.recomputeToken++;
        clearTimeout(S.recomputeTimer);
        for (const ctl of S.controllers) ctl.abort();
        S.controllers.clear();
        S.inflight.clear();
        S.controls = EMPTY; S.counties = EMPTY; S.alerts = EMPTY;
        S.countyIndex = []; S.alertIndex = [];
        S.ready = { controls: false, counties: false, alerts: false };
        S.rank.clear(); S.lineRegion = -1;
        map.closePopup();
        controlLayer.clearLayers(); alertLayer.clearLayers(); stressLayer.clearLayers(); lineLayer.clearLayers();
        $("rankBody").innerHTML = '<tr><td colspan="4" class="small">Loading selected region in parallel…</td></tr>';
        health("alertStatus", "Loading", "queued"); health("gridStatus", "Loading", "queued"); health("countyStatus", "Loading", "queued");
        health("lineStatus", $("tLines").checked ? "Loading" : "Off");
      }
      function loadRegion() {
        clearRegion();
        const b = box(), g = S.generation;
        map.fitBounds([[b.s, b.w], [b.n, b.e]], { padding: [8, 8] });
        // These calls are intentionally not awaited. Each has independent progress,
        // cancellation and caching, so the map remains interactive while data arrives.
        void loadControls(g);
        void loadCounties(g);
        void loadAlerts(g);
        if ($("tLines").checked) setTimeout(() => { if (g === S.generation) void loadLines(true); }, 350);
      }
      function toggle(id, l) {''')

# User-facing reliability explanation.
s = s.replace(
    "Live requests use timeouts, bounded spatial queries, browser\n            caching, overlap protection and retry/backoff. Scheduled NWS refresh",
    "Heavy ArcGIS polygon layers load as four bounded chunks with at most three requests in flight. Independent sources load in parallel, stale requests are aborted on region changes, and scoring yields to the browser every few control areas. API timeouts are 25 seconds with retry/backoff. Scheduled NWS refresh",
)

P.write_text(s, encoding="utf-8", newline="\n")
print("Optimized GridWatch Map 09")
