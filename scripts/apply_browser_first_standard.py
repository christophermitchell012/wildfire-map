#!/usr/bin/env python3
"""Apply browser-first performance safeguards to all numbered daily maps.

This pass encodes lessons from NetWatch Map 11. It intentionally avoids blindly
chunking or rewriting source-specific loaders. Instead it hardens shared runtime
behavior, storage failure isolation, background-safe yielding, timer hygiene, and
nested timeout ownership across every map.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAP_DIR = ROOT / "daily-maps"
MARKER = '<meta name="mitchellco-browser-first" content="v1" />'
RUNTIME_RE = re.compile(
    r'<script\s+data-mitchellco-runtime=["\']evacwatch-v2["\'][^>]*>.*?</script>',
    re.I | re.S,
)


def harden_runtime(block: str) -> str:
    # Background-safe yielding. requestAnimationFrame may stop in hidden tabs.
    block = re.sub(
        r"const yieldControl = \(\) =>\s*new Promise\(\(resolve\) => \{.*?\n\s*\}\);",
        """const yieldControl = async () => {
          if (globalThis.scheduler?.yield) {
            await globalThis.scheduler.yield();
            return;
          }
          await new Promise((resolve) => setTimeout(resolve, 0));
        };""",
        block,
        count=1,
        flags=re.S,
    )

    # One timeout/retry owner: callers that already supply an AbortSignal own
    # timeout/retry behavior. The shared wrapper observes status but does not nest.
    needle = 'if (method !== "GET") return nativeFetch(input, init);'
    if needle in block and 'Caller-supplied AbortSignal owns timeout/retry.' not in block:
        repl = '''if (method !== "GET") return nativeFetch(input, init);
          // Caller-supplied AbortSignal owns timeout/retry. Do not nest another layer.
          if (req.signal) {
            try {
              const r = await nativeFetch(input, init);
              setState(req.url, r.ok ? "Live" : "Unavailable", r.ok ? "" : `HTTP ${r.status}`);
              return r;
            } catch (e) {
              setState(req.url, "Unavailable", e?.name === "AbortError" ? "aborted/timeout" : "request failed");
              throw e;
            }
          }'''
        block = block.replace(needle, repl, 1)

    # Shared default is intentionally modest. Source-specific loaders may own a
    # longer measured timeout by passing their own signal.
    block = block.replace('for (let attempt = 0; attempt < 3; attempt++)', 'for (let attempt = 0; attempt < 2; attempt++)')
    block = block.replace('`attempt ${attempt + 1}/3`', '`attempt ${attempt + 1}/2`')
    block = block.replace('setTimeout(() => ctl.abort(), 25000)', 'setTimeout(() => ctl.abort(), 10000)')

    # Cache writes are optimization only. Refuse large localStorage entries and
    # swallow quota/storage errors so live data remains valid.
    if 'function safeLocalSet(' not in block:
        insertion = '''
        function safeLocalSet(key, value, maxBytes = 262144) {
          try {
            const text = String(value);
            if (text.length * 2 > maxBytes) return false;
            localStorage.setItem(key, text);
            return true;
          } catch (_) {
            return false;
          }
        }
        function safeLocalGet(key) {
          try { return localStorage.getItem(key); } catch (_) { return null; }
        }
        function debounce(fn, wait = 150) {
          let timer = 0;
          return (...args) => {
            clearTimeout(timer);
            timer = setTimeout(() => fn(...args), wait);
          };
        }
'''
        block = block.replace('async function openPopup(', insertion + '\n        async function openPopup(', 1)

    block = block.replace(
        'abortAll, yieldControl, idle, mapLimit, generation: () => globalGeneration,',
        'abortAll, yieldControl, idle, mapLimit, safeLocalSet, safeLocalGet, debounce, generation: () => globalGeneration,',
    )
    block = block.replace(
        'abortAll, yieldControl, idle, mapLimit, generation:() => globalGeneration',
        'abortAll, yieldControl, idle, mapLimit, safeLocalSet, safeLocalGet, debounce, generation:() => globalGeneration',
    )

    # Data-health age refresh should not wake a hidden tab needlessly.
    block = block.replace(
        'setInterval(renderHealth, 30000);',
        'setInterval(() => { if (!document.hidden) renderHealth(); }, 30000);',
    )
    return block


def transform(path: Path) -> tuple[str, bool]:
    text = path.read_text(encoding="utf-8")
    original = text

    if MARKER not in text:
        m = re.search(r'<meta name="mitchellco-map-standard"[^>]*>', text)
        if m:
            text = text[: m.end()] + "\n    " + MARKER + text[m.end() :]
        else:
            text = text.replace('</head>', '    ' + MARKER + '\n  </head>', 1)

    m = RUNTIME_RE.search(text)
    if m:
        text = text[: m.start()] + harden_runtime(m.group(0)) + text[m.end() :]

    # Existing direct localStorage writes now use the guarded helper. This is
    # deliberately a small-cache policy: entries above 256 KiB are not stored.
    text = text.replace('localStorage.setItem(', 'window.MCMap.safeLocalSet(')

    # Common low-value one-second age clocks become 10-second clocks. This is
    # intentionally limited to functions conventionally named clock/updateClock.
    text = re.sub(r'setInterval\((clock|updateClock),\s*1000\)', r'setInterval(\1, 10000)', text)

    # Common resize handler: debounce map layout work when the exact pattern exists.
    text = re.sub(
        r'window\.addEventListener\("resize",\s*\(\)\s*=>\s*map\.invalidateSize\(false\)\s*\);',
        'window.addEventListener("resize", window.MCMap.debounce(() => map.invalidateSize(false), 150));',
        text,
    )

    return text, text != original


def check(path: Path, text: str) -> list[str]:
    errs: list[str] = []
    if MARKER not in text:
        errs.append('missing browser-first marker')
    m = RUNTIME_RE.search(text)
    if not m:
        errs.append('missing shared runtime')
    else:
        block = m.group(0)
        if 'globalThis.scheduler?.yield' not in block or 'setTimeout(resolve, 0)' not in block:
            errs.append('shared yield is not background-safe')
        if 'Caller-supplied AbortSignal owns timeout/retry.' not in block:
            errs.append('shared fetch can still nest timeout/retry ownership')
        if 'function safeLocalSet(' not in block:
            errs.append('missing non-fatal bounded localStorage helper')
    # No map should directly write localStorage after this pass. Reads are okay.
    outside = RUNTIME_RE.sub('', text)
    if 'localStorage.setItem(' in outside:
        errs.append('direct localStorage.setItem remains')
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    changed = 0
    failures: list[str] = []
    for path in sorted(MAP_DIR.glob('[0-9][0-9]-*.html')):
        if args.check:
            for err in check(path, path.read_text(encoding='utf-8')):
                failures.append(f'{path.name}: {err}')
            continue
        text, did = transform(path)
        if did:
            path.write_text(text, encoding='utf-8', newline='\n')
            changed += 1
    if failures:
        print('\n'.join(failures))
        return 1
    print('browser-first standard:', 'check passed' if args.check else f'updated {changed} map(s)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
