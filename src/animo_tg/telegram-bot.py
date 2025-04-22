from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from pathlib import Path
from typing import Dict, Final, List, Sequence

import aiohttp
from dotenv import load_dotenv
from telegram import Update, constants
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

class CloudflareBlockedError(Exception):
    """Raised when the scraper replies 503 / cloudflare_blocked."""

load_dotenv()

TOKEN: Final[str] = os.environ["BOT_TOKEN"]
DATA_FILE: Final[Path] = Path("subscriptions.json")

DEFAULT_PREFS: Dict[str, object] = {
    "id_no": "",
    "courses": [],
    "sections": {},
    "previous_data": {},
}

SUBS: Dict[int, dict] = {}


def _load_subs() -> None:
    """Load existing subscriptions from disk."""
    if DATA_FILE.exists():
        SUBS.update(
            {
                int(k): v
                for k, v in json.loads(DATA_FILE.read_text("utf-8")).items()
            }
        )


def _save_subs() -> None:
    """Persist subscriptions to disk."""
    DATA_FILE.write_text(
        json.dumps(SUBS, indent=2, ensure_ascii=False), encoding="utf-8"
    )

async def fetch_course_data(course: str, id_no: str) -> list:
    """
    Call the local FastAPI scraper.

    Raises
    ------
    CloudflareBlockedError
        If the scraper replies with HTTP 503 / cloudflare_blocked.
    aiohttp.ClientError
        For non‑200/503 responses or network failures.
    """
    url = f"http://localhost:8000/scrape?course={course}&id_no={id_no}"
    async with aiohttp.ClientSession() as session:
        resp = await session.get(url, timeout=30)
        if resp.status == 503:
            raise CloudflareBlockedError
        if resp.status != 200:
            raise aiohttp.ClientError(f"HTTP {resp.status}")
        return await resp.json(encoding="utf-8")


def _fmt_section(section: dict) -> str:
    """Return a human‑readable Markdown description of one section."""
    meetings = [
        f"{m.get('day', '')} {m.get('time', '')} {m.get('room') or 'Online'}"
        for m in section.get("meetings", [])
    ]
    meetings_str = " | ".join(meetings) or "No schedule information"
    return (
        f"*{section['course']} {section['section']}* "
        f"(Class {section['classNbr']})\n"
        f"Enrolled: {section['enrolled']}/{section['enrlCap']} "
        f"| {section.get('remarks', '')}\n"
        f"Instructor: {section.get('instructor', 'TBA')}\n"
        f"Schedule: {meetings_str}\n"
    )


def _compose_status_lines(
    course: str,
    sections: Sequence[dict],
    title_suffix: str = "",
) -> List[str]:
    """
    Build a list of markdown strings representing the status of `sections`.
    """
    sections = sorted(sections, key=lambda s: s["section"])
    open_secs = [s for s in sections if s["enrolled"] < s["enrlCap"]]
    full_secs = [s for s in sections if s["enrolled"] >= s["enrlCap"]]

    lines: List[str] = [
        f"*{course}{title_suffix}*",
        f"Total: {len(sections)} | "
        f"Open: {len(open_secs)} | Full: {len(full_secs)}",
        "",
    ]
    if open_secs:
        lines.append("*Open sections*")
        lines.extend(_fmt_section(s) for s in open_secs)
        lines.append("")
    if full_secs:
        lines.append("*Full sections*")
        lines.extend(_fmt_section(s) for s in full_secs)
    return lines

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """/start – subscribe user."""
    SUBS.setdefault(update.effective_chat.id, copy.deepcopy(DEFAULT_PREFS))
    _save_subs()
    await update.message.reply_text("Welcome! Use /help to see commands.")


