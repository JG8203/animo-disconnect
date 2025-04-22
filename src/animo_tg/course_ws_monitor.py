from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Final, List, Set, Tuple

import aiohttp
import websockets
from websockets.server import WebSocketServerProtocol
from dotenv import load_dotenv

class CloudflareBlockedError(Exception):
    """Raised when the scraper replies 503 / cloudflare_blocked."""

async def fetch_course_data(course: str, id_no: str) -> List[dict]:
    """
    Call the local FastAPI scraper and return its JSON payload.

    Raises
    ------
    CloudflareBlockedError
        If the scraper replies HTTP‑503.
    aiohttp.ClientError
        For non‑200/503 responses or network failures.
    """
    url = f"http://localhost:8000/scrape?course={course}&id_no={id_no}"
    async with aiohttp.ClientSession() as session:
        resp = await session.get(url, timeout=30)
        if resp.status == 503:
            raise CloudflareBlockedError
        if resp.status != 200:
            raise aiohttp.ClientError(f"HTTP {resp.status}")
        return await resp.json(encoding="utf-8")

def _parse_course_arg(arg: str) -> Tuple[str, int | None]:
    """Split `COURSE(:CLASSNBR)` into ('COURSE', classnbr|None)."""
    arg = arg.upper()
    if ":" not in arg:
        return arg, None
    course, nbr_str = arg.split(":", 1)
    if not nbr_str.isdigit():
        raise argparse.ArgumentTypeError("Class number must be numeric")
    return course, int(nbr_str)

def _detect_openings(
    prev: Dict[int, Tuple[int, int]],
    curr_sections: List[dict]
) -> Tuple[Set[int], Dict[int, Tuple[int, int]]]:
    """
    Return {classNbr} that flipped from full→open and
    an updated {classNbr: (enrolled, cap)} mapping for next iteration.
    """
    now: Dict[int, Tuple[int, int]] = {
        s["classNbr"]: (s["enrolled"], s["enrlCap"]) for s in curr_sections
    }
    opened = {
        nbr for nbr, (enr, cap) in now.items()
        if enr < cap and (nbr not in prev or prev[nbr][0] >= prev[nbr][1])
    }
    return opened, now

CLIENTS: Set[WebSocketServerProtocol] = set()

async def ws_handler(ws: WebSocketServerProtocol):
    """Register client socket until it closes."""
    CLIENTS.add(ws)
    try:
        await ws.wait_closed()
    finally:
        CLIENTS.discard(ws)


async def broadcast(payload: dict):
    """Send `payload` to every connected client (if any)."""
    if not CLIENTS:
        logging.info("No clients connected to broadcast to")
        return
        
    if not payload.get("available"):
        logging.info("No available courses to broadcast")
        return
        
    msg = json.dumps(payload)
    logging.info(f"Broadcasting to {len(CLIENTS)} client(s): {msg}")
    
    tasks = []
    for ws in CLIENTS:
        try:
            tasks.append(ws.send(msg))
            logging.debug(f"Added send task for client")
        except Exception as e:
            logging.warning(f"Error checking WebSocket state: {e}")
    
    if tasks:
        logging.info(f"Sending to {len(tasks)} active clients")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            logging.warning(f"Encountered {len(errors)} errors during broadcast")
            for err in errors:
                logging.warning(f"Broadcast error: {err}")
    else:
        logging.warning("No active clients to send to")

async def poll_courses(
    id_no: str,
    tracking: List[Tuple[str, int | None]],
    interval: int,
):
    """
    Loop forever: fetch each course, compare with previous state,
    and broadcast openings.
    """
    logging.info("Monitoring the following courses:")
    for course, specific_nbr in tracking:
        if specific_nbr is not None:
            logging.info(f"  - {course}:{specific_nbr}")
        else:
            logging.info(f"  - {course} (all sections)")
    
    prev_state: Dict[str, Dict[int, Tuple[int, int]]] = {}
    while True:
        opened_total: List[int] = []
        current_status: List[dict] = []
        
        for course, specific_nbr in tracking:
            try:
                sections = await fetch_course_data(course, id_no)
            except CloudflareBlockedError:
                logging.warning("Cloudflare blocked – retrying later.")
                continue
            except Exception as exc:
                logging.error("Fetch error for %s: %s", course, exc)
                continue

            if specific_nbr is not None:
                sections = [s for s in sections if s["classNbr"] == specific_nbr]

            for section in sections:
                current_status.append({
                    "course": course,
                    "classNbr": section["classNbr"],
                    "enrolled": section["enrolled"],
                    "capacity": section["enrlCap"],
                    "available": section["enrlCap"] - section["enrolled"]
                })
            
            prev = prev_state.get(course, {})
            opened, now = _detect_openings(prev, sections)
            if opened:
                opened_total.extend(opened)
                logging.info(f"OPENED SLOTS DETECTED: {sorted(opened)} in {course}")
            prev_state[course] = now

        timestamp = datetime.now(timezone(timedelta(hours=8)))
        logging.info(f"Status update at {timestamp.isoformat()}")
        for status in current_status:
            logging.info(f"  {status['course']} #{status['classNbr']}: {status['enrolled']}/{status['capacity']} ({status['available']} slots available)")
        
        available_classes = [
            status["classNbr"] for status in current_status 
            if status["available"] > 0
        ]
        
        if available_classes:
            logging.info(f"Broadcasting available classes: {sorted(available_classes)}")
            await broadcast({
                "available": sorted(available_classes),
                "timestamp": timestamp.isoformat()
            })
        elif opened_total:
            logging.info(f"Broadcasting newly opened slots: {sorted(opened_total)}")
            await broadcast({
                "available": sorted(opened_total),
                "timestamp": timestamp.isoformat()
            })
            
        await asyncio.sleep(interval)

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Course slot WebSocket monitor")
    p.add_argument(
        "--id", required=False, default=os.getenv("ID_NO"),
        help="8‑digit student ID (env var ID_NO is also accepted)",
    )
    p.add_argument(
        "--interval", type=int, default=300,
        help="Polling interval in seconds (default 300)."
    )
    p.add_argument("--host", default="0.0.0.0",
                   help="WebSocket bind address (default 0.0.0.0)")
    p.add_argument("--port", type=int, default=8765,
                   help="WebSocket port (default 8765)")
    p.add_argument(
        "courses", nargs="+", type=_parse_course_arg,
        help="Course(s) to track, e.g. LCFILIB or LCFILIB:541"
    )
    return p

async def main_async(args):
    ws_server = await websockets.serve(ws_handler, args.host, args.port)
    logging.info("WebSocket server ready on ws://%s:%d/", args.host, args.port)

    poll_task = asyncio.create_task(
        poll_courses(args.id, args.courses, args.interval)
    )

    await asyncio.gather(ws_server.wait_closed(), poll_task)

def main():
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = build_arg_parser().parse_args()
    if not args.id:
        raise SystemExit("Student ID not provided (use --id or ID_NO env var).")

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logging.info("Shutting down…")

if __name__ == "__main__":
    main()
