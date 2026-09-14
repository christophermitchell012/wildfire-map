#!/usr/bin/env python3
"""Remove Map 17's legacy fetch runtime and inline Leaflet for deterministic startup."""
from __future__ import annotations

import base64
import mimetypes
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


def get_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read()


def data_uri(url: str) -> str:
    raw = get_bytes(url)
    mime = mimetypes.guess_type(url)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def inline_leaflet_css_assets(css: str) -> str:
    base = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/"

    def repl(match: re.Match[str]) -> str:
        raw = match.group(1).strip("'\"")
        if raw.startswith("data:") or raw.startswith("http"):
            return match.group(0)
        absolute = base + raw.lstrip("./")
        return f"url('{data_uri(absolute)}')"

    return re.sub(r"url\(([^)]+)\)", repl, css)


def main() -> None:
    text = TARGET.read_text(encoding="utf-8")

    # This inherited runtime globally monkey-patched window.fetch, added retries,
    # timers and abort controllers, and is inappropriate for Map 17 because all
    # government data is embedded at build time. Removing it also eliminates the
    # timeout/retry cascade reported in production.
    text, removed = re.subn(
        r"\s*<script\s+data-mitchellco-runtime=\"evacwatch-v2\"\s+data-mitchellco-performance=\"gridwatch-v1\"\s*>.*?</script>",
        "",
        text,
        count=1,
        flags=re.S,
    )
    if removed != 1 and "data-mitchellco-performance=\"gridwatch-v1\"" in text:
        raise RuntimeError("Could not remove legacy global fetch runtime")

    css = inline_leaflet_css_assets(get_text(LEAFLET_CSS))
    js = get_text(LEAFLET_JS)
    # Avoid accidentally terminating the host script/style blocks.
    js = js.replace("</script", "<\\/script")
    css = css.replace("</style", "<\\/style")

    external_css = re.compile(
        r"\s*<link\s+rel=\"stylesheet\"\s+href=\"https://cdnjs\.cloudflare\.com/ajax/libs/leaflet/1\.9\.4/leaflet\.min\.css\"\s*/?>",
        re.S,
    )
    if external_css.search(text):
        text = external_css.sub("\n    <style data-map17-leaflet=\"inline\">\n" + css + "\n    </style>", text, count=1)
    elif 'data-map17-leaflet="inline"' not in text:
        raise RuntimeError("Could not locate Leaflet CSS dependency")

    external_js = re.compile(
        r"\s*<script\s+src=\"https://cdnjs\.cloudflare\.com/ajax/libs/leaflet/1\.9\.4/leaflet\.min\.js\"\s*></script>",
        re.S,
    )
    if external_js.search(text):
        text = external_js.sub("\n    <script data-map17-leaflet=\"inline\">\n" + js + "\n    </script>", text, count=1)
    elif text.count('data-map17-leaflet="inline"') < 2:
        raise RuntimeError("Could not locate Leaflet JS dependency")

    TARGET.write_text(text, encoding="utf-8")

    # Hard postconditions.
    final = TARGET.read_text(encoding="utf-8")
    checks = {
        "legacy fetch override": "window.fetch = resilientFetch" not in final,
        "legacy runtime marker": 'data-mitchellco-performance="gridwatch-v1"' not in final,
        "external Leaflet JS": LEAFLET_JS not in final,
        "external Leaflet CSS": LEAFLET_CSS not in final,
        "inline Leaflet JS/CSS": final.count('data-map17-leaflet="inline"') == 2,
        "embedded data": "BEGIN MAP17 EMBEDDED DATA" in final,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError("Map 17 hardening failed: " + ", ".join(failed))
    print("Map 17 runtime hardened: no fetch monkey-patch; Leaflet JS/CSS embedded.")


if __name__ == "__main__":
    main()
