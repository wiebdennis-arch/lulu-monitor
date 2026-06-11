#!/usr/bin/env python3
"""
Lululemon Like New -> Discord monitor
=====================================
Watches one or more likenew.lululemon.com collection URLs (optionally with
size filters applied) and pings a Discord webhook whenever NEW items appear.

How it works
------------
likenew.lululemon.com is a JavaScript app (Archive resale platform) -- the
HTML is empty and products are loaded via background API calls. Instead of
guessing the API, this script launches a headless Chromium via Playwright,
opens your watch URLs, and intercepts the JSON responses the page itself
receives. Anything that looks like a product list is parsed, diffed against
previously-seen items, and new ones are sent to Discord as embeds.

Setup
-----
    pip install playwright requests
    playwright install chromium

    # put your webhook in the environment (don't hardcode it):
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/...."

    python likenew_monitor.py

First run seeds the database silently (no spam of 500 pings). Every run
after that only alerts on genuinely new item IDs.

Tip: open the site in your browser, apply your size/category filters,
copy the URL from the address bar, and add it to WATCH_URLS below.

Debugging
---------
    DEBUG=1 python likenew_monitor.py
dumps every captured API response to ./debug_responses/ so you can inspect
the exact JSON shape and tighten the field mapping if needed.
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

WATCH_URLS = [("Men size L", "https://likenew.lululemon.com/collections/men?productFilters=%7B%22attribute%22%3A%5B%7B%22display%22%3A%22Men%22%2C%22id%22%3A%224a3efa56-6572-50e6-8d63-9d239cd5ef23%22%2C%22type%22%3A%22gender%22%7D%2C%7B%22display%22%3A%22L%22%2C%22id%22%3A%22L%22%2C%22metadata%22%3A%7B%22size-type%22%3A%7B%22clothing%22%3A18%7D%7D%2C%22type%22%3A%22size-grouping%22%7D%5D%2C%22sortBy%22%3A%22latest%22%7D")]

POLL_INTERVAL_SECONDS = 600          # 10 minutes
JITTER_SECONDS = 45                  # random +/- so requests don't look robotic
STATE_FILE = Path("seen_items.json")
DEBUG = os.environ.get("DEBUG") == "1"
DEBUG_DIR = Path("debug_responses")

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

PAGE_LOAD_TIMEOUT_MS = 45_000
SETTLE_SECONDS = 6                   # wait after load for XHRs to finish

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
    return {"seen_ids": {}, "first_run_done": False}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


# ----------------------------------------------------------------------------
# Product extraction from intercepted API JSON
# ----------------------------------------------------------------------------

PRODUCT_KEY_HINTS = {"title", "name", "productName", "product_title"}
PRICE_KEY_HINTS = {"price", "salePrice", "sale_price", "currentPrice", "listPrice"}


def looks_like_product(d: dict) -> bool:
    keys = set(d.keys())
    return bool(keys & PRODUCT_KEY_HINTS) and bool(keys & PRICE_KEY_HINTS or
                                                   any("price" in k.lower() for k in keys))


def find_product_lists(obj, found: list) -> None:
    """Recursively walk JSON; collect any list of product-looking dicts.
    Handles Algolia-style {'results':[{'hits':[...]}]} and custom shapes."""
    if isinstance(obj, dict):
        for v in obj.values():
            find_product_lists(v, found)
    elif isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if dicts and sum(looks_like_product(d) for d in dicts) >= max(1, len(dicts) // 2):
            found.extend(d for d in dicts if looks_like_product(d))
        else:
            for v in obj:
                find_product_lists(v, found)


def first_of(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def normalize(raw: dict) -> dict | None:
    """Map a raw API product dict to our normalized shape."""
    pid = first_of(raw, "objectID", "id", "uuid", "itemId", "item_id",
                   "sku", "variantId", "listingId")
    title = first_of(raw, "title", "name", "productName", "product_title")
    if not title:
        return None
    if pid is None:
        # fall back to a stable composite key
        pid = f"{title}|{first_of(raw, 'size', 'sizeLabel', default='')}|" \
              f"{first_of(raw, 'price', 'salePrice', default='')}"
    pid = str(pid)

    price = first_of(raw, "price", "salePrice", "sale_price", "currentPrice")
    if isinstance(price, dict):
        price = first_of(price, "amount", "value", "current")
    compare = first_of(raw, "compareAtPrice", "compare_at_price",
                       "originalPrice", "listPrice", "msrp")
    if isinstance(compare, dict):
        compare = first_of(compare, "amount", "value")

    size = first_of(raw, "size", "sizeLabel", "size_label", "variantSize")
    if size is None and isinstance(raw.get("variant"), dict):
        size = first_of(raw["variant"], "size", "title")

    handle = first_of(raw, "handle", "slug", "urlKey", "url_key")
    url = first_of(raw, "url", "productUrl")
    if not url and handle:
        url = f"https://likenew.lululemon.com/products/{handle}"
    if url and url.startswith("/"):
        url = "https://likenew.lululemon.com" + url

    image = first_of(raw, "image", "imageUrl", "image_url", "thumbnail")
    if isinstance(image, dict):
        image = first_of(image, "url", "src")
    if isinstance(raw.get("images"), list) and raw["images"]:
        img0 = raw["images"][0]
        image = img0 if isinstance(img0, str) else first_of(img0, "url", "src", default=image)
    if image and image.startswith("//"):
        image = "https:" + image

    condition = first_of(raw, "condition", "conditionLabel", "quality")
    color = first_of(raw, "color", "colour", "colorName")

    return {
        "id": pid,
        "title": str(title),
        "size": str(size) if size is not None else None,
        "price": price,
        "compare_at": compare,
        "url": url,
        "image": image,
        "condition": condition,
        "color": color,
    }


# ----------------------------------------------------------------------------
# Scraping via headless browser + response interception
# ----------------------------------------------------------------------------

API_URL_BLOCKLIST = re.compile(
    r"(sentry|analytics|cloudinary|googletagmanager|gtm\.js|facebook|segment|"
    r"datadog|hotjar|clarity|doubleclick|\.css|\.js$|\.png|\.jpg|\.webp|"
    r"\.svg|\.woff)", re.I)


def fetch_products(play, label: str, url: str) -> list[dict]:
    captured: list[tuple[str, object]] = []

    browser = play.chromium.launch(headless=True)
    context = browser.new_context(user_agent=USER_AGENT,
                                  viewport={"width": 1366, "height": 900},
                                  locale="en-US")
    page = context.new_page()

    def on_response(resp):
        try:
            if API_URL_BLOCKLIST.search(resp.url):
                return
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                captured.append((resp.url, resp.json()))
            elif "text" in ctype or "component" in ctype:
                # some frameworks ship JSON with a text content-type
                txt = resp.text()
                if txt and txt.lstrip()[:1] in "[{":
                    try:
                        captured.append((resp.url, json.loads(txt)))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass

    page.on("response", on_response)

    dom_products: list[dict] = []
    try:
        page.goto(url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(SETTLE_SECONDS * 1000)
        # scroll a few times to trigger lazy-loading
        for _ in range(4):
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(1500)

        # dismiss the newsletter popup if it's covering the page
        try:
            btn = page.get_by_text("No thanks", exact=False).first
            if btn.is_visible(timeout=2000):
                btn.click()
                page.wait_for_timeout(800)
        except Exception:
            pass

        # Read product cards straight from the rendered page.
        # Like New links look like:
        #   /productStyle/<slug>---resale---<uuid>?selectedVariants={"size":{"option":"L"},...}
        dom_products = page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll('a[href*="/productStyle/"]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const idM = href.match(/---resale---([0-9a-f\\-]{36})/i);
                    if (!idM) return;

                    let size = null, color = null;
                    try {
                        const sv = new URL(href, location.origin)
                                     .searchParams.get('selectedVariants');
                        if (sv) {
                            const v = JSON.parse(sv);
                            size = (v.size && v.size.option) || null;
                            color = (v.color && v.color.option) || null;
                        }
                    } catch (e) {}

                    const key = idM[1] + '|' + (size||'') + '|' + (color||'');
                    if (seen.has(key)) return;
                    seen.add(key);

                    // walk up until we find the card with title + price text
                    let card = a, hops = 0;
                    while (card.parentElement && hops < 5 &&
                           !(card.innerText || '').includes('$')) {
                        card = card.parentElement; hops++;
                    }
                    const text = (card.innerText || '').trim();
                    const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
                    const title = lines.find(l => l.length > 3 && !l.startsWith('$')
                                   && !/^Original Retail/i.test(l)) || null;
                    const priceM = text.match(/\\$\\s?(\\d+[\\.,]?\\d*)/);
                    const retailM = text.match(/Original Retail\\s*\\$?\\s?(\\d+[\\.,]?\\d*)/i);
                    const img = card.querySelector('img');

                    out.push({
                        key: key,
                        href: href,
                        title: title,
                        price: priceM ? priceM[1] : null,
                        compare: retailM ? retailM[1] : null,
                        size: size,
                        color: color,
                        image: img ? (img.currentSrc || img.src || null) : null
                    });
                });
                return out;
            }"""
        )

        # DIAGNOSTICS: save what the invisible browser actually sees,
        # so problems (cookie walls, region blocks, layout changes) are
        # visible at a glance in last_page.png / page_dump.txt
        try:
            page.screenshot(path="last_page.png", full_page=False)
            dump = page.evaluate(
                """() => {
                    const links = [...document.querySelectorAll('a[href]')]
                        .map(a => a.getAttribute('href')).slice(0, 120);
                    return {
                        title: document.title,
                        url: location.href,
                        nLinks: document.querySelectorAll('a[href]').length,
                        links: links,
                        bodyText: (document.body.innerText || '').slice(0, 3000)
                    };
                }"""
            )
            Path("page_dump.txt").write_text(
                json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[warn] diagnostics failed: {e}")
    except Exception as e:
        print(f"[error] page load failed for '{label}': {e}")
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
        print(f"[debug] dumped {len(captured)} API responses to {DEBUG_DIR}/")

    products: dict[str, dict] = {}
    for _, body in captured:
        raw_hits: list = []
        find_product_lists(body, raw_hits)
        for raw in raw_hits:
            p = normalize(raw)
            if p:
                products[p["id"]] = p
    api_count = len(products)

    # merge DOM-extracted results (only adds items the API parse missed)
    for d in dom_products or []:
        href = d.get("href") or ""
        title = d.get("title")
        key = d.get("key")
        if not title or not key:
            continue
        pid = f"dom:{key}"
        if pid in products:
            continue
        full_url = href if href.startswith("http") else "https://likenew.lululemon.com" + href
        products[pid] = {
            "id": pid,
            "title": title,
            "size": d.get("size"),
            "price": d.get("price"),
            "compare_at": d.get("compare"),
            "url": full_url,
            "image": d.get("image"),
            "condition": None,
            "color": d.get("color"),
        }

    print(f"[info] '{label}': captured {len(captured)} JSON responses, "
          f"parsed {api_count} via API + {len(products) - api_count} via page "
          f"= {len(products)} products")
    return list(products.values())


# ----------------------------------------------------------------------------
# Discord
# ----------------------------------------------------------------------------

def fmt_price(v) -> str:
    try:
        f = float(v)
        return f"${f:,.2f}"
    except (TypeError, ValueError):
        return str(v) if v else "?"


def send_discord(new_items: list[dict], label: str) -> None:
    if not WEBHOOK_URL:
        print("[warn] DISCORD_WEBHOOK_URL not set -- printing instead:")
        for p in new_items:
            print("   NEW:", p["title"], p.get("size"), fmt_price(p.get("price")))
        return

    # Discord allows max 10 embeds per message
    for chunk_start in range(0, len(new_items), 10):
        chunk = new_items[chunk_start:chunk_start + 10]
        embeds = []
        for p in chunk:
            desc_parts = []
            if p.get("size"):
                desc_parts.append(f"**Size:** {p['size']}")
            if p.get("color"):
                desc_parts.append(f"**Color:** {p['color']}")
            if p.get("condition"):
                desc_parts.append(f"**Condition:** {p['condition']}")
            price_line = fmt_price(p.get("price"))
            if p.get("compare_at"):
                price_line += f"  (was {fmt_price(p['compare_at'])})"
            desc_parts.append(f"**Price:** {price_line}")

            embed = {
                "title": p["title"][:256],
                "description": "\n".join(desc_parts)[:2048],
                "color": 0xC8102E,  # lululemon red
                "footer": {"text": f"Like New • {label}"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if p.get("url") and str(p["url"]).startswith("http"):
                embed["url"] = p["url"]
            img = p.get("image")
            if img and str(img).startswith("http"):
                embed["thumbnail"] = {"url": img}
            embeds.append(embed)

        payload = {"content": f"🛎️ **{len(chunk)} new item(s)** in *{label}*",
                   "embeds": embeds}
        for attempt in range(3):
            r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
            if r.status_code == 429:
                wait = r.json().get("retry_after", 2)
                time.sleep(float(wait) + 0.5)
                continue
            if r.status_code >= 400:
                print(f"[error] Discord webhook {r.status_code}: {r.text[:200]}")
            break
        time.sleep(1)  # be polite between chunks


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------

def run_once(play, state: dict) -> None:
    for label, url in WATCH_URLS:
        seen: dict = state["seen_ids"].setdefault(label, {})
        seeding = len(seen) == 0   # first successful fetch for this label
        products = fetch_products(play, label, url)

        if not products:
            print(f"[warn] '{label}' returned 0 products -- site may have "
                  f"changed or page didn't finish loading; skipping diff")
            continue

        new_items = [p for p in products if p["id"] not in seen]
        now = datetime.now(timezone.utc).isoformat()
        for p in products:
            seen.setdefault(p["id"], now)

        if seeding:
            print(f"[info] first successful check: seeded {len(products)} items "
                  f"for '{label}' (no notifications)")
        elif new_items:
            print(f"[ALERT] {len(new_items)} new item(s) in '{label}'")
            send_discord(new_items, label)
        else:
            print(f"[info] '{label}': no new items")

        # prune very old entries so the state file doesn't grow forever
        if len(seen) > 5000:
            oldest = sorted(seen.items(), key=lambda kv: kv[1])[:len(seen) - 4000]
            for k, _ in oldest:
                del seen[k]

    state.pop("first_run_done", None)
    save_state(state)


def main() -> None:
    if not WEBHOOK_URL:
        print("NOTE: DISCORD_WEBHOOK_URL is not set. The script will run and "
              "log new items to the console only.\n")

    state = load_state()

    if os.environ.get("RUN_ONCE") == "1":
        # cloud mode (GitHub Actions): one check, then exit
        with sync_playwright() as play:
            run_once(play, state)
        return

    print(f"Monitoring {len(WATCH_URLS)} URL(s) every "
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
            elapsed = time.time() - start
            sleep_for = max(60, POLL_INTERVAL_SECONDS - elapsed +
                            random.uniform(-JITTER_SECONDS, JITTER_SECONDS))
            print(f"[info] sleeping {sleep_for:.0f}s\n")
            try:
                time.sleep(sleep_for)
            except KeyboardInterrupt:
                print("\nbye")
                sys.exit(0)


if __name__ == "__main__":
    main()
