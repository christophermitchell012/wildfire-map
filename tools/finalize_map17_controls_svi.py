#!/usr/bin/env python3
"""Finalize Map 17 UI controls and require a usable CDC/ATSDR SVI snapshot.

Runs after the regional-layer builder. It:
1. Loads the official CDC/ATSDR 2022 *U.S.-ranked* county CSV from the static
   svi.cdc.gov download tree (never ArcGIS/Esri).
2. Replaces the embedded CDC_SVI object and metadata in Map 17.
3. Restyles the header region selector as the rounded/ring control used by the
   earlier daily maps.
4. Removes Leaflet's upper-right layer chooser and puts every clickable layer
   toggle together in the left sidebar.
"""
from __future__ import annotations

import csv
import io
import json
import re
import urllib.request
from pathlib import Path

TARGET = Path("daily-maps/17-agricultural-drought-farm-exposure.html")
UA = "MitchellCo-DailyMap/1.0 (+https://christophermitchell012.github.io/wildfire-map/)"

# CDC historically exposes SVI static CSVs under this tree. Keep several
# filename variants because CDC has changed naming conventions over releases.
CDC_US_COUNTY_CANDIDATES = [
    "https://svi.cdc.gov/Documents/Data/2022/csv/states_counties/United%20States_COUNTY.csv",
    "https://svi.cdc.gov/Documents/Data/2022/csv/states_counties/United_States_COUNTY.csv",
    "https://svi.cdc.gov/Documents/Data/2022/csv/states_counties/UnitedStates_COUNTY.csv",
    "https://svi.cdc.gov/Documents/Data/2022/csv/United%20States_COUNTY.csv",
    "https://svi.cdc.gov/Documents/Data/2022/csv/US_COUNTY.csv",
    "https://svi.cdc.gov/Documents/Data/2022/csv/SVI2022_US_COUNTY.csv",
]


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/csv,*/*;q=0.5"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read()


def norm_fips(v) -> str | None:
    s = re.sub(r"\D", "", str(v or ""))
    return s.zfill(5) if 1 <= len(s) <= 5 else None


def load_us_ranked_svi() -> tuple[dict[str, float], str]:
    errors = []
    for url in CDC_US_COUNTY_CANDIDATES:
        try:
            raw = get(url)
            text = raw.decode("utf-8-sig", "replace")
            reader = csv.DictReader(io.StringIO(text))
            headers = reader.fieldnames or []
            if "RPL_THEMES" not in headers:
                raise RuntimeError(f"RPL_THEMES absent; headers={headers[:12]}")
            out: dict[str, float] = {}
            for row in reader:
                fips = norm_fips(row.get("FIPS") or row.get("STCNTY"))
                try:
                    value = float(row.get("RPL_THEMES", ""))
                except (TypeError, ValueError):
                    continue
                if fips and 0.0 <= value <= 1.0 and value != -999:
                    out[fips] = round(value, 6)
            # National county file should cover essentially all 3,143 counties.
            if len(out) < 3000:
                raise RuntimeError(f"only {len(out)} county values")
            print(f"CDC SVI source: {url} ({len(out)} counties)")
            return out, url
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("No usable first-party CDC U.S.-ranked county SVI CSV. " + " | ".join(errors))


def patch_svi(text: str, svi: dict[str, float], source_url: str) -> str:
    svi_json = json.dumps(svi, separators=(",", ":"), ensure_ascii=False)
    text, n = re.subn(
        r"const CDC_SVI = Object\.freeze\(\{.*?\}\);",
        f"const CDC_SVI = Object.freeze({svi_json});",
        text,
        count=1,
        flags=re.S,
    )
    if n != 1:
        raise RuntimeError("Could not replace embedded CDC_SVI")

    # Replace only the metadata value for svi, preserving other generated metadata.
    text, n = re.subn(
        r'("svi"\s*:\s*)"[^"]*"',
        lambda m: m.group(1) + json.dumps(f"CDC/ATSDR SVI 2022 county overall percentile; U.S.-ranked; static CSV {source_url}"),
        text,
        count=1,
    )
    if n != 1:
        raise RuntimeError("Could not update SVI metadata")
    return text


def patch_header(text: str) -> str:
    old = re.compile(
        r'<div class="spacer"></div>\s*<label class="region-control">Region <select id="region" aria-label="Map region">'
        r'<option value="northeast">Northeast</option><option value="midwest">Midwest</option><option value="south" selected>South</option><option value="west">West</option>'
        r'</select></label><button id="refresh">Refresh</button>'
    )
    new = '''<div class="spacer"></div>
      <div class="region-ring" aria-label="Region control">
        <label for="region">Region</label>
        <select id="region" aria-label="Map region"><option value="northeast">Northeast</option><option value="midwest">Midwest</option><option value="south" selected>South</option><option value="west">West</option></select>
      </div>
      <button id="refresh">Refresh</button>'''
    text, n = old.subn(new, text, count=1)
    if n != 1 and 'class="region-ring"' not in text:
        raise RuntimeError("Could not restyle region control")

    if ".region-ring" not in text:
        marker = "      .region-control { display:flex; align-items:center; gap:6px; color:var(--muted); font-size:11px; }"
        css = '''      .region-control { display:none; }
      .region-ring {
        display:flex; align-items:center; gap:7px; padding:3px 5px 3px 10px;
        border:1px solid var(--line); border-radius:999px; background:var(--card);
        color:var(--muted); font-size:11px; box-shadow:inset 0 0 0 1px rgba(255,255,255,.025);
      }
      .region-ring select {
        border:0; border-left:1px solid var(--line); border-radius:999px;
        background:#11171b; padding:4px 22px 4px 8px; min-width:108px;
      }
      .region-ring select:focus { outline:2px solid #62a8ff; outline-offset:1px; }
      .layer-toggle { display:flex; align-items:center; gap:8px; margin:7px 0; font-size:12px; cursor:pointer; }
      .layer-toggle input { accent-color:#62a8ff; }
      .layer-toggle .sw { flex:0 0 auto; }
      .layer-note { margin-top:6px; color:var(--muted); font-size:10.5px; }'''
        if marker not in text:
            raise RuntimeError("Could not locate region-control CSS marker")
        text = text.replace(marker, css, 1)
    return text


def patch_sidebar_layers(text: str) -> str:
    card = '''
        <div class="card" id="layer-controls">
          <b>Map layers</b>
          <label class="layer-toggle"><input id="layer-drought" type="checkbox" checked><span class="sw" style="background:#e60000"></span><span>Drought severity (USDM)</span></label>
          <label class="layer-toggle"><input id="layer-ag" type="checkbox"><span class="sw" style="background:#cf7900"></span><span>Agricultural exposure</span></label>
          <label class="layer-toggle"><input id="layer-svi" type="checkbox"><span class="sw" style="background:#8438a6"></span><span>Community vulnerability (CDC/ATSDR SVI)</span></label>
          <label class="layer-toggle"><input id="layer-score" type="checkbox" checked><span class="sw" style="background:transparent;border:2px dashed #ff5b8d;height:12px"></span><span>Highest combined priority</span></label>
          <label class="layer-toggle"><input id="layer-boundaries" type="checkbox" checked><span class="sw" style="background:transparent;border:1px solid #8a949b"></span><span>County boundaries</span></label>
          <div class="layer-note">All interactive layers are controlled here. Click a county on an enabled thematic layer for details.</div>
        </div>'''
    if 'id="layer-controls"' not in text:
        text = text.replace("      <aside>", "      <aside>" + card, 1)

    # Remove Leaflet's upper-right layer chooser entirely.
    text, n = re.subn(
        r'const layerControl = L\.control\.layers\(null, \{.*?\}, \{ collapsed: false \}\)\.addTo\(map\);',
        'const layerControl = null;',
        text,
        count=1,
        flags=re.S,
    )
    if n != 1 and "const layerControl = null;" not in text:
        raise RuntimeError("Could not remove Leaflet layer chooser")

    if "function syncSidebarLayers()" not in text:
        hook = '''
        function syncSidebarLayers() {
          const pairs = [
            ["layer-drought", droughtLayer], ["layer-ag", agLayer], ["layer-svi", sviLayer],
            ["layer-score", scoreLayer], ["layer-boundaries", boundaryLayer],
          ];
          for (const [id, layer] of pairs) {
            const el = $(id); if (!el) continue;
            if (el.checked && !map.hasLayer(layer)) layer.addTo(map);
            if (!el.checked && map.hasLayer(layer)) map.removeLayer(layer);
          }
        }
        for (const id of ["layer-drought","layer-ag","layer-svi","layer-score","layer-boundaries"]) {
          $(id)?.addEventListener("change", syncSidebarLayers);
        }
        syncSidebarLayers();
'''
        # Place after region-change listener so all layer objects and $ exist.
        needle = '$("region").addEventListener("change", async () => {'
        idx = text.find(needle)
        if idx < 0:
            raise RuntimeError("Could not locate region listener for sidebar layer wiring")
        text = text[:idx] + hook + "        " + text[idx:]
    return text


def validate(text: str, svi_count: int) -> None:
    checks = {
        "SVI coverage": svi_count >= 3000,
        "ring region control": 'class="region-ring"' in text,
        "sidebar layer controls": 'id="layer-controls"' in text,
        "no Leaflet layer chooser": "L.control.layers(null" not in text,
        "SVI layer toggle": 'id="layer-svi"' in text,
        "no Esri runtime": not re.search(r"services[0-9]*\.arcgis\.com|onemap\.cdc\.gov|FeatureServer|MapServer|ImageServer", text, re.I),
    }
    failed = [k for k,v in checks.items() if not v]
    if failed:
        raise RuntimeError("Map 17 finalization checks failed: " + ", ".join(failed))


def main() -> None:
    svi, source = load_us_ranked_svi()
    text = TARGET.read_text(encoding="utf-8")
    text = patch_svi(text, svi, source)
    text = patch_header(text)
    text = patch_sidebar_layers(text)
    validate(text, len(svi))
    TARGET.write_text(text, encoding="utf-8")
    print(f"Map 17 finalized: {len(svi)} CDC/ATSDR SVI counties; ring region control; sidebar-only layer toggles.")


if __name__ == "__main__":
    main()
