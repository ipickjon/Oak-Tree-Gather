import calendar
import io
import json
import os
import re
import sqlite3
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlencode

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv


# ==================================================
# CONFIGURATION
# ==================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")
DEFAULT_TIMEZONE = os.getenv("GATHER_TIMEZONE", "America/Denver")
DATABASE = os.getenv("GATHER_DATABASE", "gather.db")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from the environment")

# DISCORD_GUILD_ID is optional in production. If present, it is treated as
# the legacy test guild so old guild-scoped commands can be cleaned up once.
TEST_GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
UTC = timezone.utc

REPEAT_LABELS = {
    "none": "Does not repeat",
    "weekly": "Weekly",
    "biweekly": "Every 2 weeks",
    "monthly": "Monthly",
}

# Public event cards keep recurrence out of the main body and show it in the
# footer, similar to Apollo: "Created by jon • Repeats weekly".
REPEAT_FOOTER_LABELS = {
    "weekly": "Repeats weekly",
    "biweekly": "Repeats every 2 weeks",
    "monthly": "Repeats monthly",
}


# ==================================================
# DATE / TIME HELPERS
# ==================================================

DATE_FORMATS = (
    "%m/%d/%Y",
    "%Y-%m-%d",
    "%m/%d/%y",
)

DATETIME_FORMATS = (
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %I %p",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%d %I:%M %p",
    "%Y-%m-%d %I %p",
    "%Y-%m-%d %H:%M",
)

TIME_FORMATS = (
    "%I:%M %p",
    "%I %p",
    "%H:%M",
)


def get_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"Unknown timezone '{tz_name}'. Example: America/Denver"
        ) from exc


def parse_date_text(date_text: str):
    value = date_text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError("Date must look like 09/25/2026 or 2026-09-25.")


MONTH_ABBR = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sept", 10: "Oct", 11: "Nov", 12: "Dec",
}


def format_event_date(date_text: str) -> str:
    """Return a compact friendly date such as 'Thursday, Sept 24, 2026'."""
    try:
        value = parse_date_text(date_text)
    except ValueError:
        # Preserve legacy/free-form data instead of breaking old event cards.
        return date_text
    return f"{value.strftime('%A')}, {MONTH_ABBR[value.month]} {value.day}, {value.year}"


def format_display_datetime(dt: datetime | None, tz_name: str) -> str:
    """Compact local datetime, e.g. 'Thursday, Sept 24, 2026 4:00 PM'."""
    if dt is None:
        return "Not set"
    local_dt = dt.astimezone(get_zone(tz_name))
    hour = local_dt.strftime("%I").lstrip("0") or "0"
    return (
        f"{local_dt.strftime('%A')}, {MONTH_ABBR[local_dt.month]} {local_dt.day}, "
        f"{local_dt.year} {hour}:{local_dt.strftime('%M')} {local_dt.strftime('%p')}"
    )


def extract_start_time_text(time_text: str) -> str:
    # Supports "6:30 PM", "6:30 PM - 10:00 PM", en/em dashes, and "to".
    parts = re.split(r"\s+(?:-|–|—|to)\s+", time_text.strip(), maxsplit=1, flags=re.I)
    return parts[0].strip()


def parse_time_text(time_text: str):
    value = extract_start_time_text(time_text)
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            pass
    raise ValueError("Start time must look like 6:30 PM or 18:30.")


def parse_event_start(date_text: str, time_text: str, tz_name: str) -> datetime:
    zone = get_zone(tz_name)
    event_date = parse_date_text(date_text)
    event_time = parse_time_text(time_text)
    local_dt = datetime.combine(event_date, event_time, tzinfo=zone)
    return local_dt.astimezone(UTC)


def parse_local_datetime(value: str, tz_name: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None

    zone = get_zone(tz_name)
    for fmt in DATETIME_FORMATS:
        try:
            naive = datetime.strptime(value, fmt)
            return naive.replace(tzinfo=zone).astimezone(UTC)
        except ValueError:
            pass

    raise ValueError(
        "Cutoff must look like 09/24/2026 6:00 PM or 2026-09-24 18:00."
    )


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def discord_timestamp(dt: datetime | None, style: str = "F") -> str:
    if dt is None:
        return "—"
    return f"<t:{int(dt.timestamp())}:{style}>"


def format_local_cutoff(dt: datetime | None, tz_name: str) -> str:
    if dt is None:
        return "Not set"
    local_dt = dt.astimezone(get_zone(tz_name))
    return local_dt.strftime("%m/%d/%Y %I:%M %p").replace(" 0", " ")


def recurring_cutoff_text(start_at: datetime | None, cutoff_at: datetime | None, tz_name: str) -> str:
    """Describe a recurring cutoff as a calendar-day rule, e.g. 1 day before at 9:00 PM."""
    if cutoff_at is None:
        return "Not set"
    if start_at is None:
        return format_local_cutoff(cutoff_at, tz_name)
    zone = get_zone(tz_name)
    start_local = start_at.astimezone(zone)
    cutoff_local = cutoff_at.astimezone(zone)
    days = (start_local.date() - cutoff_local.date()).days
    if days == 0:
        day_text = "Same day"
    elif days == 1:
        day_text = "1 day before"
    else:
        day_text = f"{days} days before"
    time_text = cutoff_local.strftime("%I:%M %p").lstrip("0")
    return f"{day_text} at {time_text}"


def recurring_cutoff_from_parts(start_at: datetime, days_before: int | None, clock_text: str | None, tz_name: str) -> datetime | None:
    """Create the first cutoff from a day-before rule and local clock time."""
    if days_before is None:
        return None
    if not clock_text:
        raise ValueError("Enter a cutoff time, or choose No cutoff.")
    zone = get_zone(tz_name)
    local_start = start_at.astimezone(zone)
    cutoff_date = local_start.date() - timedelta(days=days_before)
    cutoff_time = parse_time_text(clock_text)
    return datetime.combine(cutoff_date, cutoff_time, tzinfo=zone).astimezone(UTC)


def recurrence_next_local(latest_utc: datetime, rule: str, tz_name: str, anchor_day: int) -> datetime:
    zone = get_zone(tz_name)
    latest_local = latest_utc.astimezone(zone)

    if rule == "weekly":
        return latest_local + timedelta(days=7)

    if rule == "biweekly":
        return latest_local + timedelta(days=14)

    if rule == "monthly":
        year = latest_local.year
        month = latest_local.month + 1
        if month == 13:
            year += 1
            month = 1

        day = min(anchor_day, calendar.monthrange(year, month)[1])
        return latest_local.replace(year=year, month=month, day=day)

    raise ValueError(f"Unsupported repeat rule: {rule}")


# ==================================================
# DATABASE
# ==================================================


def get_db():
    db = sqlite3.connect(DATABASE, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 10000")
    return db


def table_columns(db, table_name: str) -> set[str]:
    return {
        row["name"]
        for row in db.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def ensure_column(db, table_name: str, column_name: str, ddl: str):
    if column_name not in table_columns(db, table_name):
        print(f"Database migration: adding {table_name}.{column_name}")
        db.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")


def initialize_database():
    with get_db() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS event_series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                creator_name TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                date_text TEXT NOT NULL,
                time_text TEXT NOT NULL,
                repeat_rule TEXT NOT NULL,
                location_mode TEXT NOT NULL,
                image_url TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS event_instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                message_id INTEGER,
                date_text TEXT NOT NULL,
                selected_location_id INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                FOREIGN KEY(series_id) REFERENCES event_series(id) ON DELETE CASCADE
            )
            """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS location_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                position INTEGER NOT NULL,
                FOREIGN KEY(series_id) REFERENCES event_series(id) ON DELETE CASCADE
            )
            """
        )

        # Saved location book. Addresses stay out of public event UI until
        # a location is finalized.
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                address TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, name COLLATE NOCASE)
            )
            """
        )

        # Location options snapshot the address so old events/templates keep
        # working even if a saved place is later edited or deleted.
        ensure_column(db, "location_options", "address", "address TEXT")
        ensure_column(db, "location_options", "saved_location_id", "saved_location_id INTEGER")

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS location_votes (
                instance_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                location_id INTEGER NOT NULL,
                PRIMARY KEY (instance_id, user_id, location_id),
                FOREIGN KEY(instance_id) REFERENCES event_instances(id) ON DELETE CASCADE,
                FOREIGN KEY(location_id) REFERENCES location_options(id) ON DELETE CASCADE
            )
            """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS signup_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                selection_type TEXT NOT NULL,
                position INTEGER NOT NULL,
                FOREIGN KEY(series_id) REFERENCES event_series(id) ON DELETE CASCADE
            )
            """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS signup_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                position INTEGER NOT NULL,
                FOREIGN KEY(group_id) REFERENCES signup_groups(id) ON DELETE CASCADE
            )
            """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS signup_responses (
                instance_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                option_id INTEGER NOT NULL,
                PRIMARY KEY (instance_id, user_id, option_id),
                FOREIGN KEY(instance_id) REFERENCES event_instances(id) ON DELETE CASCADE,
                FOREIGN KEY(option_id) REFERENCES signup_options(id) ON DELETE CASCADE
            )
            """
        )

        # event_series migrations
        ensure_column(db, "event_series", "image_url", "image_url TEXT")
        ensure_column(db, "event_series", "image_blob", "image_blob BLOB")
        ensure_column(db, "event_series", "image_filename", "image_filename TEXT")
        ensure_column(db, "event_series", "timezone", f"timezone TEXT NOT NULL DEFAULT '{DEFAULT_TIMEZONE}'")
        ensure_column(db, "event_series", "start_time_local", "start_time_local TEXT")
        ensure_column(db, "event_series", "anchor_day", "anchor_day INTEGER")
        ensure_column(db, "event_series", "ping_role_id", "ping_role_id INTEGER")
        ensure_column(db, "event_series", "ping_role_name", "ping_role_name TEXT")
        ensure_column(db, "event_series", "vote_cutoff_offset_minutes", "vote_cutoff_offset_minutes INTEGER")
        ensure_column(db, "event_series", "rsvp_cutoff_offset_minutes", "rsvp_cutoff_offset_minutes INTEGER")
        ensure_column(db, "event_series", "post_days_before", "post_days_before INTEGER NOT NULL DEFAULT 5")
        ensure_column(db, "event_series", "reminder_minutes", "reminder_minutes INTEGER NOT NULL DEFAULT 120")
        ensure_column(db, "event_series", "active", "active INTEGER NOT NULL DEFAULT 1")
        ensure_column(db, "event_series", "thread_enabled", "thread_enabled INTEGER NOT NULL DEFAULT 1")
        ensure_column(db, "event_series", "capacity", "capacity INTEGER")
        # Phase 3 advanced voting settings.
        ensure_column(db, "event_series", "vote_type", "vote_type TEXT NOT NULL DEFAULT 'single'")
        ensure_column(db, "event_series", "vote_visibility", "vote_visibility TEXT NOT NULL DEFAULT 'public'")

        # Phase 3: older databases used one location vote per user via
        # PRIMARY KEY(instance_id, user_id). Rebuild once so multiple-choice
        # voting can store one row per selected location. Existing votes survive.
        vote_table_sql_row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='location_votes'"
        ).fetchone()
        vote_table_sql = (vote_table_sql_row["sql"] if vote_table_sql_row else "") or ""
        normalized_vote_sql = "".join(vote_table_sql.lower().split())
        if "primarykey(instance_id,user_id,location_id)" not in normalized_vote_sql:
            print("Database migration: upgrading location_votes for multi-choice voting")
            db.execute("ALTER TABLE location_votes RENAME TO location_votes_phase2_backup")
            db.execute(
                """
                CREATE TABLE location_votes (
                    instance_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    location_id INTEGER NOT NULL,
                    PRIMARY KEY (instance_id, user_id, location_id),
                    FOREIGN KEY(instance_id) REFERENCES event_instances(id) ON DELETE CASCADE,
                    FOREIGN KEY(location_id) REFERENCES location_options(id) ON DELETE CASCADE
                )
                """
            )
            db.execute(
                """
                INSERT OR IGNORE INTO location_votes(instance_id,user_id,user_name,location_id)
                SELECT instance_id,user_id,user_name,location_id FROM location_votes_phase2_backup
                """
            )
            db.execute("DROP TABLE location_votes_phase2_backup")

        # event_instances migrations
        ensure_column(db, "event_instances", "occurrence_start_at", "occurrence_start_at TEXT")
        ensure_column(db, "event_instances", "vote_cutoff_at", "vote_cutoff_at TEXT")
        ensure_column(db, "event_instances", "rsvp_cutoff_at", "rsvp_cutoff_at TEXT")
        ensure_column(db, "event_instances", "location_finalized", "location_finalized INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "event_instances", "vote_reminder_sent", "vote_reminder_sent INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "event_instances", "rsvp_reminder_sent", "rsvp_reminder_sent INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "event_instances", "vote_closed_announced", "vote_closed_announced INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "event_instances", "rsvp_closed_announced", "rsvp_closed_announced INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "event_instances", "created_at", "created_at TEXT")
        ensure_column(db, "event_instances", "thread_id", "thread_id INTEGER")
        ensure_column(db, "event_instances", "thread_archived", "thread_archived INTEGER NOT NULL DEFAULT 0")

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS signup_response_details (
                instance_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                option_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (instance_id, user_id, option_id),
                FOREIGN KEY(instance_id) REFERENCES event_instances(id) ON DELETE CASCADE,
                FOREIGN KEY(option_id) REFERENCES signup_options(id) ON DELETE CASCADE
            )
            """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS event_waitlist (
                instance_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (instance_id, user_id),
                FOREIGN KEY(instance_id) REFERENCES event_instances(id) ON DELETE CASCADE
            )
            """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS event_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                source_series_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, name COLLATE NOCASE),
                FOREIGN KEY(source_series_id) REFERENCES event_series(id) ON DELETE CASCADE
            )
            """
        )

        # Best-effort backfill for older test events.
        old_rows = db.execute(
            """
            SELECT i.id, i.date_text, s.time_text, s.timezone, s.anchor_day, s.start_time_local
            FROM event_instances i
            JOIN event_series s ON s.id = i.series_id
            WHERE i.occurrence_start_at IS NULL
            """
        ).fetchall()

        for row in old_rows:
            try:
                tz_name = row["timezone"] or DEFAULT_TIMEZONE
                start_at = parse_event_start(row["date_text"], row["time_text"], tz_name)
                local_start = start_at.astimezone(get_zone(tz_name))
                db.execute(
                    "UPDATE event_instances SET occurrence_start_at = ? WHERE id = ?",
                    (iso_utc(start_at), row["id"]),
                )
                if not row["anchor_day"]:
                    db.execute(
                        "UPDATE event_series SET anchor_day = ?, start_time_local = ? WHERE id = (SELECT series_id FROM event_instances WHERE id = ?)",
                        (local_start.day, local_start.strftime("%H:%M"), row["id"]),
                    )
            except Exception:
                # Old scratch events may have intentionally invalid test dates/times.
                pass


# ==================================================
# DATABASE HELPERS
# ==================================================


def get_event(instance_id: int):
    with get_db() as db:
        return db.execute(
            """
            SELECT
                i.id AS instance_id,
                i.series_id,
                i.message_id,
                i.date_text,
                i.selected_location_id,
                i.status,
                i.occurrence_start_at,
                i.vote_cutoff_at,
                i.rsvp_cutoff_at,
                i.location_finalized,
                i.vote_reminder_sent,
                i.rsvp_reminder_sent,
                i.vote_closed_announced,
                i.rsvp_closed_announced,
                i.thread_id,
                i.thread_archived,
                s.guild_id,
                s.channel_id,
                s.creator_id,
                s.creator_name,
                s.title,
                s.description,
                s.time_text,
                s.repeat_rule,
                s.location_mode,
                s.vote_type,
                s.vote_visibility,
                s.image_url,
                s.image_blob,
                s.image_filename,
                s.timezone,
                s.start_time_local,
                s.anchor_day,
                s.ping_role_id,
                s.ping_role_name,
                s.vote_cutoff_offset_minutes,
                s.rsvp_cutoff_offset_minutes,
                s.post_days_before,
                s.reminder_minutes,
                s.thread_enabled,
                s.capacity,
                s.active AS series_active
            FROM event_instances i
            JOIN event_series s ON s.id = i.series_id
            WHERE i.id = ?
            """,
            (instance_id,),
        ).fetchone()


def get_series(series_id: int):
    with get_db() as db:
        return db.execute("SELECT * FROM event_series WHERE id = ?", (series_id,)).fetchone()


def get_locations(series_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT id, label, position, address, saved_location_id FROM location_options WHERE series_id = ? ORDER BY position",
            (series_id,),
        ).fetchall()


def get_location(series_id: int, location_id: int | None):
    if not location_id:
        return None
    with get_db() as db:
        return db.execute(
            "SELECT id, label, address, saved_location_id FROM location_options WHERE series_id = ? AND id = ?",
            (series_id, location_id),
        ).fetchone()


def get_saved_locations(guild_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT id, guild_id, creator_id, name, address FROM saved_locations WHERE guild_id=? ORDER BY name COLLATE NOCASE",
            (guild_id,),
        ).fetchall()


def get_saved_location(location_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT id, guild_id, creator_id, name, address FROM saved_locations WHERE id=?",
            (location_id,),
        ).fetchone()


def save_saved_location(guild_id: int, creator_id: int, name: str, address: str | None):
    clean_name = name.strip()
    clean_address = (address or "").strip() or None
    if not clean_name:
        raise ValueError("Location name is required.")
    with get_db() as db:
        existing = db.execute(
            "SELECT id FROM saved_locations WHERE guild_id=? AND name=? COLLATE NOCASE",
            (guild_id, clean_name),
        ).fetchone()
        if existing:
            raise ValueError(
                "A saved location with that name already exists. Use /event locations to edit it."
            )
        cur = db.execute(
            "INSERT INTO saved_locations(guild_id,creator_id,name,address) VALUES(?,?,?,?)",
            (guild_id, creator_id, clean_name, clean_address),
        )
        return cur.lastrowid


