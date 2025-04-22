from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone

import websockets

HOST = "localhost"
PORT = 9000
CLASS_NBRS = ["4091", "2523", "101", "656", "541", "3348"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s — %(message)s",
                    datefmt="%H:%M:%S")


async def handler(ws):
    """Push the full list once and keep the connection alive."""
    payload = {
        "available": CLASS_NBRS,                       # all at once
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await ws.send(json.dumps(payload))
    logging.info("Pushed %s", payload)
    await asyncio.Future()

async def main():
    async with websockets.serve(handler, HOST, PORT):
        logging.info("Mock WS server ready on ws://%s:%s/", HOST, PORT)
        await asyncio.Future()          # run forever

if __name__ == "__main__":
    asyncio.run(main())
