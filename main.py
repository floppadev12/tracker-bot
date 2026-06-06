import asyncio
import datetime as dt
import os
import tempfile
from collections import defaultdict
from zoneinfo import ZoneInfo

import discord
import psycopg2
import psycopg2.extras
from discord import app_commands
from discord.ext import commands, tasks as discord_tasks
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")

# Preserved settings from the previous bot.
PROJECT_NAME = "Project Floppa"
REPORT_CHANNEL_ID = 1490317756136947942
WEEKLY_DASHBOARD_CHANNEL_ID = 1509800687871987732
TIMEZONE = "Europe/Bratislava"
USD_PER_ROBUX = 0.0038
EMBED_COLOR = discord.Color(0xFFF9EB)

SELF_CARE_TASK = "Self-care"
CHECKIN_INTERVAL = dt.timedelta(minutes=30)
CHECKIN_RETRY_DELAYS = (60, 10, 10, 10)
MAX_CUSTOM_TASKS = 24

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

conn = None
checkin_locks = set()
GUILD_ID = int(DISCORD_GUILD_ID) if DISCORD_GUILD_ID else None
commands_synced = False
last_weekly_dashboard_post_date = None


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def local_now():
    return utc_now().astimezone(ZoneInfo(TIMEZONE))


def parse_local_date(value: str | None):
    if not value:
        return local_now().date()
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Use date format YYYY-MM-DD.") from exc


def get_conn():
    global conn
    if conn is None or conn.closed:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is missing.")
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
    return conn


def db_one(query, params=()):
    with get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        return cur.fetchone()


def db_all(query, params=()):
    with get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def db_exec(query, params=()):
    with get_conn().cursor() as cur:
        cur.execute(query, params)


def init_db():
    db_exec(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            id SERIAL PRIMARY KEY,
            owner_discord_user_id BIGINT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'Executive',
            channel_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (display_name)
        );
        """
    )
    migrate_profile_schema()
    db_exec(
        """
        CREATE TABLE IF NOT EXISTS work_days (
            id SERIAL PRIMARY KEY,
            profile_id INTEGER REFERENCES profiles(id),
            discord_user_id BIGINT,
            work_date DATE NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            closed_at TIMESTAMPTZ,
            paused_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'active',
            next_checkin_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS day_tasks (
            id SERIAL PRIMARY KEY,
            day_id INTEGER NOT NULL REFERENCES work_days(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            removed_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS task_segments (
            id SERIAL PRIMARY KEY,
            day_id INTEGER NOT NULL REFERENCES work_days(id) ON DELETE CASCADE,
            task_id INTEGER NOT NULL REFERENCES day_tasks(id),
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ,
            source TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS checkins (
            id SERIAL PRIMARY KEY,
            day_id INTEGER NOT NULL REFERENCES work_days(id) ON DELETE CASCADE,
            task_id INTEGER REFERENCES day_tasks(id),
            asked_at TIMESTAMPTZ NOT NULL,
            answered_at TIMESTAMPTZ,
            source TEXT NOT NULL
        );
        """
    )
    migrate_profile_schema()
    if not table_has_column("work_days", "paused_at"):
        db_exec("ALTER TABLE work_days ADD COLUMN paused_at TIMESTAMPTZ")
    if not table_has_column("day_tasks", "removed_at"):
        db_exec("ALTER TABLE day_tasks ADD COLUMN removed_at TIMESTAMPTZ")
    db_exec(
        """
        WITH duplicate_active AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY discord_user_id
                    ORDER BY started_at DESC, id DESC
                ) AS row_number
            FROM work_days
            WHERE status = 'active' AND discord_user_id IS NOT NULL
        )
        UPDATE task_segments
        SET ended_at = NOW()
        WHERE ended_at IS NULL
          AND day_id IN (SELECT id FROM duplicate_active WHERE row_number > 1)
        """
    )
    db_exec(
        """
        WITH duplicate_active AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY discord_user_id
                    ORDER BY started_at DESC, id DESC
                ) AS row_number
            FROM work_days
            WHERE status = 'active' AND discord_user_id IS NOT NULL
        )
        UPDATE work_days
        SET status = 'closed',
            closed_at = COALESCE(closed_at, NOW()),
            paused_at = NULL,
            next_checkin_at = NULL
        WHERE id IN (SELECT id FROM duplicate_active WHERE row_number > 1)
        """
    )
    db_exec(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS work_days_one_active_user_idx
        ON work_days (discord_user_id)
        WHERE status = 'active'
        """
    )


def table_has_column(table_name: str, column_name: str):
    row = db_one(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table_name, column_name),
    )
    return row is not None


def table_exists(table_name: str):
    row = db_one("SELECT to_regclass(%s) AS table_name", (table_name,))
    return bool(row and row["table_name"])


def migrate_profile_schema():
    if not table_has_column("profiles", "owner_discord_user_id"):
        if table_exists("work_days"):
            db_exec("ALTER TABLE work_days DROP CONSTRAINT IF EXISTS work_days_discord_user_id_fkey")
        db_exec("ALTER TABLE profiles RENAME TO profiles_legacy")
        db_exec(
            """
            CREATE TABLE profiles (
                id SERIAL PRIMARY KEY,
                owner_discord_user_id BIGINT NOT NULL,
                display_name TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL DEFAULT 'Executive',
                channel_id BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        db_exec(
            """
            INSERT INTO profiles (owner_discord_user_id, display_name, role, channel_id, created_at)
            SELECT discord_user_id, display_name, role, channel_id, created_at
            FROM profiles_legacy
            ON CONFLICT (display_name) DO NOTHING
            """
        )

    if not table_exists("work_days"):
        return

    if not table_has_column("work_days", "profile_id"):
        db_exec("ALTER TABLE work_days ADD COLUMN profile_id INTEGER")

    db_exec(
        """
        UPDATE work_days wd
        SET profile_id = p.id
        FROM profiles p
        WHERE wd.profile_id IS NULL
          AND wd.discord_user_id IS NOT NULL
          AND p.owner_discord_user_id = wd.discord_user_id
        """
    )
    db_exec(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'work_days_profile_id_fkey'
            ) THEN
                ALTER TABLE work_days
                ADD CONSTRAINT work_days_profile_id_fkey
                FOREIGN KEY (profile_id) REFERENCES profiles(id);
            END IF;
        END $$;
        """
    )


