#!/usr/bin/env python3
"""
update.py — NostalgicSoftware.com Site Generator
=================================================
Fetches live listings directly from eBay and generates the full static site.
Run manually or via GitHub Actions (runs daily at 6 AM ET automatically).

What it does:
  1. Fetches all active listings from eBay store via Finding API
  2. Fetches full-res images via eBay Shopping API (s-l1600)
  3. Fetches full HTML descriptions via eBay Shopping API
  4. Diffs against existing /items/ pages:
     - New listing    → generate fresh item page
     - Still live     → regenerate (refresh price/image/description)
     - Sold/gone      → convert to tombstone page with 4 suggestions
  5. Regenerates sitemap.xml and robots.txt
  6. Prints a change summary

Usage:
  python3 update.py

Requirements:
  pip install opencv-python-headless

Output:
  index.html, sitemap.xml, robots.txt
  items/[3-word-slug]-[ebayID].html  (one page per listing)
  nostalgicsoftware-hero-thumb.jpg   (extracted from hero video)
"""

import os, re, sys, json, hashlib, textwrap, urllib.request, xml.etree.ElementTree as ET, time
from datetime import date
from html import escape

# ─────────────────────────────────────────────────────────────
#  CONFIG  — edit these
# ─────────────────────────────────────────────────────────────
EBAY_STORE    = "nostalgic-software"    # your eBay seller ID
SITE_BASE     = "https://nostalgicsoftware.com"
GA_ID         = "G-BPE3G9Q26B"
OUTPUT_DIR    = "items"          # relative to where you run the script
SITEMAP_PATH  = "sitemap.xml"
TODAY         = date.today().isoformat()

# eBay API credentials — stored as GitHub Secrets, fallback to hardcoded for local dev
EBAY_APP_ID   = os.environ.get("EBAY_APP_ID",  "GrahamRo-Nostalgi-PRD-4bf1cc952-2f5fcafe")
EBAY_CERT_ID  = os.environ.get("EBAY_CERT_ID", "")   # PRD-... from developer.ebay.com
EBAY_USER_TOKEN = os.environ.get("EBAY_USER_TOKEN", "")  # Auth'n'Auth token — expires Sep 13 2027

# ─────────────────────────────────────────────────────────────
#  KEYWORD MAP — per-category extra SEO terms
# ─────────────────────────────────────────────────────────────
CAT_EXTRA_KW = {
    "disney":       "Walt Disney World collectibles, Disney pins eBay, Disney memorabilia, theme park souvenirs, Mickey Mouse merchandise",
    "collectibles": "vintage collectibles eBay, advertising collectibles, rare finds, antique items for sale, nostalgic memorabilia",
    "electronics":  "vintage electronics eBay, replacement parts, retro tech, consumer electronics, electronics accessories",
    "sports":       "Tampa Bay Lightning merchandise, Stanley Cup memorabilia, sports collectibles eBay, NHL memorabilia, gaming accessories",
    "home":         "home goods eBay, kitchen accessories, Keurig parts, Starbucks coffee eBay, home decor deals",
    "clothing":     "clothing eBay deals, jackets for sale, wearable collectibles, apparel eBay, fashion finds",
    "beauty":       "health beauty eBay, personal care products, massage therapy devices, beauty tools, wellness products",
    "toys":         "kids toys eBay, baby products, Disney toys, action figures, children's gifts",
    "other":        "unique finds eBay, miscellaneous collectibles, hard to find items, eBay deals, nostalgic software store",
}

# category keyword detector
CAT_MAP = [
    ("disney",       ["disney","mickey","minnie","goofy","donald","wdw","pluto","daisy","pixar"]),
    ("collectibles", ["collectible","vintage","antique","pin","button","spatula","advertising","nehi","screener"]),
    ("electronics",  ["remote","adapter","hdmi","usb","charging","battery","tv","lcd","bravia","samsung","camera","keurig","razor","scooter","calculator","polarizer","lens","dvd","vcr"]),
    ("sports",       ["lightning","stanley cup","bud light","baseball","wii","xbox","ps5","playstation","nintendo","nes","nfl","nhl","nba"]),
    ("home",         ["coffee","starbucks","mattress","light","led","sprinkler","splash","coasters","thermal","paper","swing","beer","keg","faucet","kitchen"]),
    ("clothing",     ["shirt","jacket","shorts","lounge","poncho","pajama","tee","leather","gloves","bracelet","insoles","gel","foot","heel","mask","hat","cap"]),
    ("beauty",       ["shampoo","hair","beard","nails","dip","nail","massager","cream","lotion"]),
    ("toys",         ["toy","happy meal","mcdonald","lego","plush","tsum","action figure","balance board","kids","baby","toddler","yoda"]),
]

def categorize(title):
    t = title.lower()
    for key, kws in CAT_MAP:
        if any(k in t for k in kws):
            return key
    return "other"

def smart_keywords(title, category):
    """Build keywords string filtering out redundant extras already in title."""
    noise = {"a","an","the","and","or","for","of","in","on","at","to","with","by",
             "from","is","it","as","be","we","new","used","lot","set","pack"}
    title_sig = set(re.sub(r"[^a-z0-9\s]"," ",title.lower()).split()) - noise
    filtered = [p.strip() for p in CAT_EXTRA_KW.get(category,"").split(", ")
                if not ((set(re.sub(r"[^a-z0-9\s]"," ",p.lower()).split())-noise) & title_sig)]
    return ", ".join([title] + filtered + ["NostalgicSoftware eBay store","nostalgic-software"])

# ─────────────────────────────────────────────────────────────
#  SLUG REGISTRY — permanent slugs, never change after first set
#  Edit slugs.json in the repo to change a specific slug.
# ─────────────────────────────────────────────────────────────
SLUG_REGISTRY_PATH = "slugs.json"

SLUG_STOP = {"a","an","the","and","or","for","of","in","on","at","to","with","by","from",
             "new","free","s/h","w/","size","color","set","lot","pack","box","per",
             "2019","2020","2021","2022","2023","2024","2025","2026"}

