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

# Preserved settings from the previous bot.
PROJECT_NAME = "Project Floppa"
REPORT_CHANNEL_ID = 1490317756136947942
TIMEZONE = "Europe/Bratislava"
USD_PER_ROBUX = 0.0038
EMBED_COLOR = discord.Color.from_rgb(255, 255, 255)

ROLE_NAME = "Executive"
SELF_CARE_TASK = "Self-care"
CHECKIN_INTERVAL = dt.timedelta(minutes=30)
CHECKIN_RETRY_DELAYS = (60, 10, 10, 10)
MAX_CUSTOM_TASKS = 24

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

conn = None
checkin_locks = set()


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
            discord_user_id BIGINT PRIMARY KEY,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'Executive',
            channel_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS work_days (
            id SERIAL PRIMARY KEY,
            discord_user_id BIGINT NOT NULL REFERENCES profiles(discord_user_id),
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


def ensure_profile(user: discord.abc.User, display_name=None, channel_id=None):
    name = display_name or user.display_name
    db_exec(
        """
        INSERT INTO profiles (discord_user_id, display_name, role, channel_id)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (discord_user_id)
        DO UPDATE SET
            display_name = EXCLUDED.display_name,
            role = EXCLUDED.role,
            channel_id = COALESCE(EXCLUDED.channel_id, profiles.channel_id)
        """,
        (user.id, name, ROLE_NAME, channel_id),
    )


def get_profile(user_id: int):
    return db_one("SELECT * FROM profiles WHERE discord_user_id = %s", (user_id,))


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
    minutes = rem // 60
    return f"{hours}h {minutes}m"


def build_summary_text(title: str, total: dt.timedelta, totals: dict[str, dt.timedelta]):
    lines = [f"**{title}**", f"Total worked: **{format_duration(total)}**", ""]
    if not totals:
        lines.append("No tracked time yet.")
        return "\n".join(lines)

    for name, duration in sorted(totals.items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- **{name}**: {format_duration(duration)}")
    return "\n".join(lines)


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
        await interaction.response.send_message("Saved.", ephemeral=True)
        self.view.stop()


async def ask_task_choice(user: discord.User, day_id: int, reason: str):
    task_rows = get_day_tasks(day_id)
    if not task_rows:
        return None, "no_tasks"

    prompt = "What are you doing right now?"
    for attempt, timeout in enumerate(CHECKIN_RETRY_DELAYS, start=1):
        try:
            view = TaskSelectView(task_rows, prompt)
            await user.send(
                f"**{PROJECT_NAME} productivity check-in**\n{reason}\nChoose your current task:",
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


async def run_checkin(user_id: int, reason: str):
    if user_id in checkin_locks:
        return
    checkin_locks.add(user_id)
    try:
        day = get_active_day(user_id)
        if not day:
            return

        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        asked_at = utc_now()
        task_id, source = await ask_task_choice(user, day["id"], reason)
        answered_at = utc_now() if source == "answered" else None
        if task_id:
            switch_task(day["id"], task_id, answered_at or utc_now(), source)

        db_exec(
            """
            INSERT INTO checkins (day_id, task_id, asked_at, answered_at, source)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (day["id"], task_id, asked_at, answered_at, source),
        )
        set_next_checkin(day["id"], utc_now() + CHECKIN_INTERVAL)
    finally:
        checkin_locks.discard(user_id)


@discord_tasks.loop(minutes=1)
async def checkin_scheduler():
    rows = db_all(
        """
        SELECT discord_user_id
        FROM work_days
        WHERE status = 'active' AND next_checkin_at IS NOT NULL AND next_checkin_at <= %s
        """,
        (utc_now(),),
    )
    for row in rows:
        asyncio.create_task(run_checkin(row["discord_user_id"], "Scheduled 30-minute check-in."))


def create_day(user_id: int, task_names: list[str]):
    started_at = utc_now()
    work_date = started_at.astimezone(ZoneInfo(TIMEZONE)).date()
    with get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO work_days (discord_user_id, work_date, started_at, status)
            VALUES (%s, %s, %s, 'active')
            RETURNING id
            """,
            (user_id, work_date, started_at),
        )
        day_id = cur.fetchone()["id"]
        for index, name in enumerate(task_names, start=1):
            cur.execute(
                """
                INSERT INTO day_tasks (day_id, name, sort_order)
                VALUES (%s, %s, %s)
                """,
                (day_id, name, index),
            )
    return day_id


def weekly_range(anchor: dt.date):
    start = anchor - dt.timedelta(days=anchor.weekday())
    end = start + dt.timedelta(days=7)
    return start, end


def weekly_rows(user_id: int, anchor: dt.date):
    start, end = weekly_range(anchor)
    return db_all(
        """
        SELECT id, work_date, started_at, closed_at, status
        FROM work_days
        WHERE discord_user_id = %s AND work_date >= %s AND work_date < %s
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


def get_font(size: int, bold=False):
    names = ["arialbd.ttf" if bold else "arial.ttf", "seguiemj.ttf", "calibri.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_dashboard_image(profile, daily, week_total, task_totals, anchor: dt.date):
    width, height = 1200, 800
    image = Image.new("RGB", (width, height), "#07111f")
    draw = ImageDraw.Draw(image)
    title_font = get_font(48, True)
    heading_font = get_font(28, True)
    body_font = get_font(24)
    small_font = get_font(18)

    for y in range(height):
        blue = 31 + int(y / height * 30)
        draw.line((0, y, width, y), fill=(7, 17, blue))

    start, end = weekly_range(anchor)
    draw.rounded_rectangle((40, 40, 1160, 760), radius=24, fill="#0b1628", outline="#17345c", width=2)
    draw.text((80, 78), "Weekly Productivity", font=title_font, fill="#f5f9ff")
    draw.text((82, 138), f"{profile['display_name']} | {ROLE_NAME} | {start} to {end - dt.timedelta(days=1)}", font=body_font, fill="#8fb7e8")
    draw.text((880, 86), format_duration(week_total), font=title_font, fill="#58a6ff")
    draw.text((884, 142), "total focused time", font=small_font, fill="#9fb3c8")

    max_daily = max([total.total_seconds() for _, total, _ in daily] + [1])
    x, y = 82, 220
    draw.text((x, y - 45), "Daily totals", font=heading_font, fill="#f5f9ff")
    for index in range(7):
        day = start + dt.timedelta(days=index)
        match = next((item for item in daily if item[0] == day), None)
        total = match[1] if match else dt.timedelta()
        bar_h = int((total.total_seconds() / max_daily) * 210)
        bx = x + index * 90
        draw.rounded_rectangle((bx, y, bx + 54, y + 230), radius=12, fill="#101f36")
        draw.rounded_rectangle((bx, y + 230 - bar_h, bx + 54, y + 230), radius=12, fill="#2f81f7")
        draw.text((bx - 2, y + 246), day.strftime("%a"), font=small_font, fill="#c7d7ea")
        draw.text((bx - 10, y + 272), format_duration(total), font=small_font, fill="#8fb7e8")

    tx, ty = 760, 220
    draw.text((tx, ty - 45), "Top tasks", font=heading_font, fill="#f5f9ff")
    max_task = max([duration.total_seconds() for duration in task_totals.values()] + [1])
    for index, (task, duration) in enumerate(sorted(task_totals.items(), key=lambda item: item[1], reverse=True)[:8]):
        row_y = ty + index * 56
        draw.text((tx, row_y), task[:26], font=body_font, fill="#eaf2ff")
        draw.text((1040, row_y), format_duration(duration), font=small_font, fill="#8fb7e8")
        bar_w = int((duration.total_seconds() / max_task) * 330)
        draw.rounded_rectangle((tx, row_y + 32, tx + 330, row_y + 42), radius=5, fill="#10223d")
        draw.rounded_rectangle((tx, row_y + 32, tx + bar_w, row_y + 42), radius=5, fill="#58a6ff")

    output = os.path.join(tempfile.gettempdir(), f"productivity_week_{profile['discord_user_id']}_{anchor}.png")
    image.save(output)
    return output


@bot.tree.command(name="profile", description="Create or update your productivity profile.")
@app_commands.describe(display_name="Profile display name", channel_id="Your manually-created private channel ID")
async def profile(interaction: discord.Interaction, display_name: str, channel_id: str | None = None):
    parsed_channel_id = int(channel_id) if channel_id else None
    ensure_profile(interaction.user, display_name, parsed_channel_id)
    await interaction.response.send_message(
        f"Profile saved: **{display_name}** | Role: **{ROLE_NAME}**",
        ephemeral=True,
    )


@bot.tree.command(name="startday", description="Start your productivity day and send today's tasks.")
async def startday(interaction: discord.Interaction):
    if get_active_day(interaction.user.id):
        await interaction.response.send_message("You already have an active day. Use `/closeday` first.", ephemeral=True)
        return

    ensure_profile(interaction.user)
    await interaction.response.send_message(
        "Send today's tasks in this channel now, one task per line. I will add `Self-care` as the last option.",
        ephemeral=True,
    )

    def check(message: discord.Message):
        return message.author.id == interaction.user.id and message.channel.id == interaction.channel_id

    try:
        message = await bot.wait_for("message", check=check, timeout=300)
    except asyncio.TimeoutError:
        await interaction.followup.send("Timed out waiting for your task list.", ephemeral=True)
        return

    task_names = [line.strip() for line in message.content.splitlines() if line.strip()]
    task_names = [name for name in task_names if name.lower() != SELF_CARE_TASK.lower()]
    if not task_names:
        await interaction.followup.send("No tasks found. Start again and send at least one task.", ephemeral=True)
        return
    if len(task_names) > MAX_CUSTOM_TASKS:
        await interaction.followup.send(f"Too many tasks. Send up to {MAX_CUSTOM_TASKS} tasks plus Self-care.", ephemeral=True)
        return

    task_names.append(SELF_CARE_TASK)
    day_id = create_day(interaction.user.id, task_names)
    await interaction.followup.send(
        f"Day started with {len(task_names)} tasks. Check your DMs to choose your starting task.",
        ephemeral=True,
    )
    await run_checkin(interaction.user.id, "Choose what you are starting with.")


@bot.tree.command(name="closeday", description="Close your active day and show the time spent per task.")
async def closeday(interaction: discord.Interaction):
    day = get_active_day(interaction.user.id)
    if not day:
        await interaction.response.send_message("You do not have an active day.", ephemeral=True)
        return

    closed_at = utc_now()
    db_exec("UPDATE task_segments SET ended_at = %s WHERE day_id = %s AND ended_at IS NULL", (closed_at, day["id"]))
    db_exec("UPDATE work_days SET status = 'closed', closed_at = %s, next_checkin_at = NULL WHERE id = %s", (closed_at, day["id"]))
    total, totals = day_summary(day["id"], closed_at)
    await interaction.response.send_message(build_summary_text("Day closed", total, totals))


@bot.tree.command(name="stats", description="Show a user's saved profile stats.")
@app_commands.describe(user="User to check")
async def stats(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user
    profile_row = get_profile(target.id)
    if not profile_row:
        await interaction.response.send_message("No profile found for that user.", ephemeral=True)
        return

    days = db_all(
        "SELECT id, closed_at FROM work_days WHERE discord_user_id = %s AND status = 'closed'",
        (target.id,),
    )
    total = dt.timedelta()
    for row in days:
        day_total, _ = day_summary(row["id"], row["closed_at"])
        total += day_total
    average = total / len(days) if days else dt.timedelta()
    await interaction.response.send_message(
        f"**{profile_row['display_name']}**\nRole: **{ROLE_NAME}**\n"
        f"Closed days: **{len(days)}**\nDaily average: **{format_duration(average)}**"
    )


@bot.tree.command(name="weekly", description="Show weekly productivity stats.")
@app_commands.describe(user="User to check", mode="How to show the weekly stats", date="Any date in the target week, YYYY-MM-DD")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="overview_text", value="overview"),
        app_commands.Choice(name="separate_days", value="days"),
        app_commands.Choice(name="dashboard_image", value="image"),
    ]
)
async def weekly(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    mode: app_commands.Choice[str] | None = None,
    date: str | None = None,
):
    await interaction.response.defer()
    target = user or interaction.user
    profile_row = get_profile(target.id)
    if not profile_row:
        await interaction.followup.send("No profile found for that user.")
        return

    try:
        anchor = parse_local_date(date)
    except ValueError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return

    selected_mode = mode.value if mode else "overview"
    daily, week_total, task_totals = weekly_summary(target.id, anchor)
    if selected_mode == "image":
        path = make_dashboard_image(profile_row, daily, week_total, task_totals, anchor)
        await interaction.followup.send(file=discord.File(path, filename="weekly-productivity.png"))
        return

    if selected_mode == "days":
        if not daily:
            await interaction.followup.send("No days found for that week.")
            return
        for work_date, total, totals in daily:
            await interaction.followup.send(build_summary_text(str(work_date), total, totals))
        return

    start, end = weekly_range(anchor)
    text = build_summary_text(f"Week {start} to {end - dt.timedelta(days=1)}", week_total, task_totals)
    for chunk in split_discord_messages(text):
        await interaction.followup.send(chunk)


@bot.tree.command(name="daystats", description="Show one saved day for a user.")
@app_commands.describe(user="User to check", date="Day to show, YYYY-MM-DD")
async def daystats(interaction: discord.Interaction, user: discord.Member | None = None, date: str | None = None):
    target = user or interaction.user
    try:
        work_date = parse_local_date(date)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    row = db_one(
        """
        SELECT id, closed_at
        FROM work_days
        WHERE discord_user_id = %s AND work_date = %s
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (target.id, work_date),
    )
    if not row:
        await interaction.response.send_message("No day found for that date.", ephemeral=True)
        return
    total, totals = day_summary(row["id"], row["closed_at"] or utc_now())
    await interaction.response.send_message(build_summary_text(str(work_date), total, totals))


@bot.tree.command(name="test_profile", description="Test profile saving.")
async def test_profile(interaction: discord.Interaction):
    ensure_profile(interaction.user)
    await interaction.response.send_message("Profile test passed.", ephemeral=True)


@bot.tree.command(name="test_startday", description="Test the startday prompt.")
async def test_startday(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Startday test: `/startday` will ask for a multiline task message, then append Self-care.",
        ephemeral=True,
    )


@bot.tree.command(name="test_checkin", description="Send yourself a check-in using your active day.")
async def test_checkin(interaction: discord.Interaction):
    if not get_active_day(interaction.user.id):
        await interaction.response.send_message("Start a day first with `/startday`.", ephemeral=True)
        return
    await interaction.response.send_message("Sending test check-in to your DMs.", ephemeral=True)
    await run_checkin(interaction.user.id, "Manual test check-in.")


@bot.tree.command(name="test_closeday", description="Preview your active day totals without closing it.")
async def test_closeday(interaction: discord.Interaction):
    day = get_active_day(interaction.user.id)
    if not day:
        await interaction.response.send_message("No active day to preview.", ephemeral=True)
        return
    total, totals = day_summary(day["id"])
    await interaction.response.send_message(build_summary_text("Close day preview", total, totals), ephemeral=True)


@bot.tree.command(name="test_weekly_image", description="Generate a test weekly dashboard image.")
async def test_weekly_image(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    profile_row = get_profile(interaction.user.id)
    if not profile_row:
        ensure_profile(interaction.user)
        profile_row = get_profile(interaction.user.id)
    daily, week_total, task_totals = weekly_summary(interaction.user.id, local_now().date())
    path = make_dashboard_image(profile_row, daily, week_total, task_totals, local_now().date())
    await interaction.followup.send(file=discord.File(path, filename="weekly-productivity-test.png"), ephemeral=True)


@bot.event
async def on_ready():
    init_db()
    synced = await bot.tree.sync()
    if not checkin_scheduler.is_running():
        checkin_scheduler.start()
    print(f"{bot.user} is online. Synced {len(synced)} slash command(s).")


def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