def date_choice_label(row):
    total, _ = day_summary(row["id"], row["closed_at"] or utc_now())
    status = "closed" if row["closed_at"] else "active"
    if row.get("paused_at"):
        status = "paused"
    return f"{row['work_date']} | {format_duration(total)} | {status}"


async def work_date_autocomplete(interaction: discord.Interaction, current: str):
    selected_user = getattr(interaction.namespace, "user", None)
    target_user_id = selected_user.id if selected_user else interaction.user.id
    search = f"{current}%"
    rows = db_all(
        """
        SELECT id, work_date, closed_at, paused_at
        FROM work_days
        WHERE discord_user_id = %s
          AND (%s = '' OR work_date::TEXT LIKE %s)
        ORDER BY work_date DESC, started_at DESC
        LIMIT 25
        """,
        (target_user_id, current, search),
    )
    return [
        app_commands.Choice(name=date_choice_label(row)[:100], value=str(row["work_date"]))
        for row in rows
    ]


def get_active_day(user_id: int):
    return db_one(
        """
        SELECT * FROM work_days
        WHERE discord_user_id = %s AND status = 'active'
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (user_id,),
    )


def get_day_tasks(day_id: int):
    return db_all(
        "SELECT * FROM day_tasks WHERE day_id = %s AND removed_at IS NULL ORDER BY sort_order ASC",
        (day_id,),
    )


async def active_task_autocomplete(interaction: discord.Interaction, current: str):
    day = get_active_day(interaction.user.id)
    if not day:
        return []

    search = current.lower()
    rows = [
        row for row in get_day_tasks(day["id"])
        if not search or search in row["name"].lower()
    ][:25]
    return [
        app_commands.Choice(name=row["name"][:100], value=str(row["id"]))
        for row in rows
    ]


def get_latest_closed_day(user_id: int):
    return db_one(
        """
        SELECT *
        FROM work_days
        WHERE discord_user_id = %s AND status = 'closed'
        ORDER BY work_date DESC, started_at DESC
        LIMIT 1
        """,
        (user_id,),
    )


def get_user_day_by_date(user_id: int, work_date: dt.date):
    return db_one(
        """
        SELECT *
        FROM work_days
        WHERE discord_user_id = %s
          AND work_date = %s
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (user_id, work_date),
    )


def get_day_task_time_rows(day_id: int, end_at=None):
    end_at = end_at or utc_now()
    return db_all(
        """
        SELECT
            t.id,
            t.name,
            SUM(EXTRACT(EPOCH FROM (COALESCE(s.ended_at, %s) - s.started_at))) AS seconds
        FROM day_tasks t
        JOIN task_segments s ON s.task_id = t.id
        WHERE t.day_id = %s
        GROUP BY t.id, t.name, t.sort_order
        HAVING SUM(EXTRACT(EPOCH FROM (COALESCE(s.ended_at, %s) - s.started_at))) > 0
        ORDER BY t.sort_order ASC
        """,
        (end_at, day_id, end_at),
    )


async def saved_task_autocomplete(interaction: discord.Interaction, current: str):
    day = None
    selected_date = getattr(interaction.namespace, "date", None)
    if selected_date:
        try:
            day = get_user_day_by_date(interaction.user.id, parse_local_date(selected_date))
        except ValueError:
            return []
    else:
        day = get_latest_closed_day(interaction.user.id)

    if not day:
        return []

    search = current.lower()
    rows = [
        row for row in get_day_task_time_rows(day["id"], day["closed_at"] or utc_now())
        if not search or search in row["name"].lower()
    ][:25]
    return [
        app_commands.Choice(
            name=f"{row['name']} | {format_duration(dt.timedelta(seconds=int(row['seconds'])))}"[:100],
            value=str(row["id"]),
        )
        for row in rows
    ]


def get_day_task(day_id: int, task_id: int):
    return db_one(
        "SELECT * FROM day_tasks WHERE day_id = %s AND id = %s AND removed_at IS NULL",
        (day_id, task_id),
    )


def get_day_task_by_name(day_id: int, name: str):
    return db_one(
        """
        SELECT *
        FROM day_tasks
        WHERE day_id = %s
          AND lower(name) = lower(%s)
          AND removed_at IS NULL
        LIMIT 1
        """,
        (day_id, name),
    )


def get_saved_day_task(day_id: int, task: str):
    try:
        task_id = int(task)
    except ValueError:
        return db_one(
            """
            SELECT *
            FROM day_tasks
            WHERE day_id = %s
              AND lower(name) = lower(%s)
            LIMIT 1
            """,
            (day_id, task.strip()),
        )

    return db_one("SELECT * FROM day_tasks WHERE day_id = %s AND id = %s", (day_id, task_id))


def add_task_to_day(day_id: int, name: str):
    row = db_one(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_sort_order FROM day_tasks WHERE day_id = %s",
        (day_id,),
    )
    sort_order = row["next_sort_order"] if row else 1
    return db_one(
        """
        INSERT INTO day_tasks (day_id, name, sort_order)
        VALUES (%s, %s, %s)
        RETURNING *
        """,
        (day_id, name, sort_order),
    )


def remove_task_from_day(day_id: int, task_id: int, when: dt.datetime):
    db_exec(
        """
        UPDATE day_tasks
        SET removed_at = %s
        WHERE day_id = %s AND id = %s AND removed_at IS NULL
        """,
        (when, day_id, task_id),
    )


