#!/usr/bin/env python3
"""
Analyst Upgrades -> Discord monitor
===================================
Watches Benzinga's public analyst ratings page and pings a Discord webhook
when Goldman Sachs, UBS, or Bank of America UPGRADE a stock.

Same architecture as the lululemon monitor: headless Chromium loads the
page, we intercept the JSON the page itself fetches, filter, diff against
seen_ratings.json, notify.

Env:
    DISCORD_WEBHOOK_URL  (required for pings; falls back to console)
    RATINGS_WEBHOOK_URL  (optional: separate channel just for ratings)
    RUN_ONCE=1           (cloud mode: one check, then exit)
    DEBUG=1              (dump captured API responses + page screenshot)
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

PAGE_URL = "https://www.benzinga.com/analyst-stock-ratings"

# substrings matched case-insensitively against the analyst-firm name
WATCH_FIRMS = {
    "Goldman Sachs": ["goldman"],
    "UBS": ["ubs"],
    "Bank of America": ["b of a", "bofa", "bank of america", "bank of amer"],
}

# which actions count; Benzinga uses values like "Upgrades", "Downgrades",
# "Initiates Coverage On", "Maintains", "Reiterates"
WATCH_ACTIONS = ["upgrade"]   # substring match, so "Upgrades" hits

POLL_INTERVAL_SECONDS = 600
JITTER_SECONDS = 45
STATE_FILE = Path("seen_ratings.json")
DEBUG = os.environ.get("DEBUG") == "1"
DEBUG_DIR = Path("debug_ratings")

WEBHOOK_URL = (os.environ.get("RATINGS_WEBHOOK_URL")
               or os.environ.get("DISCORD_WEBHOOK_URL", "")).strip()

PAGE_LOAD_TIMEOUT_MS = 60_000
SETTLE_SECONDS = 8

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            print("[warn] state file corrupt, starting fresh")
    return {"seen_ids": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


# ----------------------------------------------------------------------------
# Extract rating events from intercepted JSON
# ----------------------------------------------------------------------------

def looks_like_rating(d: dict) -> bool:
    keys = {k.lower() for k in d.keys()}
    has_ticker = bool(keys & {"ticker", "symbol", "stock"})
    has_rating = any("rating" in k or "action" in k or "analyst" in k
                     for k in keys)
    return has_ticker and has_rating


def find_rating_lists(obj, found: list) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            find_rating_lists(v, found)
    elif isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if dicts and sum(looks_like_rating(d) for d in dicts) >= max(1, len(dicts) // 2):
            found.extend(d for d in dicts if looks_like_rating(d))
        else:
            for v in obj:
                find_rating_lists(v, found)


def first_of(d: dict, *keys, default=None):
    lower = {k.lower(): v for k, v in d.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v not in (None, ""):
            return v
    return default


def normalize(raw: dict) -> dict | None:
    ticker = first_of(raw, "ticker", "symbol", "stock")
    firm = first_of(raw, "analyst", "analyst_name", "firm", "company_name")
    action = first_of(raw, "action_company", "action", "rating_action", default="")
    if not ticker or not firm:
        return None
    return {
        "ticker": str(ticker).upper(),
        "firm": str(firm),
        "action": str(action),
        "rating_prior": first_of(raw, "rating_prior", "prior_rating"),
        "rating_current": first_of(raw, "rating_current", "current_rating", "rating"),
        "pt_prior": first_of(raw, "pt_prior", "price_target_prior"),
        "pt_current": first_of(raw, "pt_current", "price_target", "pt"),
        "date": first_of(raw, "date", "created", "updated", default=""),
        "time": first_of(raw, "time", default=""),
        "name": first_of(raw, "name", "company", "company_name_full", default=""),
        "notes": first_of(raw, "notes", default=""),
        "url": first_of(raw, "url", "url_news", default=""),
    }


def firm_label(firm_name: str) -> str | None:
    """Return our label if this firm is on the watch list, else None."""
    f = firm_name.lower()
    for label, needles in WATCH_FIRMS.items():
        if any(n in f for n in needles):
            return label
    return None


def is_watched(ev: dict) -> bool:
    if not firm_label(ev["firm"]):
        return False
    action = (ev["action"] or "").lower()
    return any(a in action for a in WATCH_ACTIONS)


def event_id(ev: dict) -> str:
    return f"{ev['date']}|{ev['ticker']}|{ev['firm']}|{ev['rating_current']}"


# ----------------------------------------------------------------------------
# Scrape via headless browser
# ----------------------------------------------------------------------------

BLOCKLIST = re.compile(
    r"(sentry|analytics|googletagmanager|gtm\.js|facebook|segment|datadog|"
    r"hotjar|clarity|doubleclick|adservice|prebid|\.css|\.js$|\.png|\.jpg|"
    r"\.webp|\.svg|\.woff)", re.I)


def fetch_ratings(play) -> list[dict]:
    captured: list[tuple[str, object]] = []

    browser = play.chromium.launch(headless=True)
    context = browser.new_context(user_agent=USER_AGENT,
                                  viewport={"width": 1440, "height": 1000},
                                  locale="en-US",
                                  timezone_id="America/New_York")
    page = context.new_page()

    def on_response(resp):
        try:
            if BLOCKLIST.search(resp.url):
                return
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                captured.append((resp.url, resp.json()))
            elif "text" in ctype:
                txt = resp.text()
                if txt and txt.lstrip()[:1] in "[{":
                    try:
                        captured.append((resp.url, json.loads(txt)))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass

    page.on("response", on_response)

    try:
        page.goto(PAGE_URL, timeout=PAGE_LOAD_TIMEOUT_MS,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(SETTLE_SECONDS * 1000)
        for _ in range(2):
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(1500)

        # diagnostics: what did the robot actually see?
        try:
            page.screenshot(path="last_ratings_page.png", full_page=False)
            dump = page.evaluate(
                """() => ({
                    title: document.title,
                    url: location.href,
                    bodyText: (document.body.innerText || '').slice(0, 3000)
                })"""
            )
            Path("ratings_page_dump.txt").write_text(
                json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[warn] diagnostics failed: {e}")
    except Exception as e:
        print(f"[error] page load failed: {e}")
    finally:
        context.close()
        browser.close()

    if DEBUG:
        DEBUG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        for i, (u, body) in enumerate(captured):
            safe = re.sub(r"\W+", "_", u)[:80]
            (DEBUG_DIR / f"{stamp}_{i}_{safe}.json").write_text(
                json.dumps(body, indent=2)[:500_000])
        print(f"[debug] dumped {len(captured)} responses to {DEBUG_DIR}/")

    events: dict[str, dict] = {}
    for _, body in captured:
        raw_hits: list = []
        find_rating_lists(body, raw_hits)
        for raw in raw_hits:
            ev = normalize(raw)
            if ev:
                events[event_id(ev)] = ev

    watched = [ev for ev in events.values() if is_watched(ev)]
    print(f"[info] captured {len(captured)} JSON responses, parsed "
          f"{len(events)} rating events, {len(watched)} match "
          f"GS/UBS/BofA upgrades")
    return watched


# ----------------------------------------------------------------------------
# Discord
# ----------------------------------------------------------------------------

def fmt_pt(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v) if v else None


def send_discord(new_events: list[dict]) -> None:
    if not WEBHOOK_URL:
        print("[warn] no webhook set -- printing instead:")
        for ev in new_events:
            print("   UPGRADE:", ev["firm"], ev["ticker"],
                  ev.get("rating_prior"), "->", ev.get("rating_current"))
        return

    for chunk_start in range(0, len(new_events), 10):
        chunk = new_events[chunk_start:chunk_start + 10]
        embeds = []
        for ev in chunk:
            label = firm_label(ev["firm"]) or ev["firm"]
            lines = []
            if ev.get("name"):
                lines.append(f"**Company:** {ev['name']}")
            if ev.get("rating_prior") or ev.get("rating_current"):
                lines.append(f"**Rating:** {ev.get('rating_prior') or '—'} → "
                             f"**{ev.get('rating_current') or '—'}**")
            pt_c, pt_p = fmt_pt(ev.get("pt_current")), fmt_pt(ev.get("pt_prior"))
            if pt_c:
                lines.append(f"**Price target:** {pt_c}"
                             + (f" (from {pt_p})" if pt_p else ""))
            if ev.get("date"):
                lines.append(f"**Date:** {ev['date']} {ev.get('time') or ''}".strip())

            embed = {
                "title": f"📈 {label} upgrades ${ev['ticker']}",
                "description": "\n".join(lines)[:2048],
                "color": 0x2ECC71,
                "footer": {"text": "Analyst Ratings Monitor"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            link = ev.get("url") or f"https://www.benzinga.com/quote/{ev['ticker']}"
            if str(link).startswith("http"):
                embed["url"] = link
            embeds.append(embed)

        payload = {"content": f"📊 **{len(chunk)} new analyst upgrade(s)**",
                   "embeds": embeds}
        for attempt in range(3):
            r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                continue
            if r.status_code >= 400:
                print(f"[error] Discord webhook {r.status_code}: {r.text[:200]}")
            break
        time.sleep(1)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def run_once(play, state: dict) -> None:
    seen: dict = state.setdefault("seen_ids", {})
    seeding = len(seen) == 0
    events = fetch_ratings(play)

    if not events and seeding:
        # could be a weekend/holiday with no matching upgrades -- seed anyway
        # only if we at least parsed the page; handled implicitly: nothing to add
        print("[info] no matching upgrades right now")

    new_events = [ev for ev in events if event_id(ev) not in seen]
    now = datetime.now(timezone.utc).isoformat()
    for ev in events:
        seen.setdefault(event_id(ev), now)

    if seeding and new_events:
        print(f"[info] first successful check: seeded {len(new_events)} "
              f"current upgrades (no notifications)")
    elif new_events:
        print(f"[ALERT] {len(new_events)} new upgrade(s)")
        send_discord(new_events)
    else:
        print("[info] no new upgrades")

    if len(seen) > 5000:
        oldest = sorted(seen.items(), key=lambda kv: kv[1])[:len(seen) - 4000]
        for k, _ in oldest:
            del seen[k]

    save_state(state)


def main() -> None:
    state = load_state()

    if os.environ.get("RUN_ONCE") == "1":
        with sync_playwright() as play:
            run_once(play, state)
        return

    print("Monitoring analyst upgrades (GS / UBS / BofA) every "
          f"{POLL_INTERVAL_SECONDS // 60} min. Ctrl+C to stop.\n")
    with sync_playwright() as play:
        while True:
            start = time.time()
            try:
                run_once(play, state)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"[error] cycle failed: {e}")
            sleep_for = max(60, POLL_INTERVAL_SECONDS - (time.time() - start) +
                            random.uniform(-JITTER_SECONDS, JITTER_SECONDS))
            print(f"[info] sleeping {sleep_for:.0f}s\n")
            try:
                time.sleep(sleep_for)
            except KeyboardInterrupt:
                print("\nbye")
                sys.exit(0)


if __name__ == "__main__":
    main()
