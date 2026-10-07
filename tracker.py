#!/usr/bin/env python3
"""
meebo tracker.

Watches two pages and pushes a notification when something changes:
  1. Three Priceless pages (Riot Games, LoL Esports, LoL Esports shop) -> alerts the
     moment ANY new experience/product link appears (all three are empty right now).
  2. Chase Cashback Moments Worlds page -> alerts when the event description block
     changes (Chase has no explicit "on sale" marker, so any edit is a signal).
  3. lolesports.com/news and Chase's media center -> alerts on new articles that mention
     Priceless / Mastercard / Chase Freedom / Fan Fest / presale, with a link to the article.
  4. Optional RSS feeds (e.g. an X account via rss.app) with the same keyword filter.

Stdlib only. Notifications via ntfy.sh (phone push) and/or a Discord webhook.

Usage:
  python tracker.py                 # loop forever, check every INTERVAL seconds
  python tracker.py --once          # single check (for cron / GitHub Actions)
  python tracker.py --once --only x       # just the X search (the server runs this every minute)
  python tracker.py --once --only sites   # everything but X (the server runs this every 10 minutes)
  python tracker.py --test-notify   # send a test push and exit
  python tracker.py --test-alarm    # send a test Pushover emergency alarm and exit
  python tracker.py --status        # print what the pages look like right now

Env vars:
  NTFY_TOPIC       ntfy.sh topic name (make it long and random — topics are public)
  NTFY_SERVER      optional, defaults to https://ntfy.sh
  DISCORD_WEBHOOK  optional Discord webhook URL
  PUSHOVER_USER    optional Pushover user key   } drop alerts also ring as a Pushover
  PUSHOVER_TOKEN   optional Pushover app token  } emergency alarm until acknowledged
  PUSHOVER_SOUND   optional, defaults to "persistent" (a long sound)
  QUIET_TZ         time zone for quiet hours (default America/Los_Angeles)
  QUIET_HOURS      quiet hours as START-END in 24h local time (default 0-6)

Only a ticket listing rings: a Pushover emergency alarm plus an urgent ntfy push. Everything else
(news, the Chase page, tracker warnings) is a normal push by day and a silent one in quiet hours.
  KEYWORDS         regex an article/post must match to alert (default: priceless/mastercard/...)
  X_RSS_FEEDS      optional comma list of RSS/Atom feed URLs (e.g. an rss.app feed of @LoLEsports)
  X_BEARER_TOKEN   optional X API bearer token: searches X for drop announcements (pay-per-use)
  X_QUERY          optional, overrides the X search query below
  INTERVAL         seconds between checks in loop mode (default 120)
  STATE_FILE       where to keep state (default state.json next to this script)
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

PRICELESS_PAGES = {
    "Riot Games": "https://www.priceless.com/celebrity/22136/riot-games",
    "LoL Esports": "https://www.priceless.com/celebrity/19286/league-of-legends-e-sports",
    "LoL Esports shop": "https://www.priceless.com/lolesports-shop",  # target of priceless.com/LoLesports
}
PRICELESS_URL = PRICELESS_PAGES["Riot Games"]
CHASE_URL = ("https://cashbackmoments.chase.com/cashbackmoments/future"
             "?EventCode=202611_2026_League_of_Legends_World_Championship")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

HERE = Path(__file__).resolve().parent
STATE_FILE = Path(os.environ.get("STATE_FILE", HERE / "state.json"))
FAIL_ALERT_AFTER = 3  # consecutive failures before warning that the tracker may be broken


# ---------------------------------------------------------------- helpers
def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def to_text(fragment):
    fragment = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    fragment = html.unescape(fragment).replace("opens in the same window", " ")
    return re.sub(r"\s+", " ", fragment).strip()


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- page checks
PRODUCT_RE = re.compile(r'href=["\']((?:https://www\.priceless\.com)?/[a-z-]+/product/(\d+)/[a-z0-9-]+[^"\']*)["\']', re.I)


def check_priceless_page(url):
    """Return {product_id: product_url} for every experience tile linked on the page.
    Product tiles look like <a href="/sports/product/237665/lcs-champs" pid="237665">.
    The Riot / LoL pages currently have zero of these anywhere (header/footer included)."""
    page = fetch(url)
    if "priceless" not in page.lower() or len(page) < 5000:
        raise RuntimeError(f"unexpected response from {url} ({len(page)} bytes)")
    products = {}
    for href, pid in PRODUCT_RE.findall(page):
        if not href.startswith("http"):
            href = "https://www.priceless.com" + href
        products.setdefault(pid, href)
    return products


def check_priceless():
    return {name: check_priceless_page(url) for name, url in PRICELESS_PAGES.items()}


# ---------------------------------------------------------------- sitemap catch-all
# Priceless has 16 look-alike League/Riot pages (Oct 2026: four "Riot Games", seven "League of
# Legends Esports", ...), and filed the LCS 2026 experience under "LoL Esports" rather than "Riot
# Games". So the sitemap, which lists every live page and product and is rebuilt daily, is used to
# (1) watch every League/Riot page, including ones created later, and (2) inspect every product
# that appears anywhere on the site, in case the experience is filed under something unrelated.
SITEMAP_URL = "https://www.priceless.com/sitemap.xml"
CELEBRITY_RE = re.compile(
    r"https://www\.priceless\.com/celebrity/(\d+)/([a-z0-9-]*(?:riot-games|league-of-legends|lolesports|arcane)[a-z0-9-]*)$")
PRODUCT_LOC_RE = re.compile(r"https://www\.priceless\.com/[a-z-]+/product/(\d+)/([a-z0-9-]+)")
# Only words that mean League and nothing else: bare "riot" is also Riot Fest (a music festival),
# "lol" a comedy night, "lta" the Lawn Tennis Association. These ring an alarm, so no guessing.
PRODUCT_SLUG_RE = re.compile(
    r"(?:^|-)(?:lcs|summoners?)(?:-|$)|riot-games|worlds-20\d\d|league-of-legends|lolesports|lol-esports")
PRODUCT_TEXT_RE = re.compile(r"league of legends|riot games|lol ?esports", re.I)
MAX_PRODUCT_FETCHES = 60  # per sitemap check; any left over are checked next time
# Priceless rebuilds the sitemap about daily and it is 4 MB, so it is read hourly rather than on
# every run. The League/Riot pages themselves are still checked every run.
SITEMAP_EVERY = 55 * 60  # seconds; a little under an hour so runs on the hour don't skip one


def sitemap_locs():
    xml = fetch(SITEMAP_URL, timeout=60)
    locs = re.findall(r"<loc>([^<]+)</loc>", xml)
    if len(locs) < 1000:
        raise RuntimeError(f"sitemap has only {len(locs)} URLs (blocked or changed?)")
    return locs


def product_is_relevant(url, slug):
    """Slug first (free). Otherwise read the page: its celebrity links and its text.
    Products for other countries come back as a blank template, so for those the slug is all
    there is to go on."""
    if PRODUCT_SLUG_RE.search(slug):
        return True
    try:
        page = fetch(url)
    except Exception as e:
        print(f"[{now()}] sitemap: could not read {url}: {e}", file=sys.stderr)
        return False
    celebs = ["https://www.priceless.com" + c for c in re.findall(r'/celebrity/\d+/[a-z0-9-]+', page)]
    return any(CELEBRITY_RE.match(c) for c in celebs) or bool(PRODUCT_TEXT_RE.search(to_text(page)))


def run_sitemap(state):
    sm = state.setdefault("sitemap", {})
    state["fails"].setdefault("sitemap", 0)
    if "seen" in sm and time.time() - sm.get("checked_at", 0) < SITEMAP_EVERY:
        return
    try:
        locs = sitemap_locs()
    except Exception as e:
        state["fails"]["sitemap"] += 1
        print(f"[{now()}] sitemap failed ({state['fails']['sitemap']}): {e}", file=sys.stderr)
        if state["fails"]["sitemap"] == FAIL_ALERT_AFTER:
            notify("Tracker warning: Priceless sitemap failing", str(e), url=SITEMAP_URL)
        return
    state["fails"]["sitemap"] = 0
    sm["checked_at"] = int(time.time())

    fixed = set(PRICELESS_PAGES.values())
    sm["celebrity_pages"] = sorted({u for u in locs if CELEBRITY_RE.match(u)} - fixed)

    products = {}
    for u in locs:
        m = PRODUCT_LOC_RE.match(u)
        if m:
            products.setdefault(m.group(1), (u, m.group(2)))
    # A truncated sitemap would make every product it left out look new the next time it came back
    # complete, burying a real drop under days of backlog. Treat a sudden shrink as a failure.
    if sm.get("seen") and len(products) < 0.8 * len(sm["seen"]):
        state["fails"]["sitemap"] += 1
        print(f"[{now()}] sitemap shrank to {len(products)} products from {len(sm['seen'])}; ignoring it",
              file=sys.stderr)
        if state["fails"]["sitemap"] == FAIL_ALERT_AFTER:
            notify("Tracker warning: Priceless sitemap looks incomplete",
                   f"{len(products)} listings, was {len(sm['seen'])}", url=SITEMAP_URL)
        return

    if "seen" not in sm:  # first run: baseline everything already live
        sm["seen"] = sorted(products)
        print(f"[{now()}] sitemap: baseline of {len(products)} product(s), "
              f"{len(sm['celebrity_pages'])} extra League/Riot page(s)")
        return

    seen = set(sm["seen"])
    new = [pid for pid in products if pid not in seen]
    hits = 0
    for pid in new[:MAX_PRODUCT_FETCHES]:
        url, slug = products[pid]
        if product_is_relevant(url, slug):
            hits += 1
            notify("PRICELESS DROP: League/Riot listing found", f"New Priceless listing - go now:\n{url}",
                   url=url, ring=True)
        seen.add(pid)
    sm["seen"] = sorted(seen & set(products))  # forget products that left the sitemap
    print(f"[{now()}] sitemap: {len(new)} new product(s), checked {min(len(new), MAX_PRODUCT_FETCHES)}, "
          f"{hits} League/Riot; watching {len(sm['celebrity_pages'])} extra page(s)")


def watched_pages(state):
    """The fixed pages plus every League/Riot page the sitemap has turned up."""
    pages = dict(PRICELESS_PAGES)
    for url in state.get("sitemap", {}).get("celebrity_pages", []):
        m = CELEBRITY_RE.match(url)
        if m:
            pages[f"{m.group(2)} ({m.group(1)})"] = url
    return pages


def check_chase():
    """Return the normalized text of the event block + its hash."""
    page = fetch(CHASE_URL)
    a = page.find("<h1")
    b = page.find("EXPLORE MORE", a)
    if a == -1 or b == -1:
        raise RuntimeError("Chase: event block markers not found (layout changed?)")
    block = to_text(page[a:b])
    # Look for anything that smells like a purchase link inside the block.
    buy_links = re.findall(r'href=["\']([^"\']*(?:priceless|ticket|register|rsvp|book)[^"\']*)["\']',
                           page[a:b], re.I)
    return {"hash": sha(block), "text": block, "buy_links": sorted(set(buy_links))}


# ---------------------------------------------------------------- notifications
ALARM_RETRY = 30      # seconds between repeats of a Pushover emergency alarm (Pushover minimum)
ALARM_EXPIRE = 3600   # stop repeating after an hour if never acknowledged


def alarm(title, message, url=None, expire=ALARM_EXPIRE):
    """Pushover emergency priority: repeats every ALARM_RETRY seconds until acknowledged
    in the app, and sounds through silent mode if Critical Alerts are allowed on the phone."""
    user, token = os.environ.get("PUSHOVER_USER"), os.environ.get("PUSHOVER_TOKEN")
    if not (user and token):
        return False
    fields = {"token": token, "user": user, "title": title[:250], "message": message[:1024],
              "priority": 2, "retry": ALARM_RETRY, "expire": expire,
              "sound": os.environ.get("PUSHOVER_SOUND", "persistent")}
    if url:
        fields.update(url=url[:512], url_title="Open page")
    try:
        req = urllib.request.Request("https://api.pushover.net/1/messages.json",
                                     data=urllib.parse.urlencode(fields).encode(), method="POST")
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as e:
        print(f"[{now()}] pushover failed: {e}", file=sys.stderr)
        return False


def in_quiet_hours(at=None):
    start, end = (int(x) for x in os.environ.get("QUIET_HOURS", "0-6").split("-"))
    hour = (at or datetime.now(ZoneInfo(os.environ.get("QUIET_TZ", "America/Los_Angeles")))).hour
    return start <= hour < end if start <= end else (hour >= start or hour < end)


def notify(title, message, url=None, ring=False):
    """ring=True is for tickets on sale and nothing else: a Pushover emergency alarm plus an urgent
    ntfy push, at any hour. Anything else is a normal push, silent during quiet hours."""
    if ring:
        priority = "urgent"
    else:
        priority = "min" if in_quiet_hours() else "default"
    sent = alarm(title, message, url) if ring else False
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority,
                   "Tags": "rotating_light,video_game"}
        if url:
            headers["Click"] = url
            headers["Actions"] = f"view, Open page, {url}"
        try:
            req = urllib.request.Request(f"{server}/{topic}", data=message.encode(),
                                         headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=20).read()
            sent = True
        except Exception as e:
            print(f"[{now()}] ntfy failed: {e}", file=sys.stderr)

    hook = os.environ.get("DISCORD_WEBHOOK")
    if hook:
        body = json.dumps({"content": f"**{title}**\n{message}" + (f"\n{url}" if url else "")})
        try:
            req = urllib.request.Request(hook, data=body.encode(), method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": UA})
            urllib.request.urlopen(req, timeout=20).read()
            sent = True
        except Exception as e:
            print(f"[{now()}] discord failed: {e}", file=sys.stderr)

    if not sent:
        print(f"[{now()}] (no notifier configured) {title}: {message}")
    print("\a", end="", flush=True)  # terminal bell when running locally


# ---------------------------------------------------------------- state
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ---------------------------------------------------------------- news + social (free sources)
KEYWORDS = re.compile(os.environ.get(
    "KEYWORDS",
    r"\bpriceless\b|mastercard|chase freedom|freedom flex|@chase\b|cardholder|pre-?sale|"
    r"cashback moments|fan ?fest|playtest|experiences? (?:is|are) live|on sale now"), re.I)
X_KEYWORDS = KEYWORDS  # back-compat name

# Official news pages that would carry a written announcement. Each is server-rendered,
# so new article links show up in plain HTML. A new article alerts if its slug or body
# matches KEYWORDS (lolesports) or the topic filter (Chase, which posts about everything).
NEWS_SOURCES = [
    {
        "name": "lolesports.com news",
        "url": "https://lolesports.com/en-US/news",
        "base": "https://lolesports.com",
        "link_re": r'href="(/(?:en-US/)?news/[a-z0-9-]+)"',
        "topic_re": None,
    },
    {
        "name": "Chase media center",
        "url": "https://media.chase.com/news",
        "base": "https://media.chase.com",
        "link_re": r'href="(/news/[A-Za-z0-9-]+)"',
        "topic_re": r"league|riot|worlds|esports|lol|gaming|cashback-moments|freedom",
    },
]
MAX_ARTICLE_FETCHES = 8  # per source per cycle


def check_rss_feed(feed_url, feed_state):
    """Free fallback: poll an RSS/Atom feed of an X account (e.g. generated by rss.app)."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(fetch(feed_url))
    items = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        def get(*names):
            for c in it:
                if c.tag.split("}")[-1] in names:
                    return c
            return None
        title_el, desc_el = get("title"), get("description", "content", "summary")
        title = (title_el.text or "") if title_el is not None else ""
        desc = (desc_el.text or "") if desc_el is not None else ""
        text = to_text(title + " " + desc)
        link_el = get("link")
        link = ((link_el.get("href") or link_el.text) if link_el is not None else None) or feed_url
        guid_el = get("guid", "id")
        guid = (guid_el.text if guid_el is not None else None) or link
        items.append((guid, text, link))
    seen = set(feed_state.get("seen", []))
    first_run = "seen" not in feed_state
    feed_state["seen"] = [g for g, _, _ in items][:200]
    if first_run:
        return []
    return [(t, l) for g, t, l in items if g not in seen and X_KEYWORDS.search(t)]


