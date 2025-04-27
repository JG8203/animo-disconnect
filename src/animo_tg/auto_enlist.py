#!/usr/bin/env python3
"""
Auto-enlist + auto-clicker for Animo.sys.

1. Connects to a WS for available classNbrs → adds them to your cart.
2. Once done, brute-forces the two commit buttons
   (“Proceed to Step 2 of 3” → “Finish Enrolling”) inside the iframe
   until they vanish or a safety cap is reached.
"""

from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from time import sleep
from typing import Iterable, Set

import websockets
from dotenv import load_dotenv

from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import (
    ElementNotFoundError,
    WaitTimeoutError,
    ElementLostError,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
LOGIN_URL = "https://animo.sys.dlsu.edu.ph/psp/ps/"
CART_URL = (
    "https://animo.sys.dlsu.edu.ph/psp/ps/EMPLOYEE/HRMS/c/"
    "SA_LEARNER_SERVICES.SSR_SSENRL_CART.GBL?"
    "FolderPath=PORTAL_ROOT_OBJECT.CO_EMPLOYEE_SELF_SERVICE."
    "HCCC_ENROLLMENT.HC_SSR_SSENRL_CART_GBL&IsFolder=false"
)

# iframe and button text
IFRAME_QS    = '@id=ptifrmtgtframe'
PROCEED_TEXT = "Proceed to Step 2 of 3"
FINISH_TEXT  = "Finish Enrolling"

# spam-click settings
MAX_CLICKS        = 50
MIN_DELAY, MAX_DELAY = 0.10, 0.30

# load environment vars
load_dotenv()
WS_URI   = os.getenv("WS_URI")
USERNAME = os.getenv("ANIMO_USER")
PASSWORD = os.getenv("ANIMO_PASS")

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(levelname)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("auto_enlist")

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def wait_and_input(page, selector: str, text: str) -> None:
    """Wait for an input, clear it, then type text."""
    try:
        el = page.ele(selector, timeout=10)
        el.clear(by_js=True)
        el.input(text)
    except (ElementNotFoundError, WaitTimeoutError) as e:
        logger.error("Input error [%s]: %s", selector, e)
        raise

def wait_and_click(page_or_frame, selector: str) -> None:
    """Wait for a clickable element and click it."""
    try:
        page_or_frame.ele(selector, timeout=10).click()
    except (ElementNotFoundError, WaitTimeoutError) as e:
        logger.error("Click error [%s]: %s", selector, e)
        raise

def spam_click(page, text: str, label: str) -> bool:
    """
    Spam-click the <a> by its visible text inside the ptifrmtgtframe iframe,
    re-entering and re-locating each loop to handle reloads.
    """
    locator = f'@text()={text}'
    for i in range(1, MAX_CLICKS + 1):
        frame = page.get_frame(IFRAME_QS) or None
        if frame is None:
            logger.warning("iframe missing; retrying… (%d/%d)", i, MAX_CLICKS)
            time.sleep(0.3)
            continue
        try:
            btn = frame.ele(locator, timeout=4)
            btn.click(by_js=True)
            frame.wait.doc_loaded()
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        except ElementNotFoundError:
            logger.info("✅ %s gone after %d clicks", label, i - 1)
            return True
        except (ElementLostError, WaitTimeoutError):
            logger.debug("Stale/timeout on %s; re-locating… (%d)", label, i)
            time.sleep(0.2)
            continue

    logger.warning("⚠️  hit MAX_CLICKS for %s", label)
    return False

# ─────────────────────────────────────────────────────────────────────────────
# Enlister class
# ─────────────────────────────────────────────────────────────────────────────
class Enlister:
    """Handles login, cart-refresh, add-classes, then spam-click commit buttons."""

    def __init__(self, username: str, password: str):
        co = (
            ChromiumOptions(read_file=False)
            .set_local_port(9112)
            .set_user_data_path("enlister-data")
            .set_pref("autofill.profile_enabled", False)
        )
        self.browser = Chromium(co)
        self.page    = self.browser.latest_tab
        self._in_cart: Set[int] = set()
        self._login(username, password)

    def _login(self, user: str, pw: str) -> None:
        logger.info("Logging in…")
        self.page.get(LOGIN_URL)
        wait_and_input(self.page, "#userid", user)
        wait_and_input(self.page, "#pwd",    pw)
        wait_and_click(self.page, "@value=Sign In")
        self.page.wait.doc_loaded()

    def _refresh_cart(self) -> None:
        logger.info("Refreshing cart…")
        self.page.get(CART_URL)
        frame = self.page.get_frame(IFRAME_QS)
        if not frame:
            logger.error("Cart iframe not found!")
            return
        try:
            frame.wait.doc_loaded()
        except Exception as e:
            logger.warning("Frame load wait failed: %s", e)
        html = frame.html
        ids = {int(n) for n in re.findall(r"\((\d+)\)", html)}
        self._in_cart = ids
        logger.info("In-cart classNbrs: %s", sorted(ids))

    def add_classes(self, ids: Iterable[int | str]) -> None:
        self._refresh_cart()

        to_add = [str(i) for i in ids if int(i) not in self._in_cart]
        if to_add:
            for nbr in to_add:
                logger.info("Adding %s…", nbr)
                self.page.get(CART_URL)
                wait_and_input(self.page, "#DERIVED_REGFRM1_CLASS_NBR", nbr)
                wait_and_click(self.page, "#DERIVED_REGFRM1_SSR_PB_ADDTOLIST2$70$")
                sleep(2)
                wait_and_click(self.page, "#DERIVED_CLS_DTL_NEXT_PB$76$")
                sleep(1)
                self._in_cart.add(int(nbr))
                logger.info("Enlisted %s", nbr)
        else:
            logger.info("Nothing new to add.")

        logger.info("🚀 Spamming PROCEED buttons...")
        spam_click(self.page, PROCEED_TEXT, "Proceed")
        logger.info("🚀 Spamming FINISH buttons...")
        spam_click(self.page, FINISH_TEXT, "Finish Enrolling")

async def listen_and_enlist(uri: str, enlister: Enlister) -> None:
    logger.info("Listening on WS %s", uri)
    retry = 0
    while True:
        try:
            retry += 1
            logger.info("Connection attempt #%d to %s", retry, uri)
            async with websockets.connect(uri) as ws:
                retry = 0
                async for msg in ws:
                    data = json.loads(msg)
                    logger.info("WS received: %s", data)
                    await asyncio.to_thread(enlister.add_classes, data.get("available", []))
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning("WS closed: %s — reconnecting...", e)
            await asyncio.sleep(5)
        except Exception as e:
            logger.warning("WS error: %s — reconnecting...", e)
            await asyncio.sleep(5)

# ─────────────────────────────────────────────────────────────────────────────
# CLI entry-point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-enlist in DLSU classes")
    parser.add_argument("--user", default=USERNAME, help="ANIMO SYS username")
    parser.add_argument("--pass", dest="pw", default=PASSWORD, help="ANIMO SYS password")
    parser.add_argument("--ws",   default=WS_URI,   help="WebSocket URI")
    args = parser.parse_args()

    if not (args.user and args.pw and args.ws):
        logger.error("Missing --user/--pass/--ws or env vars.")
        sys.exit(1)

    enlister = Enlister(args.user, args.pw)
    try:
        sleep(1)
        asyncio.run(listen_and_enlist(args.ws, enlister))
    except KeyboardInterrupt:
        logger.info("Shutting down...")

if __name__ == "__main__":
    main()