def remove_time_from_task(day_id: int, task_id: int, duration: dt.timedelta):
    seconds_to_remove = int(duration.total_seconds())
    if seconds_to_remove <= 0:
        return dt.timedelta()

    connection = get_conn()
    previous_autocommit = connection.autocommit
    removed_seconds = 0
    try:
        connection.autocommit = False
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, started_at, ended_at
                FROM task_segments
                WHERE day_id = %s
                  AND task_id = %s
                  AND ended_at IS NOT NULL
                ORDER BY ended_at DESC, id DESC
                FOR UPDATE
                """,
                (day_id, task_id),
            )
            rows = cur.fetchall()
            for row in rows:
                if seconds_to_remove <= 0:
                    break

                segment_seconds = int((row["ended_at"] - row["started_at"]).total_seconds())
                if segment_seconds <= 0:
                    continue

                if segment_seconds <= seconds_to_remove:
                    cur.execute("DELETE FROM task_segments WHERE id = %s", (row["id"],))
                    removed_seconds += segment_seconds
                    seconds_to_remove -= segment_seconds
                    continue

                new_end = row["ended_at"] - dt.timedelta(seconds=seconds_to_remove)
                cur.execute("UPDATE task_segments SET ended_at = %s WHERE id = %s", (new_end, row["id"]))
                removed_seconds += seconds_to_remove
                seconds_to_remove = 0

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.autocommit = previous_autocommit

    return dt.timedelta(seconds=removed_seconds)


def get_open_segment(day_id: int):
    return db_one(
        """
        SELECT s.*, t.name AS task_name
        FROM task_segments s
        JOIN day_tasks t ON t.id = s.task_id
        WHERE s.day_id = %s AND s.ended_at IS NULL
        ORDER BY s.started_at DESC
        LIMIT 1
        """,
        (day_id,),
    )


def set_next_checkin(day_id: int, when: dt.datetime):
    db_exec(
        "UPDATE work_days SET next_checkin_at = %s WHERE id = %s",
        (when, day_id),
    )


def switch_task(day_id: int, task_id: int, when: dt.datetime, source: str):
    current = get_open_segment(day_id)
    if current and current["task_id"] == task_id:
        return

    db_exec(
        """
        UPDATE task_segments
        SET ended_at = %s
        WHERE day_id = %s AND ended_at IS NULL
        """,
        (when, day_id),
    )
    db_exec(
        """
        INSERT INTO task_segments (day_id, task_id, started_at, source)
        VALUES (%s, %s, %s, %s)
        """,
        (day_id, task_id, when, source),
    )
    db_exec("UPDATE work_days SET paused_at = NULL WHERE id = %s", (day_id,))


def pause_day(day_id: int, when: dt.datetime):
    db_exec(
        """
        UPDATE task_segments
        SET ended_at = %s
        WHERE day_id = %s AND ended_at IS NULL
        """,
        (when, day_id),
    )
    db_exec(
        """
        UPDATE work_days
        SET paused_at = %s, next_checkin_at = NULL
        WHERE id = %s AND status = 'active'
        """,
        (when, day_id),
    )


def resume_day(day_id: int, task_id: int, when: dt.datetime, source: str):
    switch_task(day_id, task_id, when, source)
    set_next_checkin(day_id, when + CHECKIN_INTERVAL)


def day_summary(day_id: int, end_at=None):
    end_at = end_at or utc_now()
    rows = db_all(
        """
        SELECT t.name, s.started_at, COALESCE(s.ended_at, %s) AS ended_at
        FROM task_segments s
        JOIN day_tasks t ON t.id = s.task_id
        WHERE s.day_id = %s
        ORDER BY s.started_at ASC
        """,
        (end_at, day_id),
    )

    totals = defaultdict(dt.timedelta)
    total = dt.timedelta()
    for row in rows:
        duration = row["ended_at"] - row["started_at"]
        if duration.total_seconds() > 0:
            totals[row["name"]] += duration
            total += duration
    return total, dict(totals)


def format_duration(duration: dt.timedelta):
    seconds = max(0, int(duration.total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes, leftover_seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{leftover_seconds}s"


def build_summary_text(title: str, total: dt.timedelta, totals: dict[str, dt.timedelta]):
    lines = [f"**{title}**", f"⏱️ Total worked: **{format_duration(total)}**", ""]
    if not totals:
        lines.append("No tracked time yet.")
        return "\n".join(lines)

    for name, duration in sorted(totals.items(), key=lambda item: item[1], reverse=True):
        lines.append(f"• **{name}**: {format_duration(duration)}")
    return "\n".join(lines)


def make_embed(title: str | None = None, description: str | None = None):
    return discord.Embed(title=title, description=description, color=EMBED_COLOR)


def build_summary_embed(title: str, total: dt.timedelta, totals: dict[str, dt.timedelta]):
    description = f"⏱️ Total worked: **{format_duration(total)}**"
    embed = make_embed(title, description)
    if not totals:
        embed.add_field(name="Tasks", value="No tracked time yet.", inline=False)
        return embed

    task_lines = [
        f"**{name}**: {format_duration(duration)}"
        for name, duration in sorted(totals.items(), key=lambda item: item[1], reverse=True)
    ]
    embed.add_field(name="Task breakdown", value="\n".join(task_lines)[:1024], inline=False)
    return embed


def split_discord_messages(text: str, limit=1900):
    chunks = []
    current = []
    size = 0
    for line in text.splitlines():
        line_size = len(line) + 1
        if current and size + line_size > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += line_size
    if current:
        chunks.append("\n".join(current))
    return chunks


class TaskSelectView(discord.ui.View):
    def __init__(self, task_rows, prompt: str, future: asyncio.Future | None = None, day_id: int | None = None):
        super().__init__(timeout=None)
        self.future = future or asyncio.get_running_loop().create_future()
        self.add_item(TaskSelect(task_rows, prompt, self.future, day_id))


class TaskSelect(discord.ui.Select):
    def __init__(self, task_rows, prompt: str, future: asyncio.Future, day_id: int | None = None):
        self.future = future
        self.day_id = day_id
        options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]))
            for row in task_rows
        ]
        super().__init__(placeholder=prompt[:100], options=options)

    async def callback(self, interaction: discord.Interaction):
        task_id = int(self.values[0])
        if not self.future.done():
            self.future.set_result((task_id, interaction.created_at))
        if self.day_id is not None:
            day = db_one("SELECT status, paused_at FROM work_days WHERE id = %s", (self.day_id,))
            if day and day["status"] == "active" and day["paused_at"]:
                resume_day(self.day_id, task_id, interaction.created_at, "checkin_resume")
        await interaction.response.send_message("✅ Saved.", ephemeral=True)
        self.view.stop()


class StartDayTaskModal(discord.ui.Modal, title="Start productivity day"):
    def __init__(self, user: discord.abc.User):
        super().__init__()
        self.user = user

    task_list = discord.ui.TextInput(
        label="Today's tasks",
        placeholder="Write one task per line. Self-care is added automatically.",
        style=discord.TextStyle.paragraph,
        min_length=1,
        max_length=1800,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if get_active_day(self.user.id):
            await interaction.response.send_message(
                embed=make_embed("Active Day", "⚠️ You already have an active day. Use `/closeday` first."),
                ephemeral=True,
            )
            return

        task_names = [line.strip() for line in str(self.task_list).splitlines() if line.strip()]
        task_names = [name for name in task_names if name.lower() != SELF_CARE_TASK.lower()]
        if not task_names:
            await interaction.response.send_message(
                embed=make_embed("No Tasks Found", "Add at least one task and start again."),
                ephemeral=True,
            )
            return
        if len(task_names) > MAX_CUSTOM_TASKS:
            await interaction.response.send_message(
                embed=make_embed("Too Many Tasks", f"Enter up to **{MAX_CUSTOM_TASKS}** tasks plus Self-care."),
                ephemeral=True,
            )
            return

        task_names.append(SELF_CARE_TASK)
        day_id, started_at = create_day(self.user.id, task_names)
        await interaction.response.send_message(
            embed=make_embed(
                f"Welcome back, {self.user.display_name}",
                f"👋 Day started with **{len(task_names)}** tasks.\nCheck your DMs to choose what you are starting with.",
            )
        )
        asyncio.create_task(run_checkin(self.user.id, "Choose what you are starting with.", started_at))


async def ask_task_choice(user: discord.User, day_id: int, reason: str):
    task_rows = get_day_tasks(day_id)
    if not task_rows:
        return None, "no_tasks", None

    prompt = "What are you doing right now?"
    future = asyncio.get_running_loop().create_future()
    for attempt, timeout in enumerate(CHECKIN_RETRY_DELAYS, start=1):
        try:
            view = TaskSelectView(task_rows, prompt, future, day_id)
            await user.send(
                embed=make_embed(f"📌 {PROJECT_NAME} productivity check-in", reason),
                view=view,
            )
            task_id, answered_at = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            return task_id, "answered", answered_at
        except asyncio.TimeoutError:
            prompt = "Still need your current task"
            if attempt == 1:
                reason = "No answer yet. I will ask a few quick times, then keep your current task running."
            else:
                reason = "Quick retry."
        except discord.Forbidden:
            return None, "dm_blocked_continued", None

    try:
        await user.send(
            embed=make_embed(
                "Check-in Missed",
                "No answer received. I kept your current task running and will ask again at the next check-in.",
            )
        )
    except discord.Forbidden:
        return None, "dm_blocked_continued", None

    return None, "unanswered_continued", None


async def run_checkin(user_id: int, reason: str, segment_started_at: dt.datetime | None = None):
    if user_id in checkin_locks:
        return
    checkin_locks.add(user_id)
    try:
        day = get_active_day(user_id)
        if not day:
            return
        if day["paused_at"] and segment_started_at is None:
            return

        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        asked_at = utc_now()
        task_id, source, answered_at = await ask_task_choice(user, day["id"], reason)
        if task_id:
            start_at = segment_started_at or answered_at or utc_now()
            if source == "answered_after_pause":
                resume_day(day["id"], task_id, start_at, source)
            else:
                switch_task(day["id"], task_id, start_at, source)

        db_exec(
            """
            INSERT INTO checkins (day_id, task_id, asked_at, answered_at, source)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (day["id"], task_id, asked_at, answered_at, source),
        )
        if task_id and source != "answered_after_pause":
            set_next_checkin(day["id"], utc_now() + CHECKIN_INTERVAL)
        elif source in {"unanswered_continued", "dm_blocked_continued"}:
            set_next_checkin(day["id"], utc_now() + CHECKIN_INTERVAL)
    finally:
        checkin_locks.discard(user_id)


