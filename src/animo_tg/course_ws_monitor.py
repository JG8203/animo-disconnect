"""
WebSocket-based course enrollment monitor.

This module tracks course enrollment status and notifies connected 
WebSocket clients when course slots become available.
"""

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set, Tuple

import aiohttp
import websockets
from websockets.legacy.server import WebSocketServerProtocol
from dotenv import load_dotenv

ClassNumber = int
CourseCode = str
EnrollmentData = Tuple[int, int]

CLIENTS: Set[WebSocketServerProtocol] = set()


class CloudflareBlockedError(Exception):
    """Raised when the scraper replies 503 / cloudflare_blocked."""


async def fetch_course_data(course: str, id_no: str) -> List[dict]:
    """Call the local FastAPI scraper and return its JSON payload."""
    url = f"http://localhost:8000/scrape?course={course}&id_no={id_no}"
    async with aiohttp.ClientSession() as session:
        resp = await session.get(url, timeout=30)
        if resp.status == 503:
            raise CloudflareBlockedError
        if resp.status != 200:
            raise aiohttp.ClientError(f"HTTP {resp.status}")
        return await resp.json(encoding="utf-8")


def _parse_course_arg(arg: str) -> Tuple[CourseCode, Optional[ClassNumber]]:
    """Split `COURSE(:CLASSNBR)` into ('COURSE', classnbr|None)."""
    arg = arg.upper()
    if ":" not in arg:
        return arg, None
    course, nbr_str = arg.split(":", 1)
    if not nbr_str.isdigit():
        raise argparse.ArgumentTypeError("Class number must be numeric")
    return course, int(nbr_str)


def _detect_openings(
    prev: Dict[ClassNumber, EnrollmentData], curr_sections: List[dict]
) -> Tuple[Set[ClassNumber], Dict[ClassNumber, EnrollmentData]]:
    """
    Return {classNbr} that flipped from full→open and
    an updated {classNbr: (enrolled, cap)} mapping for next iteration.
    """
    now = {s["classNbr"]: (s["enrolled"], s["enrlCap"]) for s in curr_sections}
    opened = {
        nbr
        for nbr, (enr, cap) in now.items()
        if enr < cap and (nbr not in prev or prev[nbr][0] >= prev[nbr][1])
    }
    return opened, now


async def ws_handler(ws: WebSocketServerProtocol):
    """Register client socket until it closes."""
    CLIENTS.add(ws)
    logging.info("New client connected. Total clients: %d", len(CLIENTS))
    try:
        await ws.wait_closed()
    finally:
        CLIENTS.discard(ws)
        logging.info("Client disconnected. Remaining clients: %d", len(CLIENTS))


async def broadcast(payload: dict):
    """Send payload to every connected client (if any)."""
    if not CLIENTS:
        logging.info("No clients connected to broadcast to")
        return
    if not payload.get("available"):
        logging.info("No available courses to broadcast")
        return
    msg = json.dumps(payload)
    logging.info("Broadcasting to %d client(s): %s", len(CLIENTS), msg)
    active_clients = [ws for ws in CLIENTS if not ws.closed]
    if active_clients:
        results = await asyncio.gather(
            *[ws.send(msg) for ws in active_clients], return_exceptions=True
        )
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            logging.warning("Encountered %d errors during broadcast", len(errors))
            for err in errors:
                logging.warning("Broadcast error: %s", err)
    else:
        logging.warning("No active clients to send to")


async def poll_courses(
    id_no: str,
    tracking: List[Tuple[CourseCode, Optional[ClassNumber]]],
    interval: int,
):
    """
    Loop forever: fetch each course, compare with previous state,
    and broadcast openings.
    """
    logging.info("Monitoring the following courses:")
    for course, specific_nbr in tracking:
        if specific_nbr is not None:
            logging.info("  - %s:%s", course, specific_nbr)
        else:
            logging.info("  - %s (all sections)", course)
    prev_state: Dict[CourseCode, Dict[ClassNumber, EnrollmentData]] = {}
    while True:
        opened_total = []
        current_status = []
        for course, specific_nbr in tracking:
            try:
                sections = await fetch_course_data(course, id_no)
            except CloudflareBlockedError:
                logging.warning("Cloudflare blocked – retrying later.")
                continue
            except (
                aiohttp.ClientError,
                json.JSONDecodeError,
                asyncio.TimeoutError,
            ) as exc:
                logging.error("Fetch error for %s: %s", course, exc)
                continue

            if specific_nbr is not None:
                sections = [s for s in sections if s["classNbr"] == specific_nbr]

            for section in sections:
                current_status.append(
                    {
                        "course": course,
                        "classNbr": section["classNbr"],
                        "enrolled": section["enrolled"],
                        "capacity": section["enrlCap"],
                        "available": section["enrlCap"] - section["enrolled"],
                    }
                )
            prev = prev_state.get(course, {})
            opened, now = _detect_openings(prev, sections)
            if opened:
                opened_total.extend(opened)
                logging.info("OPENED SLOTS DETECTED: %s in %s", sorted(opened), course)
            prev_state[course] = now

        timestamp = datetime.now(timezone(timedelta(hours=8)))
        logging.info("Status update at %s", timestamp.isoformat())
        for status in current_status:
            logging.info(
                "  %s #%s: %s/%s (%s slots available)",
                status["course"],
                status["classNbr"],
                status["enrolled"],
                status["capacity"],
                status["available"],
            )

        available_classes = [
            status["classNbr"] for status in current_status if status["available"] > 0
        ]

        if available_classes:
            payload = {
                "available": sorted(available_classes),
                "timestamp": timestamp.isoformat(),
            }
            logging.info(
                "Broadcasting available classes: %s", sorted(available_classes)
            )
            await broadcast(payload)
        elif opened_total:
            payload = {
                "available": sorted(opened_total),
                "timestamp": timestamp.isoformat(),
            }
            logging.info("Broadcasting newly opened slots: %s", sorted(opened_total))
            await broadcast(payload)

        await asyncio.sleep(interval)


def build_arg_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(description="Course slot WebSocket monitor")
    parser.add_argument(
        "--id",
        required=False,
        help="8‑digit student ID (env var ID_NO is also accepted)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Polling interval in seconds (default 300).",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="WebSocket bind address (default 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="WebSocket port (default 8765)"
    )
    parser.add_argument(
        "courses",
        nargs="+",
        type=_parse_course_arg,
        help="Course(s) to track, e.g. LCFILIB or LCFILIB:541",
    )
    return parser


async def main_async(args):
    """Main async entry point."""
    ws_server = await websockets.serve(ws_handler, args.host, args.port)
    logging.info("WebSocket server ready on ws://%s:%s/", args.host, args.port)

    poll_task = asyncio.create_task(poll_courses(args.id, args.courses, args.interval))

    await asyncio.gather(ws_server.wait_closed(), poll_task)


def main():
    """Main entry point."""
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    parser = build_arg_parser()
    args = parser.parse_args()

    if not args.id:
        args.id = os.getenv("ID_NO")

    if not args.id:
        raise SystemExit("Student ID not provided (use --id or ID_NO env var).")

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logging.info("Shutting down…")


if __name__ == "__main__":
    main()
