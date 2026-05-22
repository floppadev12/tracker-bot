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
TIMEZONE = "Europe/Bratislava"
USD_PER_ROBUX = 0.0038
EMBED_COLOR = discord.Color(0xFFF9EB)

ROLE_NAME = "Executive"
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
            status TEXT NOT NULL DEFAULT 'active',
            next_checkin_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS day_tasks (
            id SERIAL PRIMARY KEY,
            day_id INTEGER NOT NULL REFERENCES work_days(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL
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


def ensure_profile(user: discord.abc.User, display_name=None, channel_id=None):
    name = display_name or user.display_name
    return db_one(
        """
        INSERT INTO profiles (owner_discord_user_id, display_name, role, channel_id)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (display_name)
        DO UPDATE SET
            owner_discord_user_id = EXCLUDED.owner_discord_user_id,
            role = EXCLUDED.role,
            channel_id = COALESCE(EXCLUDED.channel_id, profiles.channel_id)
        RETURNING *
        """,
        (user.id, name, ROLE_NAME, channel_id),
    )


def get_profile(profile_id: int):
    return db_one("SELECT * FROM profiles WHERE id = %s", (profile_id,))


def get_default_profile(user_id: int):
    return db_one(
        """
        SELECT *
        FROM profiles
        WHERE owner_discord_user_id = %s
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (user_id,),
    )


def get_profile_by_selector(selector: str | None, fallback_user_id: int):
    if not selector:
        return get_default_profile(fallback_user_id)

    try:
        profile_id = int(selector)
    except (TypeError, ValueError):
        return db_one(
            """
            SELECT *
            FROM profiles
            WHERE LOWER(display_name) = LOWER(%s)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (selector,),
        )
    return get_profile(profile_id)


async def profile_autocomplete(interaction: discord.Interaction, current: str):
    search = f"%{current}%"
    rows = db_all(
        """
        SELECT id, display_name
        FROM profiles
        WHERE %s = '' OR display_name ILIKE %s
        ORDER BY display_name ASC
        LIMIT 25
        """,
        (current, search),
    )
    return [
        app_commands.Choice(name=row["display_name"][:100], value=str(row["id"]))
        for row in rows
    ]


def get_active_day(profile_id: int):
    return db_one(
        """
        SELECT * FROM work_days
        WHERE profile_id = %s AND status = 'active'
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (profile_id,),
    )


def get_day_tasks(day_id: int):
    return db_all(
        "SELECT * FROM day_tasks WHERE day_id = %s ORDER BY sort_order ASC",
        (day_id,),
    )


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
    def __init__(self, task_rows, prompt: str):
        super().__init__(timeout=None)
        self.future = asyncio.get_running_loop().create_future()
        self.add_item(TaskSelect(task_rows, prompt, self.future))


class TaskSelect(discord.ui.Select):
    def __init__(self, task_rows, prompt: str, future: asyncio.Future):
        self.future = future
        options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]))
            for row in task_rows
        ]
        super().__init__(placeholder=prompt[:100], options=options)

    async def callback(self, interaction: discord.Interaction):
        if not self.future.done():
            self.future.set_result((int(self.values[0]), interaction.created_at))
        await interaction.response.send_message("✅ Saved.", ephemeral=True)
        self.view.stop()


class StartDayTaskModal(discord.ui.Modal, title="Start productivity day"):
    def __init__(self, profile_row):
        super().__init__()
        self.profile_row = profile_row

    task_list = discord.ui.TextInput(
        label="Today's tasks",
        placeholder="Write one task per line. Self-care is added automatically.",
        style=discord.TextStyle.paragraph,
        min_length=1,
        max_length=1800,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if get_active_day(self.profile_row["id"]):
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
        day_id, started_at = create_day(self.profile_row["id"], self.profile_row["owner_discord_user_id"], task_names)
        display_name = self.profile_row["display_name"]
        await interaction.response.send_message(
            embed=make_embed(
                f"Welcome back, {display_name}",
                f"👋 Day started with **{len(task_names)}** tasks.\nCheck your DMs to choose what you are starting with.",
            ),
            ephemeral=True,
        )
        asyncio.create_task(run_checkin(self.profile_row["id"], "Choose what you are starting with.", started_at))


async def ask_task_choice(user: discord.User, day_id: int, reason: str):
    task_rows = get_day_tasks(day_id)
    if not task_rows:
        return None, "no_tasks"

    prompt = "What are you doing right now?"
    for attempt, timeout in enumerate(CHECKIN_RETRY_DELAYS, start=1):
        try:
            view = TaskSelectView(task_rows, prompt)
            await user.send(
                embed=make_embed(f"📌 {PROJECT_NAME} productivity check-in", reason),
                view=view,
            )
            task_id, answered_at = await asyncio.wait_for(view.future, timeout=timeout)
            return task_id, "answered"
        except (asyncio.TimeoutError, discord.Forbidden):
            prompt = "Still need your current task"
            if attempt == 1:
                reason = "No answer yet. I will ask a few quick times before keeping your previous task."
            else:
                reason = "Quick retry."

    previous = get_open_segment(day_id)
    if previous:
        return previous["task_id"], "previous_task"
    return task_rows[0]["id"], "default_first_task"


async def run_checkin(profile_id: int, reason: str, segment_started_at: dt.datetime | None = None):
    if profile_id in checkin_locks:
        return
    checkin_locks.add(profile_id)
    try:
        day = get_active_day(profile_id)
        if not day:
            return

        profile_row = get_profile(profile_id)
        if not profile_row:
            return
        user_id = profile_row["owner_discord_user_id"]
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        asked_at = utc_now()
        task_id, source = await ask_task_choice(user, day["id"], reason)
        answered_at = utc_now() if source == "answered" else None
        if task_id:
            start_at = segment_started_at or answered_at or utc_now()
            switch_task(day["id"], task_id, start_at, source)

        db_exec(
            """
            INSERT INTO checkins (day_id, task_id, asked_at, answered_at, source)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (day["id"], task_id, asked_at, answered_at, source),
        )
        set_next_checkin(day["id"], utc_now() + CHECKIN_INTERVAL)
    finally:
        checkin_locks.discard(profile_id)


@discord_tasks.loop(minutes=1)
async def checkin_scheduler():
    rows = db_all(
        """
        SELECT profile_id
        FROM work_days
        WHERE status = 'active' AND profile_id IS NOT NULL AND next_checkin_at IS NOT NULL AND next_checkin_at <= %s
        """,
        (utc_now(),),
    )
    for row in rows:
        asyncio.create_task(run_checkin(row["profile_id"], "Scheduled 30-minute check-in."))


def create_day(profile_id: int, owner_user_id: int, task_names: list[str]):
    started_at = utc_now()
    work_date = started_at.astimezone(ZoneInfo(TIMEZONE)).date()
    with get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO work_days (profile_id, discord_user_id, work_date, started_at, status)
            VALUES (%s, %s, %s, %s, 'active')
            RETURNING id
            """,
            (profile_id, owner_user_id, work_date, started_at),
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


def weekly_rows(profile_id: int, anchor: dt.date):
    start, end = weekly_range(anchor)
    return db_all(
        """
        SELECT id, work_date, started_at, closed_at, status
        FROM work_days
        WHERE profile_id = %s AND work_date >= %s AND work_date < %s
        ORDER BY work_date ASC, started_at ASC
        """,
        (profile_id, start, end),
    )


def weekly_summary(profile_id: int, anchor: dt.date):
    rows = weekly_rows(profile_id, anchor)
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


def get_font(size: int, bold=False):
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


def make_dashboard_image(profile, daily, week_total, task_totals, anchor: dt.date):
    width, height = 1400, 820
    image = Image.new("RGB", (width, height), "#050b16")
    draw = ImageDraw.Draw(image)
    title_font = get_font(56, True)
    metric_font = get_font(46, True)
    heading_font = get_font(30, True)
    body_font = get_font(23)
    small_font = get_font(18)
    tiny_font = get_font(15)

    for y in range(height):
        for x in range(width):
            r = 5 + int((x / width) * 8)
            g = 10 + int((y / height) * 16)
            b = 24 + int((x / width) * 36) + int((y / height) * 18)
            image.putpixel((x, y), (r, g, min(78, b)))

    def panel(box, fill="#0b1628", outline="#1c3b67"):
        draw.rounded_rectangle(box, radius=28, fill=fill, outline=outline, width=2)

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
    panel((34, 34, 1366, 786), "#071326", "#1e4f87")
    draw.text((78, 72), "Weekly Productivity", font=title_font, fill="#f7fbff")
    draw.text(
        (82, 140),
        f"{profile['display_name']}  |  {ROLE_NAME}  |  {start} to {end - dt.timedelta(days=1)}",
        font=body_font,
        fill="#8fb7e8",
    )

    panel((940, 72, 1308, 176), "#0d1d35", "#255c99")
    draw.text((972, 88), format_duration(week_total), font=metric_font, fill="#66c2ff")
    draw.text((976, 142), "total focused time", font=small_font, fill="#a9bdd4")

    max_daily = max([total.total_seconds() for _, total, _ in daily] + [1])
    x, y = 82, 246
    panel((64, 210, 858, 720), "#0a172b", "#193b67")
    draw.text((x, y - 6), "Daily totals", font=heading_font, fill="#f5f9ff")
    chart_top = y + 58
    chart_bottom = 642
    chart_height = chart_bottom - chart_top
    for index in range(7):
        day = start + dt.timedelta(days=index)
        match = next((item for item in daily if item[0] == day), None)
        total = match[1] if match else dt.timedelta()
        bar_h = int((total.total_seconds() / max_daily) * chart_height)
        bx = x + 28 + index * 102
        draw.rounded_rectangle((bx, chart_top, bx + 58, chart_bottom), radius=16, fill="#0f2541")
        if bar_h:
            gradient_bar((bx, chart_bottom - bar_h, bx + 58, chart_bottom), (47, 129, 247), (102, 194, 255), 16)
        draw.text((bx + 8, chart_bottom + 18), day.strftime("%a"), font=small_font, fill="#dbe9fb")
        label = format_duration(total)
        label_w = draw.textbbox((0, 0), label, font=tiny_font)[2]
        draw.text((bx + 29 - label_w / 2, chart_bottom + 45), label, font=tiny_font, fill="#8fb7e8")

    tx, ty = 930, 246
    panel((900, 210, 1308, 720), "#0a172b", "#193b67")
    draw.text((tx, ty - 6), "Top tasks", font=heading_font, fill="#f5f9ff")
    max_task = max([duration.total_seconds() for duration in task_totals.values()] + [1])
    top_tasks = sorted(task_totals.items(), key=lambda item: item[1], reverse=True)[:7]
    if not top_tasks:
        draw.text((tx, ty + 68), "No tracked tasks yet", font=body_font, fill="#8fb7e8")
    for index, (task, duration) in enumerate(top_tasks):
        row_y = ty + 62 + index * 58
        name = task if len(task) <= 24 else task[:21] + "..."
        draw.text((tx, row_y), name, font=body_font, fill="#edf6ff")
        duration_text = format_duration(duration)
        duration_w = draw.textbbox((0, 0), duration_text, font=small_font)[2]
        draw.text((1266 - duration_w, row_y + 3), duration_text, font=small_font, fill="#9ecfff")
        bar_w = int((duration.total_seconds() / max_task) * 336)
        draw.rounded_rectangle((tx, row_y + 34, tx + 336, row_y + 47), radius=7, fill="#102944")
        if bar_w:
            gradient_bar((tx, row_y + 34, tx + bar_w, row_y + 47), (88, 166, 255), (53, 229, 255), 7)

    draw.text((82, 738), "Generated by the productivity tracker", font=tiny_font, fill="#597797")

    output = os.path.join(tempfile.gettempdir(), f"productivity_week_{profile['id']}_{anchor}.png")
    image.save(output)
    return output


@bot.tree.command(name="createprofile", description="Create or update your productivity profile.")
@app_commands.describe(display_name="Profile display name", channel_id="Your manually-created private channel ID")
async def createprofile(interaction: discord.Interaction, display_name: str, channel_id: str | None = None):
    parsed_channel_id = int(channel_id) if channel_id else None
    ensure_profile(interaction.user, display_name, parsed_channel_id)
    await interaction.response.send_message(
        embed=make_embed("Profile Saved", f"✅ **{display_name}**\nRole: **{ROLE_NAME}**"),
        ephemeral=True,
    )


@bot.tree.command(name="startday", description="Start your productivity day and send today's tasks.")
@app_commands.describe(profile_name="Saved bot profile to start")
@app_commands.autocomplete(profile_name=profile_autocomplete)
async def startday(interaction: discord.Interaction, profile_name: str | None = None):
    profile_row = get_profile_by_selector(profile_name, interaction.user.id)
    if not profile_row:
        await interaction.response.send_message(
            embed=make_embed("Profile Missing", "Create a profile first with `/createprofile`."),
            ephemeral=True,
        )
        return

    if get_active_day(profile_row["id"]):
        await interaction.response.send_message(
            embed=make_embed("Active Day", "⚠️ You already have an active day. Use `/closeday` first."),
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(StartDayTaskModal(profile_row))


@bot.tree.command(name="closeday", description="Close your active day and show the time spent per task.")
@app_commands.describe(profile_name="Saved bot profile to close")
@app_commands.autocomplete(profile_name=profile_autocomplete)
async def closeday(interaction: discord.Interaction, profile_name: str | None = None):
    profile_row = get_profile_by_selector(profile_name, interaction.user.id)
    if not profile_row:
        await interaction.response.send_message(
            embed=make_embed("Profile Missing", "Create a profile first with `/createprofile`."),
            ephemeral=True,
        )
        return

    day = get_active_day(profile_row["id"])
    if not day:
        await interaction.response.send_message(
            embed=make_embed("No Active Day", "⚠️ You do not have an active day."),
            ephemeral=True,
        )
        return

    closed_at = utc_now()
    db_exec("UPDATE task_segments SET ended_at = %s WHERE day_id = %s AND ended_at IS NULL", (closed_at, day["id"]))
    db_exec("UPDATE work_days SET status = 'closed', closed_at = %s, next_checkin_at = NULL WHERE id = %s", (closed_at, day["id"]))
    total, totals = day_summary(day["id"], closed_at)
    await interaction.response.send_message(embed=build_summary_embed("✅ Day Closed", total, totals))


@bot.tree.command(name="profile", description="Show a user's saved profile stats.")
@app_commands.describe(profile_name="Saved bot profile to check")
@app_commands.autocomplete(profile_name=profile_autocomplete)
async def profile(interaction: discord.Interaction, profile_name: str | None = None):
    profile_row = get_profile_by_selector(profile_name, interaction.user.id)
    if not profile_row:
        await interaction.response.send_message(
            embed=make_embed("Profile Missing", "⚠️ No profile found for that user."),
            ephemeral=True,
        )
        return

    days = db_all(
        "SELECT id, closed_at FROM work_days WHERE profile_id = %s AND status = 'closed'",
        (profile_row["id"],),
    )
    total = dt.timedelta()
    for row in days:
        day_total, _ = day_summary(row["id"], row["closed_at"])
        total += day_total
    average = total / len(days) if days else dt.timedelta()
    await interaction.response.send_message(
        embed=make_embed(
            f"👤 {profile_row['display_name']}",
            f"Role: **{ROLE_NAME}**\n📅 Closed days: **{len(days)}**\n⏱️ Daily average: **{format_duration(average)}**",
        )
    )


@bot.tree.command(name="weekly", description="Show weekly productivity stats.")
@app_commands.describe(profile_name="Saved bot profile to check", mode="How to show the weekly stats", date="Any date in the target week, YYYY-MM-DD")
@app_commands.autocomplete(profile_name=profile_autocomplete)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="overview_text", value="overview"),
        app_commands.Choice(name="separate_days", value="days"),
        app_commands.Choice(name="dashboard_image", value="image"),
    ]
)
async def weekly(
    interaction: discord.Interaction,
    profile_name: str | None = None,
    mode: app_commands.Choice[str] | None = None,
    date: str | None = None,
):
    await interaction.response.defer()
    profile_row = get_profile_by_selector(profile_name, interaction.user.id)
    if not profile_row:
        await interaction.followup.send(embed=make_embed("Profile Missing", "⚠️ No profile found for that user."))
        return

    try:
        anchor = parse_local_date(date)
    except ValueError as exc:
        await interaction.followup.send(embed=make_embed("Invalid Date", str(exc)), ephemeral=True)
        return

    selected_mode = mode.value if mode else "overview"
    daily, week_total, task_totals = weekly_summary(profile_row["id"], anchor)
    if selected_mode == "image":
        path = make_dashboard_image(profile_row, daily, week_total, task_totals, anchor)
        await interaction.followup.send(file=discord.File(path, filename="weekly-productivity.png"))
        return

    if selected_mode == "days":
        if not daily:
            await interaction.followup.send(embed=make_embed("No Days Found", "📭 No days found for that week."))
            return
        for work_date, total, totals in daily:
            await interaction.followup.send(embed=build_summary_embed(str(work_date), total, totals))
        return

    start, end = weekly_range(anchor)
    text = build_summary_text(f"Week {start} to {end - dt.timedelta(days=1)}", week_total, task_totals)
    for chunk in split_discord_messages(text):
        await interaction.followup.send(embed=make_embed("Weekly Overview", chunk))


@bot.tree.command(name="daystats", description="Show one saved day for a user.")
@app_commands.describe(profile_name="Saved bot profile to check", date="Day to show, YYYY-MM-DD")
@app_commands.autocomplete(profile_name=profile_autocomplete)
async def daystats(interaction: discord.Interaction, profile_name: str | None = None, date: str | None = None):
    profile_row = get_profile_by_selector(profile_name, interaction.user.id)
    if not profile_row:
        await interaction.response.send_message(
            embed=make_embed("Profile Missing", "⚠️ No profile found for that user."),
            ephemeral=True,
        )
        return

    try:
        work_date = parse_local_date(date)
    except ValueError as exc:
        await interaction.response.send_message(embed=make_embed("Invalid Date", str(exc)), ephemeral=True)
        return

    row = db_one(
        """
        SELECT id, closed_at
        FROM work_days
        WHERE profile_id = %s AND work_date = %s
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (profile_row["id"], work_date),
    )
    if not row:
        await interaction.response.send_message(embed=make_embed("No Day Found", "📭 No day found for that date."), ephemeral=True)
        return
    total, totals = day_summary(row["id"], row["closed_at"] or utc_now())
    await interaction.response.send_message(embed=build_summary_embed(str(work_date), total, totals))


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
        ensure_profile(interaction.user)
        await interaction.response.send_message(
            embed=make_embed("Test Passed", "✅ Profile saving works. Use `/createprofile` to set your display name."),
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
        profile_row = get_default_profile(interaction.user.id)
        if not profile_row or not get_active_day(profile_row["id"]):
            await interaction.response.send_message(
                embed=make_embed("No Active Day", "⚠️ Start a day first with `/startday`."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=make_embed("Check-in Test", "📩 Sending a test check-in to your DMs."),
            ephemeral=True,
        )
        await run_checkin(profile_row["id"], "Manual test check-in.")
        return

    if option.value == "closeday":
        profile_row = get_default_profile(interaction.user.id)
        day = get_active_day(profile_row["id"]) if profile_row else None
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
    profile_row = get_default_profile(interaction.user.id)
    if not profile_row:
        profile_row = ensure_profile(interaction.user)
    daily, week_total, task_totals = weekly_summary(profile_row["id"], local_now().date())
    path = make_dashboard_image(profile_row, daily, week_total, task_totals, local_now().date())
    await interaction.followup.send(file=discord.File(path, filename="weekly-productivity-test.png"), ephemeral=True)


@bot.event
async def on_ready():
    global commands_synced
    init_db()
    global_synced = []
    guild_synced = []
    if not commands_synced:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.clear_commands(guild=guild)
            bot.tree.copy_global_to(guild=guild)
            guild_synced = await bot.tree.sync(guild=guild)
            bot.tree.clear_commands(guild=None)
            global_synced = await bot.tree.sync()
        else:
            global_synced = await bot.tree.sync()
        commands_synced = True
    if not checkin_scheduler.is_running():
        checkin_scheduler.start()
    print(
        f"{bot.user} is online. "
        f"Synced {len(global_synced)} global slash command(s)"
        + (f" and {len(guild_synced)} guild slash command(s)." if GUILD_ID else ".")
    )


def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
