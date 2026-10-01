import time
import random
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import json

class MapsScraper:

    def __init__(self, headless=True):
        self.headless = headless

    # -------------------- BROWSER --------------------

    def _start_browser(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        self.context = self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
        )

        self.page = self.context.new_page()

        # Block heavy resources
        self.page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "media", "font"}
            else route.continue_(),
        )

    def _stop_browser(self):
        try:
            self.browser.close()
            self.pw.stop()
        except:
            pass

    # -------------------- SEARCH --------------------

    def _search(self, query):
        url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}/"
        print(f"  Navigating: {query}")

        self.page.goto("about:blank")
        self.page.wait_for_timeout(1000)

        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except PWTimeout:
            pass

        self.page.wait_for_timeout(3000)

        # Accept cookies
        for btn_text in ["Accept all", "Reject all", "Accept"]:
            try:
                btn = self.page.locator(f"button:has-text('{btn_text}')").first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    self.page.wait_for_timeout(1000)
                    break
            except:
                pass

    # -------------------- SCROLL --------------------

    def _scroll(self, max_scroll=100):
        print("  Scrolling results...")

        selectors = [
            "div[role='feed']",
            "div[aria-label*='Results']",
            "div.m6QErb[role='feed']"
        ]

        feed = None

        for sel in selectors:
            try:
                feed = self.page.locator(sel).first
                feed.wait_for(timeout=5000)
                break
            except:
                continue

        if not feed:
            print("  ❌ Feed not found — retrying...")
            self.page.reload()
            self.page.wait_for_timeout(3000)

            try:
                feed = self.page.locator("div[role='feed']").first
                feed.wait_for(timeout=8000)
            except:
                print("  ❌ Skipping query")
                return

        try:
            self.page.wait_for_selector("div.Nv2PK", timeout=8000)
        except:
            print("  ❌ No cards loaded")
            return

        prev_count = 0
        stale = 0

        for _ in range(max_scroll):
            cards = self.page.locator("div.Nv2PK")
            count = cards.count()

            if count == prev_count:
                stale += 1
                if stale >= 5:
                    break
            else:
                stale = 0

            prev_count = count

            feed.evaluate("el => el.scrollTop = el.scrollHeight")
            self.page.wait_for_timeout(random.randint(1000, 2000))

        print(f"  Loaded {self.page.locator('div.Nv2PK').count()} cards")

    # -------------------- EXTRACT --------------------

    def _extract_cards(self, max_results=100):
        leads = []
        seen_names = set()
        seen_data  = set()

        # Get all business names from the sidebar first to avoid virtualization shifts
        card_locator = self.page.locator("div.Nv2PK")
        total_cards = 0
        try:
            total_cards = card_locator.count()
        except:
            pass

        raw_names = []
        for idx in range(total_cards):
            try:
                name_text = card_locator.nth(idx).locator("div.qBF1Pd").inner_text(timeout=1000)
                if name_text and name_text.strip():
                    raw_names.append(name_text.strip())
            except:
                continue

        print(f"  Found {len(raw_names)} businesses in sidebar. Extracting details...")

        for i, name in enumerate(raw_names):
            try:
                # Stop once we have enough leads
                if len(leads) >= max_results:
                    print(f"  [OK] Reached target of {max_results} leads. Stopping.")
                    break

                key = name.strip().lower()
                if key in seen_names:
                    continue
                seen_names.add(key)

                # Snapshot the current detail panel's website BEFORE clicking
                # so we can verify the panel actually refreshed after click
                old_website = ""
                try:
                    old_website = self.page.locator(
                        "a[data-item-id='authority']"
                    ).first.get_attribute("href", timeout=1000) or ""
                except:
                    pass

                # Locate card by index first (since it's fast)
                card = self.page.locator("div.Nv2PK").nth(i)
                try:
                    card.scroll_into_view_if_needed(timeout=2000)
                except:
                    pass
                self.page.wait_for_timeout(600)  # Let scroll animation completely finish

                # Verify if this card actually matches the target name
                try:
                    current_name = card.locator("div.qBF1Pd").inner_text(timeout=1500).strip()
                except:
                    current_name = ""

                # If name shifted due to virtualization, find the card by name
                if current_name != name:
                    card = self.page.locator("div.Nv2PK").filter(has=self.page.locator("div.qBF1Pd", has_text=name)).first
                    try:
                        card.scroll_into_view_if_needed(timeout=2000)
                    except:
                        pass
                    self.page.wait_for_timeout(600)

                # Save current URL before clicking
                url_before = self.page.url

                card.click(timeout=5000)

                # Wait for the URL to change (Google Maps updates URL on card click)
                url_changed = False
                for _ in range(10):
                    self.page.wait_for_timeout(300)
                    if self.page.url != url_before:
                        url_changed = True
                        break

                if not url_changed:
                    # Retry with force click
                    try:
                        card.click(timeout=3000, force=True)
                        for _ in range(8):
                            self.page.wait_for_timeout(300)
                            if self.page.url != url_before:
                                url_changed = True
                                break
                    except:
                        pass

                # After URL changes, wait for the detail panel DATA to actually refresh
                # by checking if the website link has changed from the old one
                if url_changed:
                    data_refreshed = False
                    for _ in range(10):  # up to 3 seconds
                        self.page.wait_for_timeout(300)
                        try:
                            cur_website = self.page.locator(
                                "a[data-item-id='authority']"
                            ).first.get_attribute("href", timeout=500) or ""
                        except:
                            cur_website = ""
                        # Panel refreshed if website changed or disappeared
                        if cur_website != old_website:
                            data_refreshed = True
                            break
                    if not data_refreshed:
                        # Could be same website or no website — give extra settle time
                        self.page.wait_for_timeout(2000)
                else:
                    self.page.wait_for_timeout(3000)

                # ── Extract data from the detail panel ──

                # WEBSITE
                website = "N/A"
                try:
                    website = self.page.locator(
                        "a[data-item-id='authority']"
                    ).first.get_attribute("href", timeout=3000)
                    if website:
                        website = website.rstrip("/")
                except:
                    pass

                # ADDRESS
                address = "N/A"
                for sel in [
                    "button[data-item-id='address'] div",
                    "button[data-item-id*='address'] div",
                    "[data-item-id='address']",
                    "div.Io6YTe.fontBodyMedium",
                ]:
                    try:
                        el = self.page.locator(sel).first
                        text = el.inner_text(timeout=2000).strip()
                        if text and len(text) > 10:
                            address = text
                            break
                    except:
                        continue

                # PHONE
                phone = "N/A"
                for sel in [
                    "button[data-item-id^='phone'] div",
                    "button[data-item-id='phone:tel'] div",
                    "[data-item-id^='phone']",
                ]:
                    try:
                        el = self.page.locator(sel).first
                        text = el.inner_text(timeout=2000).strip()
                        digits = sum(c.isdigit() for c in text)
                        if digits >= 8:
                            phone = text
                            break
                    except:
                        continue

                # ── Dedup safety net ──
                clean_phone = phone.replace("\n", "").strip()
                clean_addr  = address.replace("\n", "").strip()[:30]
                data_fp = (clean_phone, clean_addr)
                if clean_phone != "N/A" and data_fp in seen_data:
                    print(f"  [{i+1}] SKIPPED '{name}' - duplicate data detected")
                    try:
                        if self.page.url != url_before:
                            self.page.go_back(wait_until="domcontentloaded", timeout=8000)
                            self.page.wait_for_timeout(1000)
                    except:
                        pass
                    continue
                if clean_phone != "N/A":
                    seen_data.add(data_fp)

                leads.append({
                    "name": name.replace("\n", " ").strip(),
                    "address": address.replace("\n", " ").strip(),
                    "phone": phone.replace("\n", " ").strip(),
                    "website": website if website else "N/A",
                    "instagram": "N/A",
                    "facebook": "N/A",
                })

                print(f"  [{len(leads)}/{max_results}] {name} | phone={clean_phone} | web={'yes' if website!='N/A' else 'N/A'}")

                # GO BACK to the results list
                try:
                    if self.page.url != url_before:
                        self.page.go_back(wait_until="domcontentloaded", timeout=8000)
                        self.page.wait_for_timeout(random.randint(1000, 1500))

                        if "about:blank" in self.page.url:
                            self.page.go_forward()
                            self.page.wait_for_timeout(2000)
                except:
                    self.page.reload()
                    self.page.wait_for_timeout(3000)

            except Exception:
                continue

        return leads

    # -------------------- MAIN --------------------

    def run(self, location, business_types, max_results=100):
        self._start_browser()

        all_results = []
        global_seen = set()

        try:
            for biz in business_types:
                query = f"{biz} in {location}"
                print(f"\n[Searching] {query}")

                self._search(query)

                for attempt in range(2):
                    try:
                        self._scroll(max_scroll=100)
                        break
                    except:
                        print(f"  ⚠️ Retry scroll {attempt+1}")
                        self.page.reload()
                        self.page.wait_for_timeout(3000)

                leads = self._extract_cards(max_results=max_results)

                for lead in leads:
                    key = lead["name"].strip().lower()
                    if key not in global_seen:
                        global_seen.add(key)
                        all_results.append(lead)

                print(f"  → {len(leads)} leads collected for '{biz}'")

        finally:
            self._stop_browser()

        print(f"\n🔥 TOTAL UNIQUE LEADS: {len(all_results)}")

        return all_results


# -------------------- RUN --------------------


if __name__ == "__main__":

    # Load config
    with open("config.json", "r") as f:
        config = json.load(f)

    params = config["search_params"]

    scraper = MapsScraper(
        headless=params.get("headless", True)
    )

    leads = scraper.run(
        location=params["location"],
        business_types=params["business_types"],
        max_results=params.get("max_results", 100)
    )

    df = pd.DataFrame(leads)
    df.to_csv("leads_raw.csv", index=False)

    print(f"\n✅ Saved {len(df)} leads → leads_raw.csv")
