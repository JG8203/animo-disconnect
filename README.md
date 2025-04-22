# Animo Disconnect

This project provides tools to monitor course availability at De La Salle University (DLSU) and automatically add available sections to your Animo.sys shopping cart. It includes:

1.  **Scraper Service (`scraper.py`):** A FastAPI application that scrapes the official DLSU enrollment site (`enroll.dlsu.edu.ph`) for course offering details.
2.  **Course Monitor & WebSocket Server (`course_ws_monitor.py`):** Polls the Scraper Service, detects newly available course slots based on user-defined courses/sections, and broadcasts the class numbers of *currently available* sections via a WebSocket server.
3.  **Auto-Enlister (`auto_enlist.py`):** Connects to the WebSocket server, listens for available class numbers, logs into Animo.sys, and attempts to add those classes to the user's enrollment cart using browser automation (`DrissionPage`).
4.  **Telegram Bot (`telegram_bot.py`):** Allows users to track courses/sections via Telegram. It periodically polls the Scraper Service, notifies users of changes (new sections, enrollment changes, openings), and allows manual status checks.

## Features

*   Scrapes near real-time course offering data from the DLSU enrollment site.
*   Telegram bot interface for:
    *   Setting your Student ID.
    *   Adding/removing courses or specific sections to track.
    *   Receiving automatic notifications on enrollment changes or openings.
    *   Manually checking the current status of courses.
    *   Viewing your current preferences.
*   WebSocket server broadcasting available class numbers for specific tracked courses/sections.
*   Automated addition of available classes to the Animo.sys enrollment cart.
*   Configurable polling intervals.
*   Basic persistence of Telegram user preferences (`subscriptions.json`).
*   Mock servers (`mock_server.py`, `mock_ws_server.py`) included for testing components in isolation.

## Architecture Overview

The system components interact as follows:

```
+-----------------+      (scrapes)      +------------------------+
|  DLSU Website   | <-------------------|  scraper.py (FastAPI)  |
| (enroll.dlsu...) |                     | (runs on :8000)        |
+-----------------+                     +-----------+------------+
                                                    |
                       +----------------------------+----------------------------+
                       | (polls scraper)                                         | (polls scraper)
                       v                                                         v
+------------------------+                                       +-----------------------------+
| telegram_bot.py        | -- (sends/receives messages) -->      |      Telegram User          |
| (connects to Telegram) |                                       +-----------------------------+
+------------------------+

+--------------------------+      (broadcasts available)      +--------------------------+
| course_ws_monitor.py     | --------------------------------> | WebSocket Server (:8765) |
| (polls scraper,          |                                   +------------+-------------+
|  tracks specific courses) |                                                | (listens)
+--------------------------+                                                |
                                                                            v
                                                     +----------------------------------------------+
                                                     | auto_enlist.py                               | -- (logs in, adds to cart) --> +-------------+
                                                     | (connects to WS, controls browser via DrissionPage) |                                | Animo.sys   |
                                                     +----------------------------------------------+                                +-------------+
```

*   The **Scraper** is the core data source.
*   The **Telegram Bot** uses the Scraper to provide information *to the user*.
*   The **Course Monitor** uses the Scraper to identify available slots for specific courses/sections and broadcasts them.
*   The **Auto-Enlister** listens to the **Course Monitor's** broadcast and takes action on Animo.sys.

You can run the Telegram Bot and the Auto-Enlist system independently or together.

## Prerequisites

