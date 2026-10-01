import sys
import io

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import json
import os
import queue
import threading
import time
from flask import Flask, render_template, request, Response, stream_with_context, send_file
from maps_scraper import MapsScraper
from social_extractor import SocialExtractor
from activity_checker import ActivityChecker
from sheets_handler import SheetsHandler
import pandas as pd

app = Flask(__name__)

# Global queue to stream log messages
log_queue = queue.Queue()
is_running = False
last_results = []


def log(msg):
    safe_msg = str(msg)
    try:
        print(safe_msg)
    except UnicodeEncodeError:
        # Windows console often crashes on emojis/arrows. This prints ? instead but keeps the web UI intact.
        print(safe_msg.encode('ascii', errors='replace').decode('ascii'))
    
    log_queue.put(safe_msg)


def run_scraper(business_types, location):
    global is_running, last_results
    is_running = True
    last_results = []

    try:
        # Load config so handlers work
        with open("config.json", "r") as f:
            config = json.load(f)

        max_results = config["search_params"].get("max_results", 10)

        # Update config dynamically based on user inputs
        config["search_params"]["city_or_zip"] = location
        config["search_params"]["business_types"] = business_types
        with open("config.json", "w") as f:
            json.dump(config, f, indent=2)

        # --- Phase 1: Google Maps Scraping ---
        log(f"[PHASE 1] Searching Google Maps for: {business_types} in {location}")
        scraper = MapsScraper(headless=config["search_params"].get("headless", False))
        raw_leads = scraper.run(location, business_types, max_results)

        df_raw = pd.DataFrame(raw_leads)
        df_raw.to_csv("leads_raw.csv", index=False)
        log(f"[PHASE 1] Done. {len(raw_leads)} leads found.")

        # Deduplicate by business name
        seen_names = set()
        unique_leads = []
        for lead in raw_leads:
            name_key = lead["name"].strip().lower()
            if name_key not in seen_names:
                seen_names.add(name_key)
                unique_leads.append(lead)

        if len(unique_leads) < len(raw_leads):
            log(f"[PHASE 1] Removed {len(raw_leads) - len(unique_leads)} duplicates. {len(unique_leads)} unique leads.")
        raw_leads = unique_leads

        if not raw_leads:
            log("[DONE] No leads found. Try a different search.")
            is_running = False
            return

        # --- Phase 2: Social Link Extraction from Websites ---
        log("[PHASE 2] Extracting social links from business websites...")
        extractor = SocialExtractor()
        leads_with_socials = []
        for lead in raw_leads:
            site = str(lead.get("website", "N/A")).strip()
            
            # Smart Check: If the website IS an Instagram/Facebook link, don't miss it!
            if "instagram.com" in site.lower():
                if lead["instagram"] == "N/A": lead["instagram"] = site
            elif "facebook.com" in site.lower() or "fb.com" in site.lower():
                if lead["facebook"] == "N/A": lead["facebook"] = site

            # Now run the deep extractor to find socials INSIDE the website
            socials = extractor.extract_from_url(site)
            
            if lead["instagram"] == "N/A":
                lead["instagram"] = socials["instagram"]
            if lead["facebook"] == "N/A":
                lead["facebook"] = socials["facebook"]
                
            leads_with_socials.append(lead)

        df_socials = pd.DataFrame(leads_with_socials)
        df_socials.to_csv("leads_with_socials.csv", index=False)
        log(f"[PHASE 2] Done. {len(leads_with_socials)} leads enriched.")

        # --- Phase 3: Activity Filtering ---
        log("[PHASE 3] Filtering inactive businesses via Instagram...")
        checker = ActivityChecker()
        filtered_leads = checker.process_social_leads("leads_with_socials.csv")

        if filtered_leads:
            df_filtered = pd.DataFrame(filtered_leads)
            df_filtered.to_csv("leads_filtered.csv", index=False)
            
            # Export to Excel as well
            df_filtered.to_excel("leads_filtered.xlsx", index=False)
            
            log(f"[PHASE 3] Done. {len(filtered_leads)} inactive businesses found.")
            last_results = filtered_leads
        else:
            log("[PHASE 3] No leads matched the filtering criteria.")

        # --- Phase 4: Google Sheets ---
        log("[PHASE 4] Uploading to Google Sheets...")
        try:
            handler = SheetsHandler()
            handler.update_sheet(filtered_leads if filtered_leads else [])
        except Exception as e:
            log(f"[PHASE 4] Google Sheets error: {e}")

        log(f"[RESULTS] {json.dumps(last_results)}")
        log("[DONE] Automation complete!")

    except Exception as e:
        log(f"[ERROR] {str(e)}")
    finally:
        is_running = False


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    global is_running
    if is_running:
        return {"status": "error", "message": "Scraper is already running."}, 400

    data = request.json
    business_types = [b.strip() for b in data.get("business_type", "").split(",") if b.strip()]
    location = data.get("location", "").strip()

    if not business_types or not location:
        return {"status": "error", "message": "Business type and location are required."}, 400

    # Clear queue
    while not log_queue.empty():
        log_queue.get()

    thread = threading.Thread(target=run_scraper, args=(business_types, location), daemon=True)
    thread.start()

    return {"status": "ok"}


@app.route("/stream")
def stream():
    def event_stream():
        while True:
            try:
                msg = log_queue.get(timeout=30)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield "data: [PING]\n\n"
    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")


@app.route("/status")
def status():
    return {"running": is_running}


@app.route("/leads")
def leads():
    """Read current leads directly from Google Sheet and return as JSON."""
    try:
        handler = SheetsHandler()
        if not handler.client:
            return {"status": "error", "message": "Sheets not connected"}, 500

        sheet = handler.client.open_by_key(handler.sheet_id).sheet1
        rows  = sheet.get_all_records()  # list of dicts keyed by header row

        # Normalise keys to match what the frontend expects
        key_map = {
            "Name":          "name",
            "Phone":         "phone",
            "Address":       "address",
            "Website URL":   "website",
            "Instagram URL": "instagram",
            "Facebook URL":  "facebook",
            "Post Count":    "ig_post_count",
            "Followers":     "ig_followers",
        }
        normalised = [
            {key_map.get(k, k): v for k, v in row.items()}
            for row in rows
        ]
        return {"status": "ok", "leads": normalised}
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500


@app.route("/download/<fmt>")
def download(fmt):
    if fmt == 'csv':
        if os.path.exists('leads_filtered.csv'):
            
            return send_file('leads_filtered.csv', as_attachment=True)
        return "File not found", 404
    elif fmt == 'xlsx':
        if os.path.exists('leads_filtered.xlsx'):
            return send_file('leads_filtered.xlsx', as_attachment=True)
        return "File not found", 404
    return "Invalid format", 400


if __name__ == "__main__":
    app.run(debug=False, port=5000, threaded=True)