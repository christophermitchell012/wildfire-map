#!/usr/bin/env python3
"""Remove Map 17's legacy fetch runtime and harden browser startup/rendering."""
from __future__ import annotations

import re
import urllib.request
from pathlib import Path

TARGET = Path("daily-maps/17-agricultural-drought-farm-exposure.html")
LEAFLET_CSS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"
LEAFLET_JS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"
UA = "MitchellCo-DailyMap/1.0 (+https://christophermitchell012.github.io/wildfire-map/)"


def get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", "replace")


def make_leaflet_css_standalone(css: str) -> str:
    """Drop Leaflet's decorative external image URLs.

    Map 17 uses CircleMarkers and text controls, so marker-icon and layer-control
    image assets are not required for the product. Removing those URLs avoids
    another runtime dependency.
    """
    return re.sub(r"url\((?!\s*['\"]?data:)[^)]+\)", "none", css, flags=re.I)


def literal_sub(pattern: re.Pattern[str], replacement: str, text: str) -> str:
    """Use a callable replacement so backslashes in minified JS/CSS stay literal."""
    return pattern.sub(lambda _match: replacement, text, count=1)


def main() -> None:
    text = TARGET.read_text(encoding="utf-8")

    # Remove inherited global fetch monkey patch. Map 17's government data is
    # embedded at build time, so retry/abort wrappers are unnecessary and harmful.
    text, removed = re.subn(
        r"\s*<script\s+data-mitchellco-runtime=\"evacwatch-v2\"\s+data-mitchellco-performance=\"gridwatch-v1\"\s*>.*?</script>",
        "",
        text,
        count=1,
        flags=re.S,
    )
    if removed != 1 and "data-mitchellco-performance=\"gridwatch-v1\"" in text:
        raise RuntimeError("Could not remove legacy global fetch runtime")

    # Inline Leaflet itself so failure of a third-party CDN cannot prevent L.map()
    # from existing and leave the whole UI stuck in its initial Loading state.
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

    # The original page instantiated L.canvas() separately for every marker,
    # creating thousands of renderers. One shared renderer handles all county
    # CircleMarkers and keeps the main thread responsive.
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

    # Rendering thousands of county objects must not be one long main-thread task.
    # Make render async and yield every 250 counties so controls/paint stay alive.
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
        "embedded data": "BEGIN MAP17 EMBEDDED DATA" in final,
        "shared canvas": "const sharedRenderer = L.canvas({ padding: 0.5 });" in final,
        "no per-marker canvas": "renderer: L.canvas()," not in final,
        "async render": "async function render()" in final and "await render();" in final,
        "render yielding": "renderedCount % 250" in final,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError("Map 17 hardening failed: " + ", ".join(failed))
    print("Map 17 hardened: embedded Leaflet, no fetch monkey-patch, shared/yielding canvas render.")


if __name__ == "__main__":
    main()
