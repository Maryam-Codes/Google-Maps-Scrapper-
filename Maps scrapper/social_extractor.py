"""
social_extractor.py
-------------------
Hybrid two-tier extraction engine:

  TIER 1 — requests + BeautifulSoup  (fast, ~0.5s per site)
      Works for static sites and SSR frameworks (WordPress, Squarespace
      classic, basic HTML sites). Tries the homepage then common sub-pages.

  TIER 2 — Playwright headless browser  (thorough, ~4-6s per site)
      Kicks in only when Tier 1 finds nothing. Launches a real Chromium
      instance, waits for the page to fully load, scrolls all the way to
      the bottom (triggering lazy-load and JS-rendered content), then
      parses the live DOM. Catches React / Next.js / Webflow / Squarespace
      sites where the footer is injected by JavaScript after page load.

Setup (one-time):
    pip install playwright
    playwright install chromium
"""

import re
import time
import json
import random
import requests
import pandas as pd
from bs4 import BeautifulSoup
from typing import Optional
from urllib.parse import urlparse, urljoin


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IG_NOT_PROFILE = {"/p/", "/reel/", "/stories/", "/explore/", "/tv/", "/accounts/"}

_IG_RESERVED = {
    "instagram", "home", "privacy", "terms", "about", "accounts",
    "explore", "reels", "stories", "direct", "p", "reel", "tv",
    "legal", "safety", "support", "help", "press", "sharer",
    "plugins", "share", "intent", "hashtag", "web", "developer",
}

_FB_BLOCKLIST = {
    "sharer.php", "/plugins/", "/tr?", "connect.facebook.net",
    "facebook.com/tr", "facebook.net", "facebook.com/dialog/",
    "facebook.com/login", "facebook.com/watch",
    # Legacy Facebook embed/FBML paths — not real pages
    "2008/fbml", "facebook.com/share", "facebook.com/sharer",
    "facebook.com/v2", "facebook.com/ajax",
}

_IG_PROFILE_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)/?", re.IGNORECASE)
_FB_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:facebook\.com|fb\.com)"
    r"(?:/(?:pages/[^/?\"'\s]+|\w[\w.%-]*|profile\.php)(?:[/?][^\"'\s]*)?)?",
    re.IGNORECASE,
)

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# Sub-pages tried as fallback when homepage has no socials
_FALLBACK_PATHS = [
    "/contact", "/contact-us", "/about", "/about-us",
    "/find-us", "/connect", "/follow", "/social",
]


# ---------------------------------------------------------------------------
# URL cleaners  (shared by both tiers)
# ---------------------------------------------------------------------------

def clean_ig_url(raw: str) -> str:
    if not raw or "instagram.com" not in raw:
        return "N/A"
    for bad in _IG_NOT_PROFILE:
        if bad in raw:
            return "N/A"
    m = _IG_PROFILE_RE.search(raw)
    if not m:
        return "N/A"
    username = m.group(1).split("?")[0].rstrip("/").lower()
    if not username or username in _IG_RESERVED or len(username) < 2:
        return "N/A"
    return f"https://www.instagram.com/{username}"


def clean_fb_url(raw: str) -> str:
    if not raw or not ("facebook.com" in raw or "fb.com" in raw):
        return "N/A"
    raw_lower = raw.lower()
    if any(bad in raw_lower for bad in _FB_BLOCKLIST):
        return "N/A"
    # profile.php?id= — KEEP the query string (it IS the page identity)
    if "profile.php" in raw:
        m = re.search(r"(https?://(?:www\.)?facebook\.com/profile\.php\?id=\d+)", raw)
        return m.group(1) if m else "N/A"
    clean = raw.split("?")[0].rstrip("/")
    parsed = urlparse(clean)
    path = parsed.path.strip("/")
    if not path or path in {"home", "pages"}:
        return "N/A"
    return clean


# ---------------------------------------------------------------------------
# HTML parsing helpers  (used by both tiers)
# ---------------------------------------------------------------------------

def _links_from_element(element) -> tuple:
    ig = fb = "N/A"
    for a in element.find_all("a", href=True):
        href = str(a.get("href", ""))
        if ig == "N/A" and "instagram.com" in href:
            ig = clean_ig_url(href)
        if fb == "N/A" and ("facebook.com" in href or "fb.com" in href):
            fb = clean_fb_url(href)
        if ig != "N/A" and fb != "N/A":
            break
    return ig, fb


