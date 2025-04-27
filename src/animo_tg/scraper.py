import argparse
import csv
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from DrissionPage import Chromium, ChromiumOptions
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

ENROLLMENT_URL = "https://enroll.dlsu.edu.ph/dlsu/view_course_offerings"
DAY_PATTERN = re.compile(r"^[MTWFSH]$")
IDLE_TIMEOUT = 500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("course_scraper")


class CloudflareBlockedError(RuntimeError):
    """Raised when Cloudflare's challenge page is returned instead of data."""


@dataclass
class Meeting:
    day: str
    time: str
    room: Optional[str]


def extract_table_cells(row) -> List[str]:
    return [td.text for td in row.eles("tag:td")]


# --- Browser management ---
_browser: Optional[Chromium] = None
_idle_timer: Optional[threading.Timer] = None
_browser_lock = threading.Lock()


def close_browser():
    global _browser, _idle_timer
    with _browser_lock:
        if _browser:
            _browser.quit()
            logger.debug("Idle timeout reached: closed browser")
            _browser = None
        if _idle_timer:
            _idle_timer.cancel()
            _idle_timer = None


def get_browser() -> Chromium:
    global _browser, _idle_timer
    with _browser_lock:
        if _browser is None:
            browser_options = (
                ChromiumOptions(read_file=False)
                .set_load_mode("eager")
                .set_local_port(9111)
                #.set_user_data_path("parser-data")
                #.add_extension("./proxy_ext")
                #.set_flag("allow-legacy-mv2-extensions")
            )
            _browser = Chromium(browser_options)
            logger.debug("Started new browser instance")
        if _idle_timer:
            _idle_timer.cancel()
        _idle_timer = threading.Timer(IDLE_TIMEOUT, close_browser)
        _idle_timer.daemon = True
        _idle_timer.start()
        logger.debug("Idle timer started.")
        return _browser


def write_csv(course_code: str, courses: List[Dict[str, Any]]) -> None:
    """
    Append each section's current enrolled count to <course_code>.csv.

    Columns: timestamp, classNbr, section, enrolled
    """
    filename = f"{course_code}.csv"
    is_new = not os.path.exists(filename)

    with open(filename, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "classNbr", "section", "enrolled"])
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for sec in courses:
            writer.writerow([ts, sec["classNbr"], sec["section"], sec["enrolled"]])


def scrape(course_code: str, id_no: str) -> List[Dict[str, Any]]:
    logger.info("Scraping %s for ID %s", course_code, id_no)
    browser = get_browser()
    tab = browser.latest_tab
    try:
        # Navigate
        url = (
            f"{ENROLLMENT_URL}"
            f"?p_id_no={id_no}&p_routine=1&p_last_name=&p_button=Search"
            f"&p_course_code={course_code}"
        )
        tab.get(url)

        html_lc = tab.html.lower()
        if any(p in html_lc for p in ("cf-browser-verification", "just a moment", "checking your browser")):
            raise CloudflareBlockedError("Cloudflare verification page detected")

        # Wait for course table
        xpath = 'xpath://table[.//td[contains(normalize-space(.),"Class Nbr")]]'
        if not tab.wait.ele_displayed(xpath, timeout=10):
            raise CloudflareBlockedError("Timed out waiting for course table")

        table = tab.ele(xpath)
        if not table:
            raise CloudflareBlockedError("Course table not present")

        # Parse table
        courses: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None

        for row in table.eles("tag:tr"):
            cells = extract_table_cells(row)
            if not cells:
                continue

            if cells[0].isdigit():
                current = {
                    "classNbr": int(cells[0]),
                    "course": cells[1],
                    "section": cells[2],
                    "enrlCap": int(cells[6]),
                    "enrolled": int(cells[7]),
                    "remarks": cells[8],
                    "meetings": [{"day": cells[3], "time": cells[4], "room": cells[5] or None}],
                }
                courses.append(current)
                continue

            if current and len(cells) == 1 and "," in cells[0]:
                current["instructor"] = cells[0]
                continue

            if current and len(cells) >= 6 and DAY_PATTERN.match(cells[3]):
                current["meetings"].append(
                    {"day": cells[3], "time": cells[4], "room": cells[5] or None}
                )

        logger.info("Found %d sections", len(courses))
        return courses

    except CloudflareBlockedError as cf_err:
        logger.warning("CloudflareBlockedError: %s ΓÇö opening my.dlsu.edu.ph as a new tab", cf_err)
        try:
            browser.new_tab("https://my.dlsu.edu.ph")
        except Exception as tab_err:
            logger.error("Failed to open fallback tab: %s", tab_err)
        raise


# --- FastAPI setup ---
app = FastAPI(
    title="DLSU Course Scraper API",
    version="0.3.0",
    description="Scrapes DLSU enrollment listings and logs CSV per subject.",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"]
)


@app.get("/scrape")
async def scrape_endpoint(course: str, id_no: str):
    try:
        courses = scrape(course, id_no)
        # write out CSV log
        write_csv(course, courses)
        return courses
    except CloudflareBlockedError:
        raise HTTPException(status_code=503, detail="cloudflare_blocked")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape DLSU course offerings as JSON/CSV")
    parser.add_argument("-c", "--course", default="CSOPESY", help="Course code")
    parser.add_argument("-i", "--id",     default="12209082", help="8-digit ID")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    try:
        courses = scrape(args.course, args.id)
        write_csv(args.course, courses)
        print(json.dumps(courses, indent=2, ensure_ascii=False))
    except Exception as e:
        logger.error("Scraping failed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
