"""
Walmart CA monitor for Discord.

- Requests are a few seconds apart (see GAP_MIN / GAP_MAX).
- Checks use a real Chrome window. It moves on as soon as the product data is in the page.
- Alerts only post when the PRODUCT PAGE confirms: Walmart is the seller + in stock + real price.
- It always tells you WHY (in alerts, status messages, and with !why).
- You control it from Discord with commands (type !help in your alerts channel).
- If DASHBOARD_URL and BOT_API_KEY are set (Railway Variables), the DASHBOARD becomes the boss:
  it supplies the SKU list and the pause state, and gets a copy of every alert, error and status message.

Setup:    python3 -m pip install discord.py curl_cffi
Test:     python3 walmart_monitor.py test
Check one product:  python3 walmart_monitor.py debug <SKU>
Run:      python3 walmart_monitor.py

IMPORTANT for commands: in the Discord Developer Portal, open your app -> Bot ->
turn ON "Message Content Intent", then save. Without it the bot can't read commands.
"""
import os
import re
import sys
import json
import time
import random
import asyncio
import threading
import unicodedata
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlsplit, unquote

from curl_cffi.requests import AsyncSession

# ====== PUT YOUR SETTINGS HERE (keep the quotes) ======
DISCORD_TOKEN = ""     # leave empty: set DISCORD_TOKEN in Railway Variables
CHANNEL_ID = ""        # leave empty: set CHANNEL_ID in Railway Variables
ROLE_ID = ""           # role to ping (leave "" for no ping)
PROXY = ""             # optional: http://user:pass@host:port (leave "" for none)
ALLOWED_USER_IDS = []  # optional: [123456789012345678]. Empty = anyone with "Manage Server" can use commands
# =======================================================

GAP_MIN = 2                    # seconds between ANY two requests to Walmart...
GAP_MAX = 4                    # ...random between these two numbers

# Each SKU is checked at most once per this many seconds. Curl is fast, so the floor is what
# actually sets the pace. Raise it in Railway (MIN_SECONDS_PER_ROUND) if Walmart starts blocking.
MIN_SECONDS_PER_ROUND = int(os.environ.get("MIN_SECONDS_PER_ROUND", "15"))

ONLY_WALMART_SOLD = True       # alert only when the product page shows Walmart as the seller
PING_ON_NEW_ITEM = True        # ping the role for New Item alerts too, not just restocks

# Starting watchlist (after that, use !watch / !remove in Discord - changes are saved)
WATCH_SKUS = ["59T63OIG4M6A", "2B9H97KBBQQO"]

# Search scan hits Walmart's search pages, which return the bot check much sooner than a product page.
# Leave this off and watch specific SKUs. Set SEARCH_SCAN=1 to turn the Pokemon search back on.
SEARCH_SCAN = os.environ.get("SEARCH_SCAN", "0") != "0"
SEARCH_TERMS = ["pokemon", "pokemon tcg"]

# Each round can open a search page first and then use it as the referrer. Those search pages are
# what Walmart is challenging, so this stays off unless BROWSE_FIRST=1.
BROWSE_FIRST = os.environ.get("BROWSE_FIRST", "0") != "0"

# After a block: throw away the session and its cookies (1, default) or keep them (0).
# Keeping the flagged cookies left the bot blocked hundreds of times in a row.
FRESH_BROWSER_ON_BLOCK = os.environ.get("FRESH_BROWSER_ON_BLOCK", "1") != "0"
SEARCH_PAGES = 1
SEARCH_EVERY_N_ROUNDS = 3      # scan search results every 3rd round (watchlist is checked EVERY round)

MAX_VERIFY_PER_ROUND = 4       # product-page checks triggered by search results, per round
MAX_VERIFY_TRIES = 3
RECHECK_SECONDS = 120
# If Walmart shows its bot-check page, pause this many seconds, doubling each time up to BLOCK_RETRY_MAX.
# A homepage that still loads is not a recovery. Only a real product page clears the block.
BACKOFF_START = int(os.environ.get("BLOCK_RETRY_SECONDS", "60"))
BACKOFF_MAX = int(os.environ.get("BLOCK_RETRY_MAX", "600"))
BLOCK_UPDATE_SECONDS = 600     # while a block lasts, post a "still blocked" update this often

# Search scan only considers items whose name contains one of these (accents ignored, so "Pokémon" matches).
REQUIRE_WORDS = ["pokemon"]
EXCLUDE_WORDS = ["t-shirt", "shirt", " tee", "backpack", "slipper", "pencil", "balloon",
                 "jibbitz", "swim", "shoe", "costume", "bottle", "snack", "sock", "pajama",
                 "hoodie", "blanket", "bedding", "coloring", "plush", "mug", "lunch"]

# On Railway, files vanish on every redeploy unless you attach a Volume (mount it at /data and set DATA_DIR=/data)
DATA_DIR = os.environ.get("DATA_DIR", ".")
STATE_FILE = os.path.join(DATA_DIR, "walmart_state.json")
CONFIG_FILE = os.path.join(DATA_DIR, "walmart_config.json")
BASE = "https://www.walmart.ca"

# curl_cffi impersonates a real Chrome TLS/HTTP2 fingerprint. The product page still requires a real
# browser, so Chrome is the default. Set FETCHER=curl only to compare.
IMPERSONATE = os.environ.get("IMPERSONATE", "chrome").strip() or "chrome"
FETCHER = os.environ.get("FETCHER", "browser").strip().lower()
# A normal window completes Walmart's page check. HEADLESS=1 hides it.
# Mac terminals do not set DISPLAY, so a Mac defaults to a visible window.
if os.environ.get("HEADLESS"):
    HEADLESS = os.environ.get("HEADLESS") == "1"
elif sys.platform == "darwin":
    HEADLESS = False
else:
    HEADLESS = not os.environ.get("DISPLAY")
# How long to keep looking for the product data after the page starts loading. Returns sooner if it's there.
PAGE_WAIT_SECONDS = float(os.environ.get("PAGE_WAIT_SECONDS", "8"))
# With the dashboard connected, status messages ("Now watching", blocks, Online) go only to the dashboard and
# Discord gets only Restock / New Item alerts. Set STATUS_IN_DISCORD=1 to also post them in Discord.
STATUS_IN_DISCORD = os.environ.get("STATUS_IN_DISCORD", "0") == "1"
CONFIG_EVERY_SECONDS = (50, 70)   # how often the bot asks the dashboard for its settings (random in this range)
ERROR_REPEAT_SECONDS = 600        # don't send the same error to the dashboard more often than this