def load_slug_registry():
    if os.path.exists(SLUG_REGISTRY_PATH):
        with open(SLUG_REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_slug_registry(registry):
    with open(SLUG_REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, sort_keys=True)

def auto_slug(item_id, title):
    words = re.sub(r"[^a-z0-9\s]", " ", title.lower()).split()
    words = [w for w in words if w not in SLUG_STOP and len(w) > 2]
    return "-".join(words[:3]) + f"-{item_id}"

SLUG_REGISTRY = load_slug_registry()

def get_slug(item_id, title=""):
    if item_id in SLUG_REGISTRY:
        return SLUG_REGISTRY[item_id] + f"-{item_id}"
    new_slug_base = auto_slug(item_id, title).replace(f"-{item_id}", "")
    SLUG_REGISTRY[item_id] = new_slug_base
    save_slug_registry(SLUG_REGISTRY)
    print(f"  [registry] New slug registered: {new_slug_base}-{item_id}")
    return f"{new_slug_base}-{item_id}"

def slug(item_id, title=""):
    return get_slug(item_id, title)

def filename(item_id, title=""):
    return os.path.join(OUTPUT_DIR, f"{get_slug(item_id, title)}.html")

DESC_CACHE_PATH     = "desc_cache.json"
LISTINGS_CACHE_PATH = "listings_cache.json"

def load_desc_cache():
    """Load cached descriptions from desc_cache.json."""
    if os.path.exists(DESC_CACHE_PATH):
        with open(DESC_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_desc_cache(cache):
    """Save description cache to desc_cache.json."""
    with open(DESC_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f)

def load_listings_cache():
    """Load cached listing data — title, img, cat for all known items."""
    if os.path.exists(LISTINGS_CACHE_PATH):
        with open(LISTINGS_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_listings_cache(live_items):
    """Save current live listings to cache so sold items retain their data."""
    cache = load_listings_cache()
    for item in live_items:
        cache[item["id"]] = {
            "title":    item["title"],
            "img":      item["img"],
            "category": item["category"],
            "price":    item["price"],
            "slug":     item.get("slug", ""),
        }
    with open(LISTINGS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def fetch_ebay_descriptions(item_ids):
    """
    Fetches item descriptions using Trading API GetItem (XML).
    Uses desc_cache.json — only fetches items not already cached.
    Each description is fetched once and cached permanently.
    Requires EBAY_USER_TOKEN.
    """
    import xml.etree.ElementTree as ET

    cache  = load_desc_cache()
    ids    = list(item_ids)
    needed = [iid for iid in ids if iid not in cache]

    if not needed:
        print(f"  [desc] All {len(ids)} descriptions loaded from cache")
        return {iid: cache[iid] for iid in ids if iid in cache}

    if not EBAY_USER_TOKEN:
        print(f"  [desc] No EBAY_USER_TOKEN — descriptions skipped")
        return {iid: cache[iid] for iid in ids if iid in cache}

    print(f"  [desc] Fetching {len(needed)} descriptions via Trading API GetItem (cached: {len(ids)-len(needed)})")
    ns = {"e": "urn:ebay:apis:eBLBaseComponents"}

    for idx, iid in enumerate(needed):
        xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{EBAY_USER_TOKEN}</eBayAuthToken>
  </RequesterCredentials>
  <ItemID>{iid}</ItemID>
  <DetailLevel>ItemReturnDescription</DetailLevel>
</GetItemRequest>"""
        try:
            req = urllib.request.Request(
                "https://api.ebay.com/ws/api.dll",
                data=xml_body.encode("utf-8"),
                headers={
                    "X-EBAY-API-SITEID":              "0",
                    "X-EBAY-API-COMPATIBILITY-LEVEL": "1113",
                    "X-EBAY-API-CALL-NAME":           "GetItem",
                    "X-EBAY-API-APP-NAME":            EBAY_APP_ID,
                    "X-EBAY-API-CERT-NAME":           EBAY_CERT_ID,
                    "Content-Type":                   "text/xml;charset=utf-8",
                }
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
            root = ET.fromstring(raw)
            ack  = root.findtext("e:Ack", namespaces=ns) or root.findtext("Ack") or ""
            # Diagnose first item only
            if idx == 0:
                print(f"  [desc] First item Ack={ack}")
                item_el = root.find("e:Item", namespaces=ns) or root.find("Item")
                if item_el is not None:
                    tags = [child.tag.split("}")[-1] for child in item_el]
                    print(f"  [desc] Item tags returned: {tags[:15]}")
                    desc_el = None
                    for child in item_el:
                        if child.tag.split("}")[-1] == "Description":
                            desc_el = child
                            break
                    print(f"  [desc] Description element found: {desc_el is not None}")
                    if desc_el is not None:
                        print(f"  [desc] Description text length: {len(desc_el.text or '')}")
                else:
                    print(f"  [desc] No Item element found in response")
                    print(f"  [desc] Root tags: {[child.tag.split('}')[-1] for child in root]}")
            if ack in ("Success", "Warning"):
                item_el = root.find("e:Item", namespaces=ns) or root.find("Item")
                if item_el is not None:
                    # Tags returned without namespace prefix — search by local name
                    desc_el = None
                    for child in item_el:
                        if child.tag.split("}")[-1] == "Description":
                            desc_el = child
                            break
                    if desc_el is not None and desc_el.text:
                        cache[iid] = desc_el.text
            if (idx+1) % 10 == 0:
                print(f"  [desc] {idx+1}/{len(needed)} fetched...")
        except Exception as e:
            print(f"  [desc] Item {iid} failed: {e}")
            if idx == 0:
                import traceback
                traceback.print_exc()
        time.sleep(0.5)

    save_desc_cache(cache)
    result = {iid: cache[iid] for iid in ids if iid in cache}
    print(f"  [desc] Got descriptions for {len(result)}/{len(ids)} items")
    return result
def fetch_items():
    """
    Calls Trading API GetSellerList via XML POST.
    Returns all active fixed-price listings for EBAY_STORE.
    """
    import xml.etree.ElementTree as ET
    from datetime import datetime, timezone, timedelta

    if not EBAY_USER_TOKEN:
        print("  ERROR: EBAY_USER_TOKEN not set — cannot fetch listings")
        return []

    PAGE_SIZE = 200
    items     = []
    page      = 1
    now       = datetime.now(timezone.utc)
    end_from  = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_to    = (now + timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    print(f"  [Trading API] Fetching listings for: {EBAY_STORE}")

    while True:
        xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetSellerListRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{EBAY_USER_TOKEN}</eBayAuthToken>
  </RequesterCredentials>
  <UserID>{EBAY_STORE}</UserID>
  <GranularityLevel>Fine</GranularityLevel>
  <EndTimeFrom>{end_from}</EndTimeFrom>
  <EndTimeTo>{end_to}</EndTimeTo>
  <Pagination>
    <EntriesPerPage>{PAGE_SIZE}</EntriesPerPage>
    <PageNumber>{page}</PageNumber>
  </Pagination>
  <DetailLevel>ReturnAll</DetailLevel>
</GetSellerListRequest>"""

        try:
            req = urllib.request.Request(
                "https://api.ebay.com/ws/api.dll",
                data=xml_body.encode("utf-8"),
                headers={
                    "X-EBAY-API-SITEID":              "0",
                    "X-EBAY-API-COMPATIBILITY-LEVEL": "1113",
                    "X-EBAY-API-CALL-NAME":           "GetSellerList",
                    "X-EBAY-API-APP-NAME":            EBAY_APP_ID,
                    "X-EBAY-API-DEV-NAME":            "",
                    "X-EBAY-API-CERT-NAME":           EBAY_CERT_ID,
                    "Content-Type":                   "text/xml;charset=utf-8",
                }
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"  [Trading API] HTTP {e.code}: {body[:400]}")
            return []
        except Exception as e:
            print(f"  [Trading API] ERROR: {e}")
            return []

        # Parse XML response
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            print(f"  [Trading API] XML parse error: {e}")
            print(f"  [Trading API] Response: {raw[:300]}")
            return []

        ns = {"e": "urn:ebay:apis:eBLBaseComponents"}

        ack = root.findtext("e:Ack", namespaces=ns) or root.findtext("Ack") or ""
        if ack not in ("Success", "Warning"):
            errors = root.find("e:Errors", namespaces=ns) or root.find("Errors")
            err_msg = errors.findtext("e:LongMessage", namespaces=ns) if errors else "unknown"
            print(f"  [Trading API] API error ({ack}): {err_msg}")
            return []

        # Pagination
        pagination = root.find("e:PaginationResult", namespaces=ns) or root.find("PaginationResult")
        total_pages = int(pagination.findtext("e:TotalNumberOfPages", namespaces=ns) or pagination.findtext("TotalNumberOfPages") or 1) if pagination else 1
        total_items = int(pagination.findtext("e:TotalNumberOfEntries", namespaces=ns) or pagination.findtext("TotalNumberOfEntries") or 0) if pagination else 0

        item_array = root.find("e:ItemArray", namespaces=ns) or root.find("ItemArray")
        listing_array = item_array.findall("e:Item", namespaces=ns) if item_array else []
        if not listing_array:
            listing_array = item_array.findall("Item") if item_array else []

        for el in listing_array:
            def txt(tag):
                v = el.findtext(f"e:{tag}", namespaces=ns)
                return v if v is not None else (el.findtext(tag) or "")

            listing_type = txt("ListingType")
            if listing_type not in ("FixedPriceItem", "StoresFixedPrice"):
                continue

            item_id = txt("ItemID").strip()
            title   = txt("Title").strip()

            # Price
            selling = el.find("e:SellingStatus", namespaces=ns) or el.find("SellingStatus")
            price   = 0.0
            if selling is not None:
                cp = selling.find("e:CurrentPrice", namespaces=ns) or selling.find("CurrentPrice")
                if cp is not None:
                    price = float(cp.text or "0")

            # Images
            # Search by local tag name — avoids namespace issues
            img = ""
            pic_details = None
            for child in el:
                if child.tag.split("}")[-1] == "PictureDetails":
                    pic_details = child
                    break
            if pic_details is not None:
                for child in pic_details:
                    if child.tag.split("}")[-1] == "PictureURL" and child.text:
                        img = child.text
                        break
            if img:
                img = re.sub(r"s-l\d+", "s-l1600", img)
                img = re.sub(r"\$_\d+", "$_57", img)

            # Description — Trading API returns full HTML description with GranularityLevel=Fine
            desc_el = el.find("e:Description", namespaces=ns)
            if desc_el is None:
                desc_el = el.find("Description")
            desc = (desc_el.text or "") if desc_el is not None else ""

            free_ship = bool(re.search(r"free\s*s/?h|free\s*ship", title, re.I))

            if item_id and title:
                items.append({
                    "id":        item_id,
                    "title":     title,
                    "slug":      get_slug(item_id, title),
                    "ebay_url":  f"https://www.ebay.com/itm/{item_id}",
                    "img":       img,
                    "price":     price,
                    "free_ship": free_ship,
                    "category":  categorize(title),
                    "ebay_desc": desc,
                })

        print(f"  [Trading API] Page {page}/{total_pages}: {len(listing_array)} listings ({total_items} total)")
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.5)

    print(f"  [Trading API] Total fetched: {len(items)}")
    return items
# ─────────────────────────────────────────────────────────────
#  EXISTING PAGE INVENTORY
# ─────────────────────────────────────────────────────────────
def load_existing():
    """
    Returns dict of item_id -> 'active' | 'tombstone'.
    Matches any slug format ending in -ITEMID.html or item-ITEMID.html.
    Deletes any files that don't match valid slug pattern (cleanup stale files).
    """
    existing = {}
    if not os.path.isdir(OUTPUT_DIR):
        return existing
    for fname in os.listdir(OUTPUT_DIR):
        # Match both old format (item-ID.html) and new (words-ID.html)
        m = re.search(r"-(\d{10,})\.html$", fname)
        if not m:
            # Delete files that don't match any valid pattern
            stale_path = os.path.join(OUTPUT_DIR, fname)
            os.remove(stale_path)
            print(f"  [cleanup] Removed stale file: {fname}")
            continue
        item_id = m.group(1)
        path = os.path.join(OUTPUT_DIR, fname)
        with open(path, "r", encoding="utf-8") as f:
            page_content = f.read()
        if "missed-this-one" in page_content or "LOOKS LIKE YOU MISSED" in page_content:
            existing[item_id] = ("tombstone", fname)
        else:
            existing[item_id] = ("active", fname)
    return existing

# ─────────────────────────────────────────────────────────────
#  HTML TEMPLATES
# ─────────────────────────────────────────────────────────────
GA_SNIPPET = f"""<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id={GA_ID}"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag('js', new Date());
  gtag('config', '{GA_ID}');
</script>"""

SHARED_FONTS = '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&family=VT323&display=swap" rel="stylesheet">'

SHARED_CSS = """
<style>
:root{--bg:#0a0a0a;--bg2:#0f0f0f;--cyan:#00c8ff;--cyan-dim:#0088bb;--cyan-glow:rgba(0,200,255,0.15);--green:#00ff41;--text:#a0d8e8;--text-dim:#507080;--border:#1a1a2a;}
*{box-sizing:border-box;margin:0;padding:0;}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:'Share Tech Mono',monospace;overflow-x:hidden;}

.wrap{position:relative;z-index:1;max-width:860px;margin:0 auto;padding:40px 20px 60px;}
.topbar{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--cyan-dim);padding-bottom:12px;margin-bottom:40px;font-size:11px;letter-spacing:2px;}
.topbar-l{color:var(--text-dim);text-transform:uppercase;}
.topbar-r a{color:var(--cyan-dim);text-decoration:none;font-size:11px;}
.topbar-r a:hover{color:var(--cyan);}
.divider{height:1px;background:linear-gradient(90deg,transparent,var(--cyan),transparent);margin:0 auto 32px;}
footer{border-top:1px solid var(--border);padding-top:20px;display:flex;justify-content:space-between;align-items:center;font-size:11px;color:var(--text-dim);margin-top:60px;}
footer a{color:var(--cyan-dim);text-decoration:none;}
footer a:hover{color:var(--cyan);}
.blink{animation:blink 1.2s step-end infinite;}
@keyframes blink{0%,100%{opacity:1;}50%{opacity:0;}}
</style>
"""

def build_active_page(item, all_items):
    """Generate a full active item page."""
    cat      = item["category"]
    extra_kw = CAT_EXTRA_KW.get(cat, "")
    price_str = f"${item['price']:.2f}" if item["price"] > 0 else "See listing"
    free_badge = '<span class="free-badge">FREE SHIPPING</span>' if item["free_ship"] else ""
    img_html = f'<img src="{escape(item["img"])}" alt="{escape(item["title"])}" class="item-img">' if item["img"] else '<div class="img-placeholder">[ NO IMAGE ]</div>'

    # Related items — same category, different id, up to 4
    related = [i for i in all_items if i["category"] == cat and i["id"] != item["id"]][:4]
    if len(related) < 4:
        related += [i for i in all_items if i["id"] != item["id"] and i not in related][:4 - len(related)]
    related = related[:4]

    related_html = ""
    for r in related:
        r_price = f"${r['price']:.2f}" if r["price"] > 0 else "See listing"
        r_img   = f'<img src="{escape(r["img"])}" alt="{escape(r["title"])}">' if r["img"] else "<div class='rp-img-ph'>[IMG]</div>"
        related_html += f"""
        <a href="{slug(r['id'])}.html" class="rp-card">
          <div class="rp-img">{r_img}</div>
          <div class="rp-body">
            <div class="rp-title">{escape(r['title'][:60])}{'...' if len(r['title'])>60 else ''}</div>
            <div class="rp-price">{r_price}</div>
          </div>
        </a>"""

    # eBay description section — expandable iframe
    raw_desc = item.get("ebay_desc", "")
    if raw_desc:
        # Encode as data URI so it renders in an iframe without a separate file
        import base64
        desc_bytes = raw_desc.encode("utf-8")
        desc_b64   = base64.b64encode(desc_bytes).decode("ascii")
        ebay_desc_section = f"""<div class="ebay-desc-wrap">
  <div class="ebay-desc-label">// Item Description</div>
  <button class="ebay-desc-toggle" onclick="toggleDesc(this)">▶ Expand Full Description</button>
  <iframe id="descFrame" class="ebay-desc-frame" src="data:text/html;base64,{desc_b64}"
    scrolling="auto" frameborder="0" onload="autoHeight(this)"></iframe>
</div>
<script>
function toggleDesc(btn){{
  var f=document.getElementById('descFrame');
  f.classList.toggle('open');
  btn.textContent=f.classList.contains('open')?'▼ Collapse Description':'▶ Expand Full Description';
}}
function autoHeight(f){{
  try{{
    var h=f.contentWindow.document.body.scrollHeight;
    f.style.height=(h+40)+'px';
  }}catch(e){{f.style.height='600px';}}
}}
</script>"""
    else:
        ebay_desc_section = ""  # no description available

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{GA_SNIPPET}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(item['title'])} — NostalgicSoftware.com</title>
<meta name="description" content="Buy {escape(item['title'])} on eBay from NostalgicSoftware.com. {price_str}. Ships fast. 100% positive feedback seller since 2001.">
<meta name="keywords" content="{escape(item['title'])}, buy {escape(item['title'].split()[0] if item['title'] else '')}, eBay {escape(cat)}, {escape(extra_kw)}, NostalgicSoftware, nostalgic-software eBay">
<meta name="robots" content="index, follow">
<meta name="revisit-after" content="3 days">
<meta property="og:title" content="{escape(item['title'])} — NostalgicSoftware.com">
<meta property="og:description" content="Buy {escape(item['title'])} — {price_str}. Ships fast from a trusted seller with 100% positive feedback.">
<meta property="og:type" content="product">
<meta property="og:url" content="{SITE_BASE}/items/{item.get('slug', slug(item['id'], item.get('title','')))}.html">
{f'<meta property="og:image" content="{escape(item["img"])}">' if item["img"] else ""}
<link rel="canonical" href="{SITE_BASE}/items/{item.get('slug', slug(item['id'], item.get('title','')))}.html">
{SHARED_FONTS}
{SHARED_CSS}
<style>
.item-layout{{display:grid;grid-template-columns:1fr 1fr;gap:40px;margin-bottom:40px;}}
.item-img-wrap{{}}
.item-img{{width:100%;border:1px solid var(--cyan-dim);box-shadow:0 0 30px var(--cyan-glow);display:block;}}
.img-placeholder{{width:100%;aspect-ratio:1;background:#111;display:flex;align-items:center;justify-content:center;font-family:'VT323',monospace;font-size:16px;color:#333;border:1px solid var(--border);}}
.item-meta{{display:flex;flex-direction:column;gap:16px;}}
.item-cat{{font-size:10px;color:var(--cyan-dim);letter-spacing:3px;text-transform:uppercase;}}
.item-title{{font-family:'Orbitron',sans-serif;font-size:clamp(14px,2vw,20px);font-weight:700;color:var(--text);line-height:1.4;}}
.item-price{{font-family:'Orbitron',sans-serif;font-size:clamp(20px,3vw,32px);font-weight:900;color:var(--cyan);text-shadow:0 0 20px rgba(0,200,255,0.5);}}
.free-badge{{display:inline-block;background:rgba(0,255,65,0.1);color:#00ff41;border:1px solid rgba(0,255,65,0.3);font-size:10px;letter-spacing:2px;padding:3px 10px;text-transform:uppercase;}}
.item-desc{{font-size:12px;color:var(--text-dim);line-height:1.7;}}
.buy-btn{{display:inline-block;font-family:'Orbitron',sans-serif;font-size:13px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:#0a0a0a;background:var(--cyan);padding:18px 48px;text-decoration:none;clip-path:polygon(8px 0%,100% 0%,calc(100% - 8px) 100%,0% 100%);transition:all 0.2s;margin-top:8px;}}
.buy-btn:hover{{background:#40d8ff;box-shadow:0 0 30px rgba(0,200,255,0.6);transform:translateY(-2px);}}
.trust-bar{{display:flex;gap:24px;padding:12px 16px;border:1px solid var(--border);background:var(--bg2);font-size:11px;color:var(--text-dim);}}
.trust-bar span{{color:var(--green);}}
/* eBay description section */
.ebay-desc-wrap{{margin:32px 0;}}
.ebay-desc-label{{font-family:'Orbitron',sans-serif;font-size:11px;font-weight:700;color:var(--cyan);letter-spacing:3px;text-transform:uppercase;margin-bottom:14px;}}
.ebay-desc-toggle{{background:none;border:1px solid var(--cyan-dim);color:var(--cyan-dim);font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:2px;padding:8px 20px;cursor:pointer;text-transform:uppercase;transition:all 0.2s;}}
.ebay-desc-toggle:hover{{border-color:var(--cyan);color:var(--cyan);}}
.ebay-desc-frame{{width:100%;border:1px solid var(--border);background:#fff;margin-top:12px;display:none;}}
.ebay-desc-frame.open{{display:block;}}
.related-title{{font-family:'Orbitron',sans-serif;font-size:11px;font-weight:700;color:var(--cyan);letter-spacing:3px;text-transform:uppercase;margin-bottom:14px;}}
.related-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:2px;}}
.rp-card{{background:var(--bg2);border:1px solid var(--border);text-decoration:none;color:inherit;transition:border-color 0.2s;display:block;}}
.rp-card:hover{{border-color:var(--cyan-dim);}}
.rp-img{{width:100%;aspect-ratio:1;overflow:hidden;background:#111;}}
.rp-img img{{width:100%;height:100%;object-fit:cover;opacity:0.9;}}
.rp-img-ph{{width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-family:'VT323',monospace;font-size:12px;color:#333;}}
.rp-body{{padding:8px;}}
.rp-title{{font-size:10px;color:var(--text);line-height:1.4;margin-bottom:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}}
.rp-price{{font-family:'Orbitron',sans-serif;font-size:11px;font-weight:700;color:var(--cyan);}}
@media(max-width:600px){{.item-layout{{grid-template-columns:1fr;}}.related-grid{{grid-template-columns:repeat(2,1fr);}}.trust-bar{{flex-direction:column;gap:8px;}}}}
</style>
</head>
<body>
<div class="wrap">

  <div class="topbar">
    <div class="topbar-l">// nostalgicsoftware.com</div>
    <div class="topbar-r">
      <a href="../index.html">← Back to Store</a>
      &nbsp;&nbsp;
      <a href="https://www.ebay.com/usr/nostalgic-software" target="_blank" rel="noopener">eBay Store ↗</a>
    </div>
  </div>

  <div class="divider"></div>

  <div class="item-layout">
    <div class="item-img-wrap">
      {img_html}
    </div>
    <div class="item-meta">
      <div class="item-cat">// {escape(cat)}</div>
      <div class="item-title">{escape(item['title'])}</div>
      <div class="item-price">{price_str}</div>
      {free_badge}
      <div class="item-desc">Listed on eBay by <strong style="color:var(--text)">nostalgic-software</strong> — trusted seller since 2001 with 100% positive feedback and 2,300+ items sold. Ships fast.</div>
      <a href="{item['ebay_url']}" target="_blank" rel="noopener" class="buy-btn">Buy Now on eBay →</a>
      <div class="trust-bar">
        <div><span>✓</span> 100% Positive Feedback</div>
        <div><span>✓</span> Since 2001</div>
        <div><span>✓</span> 2,300+ Sold</div>
      </div>
    </div>
  </div>

  <div class="divider"></div>

  {ebay_desc_section}

  <div class="related-title">// You Might Also Like</div>
  <div class="related-grid">
    {related_html}
  </div>

  <footer>
    <div>© 2026 NostalgicSoftware.com</div>
    <div><a href="../index.html">← All Listings</a> &nbsp;|&nbsp; <a href="https://www.ebay.com/usr/nostalgic-software" target="_blank" rel="noopener">eBay Store</a></div>
  </footer>

</div>
</body>
</html>"""


def generate_seo_paragraph(title, category):
    """Generate 2 unique paragraphs of SEO content for tombstone pages. Returns HTML."""
    t = escape(title.strip())

    if category == "disney":
        return (f"<p><strong>{t}</strong> was a Disney collectible sold through NostalgicSoftware.com. "
                f"Walt Disney World resort merchandise, Disney pins, plush, and limited edition theme park items "
                f"are among the most actively collected categories on eBay. Pins alone have a dedicated global "
                f"collector community with thousands of trading and purchasing transactions daily. "
                f"Items sourced directly from Walt Disney World resorts and parks carry particular value "
                f"as they are exclusive to the parks and unavailable through standard retail channels.</p>"
                f"<p>NostalgicSoftware.com has been a trusted Disney collectibles source on eBay since 2001 — "
                f"over two decades of sourcing pins, plush, apparel, mugs, and limited edition merchandise "
                f"from Walt Disney World and Disneyland. With 100% positive feedback across 2,300+ transactions, "
                f"our store is a reliable destination for Disney collectors. "
                f"Browse our active Disney listings for currently available pieces.</p>")

    elif category == "collectibles":
        return (f"<p><strong>{t}</strong> was a rare collectible sold through NostalgicSoftware.com. "
                f"For Your Consideration screeners, vintage promotional DVDs, and awards-season discs "
                f"represent a unique category of Hollywood memorabilia. Studios distributed these exclusively "
                f"to Academy members, guild voters, and industry professionals during awards voting seasons — "
                f"they were never sold at retail and most were intended to be destroyed after the voting period. "
                f"Surviving examples in sellable condition are increasingly rare and sought after by film collectors worldwide.</p>"
                f"<p>NostalgicSoftware.com specializes in hard-to-find collectibles spanning vintage advertising, "
                f"promotional media, limited edition items, and pop culture memorabilia. "
                f"Trusted on eBay since 2001 with 100% positive feedback across 2,300+ completed sales. "
                f"Browse our active collectibles listings — new rare finds listed regularly.</p>")

    elif category == "electronics":
        return (f"<p><strong>{t}</strong> was a vintage electronics or OEM replacement parts item "
                f"sold through NostalgicSoftware.com. Sourcing original manufacturer parts for discontinued "
                f"consumer electronics can be challenging as retail supply chains age out components years before "
                f"the devices themselves become obsolete. eBay remains the primary marketplace where genuine OEM "
                f"remote controls, power supplies, cables, and accessories continue to circulate after retail availability ends.</p>"
                f"<p>NostalgicSoftware.com has been sourcing and selling genuine electronics parts and accessories "
                f"on eBay since 2001 — covering Sony, Samsung, Keurig, Razor, Nintendo, and dozens of other brands. "
                f"100% positive feedback, ships fast via USPS. "
                f"Browse our active electronics listings for currently available parts and accessories.</p>")

    elif category == "sports":
        return (f"<p><strong>{t}</strong> was a sports collectible sold through NostalgicSoftware.com. "
                f"The Tampa Bay Lightning's back-to-back Stanley Cup championships in 2020 and 2021 produced "
                f"a wave of limited edition licensed merchandise that has grown in collectible value as the "
                f"championships recede in time. Championship-era Bud Light bottles, magnets, and licensed items "
                f"from both Cup runs represent a unique dual-championship moment in NHL history that may not repeat for decades.</p>"
                f"<p>NostalgicSoftware.com specializes in Tampa Bay Lightning memorabilia alongside gaming accessories, "
                f"Nintendo, PlayStation, and Xbox items, and sports collectibles across major leagues. "
                f"Trusted eBay seller since 2001, 100% positive feedback, 2,300+ items sold. "
                f"Browse our active sports listings for currently available items.</p>")

    elif category == "home":
        return (f"<p><strong>{t}</strong> was a home goods item sold through NostalgicSoftware.com. "
                f"Genuine OEM replacement parts for popular home appliances — particularly Keurig coffee makers — "
                f"are frequently discontinued by manufacturers before the appliances themselves wear out, "
                f"leaving owners searching eBay and specialty retailers for original components. "
                f"Finding the correct part from a trusted seller with verifiable feedback is critical "
                f"to ensuring compatibility and longevity.</p>"
                f"<p>NostalgicSoftware.com carries Keurig K-Duo replacement parts, Starbucks whole bean coffee, "
                f"LED lighting, outdoor accessories, and a rotating selection of household items at competitive prices. "
                f"Trusted eBay seller since 2001, ships fast via USPS First Class. "
                f"Browse our active home and kitchen listings.</p>")

    elif category == "clothing":
        return (f"<p><strong>{t}</strong> was a clothing item sold through NostalgicSoftware.com. "
                f"Our apparel selection spans Disney character merchandise, seasonal and holiday clothing, "
                f"genuine leather jackets, loungewear, and wearable collectibles across a wide range of "
                f"sizes and styles. Vintage and branded apparel from established names retains value "
                f"particularly when in new or excellent condition.</p>"
                f"<p>NostalgicSoftware.com has offered clothing and apparel on eBay since 2001 — "
                f"from lambskin leather jackets to Disney rain ponchos and Halloween seasonal pieces. "
                f"100% positive feedback, fast USPS shipping. "
                f"Browse our active clothing listings for currently available items.</p>")

    elif category == "beauty":
        return (f"<p><strong>{t}</strong> was a health and beauty item sold through NostalgicSoftware.com. "
                f"Specialty health items — CPAP replacement cushions, plantar fasciitis supports, "
                f"pulse therapy massagers, and professional nail systems — are frequently discontinued "
                f"by manufacturers or become difficult to source through standard retail after initial "
                f"product runs end. eBay fills this gap as a reliable secondary market for these items.</p>"
                f"<p>NostalgicSoftware.com sources health, wellness, and beauty accessories across "
                f"personal care, therapeutic devices, and cosmetic tools. "
                f"Trusted since 2001 with 100% positive feedback, ships fast. "
                f"Browse our active health and beauty listings for currently available items.</p>")

    elif category == "toys":
        return (f"<p><strong>{t}</strong> was a toys and kids item sold through NostalgicSoftware.com. "
                f"Children's collectibles — Disney Happy Meal toys, Tsum Tsum ornaments, limited edition "
                f"holiday decorations, and themed kids products — often carry collector value well beyond "
                f"their original retail price, particularly for items tied to specific Disney parks, "
                f"film releases, or seasonal limited runs that are no longer manufactured.</p>"
                f"<p>NostalgicSoftware.com carries Disney toys, McDonald's collectibles, baby and toddler "
                f"products, outdoor play equipment, and holiday items for children. "
                f"Trusted eBay seller since 2001, 2,300+ items sold, 100% positive feedback. "
                f"Browse our active toys and kids listings.</p>")

    else:
        return (f"<p><strong>{t}</strong> was a unique item sold through NostalgicSoftware.com — "
                f"an eBay storefront with over 25 years of selling history, 100% positive feedback, "
                f"and more than 2,300 completed transactions. Our inventory spans collectibles, "
                f"vintage electronics, home goods, clothing, Disney merchandise, sports memorabilia, "
                f"health and beauty, and more — with new items listed on a regular basis.</p>"
                f"<p>NostalgicSoftware.com has operated on eBay continuously since November 2001, "
                f"making it one of the longer-running independent storefronts on the platform. "
                f"We source from estate sales, closeouts, unique finds, and specialty channels. "
                f"Browse our active listings — something worth finding arrives every week.</p>")


def build_tombstone_page(item_id, old_title, old_img, old_cat, suggestions):
    """Generate a sold/unavailable tombstone page with suggestions."""
    extra_kw = CAT_EXTRA_KW.get(old_cat, "")
    seo_paragraph = generate_seo_paragraph(old_title or "This item", old_cat)
    sugg_html = ""
    for s in suggestions[:4]:
        s_price = f"${s['price']:.2f}" if s["price"] > 0 else "See listing"
        s_img   = f'<img src="{escape(s["img"])}" alt="{escape(s["title"])}">' if s["img"] else "<div class='rp-img-ph'>[IMG]</div>"
        s_slug  = s.get("slug", get_slug(s["id"], s.get("title","")))
        sugg_html += f"""
        <a href="{s_slug}.html" class="rp-card">
          <div class="rp-img">{s_img}</div>
          <div class="rp-body">
            <div class="rp-title">{escape(s['title'][:60])}{'...' if len(s['title'])>60 else ''}</div>
            <div class="rp-price">{s_price}</div>
          </div>
        </a>"""

    old_title_esc = escape(old_title) if old_title else "This Item"
    img_html = (f'<img src="{escape(old_img)}" alt="{old_title_esc}" class="sold-img" loading="eager">'
                if old_img else '<div class="sold-img" style="aspect-ratio:1;background:#111;min-height:200px;"></div>')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{GA_SNIPPET}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{old_title_esc} — Sold | NostalgicSoftware.com</title>
<meta name="description" content="{old_title_esc} has sold or is no longer available at NostalgicSoftware.com. Browse similar items from our active eBay store.">
<meta name="keywords" content="{escape(old_title)}, {escape(extra_kw)}, NostalgicSoftware.com, nostalgic-software eBay">
<meta name="robots" content="index, follow">
<link rel="canonical" href="{SITE_BASE}/items/{slug(item_id)}.html">
{SHARED_FONTS}
{SHARED_CSS}
<style>
.sold-img-wrap{{position:relative;width:100%;max-width:480px;margin:0 auto 32px;}}
.sold-img{{width:100%;display:block;filter:grayscale(80%) opacity(0.5);border:1px solid var(--border);}}
.sold-stamp{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%) rotate(-25deg);font-family:'Orbitron',sans-serif;font-size:clamp(32px,8vw,72px);font-weight:900;color:#ff2222;border:6px solid #ff2222;padding:8px 20px;opacity:0.85;letter-spacing:6px;text-transform:uppercase;white-space:nowrap;pointer-events:none;text-shadow:0 0 20px rgba(255,34,34,0.4);}}
.sold-layout{{display:grid;grid-template-columns:1fr 1fr;gap:40px;margin-bottom:40px;align-items:start;}}
.sold-meta{{display:flex;flex-direction:column;gap:16px;}}
.sold-cat{{font-size:10px;color:var(--text-dim);letter-spacing:3px;text-transform:uppercase;}}
.sold-title{{font-family:'Orbitron',sans-serif;font-size:clamp(13px,2vw,17px);font-weight:700;color:var(--text-dim);line-height:1.5;opacity:0.75;}}
.sold-badge{{display:inline-block;background:rgba(255,34,34,0.1);color:#ff4444;border:1px solid rgba(255,34,34,0.4);font-family:'Orbitron',sans-serif;font-size:11px;font-weight:700;letter-spacing:3px;padding:6px 16px;text-transform:uppercase;}}
.sold-heading{{font-family:'Orbitron',sans-serif;font-size:clamp(16px,2.5vw,22px);font-weight:900;color:var(--cyan);text-shadow:0 0 20px rgba(0,200,255,0.4);}}
.sold-sub{{font-size:12px;color:var(--text-dim);line-height:1.7;}}
.seo-section{{background:var(--bg2);border:1px solid var(--border);padding:20px 24px;margin:32px 0;}}
.seo-label{{font-family:'Orbitron',sans-serif;font-size:10px;font-weight:700;color:var(--cyan-dim);letter-spacing:3px;text-transform:uppercase;margin-bottom:10px;}}
.seo-text{{font-size:12px;color:var(--text-dim);line-height:1.8;}}
.browse-btn{{display:inline-block;font-family:'Orbitron',sans-serif;font-size:12px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:#0a0a0a;background:var(--cyan);padding:14px 36px;text-decoration:none;clip-path:polygon(8px 0%,100% 0%,calc(100% - 8px) 100%,0% 100%);transition:all 0.2s;}}
.browse-btn:hover{{background:#40d8ff;box-shadow:0 0 30px rgba(0,200,255,0.6);transform:translateY(-2px);}}
.sugg-title{{font-family:'Orbitron',sans-serif;font-size:11px;font-weight:700;color:var(--cyan);letter-spacing:3px;text-transform:uppercase;margin-bottom:14px;}}
.related-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:2px;}}
.rp-card{{background:var(--bg2);border:1px solid var(--border);text-decoration:none;color:inherit;transition:border-color 0.2s;display:block;}}
.rp-card:hover{{border-color:var(--cyan-dim);}}
.rp-img{{width:100%;aspect-ratio:1;overflow:hidden;background:#111;}}
.rp-img img{{width:100%;height:100%;object-fit:cover;opacity:0.9;}}
.rp-img-ph{{width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-family:'VT323',monospace;font-size:12px;color:#333;}}
.rp-body{{padding:8px;}}
.rp-title{{font-size:10px;color:var(--text);line-height:1.4;margin-bottom:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}}
.rp-price{{font-family:'Orbitron',sans-serif;font-size:11px;font-weight:700;color:var(--cyan);}}
@media(max-width:600px){{.sold-layout{{grid-template-columns:1fr;}}.related-grid{{grid-template-columns:repeat(2,1fr);}}}}
</style>
</head>
<body>
<div class="wrap">

  <div class="topbar">
    <div class="topbar-l">// nostalgicsoftware.com</div>
    <div class="topbar-r">
      <a href="../index.html">← Back to Store</a>
      &nbsp;&nbsp;
      <a href="https://www.ebay.com/usr/nostalgic-software" target="_blank" rel="noopener">eBay Store ↗</a>
    </div>
  </div>

  <div class="divider"></div>

  <div class="divider"></div>

  <div class="sold-layout">
    <div>
      <div class="sold-img-wrap">
        {img_html}
        <div class="sold-stamp">SOLD</div>
      </div>
    </div>
    <div class="sold-meta">
      <div class="sold-cat">// {escape(old_cat)}</div>
      <div class="sold-status">&#10003; SOLD</div>
      <div class="sold-title">{old_title_esc}</div>
      <div class="sold-detail"><strong>Seller:</strong> nostalgic-software &nbsp;|&nbsp; <strong>Platform:</strong> eBay</div>
      <div class="sold-detail">Trusted since 2001 — 100% positive feedback, 2,300+ items sold.</div>
      <a href="https://www.ebay.com/itm/{item_id}" target="_blank" rel="noopener" class="ebay-link">View on eBay &#8599;</a>
      <a href="../index.html" class="browse-btn">Browse Active Listings &#8594;</a>
      <div class="trust-bar">
        <div><span>&#10003;</span> 100% Positive</div>
        <div><span>&#10003;</span> Since 2001</div>
        <div><span>&#10003;</span> 2,300+ Sold</div>
      </div>
    </div>
  </div>

  <div class="divider"></div>

  <div class="seo-section">
    <div class="seo-label">// About This Item</div>
    <div class="seo-text">{seo_paragraph}</div>
  </div>

  <div class="divider"></div>

  <div class="sugg-title">// You Might Still Like These</div>
  <div class="related-grid">
    {sugg_html}
  </div>

  <footer>
    <div>© 2026 NostalgicSoftware.com</div>
    <div><a href="../index.html">← All Listings</a> &nbsp;|&nbsp; <a href="https://www.ebay.com/usr/nostalgic-software" target="_blank" rel="noopener">eBay Store</a></div>
  </footer>

</div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
#  SITEMAP WRITER
# ─────────────────────────────────────────────────────────────
def write_sitemap(active_ids, tombstone_ids, live_by_id=None):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
             '',
             '  <!-- Homepage -->',
             '  <url>',
             f'    <loc>{SITE_BASE}/</loc>',
             f'    <lastmod>{TODAY}</lastmod>',
             '    <changefreq>daily</changefreq>',
             '    <priority>1.0</priority>',
             '  </url>',
             '']

    for item_id in sorted(active_ids):
        lines += [
            '  <url>',
            f'    <loc>{SITE_BASE}/items/{slug(item_id, live_by_id.get(item_id, {}).get("title", ""))}.html</loc>',
            f'    <lastmod>{TODAY}</lastmod>',
            '    <changefreq>daily</changefreq>',
            '    <priority>0.8</priority>',
            '  </url>',
            ''
        ]

    for item_id in sorted(tombstone_ids):
        lines += [
            '  <url>',
            f'    <loc>{SITE_BASE}/items/{slug(item_id, live_by_id.get(item_id, {}).get("title", ""))}.html</loc>',
            f'    <lastmod>{TODAY}</lastmod>',
            '    <changefreq>monthly</changefreq>',
            '    <priority>0.3</priority>',
            '  </url>',
            ''
        ]

    lines.append('</urlset>')

    with open(SITEMAP_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  Wrote {SITEMAP_PATH}  ({len(active_ids)} active + {len(tombstone_ids)} tombstone URLs)")


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def main():
    print("\n╔══════════════════════════════════════════════╗")
    print("║  NostalgicSoftware.com — Page Generator      ║")
    print(f"║  {TODAY}                                ║")
    print("╚══════════════════════════════════════════════╝\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 1. Fetch live feed
    live_items  = fetch_items()
    live_ids    = {i["id"] for i in live_items}
    live_by_id  = {i["id"]: i for i in live_items}

    # 1b. Images come from Trading API directly
    # Descriptions fetched via Shopping API with local cache (desc_cache.json)
    imgs_found = sum(1 for i in live_items if i.get("img"))
    imgs_empty = [i["id"] for i in live_items if not i.get("img")]
    print(f"  Images from Trading API: {imgs_found}/{len(live_items)}")
    if imgs_empty:
        print(f"  Items with no image: {imgs_empty}")
    for item in live_items:
        if item.get("img"):
            print(f"  Sample image URL: {item['img'][:100]}")
            break

    print("  Fetching descriptions (cached)...")
    ebay_descs = fetch_ebay_descriptions(live_ids)
    for iid, item in live_by_id.items():
        item["ebay_desc"] = ebay_descs.get(iid, "")
    desc_found = sum(1 for i in live_items if i.get("ebay_desc"))
    print(f"  Descriptions loaded: {desc_found}/{len(live_items)}")
    print()

    # Save listings cache — preserves title/image/category when items sell
    save_listings_cache(live_items)

    # 2. Load existing pages — cleans stale files automatically
    existing    = load_existing()
    # existing values are tuples: (status, filename)
    existing_active    = {k for k, v in existing.items() if v[0] == "active"}
    existing_tombstone = {k for k, v in existing.items() if v[0] == "tombstone"}

    # 3. Diff
    new_ids  = live_ids - set(existing.keys())
    sold_ids = existing_active - live_ids

    print(f"  Live in feed:      {len(live_ids)}")
    print(f"  New listings:      {len(new_ids)}")
    print(f"  Rebuilding:        {len(live_ids)}")
    print(f"  Sold/gone:         {len(sold_ids)}")
    print(f"  Already tombstone: {len(existing_tombstone)}\n")

    # 4. Rebuild all active pages every run
    #    GitHub Actions always starts with a fresh checkout so pages must be regenerated.
    #    Slugs are permanent via slugs.json — URLs never change even though pages rebuild.
    wrote = 0
    for item_id in live_ids:
        item     = live_by_id[item_id]
        new_path = filename(item_id, item.get("title", ""))
        html     = build_active_page(item, live_items)
        with open(new_path, "w", encoding="utf-8") as f:
            f.write(html)
        tag = "NEW" if item_id in new_ids else "BUILD"
        if item_id in new_ids:
            print(f"  [NEW] {new_path}")
        wrote += 1

    # 5. Convert sold items to tombstones — keep URL alive with suggestions
    listings_cache = load_listings_cache()
    for item_id in sold_ids:
        old_title = ""
        old_img   = ""
        old_cat   = "other"
        tomb_path = None

        # First try listings cache — most reliable since it's saved every run
        if item_id in listings_cache:
            cached = listings_cache[item_id]
            old_title = cached.get("title", "")
            old_img   = cached.get("img", "")
            old_cat   = cached.get("category", "other")
            # Upgrade image URL to full-res
            if old_img:
                old_img = re.sub(r"s-l\d+", "s-l1600", old_img)
                old_img = re.sub(r"\$_\d+", "$_57", old_img)

        # If no image from cache, try fetching from Trading API GetItem
        if not old_img and EBAY_USER_TOKEN:
            try:
                ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
                xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{EBAY_USER_TOKEN}</eBayAuthToken></RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ItemReturnAttributes</DetailLevel>
</GetItemRequest>"""
                req = urllib.request.Request(
                    "https://api.ebay.com/ws/api.dll",
                    data=xml_body.encode("utf-8"),
                    headers={
                        "X-EBAY-API-SITEID":"0","X-EBAY-API-COMPATIBILITY-LEVEL":"1113",
                        "X-EBAY-API-CALL-NAME":"GetItem","X-EBAY-API-APP-NAME":EBAY_APP_ID,
                        "X-EBAY-API-CERT-NAME":EBAY_CERT_ID,"Content-Type":"text/xml;charset=utf-8",
                    }
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    root = ET.fromstring(r.read())
                item_el = root.find("e:Item", namespaces=ns) or root.find("Item")
                if item_el is not None:
                    for child in item_el:
                        if child.tag.split("}")[-1] == "PictureDetails":
                            for pic in child:
                                if pic.tag.split("}")[-1] == "PictureURL" and pic.text:
                                    old_img = re.sub(r"s-l\d+","s-l1600",pic.text)
                                    old_img = re.sub(r"\$_\d+","$_57",old_img)
                                    break
                            break
            except Exception as e:
                pass  # silent fail — image just won't show

        # Fall back to parsing existing page on disk if cache misses
        if not old_title and item_id in existing:
            old_fname = existing[item_id][1]
            old_path  = os.path.join(OUTPUT_DIR, old_fname)
            if os.path.exists(old_path):
                with open(old_path, "r", encoding="utf-8") as f:
                    page_content = f.read()
                t = re.search(r"<title>(.+?) — NostalgicSoftware", page_content)
                if t: old_title = t.group(1)
                im = re.search(r'<meta property="og:image" content="([^"]+)"', page_content)
                if im: old_img = im.group(1)
                c = re.search(r"// (\w+)</div>", page_content)
                if c: old_cat = c.group(1)

        # Determine tomb file path
        if item_id in existing:
            tomb_path = os.path.join(OUTPUT_DIR, existing[item_id][1])
        if not tomb_path:
            tomb_path = filename(item_id, old_title or item_id)

        suggestions = [i for i in live_items if i["category"] == old_cat][:4]
        if len(suggestions) < 4:
            suggestions += live_items[:4 - len(suggestions)]

        html = build_tombstone_page(item_id, old_title, old_img, old_cat, suggestions)
        with open(tomb_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  [SOLD→TOMBSTONE] {tomb_path}  (\"{old_title[:50]}\")")
        wrote += 1
    # 6. Sitemap
    write_sitemap(live_ids, existing_tombstone | sold_ids, live_by_id)

    # 7. Update index.html CATALOG with real Trading API image URLs
    if os.path.exists("index.html"):
        try:
            with open("index.html", "r", encoding="utf-8") as f:
                idx_html = f.read()

            # Build updated CATALOG JSON
            catalog = []
            for item in live_items:
                catalog.append({
                    "id":    item["id"],
                    "title": item["title"],
                    "price": item["price"],
                    "free":  item["free_ship"],
                    "cat":   item["category"],
                    "img":   item["img"],
                    "slug":  item.get("slug", get_slug(item["id"], item["title"])),
                    "url":   item["ebay_url"],
                    "sold":  False,
                })

            # Add tombstone items to CATALOG with sold=True
            for tomb_id in (existing_tombstone | sold_ids):
                tomb_fname = existing.get(tomb_id, ("tombstone",""))[1] if tomb_id in existing else ""
                tomb_slug  = tomb_fname.replace(".html","") if tomb_fname else get_slug(tomb_id)
                # Try to get title/img from existing tombstone page
                tomb_path  = os.path.join(OUTPUT_DIR, tomb_fname) if tomb_fname else ""
                tomb_title, tomb_img, tomb_cat = "", "", "other"
                if tomb_path and os.path.exists(tomb_path):
                    try:
                        pg = open(tomb_path, encoding="utf-8").read()
                        import re as _re
                        m = _re.search(r'<div class="sold-title">([^<]+)</div>', pg)
                        if m: tomb_title = m.group(1)
                        m2 = _re.search(r'class="sold-img"[^>]*src="([^"]+)"', pg)
                        if not m2: m2 = _re.search(r'src="([^"]+)"[^>]*class="sold-img"', pg)
                        if m2: tomb_img = m2.group(1)
                        m3 = _re.search(r'<div class="sold-cat">// ([^<]+)</div>', pg)
                        if m3: tomb_cat = m3.group(1).strip()
                    except: pass
                if tomb_title or tomb_slug:
                    catalog.append({
                        "id":    tomb_id,
                        "title": tomb_title or tomb_slug,
                        "price": 0,
                        "free":  False,
                        "cat":   tomb_cat,
                        "img":   tomb_img,
                        "slug":  tomb_slug,
                        "url":   f"https://www.ebay.com/itm/{tomb_id}",
                        "sold":  True,
                    })

            new_catalog_str = "const CATALOG = " + json.dumps(catalog, ensure_ascii=False) + ";"
            # Replace existing CATALOG definition
            # Find and replace the CATALOG block
            cat_start = idx_html.find("const CATALOG = [")
            cat_end   = idx_html.find("];", cat_start) + 2
            if cat_start >= 0 and cat_end > cat_start:
                idx_html = idx_html[:cat_start] + new_catalog_str + idx_html[cat_end:]
            with open("index.html", "w", encoding="utf-8") as f:
                f.write(idx_html)
            print(f"  index.html CATALOG updated with {len(catalog)} live items and real image URLs")
        except Exception as e:
            print(f"  WARNING: Could not update index.html CATALOG: {e}")

    print(f"\n  ✓ Done. {wrote} pages written.\n")


if __name__ == "__main__":
    main()