def check_news_source(src, src_state):
    """Return [(title_or_slug, url)] for new articles that match the keywords."""
    page = fetch(src["url"])
    slugs = list(dict.fromkeys(re.findall(src["link_re"], page)))  # ordered, unique
    if not slugs:
        raise RuntimeError("no article links found (layout changed?)")
    first_run = "seen" not in src_state
    seen = set(src_state.get("seen", []))
    src_state["seen"] = sorted(seen | set(slugs))[-1000:]
    if first_run:
        return []
    hits = []
    for slug in [x for x in slugs if x not in seen][:MAX_ARTICLE_FETCHES]:
        url = src["base"] + slug
        if src["topic_re"] and not re.search(src["topic_re"], slug, re.I):
            continue
        try:
            body = to_text(fetch(url))
        except Exception:
            body = ""
        title = slug.rsplit("/", 1)[-1].replace("-", " ")
        if KEYWORDS.search(slug.replace("-", " ") + " " + body) or src["topic_re"]:
            hits.append((title, url))
    return hits


# ---------------------------------------------------------------- X search (pay-per-use API)
# Billed per post returned ($0.005 as of Oct 2026), so the filtering happens in X's query, not
# here: only matching posts are ever returned, which keeps a month to well under a dollar.
# Keywords come from how past drops were actually announced:
#   @lolesports  "CALLING ALL CHASE FREEDOM FLEX MASTERCARD CARDHOLDERS! ... exclusive sale ...
#                 Limited quantities"
#   @LCSOfficial "with your Chase Freedom Flex ... playtest ... priceless.com/LCSChampsPlaytest"
#   @MastercardGG "Attention Mastercard Cardholders: The Mastercard Presale for @Lolesports ..."
# @MastercardGG covers every game, so it must also mention League.
X_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
X_QUERY = os.environ.get("X_QUERY") or (
    '((from:lolesports OR from:riotgames OR from:LCSOfficial) '
    '(priceless OR mastercard OR cardholders OR cardholder OR presale OR "pre-sale" '
    'OR "freedom flex" OR "chase freedom" OR "exclusive sale" OR "limited quantities" '
    'OR playtest OR "fan fest" OR url:priceless)) '
    'OR (from:MastercardGG (lolesports OR "league of legends" OR worlds OR LCS OR worlds2026))')