LOCAL_TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/Toronto"))   # Quebec time; Railway's clock is UTC
BLOCK_REASON = "Walmart's bot-check page appeared, so this check was paused."


def local_now():
    return datetime.now(LOCAL_TZ)


def page_name(job):
    if not job:
        return "unknown page"
    if job[0] == "home":
        return "the homepage"
    if job[0] in ("browse", "search"):
        return f"the search page for '{job[1]}'"
    return f"the product page for {job[1]}"


NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
SKU_RE = re.compile(r"^[A-Z0-9]{6,16}$")


class Blocked(Exception):
    """Walmart returned its bot-check page instead of real data."""


# ------------------------------------------------------------ pure helpers
def is_blocked(html):
    m = re.search(r"<title>(.*?)</title>", (html or "")[:4000], re.S | re.I)
    if not m:
        return False
    t = m.group(1).lower()
    return ("verify your ide" in t) or ("robot or human" in t) or ("access denied" in t)


def page_title(html):
    m = re.search(r"<title>(.*?)</title>", (html or "")[:4000], re.S | re.I)
    return (m.group(1).strip() if m else "")[:80]


def get_proxy():
    """Return a usable proxy URL. Accepts both 'user:pass@host:port' and 'http://user:pass@host:port'."""
    raw = (os.environ.get("PROXY") or PROXY or "").strip()
    if not raw:
        return None
    # Webshare / many providers give "user:pass@host:port" without the scheme
    if "://" not in raw:
        raw = "http://" + raw
    return raw


def proxies_for(proxy):
    return {"http": proxy, "https": proxy} if proxy else None


def search_url(term, page=1):
    return f"{BASE}/en/search?q={quote(term)}&sort=best_match&page={page}"


def excluded(name):
    """Search-scan filter: skip items without a required word (Walmart's "pokemon" results include
    sponsored toys like Toy Story) and items with an excluded word."""
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    if not any(w in n for w in REQUIRE_WORDS):
        return True
    return any(w in n for w in EXCLUDE_WORDS)


def parse_sku(s):
    """Accepts a SKU or a full Walmart link. Returns a clean SKU or None."""
    s = (s or "").strip().strip("`<>")
    if "/" in s:
        s = s.split("?")[0].rstrip("/").split("/")[-1]
    s = s.upper()
    return s if SKU_RE.match(s) else None


def decide(prev, ok, explicit, first_run):
    """'Restock' / 'New Item' if an alert should fire, else None.
    prev: last known 'Walmart in stock' (True/False/None). ok: it is now."""
    if not ok or prev is True:
        return None
    if first_run and not explicit:
        return None            # silent baseline on first start
    return "Restock" if (prev is False or explicit) else "New Item"


def reason_text(v, ok, status=None):
    if v is None:
        return (f"Couldn't read the product page (HTTP {status}), so I can't confirm the "
                f"price and won't alert.")
    seller = v.get("seller") or "a marketplace seller"
    if ok:
        if v.get("internal"):
            return f"Walmart is the seller and it's in stock at ${v['price']:.2f}."
        return f"In stock at ${v['price']:.2f} (seller: {seller})."
    if not v.get("in_stock"):
        return "Not in stock on the product page."
    return (f"In stock, but sold by {seller} at ${v['price']:.2f}, not Walmart. "
            f"Waiting for Walmart to take over.")


