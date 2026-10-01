"""
sheets_handler.py
-----------------
Writes filtered leads to a Google Sheet using a service account.
"""

import json
import os
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials


class SheetsHandler:

    SCOPE = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    HEADERS = [
        "Name", "Phone", "Address", "Website URL",
        "Instagram URL", "Facebook URL", "Post Count", "Followers",
    ]

    def __init__(self, config_path: str = "config.json", credentials_path: str = "credentials.json"):
        with open(config_path, "r") as f:
            self.sheet_cfg = json.load(f)["google_sheets"]

        self.sheet_id = self.sheet_cfg["sheet_id"]
        self.client   = None

        if os.path.exists(credentials_path):
            try:
                creds       = ServiceAccountCredentials.from_json_keyfile_name(credentials_path, self.SCOPE)
                self.client = gspread.authorize(creds)
                print("Google Sheets: authenticated ✓")
            except Exception as e:
                print(f"Google Sheets: auth failed — {e}")
        else:
            print(f"Google Sheets: {credentials_path} not found — upload skipped.")

    @staticmethod
    def _clean(val) -> str:
        if val is None:
            return "N/A"
        if isinstance(val, float) and pd.isna(val):
            return "N/A"
        s = str(val).strip()
        if s.lower() in {"nan", "none", ""}:
            return "N/A"
        # Strip newlines, carriage returns, and other control characters
        # that cause Google Sheets to split a single cell across multiple rows
        s = s.replace("\n", " ").replace("\r", " ")
        # Collapse multiple spaces into one
        while "  " in s:
            s = s.replace("  ", " ")
        return s.strip()

    def update_sheet(self, leads: list):
        if not self.client:
            print("No Sheets client - skipping upload.")
            return
        if not leads:
            print("No leads to upload.")
            return

        try:
            sheet = self.client.open_by_key(self.sheet_id).sheet1
            rows  = []
            for lead in leads:
                rows.append([
                    self._clean(lead.get("name")),
                    self._clean(lead.get("phone")),
                    self._clean(lead.get("address")),
                    self._clean(lead.get("website")),
                    self._clean(lead.get("instagram")),
                    self._clean(lead.get("facebook")),
                    str(lead.get("ig_post_count", 0)),
                    str(lead.get("ig_followers",  0)),
                ])

            print(f"\nPreview (first 5 of {len(rows)} rows):")
            for i, r in enumerate(rows[:5]):
                print(f"  {i+1}. {r[0]} | IG={r[4]} | Posts={r[6]} | Followers={r[7]}")

            # Clear the sheet first
            sheet.clear()

            # Write header + data as one block using RAW mode
            # RAW prevents Google from re-interpreting phone numbers (e.g. +43...)
            # or other values, which can cause column/row shifting
            all_values = [self.HEADERS] + rows
            sheet.update(
                range_name="A1",
                values=all_values,
                value_input_option="RAW",
            )
            print(f"[OK] Uploaded {len(rows)} rows to Google Sheets.")

        except Exception as e:
            print(f"[ERROR] Google Sheets update failed: {e}")