@discord_tasks.loop(minutes=1)
async def checkin_scheduler():
    rows = db_all(
        """
        SELECT discord_user_id
        FROM work_days
        WHERE status = 'active'
          AND discord_user_id IS NOT NULL
          AND paused_at IS NULL
          AND next_checkin_at IS NOT NULL
          AND next_checkin_at <= %s
        """,
        (utc_now(),),
    )
    for row in rows:
        asyncio.create_task(run_checkin(row["discord_user_id"], "Scheduled 30-minute check-in."))


def create_day(owner_user_id: int, task_names: list[str]):
    started_at = utc_now()
    work_date = started_at.astimezone(ZoneInfo(TIMEZONE)).date()
    with get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO work_days (profile_id, discord_user_id, work_date, started_at, status)
            VALUES (%s, %s, %s, %s, 'active')
            RETURNING id
            """,
            (None, owner_user_id, work_date, started_at),
        )
        day_id = cur.fetchone()["id"]
        first_task_id = None
        for index, name in enumerate(task_names, start=1):
            cur.execute(
                """
                INSERT INTO day_tasks (day_id, name, sort_order)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (day_id, name, index),
            )
            task_id = cur.fetchone()["id"]
            if first_task_id is None:
                first_task_id = task_id
        if first_task_id:
            cur.execute(
                """
                INSERT INTO task_segments (day_id, task_id, started_at, source)
                VALUES (%s, %s, %s, 'start_default')
                """,
                (day_id, first_task_id, started_at),
            )
    return day_id, started_at


def weekly_range(anchor: dt.date):
    start = anchor - dt.timedelta(days=anchor.weekday())
    end = start + dt.timedelta(days=7)
    return start, end


def weekly_rows(user_id: int, anchor: dt.date):
    start, end = weekly_range(anchor)
    return db_all(
        """
        SELECT id, work_date, started_at, closed_at, paused_at, status
        FROM work_days
        WHERE discord_user_id = %s
          AND work_date >= %s AND work_date < %s
        ORDER BY work_date ASC, started_at ASC
        """,
        (user_id, start, end),
    )


def weekly_summary(user_id: int, anchor: dt.date):
    rows = weekly_rows(user_id, anchor)
    daily = []
    task_totals = defaultdict(dt.timedelta)
    week_total = dt.timedelta()
    for row in rows:
        end_at = row["closed_at"] or utc_now()
        total, totals = day_summary(row["id"], end_at)
        daily.append((row["work_date"], total, totals))
        week_total += total
        for task, duration in totals.items():
            task_totals[task] += duration
    return daily, week_total, dict(task_totals)


