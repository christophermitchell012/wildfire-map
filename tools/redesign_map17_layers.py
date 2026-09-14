#!/usr/bin/env python3
"""Redesign Map 17 around regional county polygons and distinct thematic layers.

Build-time responsibilities:
- Download the authoritative Census 2025 1:20m county cartographic boundaries.
- Split them into the four Census regions and publish small same-origin GeoJSON files.
- Patch Map 17 to cache county geometry per region in localStorage.
- Replace stacked circle markers with county choropleths + a selective priority outline.

No ArcGIS/Esri runtime or build-time dependency is used.
"""
from __future__ import annotations

import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path

import shapefile

TARGET = Path("daily-maps/17-agricultural-drought-farm-exposure.html")
DATA_DIR = Path("daily-maps/data")
COUNTY_ZIP = "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_county_20m.zip"
GEOM_VERSION = "2025-cb20m-v1"
UA = "MitchellCo-DailyMap/1.0 (+https://christophermitchell012.github.io/wildfire-map/)"

REGIONS = {
    "northeast": {"CT", "ME", "MA", "NH", "RI", "VT", "NJ", "NY", "PA"},
    "midwest": {"IN", "IL", "MI", "OH", "WI", "IA", "KS", "MN", "MO", "NE", "ND", "SD"},
    "south": {"DE", "DC", "FL", "GA", "MD", "NC", "SC", "VA", "WV", "AL", "KY", "MS", "TN", "AR", "LA", "OK", "TX"},
    "west": {"AZ", "CO", "ID", "MT", "NV", "NM", "UT", "WY", "AK", "CA", "HI", "OR", "WA"},
}
STATEFP_TO_ABBR = {
    "01":"AL","02":"AK","04":"AZ","05":"AR","06":"CA","08":"CO","09":"CT","10":"DE","11":"DC","12":"FL",
    "13":"GA","15":"HI","16":"ID","17":"IL","18":"IN","19":"IA","20":"KS","21":"KY","22":"LA","23":"ME",
    "24":"MD","25":"MA","26":"MI","27":"MN","28":"MS","29":"MO","30":"MT","31":"NE","32":"NV","33":"NH",
    "34":"NJ","35":"NM","36":"NY","37":"NC","38":"ND","39":"OH","40":"OK","41":"OR","42":"PA","44":"RI",
    "45":"SC","46":"SD","47":"TN","48":"TX","49":"UT","50":"VT","51":"VA","53":"WA","54":"WV","55":"WI","56":"WY",
}


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read()


def region_for(state: str) -> str | None:
    for name, states in REGIONS.items():
        if state in states:
            return name
    return None


def build_region_geojson() -> dict[str, int]:
    raw = get(COUNTY_ZIP)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        shp_name = next(n for n in zf.namelist() if n.lower().endswith(".shp"))
        base = shp_name[:-4]
        shp = io.BytesIO(zf.read(base + ".shp"))
        shx = io.BytesIO(zf.read(base + ".shx"))
        dbf = io.BytesIO(zf.read(base + ".dbf"))
        reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf)
        field_names = [f[0] for f in reader.fields[1:]]
        buckets: dict[str, list[dict]] = {r: [] for r in REGIONS}
        for sr in reader.iterShapeRecords():
            attrs = dict(zip(field_names, sr.record))
            fips = str(attrs.get("GEOID", "")).zfill(5)
            st = STATEFP_TO_ABBR.get(str(attrs.get("STATEFP", "")).zfill(2))
            reg = region_for(st or "")
            if not reg or not re.fullmatch(r"\d{5}", fips):
                continue
            geom = sr.shape.__geo_interface__
            buckets[reg].append({
                "type": "Feature",
                "properties": {
                    "fips": fips,
                    "name": str(attrs.get("NAME", fips)),
                    "state": st,
                },
                "geometry": geom,
            })

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    counts = {}
    for reg, features in buckets.items():
        if len(features) < 100:
            raise RuntimeError(f"Suspicious county coverage for {reg}: {len(features)}")
        payload = {
            "type": "FeatureCollection",
            "version": GEOM_VERSION,
            "region": reg,
            "source": "U.S. Census Bureau 2025 Cartographic Boundary Files, counties, 1:20,000,000",
            "features": features,
        }
        path = DATA_DIR / f"county-geometry-{GEOM_VERSION}-{reg}.geojson"
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        counts[reg] = len(features)
    return counts


def literal_sub(pattern: re.Pattern[str], replacement: str, text: str) -> str:
    return pattern.sub(lambda _m: replacement, text, count=1)


