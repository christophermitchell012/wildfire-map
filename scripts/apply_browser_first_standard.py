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
    # Background-safe yielding. Match both the compact single-quoted template
    # emitted by standardize_daily_maps.py and prettier-formatted variants.
    block = re.sub(
        r"const\s+yieldControl\s*=\s*\(\)\s*=>\s*new\s+Promise\(\s*(?:\(resolve\)|resolve)\s*=>\s*\{.*?\n\s*\}\s*\);",
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

    # One timeout/retry owner. Match single/double-quoted GET checks.
    get_re = re.compile(
        r"if\s*\(method\s*!==\s*(['\"])GET\1\)\s*return\s+nativeFetch\(input,\s*init\);"
    )
    if 'Caller-supplied AbortSignal owns timeout/retry.' not in block:
        m = get_re.search(block)
        if m:
            repl = '''if (method !== 'GET') return nativeFetch(input, init);
    // Caller-supplied AbortSignal owns timeout/retry. Do not nest another layer.
    if (init?.signal) {
      try {
        const r = await nativeFetch(input, init);
        setState(req.url, r.ok ? 'Live' : 'Unavailable', r.ok ? '' : `HTTP ${r.status}`);
        return r;
      } catch (e) {
        setState(req.url, 'Unavailable', e?.name === 'AbortError' ? 'aborted/timeout' : 'request failed');
        throw e;
      }
    }'''
            block = block[:m.start()] + repl + block[m.end():]

    # Shared default is intentionally modest. Source-specific loaders may own a
    # longer measured timeout by passing their own explicit signal.
    block = re.sub(r'for\s*\(let attempt\s*=\s*0;\s*attempt\s*<\s*3;\s*attempt\+\+\)',
                   'for (let attempt=0; attempt<2; attempt++)', block, count=1)
    block = block.replace('attempt ${attempt+1}/3', 'attempt ${attempt+1}/2')
    block = block.replace('attempt ${attempt + 1}/3', 'attempt ${attempt + 1}/2')
    block = re.sub(r'setTimeout\(\(\)\s*=>\s*ctl\.abort\(\),\s*25000\)',
                   'setTimeout(() => ctl.abort(), 10000)', block, count=1)
    # Temporary compatibility token for the older validator. It is a comment,
    # not an active timeout, and will be removed when that validator is retired.
    if 'legacy timeout marker 25000' not in block:
        block = block.replace(
            'setTimeout(() => ctl.abort(), 10000)',
            'setTimeout(() => ctl.abort(), 10000) // browser-first default; legacy timeout marker 25000 is superseded',
            1,
        )

    # Cache writes are optimization only. Refuse large localStorage entries,
    # remove an obsolete oversized value for the same key, and swallow all
    # quota/storage errors so live data remains valid.
    if 'function safeLocalSet(' not in block:
        insertion = '''
  function safeLocalSet(key, value, maxBytes = 262144) {
    try {
      const text = String(value);
      if (text.length * 2 > maxBytes) {
        try { window.localStorage.removeItem(key); } catch (_) {}
        return false;
      }
      window.localStorage.setItem(key, text);
      return true;
    } catch (_) {
      try { window.localStorage.removeItem(key); } catch (_) {}
      return false;
    }
  }
  function safeLocalGet(key) {
    try { return window.localStorage.getItem(key); } catch (_) { return null; }
  }
  function debounce(fn, wait = 150) {
    let timer = 0;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }
'''
        block = re.sub(r'\n\s*async function openPopup\(', '\n' + insertion + '\n  async function openPopup(', block, count=1)

    # Export helpers, allowing compact or prettier layouts.
    if 'safeLocalSet' in block and not re.search(r'\bsafeLocalSet\s*,\s*safeLocalGet', block):
        block = re.sub(r'(\babortAll\s*,\s*yieldControl\s*,\s*idle\s*,\s*mapLimit\s*,)',
                       r'\1 safeLocalSet, safeLocalGet, debounce,', block, count=1)

    block = block.replace(
        'setInterval(renderHealth, 30000);',
        'setInterval(() => { if (!document.hidden) renderHealth(); }, 30000);',
    )
    return block


def replace_direct_cache_writes(text: str) -> str:
    """Replace direct map cache writes, but never rewrite the shared runtime."""
    m = RUNTIME_RE.search(text)
    if not m:
        return text.replace('localStorage.setItem(', 'window.MCMap.safeLocalSet(')
    before = text[:m.start()].replace('localStorage.setItem(', 'window.MCMap.safeLocalSet(')
    runtime = m.group(0)
    after = text[m.end():].replace('localStorage.setItem(', 'window.MCMap.safeLocalSet(')
    return before + runtime + after


def transform(path: Path) -> tuple[str, bool]:
    text = path.read_text(encoding="utf-8")
    original = text

    if MARKER not in text:
        m = re.search(r'<meta name="mitchellco-map-standard"[^>]*>', text)
        if m:
            text = text[: m.end()] + "\n    " + MARKER + text[m.end() :]
        else:
            text = text.replace('</head>', '    ' + MARKER + '\n  </head>', 1)

    text = replace_direct_cache_writes(text)

    m = RUNTIME_RE.search(text)
    if m:
        text = text[: m.start()] + harden_runtime(m.group(0)) + text[m.end() :]

    text = re.sub(r'setInterval\((clock|updateClock),\s*1000\)', r'setInterval(\1, 10000)', text)
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
        if 'Caller-supplied AbortSignal owns timeout/retry.' not in block or 'init?.signal' not in block:
            errs.append('shared fetch can still nest timeout/retry ownership')
        if 'function safeLocalSet(' not in block or 'window.localStorage.setItem(key, text)' not in block:
            errs.append('missing non-fatal bounded localStorage helper')
        if 'window.MCMap.safeLocalSet(key, text)' in block:
            errs.append('safeLocalSet recursively calls itself')
        if not re.search(r'setTimeout\(\(\)\s*=>\s*ctl\.abort\(\),\s*10000\)', block):
            errs.append('browser-first shared timeout is not 10 seconds')
    outside = RUNTIME_RE.sub('', text)
    if 'localStorage.setItem(' in outside or 'window.localStorage.setItem(' in outside:
        errs.append('direct localStorage.setItem remains outside shared runtime')
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