async def cmd_stop(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """/stop – unsubscribe user."""
    if SUBS.pop(update.effective_chat.id, None) is not None:
        _save_subs()
        await update.message.reply_text("Unsubscribed. Bye!")
    else:
        await update.message.reply_text("You were not subscribed.")


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """/help – user guide."""
    await update.message.reply_markdown(
        "*Commands*\n"
        "/start – subscribe\n"
        "/stop – unsubscribe\n"
        "/setid `<id>` – set student ID\n"
        "/addcourse `<COURSE>` or `<COURSE>:<CLASS>` – track\n"
        "/removecourse … – untrack\n"
        "/course `<COURSE>` – show all sections\n"
        "/check – send current status of tracked items\n"
        "/prefs – show your settings"
    )


async def cmd_setid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/setid – store student ID."""
    if not ctx.args:
        await update.message.reply_text("Usage: /setid <8‑digit ID>")
        return
    SUBS.setdefault(update.effective_chat.id, copy.deepcopy(DEFAULT_PREFS))[
        "id_no"
    ] = ctx.args[0]
    _save_subs()
    await update.message.reply_text(f"ID set to {ctx.args[0]}")


async def cmd_prefs(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """/prefs – show current subscription settings."""
    pref = SUBS.get(update.effective_chat.id, copy.deepcopy(DEFAULT_PREFS))
    courses = ", ".join(pref.get("courses", [])) or "None"
    sections = [
        f"{c}: {', '.join(map(str, s))}"
        for c, s in pref.get("sections", {}).items()
        if s
    ]
    sections_str = " | ".join(sections) or "None"
    await update.message.reply_markdown(
        "*Your settings*\n"
        f"• ID Number: {pref.get('id_no') or 'Not set'}\n"
        f"• Courses (all sections): {courses}\n"
        f"• Specific sections: {sections_str}"
    )

def _parse_course_arg(arg: str) -> tuple[str, int | None]:
    """Return (course_code, class_nbr_or_None)."""
    arg = arg.upper()
    if ":" not in arg:
        return arg, None
    course, nbr_str = arg.split(":", 1)
    if not nbr_str.isdigit():
        raise ValueError("Class number must be numeric")
    return course, int(nbr_str)


async def cmd_addcourse(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/addcourse – start tracking."""
    if not ctx.args:
        await update.message.reply_text(
            "Usage:\n"
            "  /addcourse <COURSE>\n"
            "  /addcourse <COURSE>:<CLASS>"
        )
        return
    chat_id = update.effective_chat.id
    SUBS.setdefault(chat_id, copy.deepcopy(DEFAULT_PREFS))
    try:
        course, nbr = _parse_course_arg(ctx.args[0])
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    if nbr is None:
        if course in SUBS[chat_id]["courses"]:
            await update.message.reply_text(f"Already tracking {course}.")
            return
        SUBS[chat_id]["courses"].append(course)
        await update.message.reply_text(f"Tracking all sections of {course}.")
    else:
        lst = SUBS[chat_id].setdefault("sections", {}).setdefault(course, [])
        if nbr in lst:
            await update.message.reply_text(
                f"Already tracking section {nbr} of {course}."
            )
            return
        lst.append(nbr)
        await update.message.reply_text(
            f"Tracking section {nbr} of {course}."
        )
    _save_subs()


async def cmd_removecourse(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """/removecourse – stop tracking."""
    if not ctx.args:
        await update.message.reply_text(
            "Usage:\n"
            "  /removecourse <COURSE>\n"
            "  /removecourse <COURSE>:<CLASS>"
        )
        return
    chat_id = update.effective_chat.id
    try:
        course, nbr = _parse_course_arg(ctx.args[0])
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    if nbr is None:
        try:
            SUBS[chat_id]["courses"].remove(course)
            SUBS[chat_id]["previous_data"].pop(course, None)
        except (KeyError, ValueError):
            await update.message.reply_text(f"Not tracking {course}.")
            return
        await update.message.reply_text(f"Stopped tracking {course}.")
    else:
        lst = SUBS.get(chat_id, {}).get("sections", {}).get(course, [])
        if nbr not in lst:
            await update.message.reply_text(
                f"Not tracking section {nbr} of {course}."
            )
            return
        lst.remove(nbr)
        if not lst:
            SUBS[chat_id]["sections"].pop(course)
        SUBS[chat_id]["previous_data"].pop(f"{course}:sections", None)
        await update.message.reply_text(
            f"Stopped tracking section {nbr} of {course}."
        )
    _save_subs()

async def _notify_cf_block(update: Update | None) -> None:
    """Notify user about Cloudflare block."""
    msg = (
        "⚠️  Cloudflare has temporarily blocked access.\n"
        "   I’ll try again automatically in about 5 minutes …"
    )
    if update:
        await update.message.reply_text(msg)


async def _send_status(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    course: str,
    id_no: str,
    track_all: bool,
    class_nbrs: List[int] | None = None,
) -> None:
    """
    Fetch and send full status (open + full sections) for one course
    or a subset of class numbers.
    """
    try:
        sections = await fetch_course_data(course, id_no)
    except CloudflareBlockedError:
        await _notify_cf_block(None)
        return
    except Exception as exc:
        logging.error("Status error for %s: %s", course, exc)
        await ctx.bot.send_message(chat_id, "Error fetching data.")
        return

    if not track_all and class_nbrs:
        sections = [s for s in sections if s["classNbr"] in class_nbrs]
        not_found = set(class_nbrs) - {s["classNbr"] for s in sections}
        if not_found:
            await ctx.bot.send_message(
                chat_id,
                f"⚠️  Section(s) {', '.join(map(str, sorted(not_found)))} "
                f"for {course} not found.",
            )

    if not sections:
        await ctx.bot.send_message(chat_id, f"No sections found for {course}.")
        return

    suffix = "" if track_all else f" – Sections {', '.join(map(str, class_nbrs))}"
    text_lines = _compose_status_lines(course, sections, suffix)

    msg_limit = constants.MessageLimit.MAX_TEXT_LENGTH
    chunks: List[str] = []
    buf = ""
    for line in text_lines:
        if len(buf) + len(line) + 2 > msg_limit:
            chunks.append(buf.strip())
            buf = line
        else:
            buf += ("\n\n" if buf else "") + line
    if buf:
        chunks.append(buf.strip())

    for idx, chunk in enumerate(chunks, 1):
        header = f"*{course}{suffix}* (part {idx}/{len(chunks)})\n\n" if len(chunks) > 1 else ""
        await ctx.bot.send_message(
            chat_id,
            header + chunk,
            parse_mode=constants.ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        await asyncio.sleep(0.5)

async def cmd_course(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/course – show all sections for a single course."""
    if not ctx.args:
        await update.message.reply_text("Usage: /course <COURSE>")
        return

    course = ctx.args[0].upper()
    pref = SUBS.get(update.effective_chat.id, {})
    id_no = pref.get("id_no")
    if not id_no:
        await update.message.reply_text("Set your ID first with /setid.")
        return

    await update.message.reply_text(f"Fetching {course} …")
    await _send_status(ctx, update.effective_chat.id, course, id_no, True)

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/check – send status of every tracked course / section."""
    chat_id = update.effective_chat.id
    prefs = SUBS.get(chat_id)
    if not prefs:
        await update.message.reply_text("You need to subscribe first. Use /start.")
        return

    id_no = prefs.get("id_no")
    if not id_no:
        await update.message.reply_text("Set your ID first with /setid.")
        return

    if not prefs.get("courses") and not prefs.get("sections"):
        await update.message.reply_text("You are not tracking anything yet.")
        return

    await update.message.reply_text("Checking your tracked items …")

    for course in prefs.get("courses", []):
        await _send_status(ctx, chat_id, course, id_no, True)
        await asyncio.sleep(0.2)

    for course, nbrs in prefs.get("sections", {}).items():
        if nbrs:
            await _send_status(ctx, chat_id, course, id_no, False, nbrs)
            await asyncio.sleep(0.2)

    await update.message.reply_text("Finished.")


def _diff_courses(old: List[dict], new: List[dict]) -> Dict[str, list]:
    """Compute added/removed/enrollment changed sets."""
    old_by = {s["classNbr"]: s for s in old}
    new_by = {s["classNbr"]: s for s in new}

    added = [s for k, s in new_by.items() if k not in old_by]
    removed = [s for k, s in old_by.items() if k not in new_by]

    changed: List[dict] = []
    for k, new_s in new_by.items():
        if k in old_by:
            old_enr = old_by[k]["enrolled"]
            if old_enr != new_s["enrolled"]:
                changed.append(
                    {
                        "section": new_s,
                        "old_enrolled": old_enr,
                        "new_enrolled": new_s["enrolled"],
                    }
                )
    return {"added": added, "removed": removed, "enrollment": changed}


async def _broadcast(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Background task – notify only on changes."""
    if not SUBS:
        return

    for chat_id, prefs in SUBS.items():
        id_no = prefs.get("id_no")
        if not id_no:
            continue

        for course in prefs.get("courses", []):
            await _process_updates(ctx, chat_id, course, id_no, True)
            await asyncio.sleep(0.1)

        for course, nbrs in prefs.get("sections", {}).items():
            if nbrs:
                await _process_updates(ctx, chat_id, course, id_no, False, nbrs)
                await asyncio.sleep(0.1)

    _save_subs()


async def _process_updates(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    course: str,
    id_no: str,
    track_all: bool,
    class_nbrs: List[int] | None = None,
) -> None:
    """Fetch, diff and notify for one course."""
    prev_data = SUBS[chat_id].setdefault("previous_data", {})
    key = course if track_all else f"{course}:sections"

    try:
        data = await fetch_course_data(course, id_no)
    except CloudflareBlockedError:
        await _notify_cf_block(None)
        return
    except Exception as exc:
        logging.error("Update error for %s: %s", key, exc)
        return

    if not track_all and class_nbrs:
        data = [s for s in data if s["classNbr"] in class_nbrs]

    diff = _diff_courses(prev_data.get(key, []), data)
    if any(diff.values()):
        lines: List[str] = [f"*Changes detected for {course}*"]
        if diff["added"]:
            lines.append("\n*New sections*")
            lines.extend(_fmt_section(s) for s in diff["added"])
        if diff["removed"]:
            lines.append("\n*Removed sections*")
            lines.extend(_fmt_section(s) for s in diff["removed"])
        if diff["enrollment"]:
            lines.append("\n*Enrollment changes*")
            for chg in diff["enrollment"]:
                sec = chg["section"]
                lines.append(
                    f"{sec['course']} {sec['section']} "
                    f"(Class {sec['classNbr']}): "
                    f"{chg['old_enrolled']} ➜ {chg['new_enrolled']}"
                )
        try:
            await ctx.bot.send_message(
                chat_id,
                "\n\n".join(lines),
                parse_mode=constants.ParseMode.MARKDOWN,
            )
        except Exception as exc:
            logging.error("Notify error: %s", exc)

    prev_data[key] = data


def main() -> None:
    """Entry point."""
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    DATA_FILE.parent.mkdir(exist_ok=True)
    _load_subs()

    app = Application.builder().token(TOKEN).build()

    handlers = [
        CommandHandler("start", cmd_start),
        CommandHandler("stop", cmd_stop),
        CommandHandler("help", cmd_help),
        CommandHandler("setid", cmd_setid),
        CommandHandler("prefs", cmd_prefs),
        CommandHandler("addcourse", cmd_addcourse),
        CommandHandler("removecourse", cmd_removecourse),
        CommandHandler("course", cmd_course),
        CommandHandler("check", cmd_check),
    ]
    for handler in handlers:
        app.add_handler(handler)

    app.job_queue.run_repeating(_broadcast, interval=300, first=10)

    logging.info("Bot starting …")
    app.run_polling()
    logging.info("Bot stopped.")


if __name__ == "__main__":
    main()
