from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional
from time import sleep

from dotenv import load_dotenv
from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import ElementNotFoundError, WaitTimeoutError

LOGIN_URL = (
    "https://animo.sys.dlsu.edu.ph/psp/ps/"
    "?cmd=login&languageCd=ENG"
)
CART_URL = (
    "https://animo.sys.dlsu.edu.ph/psp/ps/EMPLOYEE/HRMS/c/"
    "SA_LEARNER_SERVICES.SSR_SSENRL_CART.GBL?"
    "FolderPath=PORTAL_ROOT_OBJECT.CO_EMPLOYEE_SELF_SERVICE."
    "HCCC_ENROLLMENT.HC_SSR_SSENRL_CART_GBL&IsFolder=false"
)
BASE_PORT = 9333

COOKIE_DIR = Path("cookies").resolve()
COOKIE_DIR.mkdir(exist_ok=True)
SCREENSHOT_DIR = Path("screenshots").resolve()
SCREENSHOT_DIR.mkdir(exist_ok=True)

CF_MARKERS = ("cf-browser-verification", "just a moment", "checking your browser")
WAITING_ROOM_MARK = ("waiting room", "virtual queue")


class CloudflareBlockedError(RuntimeError):
    pass


def wait_and_input(tab, selector: str, text: str) -> None:
    try:
        elem = tab.ele(selector, timeout=10)
        elem.input(text, clear=True, by_js=True)
    except (ElementNotFoundError, WaitTimeoutError):
        raise RuntimeError(f"Cannot locate input {selector!r}")


def wait_and_click(tab, selector: str) -> None:
    try:
        tab.ele(selector, timeout=10).click(by_js=True)
    except (ElementNotFoundError, WaitTimeoutError):
        raise RuntimeError(f"Cannot click {selector!r}")


def _contains_marker(html: str, markers: tuple[str, ...]) -> bool:
    lower = html.lower()
    return any(m in lower for m in markers)


def detect_cloudflare(tab) -> None:
    if _contains_marker(tab.html, CF_MARKERS):
        raise CloudflareBlockedError("Cloudflare Turnstile detected on top page")
    for frame in tab.get_frames():
        try:
            html = frame.html
        except Exception:
            continue
        if _contains_marker(html, CF_MARKERS):
            raise CloudflareBlockedError("Cloudflare Turnstile detected in iframe")
        if _contains_marker(html, WAITING_ROOM_MARK):
            print("ℹ️  Waiting-room iframe detected; will continue and save state.")


def detect_top_waiting_room(tab) -> bool:
    return _contains_marker(tab.html, WAITING_ROOM_MARK)


def spawn_instances(total: int, user: str, pw: str, base_port: int, spawn_interval: float) -> None:
    for idx in range(total):
        if idx > 0 and spawn_interval > 0:
            sleep(spawn_interval * 60)

        port = base_port + idx
        data_path = Path(f"data_{idx}")

        opts = (
            ChromiumOptions(read_file=False)
            .set_local_port(port)
            .set_user_data_path(str(data_path))
            .set_argument("lang", "en-US")
            .set_pref("intl.accept_languages", "en-US")
            .set_pref("autofill.profile_enabled", False)
        )
        browser = Chromium(opts)
        tab = browser.new_tab(url=LOGIN_URL)
        tab.wait.doc_loaded()

        if detect_top_waiting_room(tab):
            print(f"[#-{idx}] ⚠️  Waiting room detected on login page; saving state and exiting.")
            (COOKIE_DIR / f"instance-{idx}.json").write_text(
                json.dumps(tab.cookies().as_dict(), indent=2)
            )
            shot = tab.screenshot(
                path=str(SCREENSHOT_DIR),
                filename=f"waiting-room-{idx}.png"
            )
            print(f"[#-{idx}] 📸 Screenshot saved → {shot}")
            browser.quit()
            shutil.rmtree(data_path, ignore_errors=True)
            sys.exit(0)

        try:
            wait_and_input(tab, "#userid", user)
            wait_and_input(tab, "#pwd", pw)
            wait_and_click(tab, '@value=Sign In')
            tab.wait.doc_loaded()

            sleep(1)
            tab.get(CART_URL)
            tab.wait.doc_loaded()

            detect_cloudflare(tab)

            cfile = COOKIE_DIR / f"instance-{idx}.json"
            cfile.write_text(json.dumps(tab.cookies().as_dict(), indent=2))
            print(f"[#-{idx}] ✅ Cookies dumped → {cfile}")

        except CloudflareBlockedError as e:
            print(f"[#-{idx}] ❌ {e}; aborting run.", file=sys.stderr)
            browser.quit()
            shutil.rmtree(data_path, ignore_errors=True)
            sys.exit(1)
        except Exception as e:
            print(f"[#-{idx}] ⚠️  Error: {e}", file=sys.stderr)
        finally:
            browser.quit()
            shutil.rmtree(data_path, ignore_errors=True)


if __name__ == "__main__":
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Spawn DrissionPage browsers, log in, access cart, save cookies/screenshots."
    )
    parser.add_argument(
        "-n", "--number",
        type=int,
        default=9,
        help="How many Chromium instances to launch"
    )
    parser.add_argument(
        "--spawn-interval", "-i",
        type=float,
        default=0.0,
        help="Minutes to wait between each browser spawn"
    )
    parser.add_argument("--user", help="Animo.sys ID (overrides ANIMO_USER in .env)")
    parser.add_argument("--password", help="Animo.sys password (overrides ANIMO_PASS in .env)")
    args = parser.parse_args()

    USER = args.user or os.getenv("ANIMO_USER")
    PASS = args.password or os.getenv("ANIMO_PASS")
    if not (USER and PASS):
        parser.error("Missing credentials: set ANIMO_USER/ANIMO_PASS in .env or use --user/--password")

    spawn_instances(args.number, USER, PASS, BASE_PORT, args.spawn_interval)