def extract_next_data(html):
    m = NEXT_DATA.search(html or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def parse_items(html):
    data = extract_next_data(html)
    if data is None:
        return None
    try:
        stacks = data["props"]["pageProps"]["initialData"]["searchResult"]["itemStacks"]
    except Exception:
        return None
    items = []
    for stack in stacks:
        for it in stack.get("items", []):
            if it.get("__typename") != "Product":
                continue
            avail = (it.get("availabilityStatusV2") or {}).get("value", "")
            items.append({
                "sku": it.get("usItemId"),
                "name": it.get("name") or "",
                "tile_price": it.get("price"),
                "in_stock": avail == "IN_STOCK" and bool(it.get("canAddToCart", False)),
                "tile_seller": it.get("sellerName"),
                "image": it.get("image"),
                "url": BASE + (it.get("canonicalUrl") or "").split("?")[0],
            })
    return items


def referer_headers(referer):
    return {"Referer": referer} if referer else None


async def fetch_home(session, proxy=None):
    r = await session.get(f"{BASE}/en", timeout=20, proxies=proxies_for(proxy))
    if is_blocked(r.text):
        raise Blocked(f'HTTP {r.status_code}, page title "{page_title(r.text)}"')


async def fetch_items(session, term, proxy=None, page=1, referer=None):
    r = await session.get(search_url(term, page), timeout=20, proxies=proxies_for(proxy),
                          headers=referer_headers(referer))
    if is_blocked(r.text):
        raise Blocked(f'HTTP {r.status_code}, page title "{page_title(r.text)}"')
    items = parse_items(r.text)
    if items is None:
        print(f"[{term}] no product data (status {r.status_code}). "
              f"Start of page: {r.text[:150]!r}")
    return items


def find_product_node(node, sku):
    if isinstance(node, dict):
        if node.get("usItemId") == sku and isinstance(node.get("priceInfo"), dict):
            return node
        for v in node.values():
            found = find_product_node(v, sku)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = find_product_node(v, sku)
            if found is not None:
                return found
    return None


def extract_price(node):
    pi = node.get("priceInfo") or {}
    cur = pi.get("currentPrice")
    if isinstance(cur, dict) and isinstance(cur.get("price"), (int, float)):
        return float(cur["price"])
    for key in ("linePrice", "itemPrice"):
        s = pi.get(key)
        if isinstance(s, str):
            m = re.search(r"\d[\d,]*\.?\d*", s)
            if m:
                return float(m.group().replace(",", ""))
    if isinstance(node.get("price"), (int, float)):
        return float(node["price"])
    return None


def verify(node):
    if not node:
        return None
    price = extract_price(node)
    if price is None:
        return None
    avail = node.get("availabilityStatus")
    if avail is None:
        v2 = node.get("availabilityStatusV2")
        avail = v2.get("value") if isinstance(v2, dict) else None
    img = (node.get("imageInfo") or {}).get("thumbnailUrl")
    return {
        "price": price,
        "name": node.get("name"),
        "seller": (node.get("sellerName") or node.get("sellerDisplayName")
                   or ("Walmart" if node.get("sellerType") == "INTERNAL" else "Marketplace seller")),
        "internal": node.get("sellerType") == "INTERNAL",
        "offer_id": node.get("offerId"),
        "cart_limit": node.get("orderLimit"),
        "in_stock": (avail == "IN_STOCK") if avail else None,
        "image": img,
    }


async def get_verified(session, sku, proxy, referer=None):
    r = await session.get(f"{BASE}/en/ip/-/{sku}", timeout=20, proxies=proxies_for(proxy),
                          headers=referer_headers(referer))
    if is_blocked(r.text):
        raise Blocked(f'HTTP {r.status_code}, page title "{page_title(r.text)}"')
    data = extract_next_data(r.text)
    node = find_product_node(data, sku) if data else None
    return verify(node), r.status_code, data


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def save_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(obj, f)
    except Exception as e:
        print(f"[warning] couldn't save {path}: {e}")


# ------------------------------------------------------------ http session
class PageResult:
    def __init__(self, text, status_code):
        self.text = text
        self.status_code = status_code


def browser_proxy(proxy):
    if not proxy:
        return None
    u = urlsplit(proxy)
    out = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        out["username"] = unquote(u.username)
        out["password"] = unquote(u.password or "")
    return out


class BrowserSession:
    """One real Chrome tab, reused. No fixed extra wait and no trip to a blank page between checks."""

    def __init__(self, proxy=None):
        self.proxy = proxy
        self._pw = self._browser = self._ctx = self._page = None
        self._crashed = False
        self._closing = False
        self._lock = asyncio.Lock()

    def _alive(self):
        return (self._page is not None and not self._crashed and not self._page.is_closed()
                and self._browser.is_connected())

    def _on_crash(self, _page):
        self._crashed = True
        print("[browser] the Walmart tab crashed")

    async def _start(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        args = ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage",
                "--disable-extensions", "--blink-settings=imagesEnabled=false", "--no-first-run",
                "--renderer-process-limit=1", "--disable-background-networking",
                "--disable-component-update", "--disable-gpu"]
        if sys.platform.startswith("linux"):
            # Chrome refuses to start as root inside Docker without these.
            args += ["--no-sandbox", "--disable-setuid-sandbox"]
        if self.proxy:
            args += ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                     "--webrtc-ip-handling-policy=disable_non_proxied_udp"]
        # On a server there is no Google Chrome, only Playwright's Chromium. On a Mac, use Chrome.
        default_channel = "chromium" if sys.platform.startswith("linux") else "chrome"
        prefer = os.environ.get("CHROME_CHANNEL", default_channel).strip() or default_channel
        channels = []
        for name in (prefer, "chrome", "msedge", "chromium"):
            if name not in channels:
                channels.append(name)
        last = None
        for channel in channels:
            try:
                self._browser = await self._pw.chromium.launch(
                    channel=channel, headless=HEADLESS, proxy=browser_proxy(self.proxy), args=args)
                print(f"[browser] using {channel} {self._browser.version}")
                last = None
                break
            except Exception as e:
                last = e
                print(f"[browser] couldn't start {channel}: {type(e).__name__}: {str(e).splitlines()[0][:160]}")
        if last is not None or self._browser is None:
            detail = str(last or "couldn't start Chrome")
            if "Executable doesn't exist" in detail or "BrowserType.launch" in detail:
                raise Blocked("Chrome is not installed in this container, so no product page can be opened.")
            raise last or RuntimeError("couldn't start Chrome")
        self._closing = False
        self._browser.on("disconnected", lambda _b: None if self._closing else
                         print("[browser] Chrome closed unexpectedly"))
        major = self._browser.version.split(".")[0]
        ua = (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
        self._ctx = await self._browser.new_context(
            user_agent=ua, locale="en-CA", timezone_id="America/Toronto",
            viewport={"width": 1366, "height": 768})
        self._page = await self._ctx.new_page()
        self._page.on("crash", self._on_crash)
        self._crashed = False

    async def _html_when_ready(self):
        """Return as soon as the product data is in the page. Don't sit out a fixed extra wait."""
        deadline = time.monotonic() + PAGE_WAIT_SECONDS
        while True:
            try:
                if await self._page.locator("#__NEXT_DATA__").count():
                    break
            except Exception:
                break
            if time.monotonic() >= deadline:
                break
            await self._page.wait_for_timeout(200)
        return await self._page.content()

    async def get(self, url, headers=None, timeout=20, proxies=None):
        async with self._lock:
            if self._page is None:
                await self._start()
            elif not self._alive():
                print("[browser] restarting Chrome")
                await self.close()
                await self._start()
            referer = (headers or {}).get("Referer")
            try:
                resp = await self._page.goto(url, referer=referer, wait_until="domcontentloaded",
                                             timeout=(timeout + 10) * 1000)
                html = await self._html_when_ready()
            except Exception as e:
                print(f"[browser] loading {url} failed: {type(e).__name__}: {str(e)[:300]}")
                raise
            return PageResult(html, resp.status if resp else 0)

    async def close(self):
        self._closing = True
        for obj in (self._ctx, self._browser):
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._pw = self._browser = self._ctx = self._page = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


def make_session(proxy=None):
    """Chrome by default. FETCHER=curl keeps the old request path."""
    if FETCHER == "curl":
        kwargs = {"impersonate": IMPERSONATE}
        if proxy:
            kwargs["proxy"] = proxy
        return AsyncSession(**kwargs)
    return BrowserSession(proxy)


# ------------------------------------------------------------ dashboard link
class Dashboard:
    """Talks to your Lovable dashboard. Header on every request: x-bot-key."""

    def __init__(self, base_url, key, session=None):
        self.base = (base_url or "").rstrip("/")
        self.headers = {"x-bot-key": key}
        self.session = session if session is not None else AsyncSession(impersonate="chrome")

    async def get_config(self):
        r = await self.session.get(f"{self.base}/api/public/bot/config", headers=self.headers, timeout=15)
        if r.status_code in (401, 403):
            raise RuntimeError(f"HTTP {r.status_code}: the dashboard rejected BOT_API_KEY (check the Railway variable)")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.json()

    async def post_log(self, kind, content, sku=None):
        body = {"kind": kind, "content": (content or "")[:1500]}
        if sku:
            body["sku"] = sku
        r = await self.session.post(f"{self.base}/api/public/bot/log", headers=self.headers, json=body, timeout=15)
        if r.status_code >= 300:
            raise RuntimeError(f"HTTP {r.status_code}")


class LogShipper:
    """Sends log lines to the dashboard in the background so Walmart checks never wait on it."""

    def __init__(self, dashboard, sleep=asyncio.sleep):
        self.dash = dashboard
        self.sleep = sleep
        self.q = asyncio.Queue(maxsize=300)

    def push(self, kind, content, sku=None):
        item = (kind, content, sku)
        try:
            self.q.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self.q.get_nowait()       # drop the oldest
                self.q.put_nowait(item)
            except Exception:
                pass

    async def run(self):
        while True:
            kind, content, sku = await self.q.get()
            for attempt in range(3):
                try:
                    await self.dash.post_log(kind, content, sku)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[dashboard] couldn't send a log line ({type(e).__name__}: {e}), try {attempt + 1}/3")
                    await self.sleep(5 * (attempt + 1))


# ------------------------------------------------------------ the monitor
class Monitor:
    """All the logic. Talks to Discord only through send_alert / send_text callbacks."""

    def __init__(self, session, send_alert, send_text, proxy=None,
                 clock=time.monotonic, sleep=asyncio.sleep, rand=random.uniform,
                 dashboard=None, shipper=None, session_factory=None, send_error=None):
        self.session = session
        self.send_error = send_error
        self.session_factory = session_factory
        self.referer = None
        self.dashboard = dashboard
        self.shipper = shipper
        self.synced_once = False
        self._err_seen = {}
        self.send_alert = send_alert
        self.send_text = send_text
        self.proxy = proxy
        self.clock = clock
        self.sleep = sleep
        self.rand = rand

        st = load_json(STATE_FILE)
        self.first_run = st is None
        self.state = st if isinstance(st, dict) else {}
        cfg = load_json(CONFIG_FILE)
        if isinstance(cfg, dict) and isinstance(cfg.get("watch"), list):
            self.watch = [parse_sku(s) for s in cfg["watch"] if parse_sku(s)]
        elif dashboard is not None:
            self.watch = []        # the dashboard decides; start empty until the first sync
        else:
            self.watch = [s for s in (parse_sku(x) for x in WATCH_SKUS) if s]

        self.paused = False
        self.blocked_until = 0.0
        self.backoff = BACKOFF_START
        self.was_blocked = False
        self.blocks_in_row = 0
        self.block_started = self.last_block_note = 0.0
        self.block_places = {}
        self.good_pages = 0
        self.last_request = clock() - GAP_MAX
        self.requests_made = 0
        self.round = deque()
        self.round_open = False
        self.next_round_at = 0.0
        self.round_no = 0
        self.priority = deque([("home",)])
        self.refresh = False
        self.reasons = {}      # sku -> (text, time string)
        self.names = {}
        self.checked = {}      # sku -> clock time of last product-page check
        self.verify_budget = MAX_VERIFY_PER_ROUND

    # ---- small helpers
    def dash_log(self, kind, content, sku=None):
        if self.shipper:
            self.shipper.push(kind, re.sub(r"[*`]", "", content), sku)

    def dash_error(self, key, content, sku=None):
        """Every error goes to the Discord error channel; the dashboard gets the same one at most every ERROR_REPEAT_SECONDS."""
        self.error_channel(content)
        now = self.clock()
        if now - self._err_seen.get(key, -1e9) < ERROR_REPEAT_SECONDS:
            return
        self._err_seen[key] = now
        self.dash_log("error", content, sku)

    def error_channel(self, text):
        """Copy an error to the Discord error channel (ERROR_CHANNEL_ID), without making the caller wait."""
        if self.send_error is None:
            return

        async def send():
            try:
                await self.send_error(text)
            except Exception as e:
                print(f"[discord] couldn't send to the error channel: {e}")
        try:
            asyncio.get_running_loop().create_task(send())
        except RuntimeError:
            pass

    async def _say(self, text, kind="info", sku=None):
        """Status messages. With the dashboard connected they go only there, so Discord just gets alerts."""
        print(f"[status] {text}")
        if not self.shipper or STATUS_IN_DISCORD:
            try:
                await self.send_text(text)
            except Exception as e:
                print(f"[discord] couldn't send message: {e}")
        self.dash_log(kind, text, sku)

    async def _alert(self, alert):
        try:
            await self.send_alert(alert)
        except Exception as e:
            print(f"[discord] couldn't send alert: {e}")
        v = alert["v"]
        self.dash_log("message",
                      f"{alert['kind']}: {alert['name']} | ${v['price']:.2f} | Seller: {v['seller']} | "
                      f"SKU {alert['sku']} | {alert['why']}", alert["sku"])

    def set_reason(self, sku, text):
        changed = self.reasons.get(sku, ("",))[0] != text
        self.reasons[sku] = (text, local_now().strftime("%I:%M:%S %p"))
        return changed

    def save_config(self):
        save_json(CONFIG_FILE, {"watch": self.watch})

    def round_estimate(self):
        jobs = len(self.watch)
        if SEARCH_SCAN:
            jobs += len(SEARCH_TERMS) * SEARCH_PAGES
        elif BROWSE_FIRST and jobs:
            jobs += 1
        return max(int(jobs * (GAP_MIN + GAP_MAX) / 2), MIN_SECONDS_PER_ROUND if jobs else 0)

    async def wait_turn(self):
        gap = self.rand(GAP_MIN, GAP_MAX)
        wait = self.last_request + gap - self.clock()
        if wait > 0:
            await self.sleep(wait)
        self.last_request = self.clock()
        self.requests_made += 1

    # ---- rounds
    def build_round(self):
        jobs = []
        if SEARCH_SCAN and self.round_no % SEARCH_EVERY_N_ROUNDS == 0:
            for term in SEARCH_TERMS:
                for page in range(1, SEARCH_PAGES + 1):
                    jobs.append(("search", term, page))
        elif BROWSE_FIRST and self.watch:
            jobs.append(("browse", random.choice(SEARCH_TERMS), 1))
        sku_jobs = [("sku", sku, False) for sku in self.watch]
        random.shuffle(sku_jobs)              # different order every round, less robotic
        jobs.extend(sku_jobs)
        self.round = deque(jobs)
        self.next_round_at = self.clock() + MIN_SECONDS_PER_ROUND
        self.round_no += 1
        self.verify_budget = MAX_VERIFY_PER_ROUND

    def finish_round(self):
        self.first_run = False
        save_json(STATE_FILE, self.state)

    def next_job(self):
        if self.refresh:
            self.refresh = False
            self.round_no = 0            # a refresh always includes the search scan
            self.build_round()
            self.round_open = True
        if self.priority:
            return self.priority.popleft()
        if not self.round:
            if self.round_open:
                self.finish_round()
                self.round_open = False
            if self.clock() < self.next_round_at:
                return None              # idle: don't re-check the same pages too soon
            self.build_round()
            self.round_open = True
        return self.round.popleft() if self.round else None

    async def run(self):
        while True:
            job = None
            try:
                if self.paused:
                    await self.sleep(1)
                    continue
                remaining = self.blocked_until - self.clock()
                if remaining > 0:
                    await self.sleep(min(5, remaining))
                    continue
                job = self.next_job()
                if job is None:
                    await self.sleep(1)
                    continue
                await self.wait_turn()
                if self.paused:                      # paused while we were waiting
                    self.priority.appendleft(job)
                    continue
                await self.run_job(job)
                self.good_pages += 1
                # Homepage and search often still load while product pages are challenged.
                # Don't call that a recovery or the pause resets and the same page is hit again.
                if self.was_blocked and job[0] in ("sku", "verify"):
                    self.was_blocked = False
                    self.backoff = BACKOFF_START
                    self.blocks_in_row = 0
                    mins = (self.clock() - self.block_started) / 60
                    tries = (f" after several blocked tries over {mins:.0f} min"
                             if mins >= 1 else "")
                    await self._say(f"Back to normal{tries}: product pages are loading again "
                                    f"({page_name(job)}).")
            except Blocked as b:
                first_block = not self.was_blocked
                self.was_blocked = True
                self.blocks_in_row = 1 if first_block else self.blocks_in_row + 1
                where = page_name(job)
                if first_block:
                    self.block_places = {}
                self.block_places[where] = self.block_places.get(where, 0) + 1
                good, self.good_pages = self.good_pages, 0
                wait = (f"{self.backoff / 60:.0f} min" if self.backoff >= 120 else f"{self.backoff:.0f} seconds")
                self.blocked_until = self.clock() + self.backoff
                if job is not None and job[0] != "home":
                    self.priority.appendleft(job)
                    if job[0] in ("sku", "verify"):
                        self.set_reason(job[1], BLOCK_REASON)
                await self.reset_session()
                msg = (f"Walmart is showing its bot-check page to this connection. Blocked on: {where}, "
                       f"after {good} page(s) loaded fine. I'm pausing ALL requests for {wait}, "
                       f"then I'll retry on my own. Walmart returned: {b}.")
                print(f"[BLOCKED] (#{self.blocks_in_row} in a row) {msg}")
                self.error_channel(f"BLOCKED (#{self.blocks_in_row} in a row): {msg}")
                now = self.clock()
                if first_block:          # one message when a block starts, then an update every 10 minutes
                    self.block_started = self.last_block_note = now
                    await self._say(msg, kind="error")
                elif now - self.last_block_note >= BLOCK_UPDATE_SECONDS:
                    self.last_block_note = now
                    await self._say(f"Still blocked by Walmart: {(now - self.block_started) / 60:.0f} min so far, "
                                    f"{self.blocks_in_row} tries in a row. Last blocked on: {where}. "
                                    f"Next retry in {wait}.", kind="error")
                self.backoff = min(self.backoff * 2, BACKOFF_MAX)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[error] {type(e).__name__}: {e}")
                self.dash_error(f"exc:{type(e).__name__}", f"Unexpected error in the bot: {type(e).__name__}: {e}")
                await self.sleep(5)

    async def reset_session(self):
        """After a block, go back through the homepage and drop the flagged cookies."""
        self.referer = None
        if FRESH_BROWSER_ON_BLOCK and self.session_factory is not None:
            try:
                await self.session.close()
            except Exception:
                pass
            self.session = self.session_factory()
        if not any(j[0] == "home" for j in self.priority):
            self.priority.appendleft(("home",))

    async def run_job(self, job):
        kind = job[0]
        if kind == "home":
            await fetch_home(self.session, self.proxy)
            self.referer = f"{BASE}/en"
        elif kind == "browse":
            await fetch_items(self.session, job[1], self.proxy, job[2], referer=self.referer)
            self.referer = search_url(job[1], job[2])
        elif kind == "search":
            await self.do_search(job[1], job[2])
            self.referer = search_url(job[1], job[2])
        elif kind == "sku":
            await self.check_sku(job[1], explicit=True, confirm=job[2])
        elif kind == "verify":
            await self.check_sku(job[1], explicit=False, meta=job[2])

    # ---- search scan: spots new listings / tiles that flip to in-stock
    def schedule_verify(self, sku, it):
        """True if handled (queued, already covered, or recently checked). False if out of budget."""
        if sku in self.watch:
            return True
        if any(j[0] == "verify" and j[1] == sku for j in self.priority):
            return True
        if self.clock() - self.checked.get(sku, -1e9) < RECHECK_SECONDS:
            return True
        if self.verify_budget <= 0:
            return False
        self.verify_budget -= 1
        self.priority.append(("verify", sku, {"name": it["name"], "url": it["url"],
                                              "img": it["image"], "tries": 0}))
        return True

    async def do_search(self, term, page):
        items = await fetch_items(self.session, term, self.proxy, page, referer=self.referer)
        if items is None:
            return
        for it in items:
            sku = it["sku"]
            if not sku or excluded(it["name"]):
                continue
            self.names[sku] = it["name"]
            prev = self.state.get(sku)
            if not isinstance(prev, dict):
                if self.first_run:
                    self.state[sku] = {"in_stock": it["in_stock"]}
                elif self.schedule_verify(sku, it):
                    self.state[sku] = {"in_stock": it["in_stock"]}
            elif (not prev["in_stock"]) and it["in_stock"]:
                if self.schedule_verify(sku, it):
                    prev["in_stock"] = True
            else:
                prev["in_stock"] = it["in_stock"]

    # ---- the product-page check (the only thing that can trigger an alert)
    async def check_sku(self, sku, explicit, confirm=False, meta=None):
        meta = meta or {}
        try:
            v, status, _ = await get_verified(self.session, sku, self.proxy, referer=self.referer)
        except Blocked:
            raise
        except Exception as e:
            detail = (str(e).strip().splitlines() or [""])[0][:150]
            v, status = None, f"error: {type(e).__name__}{': ' + detail if detail else ''}"
        self.checked[sku] = self.clock()

        if v is None:
            reason = reason_text(None, False, status)
            if self.set_reason(sku, reason):
                print(f"[not posted] {sku}: {reason}")
            self.dash_error(f"unreadable:{sku}", f"{sku}: {reason}", sku)
            if not explicit and meta.get("tries") is not None:
                tries = meta["tries"] + 1
                if tries < MAX_VERIFY_TRIES:
                    self.priority.append(("verify", sku, dict(meta, tries=tries)))
            if confirm:
                await self._say(f"Added `{sku}` to the watchlist, but: {reason}", sku=sku)
            return

        name = v["name"] or meta.get("name") or self.names.get(sku) or sku
        self.names[sku] = name
        ok = bool(v["in_stock"]) and (v["internal"] or not ONLY_WALMART_SOLD)
        key = f"watch:{sku}"
        prev = self.state.get(key)
        self.state[key] = ok
        prev_reason = self.reasons.get(sku, (None,))[0]
        reason = reason_text(v, ok)
        if self.set_reason(sku, reason):
            print(f"[{'watch' if explicit else 'verify'}] {sku}: {reason}")

        kind = decide(prev, ok, explicit, self.first_run)
        if kind:
            if kind == "Restock":
                why = reason
                if prev_reason and prev_reason != reason and prev_reason != BLOCK_REASON:
                    why += f" Before: {prev_reason}"
            else:
                why = "New listing found. " + reason
            await self._alert({
                "kind": kind, "name": name, "sku": sku, "v": v, "why": why,
                "url": meta.get("url") or f"{BASE}/en/ip/-/{sku}", "img": meta.get("img"),
            })
        if confirm:
            await self._say(f"Now watching **{name}** (`{sku}`). Right now: {reason}", sku=sku)

    # ---- dashboard sync
    def apply_config(self, cfg):
        """Apply the dashboard's settings: pause state + the Walmart SKU list."""
        if not isinstance(cfg, dict):
            return
        paused = bool(cfg.get("paused"))
        raw = cfg.get("skus")
        if isinstance(raw, list):
            new, labels = [], {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                store = str(item.get("store") or "walmart").lower()
                if not store.startswith("walmart"):
                    continue                      # Amazon / EB Games / Hobbies are "coming soon"
                sku = parse_sku(str(item.get("sku") or ""))
                if not sku:
                    self.dash_error(f"badsku:{item.get('sku')}",
                                    f"Ignoring '{item.get('sku')}': that isn't a valid Walmart SKU.")
                    continue
                if sku not in new:
                    new.append(sku)
                label = str(item.get("label") or "").strip()
                if label:
                    labels[sku] = label
            for sku, label in labels.items():
                self.names.setdefault(sku, label)
            added = [x for x in new if x not in self.watch]
            removed = [x for x in self.watch if x not in new]
            self.watch = new
            for sku in added:
                self.priority.append(("sku", sku, self.synced_once))   # confirm message only after the first sync
            for sku in removed:
                self.round = deque(j for j in self.round if not (j[0] == "sku" and j[1] == sku))
                self.priority = deque(j for j in self.priority if not (j[0] in ("sku", "verify") and j[1] == sku))
            if added or removed:
                self.save_config()
                if self.synced_once:
                    self.dash_log("info", f"Watchlist updated from the dashboard: {len(added)} added, "
                                          f"{len(removed)} removed, {len(new)} total.")
        if paused != self.paused:
            self.paused = paused
            if self.synced_once or paused:
                note = ("Paused from the dashboard. No requests to Walmart until you resume."
                        if paused else "Resumed from the dashboard.")
                print(f"[dashboard] {note}")
                self.dash_log("info", note)
        self.synced_once = True

    async def run_sync(self):
        """Every ~minute: ask the dashboard for settings (this also counts as the check-in)."""
        if not self.dashboard:
            return
        fails = 0
        while True:
            try:
                cfg = await self.dashboard.get_config()
                self.apply_config(cfg)
                if fails >= 3:
                    print("[dashboard] connection restored")
                fails = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                fails += 1
                print(f"[dashboard] couldn't get settings ({type(e).__name__}: {e}); keeping the last settings")
            await self.sleep(self.rand(*CONFIG_EVERY_SECONDS))

    # ---- Discord commands (returns reply text, or None if not a command)
    def command(self, text):
        parts = (text or "").strip().split()
        if not parts or not parts[0].startswith("!"):
            return None
        cmd, args = parts[0][1:].lower(), parts[1:]

        if self.dashboard and cmd in ("watch", "add", "remove", "unwatch", "delete", "pause", "resume"):
            return "The dashboard controls the SKU list and pause/resume now. Make that change there."

        if cmd in ("watch", "add"):
            if not args:
                return "Usage: `!watch SKU` (a SKU or a Walmart product link). You can list several."
            added, already, bad = [], [], []
            for a in args:
                sku = parse_sku(a)
                if not sku:
                    bad.append(a)
                elif sku in self.watch:
                    already.append(sku)
                else:
                    self.watch.append(sku)
                    self.priority.append(("sku", sku, True))
                    added.append(sku)
            if added:
                self.save_config()
            out = []
            if added:
                tail = " Resume with `!resume` to get the confirmation." if self.paused else \
                       " I'll check it next and tell you what it is and whether Walmart is selling it."
                out.append("Added: " + ", ".join(f"`{s}`" for s in added) + "." + tail)
            if already:
                out.append("Already watching: " + ", ".join(f"`{s}`" for s in already) + ".")
            if bad:
                out.append("Doesn't look like a SKU: " + ", ".join(f"`{b}`" for b in bad) + ".")
            return " ".join(out)

        if cmd in ("remove", "unwatch", "delete"):
            if not args:
                return "Usage: `!remove SKU`"
            removed, missing = [], []
            for a in args:
                sku = parse_sku(a)
                if sku and sku in self.watch:
                    self.watch.remove(sku)
                    self.round = deque(j for j in self.round if not (j[0] == "sku" and j[1] == sku))
                    self.priority = deque(j for j in self.priority if not (j[0] in ("sku", "verify") and j[1] == sku))
                    removed.append(sku)
                else:
                    missing.append(a)
            if removed:
                self.save_config()
            out = []
            if removed:
                out.append("Removed: " + ", ".join(f"`{s}`" for s in removed) + ".")
            if missing:
                out.append("Wasn't watching: " + ", ".join(f"`{m}`" for m in missing) + ".")
            return " ".join(out)

        if cmd == "list":
            if not self.watch:
                return "Not watching anything. Add one with `!watch SKU`."
            lines = []
            for sku in self.watch:
                name = (self.names.get(sku) or "name unknown yet")[:50]
                r = self.reasons.get(sku)
                lines.append(f"`{sku}` {name}: {r[0] if r else 'not checked yet'}")
            return f"Watching {len(self.watch)}:\n" + "\n".join(lines)

        if cmd == "pause":
            self.paused = True
            return "Paused. I'm making NO requests to Walmart until you send `!resume`."

        if cmd == "resume":
            self.paused = False
            return f"Resumed. Checks continue (each request {GAP_MIN:g}-{GAP_MAX:g} seconds apart)."

        if cmd == "refresh":
            self.refresh = True
            return (f"Refresh queued: I'll start a fresh round now ({'search scan + ' if SEARCH_SCAN else ''}every watched SKU). "
                    f"The first request goes out within {GAP_MAX} seconds, and a full round takes about "
                    f"{self.round_estimate()} seconds.")

        if cmd == "status":
            if self.paused:
                state = "PAUSED from the dashboard" if self.dashboard else "PAUSED by command"
            elif self.was_blocked:
                state = (f"BLOCKED by Walmart for {(self.clock() - self.block_started) / 60:.0f} min "
                         f"({self.blocks_in_row} tries in a row), retrying on its own")
            else:
                state = "running"
            since = int(self.clock() - self.last_request)
            return (f"Status: {state}\nWatching {len(self.watch)} SKU(s), each checked about every "
                    f"{self.round_estimate()}s (requests spaced {GAP_MIN}-{GAP_MAX}s apart).\n"
                    f"Requests made since start: {self.requests_made}. Last request {since}s ago.\n"
                    f"Queued extra checks: {len(self.priority)}.")

        if cmd == "why":
            if not args:
                return "Usage: `!why SKU`"
            sku = parse_sku(args[0])
            if not sku:
                return "That doesn't look like a SKU."
            r = self.reasons.get(sku)
            if not r:
                where = "I'm watching it but haven't checked it yet." if sku in self.watch else \
                        "I'm not watching it. Add it with `!watch`."
                return f"No decision recorded for `{sku}` yet. {where}"
            name = self.names.get(sku, sku)
            return f"**{name}** (`{sku}`), last checked {r[1]}: {r[0]}"

        if cmd == "help":
            return ("**Commands**\n"
                    "`!watch SKU` - start watching a product (SKU or Walmart link)\n"
                    "`!remove SKU` - stop watching it\n"
                    "`!list` - what I'm watching and why each isn't alerting\n"
                    "`!why SKU` - the reason for my latest decision on one product\n"
                    "`!pause` / `!resume` - stop or restart all requests\n"
                    "`!refresh` - start a fresh check round right now\n"
                    "`!status` - am I running, blocked or paused")
        return None


# ------------------------------------------------------------ test + debug
async def test_mode():
    async with make_session(get_proxy()) as s:
        for term in SEARCH_TERMS:
            items = await fetch_items(s, term, get_proxy())
            if items is None:
                continue
            print(f"\n[{term}] got {len(items)} products (search-tile data, NOT verified)")
            for it in items[:10]:
                flag = "IN STOCK" if it["in_stock"] else "out"
                print(f"  {it['sku']}  tile ${it['tile_price']}  {flag:8}  {it['tile_seller']}  {it['name'][:45]}")
            await asyncio.sleep(random.uniform(GAP_MIN, GAP_MAX))


async def debug_sku(sku):
    proxy = get_proxy()
    async with make_session(proxy) as s:
        print(f"Using Chrome. Homepage, then the product page.")
        await fetch_home(s, proxy)
        await asyncio.sleep(random.uniform(GAP_MIN, GAP_MAX))
        print("--- PRODUCT PAGE (what the bot will alert with) ---")
        v, status, data = await get_verified(s, sku, proxy, referer=f"{BASE}/en")
        print(f"  page status: {status}")
        if v is None:
            print("  COULD NOT READ A PRICE from the product page. The bot will NOT alert on this item.")
            if data is not None:
                fname = f"debug_{sku}.json"
                save_json(fname, data)
                print(f"  Saved the page data to {fname} so the layout can be checked.")
            return
        for k, val in v.items():
            if k != "image":
                print(f"  {k}: {val}")
        ok = bool(v["in_stock"]) and (v["internal"] or not ONLY_WALMART_SOLD)
        print(f"\n  Why: {reason_text(v, ok)}")
        print(f"  Would the bot alert right now? {'YES' if ok else 'no'}")


# ------------------------------------------------------------ discord bot
def listen_for_railway(port):
    """Railway restarts a container that never opens a port. This only answers 'ok'."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    try:
        HTTPServer(("0.0.0.0", port), Handler).serve_forever()
    except Exception as e:
        print(f"[health] could not listen on {port}: {e}", flush=True)


def run_bot():
    print("walmart monitor process started", flush=True)
    port = int(os.environ.get("PORT", "0") or "0")
    if port:
        threading.Thread(target=listen_for_railway, args=(port,), daemon=True).start()
        print(f"health check listening on {port}", flush=True)
    import discord

    token = os.environ.get("DISCORD_TOKEN") or DISCORD_TOKEN
    channel_raw = os.environ.get("CHANNEL_ID") or CHANNEL_ID
    role_id = os.environ.get("ROLE_ID") or ROLE_ID or None
    proxy = get_proxy()
    dash_url = os.environ.get("DASHBOARD_URL", "").strip()
    dash_key = os.environ.get("BOT_API_KEY", "").strip()
    if not token or not channel_raw:
        sys.exit("Missing DISCORD_TOKEN or CHANNEL_ID. Fill them in at the top of the file.")
    channel_id = int(channel_raw)
    error_channel_id = int(os.environ.get("ERROR_CHANNEL_ID", "").strip() or 0)   # every error is copied here

    intents = discord.Intents.default()
    intents.message_content = True      # needed to read !commands (enable it in the Developer Portal too)

    class Bot(discord.Client):
        async def setup_hook(self):
            dash = shipper = None
            if dash_url and dash_key:
                dash = Dashboard(dash_url, dash_key)
                shipper = LogShipper(dash)
            elif dash_url or dash_key:
                print("[dashboard] DASHBOARD_URL and BOT_API_KEY must BOTH be set. Running without the dashboard.")
            self.shipper = shipper
            def new_session():
                return make_session(proxy)
            self.mon = Monitor(new_session(), self.post_alert, self.post_text,
                               proxy, dashboard=dash, shipper=shipper, session_factory=new_session,
                               send_error=self.post_error if error_channel_id else None)
            self.bg = asyncio.create_task(self.run_monitor())

        async def run_monitor(self):
            try:
                await self.wait_until_ready()
                where = "dashboard connected" if self.mon.dashboard else "no dashboard, using Discord commands"

                # Show proxy status + real outbound IP in Discord
                proxy = get_proxy()
                if proxy:
                    # Mask password for the message
                    safe = proxy
                    try:
                        if "@" in proxy and "://" in proxy:
                            scheme, rest = proxy.split("://", 1)
                            creds, host = rest.rsplit("@", 1)
                            user = creds.split(":")[0] if ":" in creds else "****"
                            safe = f"{scheme}://{user}:****@{host}"
                    except Exception:
                        safe = "(proxy set)"
                    proxy_line = f"Proxy: `{safe}`"
                    try:
                        async with AsyncSession(impersonate="chrome") as s:
                            r = await s.get("https://api.ipify.org",
                                            proxies={"http": proxy, "https": proxy},
                                            timeout=12)
                            proxy_line += f"\nOutbound IP: `{r.text.strip()}`"
                    except Exception as e:
                        proxy_line += f"\nOutbound IP check failed: `{type(e).__name__}: {e}`"
                else:
                    proxy_line = "Proxy: **not set** (this computer's IP)"
                    try:
                        async with AsyncSession(impersonate="chrome") as s:
                            r = await s.get("https://api.ipify.org", timeout=12)
                            proxy_line += f"\nOutbound IP: `{r.text.strip()}`"
                    except Exception as e:
                        proxy_line += f"\nOutbound IP check failed: `{type(e).__name__}: {e}`"

                if FETCHER == "curl":
                    method = f"curl, Chrome fingerprint ({IMPERSONATE}). No browser."
                else:
                    mode = "hidden Chrome" if HEADLESS else "a normal Chrome window (leave it open)"
                    method = f"Chrome, {mode}. No extra wait after the product data loads."
                await self.mon._say(
                    f"Online ({where}).\n{proxy_line}\n"
                    f"Method: {method}\n"
                    f"Requests are {GAP_MIN:g}-{GAP_MAX:g}s apart, and each SKU is checked about every "
                    f"{self.mon.round_estimate()}s. Type `!help` for commands."
                )
                jobs = [self.mon.run(), self.mon.run_sync()]
                if self.shipper:
                    jobs.append(self.shipper.run())
                await asyncio.gather(*jobs)
            except Exception as e:
                print(f"[fatal] monitor stopped: {type(e).__name__}: {e}")

        async def post_text(self, text):
            ch = self.get_channel(channel_id)
            if ch is not None:
                await ch.send(text[:1900])

        async def post_error(self, text):
            ch = self.get_channel(error_channel_id)
            if ch is None:
                print(f"[discord] error channel {error_channel_id} not found (check ERROR_CHANNEL_ID and the bot's access)")
                return
            await ch.send(f"[{local_now():%I:%M %p}] {text}"[:1900])

        async def post_alert(self, a):
            v, kind = a["v"], a["kind"]
            e = discord.Embed(title=(a["name"] or a["sku"])[:250], url=a["url"],
                              color=0x2ECC71 if kind == "Restock" else 0x3498DB)
            e.set_author(name=f"Walmart CA - {kind}")
            e.add_field(name="Price", value=f"${v['price']:.2f}")
            e.add_field(name="Seller", value=v["seller"] or "?")
            e.add_field(name="Cart Limit", value=str(v["cart_limit"]))
            e.add_field(name="SKU", value=a["sku"])
            e.add_field(name="Offer ID", value=v["offer_id"] or "?", inline=False)
            e.add_field(name="Why this alert", value=a["why"][:1000], inline=False)
            img = v["image"] or a.get("img")
            if img:
                e.set_thumbnail(url=img)
            e.set_footer(text=f"Walmart CA - verified on product page - {local_now():%I:%M:%S %p}")
            ping = f"<@&{role_id}> " if role_id and (kind == "Restock" or PING_ON_NEW_ITEM) else ""
            ch = self.get_channel(channel_id)
            if ch is not None:
                await ch.send(ping, embed=e)

        def allowed(self, user):
            if ALLOWED_USER_IDS:
                return user.id in ALLOWED_USER_IDS
            perms = getattr(user, "guild_permissions", None)
            return bool(perms and perms.manage_guild)

        async def on_message(self, message):
            if message.author.bot or message.channel.id != channel_id:
                return
            if not message.content.startswith("!"):
                return
            if not self.allowed(message.author):
                await message.reply("You don't have permission to control this bot.", mention_author=False)
                return
            reply = self.mon.command(message.content)
            if reply is None:
                return
            await message.reply(reply[:1900], mention_author=False)

    Bot(intents=intents).run(token)


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "test":
            asyncio.run(test_mode())
        elif len(sys.argv) > 2 and sys.argv[1] == "debug":
            asyncio.run(debug_sku(sys.argv[2]))
        else:
            run_bot()
    except Blocked as b:
        print(f"\nBLOCKED ({b}): Walmart is showing its bot-check page to this connection right now.\n"
              "Stop the bot, wait 30-60 minutes (or switch to a different network, like a phone hotspot), "
              "then try again.")
