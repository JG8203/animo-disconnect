#!/usr/bin/env python3
"""
Spawn multiple independent DrissionPage browsers in parallel,
log in, visit the cart, detect CF/waiting-room, and dump cookies.
"""
import json, os, shutil, sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import ElementNotFoundError, WaitTimeoutError
from time import sleep

LOGIN_URL      = "https://animo.sys.dlsu.edu.ph/psp/ps/?cmd=login&languageCd=ENG"
CART_URL       = (
    "https://animo.sys.dlsu.edu.ph/psp/ps/EMPLOYEE/HRMS/c/"
    "SA_LEARNER_SERVICES.SSR_SSENRL_CART.GBL&IsFolder=false"
)
COOKIE_DIR     = Path("cookies");     COOKIE_DIR.mkdir(exist_ok=True)
SCREENSHOT_DIR = Path("screenshots"); SCREENSHOT_DIR.mkdir(exist_ok=True)

CF_MARKERS     = ("cf-browser-verification","just a moment","checking your browser")
WAIT_MARK      = ("waiting room","virtual queue")


def _contains(html: str, markers: tuple[str,...]) -> bool:
    return any(m in html.lower() for m in markers)


def handle_instance(idx: int, user: str, pw: str):
    # 1) Build fresh options per browser
    opts = (
        ChromiumOptions(read_file=False)
        .auto_port()
        .set_pref("intl.accept_languages","en-US")
        .set_pref("autofill.profile_enabled", False)
    )

    browser = Chromium(opts)
    tab     = browser.new_tab(url=LOGIN_URL)
    tab.wait.doc_loaded()

    # 2) Early waiting-room detection
    if _contains(tab.html, WAIT_MARK):
        print(f"[{idx}] ⚠️ waiting room on login; saving cookies & screenshot.")
        Path(COOKIE_DIR/f"{idx}.json") \
            .write_text(json.dumps(tab.cookies().as_dict(), indent=2))
        shot = tab.screenshot(path=str(SCREENSHOT_DIR), filename=f"wait-{idx}.png")
        print(f"[{idx}] 📸 {shot}")
        browser.quit()
        shutil.rmtree(f"data_{idx}", ignore_errors=True)
        return

    try:
        # 3) Login
        tab.ele("#userid", timeout=10).input(user, clear=True, by_js=True)
        tab.ele("#pwd", timeout=10).input(pw,   clear=True, by_js=True)
        tab.ele('@value=Sign In', timeout=10).click(by_js=True)
        tab.wait.doc_loaded()

        sleep(1)
        # 4) Go to cart
        tab.get(CART_URL); tab.wait.doc_loaded()

        # 5) Cloudflare scan
        if _contains(tab.html, CF_MARKERS):
            raise RuntimeError("Cloudflare Turnstile detected (top)")
        for frame in tab.get_frames():
            try:
                html = frame.html
            except:
                continue
            if _contains(html, CF_MARKERS):
                raise RuntimeError("Cloudflare Turnstile detected (iframe)")
            if _contains(html, WAIT_MARK):
                print(f"[{idx}] ℹ️ waiting-room iframe; cookies will still be saved.")

        # 6) Dump cookies
        Path(COOKIE_DIR/f"{idx}.json") \
            .write_text(json.dumps(tab.cookies().as_dict(), indent=2))
        print(f"[{idx}] ✅ cookies saved")

    except Exception as e:
        print(f"[{idx}] ❌ {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        browser.quit()
        shutil.rmtree(f"data_{idx}", ignore_errors=True)


if __name__ == "__main__":
    load_dotenv()
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("-n","--number",type=int,default=5)
    p.add_argument("--user"); p.add_argument("--password")
    args = p.parse_args()

    USER = args.user     or os.getenv("ANIMO_USER")
    PASS = args.password or os.getenv("ANIMO_PASS")
    if not (USER and PASS):
        p.error("Set ANIMO_USER/PASS in .env or use flags")

    # 7) Run in parallel
    with ThreadPoolExecutor(max_workers=args.number) as exe:
        futures = [
            exe.submit(handle_instance, idx, USER, PASS)
            for idx in range(args.number)
        ]
        for future in as_completed(futures):
            future.result()  # re-raise any exceptions
