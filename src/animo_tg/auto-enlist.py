import argparse
import asyncio
import json
import logging
import os
import re
import sys
from time import sleep
from typing import Iterable, Set

import websockets
from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import ElementNotFoundError, WaitTimeoutError
from dotenv import load_dotenv
from tabulate import tabulate
from DrissionPage.common import Keys

LOGIN_URL = "https://animo.sys.dlsu.edu.ph/psp/ps/?cmd=login&languageCd=ENG"
CART_URL = (
    "https://animo.sys.dlsu.edu.ph/psp/ps/EMPLOYEE/HRMS/c/"
    "SA_LEARNER_SERVICES.SSR_SSENRL_CART.GBL?"
    "FolderPath=PORTAL_ROOT_OBJECT.CO_EMPLOYEE_SELF_SERVICE."
    "HCCC_ENROLLMENT.HC_SSR_SSENRL_CART_GBL&IsFolder=false"
)

load_dotenv()
WS_URI = os.getenv("WS_URI")
USERNAME = os.getenv("ANIMO_USER")
PASSWORD = os.getenv("ANIMO_PASS")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(levelname)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("auto_enlist")


def wait_and_input(page, css: str, text: str) -> None:
    """Wait for an element to be available and input text."""
    try:
        element = page.ele(css)
        element.clear(by_js=True)
        element.input(text)
    except (ElementNotFoundError, WaitTimeoutError) as exc:
        logger.error(f"Input error {css}: {exc}")
        raise


def wait_and_click(page, css: str) -> None:
    """Wait for an element to be available and click it."""
    try:
        page.ele(css).click()
    except (ElementNotFoundError, WaitTimeoutError) as exc:
        logger.error(f"Click error {css}: {exc}")
        raise


class Enlister:
    """Class that handles the enrollment process."""

    def __init__(self, username: str, password: str):
        """Initialize the Enlister with login credentials."""
        load_dotenv()
        co = (
            ChromiumOptions(read_file=False)
            .set_local_port(9112)
            .set_user_data_path("enlister-data")
            .set_pref('autofill.profile_enabled', False)
        )
        self.browser = Chromium(co)
        self.page = self.browser.latest_tab
        self._in_cart: Set[int] = set()
        
        self._login(username, password)
        
    def _login(self, username: str, password: str) -> None:
        """Log in to the system with provided credentials."""
        logger.info("Logging in...")
        self.page.get(LOGIN_URL)
        wait_and_input(self.page, "#userid", username)
        wait_and_input(self.page, "#pwd", password)
        wait_and_click(self.page, "@value=Sign In")

    def _refresh_cart(self) -> None:
        """Refresh the cart page and update the list of classes in cart."""
        logger.info("Navigating to cart page...")
        self.page.get(CART_URL)
        
        iframe = self.page.get_frame("@id=ptifrmtgtframe")
        if iframe is None:
            logger.error("Cart iframe not found!")
            return
            
        try:
            logger.debug("Waiting for cart frame to finish loading...")
            iframe.wait.doc_loaded()
        except Exception as e:
            logger.warning(f"Frame load wait failed: {e}")
            
        html = iframe.html
        ids = {int(nbr) for nbr in re.findall(r"\((\d+)\)", html)}
        self._in_cart = ids
        logger.info(f"Detected in-cart classNbrs: {sorted(ids)}")

    def add_classes(self, ids: Iterable[int | str]) -> None:
        """Add classes to the cart if they aren't already there."""
        self._refresh_cart()
        
        to_add = [str(i) for i in ids if int(i) not in self._in_cart]
                 
        if not to_add:
            logger.info("Nothing new to add.")
            return
            
        for nbr in to_add:
            logger.info(f"Adding {nbr}...")
            self.page.get(CART_URL)
            wait_and_input(self.page, "#DERIVED_REGFRM1_CLASS_NBR", nbr)
            wait_and_click(self.page, "#DERIVED_REGFRM1_SSR_PB_ADDTOLIST2$70$")
            sleep(2)
            wait_and_click(self.page, "#DERIVED_CLS_DTL_NEXT_PB$76$")
            sleep(1)
            self._in_cart.add(int(nbr))
            logger.info(f"Enlisted {nbr}")


async def listen_and_enlist(uri: str, enlister: Enlister) -> None:
    """Listen to WebSocket for class availability and enlist in them."""
    logger.info(f"Listening on WS {uri}")
    retry_count = 0
    
    while True:
        try:
            retry_count += 1
            logger.info(f"Connection attempt #{retry_count} to {uri}")
            
            async with websockets.connect(uri) as ws:
                logger.info(f"Successfully connected to {uri}")
                retry_count = 0
                
                async for msg in ws:
                    data = json.loads(msg)
                    logger.info(f"WS received: {data}")
                    await asyncio.to_thread(enlister.add_classes, data.get("available", []))
                    
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WS connection closed: {e} — reconnecting...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.warning(f"WS error: {e} — reconnecting...")
            await asyncio.sleep(5)


def main() -> None:
    """Main function to run the auto-enlister."""
    parser = argparse.ArgumentParser(description="Auto-enlist in DLSU classes")
    parser.add_argument("--user", default=USERNAME, help="ANIMO SYS username")
    parser.add_argument("--pass", dest="pw", default=PASSWORD, help="ANIMO SYS password")
    parser.add_argument("--ws", default=WS_URI, help="WebSocket URI to connect to")
    
    args = parser.parse_args()
    
    enlister = Enlister(args.user, args.pw)
    
    try:
        asyncio.run(listen_and_enlist(args.ws, enlister))
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    main()