def update_saved_location(location_id: int, name: str, address: str | None):
    clean_name = name.strip()
    clean_address = (address or "").strip() or None
    if not clean_name:
        raise ValueError("Location name is required.")
    with get_db() as db:
        row = db.execute("SELECT guild_id FROM saved_locations WHERE id=?", (location_id,)).fetchone()
        if not row:
            raise ValueError("Saved location not found.")
        duplicate = db.execute(
            "SELECT id FROM saved_locations WHERE guild_id=? AND name=? COLLATE NOCASE AND id<>?",
            (row["guild_id"], clean_name, location_id),
        ).fetchone()
        if duplicate:
            raise ValueError("A saved location with that name already exists.")
        db.execute(
            "UPDATE saved_locations SET name=?, address=? WHERE id=?",
            (clean_name, clean_address, location_id),
        )


def delete_saved_location(location_id: int):
    with get_db() as db:
        db.execute("DELETE FROM saved_locations WHERE id=?", (location_id,))


def google_maps_directions_url(address: str | None) -> str | None:
    if not address:
        return None
    return "https://www.google.com/maps/dir/?" + urlencode(
        {"api": "1", "destination": address}
    )


def get_signup_groups(series_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT id, name, selection_type, position FROM signup_groups WHERE series_id = ? ORDER BY position",
            (series_id,),
        ).fetchall()


def get_signup_options(group_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT id, label, position FROM signup_options WHERE group_id = ? ORDER BY position",
            (group_id,),
        ).fetchall()


def get_all_signup_options(series_id: int):
    with get_db() as db:
        return db.execute(
            """
            SELECT o.id, o.label, o.position, g.id AS group_id, g.name AS group_name, g.selection_type
            FROM signup_options o
            JOIN signup_groups g ON g.id = o.group_id
            WHERE g.series_id = ?
            ORDER BY g.position, o.position
            """,
            (series_id,),
        ).fetchall()


def get_option_people(instance_id: int, option_id: int):
    with get_db() as db:
        return db.execute(
            """
            SELECT user_id, user_name
            FROM signup_responses
            WHERE instance_id = ? AND option_id = ?
            ORDER BY user_name COLLATE NOCASE
            """,
            (instance_id, option_id),
        ).fetchall()


def get_option_user_ids(instance_id: int, option_id: int) -> set[int]:
    return {row["user_id"] for row in get_option_people(instance_id, option_id)}


def get_option_count(instance_id: int, option_id: int) -> int:
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) AS total FROM signup_responses WHERE instance_id = ? AND option_id = ?",
            (instance_id, option_id),
        ).fetchone()
        return int(row["total"])


def get_response_quantity(instance_id: int, user_id: int, option_id: int, default: int = 1) -> int:
    with get_db() as db:
        row = db.execute(
            "SELECT quantity FROM signup_response_details WHERE instance_id=? AND user_id=? AND option_id=?",
            (instance_id, user_id, option_id),
        ).fetchone()
    return int(row["quantity"]) if row else default


def set_response_quantity(instance_id: int, user_id: int, option_id: int, quantity: int):
    with get_db() as db:
        if quantity <= 0:
            db.execute(
                "DELETE FROM signup_response_details WHERE instance_id=? AND user_id=? AND option_id=?",
                (instance_id, user_id, option_id),
            )
        else:
            db.execute(
                """INSERT INTO signup_response_details(instance_id,user_id,option_id,quantity)
                   VALUES(?,?,?,?)
                   ON CONFLICT(instance_id,user_id,option_id) DO UPDATE SET quantity=excluded.quantity""",
                (instance_id, user_id, option_id, quantity),
            )


def get_semantic_option(series_id: int, semantic: str):
    for option in get_all_signup_options(series_id):
        if option_semantic(option["label"]) == semantic:
            return option
    return None


def get_waitlist(instance_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT user_id,user_name,created_at FROM event_waitlist WHERE instance_id=? ORDER BY created_at, rowid",
            (instance_id,),
        ).fetchall()


def remove_from_waitlist(instance_id: int, user_id: int):
    with get_db() as db:
        db.execute("DELETE FROM event_waitlist WHERE instance_id=? AND user_id=?", (instance_id, user_id))


def guest_quantity_for_user(instance_id: int, series_id: int, user_id: int) -> int:
    option = get_semantic_option(series_id, "guest")
    if not option:
        return 0
    with get_db() as db:
        active = db.execute(
            "SELECT 1 FROM signup_responses WHERE instance_id=? AND user_id=? AND option_id=?",
            (instance_id, user_id, option["id"]),
        ).fetchone()
    if not active:
        return 0
    return get_response_quantity(instance_id, user_id, option["id"], 1)


def occupied_capacity(instance_id: int, series_id: int) -> int:
    going = get_semantic_option(series_id, "going")
    if not going:
        return 0
    users = get_option_user_ids(instance_id, going["id"])
    return sum(1 + guest_quantity_for_user(instance_id, series_id, uid) for uid in users)


def available_capacity(instance_id: int, series_id: int, capacity: int | None) -> int | None:
    if not capacity or capacity <= 0:
        return None
    return max(0, int(capacity) - occupied_capacity(instance_id, series_id))


def promote_waitlist(instance_id: int):
    event = get_event(instance_id)
    if not event or not event["capacity"]:
        return []
    going = get_semantic_option(event["series_id"], "going")
    if not going:
        return []
    promoted = []
    for row in get_waitlist(instance_id):
        needed = 1 + guest_quantity_for_user(instance_id, event["series_id"], row["user_id"])
        available = available_capacity(instance_id, event["series_id"], event["capacity"])
        if available is None or needed <= available:
            with get_db() as db:
                # Clear any other single-select attendance response first.
                group_options = db.execute(
                    "SELECT id FROM signup_options WHERE group_id=?", (going["group_id"],)
                ).fetchall()
                ids = [r["id"] for r in group_options]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    db.execute(
                        f"DELETE FROM signup_responses WHERE instance_id=? AND user_id=? AND option_id IN ({placeholders})",
                        [instance_id, row["user_id"], *ids],
                    )
                db.execute(
                    "INSERT OR IGNORE INTO signup_responses(instance_id,user_id,user_name,option_id) VALUES(?,?,?,?)",
                    (instance_id, row["user_id"], row["user_name"], going["id"]),
                )
                db.execute("DELETE FROM event_waitlist WHERE instance_id=? AND user_id=?", (instance_id, row["user_id"]))
            promoted.append(row["user_name"])
        else:
            # FIFO: if the first person cannot fit with their guests, leave them first.
            break
    return promoted


def get_location_vote_counts(instance_id: int):
    with get_db() as db:
        return db.execute(
            """
            SELECT
                l.id,
                l.label,
                l.position,
                COUNT(v.user_id) AS vote_count
            FROM location_options l
            JOIN event_instances i ON i.series_id = l.series_id
            LEFT JOIN location_votes v
                ON v.location_id = l.id
               AND v.instance_id = i.id
            WHERE i.id = ?
            GROUP BY l.id, l.label, l.position
            ORDER BY l.position
            """,
            (instance_id,),
        ).fetchall()


def get_location_voters(instance_id: int, location_id: int):
    with get_db() as db:
        return db.execute(
            """
            SELECT user_id, user_name
            FROM location_votes
            WHERE instance_id = ? AND location_id = ?
            ORDER BY user_name COLLATE NOCASE
            """,
            (instance_id, location_id),
        ).fetchall()


def safe_field_value(value: str) -> str:
    if len(value) <= 1024:
        return value
    return value[:1021] + "..."


def parse_options(text: str | None) -> list[str]:
    if not text:
        return []
    output = []
    seen = set()
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            continue
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            output.append(item)
    return output


# ==================================================
# SEMANTIC OPTION HELPERS / SUMMARY
# ==================================================


def label_lower(label: str) -> str:
    return label.casefold()


def option_semantic(label: str) -> str | None:
    lower = label_lower(label)
    if ("can't" in lower or "cannot" in lower or "not going" in lower or "decline" in lower):
        return "not_going"
    if "tentative" in lower or "maybe" in lower:
        return "tentative"
    if "going" in lower or "attending" in lower or "accepted" in lower:
        return "going"
    if "need a ride" in lower or "need ride" in lower or "ride needed" in lower:
        return "need_ride"
    if "driv" in lower:
        return "driving"
    if "+1" in lower or "plus one" in lower or "guest" in lower:
        return "guest"
    if "eat" in lower or "food" in lower or "meal" in lower:
        return "eating"
    return None


def summary_counts(instance_id: int, series_id: int) -> dict[str, int | bool]:
    semantic_options: dict[str, list[int]] = {}
    option_rows = get_all_signup_options(series_id)
    for option in option_rows:
        semantic = option_semantic(option["label"])
        if semantic:
            semantic_options.setdefault(semantic, []).append(option["id"])

    semantic_users: dict[str, set[int]] = {}
    for semantic, option_ids in semantic_options.items():
        users: set[int] = set()
        for option_id in option_ids:
            users |= get_option_user_ids(instance_id, option_id)
        semantic_users[semantic] = users

    going_users = semantic_users.get("going", set())
    tentative_users = semantic_users.get("tentative", set())
    not_going_users = semantic_users.get("not_going", set())
    guest_users = semantic_users.get("guest", set())
    eating_users = semantic_users.get("eating", set())
    driving_users = semantic_users.get("driving", set())
    need_ride_users = semantic_users.get("need_ride", set())

    guest_option_ids = semantic_options.get("guest", [])
    driving_option_ids = semantic_options.get("driving", [])

    guest_total = 0
    eating_guest_total = 0
    for user_id in guest_users:
        qty = 1
        for option_id in guest_option_ids:
            if user_id in get_option_user_ids(instance_id, option_id):
                qty = get_response_quantity(instance_id, user_id, option_id, 1)
                break
        if user_id in going_users:
            guest_total += qty
        if user_id in eating_users:
            eating_guest_total += qty

    available_seats = 0
    for user_id in driving_users:
        seats = 1
        for option_id in driving_option_ids:
            if user_id in get_option_user_ids(instance_id, option_id):
                seats = get_response_quantity(instance_id, user_id, option_id, 1)
                break
        available_seats += seats

    return {
        "going": len(going_users),
        "tentative": len(tentative_users),
        "not_going": len(not_going_users),
        "guests": guest_total,
        "expected": len(going_users) + guest_total,
        "eating": len(eating_users) + eating_guest_total,
        "drivers": len(driving_users),
        "available_seats": available_seats,
        "need_ride": len(need_ride_users),
        "waitlist": len(get_waitlist(instance_id)),
        "has_not_going": "not_going" in semantic_options,
        "has_guest": "guest" in semantic_options,
        "has_eating": "eating" in semantic_options,
        "has_driving": "driving" in semantic_options,
        "has_need_ride": "need_ride" in semantic_options,
    }



def parse_event_end(date_text: str, time_text: str, tz_name: str) -> datetime:
    start = parse_event_start(date_text, time_text, tz_name)
    parts = re.split(r"\s*(?:-|–|—|to)\s*", time_text.strip(), maxsplit=1, flags=re.IGNORECASE)
    if len(parts) < 2:
        return start + timedelta(hours=3)
    end_text = parts[1].strip()
    for fmt in TIME_FORMATS:
        try:
            end_t = datetime.strptime(end_text.upper(), fmt).time()
            local_start = start.astimezone(get_zone(tz_name))
            local_end = datetime.combine(local_start.date(), end_t, tzinfo=get_zone(tz_name))
            if local_end <= local_start:
                local_end += timedelta(days=1)
            return local_end.astimezone(UTC)
        except ValueError:
            continue
    return start + timedelta(hours=3)


def build_ics(event) -> bytes:
    start = from_iso(event["occurrence_start_at"]) or parse_event_start(
        event["date_text"], event["time_text"], event["timezone"] or DEFAULT_TIMEZONE
    )
    end = parse_event_end(event["date_text"], event["time_text"], event["timezone"] or DEFAULT_TIMEZONE)
    location = get_location(event["series_id"], event["selected_location_id"])
    def esc(v: str) -> str:
        return (v or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
    uid = f"gather-{event['instance_id']}@oak-tree-gather"
    text = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Oak Tree Gather//EN", "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}", f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{esc(event['title'])}", f"DESCRIPTION:{esc(event['description'] or '')}",
        f"LOCATION:{esc(location['label'] if location else '')}", "END:VEVENT", "END:VCALENDAR", ""
    ])
    return text.encode("utf-8")


def get_templates(guild_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT id, name, source_series_id, creator_id FROM event_templates WHERE guild_id = ? ORDER BY name COLLATE NOCASE",
            (guild_id,),
        ).fetchall()


def get_template(template_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT id, guild_id, creator_id, name, source_series_id FROM event_templates WHERE id = ?",
            (template_id,),
        ).fetchone()


def rename_template(template_id: int, new_name: str):
    clean_name = new_name.strip()
    if not clean_name:
        raise ValueError("Template name cannot be blank.")
    with get_db() as db:
        template = db.execute("SELECT guild_id FROM event_templates WHERE id=?", (template_id,)).fetchone()
        if not template:
            raise ValueError("Template not found.")
        existing = db.execute(
            "SELECT id FROM event_templates WHERE guild_id=? AND name=? COLLATE NOCASE AND id<>?",
            (template["guild_id"], clean_name, template_id),
        ).fetchone()
        if existing:
            raise ValueError("A template with that name already exists.")
        db.execute("UPDATE event_templates SET name=? WHERE id=?", (clean_name, template_id))


def update_template_source(template_id: int, source_series_id: int):
    with get_db() as db:
        db.execute(
            "UPDATE event_templates SET source_series_id=? WHERE id=?",
            (source_series_id, template_id),
        )


def delete_template(template_id: int):
    with get_db() as db:
        db.execute("DELETE FROM event_templates WHERE id=?", (template_id,))


def save_template(guild_id: int, creator_id: int, name: str, source_series_id: int):
    clean_name = name.strip()
    with get_db() as db:
        db.execute("DELETE FROM event_templates WHERE guild_id=? AND name=? COLLATE NOCASE", (guild_id, clean_name))
        db.execute(
            "INSERT INTO event_templates (guild_id, creator_id, name, source_series_id) VALUES (?, ?, ?, ?)",
            (guild_id, creator_id, clean_name, source_series_id),
        )


def copy_series_configuration(source_series_id: int, *, guild_id: int, channel_id: int, creator_id: int,
                              creator_name: str, title: str, description: str, date_text: str,
                              time_text: str, repeat_rule: str = "none") -> int:
    src = get_series(source_series_id)
    if not src:
        raise RuntimeError("Source event series not found.")
    start = parse_event_start(date_text, time_text, src["timezone"] or DEFAULT_TIMEZONE)
    local_start = start.astimezone(get_zone(src["timezone"] or DEFAULT_TIMEZONE))
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO event_series (
                guild_id, channel_id, creator_id, creator_name, title, description, date_text, time_text,
                repeat_rule, location_mode, vote_type, vote_visibility, image_url, image_blob, image_filename, timezone, start_time_local,
                anchor_day, ping_role_id, ping_role_name, vote_cutoff_offset_minutes, rsvp_cutoff_offset_minutes,
                post_days_before, reminder_minutes, active, thread_enabled, capacity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (guild_id, channel_id, creator_id, creator_name, title, description, date_text, time_text,
             repeat_rule, src["location_mode"], src["vote_type"] if "vote_type" in src.keys() else "single",
             src["vote_visibility"] if "vote_visibility" in src.keys() else "public",
             src["image_url"], src["image_blob"], src["image_filename"],
             src["timezone"], local_start.strftime("%H:%M"), local_start.day, src["ping_role_id"], src["ping_role_name"],
             src["vote_cutoff_offset_minutes"], src["rsvp_cutoff_offset_minutes"], src["post_days_before"],
             src["reminder_minutes"], src["thread_enabled"] if "thread_enabled" in src.keys() else 1,
             src["capacity"] if "capacity" in src.keys() else None)
        )
        new_series = cur.lastrowid
        locs = db.execute(
            "SELECT label, position, address, saved_location_id FROM location_options WHERE series_id=? ORDER BY position",
            (source_series_id,),
        ).fetchall()
        for loc in locs:
            db.execute(
                "INSERT INTO location_options(series_id,label,position,address,saved_location_id) VALUES(?,?,?,?,?)",
                (new_series, loc["label"], loc["position"], loc["address"], loc["saved_location_id"]),
            )
        groups = db.execute("SELECT id,name,selection_type,position FROM signup_groups WHERE series_id=? ORDER BY position", (source_series_id,)).fetchall()
        for group in groups:
            gc = db.execute("INSERT INTO signup_groups(series_id,name,selection_type,position) VALUES(?,?,?,?)",
                            (new_series, group["name"], group["selection_type"], group["position"]))
            new_gid = gc.lastrowid
            opts = db.execute("SELECT label,position FROM signup_options WHERE group_id=? ORDER BY position", (group["id"],)).fetchall()
            for opt in opts:
                db.execute("INSERT INTO signup_options(group_id,label,position) VALUES(?,?,?)", (new_gid,opt["label"],opt["position"]))
    return new_series


def get_manageable_instances(guild_id: int, user_id: int, can_manage: bool):
    now_iso = iso_utc(datetime.now(UTC) - timedelta(days=1))
    with get_db() as db:
        if can_manage:
            return db.execute(
                """SELECT i.id, i.date_text, s.title FROM event_instances i JOIN event_series s ON s.id=i.series_id
                   WHERE s.guild_id=? AND i.status='active' AND (i.occurrence_start_at IS NULL OR i.occurrence_start_at>=?)
                   ORDER BY i.occurrence_start_at LIMIT 25""", (guild_id, now_iso)
            ).fetchall()
        return db.execute(
            """SELECT i.id, i.date_text, s.title FROM event_instances i JOIN event_series s ON s.id=i.series_id
               WHERE s.guild_id=? AND s.creator_id=? AND i.status='active' AND (i.occurrence_start_at IS NULL OR i.occurrence_start_at>=?)
               ORDER BY i.occurrence_start_at LIMIT 25""", (guild_id, user_id, now_iso)
        ).fetchall()

