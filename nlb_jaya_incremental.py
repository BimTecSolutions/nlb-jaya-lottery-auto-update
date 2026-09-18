"""
NLB Jaya incremental scraper.

Checks Firestore for the latest draw number already stored, scrapes any
newer draws from nlb.lk, and uploads only the new ones. If Firestore is
already up to date, it does nothing.

Designed to run unattended (e.g. via GitHub Actions on a cron schedule),
so it never uses input() and always exits cleanly.
"""

import os
import re
import sys
import json
import time
import base64
import requests
from bs4 import BeautifulSoup
from typing import Dict, Optional
from datetime import datetime

import firebase_admin
from firebase_admin import credentials, firestore


class NLBJayaFirebaseScraper:
    """Scrapes NLB Jaya results from nlb.lk and uploads new draws to Firestore."""

    BASE_URL = "https://www.nlb.lk"
    LOTTERY_SLUG = "nlb-jaya"
    LOTTERY_NAME = "NLB Jaya"

    def __init__(self, use_selenium: bool = False, debug: bool = False):
        self.use_selenium = use_selenium
        self.debug = debug
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._driver = None
        self.max_retries = 3
        self.db = None

    # ------------------------------------------------------------------
    # Firebase
    # ------------------------------------------------------------------
    def _load_service_account_info(self) -> dict:
        """
        Load the service account JSON from whichever source is available.
        Accepts either raw JSON or base64-encoded JSON in either env var,
        so a misconfigured secret (e.g. raw JSON pasted into the "base64"
        var) still works instead of failing with a cryptic decode error.
        """
        candidates = []
        for var in ("FIREBASE_SERVICE_ACCOUNT_JSON", "FIREBASE_SERVICE_ACCOUNT_BASE64"):
            val = os.environ.get(var)
            if val:
                candidates.append((var, val.strip()))

        if not candidates and os.path.exists("serviceAccount.json"):
            with open("serviceAccount.json", "r", encoding="utf-8") as f:
                return json.load(f)

        if not candidates:
            raise RuntimeError(
                "No Firebase credentials found. Set FIREBASE_SERVICE_ACCOUNT_JSON "
                "(raw JSON) or FIREBASE_SERVICE_ACCOUNT_BASE64 (base64-encoded JSON), "
                "or provide serviceAccount.json locally."
            )

        last_error = None
        for var_name, val in candidates:
            # Strip accidental wrapping quotes, e.g. if someone pasted '{"...": ...}'
            cleaned = val.strip().strip("'").strip('"')

            # Try as raw JSON first.
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass

            # Fall back to base64.
            try:
                decoded = base64.b64decode(cleaned, validate=True)
                return json.loads(decoded.decode("utf-8"))
            except Exception as e:
                last_error = e
                print(f"⚠️ Could not parse {var_name} as raw JSON or valid base64 JSON: {e}")

        raise RuntimeError(
            f"Found credential env var(s) but couldn't parse them as JSON or base64 JSON. "
            f"Last error: {last_error}. Double check the secret value was copied in full, "
            f"with no extra quotes added around it."
        )

    def init_firebase(self):
        """
        Initialize Firestore. Looks for credentials in this order:
          1. FIREBASE_SERVICE_ACCOUNT_JSON env var (raw JSON string) — simplest, used in CI
          2. FIREBASE_SERVICE_ACCOUNT_BASE64 env var (base64-encoded JSON) — also supported
          3. serviceAccount.json file in the working directory — used locally
        """
        try:
            if not firebase_admin._apps:
                info = self._load_service_account_info()
                cred = credentials.Certificate(info)
                firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            print("✅ Firebase initialized")
        except Exception as e:
            print(f"❌ Firebase initialization error: {e}")
            sys.exit(1)

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------
    def _get_driver(self):
        if self._driver is None and self.use_selenium:
            try:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                options = Options()
                options.add_argument('--headless=new')
                options.add_argument('--no-sandbox')
                options.add_argument('--disable-dev-shm-usage')
                options.add_argument('--disable-gpu')
                options.add_argument('--window-size=1280,1696')
                options.add_argument('--disable-blink-features=AutomationControlled')
                options.add_experimental_option("excludeSwitches", ["enable-automation"])
                options.add_experimental_option('useAutomationExtension', False)

                chrome_binary = os.environ.get("CHROME_PATH") or os.environ.get("CHROME_BIN")
                if chrome_binary:
                    options.binary_location = chrome_binary

                self._driver = webdriver.Chrome(options=options)
            except Exception as e:
                print(f"⚠️ Selenium init error: {e}")
                self.use_selenium = False
        return self._driver

    def fetch_page_with_retry(self, url: str, max_retries: int = 3) -> Optional[str]:
        for attempt in range(max_retries):
            try:
                if self.use_selenium:
                    driver = self._get_driver()
                    if driver:
                        try:
                            driver.get(url)
                            time.sleep(3)
                            from selenium.webdriver.common.by import By
                            from selenium.webdriver.support.ui import WebDriverWait
                            from selenium.webdriver.support import expected_conditions as EC
                            WebDriverWait(driver, 15).until(
                                EC.presence_of_element_located((By.TAG_NAME, "body"))
                            )
                            return driver.page_source
                        except Exception as e:
                            print(f"  Attempt {attempt + 1} Selenium error: {e}")
                            if attempt < max_retries - 1:
                                time.sleep(2 ** attempt)
                            continue

                resp = self.session.get(url, timeout=30)
                if resp.status_code == 200 and len(resp.text) > 1000:
                    return resp.text
                else:
                    print(f"  Attempt {attempt + 1} returned status {resp.status_code}, length {len(resp.text)}")

            except Exception as e:
                print(f"  Attempt {attempt + 1} error: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                continue

        return None

    # ------------------------------------------------------------------
    # Parsing (unchanged logic from the original scraper)
    # ------------------------------------------------------------------
    def extract_winning_numbers(self, text: str, draw_number: str) -> Dict:
        result = {
            "symbol": None,
            "numbers": [],
            "numbers_int": [],
            "sunday_bonus_numbers": [],
            "sunday_bonus_numbers_int": [],
            "has_sunday_bonus": False,
            "found": False
        }

        draw_patterns = [
            rf'(?:Draw|Lottery)\s*(?:No|Number)?\.?\s*:?\s*{re.escape(draw_number)}',
            rf'(?:Draw|Lottery)\s*(?:No|Number)?\.?\s*:?\s*{int(draw_number)}',
        ]

        draw_match = None
        for pattern in draw_patterns:
            draw_match = re.search(pattern, text, re.I)
            if draw_match:
                break

        if not draw_match:
            return result

        start = draw_match.end()
        section = text[start:start + 1000]

        main_pattern = r'\b([A-Z])\s*([0-9])\s*([0-9])\s*([0-9])\s*([0-9])\b'
        main_matches = re.findall(main_pattern, section, re.I)

        for match in main_matches:
            symbol = match[0].upper()
            numbers = [int(n) for n in match[1:5]]
            if len(numbers) == 4 and all(0 <= n <= 9 for n in numbers):
                result["symbol"] = symbol
                result["numbers"] = [f"{n:02d}" for n in numbers]
                result["numbers_int"] = numbers
                result["found"] = True
                break

        bonus_pattern = (
            r'Sunday\s*Bonus\s*[:\-]?\s*'
            r'([0-9])\s*[,\-]?\s*'
            r'([0-9])\s*[,\-]?\s*'
            r'([0-9])\s*[,\-]?\s*'
            r'([0-9])\b'
        )
        bonus_match = re.search(bonus_pattern, section, re.I)

        if not bonus_match:
            bonus_pattern_fallback = (
                r'Sunday\s*Bonus(?:\s+Number)?\D{0,20}'
                r'([0-9])\D{0,5}([0-9])\D{0,5}([0-9])\D{0,5}([0-9])\b'
            )
            bonus_match = re.search(bonus_pattern_fallback, section, re.I | re.S)

        if bonus_match:
            bonus_numbers = [int(n) for n in bonus_match.groups()[-4:]]
            if len(bonus_numbers) == 4 and all(0 <= n <= 9 for n in bonus_numbers):
                result["sunday_bonus_numbers"] = [f"{n:02d}" for n in bonus_numbers]
                result["sunday_bonus_numbers_int"] = bonus_numbers
                result["has_sunday_bonus"] = True

        return result

    def extract_draw_date(self, text: str) -> Optional[str]:
        date_patterns = [
            r'(?:Date|Draw Date)\s*:?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})',
            r'([A-Za-z]+\s+\d{1,2},?\s+\d{4})',
        ]
        for pattern in date_patterns:
            match = re.search(pattern, text, re.I)
            if match:
                return match.group(1)
        return None

    def parse_date(self, date_str: str) -> Optional[str]:
        if not date_str:
            return None
        try:
            clean_date = re.sub(r'^[A-Za-z]+\s+', '', date_str)
            dt = datetime.strptime(clean_date, "%B %d, %Y")
            return dt.isoformat()
        except Exception:
            try:
                dt = datetime.strptime(date_str, "%B %d, %Y")
                return dt.isoformat()
            except Exception:
                return date_str

    def format_draw_number(self, draw_number: int) -> str:
        return f"{draw_number:04d}"

    def get_result_with_retry(self, draw_number: int) -> Optional[Dict]:
        draw_number_str = self.format_draw_number(draw_number)
        draw_number_int = str(draw_number)

        urls_to_try = [
            f"{self.BASE_URL}/results/{self.LOTTERY_SLUG}/{draw_number_str}",
            f"{self.BASE_URL}/results/{self.LOTTERY_SLUG}/{draw_number_int}",
            f"{self.BASE_URL}/results/{self.LOTTERY_SLUG}/{draw_number_str}/",
            f"{self.BASE_URL}/lottery/{self.LOTTERY_SLUG}/results/{draw_number_str}",
        ]

        for url_attempt in urls_to_try:
            html = self.fetch_page_with_retry(url_attempt, max_retries=self.max_retries)
            if not html:
                continue

            soup = BeautifulSoup(html, 'html.parser')
            all_text = soup.get_text(separator=" ", strip=True)

            if draw_number_str not in all_text and draw_number_int not in all_text:
                continue

            date = self.extract_draw_date(all_text)
            extracted = self.extract_winning_numbers(all_text, draw_number_str)

            if extracted["found"]:
                return {
                    "lottery": self.LOTTERY_NAME,
                    "lottery_slug": self.LOTTERY_SLUG,
                    "draw_number": draw_number,
                    "draw_number_str": draw_number_str,
                    "date": date,
                    "date_iso": self.parse_date(date),
                    "symbol": extracted["symbol"],
                    "numbers": extracted["numbers"],
                    "numbers_int": extracted["numbers_int"],
                    "sunday_bonus_numbers": extracted["sunday_bonus_numbers"],
                    "sunday_bonus_numbers_int": extracted["sunday_bonus_numbers_int"],
                    "has_sunday_bonus": extracted["has_sunday_bonus"],
                    "url": url_attempt,
                    "scraped_at": datetime.now().isoformat(),
                    "source": "National Lotteries Board, Sri Lanka"
                }

        return None

    # ------------------------------------------------------------------
    # Firestore read/write
    # ------------------------------------------------------------------
    def get_latest_draw_number(self, main_collection: str, sub_collection: str) -> Optional[int]:
        """Return the highest draw_number currently stored in Firestore, or None if empty."""
        coll_ref = (
            self.db.collection(main_collection)
            .document(sub_collection)
            .collection(sub_collection)
        )
        query = coll_ref.order_by("draw_number", direction=firestore.Query.DESCENDING).limit(1)
        docs = list(query.stream())
        if not docs:
            return None
        return docs[0].to_dict().get("draw_number")

    def upload_to_firestore(self, data: Dict, main_collection: str, sub_collection: str) -> bool:
        for attempt in range(self.max_retries):
            try:
                doc_id = f"NLB-Jaya-{data['draw_number_str']}"
                doc_ref = (
                    self.db.collection(main_collection)
                    .document(sub_collection)
                    .collection(sub_collection)
                    .document(doc_id)
                )
                doc_ref.set(data)
                return True
            except Exception as e:
                print(f"  Upload attempt {attempt + 1} failed: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
        return False

    # ------------------------------------------------------------------
    # Incremental update
    # ------------------------------------------------------------------
    def run_incremental_update(
        self,
        main_collection: str = "NLB_Results_All",
        sub_collection: str = "NLB_Jaya",
        max_new_draws: int = 10,
        delay: float = 1.0,
    ):
        """
        Finds the latest draw stored in Firestore, then scrapes forward from
        there. Stops as soon as a draw isn't found on the site yet (meaning
        we've caught up), or after max_new_draws in one run as a safety cap.
        """
        latest = self.get_latest_draw_number(main_collection, sub_collection)

        if latest is None:
            print("⚠️ No existing draws found in Firestore. Set a starting draw number manually")
            print("   (this script is meant to top up an existing collection, not backfill it).")
            return

        print(f"📌 Latest draw in Firestore: {self.format_draw_number(latest)}")

        next_draw = latest + 1
        checked = 0
        uploaded = 0

        while checked < max_new_draws:
            draw_str = self.format_draw_number(next_draw)
            print(f"🔎 Checking draw {draw_str}...", end=" ")

            result = self.get_result_with_retry(next_draw)
            checked += 1

            if result:
                numbers = ', '.join(result.get('numbers', []))
                bonus = ', '.join(result.get('sunday_bonus_numbers', []))
                bonus_text = f" | Sunday Bonus: {bonus}" if bonus else ""

                if self.upload_to_firestore(result, main_collection, sub_collection):
                    uploaded += 1
                    print(f"✅ Uploaded: {result.get('symbol', '')} {numbers}{bonus_text}")
                else:
                    print(f"❌ Found but upload failed: {result.get('symbol', '')} {numbers}{bonus_text}")

                next_draw += 1
                time.sleep(delay)
            else:
                print("— not published yet, stopping here.")
                break

        if uploaded == 0 and checked <= 1:
            print("✅ Firestore already up to date. Nothing new to upload.")
        else:
            print(f"📊 Done. Checked {checked} draw(s), uploaded {uploaded} new draw(s).")

    def close(self):
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None


def main():
    use_selenium = os.environ.get("USE_SELENIUM", "true").lower() == "true"
    debug = os.environ.get("DEBUG", "false").lower() == "true"

    main_collection = os.environ.get("MAIN_COLLECTION", "NLB_Results_All")
    sub_collection = os.environ.get("SUB_COLLECTION", "NLB_Jaya")
    max_new_draws = int(os.environ.get("MAX_NEW_DRAWS", "10"))

    scraper = NLBJayaFirebaseScraper(use_selenium=use_selenium, debug=debug)
    scraper.init_firebase()

    try:
        scraper.run_incremental_update(
            main_collection=main_collection,
            sub_collection=sub_collection,
            max_new_draws=max_new_draws,
        )
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
