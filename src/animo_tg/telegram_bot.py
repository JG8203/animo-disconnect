"""
Telegram bot to monitor DLSU course enrollment status and notify users of changes.

Uses a separate scraper microservice (FastAPI) to fetch data.
Stores user subscriptions and preferences in a JSON file.
Periodically checks for updates and sends notifications for changes.
"""

import asyncio
import copy
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Final, List, Optional, Tuple

import aiohttp
from dotenv import load_dotenv
from telegram import Update, constants
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TOKEN: Final[str] = os.environ["BOT_TOKEN"]
DATA_FILE: Final[Path] = Path("subscriptions.json")
DEFAULT_POLLING_INTERVAL: Final[int] = 300
SCRAPER_URL: Final[str] = os.environ.get("SCRAPER_URL", "http://localhost:8000/scrape")

DEFAULT_PREFS: Dict[str, Any] = {
    "id_no": "",
    "courses": [],
    "sections": {},
    "previous_data": {},
}

SUBSCRIPTIONS: Dict[int, dict] = {}


class CloudflareBlockedError(Exception):
    """Raised when the scraper replies 503 / cloudflare_blocked."""


@dataclass
class TrackingInfo:
    """Holds information needed to fetch and process course data for a user."""

    chat_id: int
    student_id: str
    course: str
    track_all: bool = True
    class_numbers: List[int] = field(default_factory=list)

    def get_data_key(self) -> str:
        """Returns the key used for storing previous data."""
        return self.course if self.track_all else f"{self.course}:sections"


def load_subscriptions() -> None:
    """Load existing subscriptions from disk."""
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text("utf-8"))
            SUBSCRIPTIONS.update({int(k): v for k, v in data.items()})
            logging.info(
                "Loaded %d subscriptions from %s", len(SUBSCRIPTIONS), DATA_FILE
            )
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logging.error("Error loading subscriptions from %s: %s", DATA_FILE, e)