def clear_stats(user_id: int | None = None, scope: str = "all", anchor: dt.date | None = None):
    params = []
    where_parts = []
    if user_id is not None:
        where_parts.append("discord_user_id = %s")
        params.append(user_id)

    if scope == "day":
        target_date = anchor or local_now().date()
        where_parts.append("work_date = %s")
        params.append(target_date)
    elif scope == "week":
        start, end = weekly_range(anchor or local_now().date())
        where_parts.append("work_date >= %s AND work_date < %s")
        params.extend([start, end])
    elif scope != "all":
        raise ValueError("Invalid clear scope.")

    where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
    row = db_one(f"SELECT COUNT(*) AS count FROM work_days{where_sql}", tuple(params))
    db_exec(f"DELETE FROM work_days{where_sql}", tuple(params))
    return row["count"] if row else 0


def users_with_weekly_stats(anchor: dt.date):
    start, end = weekly_range(anchor)
    return db_all(
        """
        SELECT DISTINCT discord_user_id
        FROM work_days
        WHERE discord_user_id IS NOT NULL
          AND work_date >= %s AND work_date < %s
        ORDER BY discord_user_id ASC
        """,
        (start, end),
    )


async def post_weekly_dashboards(anchor: dt.date):
    channel = bot.get_channel(WEEKLY_DASHBOARD_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(WEEKLY_DASHBOARD_CHANNEL_ID)

    rows = users_with_weekly_stats(anchor)
    start, end = weekly_range(anchor)
    if not rows:
        await channel.send(
            embed=make_embed(
                "Weekly Dashboards",
                f"No tracked stats found for {start} to {end - dt.timedelta(days=1)}.",
            )
        )
        return

    await channel.send(
        embed=make_embed(
            "Weekly Dashboards",
            f"Posting dashboard images for {start} to {end - dt.timedelta(days=1)}.",
        )
    )
    for row in rows:
        user_id = row["discord_user_id"]
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            display_name = user.display_name
        except discord.DiscordException:
            display_name = f"User {user_id}"

        daily, week_total, task_totals = weekly_summary(user_id, anchor)
        path = make_dashboard_image(display_name, user_id, daily, week_total, task_totals, anchor)
        await channel.send(
            content=f"Weekly dashboard for **{display_name}**",
            file=discord.File(path, filename=f"weekly-productivity-{user_id}.png"),
        )


@discord_tasks.loop(minutes=1)
async def weekly_dashboard_scheduler():
    global last_weekly_dashboard_post_date
    now = local_now()
    today = now.date()
    if now.weekday() != 6 or now.hour != 20 or now.minute != 0:
        return
    if last_weekly_dashboard_post_date == today:
        return

    last_weekly_dashboard_post_date = today
    try:
        await post_weekly_dashboards(today)
    except Exception as exc:
        print(f"Weekly dashboard post failed: {exc}")


def get_font(size: int, bold=False):
    inter_path = os.path.join(os.path.dirname(__file__), "assets", "fonts", "Inter.ttf")
    if os.path.exists(inter_path):
        try:
            font = ImageFont.truetype(inter_path, size)
            try:
                font.set_variation_by_axes([700 if bold else 450])
            except (AttributeError, OSError, ValueError):
                pass
            return font
        except OSError:
            pass

    names = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "arialbd.ttf" if bold else "arial.ttf",
    ]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_dashboard_image(display_name: str, user_id: int, daily, week_total, task_totals, anchor: dt.date):
    width, height = 1400, 820
    image = Image.new("RGB", (width, height), "#0b0f14")
    draw = ImageDraw.Draw(image)
    title_font = get_font(48, True)
    metric_font = get_font(44, True)
    heading_font = get_font(26, True)
    body_font = get_font(22)
    small_font = get_font(17)
    tiny_font = get_font(14)

    for y in range(height):
        for x in range(width):
            r = 10 + int((x / width) * 5)
            g = 14 + int((y / height) * 7)
            b = 20 + int((x / width) * 9) + int((y / height) * 5)
            image.putpixel((x, y), (r, g, b))

    def panel(box, fill="#111820", outline="#2b3642", radius=18):
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=1)

    def gradient_bar(box, color_a, color_b, radius=10):
        x1, y1, x2, y2 = box
        mask = Image.new("L", (x2 - x1, y2 - y1), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle((0, 0, x2 - x1, y2 - y1), radius=radius, fill=255)
        grad = Image.new("RGB", (x2 - x1, y2 - y1), color_a)
        grad_pixels = grad.load()
        for gx in range(x2 - x1):
            ratio = gx / max(1, x2 - x1 - 1)
            rgb = tuple(int(color_a[i] + (color_b[i] - color_a[i]) * ratio) for i in range(3))
            for gy in range(y2 - y1):
                grad_pixels[gx, gy] = rgb
        image.paste(grad, (x1, y1), mask)

    start, end = weekly_range(anchor)
    panel((34, 34, 1366, 786), "#0f151c", "#26313d", 22)
    draw.text((78, 72), "Weekly Productivity", font=title_font, fill="#f4f6f8")
    draw.text(
        (82, 132),
        f"{display_name}  |  {start} to {end - dt.timedelta(days=1)}",
        font=body_font,
        fill="#9ca7b3",
    )

    panel((944, 72, 1308, 176), "#141c24", "#2e3a46", 18)
    draw.text((976, 88), format_duration(week_total), font=metric_font, fill="#36f0a1")
    draw.text((980, 142), "total focused time", font=small_font, fill="#9ca7b3")

    max_daily = max([total.total_seconds() for _, total, _ in daily] + [1])
    x, y = 82, 246
    panel((64, 210, 858, 720), "#111820", "#2b3642", 18)
    draw.text((x, y - 6), "Daily totals", font=heading_font, fill="#f4f6f8")
    chart_top = y + 58
    chart_bottom = 642
    chart_height = chart_bottom - chart_top
    for index in range(7):
        day = start + dt.timedelta(days=index)
        match = next((item for item in daily if item[0] == day), None)
        total = match[1] if match else dt.timedelta()
        bar_h = int((total.total_seconds() / max_daily) * chart_height)
        bx = x + 28 + index * 102
        draw.rounded_rectangle((bx, chart_top, bx + 58, chart_bottom), radius=12, fill="#1a232d")
        if bar_h:
            gradient_bar((bx, chart_bottom - bar_h, bx + 58, chart_bottom), (0, 171, 85), (54, 240, 161), 12)
        draw.text((bx + 8, chart_bottom + 18), day.strftime("%a"), font=small_font, fill="#d8dee4")
        label = format_duration(total)
        label_w = draw.textbbox((0, 0), label, font=tiny_font)[2]
        draw.text((bx + 29 - label_w / 2, chart_bottom + 45), label, font=tiny_font, fill="#9ca7b3")

    tx, ty = 930, 246
    panel((900, 210, 1308, 720), "#111820", "#2b3642", 18)
    draw.text((tx, ty - 6), "Top tasks", font=heading_font, fill="#f4f6f8")
    max_task = max([duration.total_seconds() for duration in task_totals.values()] + [1])
    top_tasks = sorted(task_totals.items(), key=lambda item: item[1], reverse=True)[:7]
    if not top_tasks:
        draw.text((tx, ty + 68), "No tracked tasks yet", font=body_font, fill="#9ca7b3")
    for index, (task, duration) in enumerate(top_tasks):
        row_y = ty + 62 + index * 58
        name = task if len(task) <= 24 else task[:21] + "..."
        draw.text((tx, row_y), name, font=body_font, fill="#f4f6f8")
        duration_text = format_duration(duration)
        duration_w = draw.textbbox((0, 0), duration_text, font=small_font)[2]
        draw.text((1266 - duration_w, row_y + 3), duration_text, font=small_font, fill="#b5bec9")
        bar_w = int((duration.total_seconds() / max_task) * 336)
        draw.rounded_rectangle((tx, row_y + 34, tx + 336, row_y + 47), radius=6, fill="#1e2833")
        if bar_w:
            gradient_bar((tx, row_y + 34, tx + bar_w, row_y + 47), (0, 171, 85), (54, 240, 161), 6)

    draw.text((82, 738), "Generated by the productivity tracker", font=tiny_font, fill="#6f7c89")

    output = os.path.join(tempfile.gettempdir(), f"productivity_week_{user_id}_{anchor}.png")
    image.save(output)
    return output


@bot.tree.command(name="startday", description="Start your productivity day and send today's tasks.")
async def startday(interaction: discord.Interaction):
    if get_active_day(interaction.user.id):
        await interaction.response.send_message(
            embed=make_embed("Active Day", "⚠️ You already have an active day. Use `/closeday` first."),
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(StartDayTaskModal(interaction.user))


@bot.tree.command(name="closeday", description="Close your active day and show the time spent per task.")
async def closeday(interaction: discord.Interaction):
    day = get_active_day(interaction.user.id)
    if not day:
        await interaction.response.send_message(
            embed=make_embed("No Active Day", "⚠️ You do not have an active day."),
            ephemeral=True,
        )
        return

    closed_at = utc_now()
    db_exec("UPDATE task_segments SET ended_at = %s WHERE day_id = %s AND ended_at IS NULL", (closed_at, day["id"]))
    db_exec("UPDATE work_days SET status = 'closed', closed_at = %s, paused_at = NULL, next_checkin_at = NULL WHERE id = %s", (closed_at, day["id"]))
    total, totals = day_summary(day["id"], closed_at)
    await interaction.response.send_message(embed=build_summary_embed("✅ Day Closed", total, totals))


@bot.tree.command(name="pause", description="Pause your active productivity timer.")
async def pause(interaction: discord.Interaction):
    day = get_active_day(interaction.user.id)
    if not day:
        await interaction.response.send_message(embed=make_embed("No Active Day", "You do not have an active day."), ephemeral=True)
        return
    if day["paused_at"]:
        await interaction.response.send_message(embed=make_embed("Already Paused", "Your timer is already paused."), ephemeral=True)
        return

    pause_day(day["id"], utc_now())
    await interaction.response.send_message(embed=make_embed("Paused", "Your productivity timer is paused."), ephemeral=True)


@bot.tree.command(name="addtask", description="Add a task to your active day.")
@app_commands.describe(name="Task name to add")
async def addtask(interaction: discord.Interaction, name: str):
    day = get_active_day(interaction.user.id)
    if not day:
        await interaction.response.send_message(embed=make_embed("No Active Day", "You do not have an active day."), ephemeral=True)
        return

    task_name = name.strip()
    if not task_name:
        await interaction.response.send_message(embed=make_embed("Invalid Task", "Task name cannot be empty."), ephemeral=True)
        return
    if len(task_name) > 100:
        await interaction.response.send_message(embed=make_embed("Task Too Long", "Use a task name of 100 characters or fewer."), ephemeral=True)
        return
    if len(get_day_tasks(day["id"])) >= 25:
        await interaction.response.send_message(embed=make_embed("Task Limit Reached", "A day can have up to 25 active tasks."), ephemeral=True)
        return
    if get_day_task_by_name(day["id"], task_name):
        await interaction.response.send_message(embed=make_embed("Task Exists", f"**{task_name}** is already on today's task list."), ephemeral=True)
        return

    add_task_to_day(day["id"], task_name)
    await interaction.response.send_message(embed=make_embed("Task Added", f"Added **{task_name}** to today's task list."), ephemeral=True)


@bot.tree.command(name="removetask", description="Remove a task from your active day.")
@app_commands.describe(task="Task to remove")
@app_commands.autocomplete(task=active_task_autocomplete)
async def removetask(interaction: discord.Interaction, task: str):
    day = get_active_day(interaction.user.id)
    if not day:
        await interaction.response.send_message(embed=make_embed("No Active Day", "You do not have an active day."), ephemeral=True)
        return

    task_row = None
    try:
        task_row = get_day_task(day["id"], int(task))
    except ValueError:
        task_row = get_day_task_by_name(day["id"], task.strip())

    if not task_row:
        await interaction.response.send_message(embed=make_embed("Task Not Found", "That task is not on today's active task list."), ephemeral=True)
        return
    if task_row["name"].lower() == SELF_CARE_TASK.lower():
        await interaction.response.send_message(embed=make_embed("Cannot Remove Self-care", "Self-care stays on every day automatically."), ephemeral=True)
        return
    if len(get_day_tasks(day["id"])) <= 1:
        await interaction.response.send_message(embed=make_embed("Cannot Remove Last Task", "Keep at least one task on the day."), ephemeral=True)
        return

    remove_task_from_day(day["id"], task_row["id"], utc_now())
    await interaction.response.send_message(embed=make_embed("Task Removed", f"Removed **{task_row['name']}** from future check-ins."), ephemeral=True)


@bot.tree.command(name="resume", description="Resume your paused productivity timer.")
async def resume(interaction: discord.Interaction):
    day = get_active_day(interaction.user.id)
    if not day:
        await interaction.response.send_message(embed=make_embed("No Active Day", "You do not have an active day."), ephemeral=True)
        return
    if not day["paused_at"]:
        await interaction.response.send_message(embed=make_embed("Not Paused", "Your timer is already running."), ephemeral=True)
        return

    task_rows = get_day_tasks(day["id"])
    if not task_rows:
        await interaction.response.send_message(embed=make_embed("No Tasks Found", "No tasks exist for this day."), ephemeral=True)
        return

    view = TaskSelectView(task_rows, "What are you resuming with?")
    await interaction.response.send_message(embed=make_embed("Resume", "Choose the task you are resuming."), view=view, ephemeral=True)
    task_id, answered_at = await view.future
    resume_day(day["id"], task_id, answered_at, "manual_resume")


@bot.tree.command(name="profile", description="Show your saved productivity stats.")
async def profile(interaction: discord.Interaction):
    days = db_all(
        """
        SELECT id, closed_at
        FROM work_days
        WHERE discord_user_id = %s AND status = 'closed'
        """,
        (interaction.user.id,),
    )
    total = dt.timedelta()
    for row in days:
        day_total, _ = day_summary(row["id"], row["closed_at"])
        total += day_total
    average = total / len(days) if days else dt.timedelta()
    await interaction.response.send_message(
        embed=make_embed(
            f"{interaction.user.display_name}",
            f"Closed days: **{len(days)}**\nDaily average: **{format_duration(average)}**",
        ),
        ephemeral=True,
    )


@bot.tree.command(name="weekly", description="Show weekly productivity stats.")
@app_commands.describe(user="Discord user whose stats to show", mode="How to show the weekly stats", date="Any date in the target week, YYYY-MM-DD")
@app_commands.autocomplete(date=work_date_autocomplete)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="overview_text", value="overview"),
        app_commands.Choice(name="separate_days", value="days"),
        app_commands.Choice(name="dashboard_image", value="image"),
    ]
)
async def weekly(
    interaction: discord.Interaction,
    user: discord.User,
    mode: app_commands.Choice[str] | None = None,
    date: str | None = None,
):
    await interaction.response.defer(ephemeral=True)
    try:
        anchor = parse_local_date(date)
    except ValueError as exc:
        await interaction.followup.send(embed=make_embed("Invalid Date", str(exc)), ephemeral=True)
        return

    selected_mode = mode.value if mode else "overview"
    daily, week_total, task_totals = weekly_summary(user.id, anchor)
    if selected_mode == "image":
        path = make_dashboard_image(user.display_name, user.id, daily, week_total, task_totals, anchor)
        await interaction.followup.send(file=discord.File(path, filename="weekly-productivity.png"), ephemeral=True)
        return

    if selected_mode == "days":
        if not daily:
            await interaction.followup.send(embed=make_embed("No Days Found", "📭 No days found for that week."), ephemeral=True)
            return
        for work_date, total, totals in daily:
            await interaction.followup.send(embed=build_summary_embed(str(work_date), total, totals), ephemeral=True)
        return

    start, end = weekly_range(anchor)
    text = build_summary_text(f"{user.display_name} | Week {start} to {end - dt.timedelta(days=1)}", week_total, task_totals)
    for chunk in split_discord_messages(text):
        await interaction.followup.send(embed=make_embed("Weekly Overview", chunk), ephemeral=True)


