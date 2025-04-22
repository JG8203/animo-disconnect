"""Scrapes DLSU course enrollment data and provides an API endpoint."""

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from DrissionPage import Chromium, ChromiumOptions
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

ENROLLMENT_URL = "https://enroll.dlsu.edu.ph/dlsu/view_course_offerings"
DAY_PATTERN = re.compile(r"^[MTWFSH]$")


class CloudflareBlockedError(RuntimeError):
    """Raised when Cloudflare's challenge page is returned instead of data."""


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("course_scraper")


@dataclass
class Meeting:
    """Represents a class meeting time and location."""
    day: str
    time: str
    room: Optional[str]


def extract_table_cells(row) -> List[str]:
    """Extract text from all table cells in a row."""
    return [td.text for td in row.eles("tag:td")]


def scrape(course_code: str, id_no: str) -> List[Dict[str, Any]]:
    """
    Scrape course information from the DLSU enrollment website.

    Args:
        course_code: The course code to search for (e.g., 'CSOPESY')
        id_no: The student ID number

    Returns:
        A list of course sections with their details

    Raises:
        CloudflareBlockedError: If Cloudflare anti-bot protection is triggered
    """
    logger.info("Scraping %s for ID %s", course_code, id_no)

    url = (
        f"{ENROLLMENT_URL}"
        f"?p_id_no={id_no}&p_routine=1&p_last_name=&p_button=Search"
        f"&p_course_code={course_code}"
    )
    logger.debug("URL => %s", url)

    browser_options = (
        ChromiumOptions(read_file=False)
        .set_load_mode("eager")
        .set_local_port(9111)
        .set_user_data_path('parser-data')
    )

    browser = Chromium(browser_options)
    tab = browser.latest_tab

    try:
        tab.get(url)

        html_lc = tab.html.lower()
        if any(phrase in html_lc for phrase in [
            "cf-browser-verification",
            "just a moment",
            "checking your browser"
        ]):
            raise CloudflareBlockedError("Cloudflare verification page detected")

        table_xpath = 'xpath://table[.//td[contains(normalize-space(.),"Class Nbr")]]'
        if not tab.wait.ele_displayed(table_xpath, timeout=10):
            raise CloudflareBlockedError("Timed out waiting for course table")

        table = tab.ele(table_xpath)
        if not table:
            raise CloudflareBlockedError("Course table not present")

        courses: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None

        for row in table.eles("tag:tr"):
            cells = extract_table_cells(row)
            if not cells:
                continue

            first_cell = cells[0]

            if first_cell.isdigit():
                current = {
                    "classNbr": int(first_cell),
                    "course": cells[1],
                    "section": cells[2],
                    "enrlCap": int(cells[6]),
                    "enrolled": int(cells[7]),
                    "remarks": cells[8],
                    "meetings": [
                        {"day": cells[3], "time": cells[4], "room": cells[5] or None}
                    ],
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

    finally:
        logger.debug("Session finished (%d open tab)", len(browser.tab_ids))


app = FastAPI(
    title="DLSU Course Scraper API",
    version="0.2.0",
    description="Scrapes DLSU enrollment listings on demand.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/scrape")
async def scrape_endpoint(course: str, id_no: str):
    """API endpoint to scrape course information."""
    try:
        return scrape(course, id_no)
    except CloudflareBlockedError as e:
        logger.warning("Cloudflare blocked: %s", e)
        raise HTTPException(status_code=503, detail="cloudflare_blocked") from e
    except Exception as e:
        logger.error("Error scraping %s: %s", course, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


def main() -> None:
    """Command-line interface for the scraper."""
    parser = argparse.ArgumentParser(description="Scrape DLSU course offerings → JSON")
    parser.add_argument("-c", "--course", default="CSOPESY", help="Course code")
    parser.add_argument("-i", "--id", default="12209082", help="8-digit ID")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    try:
        data = scrape(args.course, args.id)
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        logger.error("Scraping failed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