def patch_html() -> None:
    text = TARGET.read_text(encoding="utf-8")

    # Region selector in the persistent header.
    if 'id="region"' not in text:
        text = text.replace(
            '<button id="refresh">Refresh</button>',
            '<label class="region-control">Region <select id="region" aria-label="Map region">'
            '<option value="northeast">Northeast</option>'
            '<option value="midwest">Midwest</option>'
            '<option value="south" selected>South</option>'
            '<option value="west">West</option>'
            '</select></label><button id="refresh">Refresh</button>',
            1,
        )

    # Minimal select styling, matching the existing UI.
    if ".region-control" not in text:
        text = text.replace(
            "      button {\n        cursor: pointer;",
            "      button, select {\n        color: var(--text);\n        background: var(--card);\n        border: 1px solid var(--line);\n        border-radius: 6px;\n        padding: 6px 9px;\n      }\n      button {\n        cursor: pointer;",
            1,
        )
        text = text.replace(
            "      main {",
            "      .region-control { display:flex; align-items:center; gap:6px; color:var(--muted); font-size:11px; }\n      main {",
            1,
        )

    old_layers = re.compile(
        r"const sharedRenderer = L\.canvas\(\{ padding: 0\.5 \}\);\s*"
        r"const droughtLayer = L\.layerGroup\(\)\.addTo\(map\),\s*"
        r"agLayer = L\.layerGroup\(\)\.addTo\(map\),\s*"
        r"scoreLayer = L\.layerGroup\(\)\.addTo\(map\);"
    )
    new_layers = '''const sharedRenderer = L.canvas({ padding: 0.5 });
        const boundaryLayer = L.geoJSON(null, { renderer: sharedRenderer, interactive: false }).addTo(map);
        const droughtLayer = L.geoJSON(null, { renderer: sharedRenderer }).addTo(map);
        const agLayer = L.geoJSON(null, { renderer: sharedRenderer });
        const sviLayer = L.geoJSON(null, { renderer: sharedRenderer });
        const scoreLayer = L.geoJSON(null, { renderer: sharedRenderer }).addTo(map);
        const layerControl = L.control.layers(null, {
          "County boundaries": boundaryLayer,
          "Drought severity (USDM)": droughtLayer,
          "Agricultural exposure": agLayer,
          "Community vulnerability (CDC SVI)": sviLayer,
          "Highest combined priority": scoreLayer,
        }, { collapsed: false }).addTo(map);'''
    if old_layers.search(text):
        text = literal_sub(old_layers, new_layers, text)
    elif "const boundaryLayer = L.geoJSON" not in text:
        raise RuntimeError("Could not locate Map 17 layer declarations")

    # Add regional geometry/cache system and a later render() declaration. The
    # later declaration intentionally replaces the old circle-based render().
    injection_marker = "        async function refresh() {"
    if "const COUNTY_GEOMETRY_VERSION" not in text:
        regional_code = r'''        const COUNTY_GEOMETRY_VERSION = "2025-cb20m-v1";
        const REGION_LABELS = Object.freeze({ northeast: "Northeast", midwest: "Midwest", south: "South", west: "West" });
        let activeRegionGeoJSON = null;
        const geometryCacheKey = (region) => `mitchellco:county-geometry:${COUNTY_GEOMETRY_VERSION}:${region}`;
        const geometryURL = (region) => `data/county-geometry-${COUNTY_GEOMETRY_VERSION}-${region}.geojson`;
        function validRegionGeometry(payload, region) {
          return Boolean(payload && payload.type === "FeatureCollection" && payload.version === COUNTY_GEOMETRY_VERSION && payload.region === region && Array.isArray(payload.features) && payload.features.length >= 100 && payload.features.slice(0, 12).every((f) => /^\d{5}$/.test(String(f?.properties?.fips || "")) && ["Polygon", "MultiPolygon"].includes(f?.geometry?.type)));
        }
        function readRegionGeometry(region) {
          try {
            const raw = localStorage.getItem(geometryCacheKey(region));
            if (!raw) return null;
            const parsed = JSON.parse(raw);
            if (!validRegionGeometry(parsed, region)) {
              localStorage.removeItem(geometryCacheKey(region));
              return null;
            }
            return parsed;
          } catch (_err) { return null; }
        }
        function writeRegionGeometry(region, payload) {
          try { localStorage.setItem(geometryCacheKey(region), JSON.stringify(payload)); } catch (_err) {}
        }
        async function loadRegionGeometry(region, fit = true) {
          let payload = readRegionGeometry(region);
          if (!payload) {
            const response = await fetch(geometryURL(region), { cache: "force-cache" });
            if (!response.ok) throw new Error(`county geometry HTTP ${response.status}`);
            payload = await response.json();
            if (!validRegionGeometry(payload, region)) throw new Error("county geometry failed validation");
            writeRegionGeometry(region, payload);
          }
          activeRegionGeoJSON = payload;
          await render();
          if (fit) {
            const bounds = boundaryLayer.getBounds();
            if (bounds.isValid()) map.fitBounds(bounds, { padding: [12, 12] });
          }
        }
        function agColor(v) {
          if (v === null) return "#525b61";
          return v >= .8 ? "#5e2b00" : v >= .6 ? "#9a4b00" : v >= .4 ? "#cf7900" : v >= .2 ? "#f3b33d" : "#ffe5a3";
        }
        function sviColor(v) {
          if (v === null) return "#525b61";
          return v >= .8 ? "#5b1a7a" : v >= .6 ? "#8438a6" : v >= .4 ? "#aa68c7" : v >= .2 ? "#cf9fe0" : "#ead8f2";
        }
        function countyMetrics(fips) {
          const d = state.drought.get(fips) || null;
          const ag = clamp01(USDA_AG_EXPOSURE[fips]);
          const rawSvi = CDC_SVI[fips];
          const svi = rawSvi === undefined || Number(rawSvi) === -999 ? null : clamp01(rawSvi);
          const intensity = d ? droughtIntensity(d) : null;
          const score = combinedScore([
            { weight: .50, value: intensity },
            { weight: .35, value: ag },
            { weight: .15, value: svi },
          ]);
          return { d, ag, svi, intensity, score };
        }
        function countyPopup(feature) {
          const p = feature.properties || {};
          const m = countyMetrics(String(p.fips || ""));
          const cls = m.d ? worstClass(m.d) : -1;
          const partial = m.svi === null ? " <span class=\"small\">(SVI unavailable; available weights renormalized)</span>" : "";
          return `<b>${p.name || p.fips}, ${p.state || ""}</b><br>` +
            `Drought: <b>${m.d ? (cls >= 0 ? "D" + cls : "None") : "unavailable"}</b>${m.d ? ` · intensity ${(100*m.intensity).toFixed(0)}/100` : ""}<br>` +
            `Agricultural exposure: <b>${m.ag === null ? "unavailable" : (100*m.ag).toFixed(0) + "/100"}</b><br>` +
            `CDC/ATSDR SVI: <b>${m.svi === null ? "unavailable" : (100*m.svi).toFixed(0) + "th percentile"}</b><br>` +
            `Combined priority: <b>${m.score === null ? "unavailable" : m.score + "/100"}</b>${partial}`;
        }
        function bindCounty(feature, layer) {
          layer.bindPopup(() => countyPopup(feature));
          layer.on("mouseover", () => layer.setStyle({ weight: 2, color: "#ffffff" }));
          layer.on("mouseout", () => layer.setStyle(layer.options.__baseStyle || {}));
        }
        async function render() {
          if (!activeRegionGeoJSON) return;
          boundaryLayer.clearLayers(); droughtLayer.clearLayers(); agLayer.clearLayers(); sviLayer.clearLayers(); scoreLayer.clearLayers();
          state.markers.clear();
          const scored = [];
          for (const f of activeRegionGeoJSON.features) {
            const fips = String(f?.properties?.fips || "");
            const m = countyMetrics(fips);
            if (m.score !== null && m.d) scored.push({ fips, score: m.score, feature: f, metrics: m });
          }
          scored.sort((a,b) => b.score - a.score);
          const cutoffIndex = Math.max(0, Math.ceil(scored.length * .15) - 1);
          const priorityCutoff = scored.length ? scored[cutoffIndex].score : 101;
          const common = { onEachFeature: bindCounty };
          boundaryLayer.addData(activeRegionGeoJSON);
          boundaryLayer.setStyle({ color: "#8a949b", weight: .65, opacity: .75, fillOpacity: 0 });
          droughtLayer.options.onEachFeature = common.onEachFeature;
          droughtLayer.addData(activeRegionGeoJSON);
          droughtLayer.eachLayer((layer) => {
            const fips = String(layer.feature?.properties?.fips || ""), m = countyMetrics(fips), cls = m.d ? worstClass(m.d) : -1;
            const s = { color: "#31383d", weight: .55, opacity: .85, fillColor: m.d ? (cls >= 0 ? colorClass(cls) : "#d8dee2") : "#525b61", fillOpacity: m.d ? .72 : .18 };
            layer.options.__baseStyle = s; layer.setStyle(s);
          });
          agLayer.options.onEachFeature = common.onEachFeature;
          agLayer.addData(activeRegionGeoJSON);
          agLayer.eachLayer((layer) => {
            const m = countyMetrics(String(layer.feature?.properties?.fips || ""));
            const s = { color: "#31383d", weight: .55, opacity: .85, fillColor: agColor(m.ag), fillOpacity: m.ag === null ? .16 : .72 };
            layer.options.__baseStyle = s; layer.setStyle(s);
          });
          sviLayer.options.onEachFeature = common.onEachFeature;
          sviLayer.addData(activeRegionGeoJSON);
          sviLayer.eachLayer((layer) => {
            const m = countyMetrics(String(layer.feature?.properties?.fips || ""));
            const s = { color: "#31383d", weight: .55, opacity: .85, fillColor: sviColor(m.svi), fillOpacity: m.svi === null ? .12 : .72 };
            layer.options.__baseStyle = s; layer.setStyle(s);
          });
          scoreLayer.options.onEachFeature = common.onEachFeature;
          scoreLayer.addData({ type:"FeatureCollection", features: scored.filter(x => x.score >= priorityCutoff).map(x => x.feature) });
          scoreLayer.eachLayer((layer) => {
            const m = countyMetrics(String(layer.feature?.properties?.fips || ""));
            const s = { color: m.score >= 80 ? "#ff3d9a" : "#ff9f43", weight: 2.6, opacity: .95, dashArray: "6 3", fillOpacity: 0 };
            layer.options.__baseStyle = s; layer.setStyle(s);
            state.markers.set(String(layer.feature?.properties?.fips || ""), layer);
          });
          const top = scored.slice(0, 15);
          $("ranking").innerHTML = top.length ? top.map((x) => `<div class="rank-row" data-fips="${x.fips}" style="cursor:pointer;padding:5px 0;border-bottom:1px solid #303940"><b>${x.feature.properties.name}, ${x.feature.properties.state}</b> · ${x.score}/100</div>`).join("") : '<span class="small">No ranked counties in this region.</span>';
          $("ranking").querySelectorAll("[data-fips]").forEach((el) => el.addEventListener("click", () => {
            const layer = state.markers.get(el.dataset.fips);
            if (layer) { map.fitBounds(layer.getBounds(), { maxZoom: 8, padding:[30,30] }); layer.openPopup(); }
          }));
        }
'''
        if injection_marker not in text:
            raise RuntimeError("Could not locate refresh() insertion point")
        text = text.replace(injection_marker, regional_code + injection_marker, 1)

    # Ensure initial refresh loads the selected region geometry after data snapshots.
    refresh_anchor = '          await render();\n          $("updated")'
    if refresh_anchor in text:
        text = text.replace(
            refresh_anchor,
            '          if (!activeRegionGeoJSON) await loadRegionGeometry($("region").value, true);\n          else await render();\n          $("updated")',
            1,
        )

    # Region changes swap the cached regional polygon asset and fit the map.
    if '$("region").addEventListener("change"' not in text:
        listener_anchor = '        $("refresh").addEventListener("click", refresh);'
        if listener_anchor not in text:
            raise RuntimeError("Could not locate refresh event listener")
        text = text.replace(
            listener_anchor,
            '        $("region").addEventListener("change", async (e) => {\n          try { await loadRegionGeometry(e.target.value, true); }\n          catch (err) { diag(`Region geometry unavailable: ${err}`); }\n        });\n        ' + listener_anchor,
            1,
        )

    # Explanatory copy for the new visual grammar.
    text = text.replace(
        "Combined priority circles",
        "Highest combined priority outlines",
    ).replace(
        "Agricultural exposure circles",
        "Agricultural exposure choropleth",
    ).replace(
        "Drought circles",
        "Drought severity choropleth",
    )

    TARGET.write_text(text, encoding="utf-8")

    final = TARGET.read_text(encoding="utf-8")
    checks = {
        "region selector": 'id="region"' in final,
        "regional geometry cache": "mitchellco:county-geometry" in final,
        "county boundaries": '"County boundaries": boundaryLayer' in final,
        "drought polygon layer": '"Drought severity (USDM)": droughtLayer' in final,
        "ag polygon layer": '"Agricultural exposure": agLayer' in final,
        "svi polygon layer": '"Community vulnerability (CDC SVI)": sviLayer' in final,
        "priority outline": '"Highest combined priority": scoreLayer' in final and 'dashArray: "6 3"' in final,
        "no ArcGIS": not re.search(r"arcgis|FeatureServer|MapServer|ImageServer", final, re.I),
    }
    failed = [k for k,v in checks.items() if not v]
    if failed:
        raise RuntimeError("Map 17 layer redesign checks failed: " + ", ".join(failed))


def main() -> None:
    counts = build_region_geojson()
    print("Regional county geometry:", counts)
    patch_html()
    print("Map 17 redesigned: regional county polygons, choropleths, SVI layer, priority outlines.")


if __name__ == "__main__":
    main()