def save_subscriptions() -> None:
    """Persist subscriptions to disk."""
    try:
        DATA_FILE.write_text(
            json.dumps(SUBSCRIPTIONS, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logging.debug("Saved %d subscriptions to %s", len(SUBSCRIPTIONS), DATA_FILE)
    except Exception as e:
        logging.error(
            "Error saving subscriptions to %s: %s", DATA_FILE, e, exc_info=True
        )


async def fetch_course_data(course: str, id_no: str) -> List[dict]:
    """
    Call the scraper service.

    Args:
        course: Course code to fetch.
        id_no: Student ID number.

    Returns:
        List of course sections data.

    Raises:
        CloudflareBlockedError: If the scraper replies with HTTP 503.
        aiohttp.ClientError: For non-200/503 responses or network failures.
    """
    url = f"{SCRAPER_URL}?course={course}&id_no={id_no}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=30) as resp:
                if resp.status == 503:
                    logging.warning(
                        "Scraper returned 503 (Cloudflare Blocked?) for %s", url
                    )
                    raise CloudflareBlockedError(f"Scraper returned 503 for {course}")
                if resp.status != 200:
                    logging.error("Scraper returned HTTP %d for %s", resp.status, url)
                    raise aiohttp.ClientError(f"Scraper HTTP {resp.status}")
                return await resp.json(encoding="utf-8")
        except asyncio.TimeoutError as e:
            logging.error("Timeout fetching data from scraper for %s", url)
            raise aiohttp.ClientError("Scraper request timed out") from e
        except aiohttp.ClientConnectorError as e:
            logging.error("Connection error contacting scraper for %s: %s", url, e)
            raise aiohttp.ClientError(f"Cannot connect to scraper: {e}") from e


def format_section(section: dict) -> str:
    """Return a human-readable Markdown description of one section."""
    meetings = [
        f"{m.get('day', '')} {m.get('time', '')} {m.get('room') or 'Online'}"
        for m in section.get("meetings", [])
    ]
    meetings_str = (
        " | ".join(m.strip() for m in meetings if m.strip())
        or "No schedule information"
    )

    return (
        f"*{section.get('course', 'N/A')} {section.get('section', 'N/A')}* "
        f"(Class {section.get('classNbr', 'N/A')})\n"
        f"Enrolled: {section.get('enrolled', '?')}/{section.get('enrlCap', '?')} "
        f"| {section.get('remarks', '')}\n"
        f"Instructor: {section.get('instructor', 'TBA')}\n"
        f"Schedule: {meetings_str}\n"
    )


def compose_status_lines(
    course: str,
    sections: List[dict],
    title_suffix: str = "",
) -> List[str]:
    """Build a list of markdown strings representing the status of sections."""
    sections = sorted(sections, key=lambda s: s.get("section", ""))
    open_sections = [s for s in sections if s.get("enrolled", 0) < s.get("enrlCap", 0)]
    full_sections = [s for s in sections if s.get("enrolled", 0) >= s.get("enrlCap", 0)]

    lines: List[str] = [
        f"*{course}{title_suffix}*",
        f"Total: {len(sections)} | "
        f"Open: {len(open_sections)} | Full: {len(full_sections)}",
        "",
    ]

    if open_sections:
        lines.append("*Open sections*")
        lines.extend(format_section(s) for s in open_sections)
        lines.append("")

    if full_sections:
        lines.append("*Full sections*")
        lines.extend(format_section(s) for s in full_sections)

    return lines


def parse_course_arg(arg: str) -> Tuple[str, Optional[int]]:
    """Parse a course argument (e.g., "CSOPESY" or "CSOPESY:1234")."""
    arg = arg.upper().strip()
    if ":" not in arg:
        return arg, None

    course, nbr_str = arg.split(":", 1)
    if not nbr_str.isdigit():
        raise ValueError("Class number must be numeric")

    return course, int(nbr_str)


def diff_courses(old: List[dict], new: List[dict]) -> Dict[str, List]:
    """Compute differences (added, removed, enrollment changes) between sections."""
    old_by_number = {s["classNbr"]: s for s in old if "classNbr" in s}
    new_by_number = {s["classNbr"]: s for s in new if "classNbr" in s}

    added = [s for k, s in new_by_number.items() if k not in old_by_number]
    removed = [s for k, s in old_by_number.items() if k not in new_by_number]

    enrollment_changes: List[dict] = []
    for class_number, new_section in new_by_number.items():
        if class_number in old_by_number:
            old_enrolled = old_by_number[class_number].get("enrolled")
            new_enrolled = new_section.get("enrolled")
            if (
                isinstance(old_enrolled, int)
                and isinstance(new_enrolled, int)
                and old_enrolled != new_enrolled
            ):
                enrollment_changes.append(
                    {
                        "section": new_section,
                        "old_enrolled": old_enrolled,
                        "new_enrolled": new_enrolled,
                    }
                )

    return {"added": added, "removed": removed, "enrollment": enrollment_changes}


async def _send_long_message(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text_lines: List[str], title: str = ""
) -> None:
    """Sends potentially long messages by splitting them into chunks."""
    msg_limit = constants.MessageLimit.MAX_TEXT_LENGTH
    chunks: List[str] = []
    current_chunk = ""

    for line in text_lines:
        line_with_separators = ("\n\n" if current_chunk else "") + line
        if len(current_chunk) + len(line_with_separators) > msg_limit:
            chunks.append(current_chunk.strip())
            current_chunk = line
        else:
            current_chunk += line_with_separators

    if current_chunk:
        chunks.append(current_chunk.strip())

    for idx, chunk in enumerate(chunks, 1):
        header = (
            f"*{title}* (part {idx}/{len(chunks)})\n\n"
            if len(chunks) > 1 and title
            else ""
        )
        full_message = header + chunk
        try:
            await ctx.bot.send_message(
                chat_id,
                full_message,
                parse_mode=constants.ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logging.error(
                "Failed to send message chunk %d/%d to chat %d: %s",
                idx,
                len(chunks),
                chat_id,
                e,
            )
            if idx == 1:
                await ctx.bot.send_message(
                    chat_id,
                    f"❌ Error sending status update for {title}. Please try again later.",
                )
            break
        await asyncio.sleep(0.5)


async def notify_cloudflare_block(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, update: Optional[Update] = None
) -> None:
    """Notify user about Cloudflare block."""
    msg = (
        "❌ The course checker is temporarily blocked (likely by Cloudflare).\n"
        "It should resolve automatically. I'll keep trying in the background."
    )
    try:
        if update and update.message:
            await update.message.reply_text(msg)
        else:
            await ctx.bot.send_message(chat_id, msg)
    except Exception as e:
        logging.error(
            "Failed to send Cloudflare block notification to chat %d: %s", chat_id, e
        )
    logging.warning("Cloudflare block detected for chat %d", chat_id)


async def _fetch_and_filter_data(tracking_info: TrackingInfo) -> Optional[List[dict]]:
    """Fetches course data and filters it based on TrackingInfo."""
    try:
        sections = await fetch_course_data(
            tracking_info.course, tracking_info.student_id
        )
        if not tracking_info.track_all:
            sections = [
                s for s in sections if s.get("classNbr") in tracking_info.class_numbers
            ]
        return sections
    except CloudflareBlockedError:
        raise
    except aiohttp.ClientError as exc:
        logging.error(
            "Client error fetching data for %s: %s", tracking_info.course, exc
        )
        return None
    except Exception as exc:
        logging.error(
            "Unexpected error fetching/filtering data for %s: %s",
            tracking_info.course,
            exc,
            exc_info=True,
        )
        return None


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    chat_id = update.effective_chat.id
    if chat_id not in SUBSCRIPTIONS:
        SUBSCRIPTIONS[chat_id] = copy.deepcopy(DEFAULT_PREFS)
        save_subscriptions()
        await update.message.reply_text(
            "Welcome! 🎓 I can help you track DLSU course slots.\n"
            "1. Set your ID: `/setid <YOUR_ID_NUMBER>`\n"
            "2. Add courses: `/addcourse <COURSE_CODE>` (e.g., `/addcourse CSOPESY`)\n"
            "   Or specific sections: `/addcourse <COURSE_CODE>:<CLASS_NBR>` (e.g., `/addcourse CSOPESY:1234`)\n"
            "Use /help for all commands."
        )
        logging.info("User %d subscribed", chat_id)
    else:
        await update.message.reply_text(
            "You are already subscribed. Use /help to see commands. 👍"
        )


async def cmd_stop(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stop command."""
    chat_id = update.effective_chat.id
    if SUBSCRIPTIONS.pop(chat_id, None) is not None:
        save_subscriptions()
        await update.message.reply_text(
            "Unsubscribed successfully. I will no longer send you updates. Bye! 👋"
        )
        logging.info("User %d unsubscribed", chat_id)
    else:
        await update.message.reply_text("You were not subscribed. 🤔")


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await update.message.reply_markdown(
        "*DLSU Course Monitor Bot Commands* 🤖\n\n"
        "`/start` - Subscribe to the bot & see welcome message 👋\n"
        "`/stop` - Unsubscribe from the bot 🚫\n"
        "`/setid <ID_NUMBER>` - Set your 8-digit student ID (required for checking courses) 🔑\n"
        "`/addcourse <COURSE>` - Track all sections of a course (e.g., `/addcourse LBYCPA1`) ➕\n"
        "`/addcourse <COURSE>:<CLASS_NBR>` - Track a specific section (e.g., `/addcourse CSOPESY:1234`) 🔍\n"
        "`/removecourse <COURSE or COURSE:CLASS_NBR>` - Stop tracking a course or section ➖\n"
        "`/course <COURSE>` - Show current status of all sections for a course *now* 📊\n"
        "`/check` - Manually trigger an update check for all your tracked items *now* 🔄\n"
        "`/prefs` - Show your current settings (ID, tracked courses/sections) ⚙️"
    )


async def cmd_setid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /setid command."""
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text(
            "Please provide your 8-digit student ID.\nUsage: `/setid <ID_NUMBER>` 🔢",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    student_id = ctx.args[0].strip()
    if not (student_id.isdigit() and len(student_id) == 8):
        await update.message.reply_text(
            "Invalid ID format. Please provide an 8-digit number. ❌"
        )
        return

    SUBSCRIPTIONS.setdefault(chat_id, copy.deepcopy(DEFAULT_PREFS))[
        "id_no"
    ] = student_id
    save_subscriptions()

    await update.message.reply_text(
        f"Student ID set to {student_id}. You can now add courses to track. ✅"
    )
    logging.info("User %d set ID to %s", chat_id, student_id)


async def cmd_prefs(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /prefs command."""
    chat_id = update.effective_chat.id
    prefs = SUBSCRIPTIONS.get(chat_id)

    if not prefs:
        await update.message.reply_text("You are not subscribed. Use /start first. 👈")
        return

    id_no = prefs.get("id_no") or "Not set"
    courses = prefs.get("courses", [])
    sections_dict = prefs.get("sections", {})

    lines = [f"*Your Settings* ⚙️"]
    lines.append(f"👤 Student ID: `{id_no}`")

    if courses:
        lines.append(f"📚 Tracking all sections of: {', '.join(sorted(courses))}")
    else:
        lines.append("📚 Tracking all sections of: None")

    if sections_dict:
        section_lines = []
        for course, numbers in sorted(sections_dict.items()):
            if numbers:
                section_lines.append(
                    f"  - {course}: {', '.join(map(str, sorted(numbers)))}"
                )
        if section_lines:
            lines.append("🔍 Tracking specific sections:")
            lines.extend(section_lines)
        else:
            lines.append("🔍 Tracking specific sections: None")
    else:
        lines.append("🔍 Tracking specific sections: None")

    await update.message.reply_markdown("\n".join(lines))


async def cmd_addcourse(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /addcourse command."""
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text(
            "Please specify what to track.\n"
            "Usage:\n"
            "  `/addcourse <COURSE>` (e.g., `/addcourse CSOPESY`)\n"
            "  `/addcourse <COURSE>:<CLASS>` (e.g., `/addcourse CSOPESY:1234`)",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    SUBSCRIPTIONS.setdefault(chat_id, copy.deepcopy(DEFAULT_PREFS))

    try:
        course, class_number = parse_course_arg(ctx.args[0])
    except ValueError as exc:
        await update.message.reply_text(f"Invalid format: {exc} ❌")
        return

    if class_number is None:
        user_courses = SUBSCRIPTIONS[chat_id].setdefault("courses", [])
        if course in user_courses:
            await update.message.reply_text(
                f"You are already tracking all sections of {course}. 🔄"
            )
        else:
            user_courses.append(course)
            user_courses.sort()
            await update.message.reply_text(
                f"OK. Added {course} to your tracked courses. I'll notify you of any changes. ✅"
            )
            logging.info("User %d added tracking for course %s", chat_id, course)
            save_subscriptions()
    else:
        user_sections = SUBSCRIPTIONS[chat_id].setdefault("sections", {})
        course_specific_sections = user_sections.setdefault(course, [])

        if class_number in course_specific_sections:
            await update.message.reply_text(
                f"You are already tracking section {class_number} of {course}. 🔄"
            )
        else:
            course_specific_sections.append(class_number)
            course_specific_sections.sort()
            await update.message.reply_text(
                f"OK. Added section {class_number} of {course} to your tracked sections. ✅"
            )
            logging.info(
                "User %d added tracking for %s:%d", chat_id, course, class_number
            )
            save_subscriptions()


async def cmd_removecourse(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /removecourse command."""
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text(
            "Please specify what to stop tracking.\n"
            "Usage:\n"
            "  `/removecourse <COURSE>`\n"
            "  `/removecourse <COURSE>:<CLASS>`",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    if chat_id not in SUBSCRIPTIONS:
        await update.message.reply_text("You are not subscribed. Use /start first. 👈")
        return

    try:
        course, class_number = parse_course_arg(ctx.args[0])
    except ValueError as exc:
        await update.message.reply_text(f"Invalid format: {exc} ❌")
        return

    removed = False
    prefs = SUBSCRIPTIONS[chat_id]
    prev_data = prefs.setdefault("previous_data", {})

    if class_number is None:
        user_courses = prefs.get("courses", [])
        if course in user_courses:
            user_courses.remove(course)
            prev_data.pop(course, None)
            await update.message.reply_text(
                f"Stopped tracking all sections of {course}. ✅"
            )
            logging.info("User %d removed tracking for course %s", chat_id, course)
            removed = True
        else:
            await update.message.reply_text(
                f"You were not tracking all sections of {course}. 🤔"
            )
    else:
        user_sections = prefs.get("sections", {})
        course_specific_sections = user_sections.get(course, [])

        if class_number in course_specific_sections:
            course_specific_sections.remove(class_number)
            if not course_specific_sections:
                user_sections.pop(course, None)
            if course not in user_sections:
                prev_data.pop(f"{course}:sections", None)

            await update.message.reply_text(
                f"Stopped tracking section {class_number} of {course}. ✅"
            )
            logging.info(
                "User %d removed tracking for %s:%d", chat_id, course, class_number
            )
            removed = True
        else:
            await update.message.reply_text(
                f"You were not tracking section {class_number} of {course}. 🤔"
            )

    if removed:
        save_subscriptions()


async def cmd_course(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /course command - show current status."""
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/course <COURSE>` 📚", parse_mode=constants.ParseMode.MARKDOWN
        )
        return

    course = ctx.args[0].upper().strip()
    prefs = SUBSCRIPTIONS.get(chat_id)

    if not prefs:
        await update.message.reply_text("You need to subscribe first. Use /start. 👈")
        return

    student_id = prefs.get("id_no")
    if not student_id:
        await update.message.reply_text(
            "Please set your student ID first using `/setid <ID_NUMBER>`. 🔑",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(f"Fetching current status for {course}... 🔄")
    tracking_info = TrackingInfo(
        chat_id=chat_id, student_id=student_id, course=course, track_all=True
    )
    await send_course_status(
        ctx, tracking_info, update=update
    )


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /check command - force update check."""
    chat_id = update.effective_chat.id
    prefs = SUBSCRIPTIONS.get(chat_id)

    if not prefs:
        await update.message.reply_text("You need to subscribe first. Use /start. 👈")
        return

    student_id = prefs.get("id_no")
    if not student_id:
        await update.message.reply_text(
            "Please set your student ID first using `/setid <ID_NUMBER>`. 🔑",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    courses_to_check = prefs.get("courses", [])
    sections_to_check = prefs.get("sections", {})

    if not courses_to_check and not sections_to_check:
        await update.message.reply_text(
            "You are not tracking any courses or sections yet. Use /addcourse to add some. ➕"
        )
        return

    await update.message.reply_text("Checking status for your tracked items now... 🔄")

    cloudflare_blocked = False

    for course in courses_to_check:
        tracking_info = TrackingInfo(
            chat_id=chat_id, student_id=student_id, course=course, track_all=True
        )
        try:
            await send_course_status(ctx, tracking_info, update=update)
        except CloudflareBlockedError:
            cloudflare_blocked = True
            break
        await asyncio.sleep(0.5)

    if not cloudflare_blocked:
        for course, class_numbers in sections_to_check.items():
            if class_numbers:
                tracking_info = TrackingInfo(
                    chat_id=chat_id,
                    student_id=student_id,
                    course=course,
                    track_all=False,
                    class_numbers=class_numbers,
                )
                try:
                    await send_course_status(ctx, tracking_info, update=update)
                except CloudflareBlockedError:
                    cloudflare_blocked = True
                    break
                await asyncio.sleep(0.5)

    if cloudflare_blocked:
        pass
    else:
        await update.message.reply_text("Finished checking all tracked items. ✅")


async def send_course_status(
    ctx: ContextTypes.DEFAULT_TYPE,
    tracking_info: TrackingInfo,
    update: Optional[Update] = None,
) -> None:
    """Fetches and sends the *current status* of a course/sections to the user."""
    try:
        sections = await _fetch_and_filter_data(tracking_info)
    except CloudflareBlockedError:
        await notify_cloudflare_block(ctx, tracking_info.chat_id, update=update)
        raise

    if sections is None:
        await ctx.bot.send_message(
            tracking_info.chat_id,
            f"❌ Error fetching data for {tracking_info.course}. Could not check status.",
        )
        return

    if not tracking_info.track_all:
        found_numbers = {s["classNbr"] for s in sections if "classNbr" in s}
        not_found = set(tracking_info.class_numbers) - found_numbers
        if not_found:
            await ctx.bot.send_message(
                tracking_info.chat_id,
                f"❌ Note: Section(s) {', '.join(map(str, sorted(not_found)))} "
                f"for {tracking_info.course} were not found in the latest data.",
            )

    if not sections:
        msg = f"No sections found matching your criteria for {tracking_info.course}. 🤷‍♂️"
        if not tracking_info.track_all:
            msg += f" (Sections: {', '.join(map(str, tracking_info.class_numbers))})"
        await ctx.bot.send_message(tracking_info.chat_id, msg)
        return

    suffix = (
        ""
        if tracking_info.track_all
        else f" (Sections: {', '.join(map(str, tracking_info.class_numbers))})"
    )
    text_lines = compose_status_lines(tracking_info.course, sections, suffix)
    await _send_long_message(
        ctx, tracking_info.chat_id, text_lines, title=f"{tracking_info.course}{suffix}"
    )


async def process_course_updates(
    ctx: ContextTypes.DEFAULT_TYPE,
    tracking_info: TrackingInfo,
) -> None:
    """Fetches, diffs, and notifies user *only if there are changes*."""
    prefs = SUBSCRIPTIONS.get(tracking_info.chat_id)
    if not prefs:
        return

    prev_data_map = prefs.setdefault("previous_data", {})
    data_key = tracking_info.get_data_key()
    previous_sections = prev_data_map.get(data_key, [])

    try:
        current_sections = await _fetch_and_filter_data(tracking_info)
    except CloudflareBlockedError:
        logging.warning(
            "Cloudflare block during background update for %s user %d",
            data_key,
            tracking_info.chat_id,
        )
        return
    except Exception as e:
        logging.error(
            "Failed background fetch for %s user %d: %s",
            data_key,
            tracking_info.chat_id,
            e,
        )
        return

    if current_sections is None:
        logging.warning(
            "Skipping update for %s user %d due to fetch failure.",
            data_key,
            tracking_info.chat_id,
        )
        return

    changes = diff_courses(previous_sections, current_sections)

    if any(changes.values()):
        logging.info("Changes detected for %s user %d", data_key, tracking_info.chat_id)
        lines: List[str] = []
        title = tracking_info.course
        if not tracking_info.track_all:
            title += f" (Sections: {', '.join(map(str, tracking_info.class_numbers))})"

        lines.append(f"*Updates for {title}* 🔔")

        if changes["added"]:
            lines.append("\n*✨ New sections added*")
            lines.extend(format_section(s) for s in changes["added"])

        if changes["removed"]:
            lines.append("\n*🗑️ Sections removed*")
            lines.extend(format_section(s) for s in changes["removed"])

        if changes["enrollment"]:
            lines.append("\n*📊 Enrollment changes*")
            sorted_enrollment_changes = sorted(
                changes["enrollment"],
                key=lambda c: (
                    c["section"].get("course", ""),
                    c["section"].get("section", ""),
                ),
            )
            for change in sorted_enrollment_changes:
                section = change["section"]
                old_enrl = change["old_enrolled"]
                new_enrl = change["new_enrolled"]
                cap = section.get("enrlCap", "?")
                delta = new_enrl - old_enrl
                emoji = "📈" if delta > 0 else "📉"
                lines.append(
                    f"{emoji} {section.get('course','?')} {section.get('section','?')} "
                    f"(Class {section.get('classNbr','?')}) "
                    f"`{old_enrl} ➡️ {new_enrl}` / {cap}"
                )

        await _send_long_message(
            ctx, tracking_info.chat_id, lines, title=f"Updates for {title}"
        )

        prev_data_map[data_key] = current_sections

    else:
        prev_data_map[data_key] = current_sections
        logging.debug(
            "No changes detected for %s user %d", data_key, tracking_info.chat_id
        )


async def broadcast_updates(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Background task: Check all tracked items for all users and notify of changes."""
    if not SUBSCRIPTIONS:
        logging.info("Broadcast: No subscribers to check.")
        return

    logging.info(
        "Broadcast: Starting scheduled update check for %d users.", len(SUBSCRIPTIONS)
    )
    start_time = asyncio.get_event_loop().time()

    all_tracking_infos: List[TrackingInfo] = []
    for chat_id, prefs in list(SUBSCRIPTIONS.items()):
        student_id = prefs.get("id_no")
        if not student_id:
            logging.debug("Broadcast: Skipping user %d - no ID set.", chat_id)
            continue

        for course in prefs.get("courses", []):
            all_tracking_infos.append(
                TrackingInfo(chat_id, student_id, course, track_all=True)
            )

        for course, class_numbers in prefs.get("sections", {}).items():
            if class_numbers:
                all_tracking_infos.append(
                    TrackingInfo(
                        chat_id,
                        student_id,
                        course,
                        track_all=False,
                        class_numbers=class_numbers,
                    )
                )

    if not all_tracking_infos:
        logging.info("Broadcast: No items being tracked by any user.")
        return

    logging.info("Broadcast: Processing %d tracking items.", len(all_tracking_infos))

    tasks = [process_course_updates(ctx, info) for info in all_tracking_infos]
    await asyncio.gather(*tasks)

    save_subscriptions()

    end_time = asyncio.get_event_loop().time()
    logging.info(
        "Broadcast: Finished update cycle in %.2f seconds.", end_time - start_time
    )


def main() -> None:
    """Main entry point for the bot."""
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.INFO)
    logging.getLogger("telegram.bot").setLevel(logging.INFO)

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    load_subscriptions()

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
    app.add_handlers(handlers)

    app.job_queue.run_repeating(
        broadcast_updates,
        interval=DEFAULT_POLLING_INTERVAL,
        first=10,
        name="periodic_update_check",
    )

    logging.info("Bot starting polling... 🚀")
    app.run_polling()
    logging.info("Bot stopped. 👋")


if __name__ == "__main__":
    main()