*   Python 3.8+
*   [Poetry](https://python-poetry.org/docs/#installation) for dependency management.
*   A running instance of Chromium or Google Chrome (for `DrissionPage` used by `scraper.py` and `auto_enlist.py`).
*   Your 8-digit DLSU Student ID number.
*   Your Animo.sys login credentials (Username and Password) - *Only needed for `auto_enlist.py`*.
*   A Telegram Bot Token obtained from [@BotFather](https://t.me/BotFather) on Telegram - *Only needed for `telegram_bot.py`*.

## Installation

1.  **Clone the repository:**
    ```bash
    git clone <repository_url>
    cd animo-tg
    ```
2.  **Install dependencies using Poetry:**
    ```bash
    poetry install
    ```

## Configuration

Configuration is primarily done via a `.env` file in the project root directory.

1.  **Create the `.env` file:**
    ```bash
    cp .env.example .env # If an example file exists, otherwise create it manually
    ```

2.  **Edit the `.env` file** with your details:

    ```dotenv
    # --- Required for Auto-Enlister (auto_enlist.py) ---
    # Your Animo.sys username (often your email without @dlsu.edu.ph)
    ANIMO_USER=your_animo_sys_username
    # Your Animo.sys password
    ANIMO_PASS=your_animo_sys_password

    # --- Required for Scraper, Monitor, and Telegram Bot ---
    # Your 8-digit DLSU Student ID Number
    ID_NO=12345678

    # --- Required for Telegram Bot (telegram_bot.py) ---
    # Your Telegram Bot Token from @BotFather
    BOT_TOKEN=1234567890:ABC...XYZ

    # --- Optional / Defaults ---
    # WebSocket URI for Auto-Enlister to connect to. Defaults to the monitor's server.
    # WS_URI=ws://localhost:8765

    # Scraper URL used by Telegram Bot and Course Monitor. Defaults to local scraper.
    # SCRAPER_URL=http://localhost:8000/scrape
    ```

## Running the Components

You need to run the necessary components, potentially in separate terminal windows or using a process manager like `supervisor` or `pm2`.

**1. Run the Scraper Service (`scraper.py`)**

This service must be running for the *Course Monitor* and *Telegram Bot* to function (unless you modify them to use a different data source or are only using mock data).

```bash
# Option 1: Using uvicorn (recommended for stability)
uvicorn src.animo_tg.scraper:app --host 0.0.0.0 --port 8000 --reload

# Option 2: Directly (mainly for quick tests)
# Note: This doesn't run the FastAPI app, only the CLI part if __main__ is executed.
#       Use Option 1 to run the actual API service.
# python src/animo_tg/scraper.py --course <COURSE> --id <ID> # Runs a single scrape
```

*   The service will be available at `http://localhost:8000`.
*   Keep this terminal running.

**2. Run the Course Monitor & WebSocket Server (`course_ws_monitor.py`)**

This component polls the scraper and broadcasts available class numbers via WebSocket. *Only needed if you intend to use the Auto-Enlister.*

```bash
python src/animo_tg/course_ws_monitor.py --id <YOUR_ID_NO> [--interval 300] [--host 0.0.0.0] [--port 8765] <COURSE_CODE_1> [<COURSE_CODE_2>:<CLASS_NBR_2> ...]
```

*   Replace `<YOUR_ID_NO>` with your 8-digit ID (or ensure `ID_NO` is set in `.env`).
*   Specify one or more courses or course:class_number pairs you want the monitor (and subsequently the auto-enlister) to track. Examples: `LCFILIB`, `CSOPESY:1234`.
*   `--interval`: Polling interval in seconds (default: 300).
*   `--host`/`--port`: WebSocket server bind address and port (defaults shown).
*   Keep this terminal running. It will connect to the scraper running on `localhost:8000` by default.

**3. Run the Auto-Enlister (`auto_enlist.py`)**

This connects to the WebSocket server and attempts to add classes to your Animo.sys cart.

```bash
python src/animo_tg/auto_enlist.py [--user <ANIMO_USER>] [--pass <ANIMO_PASS>] [--ws <WEBSOCKET_URI>]
```

*   It will use credentials and the WebSocket URI from the `.env` file by default. You can override them with command-line arguments.
*   The default `--ws` value in the script points to `WS_URI` from `.env`, which should typically be `ws://localhost:8765` if running the monitor locally.
*   This will open a Chromium browser window controlled by `DrissionPage`. It will log in and then wait for messages from the WebSocket server. **Do not close this browser window manually.**
*   Keep this terminal running.

**4. Run the Telegram Bot (`telegram_bot.py`)**

This component runs independently to provide Telegram-based monitoring and notifications.

```bash
python src/animo_tg/telegram_bot.py
```

*   Ensure `BOT_TOKEN` and `ID_NO` are set in your `.env` file.
*   It will connect to the scraper service defined by `SCRAPER_URL` (default `http://localhost:8000/scrape`).
*   Keep this terminal running. Interact with the bot on Telegram.

## Telegram Bot Usage

Once the Telegram bot is running, find it on Telegram using the username you gave it via BotFather and start a chat.

*   `/start`: Subscribe to the bot and see the welcome message.
*   `/stop`: Unsubscribe from the bot and stop receiving updates.
*   `/help`: Show the list of available commands.
*   `/setid <ID_NUMBER>`: **Required first step!** Set your 8-digit student ID (e.g., `/setid 12345678`). The bot needs this to query the scraper.
*   `/addcourse <COURSE>`: Track *all* sections of a specific course (e.g., `/addcourse LCFILIB`).
*   `/addcourse <COURSE>:<CLASS_NBR>`: Track only a *specific* section of a course (e.g., `/addcourse CSOPESY:1234`).
*   `/removecourse <COURSE or COURSE:CLASS_NBR>`: Stop tracking a course or a specific section.
*   `/course <COURSE>`: Manually check and display the *current* status of all sections for a given course *now*.
*   `/check`: Manually trigger an update check for *all* courses and sections you are currently tracking *now*. Shows current status similar to `/course` but for everything tracked.
*   `/prefs`: Display your currently set Student ID and the list of courses/sections you are tracking.

The bot will periodically check (default every 5 minutes) the status of your tracked items and notify you *only* if there are changes (enrollment numbers changed, sections opened/closed/added/removed).

## Testing with Mock Servers

For testing `auto_enlist.py` or `telegram_bot.py` without running the live scraper or hitting the actual DLSU website, you can use the mock servers:

*   **Mock Scraper (`mock_server.py`):** Simulates the scraper API. Run with `uvicorn src.animo_tg.mock_server:app --port 8000`. Configure `telegram_bot.py` or `course_ws_monitor.py` to use `SCRAPER_URL=http://localhost:8000/scrape`.
*   **Mock WebSocket Server (`mock_ws_server.py`):** Simulates the `course_ws_monitor.py` broadcast. Run with `python src/animo_tg/mock_ws_server.py`. Configure `auto_enlist.py` to connect to `ws://localhost:9000`.

## Important Notes & Disclaimers

*   **USE AT YOUR OWN RISK.** Automating interactions with university systems like Animo.sys might be against their Terms of Service. Using this tool could potentially lead to temporary locks or other issues with your account. The developers are not responsible for any consequences of using this software.
*   **NOT AFFILIATED WITH DLSU.** This is an independent project.
*   **RELIABILITY:** The scraper's success depends on the DLSU enrollment website's structure remaining consistent and avoiding Cloudflare blocks. The auto-enlister depends on the Animo.sys interface structure. Changes on these websites may break the tool.
*   **CLOUDFLARE:** The DLSU enrollment site uses Cloudflare. The scraper may occasionally be blocked, resulting in errors (`CloudflareBlockedError` / HTTP 503). The tools have basic error handling, but persistent blocks may occur.
*   **SESSION MANAGEMENT:** The `auto_enlist.py` script maintains a browser session. If the Animo.sys session expires, it might fail. You may need to restart the script occasionally.
*   **RESOURCE USAGE:** Running browser automation (`DrissionPage`) consumes significant RAM and CPU resources.
*   **SECURITY:** Your Animo.sys credentials are stored in the `.env` file. Ensure this file and your system are secure.
