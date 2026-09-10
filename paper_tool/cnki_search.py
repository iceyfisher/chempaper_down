"""CNKI (知网) keyword search in an isolated browser subprocess.

Runs as:

    python -m paper_tool.cnki_search --query "..." --output result.json

The API process never loads Pydoll: this subprocess owns the Edge instance and
writes a JSON result file, mirroring the one-DOI-one-subprocess isolation model
used for downloads.

Robustness: CNKI loads results via XHR and occasionally ignores URL-parameter
searches, so the search flow (1) tries several search-URL formats, (2) polls
the page for result links instead of reading it once, (3) falls back to typing
the query into the search box and pressing Enter, and (4) also scans
same-origin iframes. Every attempt is recorded in the "attempts" diagnostics.

The kns8s landing page frequently presents a slider CAPTCHA ("向右滑动完成验证").
When detected, the slider gap position is located by simple template matching
between the background panel and the puzzle piece (pure stdlib PNG decode),
and the slider is dragged along a humanized trajectory. Solving is best
effort: on failure the diagnostics report captcha_unsolved and the search
degrades to the remaining strategies.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import random
import re
import struct
import time
import zlib
from pathlib import Path
from urllib.parse import quote_plus

from .browser import BrowserWorker
from .config import Settings
from .storage import write_json_atomic
from pydoll.constants import Key


SEARCH_URL_TEMPLATES = (
    "https://kns.cnki.net/kns8s/defaultresult/index?classid=WD0FTY92&korder=SU&kw={query}",
    "https://kns.cnki.net/kns8s/defaultresult/index?dbcode=CJFQ&kw={query}",
    "https://search.cnki.com.cn/Search/Result?content={query}",
)

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,;，；\"']+")
YEAR_RE = re.compile(r"(19|20)\d{2}")

# Anchors pointing at real article detail pages on any CNKI host. Same-origin
# iframes are scanned too because the kns8s UI renders results inside frames.
RESULT_DISCOVERY_JS = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const patterns = [
    /\/kcms2\/article\//,
    /kcms\/detail\/detail\.aspx/,
    /KXReader\/Detail/,
    /mall\.cnki\.net\/magazine\/Article\//
  ];
  const docs = [document];
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const d = f.contentDocument;
      if (d) docs.push(d);
    } catch (e) { /* cross-origin */ }
  }
  const records = [];
  const seen = new Set();
  for (const doc of docs) {
    for (const a of doc.querySelectorAll('a[href]')) {
      const href = a.href || '';
      if (!patterns.some(p => p.test(href)) || seen.has(href)) continue;
      seen.add(href);
      const row = a.closest('tr, li, div');
      records.push({
        url: href,
        text: (a.textContent || '').replace(/\s+/g, ' ').trim(),
        rowText: row ? (row.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 500) : ''
      });
    }
  }
  return { records: records };
})()
"""

# Locates a visible search box without trusting one fixed selector.
SEARCH_BOX_LOCATE_JS = r"""
(() => {
  const selectors = [
    'input#inputSearchText',
    'input.search-input',
    'input[name="txt_1_value1"]',
    'input[placeholder*="检索"]',
    'input[placeholder*="搜索"]',
    'input[aria-label*="检索"]',
    'input[type="text"]'
  ];
  for (const selector of selectors) {
    for (const el of document.querySelectorAll(selector)) {
      if (el.offsetParent !== null || el.getClientRects().length) return selector;
    }
  }
  return null;
})()
"""

SEARCH_SUBMIT_JS = r"""
(selector, query) => {
  const input = document.querySelector(selector);
  if (!input) return false;
  input.focus();
  input.value = query;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  const root = input.closest('form, .search, header, div') || document;
  const button = root.querySelector(
    'input.search-btn, button.search-btn, .search-btn, button[type="submit"], input[type="submit"]'
  );
  if (button) { button.click(); return true; }
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
  input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
  const form = input.closest('form');
  if (form) { form.submit(); return true; }
  return true;
}
"""


