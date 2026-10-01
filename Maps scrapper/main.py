"""
main.py
-------
Lead Generation Pipeline — 100% free, no API keys required.

Phase 1 | Playwright       -> scrapes Google Maps for businesses
Phase 2 | HTTP + Playwright -> extracts Instagram/Facebook from websites
Phase 3 | Playwright       -> checks Instagram follower count + post activity
Phase 4 | Google Sheets    -> writes filtered leads to a spreadsheet

Setup:
  1. pip install -r requirements.txt
  2. playwright install chromium
  3. credentials.json -> your Google service account (Sheets only)
  4. config.json -> set location, business_types, thresholds
"""

import json
import pandas as pd

from maps_scraper     import MapsScraper
from social_extractor import SocialExtractor
from activity_checker import ActivityChecker
from sheets_handler   import SheetsHandler


def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    return df.fillna("N/A").replace({"": "N/A", "nan": "N/A", "None": "N/A"})


def _dedup(leads: list, key: str = "name") -> list:
    seen, unique = set(), []
    for lead in leads:
        k = str(lead.get(key, "")).strip().lower()
        if k and k not in seen:
            seen.add(k)
            unique.append(lead)
    return unique


def main():
    with open("config.json", "r") as f:
        config = json.load(f)

    print("=" * 60)
    print("  Lead Generation Pipeline - Starting")
    print("=" * 60)

    # ── Phase 1: Google Maps Scraping ───────────────────────────────────
    print("\n[Phase 1] Google Maps scraping (Playwright)...")
    scraper   = MapsScraper(headless=config["search_params"].get("headless", True))
    raw_leads = scraper.run(
        config["search_params"]["location"],
        config["search_params"]["business_types"],
        config["search_params"].get("max_results", 20),
    )

    if not raw_leads:
        print("  No leads found. Check your search params in config.json.")
        return

    raw_leads = _dedup(raw_leads)
    df_raw    = _clean_df(pd.DataFrame(raw_leads))
    df_raw.to_csv("leads_raw.csv", index=False)
    print(f"\n  {len(raw_leads)} unique leads -> leads_raw.csv")

    # ── Phase 2: Social Link Extraction ─────────────────────────────────
    print("\n[Phase 2] Extracting social links from business websites...")
    extractor          = SocialExtractor(timeout=12)
    leads_with_socials = []

    for lead in raw_leads:
        website     = lead.get("website", "N/A")
        ig_existing = lead.get("instagram", "N/A")
        fb_existing = lead.get("facebook",  "N/A")

        need_ig = ig_existing in {"N/A", "nan", ""}
        need_fb = fb_existing in {"N/A", "nan", ""}

        if website not in {"N/A", "nan", ""} and (need_ig or need_fb):
            print(f"  -> {lead['name']}: {website[:55]}")
            socials = extractor.extract_from_url(website)
            if need_ig:
                lead["instagram"] = socials["instagram"]
            if need_fb:
                lead["facebook"] = socials["facebook"]
        else:
            if not (need_ig or need_fb):
                print(f"  -> {lead['name']}: socials already found")
            else:
                print(f"  -> {lead['name']}: no website, skipping")

        leads_with_socials.append(lead)

    extractor.close()
    df_socials = _clean_df(pd.DataFrame(leads_with_socials))
    df_socials.to_csv("leads_with_socials.csv", index=False)

    ig_found = sum(1 for l in leads_with_socials if l.get("instagram", "N/A") not in {"N/A", "nan"})
    fb_found = sum(1 for l in leads_with_socials if l.get("facebook",  "N/A") not in {"N/A", "nan"})
    print(f"\n  {len(leads_with_socials)} leads enriched -> leads_with_socials.csv")
    print(f"    Instagram found: {ig_found}/{len(leads_with_socials)}")
    print(f"    Facebook  found: {fb_found}/{len(leads_with_socials)}")

    # ── Phase 3: Activity Filtering ──────────────────────────────────────
    print("\n[Phase 3] Filtering by Instagram activity...")
    checker        = ActivityChecker()
    filtered_leads = checker.process_social_leads("leads_with_socials.csv")

    if not filtered_leads:
        print("  No leads passed the activity filter.")
        return

    df_filtered = _clean_df(pd.DataFrame(filtered_leads))
    df_filtered.to_csv("leads_filtered.csv", index=False)

    ig_survived = sum(1 for l in filtered_leads if l.get("instagram", "N/A") not in {"N/A", "nan"})
    print(f"\n  {len(filtered_leads)} leads passed -> leads_filtered.csv")
    print(f"    Instagram URLs retained: {ig_survived}/{len(filtered_leads)}")

    # ── Phase 4: Google Sheets ───────────────────────────────────────────
    print("\n[Phase 4] Uploading to Google Sheets...")
    handler = SheetsHandler()
    handler.update_sheet(filtered_leads)

    print("\n" + "=" * 60)
    print(f"  Pipeline complete - {len(filtered_leads)} leads ready.")
    print("=" * 60)


if __name__ == "__main__":
    main()