def extract_from_html(html: str) -> tuple:
    """
    Run all parsing passes on an HTML string.
    Returns (instagram_url_or_N/A, facebook_url_or_N/A).
    """
    soup = BeautifulSoup(html, "lxml")
    ig = fb = "N/A"

    # Pass 1 — JSON-LD sameAs (most authoritative)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                same_as = item.get("sameAs", [])
                if isinstance(same_as, str):
                    same_as = [same_as]
                for u in same_as:
                    if ig == "N/A" and "instagram.com" in u:
                        ig = clean_ig_url(u)
                    if fb == "N/A" and ("facebook.com" in u or "fb.com" in u):
                        fb = clean_fb_url(u)
        except Exception:
            continue
        if ig != "N/A" and fb != "N/A":
            break

    if ig != "N/A" and fb != "N/A":
        return ig, fb

    # Pass 2 — footer tag first (social icons almost always live here)
    footer = soup.find("footer")
    if footer:
        p_ig, p_fb = _links_from_element(footer)
        if ig == "N/A": ig = p_ig
        if fb == "N/A": fb = p_fb

    if ig != "N/A" and fb != "N/A":
        return ig, fb

    # Pass 2b — divs/sections with "footer" in class or id
    for el in soup.find_all(True, {"class": re.compile(r"footer", re.I)}):
        p_ig, p_fb = _links_from_element(el)
        if ig == "N/A": ig = p_ig
        if fb == "N/A": fb = p_fb
        if ig != "N/A" and fb != "N/A":
            break

    if ig != "N/A" and fb != "N/A":
        return ig, fb

    # Pass 3 — full page anchor sweep
    p_ig, p_fb = _links_from_element(soup)
    if ig == "N/A": ig = p_ig
    if fb == "N/A": fb = p_fb

    if ig != "N/A" and fb != "N/A":
        return ig, fb

    # Pass 4 - meta / link tags
    for tag in soup.find_all(["meta", "link"]):
        c_val = tag.get("content") or tag.get("href") or ""
        content = c_val[0] if isinstance(c_val, list) else str(c_val)
        if not content:
            continue
        if ig == "N/A" and "instagram.com" in content:
            ig = clean_ig_url(content)
        if fb == "N/A" and ("facebook.com" in content or "fb.com" in content):
            fb = clean_fb_url(content)

    if ig != "N/A" and fb != "N/A":
        return ig, fb

    # Pass 5 — raw regex scan (last resort for obfuscated / inline JS links)
    for m in _IG_PROFILE_RE.finditer(html):
        candidate = clean_ig_url(f"https://www.instagram.com/{m.group(1)}")
        if candidate != "N/A":
            ig = candidate
            break
    for m in _FB_URL_RE.finditer(html):
        candidate = clean_fb_url(m.group(0))
        if candidate != "N/A":
            fb = candidate
            break

    return ig, fb


# ---------------------------------------------------------------------------
# TIER 1 — requests + BeautifulSoup
# ---------------------------------------------------------------------------

class _RequestsTier:
    def __init__(self, timeout: int = 15, retries: int = 2):
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection":      "keep-alive",
        })

    def get(self, url: str) -> Optional[str]:
        if not url or url.lower() in {"n/a", "nan", "none", ""}:
            return None
        if not url.startswith("http"):
            url = "https://" + url

        for attempt in range(self.retries):
            try:
                self.session.headers["User-Agent"] = random.choice(_USER_AGENTS)
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True, verify=True)
                if 200 <= resp.status_code < 300:
                    return resp.text
                if resp.status_code in {403, 404}:
                    return None
            except requests.exceptions.SSLError:
                try:
                    resp = self.session.get(url, timeout=self.timeout, verify=False)
                    if resp.ok:
                        return resp.text
                except Exception:
                    pass
            except requests.exceptions.ConnectionError:
                if attempt == 0 and "//www." not in url:
                    parsed = urlparse(url)
                    www_url = f"{parsed.scheme}://www.{parsed.netloc}{parsed.path}"
                    try:
                        resp = self.session.get(www_url, timeout=self.timeout)
                        if resp.ok:
                            return resp.text
                    except Exception:
                        pass
            except Exception:
                break
            time.sleep(0.5)
        return None

    def extract(self, url: str) -> tuple:
        """Try homepage then fallback sub-pages. Returns (ig, fb)."""
        html = self.get(url)
        if not html:
            return "N/A", "N/A"

        ig, fb = extract_from_html(html)
        if ig != "N/A" and fb != "N/A":
            return ig, fb

        # Try sub-pages
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        for path in _FALLBACK_PATHS:
            if ig != "N/A" and fb != "N/A":
                break
            sub_html = self.get(base + path)
            if not sub_html:
                continue
            s_ig, s_fb = extract_from_html(sub_html)
            if ig == "N/A": ig = s_ig
            if fb == "N/A": fb = s_fb

        return ig, fb


# ---------------------------------------------------------------------------
# TIER 2 — Playwright headless browser with full scroll
# ---------------------------------------------------------------------------