# ==================================================
# EMBED BUILDING
# ==================================================


def build_embed(instance_id: int) -> discord.Embed | None:
    event = get_event(instance_id)
    if event is None:
        return None

    cancelled = event["status"] == "cancelled"
    color = discord.Color.red() if cancelled else discord.Color.blurple()
    vote_cutoff = from_iso(event["vote_cutoff_at"])
    rsvp_cutoff = from_iso(event["rsvp_cutoff_at"])

    description_lines = []
    if event["description"]:
        description_lines.append(event["description"])
    if event["location_mode"] == "vote" and vote_cutoff:
        description_lines.append(
            f"🗳️ **Voting:** {format_display_datetime(vote_cutoff, event['timezone'] or DEFAULT_TIMEZONE)}"
        )
    if rsvp_cutoff:
        description_lines.append(
            f"👥 **RSVP:** {format_display_datetime(rsvp_cutoff, event['timezone'] or DEFAULT_TIMEZONE)}"
        )

    description = None
    if description_lines:
        description = (
            "╭─────────────────────────\n"
            + "\n".join(description_lines)
            + "\n╰─────────────────────────"
        )

    embed = discord.Embed(title=event["title"], description=description, color=color)

    embed.add_field(
        name="__**🕐 Time**__",
        value=f"{format_event_date(event['date_text'])} • {event['time_text']}",
        inline=False,
    )

    now = datetime.now(UTC)

    if event["location_mode"] == "vote":
        vote_counts = get_location_vote_counts(instance_id)
        highest = max((row["vote_count"] for row in vote_counts), default=0)
        leaders = [row for row in vote_counts if row["vote_count"] == highest and highest > 0]
        selected_location = get_location(event["series_id"], event["selected_location_id"])
        voting_closed = bool(vote_cutoff and now >= vote_cutoff)

        if event["location_finalized"] and selected_location:
            location_lines = [f"**{selected_location['label']}**"]
            if selected_location["address"]:
                location_lines.append(selected_location["address"])
            embed.add_field(
                name="__**📍 Location**__",
                value=safe_field_value("\n".join(location_lines)),
                inline=False,
            )
        else:
            if voting_closed:
                if len(leaders) == 1:
                    headline = f"🔒 Voting closed — **{leaders[0]['label']} led**"
                elif len(leaders) > 1:
                    headline = "⚠️ **Voting closed with a tie — organizer must finalize**"
                else:
                    headline = "⚠️ **Voting closed with no votes — organizer must finalize**"
            elif highest == 0:
                headline = "No votes yet"
            elif len(leaders) == 1:
                headline = f"**{leaders[0]['label']} currently leads**"
            else:
                headline = "**Currently tied:** " + ", ".join(row["label"] for row in leaders)

            lines = [headline]
            for location in vote_counts:
                if event["vote_visibility"] == "anonymous":
                    count = location["vote_count"]
                    people = f"{count} vote" if count == 1 else f"{count} votes"
                else:
                    voters = get_location_voters(instance_id, location["id"])
                    people = ", ".join(row["user_name"] for row in voters) if voters else "—"
                lines.append(f"**{location['label']}:** {people}")

            embed.add_field(
                name="__**📍 Location Vote**__",
                value=safe_field_value("\n".join(lines)),
                inline=False,
            )
    elif event["location_mode"] == "set":
        selected_location = get_location(event["series_id"], event["selected_location_id"])
        if selected_location and event["location_finalized"]:
            location_lines = [f"**{selected_location['label']}**"]
            if selected_location["address"]:
                location_lines.append(selected_location["address"])
            embed.add_field(
                name="__**📍 Location**__",
                value=safe_field_value("\n".join(location_lines)),
                inline=False,
            )
        else:
            embed.add_field(name="__**📍 Location**__", value="Not selected yet", inline=False)

    counts = summary_counts(instance_id, event["series_id"])
    for group in get_signup_groups(event["series_id"]):
        is_attendance = "Attendance" in group["name"]
        lines = []
        for option in get_signup_options(group["id"]):
            people = get_option_people(instance_id, option["id"])
            semantic = option_semantic(option["label"])
            display_names = []
            quantity_total = 0

            for row in people:
                name = row["user_name"]
                if semantic == "guest":
                    qty = get_response_quantity(instance_id, row["user_id"], option["id"], 1)
                    quantity_total += qty
                    name = f"{name} (+{qty})"
                elif semantic == "driving":
                    qty = get_response_quantity(instance_id, row["user_id"], option["id"], 1)
                    name = f"{name} ({qty} seat{'s' if qty != 1 else ''})"
                display_names.append(name)

            if not is_attendance and not people:
                continue

            count = quantity_total if semantic == "guest" else len(people)
            names = ", ".join(display_names) if display_names else "—"
            lines.append(f"**{option['label']} ({count}):** {names}")

        if is_attendance and event["capacity"]:
            lines.append(f"🎟️ **Capacity:** {counts['expected']}/{event['capacity']}")

        if not lines:
            continue

        group_name = "__**👥 Attendance**__" if is_attendance else "__**ℹ️ Additional Info**__"
        embed.add_field(name=group_name, value=safe_field_value("\n".join(lines)), inline=False)

    waitlist = get_waitlist(instance_id)
    if waitlist:
        embed.add_field(
            name="__**⏳ Waitlist**__",
            value="\n".join(f"{idx}. {row['user_name']}" for idx, row in enumerate(waitlist, start=1)),
            inline=False,
        )

    if event["image_url"]:
        embed.set_image(url=event["image_url"])

    footer_bits = [f"Created by {event['creator_name']}"]
    if event["repeat_rule"] != "none":
        footer_bits.append(
            REPEAT_FOOTER_LABELS.get(
                event["repeat_rule"],
                f"Repeats {REPEAT_LABELS.get(event['repeat_rule'], event['repeat_rule']).lower()}",
            )
        )
    if cancelled:
        footer_bits.append("CANCELLED")
    embed.set_footer(text=" • ".join(footer_bits))
    return embed
# ==================================================


class LocationButton(discord.ui.Button):
    def __init__(self, instance_id: int, location_id: int, location_label: str, row: int):
        super().__init__(
            label=location_label,
            style=discord.ButtonStyle.secondary,
            custom_id=f"location:{instance_id}:{location_id}",
            row=row,
        )
        self.instance_id = instance_id
        self.location_id = location_id
        self.location_label = location_label

    async def callback(self, interaction: discord.Interaction):
        event = get_event(self.instance_id)
        if event is None:
            await interaction.response.send_message("This event no longer exists.", ephemeral=True)
            return
        if event["status"] != "active":
            await interaction.response.send_message("This event is no longer active.", ephemeral=True)
            return

        if event["location_mode"] == "set" and interaction.user.id != event["creator_id"]:
            await interaction.response.send_message(
                "Only the event organizer can change the location.", ephemeral=True
            )
            return

        vote_cutoff = from_iso(event["vote_cutoff_at"])
        if event["location_mode"] == "vote":
            if event["location_finalized"]:
                await interaction.response.send_message("Location voting has been finalized.", ephemeral=True)
                return
            if vote_cutoff and datetime.now(UTC) >= vote_cutoff:
                await interaction.response.send_message("Location voting is closed.", ephemeral=True)
                return

        # Acknowledge the component immediately. On hosted deployments, rebuilding
        # the embed can take longer than Discord's interaction response window.
        await interaction.response.defer()

        try:
            if event["location_mode"] == "set":
                with get_db() as db:
                    db.execute(
                        "UPDATE event_instances SET selected_location_id = ?, location_finalized = 1 WHERE id = ?",
                        (self.location_id, self.instance_id),
                    )
            else:
                with get_db() as db:
                    if event["vote_type"] == "multi":
                        existing = db.execute(
                            "SELECT 1 FROM location_votes WHERE instance_id=? AND user_id=? AND location_id=?",
                            (self.instance_id, interaction.user.id, self.location_id),
                        ).fetchone()
                        if existing:
                            # Multiple-choice buttons are toggles. Clicking again removes this choice.
                            db.execute(
                                "DELETE FROM location_votes WHERE instance_id=? AND user_id=? AND location_id=?",
                                (self.instance_id, interaction.user.id, self.location_id),
                            )
                        else:
                            db.execute(
                                "INSERT INTO location_votes(instance_id,user_id,user_name,location_id) VALUES(?,?,?,?)",
                                (self.instance_id, interaction.user.id, interaction.user.display_name, self.location_id),
                            )
                    else:
                        # Single-choice voting: move the user's vote to the newly clicked location.
                        db.execute(
                            "DELETE FROM location_votes WHERE instance_id=? AND user_id=?",
                            (self.instance_id, interaction.user.id),
                        )
                        db.execute(
                            "INSERT INTO location_votes(instance_id,user_id,user_name,location_id) VALUES(?,?,?,?)",
                            (self.instance_id, interaction.user.id, interaction.user.display_name, self.location_id),
                        )

            await interaction.edit_original_response(
                embed=build_embed(self.instance_id),
                view=EventView(self.instance_id),
            )
        except Exception:
            traceback.print_exc()
            await interaction.followup.send(
                "❌ Something went wrong updating the location. Check the bot logs.",
                ephemeral=True,
            )


class QuantityResponseModal(discord.ui.Modal):
    def __init__(self, button, semantic: str, user_id: int):
        title = "Guest Count" if semantic == "guest" else "Driving Seats"
        super().__init__(title=title)
        self.button_ref = button
        self.semantic = semantic
        self.user_id = user_id
        current = get_response_quantity(
            button.instance_id,
            user_id,
            button.option_id,
            1,
        )
        self.quantity = discord.ui.TextInput(
            placeholder=("1" if semantic == "guest" else "3"),
            default=str(current),
            max_length=2,
        )
        description = (
            "How many guests are you bringing? Enter 0 to remove this option."
            if semantic == "guest"
            else "How many passenger seats can you offer? Enter 0 to stop driving."
        )
        self.add_item(discord.ui.Label(text=("Guests" if semantic == "guest" else "Available Seats"), description=description, component=self.quantity))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(self.quantity.value.strip())
        except ValueError:
            await interaction.response.send_message("Please enter a whole number.", ephemeral=True)
            return
        if not 0 <= qty <= 10:
            await interaction.response.send_message("Please enter a number from 0 to 10.", ephemeral=True)
            return

        button = self.button_ref
        event = get_event(button.instance_id)
        if not event:
            await interaction.response.send_message("This event no longer exists.", ephemeral=True)
            return

        # Guests consume capacity only when the member is currently Going.
        if self.semantic == "guest" and qty > 0 and event["capacity"]:
            going = get_semantic_option(event["series_id"], "going")
            if going and interaction.user.id in get_option_user_ids(button.instance_id, going["id"]):
                old_qty = guest_quantity_for_user(button.instance_id, event["series_id"], interaction.user.id)
                projected = occupied_capacity(button.instance_id, event["series_id"]) - old_qty + qty
                if projected > int(event["capacity"]):
                    await interaction.response.send_message(
                        f"That would exceed the event capacity of {event['capacity']}. Reduce the guest count or wait for a spot to open.",
                        ephemeral=True,
                    )
                    return

        await interaction.response.defer()
        with get_db() as db:
            if qty == 0:
                db.execute(
                    "DELETE FROM signup_responses WHERE instance_id=? AND user_id=? AND option_id=?",
                    (button.instance_id, interaction.user.id, button.option_id),
                )
                db.execute(
                    "DELETE FROM signup_response_details WHERE instance_id=? AND user_id=? AND option_id=?",
                    (button.instance_id, interaction.user.id, button.option_id),
                )
            else:
                db.execute(
                    "INSERT OR IGNORE INTO signup_responses(instance_id,user_id,user_name,option_id) VALUES(?,?,?,?)",
                    (button.instance_id, interaction.user.id, interaction.user.display_name, button.option_id),
                )
        if qty > 0:
            set_response_quantity(button.instance_id, interaction.user.id, button.option_id, qty)

        promoted = promote_waitlist(button.instance_id)
        await interaction.edit_original_response(
            embed=build_embed(button.instance_id),
            view=EventView(button.instance_id),
        )
        if promoted:
            await interaction.followup.send(
                "✅ A spot opened and the waitlist promoted: " + ", ".join(promoted),
                ephemeral=True,
            )


class SignupButton(discord.ui.Button):
    def __init__(
        self,
        instance_id: int,
        group_id: int,
        group_type: str,
        option_id: int,
        option_label: str,
        row: int,
    ):
        super().__init__(
            label=option_label,
            style=discord.ButtonStyle.secondary,
            custom_id=f"signup:{instance_id}:{option_id}",
            row=row,
        )
        self.instance_id = instance_id
        self.group_id = group_id
        self.group_type = group_type
        self.option_id = option_id
        self.option_label = option_label

    async def callback(self, interaction: discord.Interaction):
        event = get_event(self.instance_id)
        if event is None or event["status"] != "active":
            await interaction.response.send_message("This event is no longer active.", ephemeral=True)
            return

        rsvp_cutoff = from_iso(event["rsvp_cutoff_at"])
        if rsvp_cutoff and datetime.now(UTC) >= rsvp_cutoff:
            await interaction.response.send_message("RSVPs are closed for this event.", ephemeral=True)
            return

        semantic = option_semantic(self.option_label)

        # Quantity-based options use a modal instead of a simple on/off toggle.
        if semantic in {"guest", "driving"}:
            await interaction.response.send_modal(QuantityResponseModal(self, semantic, interaction.user.id))
            return

        await interaction.response.defer()

        try:
            waitlisted = False
            with get_db() as db:
                current = db.execute(
                    """
                    SELECT 1 FROM signup_responses
                    WHERE instance_id = ? AND user_id = ? AND option_id = ?
                    """,
                    (self.instance_id, interaction.user.id, self.option_id),
                ).fetchone()

                if self.group_type == "single":
                    # Capacity applies to Going. If full, move the member to the
                    # FIFO waitlist instead of silently overbooking the event.
                    if semantic == "going" and event["capacity"] and not current:
                        needed = 1 + guest_quantity_for_user(
                            self.instance_id, event["series_id"], interaction.user.id
                        )
                        available = available_capacity(
                            self.instance_id, event["series_id"], int(event["capacity"])
                        )
                        if available is not None and needed > available:
                            option_ids = [
                                row["id"] for row in db.execute(
                                    "SELECT id FROM signup_options WHERE group_id = ?", (self.group_id,)
                                ).fetchall()
                            ]
                            if option_ids:
                                placeholders = ",".join("?" for _ in option_ids)
                                db.execute(
                                    f"DELETE FROM signup_responses WHERE instance_id=? AND user_id=? AND option_id IN ({placeholders})",
                                    [self.instance_id, interaction.user.id, *option_ids],
                                )
                            db.execute(
                                """INSERT INTO event_waitlist(instance_id,user_id,user_name,created_at)
                                   VALUES(?,?,?,?)
                                   ON CONFLICT(instance_id,user_id) DO UPDATE SET user_name=excluded.user_name""",
                                (self.instance_id, interaction.user.id, interaction.user.display_name, iso_utc(datetime.now(UTC))),
                            )
                            waitlisted = True

                    if not waitlisted:
                        option_ids = [
                            row["id"]
                            for row in db.execute(
                                "SELECT id FROM signup_options WHERE group_id = ?", (self.group_id,)
                            ).fetchall()
                        ]
                        if option_ids:
                            placeholders = ",".join("?" for _ in option_ids)
                            db.execute(
                                f"""
                                DELETE FROM signup_responses
                                WHERE instance_id = ? AND user_id = ? AND option_id IN ({placeholders})
                                """,
                                [self.instance_id, interaction.user.id, *option_ids],
                            )
                        db.execute(
                            """
                            INSERT INTO signup_responses (instance_id, user_id, user_name, option_id)
                            VALUES (?, ?, ?, ?)
                            """,
                            (self.instance_id, interaction.user.id, interaction.user.display_name, self.option_id),
                        )
                        db.execute(
                            "DELETE FROM event_waitlist WHERE instance_id=? AND user_id=?",
                            (self.instance_id, interaction.user.id),
                        )
                else:
                    if current:
                        db.execute(
                            "DELETE FROM signup_responses WHERE instance_id = ? AND user_id = ? AND option_id = ?",
                            (self.instance_id, interaction.user.id, self.option_id),
                        )
                        db.execute(
                            "DELETE FROM signup_response_details WHERE instance_id=? AND user_id=? AND option_id=?",
                            (self.instance_id, interaction.user.id, self.option_id),
                        )
                    else:
                        db.execute(
                            """
                            INSERT INTO signup_responses (instance_id, user_id, user_name, option_id)
                            VALUES (?, ?, ?, ?)
                            """,
                            (self.instance_id, interaction.user.id, interaction.user.display_name, self.option_id),
                        )

            promoted = promote_waitlist(self.instance_id)
            await interaction.edit_original_response(
                embed=build_embed(self.instance_id),
                view=EventView(self.instance_id),
            )
            if waitlisted:
                position = next(
                    (idx for idx, row in enumerate(get_waitlist(self.instance_id), start=1) if row["user_id"] == interaction.user.id),
                    None,
                )
                await interaction.followup.send(
                    f"⏳ The event is full. You were added to the waitlist{f' at position {position}' if position else ''}.",
                    ephemeral=True,
                )
            elif promoted:
                await interaction.followup.send(
                    "✅ A spot opened and the waitlist promoted: " + ", ".join(promoted),
                    ephemeral=True,
                )
        except Exception:
            traceback.print_exc()
            await interaction.followup.send(
                "❌ Something went wrong updating your response. Check the bot logs.",
                ephemeral=True,
            )


