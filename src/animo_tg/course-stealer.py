from __future__ import annotations
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

LOGIN_URL = "https://animo.sys.dlsu.edu.ph/psp/ps/?cmd=login&languageCd=ENG"
CART_URL = (
    "https://animo.sys.dlsu.edu.ph/psp/ps/EMPLOYEE/HRMS/c/"
    "SA_LEARNER_SERVICES.SSR_SSENRL_CART.GBL?"
    "FolderPath=PORTAL_ROOT_OBJECT.CO_EMPLOYEE_SELF_SERVICE."
    "HCCC_ENROLLMENT.HC_SSR_SSENRL_CART_GBL&IsFolder=false"
)
WS_URI   = os.getenv("WS_URI")
USERNAME = os.getenv("ANIMO_USER")
PASSWORD = os.getenv("ANIMO_PASS")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(levelname)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("auto_enlist")

def wait_and_input(page, css: str, text: str) -> None:
    try:
        ele = page.ele(css)
        ele.input(text, clear=True)
    except (ElementNotFoundError, WaitTimeoutError) as exc:
        logger.error("Input error %s: %s", css, exc)
        raise

def wait_and_click(page, css: str) -> None:
    try:
        page.ele(css).click()
    except (ElementNotFoundError, WaitTimeoutError) as exc:
        logger.error("Click error %s: %s", css, exc)
        raise

class Enlister:
    def __init__(self, username: str, password: str):
        load_dotenv()
        co = (
            ChromiumOptions(read_file=False)
            .set_local_port(9112)
            .set_user_data_path("enlister-data")
        )
        self.browser = Chromium(co)
        self.page = self.browser.latest_tab
        self._login(username, password)
        self._in_cart: Set[int] = set()

    def _login(self, user: str, pw: str):
        logger.info("Logging in…")
        self.page.get(LOGIN_URL)
        wait_and_input(self.page, "#userid", user)
        wait_and_input(self.page, "#pwd",    pw)
        wait_and_click(self.page, "@value=Sign In")
        sleep(3)

    def _refresh_cart(self) -> None:
        logger.info("Navigating to cart page…")
        self.page.get(CART_URL)
        iframe = self.page.get_frame("@id=ptifrmtgtframe")
        if iframe is None:
            logger.error("Cart iframe not found!")
            return
        try:
            logger.debug("Waiting for cart frame to finish loading…")
            iframe.wait.doc_loaded()
        except Exception as e:
            logger.warning("Frame load wait failed: %s", e)
        html = iframe.html
        ids = {int(n) for n in re.findall(r"\((\d+)\)", html)}
        self._in_cart = ids
        logger.info("Detected in‑cart classNbrs: %s", sorted(ids))

    def add_classes(self, ids: Iterable[int | str]):
        self._refresh_cart()
        to_add = [str(i) for i in ids if int(i) not in self._in_cart]
        if not to_add:
            logger.info("Nothing new to add.")
            return
        for nbr in to_add:
            logger.info("Adding %s …", nbr)
            self.page.get(CART_URL)
            wait_and_input(self.page, "#DERIVED_REGFRM1_CLASS_NBR", nbr)
            wait_and_click(self.page, "#DERIVED_REGFRM1_SSR_PB_ADDTOLIST2$70$")
            sleep(2)
            wait_and_click(self.page, "#DERIVED_CLS_DTL_NEXT_PB$76$")
            sleep(1)
            self._in_cart.add(int(nbr))
            logger.info("Enlisted %s", nbr)

async def listen_and_enlist(uri: str, enlister: Enlister):
    logger.info("Listening on WS %s", uri)
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
                    logger.info("WS received: %s", data)
                    await asyncio.to_thread(enlister.add_classes, data.get("available", []))
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WS connection closed: {e} — reconnecting…")
            await asyncio.sleep(5)
        except Exception as e:
            logger.warning(f"WS error: {e} — reconnecting…")
            await asyncio.sleep(5)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--user", default=USERNAME)
    p.add_argument("--pass", dest="pw", default=PASSWORD)
    p.add_argument("--ws",   default=WS_URI)
    args = p.parse_args()

    enlister = Enlister(args.user, args.pw)
    try:
        asyncio.run(listen_and_enlist(args.ws, enlister))
    except KeyboardInterrupt:
        logger.info("Shutting down…")

if __name__ == "__main__":
    main()