class _PlaywrightTier:
    """
    Uses a real Chromium browser to:
      1. Load the page and wait for network to be idle
      2. Scroll to the very bottom in steps (triggers lazy-load & animations)
      3. Wait for any newly injected DOM to settle
      4. Extract the full rendered HTML and parse it

    Playwright is async internally but we run it synchronously here via
    the sync_api wrapper so the rest of the codebase stays simple.
    """

    def __init__(self):
        self._browser = None
        self._pw      = None

    def _start(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw      = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

    def stop(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw      = None

    def _get_full_html(self, url: str) -> Optional[str]:
        self._start()
        if not self._browser:
            return None
            
        page = None
        try:
            context = self._browser.new_context(
                user_agent=random.choice(_USER_AGENTS),
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()

            # Block images, fonts, media — we only need the DOM
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_(),
            )

            # Navigate — use domcontentloaded (fires early) then wait a
            # fixed time for JS to hydrate. networkidle hangs forever on
            # sites with analytics beacons / long-polling (merivale, etc.)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            except Exception:
                # If even domcontentloaded times out, grab whatever loaded
                pass

            # Let JS frameworks (React/Next/Webflow) finish rendering
            page.wait_for_timeout(2_500)

            # ── FULL PAGE SCROLL ──────────────────────────────────────
            # Scroll in steps so lazy-loaded sections trigger progressively
            total_height = page.evaluate("document.body.scrollHeight")
            step         = 600          # pixels per scroll step
            current      = 0

            while current < total_height:
                page.evaluate(f"window.scrollTo(0, {current})")
                page.wait_for_timeout(120)   # 120ms between steps
                current += step
                # Re-check height in case new content was injected
                total_height = page.evaluate("document.body.scrollHeight")

            # Final jump to absolute bottom
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(800)   # let any final animations settle

            html = page.content()        # full rendered DOM
            page.close()
            context.close()
            return html

        except Exception as e:
            print(f"    ⚠ Playwright error for {url}: {e}")
            if page:
                try: page.close()
                except Exception: pass
            return None

    def extract(self, url: str) -> tuple:
        """Fetch with full scroll, parse, return (ig, fb)."""
        html = self._get_full_html(url)
        if not html:
            return "N/A", "N/A"
        return extract_from_html(html)


# ---------------------------------------------------------------------------
# Public facade — SocialExtractor
# ---------------------------------------------------------------------------

class SocialExtractor:
    """
    Two-tier social link extractor.

    Usage:
        extractor = SocialExtractor()
        result    = extractor.extract_from_url("https://example.com")
        # → {"instagram": "https://www.instagram.com/...", "facebook": "N/A"}
        extractor.close()   # always call when done
    """

    def __init__(self, timeout: int = 15):
        self._tier1 = _RequestsTier(timeout=timeout)
        self._tier2 = _PlaywrightTier()

    def close(self):
        """Shut down the Playwright browser (call when all extractions are done)."""
        self._tier2.stop()

    def extract_from_url(self, url: str) -> dict:
        url = url.strip() if url else ""
        if not url or url.lower() in {"n/a", "nan", "none", ""}:
            return {"instagram": "N/A", "facebook": "N/A"}

        if not url.startswith("http"):
            url = "https://" + url

        # --- FAST TRACK: If the URL itself is already a social link ---
        if "instagram.com" in url:
            ig_clean = clean_ig_url(url)
            if ig_clean != "N/A":
                print(f"    ✓ URL is Instagram → IG={ig_clean}")
                return {"instagram": ig_clean, "facebook": "N/A"}
                
        if "facebook.com" in url or "fb.com" in url:
            fb_clean = clean_fb_url(url)
            if fb_clean != "N/A":
                print(f"    ✓ URL is Facebook → FB={fb_clean}")
                return {"instagram": "N/A", "facebook": fb_clean}

        # ── Tier 1: fast HTTP request ────────────────────────────────
        ig, fb = self._tier1.extract(url)

        if ig != "N/A" and fb != "N/A":
            print(f"    [HIT] Tier-1 hit  -> IG={ig} FB={fb}")
            return {"instagram": ig, "facebook": fb}

        # ── Tier 2: Playwright with full scroll ──────────────────────
        print(f"    [BUSY] Tier-1 partial ({ig=}, {fb=}) -> launching browser...")
        p_ig, p_fb = self._tier2.extract(url)

        if ig == "N/A": ig = p_ig
        if fb == "N/A": fb = p_fb

        tier = "Tier-2 hit" if (ig != "N/A" or fb != "N/A") else "miss"
        print(f"    {'[HIT]' if tier == 'Tier-2 hit' else '[MISS]'} {tier} -> IG={ig} FB={fb}")
        return {"instagram": ig, "facebook": fb}

    def process_leads(self, csv_file: str) -> list:
        """Batch-enrich a CSV that has a 'website' column."""
        df = pd.read_csv(csv_file)
        results = []
        try:
            for _, row in df.iterrows():
                website     = str(row.get("website", "N/A")).strip()
                ig_existing = str(row.get("instagram", "N/A")).strip()
                fb_existing = str(row.get("facebook",  "N/A")).strip()

                socials = self.extract_from_url(website)

                results.append({
                    "name":      str(row.get("name",    "N/A")),
                    "address":   str(row.get("address", "N/A")),
                    "phone":     str(row.get("phone",   "N/A")),
                    "website":   website,
                    "instagram": ig_existing if ig_existing not in {"N/A", "nan"} else socials["instagram"],
                    "facebook":  fb_existing if fb_existing not in {"N/A", "nan"} else socials["facebook"],
                })
        finally:
            self.close()
        return results