class ClearResponseButton(discord.ui.Button):
    def __init__(self, instance_id: int, row: int):
        super().__init__(
            label="Clear Response",
            emoji="↩️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"clear_response:{instance_id}",
            row=row,
        )
        self.instance_id = instance_id

    async def callback(self, interaction: discord.Interaction):
        event = get_event(self.instance_id)
        if event is None or event["status"] != "active":
            await interaction.response.send_message("This event is no longer active.", ephemeral=True)
            return

        rsvp_cutoff = from_iso(event["rsvp_cutoff_at"])
        if rsvp_cutoff and datetime.now(UTC) >= rsvp_cutoff:
            await interaction.response.send_message("RSVPs are closed for this event.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            with get_db() as db:
                db.execute(
                    "DELETE FROM signup_responses WHERE instance_id=? AND user_id=?",
                    (self.instance_id, interaction.user.id),
                )
                db.execute(
                    "DELETE FROM signup_response_details WHERE instance_id=? AND user_id=?",
                    (self.instance_id, interaction.user.id),
                )
                db.execute(
                    "DELETE FROM event_waitlist WHERE instance_id=? AND user_id=?",
                    (self.instance_id, interaction.user.id),
                )

            promoted = promote_waitlist(self.instance_id)
            await refresh_event_message(self.instance_id)
            message = "↩️ Your Attendance and Additional Info responses were cleared."
            if promoted:
                message += " A spot opened and the waitlist promoted: " + ", ".join(promoted)
            await interaction.followup.send(message, ephemeral=True)
        except Exception:
            traceback.print_exc()
            await interaction.followup.send(
                "❌ Something went wrong clearing your response. Check the bot logs.",
                ephemeral=True,
            )


class EventView(discord.ui.View):
    def __init__(self, instance_id: int):
        super().__init__(timeout=None)
        self.instance_id = instance_id

        event = get_event(instance_id)
        if event is None:
            return

        if event["location_mode"] == "vote":
            for location in get_locations(event["series_id"]):
                self.add_item(LocationButton(instance_id, location["id"], location["label"], row=0))

        signup_row = 1 if event["location_mode"] == "vote" else 0
        for group in get_signup_groups(event["series_id"]):
            for option in get_signup_options(group["id"]):
                self.add_item(
                    SignupButton(
                        instance_id,
                        group["id"],
                        group["selection_type"],
                        option["id"],
                        option["label"],
                        row=signup_row,
                    )
                )
            signup_row += 1

        self.add_item(ClearResponseButton(instance_id, row=min(signup_row, 3)))

        selected_location = get_location(event["series_id"], event["selected_location_id"])
        directions_url = (
            google_maps_directions_url(selected_location["address"])
            if selected_location and event["location_finalized"]
            else None
        )
        link_row = min(signup_row + 1, 4)
        if directions_url:
            self.add_item(
                discord.ui.Button(
                    label="Directions", emoji="🗺️", style=discord.ButtonStyle.link,
                    url=directions_url, row=link_row
                )
            )
        if event["thread_id"]:
            thread_url = f"https://discord.com/channels/{event['guild_id']}/{event['thread_id']}"
            self.add_item(
                discord.ui.Button(
                    label="Discussion", emoji="💬", style=discord.ButtonStyle.link,
                    url=thread_url, row=link_row
                )
            )

        self.refresh_buttons()

    def refresh_buttons(self):
        event = get_event(self.instance_id)
        if event is None:
            return

        now = datetime.now(UTC)
        inactive = event["status"] != "active"
        vote_cutoff = from_iso(event["vote_cutoff_at"])
        rsvp_cutoff = from_iso(event["rsvp_cutoff_at"])

        location_buttons = [c for c in self.children if isinstance(c, LocationButton)]
        signup_buttons = [c for c in self.children if isinstance(c, SignupButton)]

        if event["location_mode"] == "vote":
            counts = get_location_vote_counts(self.instance_id)
            count_map = {row["id"]: row["vote_count"] for row in counts}
            highest = max(count_map.values(), default=0)
            leaders = [location_id for location_id, count in count_map.items() if count == highest and highest > 0]
            tied = len(leaders) > 1
            closed = bool(event["location_finalized"] or (vote_cutoff and now >= vote_cutoff))

            for button in location_buttons:
                count = count_map.get(button.location_id, 0)
                button.label = f"{button.location_label} · {count}"
                if event["location_finalized"] and event["selected_location_id"] == button.location_id:
                    button.style = discord.ButtonStyle.success
                elif button.location_id in leaders and not closed:
                    button.style = discord.ButtonStyle.primary if tied else discord.ButtonStyle.success
                else:
                    button.style = discord.ButtonStyle.secondary
                button.disabled = inactive or closed

        for button in signup_buttons:
            semantic = option_semantic(button.option_label)
            if semantic == "guest":
                count = sum(
                    get_response_quantity(self.instance_id, row["user_id"], button.option_id, 1)
                    for row in get_option_people(self.instance_id, button.option_id)
                )
            else:
                count = get_option_count(self.instance_id, button.option_id)
            button.label = f"{button.option_label} · {count}"
            lower = button.option_label.casefold()
            if "can't" in lower or "cannot" in lower or "not going" in lower:
                button.style = discord.ButtonStyle.danger
            elif "tentative" in lower or "maybe" in lower:
                button.style = discord.ButtonStyle.primary
            elif "going" in lower and "not going" not in lower:
                button.style = discord.ButtonStyle.success
            else:
                button.style = discord.ButtonStyle.secondary
            button.disabled = inactive or bool(rsvp_cutoff and now >= rsvp_cutoff)

        for child in self.children:
            if isinstance(child, ClearResponseButton):
                child.disabled = inactive or bool(rsvp_cutoff and now >= rsvp_cutoff)


class EditEventButton(discord.ui.Button):
    def __init__(self, instance_id: int, row: int):
        super().__init__(
            label="Edit Event",
            emoji="✏️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"admin_edit:{instance_id}",
            row=row,
        )
        self.instance_id = instance_id

    async def callback(self, interaction: discord.Interaction):
        event = get_event(self.instance_id)
        if not event:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if interaction.user.id != event["creator_id"] and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only the organizer or a server manager can edit this event.", ephemeral=True)
            return
        await interaction.response.send_modal(EditEventModal(self.instance_id))


class FinalizeLocationButton(discord.ui.Button):
    def __init__(self, instance_id: int, row: int):
        super().__init__(
            label="Finalize Location",
            emoji="🔒",
            style=discord.ButtonStyle.primary,
            custom_id=f"admin_finalize:{instance_id}",
            row=row,
        )
        self.instance_id = instance_id

    async def callback(self, interaction: discord.Interaction):
        event = get_event(self.instance_id)
        if not event:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if interaction.user.id != event["creator_id"] and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only the organizer or a server manager can finalize the location.", ephemeral=True)
            return
        await interaction.response.send_modal(FinalizeLocationModal(self.instance_id))


class CancelEventButton(discord.ui.Button):
    def __init__(self, instance_id: int, row: int):
        super().__init__(
            label="Cancel Event",
            emoji="🗑️",
            style=discord.ButtonStyle.danger,
            custom_id=f"admin_cancel:{instance_id}",
            row=row,
        )
        self.instance_id = instance_id

    async def callback(self, interaction: discord.Interaction):
        event = get_event(self.instance_id)
        if not event:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if interaction.user.id != event["creator_id"] and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only the organizer or a server manager can cancel this event.", ephemeral=True)
            return
        await interaction.response.send_message(
            "What would you like to cancel?",
            view=CancelConfirmView(self.instance_id),
            ephemeral=True,
        )


# ==================================================
# ORGANIZER MODALS / CONFIRMATIONS
# ==================================================


class EditEventModal(discord.ui.Modal, title="Edit Event"):
    def __init__(self, instance_id: int):
        super().__init__()
        self.instance_id = instance_id
        event = get_event(instance_id)
        if event is None:
            raise RuntimeError("Event not found")

        self.title_input = discord.ui.TextInput(
            default=event["title"], max_length=100
        )
        self.description_input = discord.ui.TextInput(
            default=event["description"] or None,
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=500,
        )
        self.date_input = discord.ui.TextInput(
            default=event["date_text"], max_length=20
        )
        self.time_input = discord.ui.TextInput(
            default=event["time_text"], max_length=50
        )
        self.repeat_select = discord.ui.Select(
            placeholder="Repeat",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=value, default=(event["repeat_rule"] == value))
                for value, label in REPEAT_LABELS.items()
            ],
        )

        self.add_item(discord.ui.Label(text="Event Name", component=self.title_input))
        self.add_item(discord.ui.Label(text="Description", component=self.description_input))
        self.add_item(discord.ui.Label(text="Date", component=self.date_input))
        self.add_item(discord.ui.Label(text="Time", component=self.time_input))
        self.add_item(discord.ui.Label(text="Repeat", component=self.repeat_select))

    async def on_submit(self, interaction: discord.Interaction):
        event = get_event(self.instance_id)
        if event is None:
            await interaction.response.send_message("Event no longer exists.", ephemeral=True)
            return

        try:
            new_start = parse_event_start(self.date_input.value, self.time_input.value, event["timezone"])
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        vote_cutoff = None
        rsvp_cutoff = None
        if event["vote_cutoff_offset_minutes"] is not None:
            vote_cutoff = new_start - timedelta(minutes=int(event["vote_cutoff_offset_minutes"]))
        if event["rsvp_cutoff_offset_minutes"] is not None:
            rsvp_cutoff = new_start - timedelta(minutes=int(event["rsvp_cutoff_offset_minutes"]))

        local_start = new_start.astimezone(get_zone(event["timezone"]))

        with get_db() as db:
            db.execute(
                """
                UPDATE event_series
                SET title = ?, description = ?, time_text = ?, repeat_rule = ?,
                    start_time_local = ?, anchor_day = ?
                WHERE id = ?
                """,
                (
                    self.title_input.value,
                    self.description_input.value or "",
                    self.time_input.value,
                    self.repeat_select.values[0],
                    local_start.strftime("%H:%M"),
                    local_start.day,
                    event["series_id"],
                ),
            )
            db.execute(
                """
                UPDATE event_instances
                SET date_text = ?, occurrence_start_at = ?, vote_cutoff_at = ?, rsvp_cutoff_at = ?
                WHERE id = ?
                """,
                (
                    self.date_input.value,
                    iso_utc(new_start),
                    iso_utc(vote_cutoff),
                    iso_utc(rsvp_cutoff),
                    self.instance_id,
                ),
            )

        await refresh_event_message(self.instance_id)
        await interaction.response.send_message("✅ Event updated.", ephemeral=True)