@bot.tree.command(name="daystats", description="Show one saved day for a user.")
@app_commands.describe(date="Day to show, YYYY-MM-DD")
@app_commands.autocomplete(date=work_date_autocomplete)
async def daystats(interaction: discord.Interaction, date: str | None = None):
    if date:
        try:
            work_date = parse_local_date(date)
        except ValueError as exc:
            await interaction.response.send_message(embed=make_embed("Invalid Date", str(exc)), ephemeral=True)
            return
        row = db_one(
            """
            SELECT id, work_date, closed_at
            FROM work_days
            WHERE discord_user_id = %s
              AND work_date = %s
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (interaction.user.id, work_date),
        )
    else:
        row = db_one(
            """
            SELECT id, work_date, closed_at
            FROM work_days
            WHERE discord_user_id = %s
            ORDER BY work_date DESC, started_at DESC
            LIMIT 1
            """,
            (interaction.user.id,),
        )
    if not row:
        await interaction.response.send_message(embed=make_embed("No Day Found", "📭 No day found for that date."), ephemeral=True)
        return
    total, totals = day_summary(row["id"], row["closed_at"] or utc_now())
    await interaction.response.send_message(embed=build_summary_embed(str(row["work_date"]), total, totals), ephemeral=True)


@bot.tree.command(name="removehours", description="Remove tracked hours from a task on a finished day.")
@app_commands.describe(
    date="Finished day to edit, YYYY-MM-DD",
    task="Task to remove time from",
    hours="Hours to remove, for example 1.5",
)
@app_commands.autocomplete(date=work_date_autocomplete, task=saved_task_autocomplete)
async def removehours(
    interaction: discord.Interaction,
    date: str,
    task: str,
    hours: app_commands.Range[float, 0.01, 1000.0],
):
    try:
        work_date = parse_local_date(date)
    except ValueError as exc:
        await interaction.response.send_message(embed=make_embed("Invalid Date", str(exc)), ephemeral=True)
        return

    day = get_user_day_by_date(interaction.user.id, work_date)
    if not day:
        await interaction.response.send_message(embed=make_embed("No Day Found", "No saved day was found for that date."), ephemeral=True)
        return
    if day["status"] != "closed":
        await interaction.response.send_message(embed=make_embed("Day Not Finished", "Use `/closeday` before removing hours from this day."), ephemeral=True)
        return

    task_row = get_saved_day_task(day["id"], task)
    if not task_row:
        await interaction.response.send_message(embed=make_embed("Task Not Found", "That task was not found on the selected day."), ephemeral=True)
        return

    before_rows = get_day_task_time_rows(day["id"], day["closed_at"])
    before_seconds = next((int(row["seconds"]) for row in before_rows if row["id"] == task_row["id"]), 0)
    if before_seconds <= 0:
        await interaction.response.send_message(embed=make_embed("No Time Found", "That task has no tracked time to remove."), ephemeral=True)
        return

    requested = dt.timedelta(seconds=int(round(hours * 3600)))
    removed = remove_time_from_task(day["id"], task_row["id"], requested)
    total, totals = day_summary(day["id"], day["closed_at"])
    description = (
        f"Removed **{format_duration(removed)}** from **{task_row['name']}** on **{day['work_date']}**.\n"
        f"Day total is now **{format_duration(total)}**."
    )
    if removed < requested:
        description += f"\nThat task only had **{format_duration(dt.timedelta(seconds=before_seconds))}** available."

    embed = build_summary_embed("Hours Removed", total, totals)
    embed.description = description
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="clearstats", description="Admin: clear productivity stats by day, week, or all time.")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    scope="Stats range to clear",
    user="Discord user to reset. Leave empty to reset everyone.",
    date="Target day or any date in the target week, YYYY-MM-DD. Defaults to today.",
    confirm="Type RESET when clearing everyone.",
)
@app_commands.autocomplete(date=work_date_autocomplete)
@app_commands.choices(
    scope=[
        app_commands.Choice(name="clear_day", value="day"),
        app_commands.Choice(name="clear_week", value="week"),
        app_commands.Choice(name="clear_all", value="all"),
    ]
)
async def clearstats(
    interaction: discord.Interaction,
    scope: app_commands.Choice[str],
    user: discord.User | None = None,
    date: str | None = None,
    confirm: str | None = None,
):
    permissions = getattr(interaction.user, "guild_permissions", None)
    if not permissions or not permissions.administrator:
        await interaction.response.send_message(
            embed=make_embed("Permission Denied", "Only server administrators can clear productivity stats."),
            ephemeral=True,
        )
        return

    if user is None and confirm != "RESET":
        await interaction.response.send_message(
            embed=make_embed("Confirmation Required", "To clear stats for everyone, include `confirm:RESET`."),
            ephemeral=True,
        )
        return

    try:
        anchor = parse_local_date(date) if scope.value in {"day", "week"} else None
    except ValueError as exc:
        await interaction.response.send_message(embed=make_embed("Invalid Date", str(exc)), ephemeral=True)
        return

    deleted_days = clear_stats(user.id if user else None, scope.value, anchor)
    target = user.display_name if user else "everyone"
    if scope.value == "day":
        scope_text = f"day {anchor}"
    elif scope.value == "week":
        start, end = weekly_range(anchor)
        scope_text = f"week {start} to {end - dt.timedelta(days=1)}"
    else:
        scope_text = "all time"
    await interaction.response.send_message(
        embed=make_embed("Stats Cleared", f"Cleared **{deleted_days}** work day(s) for **{target}** in **{scope_text}**."),
        ephemeral=True,
    )


@bot.tree.command(name="test", description="Run a productivity bot test.")
@app_commands.describe(option="Which test to run")
@app_commands.choices(
    option=[
        app_commands.Choice(name="profile", value="profile"),
        app_commands.Choice(name="startday", value="startday"),
        app_commands.Choice(name="checkin", value="checkin"),
        app_commands.Choice(name="closeday", value="closeday"),
        app_commands.Choice(name="weekly_graph", value="weekly_graph"),
    ]
)
async def test(interaction: discord.Interaction, option: app_commands.Choice[str]):
    if option.value == "profile":
        await interaction.response.send_message(
            embed=make_embed("Test Passed", "Stats are tied directly to your Discord account."),
            ephemeral=True,
        )
        return

    if option.value == "startday":
        await interaction.response.send_message(
            embed=make_embed("Startday Test", "`/startday` opens a multiline task form and appends Self-care."),
            ephemeral=True,
        )
        return

    if option.value == "checkin":
        if not get_active_day(interaction.user.id):
            await interaction.response.send_message(
                embed=make_embed("No Active Day", "⚠️ Start a day first with `/startday`."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=make_embed("Check-in Test", "📩 Sending a test check-in to your DMs."),
            ephemeral=True,
        )
        await run_checkin(interaction.user.id, "Manual test check-in.")
        return

    if option.value == "closeday":
        day = get_active_day(interaction.user.id)
        if not day:
            await interaction.response.send_message(
                embed=make_embed("No Active Day", "⚠️ No active day to preview."),
                ephemeral=True,
            )
            return
        total, totals = day_summary(day["id"])
        await interaction.response.send_message(embed=build_summary_embed("Close Day Preview", total, totals), ephemeral=True)
        return

    await send_weekly_graph_test(interaction)


async def send_weekly_graph_test(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    daily, week_total, task_totals = weekly_summary(interaction.user.id, local_now().date())
    path = make_dashboard_image(interaction.user.display_name, interaction.user.id, daily, week_total, task_totals, local_now().date())
    await interaction.followup.send(file=discord.File(path, filename="weekly-productivity-test.png"), ephemeral=True)


@bot.event
async def on_ready():
    global commands_synced
    init_db()
    global_synced = []
    guild_synced = []
    cleared_guilds = 0
    if not commands_synced:
        if GUILD_ID:
            target_guild = discord.Object(id=GUILD_ID)
            bot.tree.clear_commands(guild=target_guild)
            bot.tree.copy_global_to(guild=target_guild)
            guild_synced = await bot.tree.sync(guild=target_guild)

            for guild in bot.guilds:
                if guild.id == GUILD_ID:
                    continue
                stale_guild = discord.Object(id=guild.id)
                bot.tree.clear_commands(guild=stale_guild)
                await bot.tree.sync(guild=stale_guild)
                cleared_guilds += 1

            bot.tree.clear_commands(guild=None)
            global_synced = await bot.tree.sync()
        else:
            global_synced = await bot.tree.sync()
            for guild in bot.guilds:
                stale_guild = discord.Object(id=guild.id)
                bot.tree.clear_commands(guild=stale_guild)
                await bot.tree.sync(guild=stale_guild)
                cleared_guilds += 1
        commands_synced = True
    if not checkin_scheduler.is_running():
        checkin_scheduler.start()
    if not weekly_dashboard_scheduler.is_running():
        weekly_dashboard_scheduler.start()
    print(
        f"{bot.user} is online. "
        f"Synced {len(global_synced)} global slash command(s)"
        + (f" and {len(guild_synced)} guild slash command(s)." if GUILD_ID else ".")
        + (f" Cleared stale commands from {cleared_guilds} other guild(s)." if cleared_guilds else "")
    )


def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