X_OVERLAP = 10 * 60  # re-search the last 10 minutes each time; X can index a post a little late


def x_iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_x(xs, token):
    """Return [(text, url)] for posts not seen before. The first call only records a start time,
    so turning this on never reads (or pays for) posts from before it was on."""
    started = time.time()
    if "last_run" not in xs:
        xs["last_run"] = int(started)
        xs["seen"] = []
        return []
    # recent search only reaches back 7 days
    since = max(xs["last_run"] - X_OVERLAP, started - 6 * 86400)
    params = {"query": X_QUERY, "start_time": x_iso(since), "max_results": "100",
              "tweet.fields": "created_at"}
    posts = []
    for _ in range(5):  # pages; more than 500 matching posts in 10 minutes would be news itself
        req = urllib.request.Request(f"{X_SEARCH_URL}?{urllib.parse.urlencode(params)}",
                                     headers={"Authorization": f"Bearer {token}", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")[:200]
            hint = " (out of credit?)" if e.code in (402, 403) else ""
            raise RuntimeError(f"X API {e.code}{hint}: {detail}") from None
        posts += body.get("data", [])
        nxt = body.get("meta", {}).get("next_token")
        if not nxt:
            break
        params["next_token"] = nxt
    xs["last_run"] = int(started)
    seen = set(xs.get("seen", []))
    new = [p for p in posts if p["id"] not in seen]
    xs["seen"] = (xs.get("seen", []) + [p["id"] for p in new])[-300:]
    return [(p.get("text", ""), f"https://x.com/i/status/{p['id']}") for p in new]


def run_social(state):
    state.setdefault("news", {})
    state.setdefault("rss", {})

    for src in NEWS_SOURCES:
        key = f"news:{src['name']}"
        state["fails"].setdefault(key, 0)
        try:
            hits = check_news_source(src, state["news"].setdefault(src["name"], {}))
            state["fails"][key] = 0
            for title, url in hits:
                notify(f"{src['name']}: possible drop announcement", title, url=url)
            print(f"[{now()}] {src['name']}: {len(hits)} matching new article(s)")
        except Exception as e:
            state["fails"][key] += 1
            print(f"[{now()}] {src['name']} failed ({state['fails'][key]}): {e}", file=sys.stderr)
            if state["fails"][key] == FAIL_ALERT_AFTER:
                notify(f"Tracker warning: {src['name']} failing", str(e), url=src["url"])

    feeds = [f.strip() for f in os.environ.get("X_RSS_FEEDS", "").split(",") if f.strip()]
    for feed in feeds:
        key = f"rss:{feed}"
        state["fails"].setdefault(key, 0)
        try:
            hits = check_rss_feed(feed, state["rss"].setdefault(feed, {}))
            state["fails"][key] = 0
            for text, link in hits:
                notify("Social post about the drop", text[:400], url=link)
            print(f"[{now()}] rss: {len(hits)} matching new item(s) from {feed[:60]}")
        except Exception as e:
            state["fails"][key] += 1
            print(f"[{now()}] rss failed ({state['fails'][key]}): {e}", file=sys.stderr)
            if state["fails"][key] == FAIL_ALERT_AFTER:
                notify("Tracker warning: RSS feed failing", f"{feed}: {e}")


# ---------------------------------------------------------------- main loop
def run_x(state):
    state.setdefault("fails", {})
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if token:
        state["fails"].setdefault("x", 0)
        try:
            hits = check_x(state.setdefault("x", {}), token)
            state["fails"]["x"] = 0
            for text, link in hits:
                notify("Post on X about the drop", text[:400], url=link)
            print(f"[{now()}] x: {len(hits)} matching new post(s)")
        except Exception as e:
            state["fails"]["x"] += 1
            print(f"[{now()}] x failed ({state['fails']['x']}): {e}", file=sys.stderr)
            if state["fails"]["x"] == FAIL_ALERT_AFTER:
                notify("Tracker warning: X search failing", str(e)[:300])


def run_once(state, only=None):
    """only="x" runs just the X search; only="sites" runs everything else; None runs both."""
    if only != "sites":
        run_x(state)
    if only == "x":
        return state

    state.setdefault("fails", {})
    state["fails"].setdefault("chase", 0)

    # --- Priceless sitemap: catch-all, and discovers the League/Riot pages to watch
    run_sitemap(state)

    # --- Priceless (each page tracked separately)
    state.setdefault("priceless", {})
    newly_failing = []
    for name, url in watched_pages(state).items():
        key = f"priceless:{name}"
        state["fails"].setdefault(key, 0)
        try:
            products = check_priceless_page(url)
            state["fails"][key] = 0
            seen = state["priceless"].get(name)
            if seen is None:
                # first run: baseline (but still alert if something is already listed)
                if products:
                    notify(f"PRICELESS: {len(products)} listing(s) already on {name}",
                           "\n".join(products.values())[:500], url=url, ring=True)
            else:
                new = {k: v for k, v in products.items() if k not in seen}
                if new:
                    first = next(iter(new.values()))
                    notify(f"PRICELESS DROP: new {name} experience",
                           f"{len(new)} new listing(s) - go now:\n" + "\n".join(new.values())[:500],
                           url=first, ring=True)
            state["priceless"][name] = sorted(products)
            print(f"[{now()}] priceless/{name}: {len(products)} product(s)")
        except Exception as e:
            state["fails"][key] += 1
            print(f"[{now()}] priceless/{name} check failed ({state['fails'][key]}): {e}", file=sys.stderr)
            if state["fails"][key] == FAIL_ALERT_AFTER:
                newly_failing.append((name, url, e))
    if newly_failing:
        name, url, e = newly_failing[0]
        notify(f"Tracker warning: {len(newly_failing)} Priceless page(s) failing",
               f"{FAIL_ALERT_AFTER} failures in a row on: {', '.join(n for n, _, _ in newly_failing)[:300]}. "
               f"First error: {e}. Check Priceless manually.", url=url)

    # --- X / social
    run_social(state)

    # --- Chase
    try:
        c = check_chase()
        state["fails"]["chase"] = 0
        prev_hash = state.get("chase_hash")
        if prev_hash and c["hash"] != prev_hash:
            old = state.get("chase_text", "")
            added = [w for w in c["text"].split(". ") if w not in old]
            notify("CHASE: Worlds event page changed",
                   "Chase updated the Worlds Cashback Moments page — the drop may be live. "
                   f"New text: {' | '.join(added)[:300] or c['text'][:300]}",
                   url=PRICELESS_URL)  # where tickets will most likely be bought
        state["chase_hash"] = c["hash"]
        state["chase_text"] = c["text"]
        print(f"[{now()}] chase: hash {c['hash']}" + (" (baseline)" if not prev_hash else ""))
    except Exception as e:
        state["fails"]["chase"] += 1
        print(f"[{now()}] chase check failed ({state['fails']['chase']}): {e}", file=sys.stderr)
        if state["fails"]["chase"] == FAIL_ALERT_AFTER:
            notify("Tracker warning: Chase check failing",
                   f"{FAIL_ALERT_AFTER} failures in a row: {e}. Check the page manually.",
                   url=CHASE_URL)

    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--only", choices=["x", "sites"])
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--test-alarm", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.test_notify:
        notify("meebo tracker test", "If you see this, notifications work.", url=PRICELESS_URL)
        return

    if args.test_alarm:
        # expires after 2 minutes so a test never rings for an hour
        if not alarm("meebo tracker alarm test", "Tap Acknowledge to stop the alarm.",
                     url=PRICELESS_URL, expire=120):
            sys.exit("Pushover alarm not sent: set PUSHOVER_USER and PUSHOVER_TOKEN")
        return

    if args.status:
        print(json.dumps({"priceless": check_priceless(), "chase": check_chase()}, indent=2))
        return

    state = load_state()
    if args.once:
        save_state(run_once(state, args.only))
        return

    interval = int(os.environ.get("INTERVAL", "120"))
    print(f"Tracking every {interval}s. Ctrl+C to stop.")
    while True:
        state = run_once(state)
        save_state(state)
        time.sleep(interval)


if __name__ == "__main__":
    main()