def _unwrap(value):
    if not isinstance(value, dict):
        return value
    if "type" in value and "value" in value:
        return value["value"]
    if "result" in value:
        return _unwrap(value["result"])
    if "value" in value:
        return value["value"]
    return value


# ---------------------------------------------------------------------------
# Slider CAPTCHA ("向右滑动完成验证") best-effort solver
# ---------------------------------------------------------------------------

CAPTCHA_PROBE_JS = r"""
(() => {
  const box = document.querySelector('.verifybox');
  if (!box) return null;
  const move = document.querySelector('.verify-move-block');
  const panel = document.querySelector('.verify-img-panel img');
  const piece = document.querySelector('.verify-sub-block img');
  const bar = document.querySelector('.verify-bar-area');
  const rect = el => { if (!el) return null; const r = el.getBoundingClientRect();
    return {x: r.x, y: r.y, w: r.width, h: r.height}; };
  return {
    move: rect(move), panel: rect(panel), piece: rect(piece), bar: rect(bar),
    panelSrc: panel ? panel.src : null, pieceSrc: piece ? piece.src : null
  };
})()
"""


def _decode_png_rgba(data: bytes):
    """Minimal PNG decoder returning (width, height, rgba_rows).

    Supports the color/filter combinations CNKI uses: 8-bit truecolor with or
    without alpha. Anything else raises ValueError.
    """

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png")
    pos = 8
    width = height = None
    bit_depth = color_type = None
    idat = bytearray()
    palette = None
    transparency = None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        chunk = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if chunk == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", body[:10])
        elif chunk == b"PLTE":
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif chunk == b"tRNS":
            transparency = body
        elif chunk == b"IDAT":
            idat.extend(body)
        elif chunk == b"IEND":
            break
    if width is None:
        raise ValueError("missing IHDR")
    if bit_depth != 8 or color_type not in {2, 3, 6}:
        raise ValueError(f"unsupported png mode {bit_depth}/{color_type}")
    channels = {2: 3, 3: 1, 6: 4}[color_type]
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    rows = []
    prev = bytearray(stride)
    pixel_ptr = 0
    for y in range(height):
        filter_type = raw[pixel_ptr]
        pixel_ptr += 1
        line = bytearray(raw[pixel_ptr:pixel_ptr + stride])
        pixel_ptr += stride
        # PNG unfiltering (all five filter types)
        for x in range(stride):
            a = line[x - channels] if x >= channels else 0
            b = prev[x]
            c = prev[x - channels] if x >= channels else 0
            if filter_type == 0:
                pass
            elif filter_type == 1:
                line[x] = (line[x] + a) & 0xFF
            elif filter_type == 2:
                line[x] = (line[x] + b) & 0xFF
            elif filter_type == 3:
                line[x] = (line[x] + (a + b) // 2) & 0xFF
            elif filter_type == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 0xFF
        prev = line
        row = []
        for x in range(width):
            i = x * channels
            if color_type == 6:
                row.append(tuple(line[i:i + 4]))
            elif color_type == 2:
                row.append((line[i], line[i + 1], line[i + 2], 255))
            else:  # palette
                r, g, b = palette[line[i]]
                alpha = transparency[line[i]] if transparency else 255
                row.append((r, g, b, alpha))
        rows.append(row)
    return width, height, rows


def _png_from_data_uri(uri: str) -> bytes:
    if not uri or "base64," not in uri:
        raise ValueError("no base64 png")
    return base64.b64decode(uri.split("base64,", 1)[1])


def find_gap_candidates(panel_png: bytes, piece_png: bytes, limit: int = 4) -> list[int]:
    """Rank candidate gap offsets for the CNKI slider captcha.

    Verified against live captures: the kns panel renders the hole as a
    translucent overlay of the piece artwork (white- or dark-tinted). That is
    an affine transform of the artwork, and normalized cross-correlation is
    invariant to affine intensity changes, so the piece-to-panel NCC peaks
    exactly AT the hole. Rank the strongest NCC positions (spaced apart) as
    candidates; the retry loop advances through them across widget refreshes.
    A donut-contrast darkness ranking is kept as a degenerate-case fallback.
    """

    import numpy as np

    pw, ph, panel = _decode_png_rgba(panel_png)
    sw, sh, piece = _decode_png_rgba(piece_png)

    # Luminance planes, vectorized.
    panel_l = np.asarray(panel, dtype=np.float32)[:, :, :3].mean(axis=2)
    piece_a = np.asarray(piece, dtype=np.float32)[:, :, 3]
    piece_l = np.asarray(piece, dtype=np.float32)[:, :, :3].mean(axis=2)

    opaque_rows, opaque_cols = np.where(piece_a > 40)
    if opaque_rows.size == 0:
        return []
    ry0, ry1 = int(opaque_rows.min()), int(opaque_rows.max())
    rx0, rx1 = int(opaque_cols.min()), int(opaque_cols.max())
    pw_int = pw

    # piece window aligned so that opaque pixel (y, x) sits at panel (y, off+x)
    piece_win = piece_l[ry0:ry1 + 1, rx0:rx1 + 1]
    mask = piece_a[ry0:ry1 + 1, rx0:rx1 + 1] > 40
    tpl = piece_win[mask]
    tpl_mean = tpl.mean()
    tpl_norm = float(np.sqrt(((tpl - tpl_mean) ** 2).sum()))
    if tpl_norm == 0:
        return []

    # The hole never sits under the piece's own start position; skip the
    # left margin. Candidates must be >= 30 so the drag is always rightward.
    scored: list[tuple[float, int]] = []
    for off in range(30, pw_int - rx1):
        win = panel_l[ry0:ry1 + 1, off + rx0:off + rx1 + 1]
        if win.shape != piece_win.shape:
            continue
        w = win[mask]
        m = w.mean()
        d = float(np.sqrt(((w - m) ** 2).sum()))
        if d == 0:
            continue
        corr = float(((w - m) * (tpl - tpl_mean)).sum()) / (d * tpl_norm)
        scored.append((corr, off))
    scored.sort(reverse=True)

    # keep distinct peaks (>= 25px apart)
    picked: list[int] = []
    for score, off in scored:
        if all(abs(off - p) >= 25 for p in picked):
            picked.append(off)
        if len(picked) >= limit:
            break

    if not picked:
        # --- fallback: donut darkness contrast (row-local background) ---
        dark = []
        for y in range(ry0, ry1 + 1):
            lums = panel_l[y]
            out = np.empty(pw_int, dtype=np.float32)
            for x in range(30, pw_int):
                window = np.sort(lums[max(0, x - 25):min(pw_int, x + 25)])
                out[x] = max(0.0, float(window[int(len(window) * 0.8)]) - float(lums[x]))
            dark.append(out)
        dark = np.asarray(dark)
        piece_cols = np.arange(rx0, rx1 + 1)
        width = rx1 - rx0 + 1
        ring = 10
        scored2 = []
        for off in range(30, pw_int - width - 2):
            inside = float(dark[:, off + piece_cols].mean())
            left = dark[:, max(0, off - ring):off]
            right = dark[:, min(pw_int, off + width):min(pw_int, off + width + ring)]
            ring_mean = float(np.concatenate([left.ravel(), right.ravel()]).mean()) if left.size + right.size else 0.0
            scored2.append((inside - 0.5 * ring_mean, off))
        scored2.sort(reverse=True)
        for score, off in scored2:
            if all(abs(off - p) >= 25 for p in picked):
                picked.append(off)
            if len(picked) >= limit:
                break
    return picked


async def try_solve_slider_captcha(tab, candidate_index: int = 0) -> bool:
    """Detect and solve the CNKI slider captcha. Best effort, non-fatal.

    candidate_index picks which ranked gap offset to drag to; callers iterate
    over candidates across retries (the widget auto-refreshes on a miss).
    """

    try:
        raw = await asyncio.wait_for(
            tab.execute_script(CAPTCHA_PROBE_JS, return_by_value=True),
            timeout=8,
        )
    except Exception:
        return False
    probe = _unwrap(raw)
    if not isinstance(probe, dict) or not probe.get("move"):
        return False

    panel_png = piece_png = None
    candidates: list[int] = []
    try:
        panel_png = _png_from_data_uri(probe.get("panelSrc") or "")
        piece_png = _png_from_data_uri(probe.get("pieceSrc") or "")
        candidates = find_gap_candidates(panel_png, piece_png)
    except Exception:
        candidates = []

    move = probe["move"]
    panel = probe.get("panel") or {}
    bar = probe.get("bar") or {}
    panel_disp_w = panel.get("w") or 310
    bar_w = bar.get("w") or panel_disp_w
    start_x = move["x"] + move["w"] / 2
    start_y = move["y"] + move["h"] / 2

    if candidates:
        gap = candidates[min(candidate_index, len(candidates) - 1)]
        # Map the gap x from image pixels to displayed panel pixels, then
        # subtract the piece's current left offset relative to the panel.
        try:
            img_w = _decode_png_rgba(panel_png)[0]
        except Exception:
            img_w = panel_disp_w
        scale = panel_disp_w / img_w if img_w else 1.0
        piece_left_offset = move["x"] - panel.get("x", move["x"])
        distance = gap * scale - piece_left_offset - 2
    else:
        # Fallback: sweep the full track; some gap positions still pass.
        distance = bar_w - move["w"] - 4

    await _human_drag(tab, start_x, start_y, max(30, distance))
    await asyncio.sleep(1.2)

    # Solved when the verify box disappears.
    try:
        gone = await asyncio.wait_for(
            tab.execute_script(
                "!document.querySelector('.verifybox')", return_by_value=True
            ),
            timeout=5,
        )
        return bool(_unwrap(gone))
    except Exception:
        return False


async def _human_drag(tab, x: float, y: float, distance: float) -> None:
    """Drag the slider along a realistic human trajectory.

    Behavioral anti-bot checks on this widget family reject perfectly-uniform
    drags, so the motion mimics a hand: a short pause after press, an
    accelerate-decelerate velocity profile, uneven steps, a slight vertical
    drift, and a small overshoot corrected backwards before release.
    """

    import math

    from pydoll.commands import InputCommands

    async def mouse(kind, mx, my, count=1):
        await tab._execute_command(
            InputCommands.dispatch_mouse_event(
                type=kind, x=mx, y=my, button="left", click_count=count
            )
        )

    await mouse("mousePressed", x, y)
    await asyncio.sleep(random.uniform(0.08, 0.22))

    # Accelerate then decelerate; the easing exponent varies per attempt.
    power = random.uniform(1.8, 2.6)
    total_time = random.uniform(0.5, 1.1)
    overshoot = random.uniform(2.0, 7.0)
    drift = random.uniform(-6.0, 10.0)

    t = 0.0
    while t < total_time:
        t += random.uniform(0.008, 0.03)
        progress = min(t / total_time, 1.0)
        eased = progress ** power  # slow start, fast middle
        target_x = x + distance * eased
        # vertical drift follows a shallow arc
        target_y = y + drift * math.sin(progress * math.pi)
        await mouse("mouseMoved", target_x, target_y)
        await asyncio.sleep(random.uniform(0.006, 0.028))

    # overshoot past the gap, then correct back and release
    end_x = x + distance
    await mouse("mouseMoved", end_x + overshoot, y + drift * 0.15)
    await asyncio.sleep(random.uniform(0.04, 0.12))
    await mouse("mouseMoved", end_x, y)
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await mouse("mouseReleased", end_x, y)


async def _handle_captcha_if_present(tab, max_attempts: int = 5) -> bool:
    """Try to clear the slider captcha. Returns True when page is usable.

    Detection ranks several candidate gap offsets per captcha image. While the
    image stays the same (compare panelSrc) we advance through the ranking;
    when the widget refreshes it, we restart from the top candidate.

    When auto-solving fails, the browser window is headful on purpose: we then
    wait for the user to drag the slider manually (up to
    PAPER_TOOL_CNKI_MANUAL_WAIT seconds, default 120) and continue as soon as
    the verify box disappears.
    """

    last_panel_src = None
    candidate_index = 0
    for _ in range(max_attempts):
        try:
            present = await asyncio.wait_for(
                tab.execute_script(
                    "!!document.querySelector('.verifybox')", return_by_value=True
                ),
                timeout=5,
            )
        except Exception:
            return True
        if not _unwrap(present):
            return True

        try:
            probe = _unwrap(
                await asyncio.wait_for(
                    tab.execute_script(CAPTCHA_PROBE_JS, return_by_value=True),
                    timeout=8,
                )
            )
            panel_src = (probe or {}).get("panelSrc")
        except Exception:
            panel_src = None
        if panel_src != last_panel_src:
            candidate_index = 0
            last_panel_src = panel_src
        else:
            candidate_index += 1

        solved = await try_solve_slider_captcha(tab, candidate_index=candidate_index)
        if solved:
            await asyncio.sleep(1.0)
            return True
        # the widget usually auto-refreshes after a miss; also try the manual
        # refresh button so the next attempt sees a genuinely new image
        await asyncio.sleep(1.0)
        try:
            refresh = await tab.query(".verify-refresh", timeout=2, raise_exc=False)
            if refresh:
                await refresh.click()
        except Exception:
            pass
        await asyncio.sleep(0.8)

    # Auto-solving failed: hand over to the user in the visible window.
    import os

    wait_seconds = int(os.getenv("PAPER_TOOL_CNKI_MANUAL_WAIT", "120"))
    event("captcha_manual_wait", f"等待人工完成滑块验证（{wait_seconds}s）", seconds=wait_seconds)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(1.5)
        try:
            gone = _unwrap(
                await asyncio.wait_for(
                    tab.execute_script(
                        "!document.querySelector('.verifybox')", return_by_value=True
                    ),
                    timeout=5,
                )
            )
        except Exception:
            return True
        if gone:
            await asyncio.sleep(1.0)
            return True
    return False


def event(stage: str, message: str, **extra) -> None:
    payload = {"stage": stage, "message": message, **extra}
    print("PAPER_TOOL_EVENT " + json.dumps(payload, ensure_ascii=False), flush=True)


async def _page_state(tab) -> dict:
    try:
        raw = await asyncio.wait_for(
            tab.execute_script(
                "(() => ({ title: document.title, url: location.href, "
                "text: document.body ? document.body.innerText.slice(0, 300) : '' }))()",
                return_by_value=True,
            ),
            timeout=8,
        )
    except Exception:
        return {}
    state = _unwrap(raw)
    return state if isinstance(state, dict) else {}


async def _collect_records(tab) -> list[dict]:
    try:
        raw = await asyncio.wait_for(
            tab.execute_script(RESULT_DISCOVERY_JS, await_promise=True, return_by_value=True),
            timeout=15,
        )
    except Exception:
        return []
    return (_unwrap(raw) or {}).get("records") or []


async def _type_and_submit(tab, query: str) -> bool:
    """Fallback interaction: type the query into the search box and press Enter.

    The input must hold keyboard focus when Enter is dispatched, so the element
    is clicked/focused explicitly after typing; a JS keyboard-event dispatch is
    kept as a last resort for pages that ignore CDP key events.
    """

    try:
        raw = await asyncio.wait_for(
            tab.execute_script(SEARCH_BOX_LOCATE_JS, return_by_value=True),
            timeout=8,
        )
    except Exception:
        return False
    selector = _unwrap(raw)
    if not selector:
        return False
    try:
        element = await tab.query(selector, timeout=5, raise_exc=False)
        if element is None:
            return False
        try:
            await element.click()
        except Exception:
            pass
        try:
            await element.clear()
        except Exception:
            pass
        await element.insert_text(query)
        try:
            await element.focus()
        except Exception:
            pass
        await tab.keyboard.press(Key.ENTER)
        return True
    except Exception:
        return False


async def search_on_tab(tab, query: str, settings: Settings, settle: float) -> tuple[list[dict], list[dict]]:
    """Run the multi-strategy search on an existing tab.

    Returns (parsed_rows, attempts_diagnostics).
    """

    attempts: list[dict] = []
    for template in SEARCH_URL_TEMPLATES:
        url = template.format(query=quote_plus(query))
        attempt = {"url": template.split("?")[0], "strategy": "url_param"}
        try:
            await asyncio.wait_for(
                tab.go_to(url), timeout=settings.navigation_timeout_seconds
            )
        except Exception as exc:
            attempt["navigation_error"] = type(exc).__name__
        await asyncio.sleep(settle)

        if await _handle_captcha_if_present(tab):
            attempt["captcha"] = "cleared"
        else:
            attempt["captcha"] = "unsolved"

        records: list[dict] = []
        deadline = time.monotonic() + 14
        while time.monotonic() < deadline:
            records = await _collect_records(tab)
            if records:
                break
            await asyncio.sleep(1.2)
        attempt["records"] = len(records)

        if not records:
            # Second strategy on the same landing page: real keyboard input.
            if await _type_and_submit(tab, query):
                attempt["strategy"] += "+typed_submit"
                await _handle_captcha_if_present(tab)
                deadline = time.monotonic() + 14
                while time.monotonic() < deadline:
                    records = await _collect_records(tab)
                    if records:
                        break
                    await asyncio.sleep(1.2)
                attempt["records"] = len(records)

        state = await _page_state(tab)
        attempt["page_title"] = str(state.get("title") or "")[:80]
        attempts.append(attempt)
        if records:
            return parse_result_rows(records), attempts
    return [], attempts


def parse_result_rows(records: list[dict]) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for record in records:
        url = str(record.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        title = str(record.get("text") or "").strip()
        row_text = str(record.get("rowText") or "")
        doi_match = DOI_RE.search(row_text)
        year_match = YEAR_RE.search(row_text)
        rows.append(
            {
                "title": title or (row_text[:80] if row_text else url),
                "url": url,
                "doi": doi_match.group(0).rstrip(".,").lower() if doi_match else None,
                "year": year_match.group(0) if year_match else None,
                "snippet": row_text[:260],
                "source": "cnki",
            }
        )
    return rows[:20]


async def run_search(query: str, settings: Settings, timeout: float) -> dict:
    started = time.monotonic()
    # CNKI gates headless sessions with a slider captcha far more aggressively;
    # a persistent headful profile passes much more often and reuses the solved
    # anti-bot cookie across searches.
    worker = BrowserWorker(
        1, settings, persistent_profile=True, headless=False, profile_key="CNKI",
    )
    await worker.start()
    try:
        rows, attempts = await search_on_tab(
            worker.main_tab, query, settings, settings.settle_seconds + 1.0
        )
        return {
            "query": query,
            "count": len(rows),
            "rows": rows,
            "attempts": attempts,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
    finally:
        try:
            await worker.close()
        except Exception:
            try:
                await worker.force_cleanup()
            except Exception:
                pass


async def async_main() -> int:
    parser = argparse.ArgumentParser(description="CNKI keyword search subprocess")
    parser.add_argument("--query", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    settings = Settings.from_env()
    # The hard budget must cover auto-solve attempts plus the manual slider
    # fallback window, otherwise the parent would kill the browser mid-drag.
    manual_wait = int(os.getenv("PAPER_TOOL_CNKI_MANUAL_WAIT", "120"))
    budget = max(args.timeout, manual_wait + 90)
    try:
        payload = await asyncio.wait_for(
            run_search(args.query, settings, args.timeout), timeout=budget
        )
    except asyncio.TimeoutError:
        payload = {
            "query": args.query,
            "error": (
                f"cnki search exceeded {budget}s; if a slider CAPTCHA is shown "
                "in the popup Edge window, drag it manually and retry"
            ),
            "count": 0,
            "rows": [],
            "attempts": [],
        }
    write_json_atomic(Path(args.output), payload)
    print(json.dumps({"count": payload.get("count", 0)}, ensure_ascii=False), flush=True)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
