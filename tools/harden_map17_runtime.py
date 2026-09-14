#!/usr/bin/env python3
"""Harden Map 17 browser startup/rendering and externalize county reference data."""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

TARGET = Path("daily-maps/17-agricultural-drought-farm-exposure.html")
COUNTY_SNAPSHOT = Path("daily-maps/data/county-reference-2025-gazetteer-v1.json")
COUNTY_CACHE_VERSION = "2025-gazetteer-v1"
LEAFLET_CSS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"
LEAFLET_JS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"
UA = "MitchellCo-DailyMap/1.0 (+https://christophermitchell012.github.io/wildfire-map/)"


def get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", "replace")


def make_leaflet_css_standalone(css: str) -> str:
    return re.sub(r"url\((?!\s*['\"]?data:)[^)]+\)", "none", css, flags=re.I)


def literal_sub(pattern: re.Pattern[str], replacement: str, text: str) -> str:
    return pattern.sub(lambda _match: replacement, text, count=1)


def externalize_counties(text: str) -> str:
    """Move static county reference data out of HTML and use versioned localStorage."""
    county_re = re.compile(
        r"const COUNTY_CENTROIDS = Object\.freeze\((\{.*?\})\);\n\s*const USDM_SNAPSHOT",
        re.S,
    )
    match = county_re.search(text)
    if match:
        counties = json.loads(match.group(1))
        if len(counties) < 3000:
            raise RuntimeError(f"County reference coverage too small: {len(counties)}")
        payload = {
            "version": COUNTY_CACHE_VERSION,
            "vintage": "2025",
            "source": "U.S. Census Bureau 2025 Gazetteer county representative points",
            "counties": counties,
        }
        COUNTY_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        COUNTY_SNAPSHOT.write_text(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
        replacement = (
            f'const COUNTY_CACHE_VERSION = "{COUNTY_CACHE_VERSION}";\n'
            '        const COUNTY_CACHE_KEY = `mitchellco:county-ref:${COUNTY_CACHE_VERSION}`;\n'
            '        const COUNTY_SNAPSHOT_URL = "data/county-reference-2025-gazetteer-v1.json";\n'
            '        const COUNTY_CENTROIDS = Object.freeze({});\n'
            '        const USDM_SNAPSHOT'
        )
        text = literal_sub(county_re, replacement, text)
    elif 'const COUNTY_CACHE_VERSION = "2025-gazetteer-v1";' not in text:
        raise RuntimeError("Could not locate embedded county reference data")

    loader = r'''function validCountyPayload(payload) {
          if (!payload || payload.version !== COUNTY_CACHE_VERSION || !payload.counties || typeof payload.counties !== "object") return false;
          const entries = Object.entries(payload.counties);
          if (entries.length < 3000) return false;
          for (let i = 0; i < Math.min(12, entries.length); i++) {
            const [fips, county] = entries[i];
            if (!/^\d{5}$/.test(fips) || !county || !Number.isFinite(Number(county.lat)) || !Number.isFinite(Number(county.lon))) return false;
          }
          return true;
        }
        function readCountyCache() {
          try {
            const raw = localStorage.getItem(COUNTY_CACHE_KEY);
            if (!raw) return null;
            const parsed = JSON.parse(raw);
            if (!validCountyPayload(parsed)) {
              localStorage.removeItem(COUNTY_CACHE_KEY);
              return null;
            }
            return parsed;
          } catch (_err) {
            return null;
          }
        }
        function writeCountyCache(payload) {
          try {
            localStorage.setItem(COUNTY_CACHE_KEY, JSON.stringify(payload));
          } catch (_err) {
            // Storage may be disabled/full. The validated in-memory snapshot remains usable.
          }
        }
        async function loadCentroids(run) {
          let payload = readCountyCache();
          let fromCache = Boolean(payload);
          try {
            if (!payload) {
              const response = await fetch(COUNTY_SNAPSHOT_URL, { cache: "force-cache" });
              if (!response.ok) throw new Error(`county snapshot HTTP ${response.status}`);
              payload = await response.json();
              if (!validCountyPayload(payload)) throw new Error("county snapshot failed validation");
              writeCountyCache(payload);
            }
            if (run !== state.run) return false;
            state.counties = new Map(Object.entries(payload.counties));
            $("centroidCount").textContent = state.counties.size.toLocaleString();
            setHealth(
              "hCentroids",
              fromCache ? "Cached" : "Live",
              fromCache ? "cached" : "live",
              `${fromCache ? "localStorage" : "same-origin snapshot"} • Census Gazetteer 2025 • ${state.counties.size.toLocaleString()} counties`,
            );
            return true;
          } catch (err) {
            if (run === state.run) {
              setHealth("hCentroids", "Unavailable", "bad", String(err));
              diag(`County reference unavailable: ${err}`);
            }
            return false;
          }
        }'''
    text, n = re.subn(
        r"async function loadCentroids\(run\) \{.*?\n\s*\}\n\s*async function loadDrought\(run\) \{",
        loader + "\n        async function loadDrought(run) {",
        text,
        count=1,
        flags=re.S,
    )
    if n != 1:
        raise RuntimeError("Could not replace county loader with localStorage implementation")
    return text


def main() -> None:
    text = TARGET.read_text(encoding="utf-8")

    text, removed = re.subn(
        r"\s*<script\s+data-mitchellco-runtime=\"evacwatch-v2\"\s+data-mitchellco-performance=\"gridwatch-v1\"\s*>.*?</script>",
        "",
        text,
        count=1,
        flags=re.S,
    )
    if removed != 1 and "data-mitchellco-performance=\"gridwatch-v1\"" in text:
        raise RuntimeError("Could not remove legacy global fetch runtime")

    text = externalize_counties(text)

    css = make_leaflet_css_standalone(get_text(LEAFLET_CSS))
    js = get_text(LEAFLET_JS)
    js = js.replace("</script", "<\\/script")
    css = css.replace("</style", "<\\/style")

    external_css = re.compile(
        r"\s*<link\s+rel=\"stylesheet\"\s+href=\"https://cdnjs\.cloudflare\.com/ajax/libs/leaflet/1\.9\.4/leaflet\.min\.css\"\s*/?>",
        re.S,
    )
    if external_css.search(text):
        text = literal_sub(
            external_css,
            "\n    <style data-map17-leaflet=\"inline\">\n" + css + "\n    </style>",
            text,
        )
    elif 'data-map17-leaflet="inline"' not in text:
        raise RuntimeError("Could not locate Leaflet CSS dependency")

    external_js = re.compile(
        r"\s*<script\s+src=\"https://cdnjs\.cloudflare\.com/ajax/libs/leaflet/1\.9\.4/leaflet\.min\.js\"\s*></script>",
        re.S,
    )
    if external_js.search(text):
        text = literal_sub(
            external_js,
            "\n    <script data-map17-leaflet=\"inline\">\n" + js + "\n    </script>",
            text,
        )
    elif text.count('data-map17-leaflet="inline"') < 2:
        raise RuntimeError("Could not locate Leaflet JS dependency")

    if "renderer: L.canvas()," in text:
        text = text.replace("renderer: L.canvas(),", "renderer: sharedRenderer,")
    if "const sharedRenderer = L.canvas" not in text:
        anchor = "        const droughtLayer = L.layerGroup().addTo(map),"
        if anchor not in text:
            raise RuntimeError("Could not locate layer-group initialization")
        text = text.replace(
            anchor,
            "        const sharedRenderer = L.canvas({ padding: 0.5 });\n" + anchor,
            1,
        )

    if "        function render() {" in text:
        text = text.replace("        function render() {", "        async function render() {", 1)
    if "          let renderedCount = 0;" not in text:
        anchor = "          const ranked = [];\n          for (const [fips, county] of state.counties) {"
        if anchor not in text:
            raise RuntimeError("Could not locate county render loop")
        text = text.replace(
            anchor,
            "          const ranked = [];\n          let renderedCount = 0;\n          for (const [fips, county] of state.counties) {",
            1,
        )
    if "await new Promise((resolve) => setTimeout(resolve, 0));" not in text:
        anchor = "          }\n          ranked.sort((a, b) => b.sc - a.sc);"
        if anchor not in text:
            raise RuntimeError("Could not locate end of county render loop")
        text = text.replace(
            anchor,
            "            if (++renderedCount % 250 === 0)\n              await new Promise((resolve) => setTimeout(resolve, 0));\n          }\n          ranked.sort((a, b) => b.sc - a.sc);",
            1,
        )
    if "          render();\n          $(\"updated\")" in text:
        text = text.replace(
            "          render();\n          $(\"updated\")",
            "          await render();\n          $(\"updated\")",
            1,
        )

    TARGET.write_text(text, encoding="utf-8")

    final = TARGET.read_text(encoding="utf-8")
    checks = {
        "legacy fetch override": "window.fetch = resilientFetch" not in final,
        "legacy runtime marker": 'data-mitchellco-performance="gridwatch-v1"' not in final,
        "external Leaflet JS": LEAFLET_JS not in final,
        "external Leaflet CSS": LEAFLET_CSS not in final,
        "inline Leaflet JS/CSS": final.count('data-map17-leaflet="inline"') == 2,
        "embedded dynamic data": "BEGIN MAP17 EMBEDDED DATA" in final,
        "county data externalized": "const COUNTY_CENTROIDS = Object.freeze({});" in final,
        "county localStorage": "localStorage.getItem(COUNTY_CACHE_KEY)" in final and "localStorage.setItem(COUNTY_CACHE_KEY" in final,
        "county same-origin fallback": 'const COUNTY_SNAPSHOT_URL = "data/county-reference-2025-gazetteer-v1.json";' in final,
        "county snapshot exists": COUNTY_SNAPSHOT.exists(),
        "shared canvas": "const sharedRenderer = L.canvas({ padding: 0.5 });" in final,
        "no per-marker canvas": "renderer: L.canvas()," not in final,
        "async render": "async function render()" in final and "await render();" in final,
        "render yielding": "renderedCount % 250" in final,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError("Map 17 hardening failed: " + ", ".join(failed))
    print("Map 17 hardened: counties localStorage cache-first with same-origin fallback; embedded Leaflet; shared/yielding canvas render.")


if __name__ == "__main__":
    main()