class FinalizeLocationModal(discord.ui.Modal, title="Finalize Location"):
    def __init__(self, instance_id: int):
        super().__init__()
        self.instance_id = instance_id
        event = get_event(instance_id)
        if event is None:
            raise RuntimeError("Event not found")

        counts = get_location_vote_counts(instance_id)
        highest = max((row["vote_count"] for row in counts), default=0)
        leaders = {row["id"] for row in counts if row["vote_count"] == highest and highest > 0}

        options = []
        for location in get_locations(event["series_id"]):
            options.append(
                discord.SelectOption(
                    label=location["label"],
                    value=str(location["id"]),
                    description=f"{next((row['vote_count'] for row in counts if row['id'] == location['id']), 0)} vote(s)",
                    default=(
                        event["selected_location_id"] == location["id"]
                        or (not event["selected_location_id"] and len(leaders) == 1 and location["id"] in leaders)
                    ),
                )
            )

        self.location_select = discord.ui.Select(
            placeholder="Choose the final location",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.add_item(discord.ui.Label(text="Final Location", component=self.location_select))

    async def on_submit(self, interaction: discord.Interaction):
        location_id = int(self.location_select.values[0])
        with get_db() as db:
            db.execute(
                "UPDATE event_instances SET selected_location_id = ?, location_finalized = 1 WHERE id = ?",
                (location_id, self.instance_id),
            )
        await refresh_event_message(self.instance_id)
        event = get_event(self.instance_id)
        location = get_location(event["series_id"], location_id)
        await interaction.response.send_message(
            f"🔒 Final location set to **{location['label']}**.", ephemeral=True
        )


class CancelConfirmView(discord.ui.View):
    def __init__(self, instance_id: int):
        super().__init__(timeout=120)
        self.instance_id = instance_id

    @discord.ui.button(label="Cancel This Occurrence", style=discord.ButtonStyle.danger)
    async def cancel_occurrence(self, interaction: discord.Interaction, button: discord.ui.Button):
        with get_db() as db:
            db.execute("UPDATE event_instances SET status = 'cancelled' WHERE id = ?", (self.instance_id,))
        await refresh_event_message(self.instance_id)
        await interaction.response.edit_message(content="❌ This occurrence was cancelled.", view=None)
        self.stop()

    @discord.ui.button(label="Stop Entire Series", style=discord.ButtonStyle.danger)
    async def cancel_series(self, interaction: discord.Interaction, button: discord.ui.Button):
        event = get_event(self.instance_id)
        affected_ids = []
        if event:
            with get_db() as db:
                affected_ids = [
                    row["id"]
                    for row in db.execute(
                        "SELECT id FROM event_instances WHERE series_id = ? AND status = 'active'",
                        (event["series_id"],),
                    ).fetchall()
                ]
                db.execute("UPDATE event_series SET active = 0 WHERE id = ?", (event["series_id"],))
                db.execute(
                    "UPDATE event_instances SET status = 'cancelled' WHERE series_id = ? AND status = 'active'",
                    (event["series_id"],),
                )
        for affected_id in affected_ids:
            await refresh_event_message(affected_id)
        await interaction.response.edit_message(content="🛑 All active occurrences were cancelled and the recurring series was stopped.", view=None)
        self.stop()

    @discord.ui.button(label="Keep Event", style=discord.ButtonStyle.secondary)
    async def keep_event(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="No changes made.", view=None)
        self.stop()


# ==================================================
# EVENT DRAFT + WIZARD
# ==================================================


@dataclass
class EventDraft:
    creator_id: int
    creator_name: str
    guild_id: int
    channel_id: int
    title: str
    description: str
    date_text: str
    time_text: str
    repeat_rule: str
    timezone: str = DEFAULT_TIMEZONE
    start_at_utc: datetime | None = None
    location_mode: str | None = None
    # Phase 3 advanced voting: single = one destination, multi = any destinations.
    vote_type: str = "single"
    # public = show voter names, anonymous = only show totals.
    vote_visibility: str = "public"
    location_options: list[str] = field(default_factory=list)
    # Name -> address snapshot. Empty/None means the location has no directions address.
    location_addresses: dict[str, str | None] = field(default_factory=dict)
    # Name -> saved location ID when the option came from the server location book.
    location_saved_ids: dict[str, int | None] = field(default_factory=dict)
    initial_location_label: str | None = None
    attendance_options: list[str] = field(
        default_factory=lambda: ["✅ Going", "❓ Tentative"]
    )
    additional_info_options: list[str] = field(default_factory=list)
    image_bytes: bytes | None = None
    image_filename: str | None = None
    ping_role_id: int | None = None
    ping_role_name: str | None = None
    vote_cutoff_at_utc: datetime | None = None
    rsvp_cutoff_at_utc: datetime | None = None
    vote_cutoff_offset_minutes: int | None = None
    rsvp_cutoff_offset_minutes: int | None = None
    post_days_before: int = 5
    reminder_minutes: int = 120
    thread_enabled: bool = True
    capacity: int | None = None


def build_setup_embed(draft: EventDraft) -> discord.Embed:
    """Compact private creator preview; optional settings appear only when enabled."""
    friendly_date = format_event_date(draft.date_text)

    embed = discord.Embed(
        title="Event Setup",
        description=(
            f"### {draft.title}\n"
            f"🕐 **{friendly_date} • {draft.time_text}**\n\n"
            "Configure what you need below, then press **Create Event**."
        ),
        color=discord.Color.blurple(),
    )

    if draft.location_mode is None:
        location_text = "⚠️ Not configured"
    elif draft.location_mode == "none":
        location_text = "🌐 No Location"
    elif draft.location_mode == "vote":
        vote_type_label = "Single choice" if draft.vote_type == "single" else "Multiple choice"
        visibility_label = "Public" if draft.vote_visibility == "public" else "Anonymous"
        names = ", ".join(draft.location_options) if draft.location_options else "No options"
        location_text = f"🗳️ Vote • {vote_type_label} • {visibility_label}\n{names}"
    else:
        names = ", ".join(draft.location_options) if draft.location_options else "No options"
        if draft.initial_location_label:
            location_text = f"📍 {draft.initial_location_label}\nChoices: {names}"
        else:
            location_text = f"📍 Organizer selects\n{names}"

    embed.add_field(name="📍 Location", value=location_text, inline=False)

    attendance_text = " • ".join(draft.attendance_options) if draft.attendance_options else "None"
    embed.add_field(name="👥 Attendance", value=attendance_text, inline=False)

    if draft.additional_info_options:
        embed.add_field(
            name="ℹ️ Additional Info",
            value=" • ".join(draft.additional_info_options),
            inline=False,
        )

    if draft.ping_role_id:
        embed.add_field(name="📣 Ping", value=f"<@&{draft.ping_role_id}>", inline=False)

    if draft.repeat_rule != "none":
        repeat_text = REPEAT_FOOTER_LABELS.get(
            draft.repeat_rule,
            f"Repeats {REPEAT_LABELS.get(draft.repeat_rule, draft.repeat_rule).lower()}",
        )
        embed.add_field(name="🔁 Repeat", value=repeat_text, inline=False)

    cutoff_lines = []
    if draft.location_mode == "vote" and draft.vote_cutoff_at_utc:
        if draft.repeat_rule != "none":
            cutoff_lines.append(
                f"🗳️ Voting: {recurring_cutoff_text(draft.start_at_utc, draft.vote_cutoff_at_utc, draft.timezone)}"
            )
        else:
            cutoff_lines.append(
                f"🗳️ Voting: {format_local_cutoff(draft.vote_cutoff_at_utc, draft.timezone)}"
            )

    if draft.rsvp_cutoff_at_utc:
        if draft.repeat_rule != "none":
            cutoff_lines.append(
                f"👥 RSVP: {recurring_cutoff_text(draft.start_at_utc, draft.rsvp_cutoff_at_utc, draft.timezone)}"
            )
        else:
            cutoff_lines.append(
                f"👥 RSVP: {format_local_cutoff(draft.rsvp_cutoff_at_utc, draft.timezone)}"
            )

    if cutoff_lines:
        embed.add_field(name="⏳ Cutoffs", value="\n".join(cutoff_lines), inline=False)

    if draft.thread_enabled:
        embed.add_field(name="🧵 Discussion", value="Create automatically", inline=False)

    if draft.capacity:
        embed.add_field(
            name="🎟️ Capacity",
            value=f"{draft.capacity} people • automatic waitlist",
            inline=False,
        )

    if draft.image_filename:
        embed.add_field(name="🖼️ Image", value=f"✅ {draft.image_filename}", inline=False)

    embed.set_footer(text="Only you can see this setup.")
    return embed


class LocationModeView(discord.ui.View):
    """Small first step so irrelevant voting controls never appear for Set Location."""

    def __init__(self, setup_view):
        super().__init__(timeout=900)
        self.setup_view = setup_view

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.setup_view.draft.creator_id:
            await interaction.response.send_message(
                "Only the person creating this event can change its location setup.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="No Location", emoji="🌐", style=discord.ButtonStyle.secondary, row=0)
    async def no_location(self, interaction: discord.Interaction, button: discord.ui.Button):
        draft = self.setup_view.draft
        draft.location_mode = "none"
        draft.location_options = []
        draft.location_addresses = {}
        draft.location_saved_ids = {}
        draft.initial_location_label = None
        await interaction.response.edit_message(
            embed=build_setup_embed(draft),
            view=self.setup_view,
        )
        self.stop()

    @discord.ui.button(label="Set Location", emoji="📍", style=discord.ButtonStyle.primary, row=0)
    async def set_location(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetLocationModal(self.setup_view))

    @discord.ui.button(label="Vote", emoji="🗳️", style=discord.ButtonStyle.success, row=0)
    async def vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VoteLocationModal(self.setup_view))

    @discord.ui.button(label="Back", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=build_setup_embed(self.setup_view.draft),
            view=self.setup_view,
        )
        self.stop()


class AttendanceSetupModal(discord.ui.Modal, title="Attendance"):
    def __init__(self, setup_view):
        super().__init__()
        self.setup_view = setup_view
        self.options_input = discord.ui.TextInput(
            placeholder="✅ Going, ❓ Tentative",
            default=", ".join(setup_view.draft.attendance_options),
            style=discord.TextStyle.paragraph,
            max_length=400,
        )
        self.add_item(
            discord.ui.Label(
                text="Attendance Options",
                description="Users can select ONE of these options.",
                component=self.options_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        options = parse_options(self.options_input.value)
        if not options:
            await interaction.response.send_message("Attendance needs at least one option.", ephemeral=True)
            return
        if len(options) > 5:
            await interaction.response.send_message("Please use 5 attendance options or fewer.", ephemeral=True)
            return
        if any(len(x) > 60 for x in options):
            await interaction.response.send_message("Please keep attendance option names to 60 characters or fewer.", ephemeral=True)
            return
        self.setup_view.draft.attendance_options = options
        await interaction.response.edit_message(embed=build_setup_embed(self.setup_view.draft), view=self.setup_view)


class AdditionalInfoSetupModal(discord.ui.Modal, title="Additional Info"):
    PRESET_OPTIONS = ["🚗 Driving", "🙋 Need a Ride", "➕ Bringing +1", "🍕 Eating"]

    def __init__(self, setup_view):
        super().__init__()
        self.setup_view = setup_view
        current = setup_view.draft.additional_info_options
        current_set = {x.casefold() for x in current}

        self.preset_select = discord.ui.Select(
            placeholder="Select any that apply",
            min_values=0,
            max_values=4,
            required=False,
            options=[
                discord.SelectOption(
                    label="Driving",
                    value="🚗 Driving",
                    emoji="🚗",
                    description="I can drive / provide transportation",
                    default=("🚗 driving".casefold() in current_set),
                ),
                discord.SelectOption(
                    label="Need a Ride",
                    value="🙋 Need a Ride",
                    emoji="🙋",
                    description="I need a seat with a driver",
                    default=("🙋 need a ride".casefold() in current_set),
                ),
                discord.SelectOption(
                    label="Bringing +1",
                    value="➕ Bringing +1",
                    emoji="➕",
                    description="I'm bringing one additional person",
                    default=("➕ bringing +1".casefold() in current_set),
                ),
                discord.SelectOption(
                    label="Eating",
                    value="🍕 Eating",
                    emoji="🍕",
                    description="Include me in the food count",
                    default=("🍕 eating".casefold() in current_set),
                ),
            ],
        )

        preset_keys = {x.casefold() for x in self.PRESET_OPTIONS}
        custom_existing = [x for x in current if x.casefold() not in preset_keys]
        self.custom_input = discord.ui.TextInput(
            placeholder="Need a ride, Bringing snacks, Leaving early",
            default=", ".join(custom_existing) or None,
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=400,
        )

        self.add_item(
            discord.ui.Label(
                text="Common Options",
                description="Choose as many as you want.",
                component=self.preset_select,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Custom Options",
                description="Optional. Separate multiple options with commas.",
                component=self.custom_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        combined = []
        seen = set()
        for option in list(self.preset_select.values) + parse_options(self.custom_input.value):
            key = option.casefold()
            if key not in seen:
                seen.add(key)
                combined.append(option)

        if len(combined) > 5:
            await interaction.response.send_message("Please use no more than 5 Additional Info options.", ephemeral=True)
            return
        if any(len(x) > 60 for x in combined):
            await interaction.response.send_message("Please keep Additional Info option names to 60 characters or fewer.", ephemeral=True)
            return

        self.setup_view.draft.additional_info_options = combined
        await interaction.response.edit_message(embed=build_setup_embed(self.setup_view.draft), view=self.setup_view)


class ImageUploadModal(discord.ui.Modal, title="Event Image"):
    def __init__(self, setup_view):
        super().__init__()
        self.setup_view = setup_view
        self.upload = discord.ui.FileUpload(required=False, min_values=0, max_values=1)
        self.add_item(
            discord.ui.Label(
                text="Event Image",
                description="Upload one image. Submit with no file to remove the current image.",
                component=self.upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        attachments = self.upload.values
        if not attachments:
            self.setup_view.draft.image_bytes = None
            self.setup_view.draft.image_filename = None
            await interaction.response.edit_message(embed=build_setup_embed(self.setup_view.draft), view=self.setup_view)
            return

        attachment = attachments[0]
        if attachment.content_type and not attachment.content_type.startswith("image/"):
            await interaction.response.send_message("Please upload an image file.", ephemeral=True)
            return

        self.setup_view.draft.image_bytes = await attachment.read()
        self.setup_view.draft.image_filename = attachment.filename
        await interaction.response.edit_message(embed=build_setup_embed(self.setup_view.draft), view=self.setup_view)


class RoleSetupModal(discord.ui.Modal, title="Ping Role"):
    def __init__(self, setup_view):
        super().__init__()
        self.setup_view = setup_view
        draft = setup_view.draft
        defaults = [discord.Object(id=draft.ping_role_id)] if draft.ping_role_id else []
        self.role_select = discord.ui.RoleSelect(
            placeholder="Select a role to ping (optional)",
            min_values=0,
            max_values=1,
            required=False,
            default_values=defaults,
        )
        self.add_item(
            discord.ui.Label(
                text="Announcement Role",
                description="Example: @MTG or @Snowboarding. Leave blank for no ping.",
                component=self.role_select,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        if not self.role_select.values:
            self.setup_view.draft.ping_role_id = None
            self.setup_view.draft.ping_role_name = None
        else:
            role = self.role_select.values[0]
            if role.is_default():
                await interaction.response.send_message("Please choose a role other than @everyone.", ephemeral=True)
                return
            self.setup_view.draft.ping_role_id = role.id
            self.setup_view.draft.ping_role_name = role.name

        await interaction.response.edit_message(embed=build_setup_embed(self.setup_view.draft), view=self.setup_view)


class ScheduleSetupModal(discord.ui.Modal, title="Schedule & Cutoffs"):
    def __init__(self, setup_view):
        super().__init__()
        self.setup_view = setup_view
        draft = setup_view.draft

        self.timezone_input = discord.ui.TextInput(
            placeholder="America/Denver",
            default=draft.timezone,
            max_length=64,
        )
        self.add_item(discord.ui.Label(
            text="Timezone",
            description="IANA timezone, e.g. America/Denver",
            component=self.timezone_input,
        ))

        if draft.repeat_rule != "none":
            # Recurring events use relative calendar rules instead of making the
            # organizer calculate an exact date for the first occurrence.
            def current_days(cutoff):
                if cutoff is None or draft.start_at_utc is None:
                    return None
                zone = get_zone(draft.timezone)
                return (
                    draft.start_at_utc.astimezone(zone).date()
                    - cutoff.astimezone(zone).date()
                ).days

            def day_options(current):
                values = [
                    ("No cutoff", "none"),
                    ("Same day", "0"),
                    ("1 day before", "1"),
                    ("2 days before", "2"),
                    ("3 days before", "3"),
                    ("4 days before", "4"),
                    ("5 days before", "5"),
                    ("6 days before", "6"),
                    ("7 days before", "7"),
                ]
                selected = "none" if current is None else str(current)
                return [
                    discord.SelectOption(label=label, value=value, default=(value == selected))
                    for label, value in values
                ]

            if draft.location_mode == "vote":
                vote_days = current_days(draft.vote_cutoff_at_utc)
                vote_time = (
                    draft.vote_cutoff_at_utc.astimezone(get_zone(draft.timezone)).strftime("%I:%M %p").lstrip("0")
                    if draft.vote_cutoff_at_utc else None
                )
                self.vote_days_select = discord.ui.Select(
                    placeholder="Voting cutoff day",
                    min_values=1,
                    max_values=1,
                    options=day_options(vote_days),
                )
                self.vote_time_input = discord.ui.TextInput(
                    placeholder="6:00 PM",
                    default=vote_time,
                    required=False,
                    max_length=20,
                )
                self.add_item(discord.ui.Label(
                    text="Location voting closes",
                    description="Applies to every recurring occurrence.",
                    component=self.vote_days_select,
                ))
                self.add_item(discord.ui.Label(
                    text="Voting cutoff time",
                    description="Local time, e.g. 6:00 PM.",
                    component=self.vote_time_input,
                ))

            rsvp_days = current_days(draft.rsvp_cutoff_at_utc)
            rsvp_time = (
                draft.rsvp_cutoff_at_utc.astimezone(get_zone(draft.timezone)).strftime("%I:%M %p").lstrip("0")
                if draft.rsvp_cutoff_at_utc else None
            )
            self.rsvp_days_select = discord.ui.Select(
                placeholder="RSVP cutoff day",
                min_values=1,
                max_values=1,
                options=day_options(rsvp_days),
            )
            self.rsvp_time_input = discord.ui.TextInput(
                placeholder="9:00 PM",
                default=rsvp_time,
                required=False,
                max_length=20,
            )
            self.add_item(discord.ui.Label(
                text="RSVP closes",
                description="Applies to every recurring occurrence.",
                component=self.rsvp_days_select,
            ))
            self.add_item(discord.ui.Label(
                text="RSVP cutoff time",
                description="Local time, e.g. 9:00 PM.",
                component=self.rsvp_time_input,
            ))
        else:
            self.vote_cutoff_input = discord.ui.TextInput(
                placeholder="09/24/2026 6:00 PM",
                default=(format_local_cutoff(draft.vote_cutoff_at_utc, draft.timezone) if draft.vote_cutoff_at_utc else None),
                required=False,
                max_length=40,
            )
            self.rsvp_cutoff_input = discord.ui.TextInput(
                placeholder="09/24/2026 9:00 PM",
                default=(format_local_cutoff(draft.rsvp_cutoff_at_utc, draft.timezone) if draft.rsvp_cutoff_at_utc else None),
                required=False,
                max_length=40,
            )
            if draft.location_mode == "vote":
                self.add_item(discord.ui.Label(
                    text="Location Voting Cutoff",
                    description="Optional exact date/time for this one-time event.",
                    component=self.vote_cutoff_input,
                ))
            self.add_item(discord.ui.Label(
                text="RSVP Cutoff",
                description="Optional exact date/time for this one-time event.",
                component=self.rsvp_cutoff_input,
            ))

            self.reminder_hours_input = discord.ui.TextInput(
                placeholder="2",
                default=f"{draft.reminder_minutes / 60:g}",
                max_length=6,
            )
            self.add_item(discord.ui.Label(
                text="Reminder (hours before cutoff)",
                description="0 disables the reminder.",
                component=self.reminder_hours_input,
            ))

    async def on_submit(self, interaction: discord.Interaction):
        draft = self.setup_view.draft
        tz_name = self.timezone_input.value.strip()

        try:
            get_zone(tz_name)
            start_at = parse_event_start(draft.date_text, draft.time_text, tz_name)

            if draft.repeat_rule != "none":
                if draft.location_mode == "vote":
                    raw_vote_days = self.vote_days_select.values[0]
                    vote_days = None if raw_vote_days == "none" else int(raw_vote_days)
                    vote_time = (self.vote_time_input.value or "").strip() or None
                    vote_cutoff = recurring_cutoff_from_parts(start_at, vote_days, vote_time, tz_name)
                else:
                    vote_cutoff = None

                raw_rsvp_days = self.rsvp_days_select.values[0]
                rsvp_days = None if raw_rsvp_days == "none" else int(raw_rsvp_days)
                rsvp_time = (self.rsvp_time_input.value or "").strip() or None
                rsvp_cutoff = recurring_cutoff_from_parts(start_at, rsvp_days, rsvp_time, tz_name)
                reminder_hours = draft.reminder_minutes / 60
            else:
                vote_cutoff = (
                    parse_local_datetime(self.vote_cutoff_input.value, tz_name)
                    if draft.location_mode == "vote" else None
                )
                rsvp_cutoff = parse_local_datetime(self.rsvp_cutoff_input.value, tz_name)
                reminder_hours = float(self.reminder_hours_input.value.strip())
        except (ValueError, ZoneInfoNotFoundError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        if not 0 <= reminder_hours <= 168:
            await interaction.response.send_message("Reminder hours must be between 0 and 168.", ephemeral=True)
            return
        if vote_cutoff and vote_cutoff >= start_at:
            await interaction.response.send_message("Voting cutoff must be before the event starts.", ephemeral=True)
            return
        if rsvp_cutoff and rsvp_cutoff >= start_at:
            await interaction.response.send_message("RSVP cutoff must be before the event starts.", ephemeral=True)
            return

        draft.timezone = tz_name
        draft.start_at_utc = start_at
        draft.vote_cutoff_at_utc = vote_cutoff
        draft.rsvp_cutoff_at_utc = rsvp_cutoff
        draft.vote_cutoff_offset_minutes = (
            int((start_at - vote_cutoff).total_seconds() // 60) if vote_cutoff else None
        )
        draft.rsvp_cutoff_offset_minutes = (
            int((start_at - rsvp_cutoff).total_seconds() // 60) if rsvp_cutoff else None
        )
        draft.reminder_minutes = int(reminder_hours * 60)

        await interaction.response.edit_message(
            embed=build_setup_embed(draft),
            view=self.setup_view,
        )


class CapacitySetupModal(discord.ui.Modal, title="Capacity & Waitlist"):
    def __init__(self, setup_view):
        super().__init__()
        self.setup_view = setup_view
        self.capacity_input = discord.ui.TextInput(
            placeholder="Leave blank for unlimited",
            default=(str(setup_view.draft.capacity) if setup_view.draft.capacity else None),
            required=False,
            max_length=3,
        )
        self.add_item(
            discord.ui.Label(
                text="Event Capacity",
                description="When full, new Going responses are automatically waitlisted. Guests count toward capacity.",
                component=self.capacity_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.capacity_input.value.strip()
        if not raw:
            self.setup_view.draft.capacity = None
        else:
            try:
                capacity = int(raw)
            except ValueError:
                await interaction.response.send_message("Capacity must be a whole number.", ephemeral=True)
                return
            if not 1 <= capacity <= 999:
                await interaction.response.send_message("Capacity must be between 1 and 999.", ephemeral=True)
                return
            self.setup_view.draft.capacity = capacity
        self.setup_view.update_capacity_button_label()
        await interaction.response.edit_message(
            embed=build_setup_embed(self.setup_view.draft),
            view=self.setup_view,
        )


class RepeatSetupModal(discord.ui.Modal, title="Repeat Event"):
    def __init__(self, setup_view):
        super().__init__()
        self.setup_view = setup_view
        draft = setup_view.draft
        current = draft.repeat_rule or "none"

        self.repeat_select = discord.ui.Select(
            placeholder="Choose repeat schedule",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Does not repeat", value="none", default=current == "none"),
                discord.SelectOption(label="Weekly", value="weekly", default=current == "weekly"),
                discord.SelectOption(label="Every 2 weeks", value="biweekly", default=current == "biweekly"),
                discord.SelectOption(label="Monthly", value="monthly", default=current == "monthly"),
            ],
        )
        self.post_days_input = discord.ui.TextInput(
            placeholder="5",
            default=str(draft.post_days_before),
            max_length=2,
        )
        self.reminder_hours_input = discord.ui.TextInput(
            placeholder="2",
            default=f"{draft.reminder_minutes / 60:g}",
            max_length=6,
        )

        self.add_item(discord.ui.Label(
            text="Repeat",
            description="Each occurrence gets fresh RSVPs, votes, and a fresh location.",
            component=self.repeat_select,
        ))
        self.add_item(discord.ui.Label(
            text="Post next occurrence (days before)",
            description="Used when Repeat is enabled. 0-30 days.",
            component=self.post_days_input,
        ))
        self.add_item(discord.ui.Label(
            text="Reminder (hours before cutoff)",
            description="0 disables cutoff reminders.",
            component=self.reminder_hours_input,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        draft = self.setup_view.draft
        try:
            post_days = int(self.post_days_input.value.strip())
            reminder_hours = float(self.reminder_hours_input.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "Post days must be a whole number and reminder hours must be a number.",
                ephemeral=True,
            )
            return

        if not 0 <= post_days <= 30:
            await interaction.response.send_message("Post days must be between 0 and 30.", ephemeral=True)
            return
        if not 0 <= reminder_hours <= 168:
            await interaction.response.send_message("Reminder hours must be between 0 and 168.", ephemeral=True)
            return

        draft.repeat_rule = self.repeat_select.values[0]
        draft.post_days_before = post_days
        draft.reminder_minutes = int(reminder_hours * 60)
        self.setup_view.update_repeat_button_label()

        await interaction.response.edit_message(
            embed=build_setup_embed(draft),
            view=self.setup_view,
        )


class EventSetupView(discord.ui.View):
    def __init__(self, draft: EventDraft):
        super().__init__(timeout=900)
        self.draft = draft
        self.created = False
        self.update_repeat_button_label()
        self.update_capacity_button_label()

    def update_capacity_button_label(self):
        self.capacity_button.label = (
            f"Capacity: {self.draft.capacity}" if self.draft.capacity else "Capacity: Unlimited"
        )

    def update_repeat_button_label(self):
        labels = {
            "none": "Repeat: Off",
            "weekly": "Repeat: Weekly",
            "biweekly": "Repeat: 2 Weeks",
            "monthly": "Repeat: Monthly",
        }
        self.repeat_button.label = labels.get(self.draft.repeat_rule, "Repeat")

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.draft.creator_id:
            await interaction.response.send_message("Only the person creating this event can edit this setup.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Location", emoji="📍", style=discord.ButtonStyle.secondary, row=0)
    async def location_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        chooser = LocationModeView(self)
        embed = discord.Embed(
            title="Location Setup",
            description=(
                "How will the location be decided?\n\n"
                "🌐 **No Location** — online/Discord event; hides location entirely.\n"
                "📍 **Set Location** — organizer chooses the final place.\n"
                "🗳️ **Vote** — attendees vote on the location."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(embed=embed, view=chooser)

    @discord.ui.button(label="Attendance", emoji="👥", style=discord.ButtonStyle.secondary, row=0)
    async def attendance_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AttendanceSetupModal(self))

    @discord.ui.button(label="Additional Info", emoji="ℹ️", style=discord.ButtonStyle.secondary, row=0)
    async def additional_info_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdditionalInfoSetupModal(self))

    @discord.ui.button(label="Image", emoji="🖼️", style=discord.ButtonStyle.secondary, row=0)
    async def image_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ImageUploadModal(self))

    @discord.ui.button(label="Ping Role", emoji="📣", style=discord.ButtonStyle.secondary, row=0)
    async def role_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RoleSetupModal(self))

    @discord.ui.button(label="Repeat: Off", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def repeat_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RepeatSetupModal(self))

    @discord.ui.button(label="Cutoffs", emoji="⏳", style=discord.ButtonStyle.secondary, row=1)
    async def schedule_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ScheduleSetupModal(self))

    @discord.ui.button(label="Thread: On", emoji="🧵", style=discord.ButtonStyle.secondary, row=1)
    async def thread_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.draft.thread_enabled = not self.draft.thread_enabled
        button.label = "Thread: On" if self.draft.thread_enabled else "Thread: Off"
        await interaction.response.edit_message(embed=build_setup_embed(self.draft), view=self)

    @discord.ui.button(label="Capacity: Unlimited", emoji="🎟️", style=discord.ButtonStyle.secondary, row=2)
    async def capacity_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CapacitySetupModal(self))

    @discord.ui.button(label="Create Event", emoji="✅", style=discord.ButtonStyle.success, row=1)
    async def create_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.created:
            await interaction.response.send_message("This event was already created.", ephemeral=True)
            return
        if not self.draft.location_mode:
            await interaction.response.send_message("Configure **Location** before creating the event.", ephemeral=True)
            return
        if self.draft.location_mode != "none" and not self.draft.location_options:
            await interaction.response.send_message("Choose at least one location, or select **No Location**.", ephemeral=True)
            return
        if not self.draft.attendance_options:
            await interaction.response.send_message("Add at least one Attendance option.", ephemeral=True)
            return

        # Recompute start using the final timezone in case Schedule was never opened.
        try:
            self.draft.start_at_utc = parse_event_start(
                self.draft.date_text, self.draft.time_text, self.draft.timezone
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer()
        try:
            message = await create_event_from_draft(interaction, self.draft)
        except Exception as error:
            self.created = False
            print("\n================================\nERROR CREATING EVENT\n================================")
            traceback.print_exc()
            await interaction.followup.send(
                f"❌ I couldn't create the event.\n\n`{type(error).__name__}: {error}`\n\nThe full error was also printed in the bot terminal.",
                ephemeral=True,
            )
            return

        self.created = True
        success = discord.Embed(
            title="✅ Event Created",
            description=f"**{self.draft.title}** has been posted to the channel.",
            color=discord.Color.green(),
        )
        success.add_field(name="Event", value=message.jump_url, inline=False)
        await interaction.edit_original_response(embed=success, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Event creation cancelled.", embed=None, view=None)
        self.stop()


class BasicEventModal(discord.ui.Modal):
    def __init__(self, template_series_id: int | None = None):
        super().__init__(title="Create Event")
        self.template_series_id = template_series_id
        source = get_series(template_series_id) if template_series_id else None

        self.event_name = discord.ui.TextInput(
            placeholder="Saturday Snowboarding",
            default=(source["title"] if source else None),
            max_length=100,
        )
        self.description = discord.ui.TextInput(
            placeholder="Vote by Friday!",
            default=(source["description"] if source and source["description"] else None),
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=500,
        )
        self.date = discord.ui.TextInput(placeholder="09/25/2026", max_length=20)
        self.time = discord.ui.TextInput(
            placeholder="6:30 PM - 10:00 PM",
            default=(source["time_text"] if source else None),
            max_length=50,
        )
        self.add_item(discord.ui.Label(text="Event Name", component=self.event_name))
        self.add_item(discord.ui.Label(text="Description", component=self.description))
        self.add_item(discord.ui.Label(text="Date", component=self.date))
        self.add_item(discord.ui.Label(text="Time", component=self.time))

    async def on_submit(self, interaction: discord.Interaction):
        source = get_series(self.template_series_id) if self.template_series_id else None
        if source and source["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("That template belongs to a different server.", ephemeral=True)
            return
        tz_name = (source["timezone"] if source else DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE
        try:
            start_at = parse_event_start(self.date.value, self.time.value, tz_name)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        draft = EventDraft(
            creator_id=interaction.user.id,
            creator_name=interaction.user.display_name,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            title=self.event_name.value,
            description=self.description.value or "",
            date_text=self.date.value,
            time_text=self.time.value,
            repeat_rule=(source["repeat_rule"] if source else "none"),
            timezone=tz_name,
            start_at_utc=start_at,
        )

        if source:
            draft.location_mode = source["location_mode"]
            draft.vote_type = source["vote_type"] if "vote_type" in source.keys() else "single"
            draft.vote_visibility = source["vote_visibility"] if "vote_visibility" in source.keys() else "public"
            source_locations = get_locations(source["id"])
            draft.location_options = [r["label"] for r in source_locations]
            draft.location_addresses = {r["label"]: r["address"] for r in source_locations}
            draft.location_saved_ids = {r["label"]: r["saved_location_id"] for r in source_locations}
            groups = get_signup_groups(source["id"])
            for group in groups:
                labels = [r["label"] for r in get_signup_options(group["id"])]
                if group["selection_type"] == "single":
                    draft.attendance_options = labels
                else:
                    draft.additional_info_options = labels
            draft.image_bytes = source["image_blob"]
            draft.image_filename = source["image_filename"]
            draft.ping_role_id = source["ping_role_id"]
            draft.ping_role_name = source["ping_role_name"]
            draft.vote_cutoff_offset_minutes = source["vote_cutoff_offset_minutes"]
            draft.rsvp_cutoff_offset_minutes = source["rsvp_cutoff_offset_minutes"]
            draft.post_days_before = int(source["post_days_before"] or 5)
            draft.reminder_minutes = int(source["reminder_minutes"] or 120)
            draft.thread_enabled = bool(source["thread_enabled"])
            draft.capacity = int(source["capacity"]) if source["capacity"] else None
            if draft.vote_cutoff_offset_minutes is not None:
                draft.vote_cutoff_at_utc = start_at - timedelta(minutes=int(draft.vote_cutoff_offset_minutes))
            if draft.rsvp_cutoff_offset_minutes is not None:
                draft.rsvp_cutoff_at_utc = start_at - timedelta(minutes=int(draft.rsvp_cutoff_offset_minutes))

        setup_view = EventSetupView(draft)
        await interaction.response.send_message(
            embed=build_setup_embed(draft), view=setup_view, ephemeral=True
        )


class TemplateStartSelect(discord.ui.Select):
    def __init__(self, templates):
        options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["source_series_id"]), emoji="📋")
            for row in templates[:25]
        ]
        super().__init__(placeholder="Choose a saved template", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        source = get_series(int(self.values[0]))
        if not source or source["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("That template is not available in this server.", ephemeral=True)
            return
        await interaction.response.send_modal(BasicEventModal(template_series_id=int(self.values[0])))


class TemplateStartView(discord.ui.View):
    def __init__(self, templates):
        super().__init__(timeout=300)
        self.add_item(TemplateStartSelect(templates))

    @discord.ui.button(label="Blank Event", emoji="➕", style=discord.ButtonStyle.primary, row=1)
    async def blank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BasicEventModal())


# ==================================================
# EVENT CREATION / MESSAGE HELPERS
# ==================================================


async def resolve_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.DiscordException as exc:
            raise RuntimeError("Oak Tree Gather cannot access the event channel.") from exc
    return channel


def role_ping_content(event_or_series, guild: discord.Guild | None, channel) -> str | None:
    role_id = event_or_series["ping_role_id"]
    if not role_id or guild is None:
        return None
    role = guild.get_role(int(role_id))
    if role is None:
        return None
    member = guild.me
    if member is None:
        return None
    perms = channel.permissions_for(member)
    if role.mentionable or perms.mention_everyone:
        return role.mention
    return f"📣 **{role.name}**"


def validate_post_permissions(channel, guild: discord.Guild | None, has_image: bool, ping_role_id: int | None):
    if guild is None or guild.me is None:
        return
    permissions = channel.permissions_for(guild.me)
    missing = []
    if not permissions.view_channel:
        missing.append("View Channel")
    if not permissions.send_messages:
        missing.append("Send Messages")
    if not permissions.embed_links:
        missing.append("Embed Links")
    if not permissions.read_message_history:
        missing.append("Read Message History")
    if has_image and not permissions.attach_files:
        missing.append("Attach Files")

    if missing:
        raise RuntimeError("Oak Tree Gather is missing these channel permissions: " + ", ".join(missing))


def create_instance_record(series_id: int, start_at_utc: datetime, date_text: str | None = None) -> int:
    series = get_series(series_id)
    if series is None:
        raise RuntimeError("Event series not found")

    zone = get_zone(series["timezone"] or DEFAULT_TIMEZONE)
    local_start = start_at_utc.astimezone(zone)
    display_date = date_text or local_start.strftime("%m/%d/%Y")

    vote_cutoff = None
    rsvp_cutoff = None
    if series["vote_cutoff_offset_minutes"] is not None:
        vote_cutoff = start_at_utc - timedelta(minutes=int(series["vote_cutoff_offset_minutes"]))
    if series["rsvp_cutoff_offset_minutes"] is not None:
        rsvp_cutoff = start_at_utc - timedelta(minutes=int(series["rsvp_cutoff_offset_minutes"]))

    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO event_instances (
                series_id, date_text, occurrence_start_at, vote_cutoff_at, rsvp_cutoff_at,
                location_finalized, vote_reminder_sent, rsvp_reminder_sent,
                vote_closed_announced, rsvp_closed_announced, created_at
            )
            VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 0, ?)
            """,
            (
                series_id,
                display_date,
                iso_utc(start_at_utc),
                iso_utc(vote_cutoff),
                iso_utc(rsvp_cutoff),
                iso_utc(datetime.now(UTC)),
            ),
        )
        return cursor.lastrowid


async def post_instance_message(instance_id: int, ping_role: bool = True):
    event = get_event(instance_id)
    if event is None:
        raise RuntimeError("Event not found")

    channel = await resolve_channel(event["channel_id"])
    guild = bot.get_guild(event["guild_id"])
    validate_post_permissions(channel, guild, bool(event["image_blob"]), event["ping_role_id"] if ping_role else None)

    embed = build_embed(instance_id)
    view = EventView(instance_id)
    content = role_ping_content(event, guild, channel) if ping_role else None
    allowed_mentions = discord.AllowedMentions(everyone=False, users=False, roles=True, replied_user=False)

    event_file = None
    if event["image_blob"] and event["image_filename"]:
        event_file = discord.File(io.BytesIO(event["image_blob"]), filename=event["image_filename"])
        embed.set_image(url="attachment://" + event["image_filename"])

    if event_file:
        message = await channel.send(
            content=content,
            embed=embed,
            view=view,
            file=event_file,
            allowed_mentions=allowed_mentions,
        )
    else:
        message = await channel.send(
            content=content,
            embed=embed,
            view=view,
            allowed_mentions=allowed_mentions,
        )

    image_url = message.attachments[0].url if message.attachments else event["image_url"]

    with get_db() as db:
        db.execute("UPDATE event_instances SET message_id = ? WHERE id = ?", (message.id, instance_id))
        if image_url:
            db.execute("UPDATE event_series SET image_url = ? WHERE id = ?", (image_url, event["series_id"]))

    # Re-edit so the permanent CDN URL is inside the embed, keeping the image visually attached to the card.
    if image_url:
        await message.edit(embed=build_embed(instance_id), view=EventView(instance_id))

    # Each occurrence gets its own discussion thread when enabled.
    if event["thread_enabled"] and not event["thread_id"]:
        try:
            thread_name = f"{event['title']} — {format_event_date(event['date_text'])}"[:100]
            thread = await message.create_thread(name=thread_name, auto_archive_duration=1440)
            with get_db() as db:
                db.execute("UPDATE event_instances SET thread_id = ? WHERE id = ?", (thread.id, instance_id))
        except discord.DiscordException as exc:
            print(f"Could not create discussion thread for event {instance_id}: {exc}")

    await message.edit(embed=build_embed(instance_id), view=EventView(instance_id))
    return message


async def create_event_from_draft(interaction: discord.Interaction, draft: EventDraft):
    start_at = draft.start_at_utc or parse_event_start(draft.date_text, draft.time_text, draft.timezone)
    local_start = start_at.astimezone(get_zone(draft.timezone))

    channel = await resolve_channel(draft.channel_id)
    guild = bot.get_guild(draft.guild_id)
    validate_post_permissions(channel, guild, bool(draft.image_bytes), draft.ping_role_id)

    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO event_series (
                guild_id, channel_id, creator_id, creator_name,
                title, description, date_text, time_text, repeat_rule, location_mode, vote_type, vote_visibility,
                image_blob, image_filename, timezone, start_time_local, anchor_day,
                ping_role_id, ping_role_name, vote_cutoff_offset_minutes,
                rsvp_cutoff_offset_minutes, post_days_before, reminder_minutes, active, thread_enabled, capacity
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                draft.guild_id,
                draft.channel_id,
                draft.creator_id,
                draft.creator_name,
                draft.title,
                draft.description,
                draft.date_text,
                draft.time_text,
                draft.repeat_rule,
                draft.location_mode,
                draft.vote_type,
                draft.vote_visibility,
                draft.image_bytes,
                draft.image_filename,
                draft.timezone,
                local_start.strftime("%H:%M"),
                local_start.day,
                draft.ping_role_id,
                draft.ping_role_name,
                draft.vote_cutoff_offset_minutes,
                draft.rsvp_cutoff_offset_minutes,
                draft.post_days_before,
                draft.reminder_minutes,
                1 if draft.thread_enabled else 0,
                draft.capacity,
            ),
        )
        series_id = cursor.lastrowid

        for position, label in enumerate(draft.location_options):
            db.execute(
                """INSERT INTO location_options
                   (series_id, label, position, address, saved_location_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    series_id,
                    label,
                    position,
                    draft.location_addresses.get(label),
                    draft.location_saved_ids.get(label),
                ),
            )

        cursor = db.execute(
            "INSERT INTO signup_groups (series_id, name, selection_type, position) VALUES (?, ?, 'single', 0)",
            (series_id, "👥 Attendance"),
        )
        attendance_group_id = cursor.lastrowid
        for position, label in enumerate(draft.attendance_options):
            db.execute(
                "INSERT INTO signup_options (group_id, label, position) VALUES (?, ?, ?)",
                (attendance_group_id, label, position),
            )

        if draft.additional_info_options:
            cursor = db.execute(
                "INSERT INTO signup_groups (series_id, name, selection_type, position) VALUES (?, ?, 'multi', 1)",
                (series_id, "ℹ️ Additional Info"),
            )
            additional_group_id = cursor.lastrowid
            for position, label in enumerate(draft.additional_info_options):
                db.execute(
                    "INSERT INTO signup_options (group_id, label, position) VALUES (?, ?, ?)",
                    (additional_group_id, label, position),
                )

    instance_id = create_instance_record(series_id, start_at, date_text=draft.date_text)
    # First instance uses exact cutoff timestamps from the wizard, not merely rounded offsets.
    with get_db() as db:
        db.execute(
            "UPDATE event_instances SET vote_cutoff_at = ?, rsvp_cutoff_at = ? WHERE id = ?",
            (iso_utc(draft.vote_cutoff_at_utc), iso_utc(draft.rsvp_cutoff_at_utc), instance_id),
        )

        # Set-location events can pre-decide the first occurrence during creation.
        # Recurring future occurrences still start fresh and can be chosen in /event manage.
        if draft.location_mode == "set" and draft.initial_location_label:
            selected = db.execute(
                "SELECT id FROM location_options WHERE series_id=? AND label=? LIMIT 1",
                (series_id, draft.initial_location_label),
            ).fetchone()
            if selected:
                db.execute(
                    "UPDATE event_instances SET selected_location_id=?, location_finalized=1 WHERE id=?",
                    (selected["id"], instance_id),
                )

    return await post_instance_message(instance_id, ping_role=True)


async def refresh_event_message(instance_id: int):
    event = get_event(instance_id)
    if not event or not event["message_id"]:
        return
    try:
        channel = await resolve_channel(event["channel_id"])
        message = await channel.fetch_message(event["message_id"])
        await message.edit(embed=build_embed(instance_id), view=EventView(instance_id))
    except (discord.NotFound, discord.Forbidden):
        pass


async def send_event_notice(instance_id: int, text: str, ping_role: bool = False):
    event = get_event(instance_id)
    if not event:
        return
    channel = await resolve_channel(event["channel_id"])
    guild = bot.get_guild(event["guild_id"])
    content_parts = []
    if ping_role:
        ping = role_ping_content(event, guild, channel)
        if ping:
            content_parts.append(ping)
    content_parts.append(text)
    await channel.send(
        "\n".join(content_parts),
        allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True),
    )


# ==================================================
# BACKGROUND SCHEDULER
# ==================================================


async def process_cutoffs_and_reminders():
    now = datetime.now(UTC)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT i.id
            FROM event_instances i
            JOIN event_series s ON s.id = i.series_id
            WHERE i.status = 'active' AND s.active = 1
            """
        ).fetchall()

    for row in rows:
        instance_id = row["id"]
        event = get_event(instance_id)
        if not event:
            continue

        reminder_delta = timedelta(minutes=int(event["reminder_minutes"] or 0))
        vote_cutoff = from_iso(event["vote_cutoff_at"])
        rsvp_cutoff = from_iso(event["rsvp_cutoff_at"])

        # Location vote reminder / cutoff
        if event["location_mode"] == "vote" and vote_cutoff:
            if (
                int(event["reminder_minutes"] or 0) > 0
                and not event["vote_reminder_sent"]
                and now >= vote_cutoff - reminder_delta
                and now < vote_cutoff
            ):
                await send_event_notice(
                    instance_id,
                    f"⏰ Location voting for **{event['title']}** closes {discord_timestamp(vote_cutoff, 'R')}.",
                    ping_role=True,
                )
                with get_db() as db:
                    db.execute("UPDATE event_instances SET vote_reminder_sent = 1 WHERE id = ?", (instance_id,))

            if now >= vote_cutoff and not event["vote_closed_announced"]:
                vote_counts = get_location_vote_counts(instance_id)
                highest = max((r["vote_count"] for r in vote_counts), default=0)
                leaders = [r for r in vote_counts if r["vote_count"] == highest and highest > 0]

                if event["location_finalized"] and event["selected_location_id"]:
                    selected = get_location(event["series_id"], event["selected_location_id"])
                    notice = (
                        f"🔒 Location voting closed. Final location: **{selected['label']}**."
                        if selected
                        else "🔒 Location voting closed."
                    )
                elif len(leaders) == 1:
                    with get_db() as db:
                        db.execute(
                            "UPDATE event_instances SET selected_location_id = ?, location_finalized = 1 WHERE id = ?",
                            (leaders[0]["id"], instance_id),
                        )
                    notice = f"🔒 Location voting closed. **{leaders[0]['label']}** was automatically finalized."
                elif len(leaders) > 1:
                    notice = "⚠️ Location voting closed with a tie. The organizer needs to finalize the location."
                else:
                    notice = "⚠️ Location voting closed with no votes. The organizer needs to finalize the location."

                with get_db() as db:
                    db.execute("UPDATE event_instances SET vote_closed_announced = 1 WHERE id = ?", (instance_id,))
                await refresh_event_message(instance_id)
                await send_event_notice(instance_id, notice, ping_role=False)

        # RSVP reminder / cutoff
        if rsvp_cutoff:
            if (
                int(event["reminder_minutes"] or 0) > 0
                and not event["rsvp_reminder_sent"]
                and now >= rsvp_cutoff - reminder_delta
                and now < rsvp_cutoff
            ):
                await send_event_notice(
                    instance_id,
                    f"⏰ RSVPs for **{event['title']}** close {discord_timestamp(rsvp_cutoff, 'R')}.",
                    ping_role=True,
                )
                with get_db() as db:
                    db.execute("UPDATE event_instances SET rsvp_reminder_sent = 1 WHERE id = ?", (instance_id,))

            if now >= rsvp_cutoff and not event["rsvp_closed_announced"]:
                with get_db() as db:
                    db.execute("UPDATE event_instances SET rsvp_closed_announced = 1 WHERE id = ?", (instance_id,))
                await refresh_event_message(instance_id)
                await send_event_notice(instance_id, f"🔒 RSVPs are now closed for **{event['title']}**.", ping_role=False)


async def process_thread_archives():
    now = datetime.now(UTC)
    with get_db() as db:
        rows = db.execute(
            """SELECT i.id FROM event_instances i
               WHERE i.thread_id IS NOT NULL AND i.thread_archived=0"""
        ).fetchall()

    for row in rows:
        event = get_event(row["id"])
        if not event or not event["thread_id"]:
            continue
        try:
            end_at = parse_event_end(
                event["date_text"], event["time_text"], event["timezone"] or DEFAULT_TIMEZONE
            )
        except Exception:
            start_at = from_iso(event["occurrence_start_at"])
            end_at = start_at + timedelta(hours=8) if start_at else None
        # Archive the discussion thread as soon as the event ends.
        if not end_at or now < end_at:
            continue
        try:
            thread = bot.get_channel(int(event["thread_id"]))
            if thread is None:
                thread = await bot.fetch_channel(int(event["thread_id"]))
            if isinstance(thread, discord.Thread) and not thread.archived:
                await thread.edit(archived=True, reason="Oak Tree Gather event ended")
            with get_db() as db:
                db.execute("UPDATE event_instances SET thread_archived=1 WHERE id=?", (event["instance_id"],))
        except discord.DiscordException as exc:
            print(f"Could not archive thread for event {event['instance_id']}: {exc}")


async def process_recurring_series():
    now = datetime.now(UTC)
    with get_db() as db:
        series_rows = db.execute(
            "SELECT id FROM event_series WHERE active = 1 AND repeat_rule != 'none'"
        ).fetchall()

    for row in series_rows:
        series = get_series(row["id"])
        if not series:
            continue

        with get_db() as db:
            latest = db.execute(
                """
                SELECT occurrence_start_at
                FROM event_instances
                WHERE series_id = ? AND occurrence_start_at IS NOT NULL
                ORDER BY occurrence_start_at DESC
                LIMIT 1
                """,
                (series["id"],),
            ).fetchone()

        if not latest or not latest["occurrence_start_at"]:
            continue

        latest_start = from_iso(latest["occurrence_start_at"])
        if latest_start is None:
            continue

        # Skip missed historical occurrences and find the next future occurrence.
        next_local = recurrence_next_local(
            latest_start,
            series["repeat_rule"],
            series["timezone"] or DEFAULT_TIMEZONE,
            int(series["anchor_day"] or latest_start.astimezone(get_zone(series["timezone"] or DEFAULT_TIMEZONE)).day),
        )
        next_start = next_local.astimezone(UTC)

        safety = 0
        while next_start <= now and safety < 60:
            next_local = recurrence_next_local(
                next_start,
                series["repeat_rule"],
                series["timezone"] or DEFAULT_TIMEZONE,
                int(series["anchor_day"] or next_local.day),
            )
            next_start = next_local.astimezone(UTC)
            safety += 1

        post_at = next_start - timedelta(days=int(series["post_days_before"] or 0))
        if now < post_at:
            continue

        with get_db() as db:
            exists = db.execute(
                "SELECT 1 FROM event_instances WHERE series_id = ? AND occurrence_start_at = ?",
                (series["id"], iso_utc(next_start)),
            ).fetchone()
        if exists:
            continue

        instance_id = create_instance_record(series["id"], next_start)
        await post_instance_message(instance_id, ping_role=True)
        print(f"Created recurring occurrence {instance_id} for series {series['id']}")



def instance_id_from_message_id(message_id: int) -> int | None:
    with get_db() as db:
        row = db.execute("SELECT id FROM event_instances WHERE message_id=?", (message_id,)).fetchone()
        return int(row["id"]) if row else None


class DuplicateEventModal(discord.ui.Modal):
    def __init__(self, instance_id: int):
        event = get_event(instance_id)
        super().__init__(title="Duplicate Event")
        self.instance_id = instance_id
        self.date = discord.ui.TextInput(placeholder="10/01/2026", max_length=20)
        self.time = discord.ui.TextInput(default=(event["time_text"] if event else None), max_length=50)
        self.add_item(discord.ui.Label(text="New Date", component=self.date))
        self.add_item(discord.ui.Label(text="Time", component=self.time))

    async def on_submit(self, interaction: discord.Interaction):
        event = get_event(self.instance_id)
        if not event or event["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Event not found in this server.", ephemeral=True)
            return
        try:
            start = parse_event_start(self.date.value, self.time.value, event["timezone"] or DEFAULT_TIMEZONE)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            series_id = copy_series_configuration(
                event["series_id"],
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                creator_id=interaction.user.id,
                creator_name=interaction.user.display_name,
                title=event["title"],
                description=event["description"] or "",
                date_text=self.date.value,
                time_text=self.time.value,
                repeat_rule="none",
            )
            instance_id = create_instance_record(series_id, start, date_text=self.date.value)
            message = await post_instance_message(instance_id, ping_role=True)
            await interaction.followup.send(f"✅ Duplicated event: {message.jump_url}", ephemeral=True)
        except Exception as exc:
            traceback.print_exc()
            await interaction.followup.send(f"❌ Could not duplicate event: `{exc}`", ephemeral=True)


class TemplateNameModal(discord.ui.Modal):
    def __init__(self, instance_id: int):
        super().__init__(title="Save Event Template")
        self.instance_id = instance_id
        self.name = discord.ui.TextInput(placeholder="MTG Night", max_length=80)
        self.add_item(discord.ui.Label(text="Template Name", component=self.name))

    async def on_submit(self, interaction: discord.Interaction):
        event = get_event(self.instance_id)
        if not event or event["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Event not found in this server.", ephemeral=True)
            return
        save_template(interaction.guild_id, interaction.user.id, self.name.value, event["series_id"])
        await interaction.response.send_message(f"✅ Saved template **{self.name.value}**.", ephemeral=True)


class ExistingCapacityModal(discord.ui.Modal, title="Capacity & Waitlist"):
    def __init__(self, instance_id: int):
        super().__init__()
        self.instance_id = instance_id
        event = get_event(instance_id)
        self.capacity_input = discord.ui.TextInput(
            placeholder="Leave blank for unlimited",
            default=(str(event["capacity"]) if event and event["capacity"] else None),
            required=False,
            max_length=3,
        )
        self.add_item(
            discord.ui.Label(
                text="Event Capacity",
                description="Guests count toward capacity. Full events automatically use a FIFO waitlist.",
                component=self.capacity_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        event = get_event(self.instance_id)
        if not event:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        raw = self.capacity_input.value.strip()
        capacity = None
        if raw:
            try:
                capacity = int(raw)
            except ValueError:
                await interaction.response.send_message("Capacity must be a whole number.", ephemeral=True)
                return
            if not 1 <= capacity <= 999:
                await interaction.response.send_message("Capacity must be between 1 and 999.", ephemeral=True)
                return
        with get_db() as db:
            db.execute("UPDATE event_series SET capacity=? WHERE id=?", (capacity, event["series_id"]))
        promoted = promote_waitlist(self.instance_id)
        await refresh_event_message(self.instance_id)
        msg = "✅ Capacity updated."
        if promoted:
            msg += " Promoted from waitlist: " + ", ".join(promoted)
        await interaction.response.send_message(msg, ephemeral=True)


class SetCurrentLocationModal(discord.ui.Modal, title="Set Event Location"):
    def __init__(self, instance_id: int):
        super().__init__()
        self.instance_id = instance_id
        event = get_event(instance_id)
        if event is None:
            raise RuntimeError("Event not found")

        locations = get_locations(event["series_id"])
        if not locations:
            raise RuntimeError("This event has no configured locations")

        self.location_select = discord.ui.Select(
            placeholder="Choose the location for this occurrence",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=row["label"][:100],
                    value=str(row["id"]),
                    default=(event["selected_location_id"] == row["id"]),
                )
                for row in locations[:25]
            ],
        )
        self.add_item(discord.ui.Label(text="Location", component=self.location_select))

    async def on_submit(self, interaction: discord.Interaction):
        event = get_event(self.instance_id)
        if not event or event["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Event not found in this server.", ephemeral=True)
            return
        if interaction.user.id != event["creator_id"] and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "Only the organizer or a server manager can set the location.",
                ephemeral=True,
            )
            return

        location_id = int(self.location_select.values[0])
        location = get_location(event["series_id"], location_id)
        if not location:
            await interaction.response.send_message("Location not found.", ephemeral=True)
            return

        with get_db() as db:
            db.execute(
                "UPDATE event_instances SET selected_location_id=?, location_finalized=1 WHERE id=?",
                (location_id, self.instance_id),
            )
        await refresh_event_message(self.instance_id)
        await interaction.response.send_message(
            f"📍 Location set to **{location['label']}**.",
            ephemeral=True,
        )


class ManageSetLocationButton(discord.ui.Button):
    def __init__(self, instance_id: int):
        super().__init__(
            label="Set Location",
            emoji="📍",
            style=discord.ButtonStyle.primary,
            row=1,
        )
        self.instance_id = instance_id

    async def callback(self, interaction: discord.Interaction):
        event = get_event(self.instance_id)
        if not event or event["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Event not found in this server.", ephemeral=True)
            return
        if interaction.user.id != event["creator_id"] and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "Only the organizer or a server manager can set the location.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(SetCurrentLocationModal(self.instance_id))


class ManageEventView(discord.ui.View):
    def __init__(self, instance_id: int):
        super().__init__(timeout=600)
        self.instance_id = instance_id
        event = get_event(instance_id)
        if event and event["location_mode"] == "set":
            self.add_item(ManageSetLocationButton(instance_id))
        if event and event["thread_id"]:
            self.add_item(discord.ui.Button(
                label="Discussion", emoji="💬", style=discord.ButtonStyle.link,
                url=f"https://discord.com/channels/{event['guild_id']}/{event['thread_id']}", row=2
            ))

    async def interaction_check(self, interaction: discord.Interaction):
        event = get_event(self.instance_id)
        if not event or event["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Event not found in this server.", ephemeral=True)
            return False
        if interaction.user.id != event["creator_id"] and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only the organizer or a server manager can manage this event.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Edit", emoji="✏️", style=discord.ButtonStyle.secondary, row=0)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EditEventModal(self.instance_id))

    @discord.ui.button(label="Duplicate", emoji="📑", style=discord.ButtonStyle.secondary, row=0)
    async def duplicate(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DuplicateEventModal(self.instance_id))

    @discord.ui.button(label="Save Template", emoji="📋", style=discord.ButtonStyle.secondary, row=0)
    async def template(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TemplateNameModal(self.instance_id))

    @discord.ui.button(label="Capacity", emoji="🎟️", style=discord.ButtonStyle.secondary, row=0)
    async def capacity(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ExistingCapacityModal(self.instance_id))

    @discord.ui.button(label="Finalize Location", emoji="🔒", style=discord.ButtonStyle.primary, row=1)
    async def finalize(self, interaction: discord.Interaction, button: discord.ui.Button):
        event = get_event(self.instance_id)
        if not event or event["location_mode"] != "vote":
            await interaction.response.send_message("This event does not use location voting.", ephemeral=True)
            return
        await interaction.response.send_modal(FinalizeLocationModal(self.instance_id))

    @discord.ui.button(label="Calendar (.ics)", emoji="🗓️", style=discord.ButtonStyle.secondary, row=1)
    async def ics(self, interaction: discord.Interaction, button: discord.ui.Button):
        event = get_event(self.instance_id)
        if not event:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        filename = re.sub(r"[^A-Za-z0-9_-]+", "_", event["title"]).strip("_") or "event"
        await interaction.response.send_message(
            file=discord.File(io.BytesIO(build_ics(event)), filename=f"{filename}.ics"),
            ephemeral=True,
        )

    @discord.ui.button(label="Cancel", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "What would you like to cancel?", view=CancelConfirmView(self.instance_id), ephemeral=True
        )


class TemplateRenameModal(discord.ui.Modal):
    def __init__(self, template_id: int):
        super().__init__(title="Rename Template")
        self.template_id = template_id
        template = get_template(template_id)
        self.name = discord.ui.TextInput(
            placeholder="MTG Night",
            default=(template["name"] if template else None),
            max_length=80,
        )
        self.add_item(discord.ui.Label(text="Template Name", component=self.name))

    async def on_submit(self, interaction: discord.Interaction):
        template = get_template(self.template_id)
        if not template or template["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Template not found in this server.", ephemeral=True)
            return
        if interaction.user.id != template["creator_id"] and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only the template creator or a server manager can rename it.", ephemeral=True)
            return
        try:
            rename_template(self.template_id, self.name.value)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Template renamed to **{self.name.value.strip()}**.", ephemeral=True)


class TemplateUpdateEventSelect(discord.ui.Select):
    def __init__(self, template_id: int, rows):
        self.template_id = template_id
        options = [
            discord.SelectOption(
                label=row["title"][:100],
                description=format_event_date(row["date_text"])[:100],
                value=str(row["id"]),
                emoji="📅",
            )
            for row in rows
        ]
        super().__init__(
            placeholder="Choose the event configuration to save",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        template = get_template(self.template_id)
        if not template or template["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Template not found in this server.", ephemeral=True)
            return
        if interaction.user.id != template["creator_id"] and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only the template creator or a server manager can update it.", ephemeral=True)
            return
        event = get_event(int(self.values[0]))
        if not event or event["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Event not found in this server.", ephemeral=True)
            return
        if interaction.user.id != event["creator_id"] and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("You can only update from an event you manage.", ephemeral=True)
            return
        update_template_source(self.template_id, event["series_id"])
        await interaction.response.edit_message(
            content=f"✅ **{template['name']}** now uses the configuration from **{event['title']}**.",
            view=None,
        )


class TemplateUpdateEventView(discord.ui.View):
    def __init__(self, template_id: int, rows):
        super().__init__(timeout=600)
        self.add_item(TemplateUpdateEventSelect(template_id, rows))


class TemplateDeleteConfirmView(discord.ui.View):
    def __init__(self, template_id: int):
        super().__init__(timeout=120)
        self.template_id = template_id

    @discord.ui.button(label="Delete Template", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        template = get_template(self.template_id)
        if not template or template["guild_id"] != interaction.guild_id:
            await interaction.response.edit_message(content="Template not found in this server.", view=None)
            return
        if interaction.user.id != template["creator_id"] and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only the template creator or a server manager can delete it.", ephemeral=True)
            return
        name = template["name"]
        delete_template(self.template_id)
        await interaction.response.edit_message(content=f"🗑️ Deleted template **{name}**.", view=None)

    @discord.ui.button(label="Keep Template", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Template kept.", view=None)


class TemplateManageView(discord.ui.View):
    def __init__(self, template_id: int):
        super().__init__(timeout=600)
        self.template_id = template_id

    async def interaction_check(self, interaction: discord.Interaction):
        template = get_template(self.template_id)
        if not template or template["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Template not found in this server.", ephemeral=True)
            return False
        if interaction.user.id != template["creator_id"] and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "Only the template creator or a server manager can modify this template.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Rename", emoji="✏️", style=discord.ButtonStyle.secondary)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TemplateRenameModal(self.template_id))

    @discord.ui.button(label="Update", emoji="🔄", style=discord.ButtonStyle.primary)
    async def update(self, interaction: discord.Interaction, button: discord.ui.Button):
        can_manage = bool(interaction.user.guild_permissions.manage_guild)
        rows = get_manageable_instances(interaction.guild_id, interaction.user.id, can_manage)
        if not rows:
            await interaction.response.send_message(
                "You do not have an upcoming event to update this template from.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Choose the event whose current configuration should replace this template:",
            view=TemplateUpdateEventView(self.template_id, rows),
            ephemeral=True,
        )

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        template = get_template(self.template_id)
        await interaction.response.send_message(
            f"Delete **{template['name']}**? This does not delete any events.",
            view=TemplateDeleteConfirmView(self.template_id),
            ephemeral=True,
        )


class TemplateManageSelect(discord.ui.Select):
    def __init__(self, rows):
        options = [
            discord.SelectOption(
                label=row["name"][:100],
                value=str(row["id"]),
                emoji="📋",
            )
            for row in rows[:25]
        ]
        super().__init__(placeholder="Choose a template to manage", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        template = get_template(int(self.values[0]))
        if not template or template["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Template not found in this server.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=f"**{template['name']}**\nChoose an action:",
            view=TemplateManageView(template["id"]),
        )


class TemplateManageSelectView(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=600)
        self.add_item(TemplateManageSelect(rows))


class ManageEventSelect(discord.ui.Select):
    def __init__(self, rows):
        options = [
            discord.SelectOption(
                label=row["title"][:100],
                description=format_event_date(row["date_text"])[:100],
                value=str(row["id"]),
                emoji="📅",
            )
            for row in rows
        ]
        super().__init__(placeholder="Choose an event to manage", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        instance_id = int(self.values[0])
        event = get_event(instance_id)
        if not event or event["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Event not found in this server.", ephemeral=True)
            return
        if interaction.user.id != event["creator_id"] and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("You do not have permission to manage this event.", ephemeral=True)
            return
        embed = build_embed(instance_id)
        await interaction.response.edit_message(
            content="Organizer controls:", embed=embed, view=ManageEventView(instance_id)
        )


class ManageEventSelectView(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=600)
        self.add_item(ManageEventSelect(rows))


# ==================================================
# SAVED LOCATION BOOK
# ==================================================


def saved_locations_embed(guild_id: int) -> discord.Embed:
    rows = get_saved_locations(guild_id)
    embed = discord.Embed(
        title="📍 Saved Locations",
        description=(
            "Addresses are stored privately for event setup. "
            "Public event cards reveal an address only after that location is finalized."
        ),
        color=discord.Color.blurple(),
    )
    if rows:
        embed.add_field(
            name="Places",
            value="\n".join(f"• **{row['name']}**" for row in rows[:25]),
            inline=False,
        )
    else:
        embed.add_field(
            name="Places",
            value="No saved locations yet.",
            inline=False,
        )
    embed.set_footer(text="Use Add Location to store a name + address.")
    return embed


class SavedLocationModal(discord.ui.Modal):
    def __init__(self, guild_id: int, creator_id: int, location_id: int | None = None):
        super().__init__(title=("Edit Saved Location" if location_id else "Add Saved Location"))
        self.guild_id = guild_id
        self.creator_id = creator_id
        self.location_id = location_id
        existing = get_saved_location(location_id) if location_id else None

        self.name_input = discord.ui.TextInput(
            placeholder="Ryan's House",
            default=(existing["name"] if existing else None),
            max_length=60,
        )
        self.address_input = discord.ui.TextInput(
            placeholder="1234 Example St, Denver, CO 80202",
            default=(existing["address"] if existing and existing["address"] else None),
            required=False,
            max_length=300,
        )
        self.add_item(
            discord.ui.Label(
                text="Location Name",
                description="This is the only part shown while choosing/voting.",
                component=self.name_input,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Address",
                description="Optional. Revealed publicly only after this location is finalized.",
                component=self.address_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if self.location_id:
                existing = get_saved_location(self.location_id)
                if not existing or existing["guild_id"] != interaction.guild_id or self.guild_id != interaction.guild_id:
                    await interaction.response.send_message("Saved location not found in this server.", ephemeral=True)
                    return
                update_saved_location(
                    self.location_id,
                    self.name_input.value,
                    self.address_input.value,
                )
                message = f"✅ Updated **{self.name_input.value.strip()}**."
            else:
                save_saved_location(
                    self.guild_id,
                    self.creator_id,
                    self.name_input.value,
                    self.address_input.value,
                )
                message = f"✅ Saved **{self.name_input.value.strip()}**."
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.edit_message(
            content=message,
            embed=saved_locations_embed(self.guild_id),
            view=SavedLocationBookView(self.guild_id),
        )


class SavedLocationDeleteConfirmView(discord.ui.View):
    def __init__(self, guild_id: int, location_id: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.location_id = location_id

    @discord.ui.button(label="Delete Saved Location", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = get_saved_location(self.location_id)
        if not row or row["guild_id"] != interaction.guild_id or self.guild_id != interaction.guild_id:
            await interaction.response.send_message("Saved location not found in this server.", ephemeral=True)
            return
        if interaction.user.id != row["creator_id"] and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only the person who saved this location or a server manager can delete it.", ephemeral=True)
            return
        delete_saved_location(self.location_id)
        await interaction.response.edit_message(
            content=f"🗑️ Deleted **{row['name'] if row else 'saved location'}**. Existing events keep their address snapshot.",
            embed=saved_locations_embed(self.guild_id),
            view=SavedLocationBookView(self.guild_id),
        )

    @discord.ui.button(label="Keep", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="No changes made.",
            embed=saved_locations_embed(self.guild_id),
            view=SavedLocationBookView(self.guild_id),
        )


class SavedLocationManageView(discord.ui.View):
    def __init__(self, guild_id: int, location_id: int):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.location_id = location_id

    async def interaction_check(self, interaction: discord.Interaction):
        row = get_saved_location(self.location_id)
        if not row or row["guild_id"] != interaction.guild_id or self.guild_id != interaction.guild_id:
            await interaction.response.send_message("Saved location not found in this server.", ephemeral=True)
            return False
        if interaction.user.id != row["creator_id"] and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "Only the person who saved this location or a server manager can edit/delete it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Edit", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            SavedLocationModal(self.guild_id, interaction.user.id, self.location_id)
        )

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = get_saved_location(self.location_id)
        await interaction.response.edit_message(
            content=f"Delete **{row['name']}** from Saved Locations?",
            embed=None,
            view=SavedLocationDeleteConfirmView(self.guild_id, self.location_id),
        )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=None,
            embed=saved_locations_embed(self.guild_id),
            view=SavedLocationBookView(self.guild_id),
        )


class SavedLocationSelect(discord.ui.Select):
    def __init__(self, guild_id: int, rows):
        self.guild_id = guild_id
        options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]), emoji="📍")
            for row in rows[:25]
        ]
        super().__init__(
            placeholder="Choose a saved location",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        row = get_saved_location(int(self.values[0]))
        if not row or row["guild_id"] != interaction.guild_id or self.guild_id != interaction.guild_id:
            await interaction.response.send_message("Saved location not found in this server.", ephemeral=True)
            return
        can_manage = (
            interaction.user.id == row["creator_id"]
            or interaction.user.guild_permissions.manage_guild
        )
        if can_manage:
            address = row["address"] or "No address saved"
            description = f"**Address**\n{address}"
            view = SavedLocationManageView(self.guild_id, row["id"])
        else:
            description = (
                "Address hidden until this place is finalized for an event. "
                "Only the person who saved it or a server manager can edit it."
            )
            view = SavedLocationBookView(self.guild_id)
        embed = discord.Embed(
            title=f"📍 {row['name']}",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="This management screen is private to you.")
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=view,
        )


class SavedLocationBookView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        rows = get_saved_locations(guild_id)
        if rows:
            self.add_item(SavedLocationSelect(guild_id, rows))

    @discord.ui.button(label="Add Location", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def add_location(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            SavedLocationModal(self.guild_id, interaction.user.id)
        )


# ==================================================
# BOT
# ==================================================


class GatherBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        initialize_database()

        # Restore persistent public event controls.
        with get_db() as db:
            rows = db.execute(
                """
                SELECT id, message_id
                FROM event_instances
                WHERE message_id IS NOT NULL AND status = 'active'
                """
            ).fetchall()
        for row in rows:
            try:
                self.add_view(EventView(row["id"]), message_id=row["message_id"])
            except Exception as exc:
                # A malformed legacy/test event must never prevent the bot
                # from starting. Log it and continue restoring other events.
                print(
                    f"Warning: could not restore event instance {row['id']}: "
                    f"{type(exc).__name__}: {exc}"
                )

        # Production uses GLOBAL application commands so the same Railway
        # process works in every server that installs Oak Tree Gather.
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} global command(s).")

        # If DISCORD_GUILD_ID is still configured from the old single-server
        # setup, remove the stale guild-scoped command copy. Global commands
        # remain available in that server after Discord refreshes them.
        if TEST_GUILD is not None:
            try:
                self.tree.clear_commands(guild=TEST_GUILD)
                await self.tree.sync(guild=TEST_GUILD)
                print(f"Cleared legacy guild-scoped commands for {GUILD_ID}.")
            except Exception as exc:
                print(f"Warning: could not clear legacy guild commands: {exc}")

        if not self.scheduler_loop.is_running():
            self.scheduler_loop.start()

    @tasks.loop(seconds=30)
    async def scheduler_loop(self):
        try:
            await process_cutoffs_and_reminders()
            await process_recurring_series()
            await process_thread_archives()
        except Exception:
            print("\n================================\nSCHEDULER ERROR\n================================")
            traceback.print_exc()

    @scheduler_loop.before_loop
    async def before_scheduler(self):
        await self.wait_until_ready()


bot = GatherBot()


@bot.event
async def on_ready():
    print("------------------------------------")
    print(f"Logged in as: {bot.user}")
    print(f"Bot ID: {bot.user.id}")
    print(f"Default timezone: {DEFAULT_TIMEZONE}")
    print("------------------------------------")


# ==================================================
# SLASH COMMANDS
# ==================================================


event_group = app_commands.Group(name="event", description="Create and manage events")


@event_group.command(name="create", description="Create a new event")
@app_commands.guild_only()
async def event_create(interaction: discord.Interaction):
    templates = get_templates(interaction.guild_id)
    if not templates:
        await interaction.response.send_modal(BasicEventModal())
        return
    await interaction.response.send_message(
        "Start with a saved template, or create a blank event:",
        view=TemplateStartView(templates),
        ephemeral=True,
    )


@event_group.command(name="locations", description="Manage saved location names and directions addresses")
@app_commands.guild_only()
async def event_locations(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=saved_locations_embed(interaction.guild_id),
        view=SavedLocationBookView(interaction.guild_id),
        ephemeral=True,
    )


@event_group.command(name="manage", description="Manage one of your upcoming events")
@app_commands.guild_only()
async def event_manage(interaction: discord.Interaction):
    can_manage = bool(interaction.user.guild_permissions.manage_guild)
    rows = get_manageable_instances(interaction.guild_id, interaction.user.id, can_manage)
    if not rows:
        await interaction.response.send_message("You do not have any upcoming events to manage.", ephemeral=True)
        return
    await interaction.response.send_message(
        "Choose an event:",
        view=ManageEventSelectView(rows),
        ephemeral=True,
    )


@event_group.command(name="template_save", description="Save an existing event as a reusable template")
@app_commands.guild_only()
@app_commands.describe(message_id="Message ID of the event", name="Template name")
async def event_template_save(interaction: discord.Interaction, message_id: str, name: str):
    try:
        mid = int(message_id)
    except ValueError:
        await interaction.response.send_message("Message ID must be a number.", ephemeral=True)
        return
    instance_id = instance_id_from_message_id(mid)
    event = get_event(instance_id) if instance_id else None
    if not event or event["guild_id"] != interaction.guild_id:
        await interaction.response.send_message("I could not find that event in this server.", ephemeral=True)
        return
    if interaction.user.id != event["creator_id"] and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Only the organizer or a server manager can save this template.", ephemeral=True)
        return
    save_template(interaction.guild_id, interaction.user.id, name, event["series_id"])
    await interaction.response.send_message(f"✅ Saved template **{name}**.", ephemeral=True)


@event_group.command(name="templates", description="Manage saved event templates")
@app_commands.guild_only()
async def event_templates(interaction: discord.Interaction):
    rows = get_templates(interaction.guild_id)
    if not rows:
        await interaction.response.send_message("No saved templates yet.", ephemeral=True)
        return
    await interaction.response.send_message(
        "**Saved Templates**\nChoose a template, then Rename, Update, or Delete it.",
        view=TemplateManageSelectView(rows),
        ephemeral=True,
    )


@event_group.command(name="duplicate", description="Duplicate an event with fresh responses")
@app_commands.guild_only()
@app_commands.describe(message_id="Message ID of the event to duplicate")
async def event_duplicate(interaction: discord.Interaction, message_id: str):
    try:
        mid = int(message_id)
    except ValueError:
        await interaction.response.send_message("Message ID must be a number.", ephemeral=True)
        return
    instance_id = instance_id_from_message_id(mid)
    event = get_event(instance_id) if instance_id else None
    if not event or event["guild_id"] != interaction.guild_id:
        await interaction.response.send_message("I could not find that event in this server.", ephemeral=True)
        return
    if interaction.user.id != event["creator_id"] and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Only the organizer or a server manager can duplicate this event.", ephemeral=True)
        return
    await interaction.response.send_modal(DuplicateEventModal(instance_id))


@event_group.command(name="calendar", description="Download an event calendar file (.ics)")
@app_commands.guild_only()
@app_commands.describe(message_id="Message ID of the event")
async def event_calendar(interaction: discord.Interaction, message_id: str):
    try:
        mid = int(message_id)
    except ValueError:
        await interaction.response.send_message("Message ID must be a number.", ephemeral=True)
        return
    instance_id = instance_id_from_message_id(mid)
    event = get_event(instance_id) if instance_id else None
    if not event or event["guild_id"] != interaction.guild_id:
        await interaction.response.send_message("I could not find that event in this server.", ephemeral=True)
        return
    filename = re.sub(r"[^A-Za-z0-9_-]+", "_", event["title"]).strip("_") or "event"
    await interaction.response.send_message(
        file=discord.File(io.BytesIO(build_ics(event)), filename=f"{filename}.ics"),
        ephemeral=True,
    )


@event_group.command(name="refresh", description="Refresh one event message by its message ID")
@app_commands.guild_only()
@app_commands.describe(message_id="Discord message ID of the event")
async def event_refresh(interaction: discord.Interaction, message_id: str):
    try:
        message_id_int = int(message_id)
    except ValueError:
        await interaction.response.send_message("Message ID must be a number.", ephemeral=True)
        return

    with get_db() as db:
        row = db.execute("SELECT id FROM event_instances WHERE message_id = ?", (message_id_int,)).fetchone()
    event = get_event(row["id"]) if row else None
    if not event or event["guild_id"] != interaction.guild_id:
        await interaction.response.send_message("I could not find that event in this server.", ephemeral=True)
        return

    await refresh_event_message(row["id"])
    await interaction.response.send_message("✅ Event refreshed.", ephemeral=True)


bot.tree.add_command(event_group)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print("APP COMMAND ERROR:")
    traceback.print_exception(type(error), error, error.__traceback__)
    if not interaction.response.is_done():
        await interaction.response.send_message(
            f"Something went wrong: `{type(error).__name__}: {error}`", ephemeral=True
        )


# ==================================================
# START
# ==================================================

print(">>> Starting Oak Tree Gather...")
try:
    bot.run(TOKEN)
except Exception as error:
    print(">>> BOT CRASHED:")
    print(type(error).__name__)
    print(repr(error))
    raise
finally:
    print(">>> bot.run() ended")
