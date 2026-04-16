import os
import re
from datetime import datetime, timezone
from typing import Optional, List

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
MAINTENANCE_ROLE_ID = os.getenv("MAINTENANCE_ROLE_ID")

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
if not DISCORD_GUILD_ID:
    raise RuntimeError("Missing DISCORD_GUILD_ID")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL")
if not MAINTENANCE_ROLE_ID:
    raise RuntimeError("Missing MAINTENANCE_ROLE_ID")

GUILD_ID = int(DISCORD_GUILD_ID)
MAINTENANCE_ROLE_ID = int(MAINTENANCE_ROLE_ID)

EMBED_COLOR = discord.Color(0xFFC78A)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
db_pool: Optional[asyncpg.Pool] = None


# -----------------------------
# Utilities
# -----------------------------
def parse_duration(text: str) -> Optional[int]:
    text = text.strip().lower()
    match = re.fullmatch(r"\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*", text)
    if not match:
        return None

    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    total = hours * 60 + minutes
    return total if total > 0 else None


def format_duration(total_minutes: int) -> str:
    total_minutes = max(0, int(total_minutes))
    hours = total_minutes // 60
    minutes = total_minutes % 60

    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def has_maintenance_role(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return any(role.id == MAINTENANCE_ROLE_ID for role in interaction.user.roles)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -----------------------------
# Database
# -----------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fields (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS formats (
    id SERIAL PRIMARY KEY,
    field_id INTEGER NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS segments (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS projects (
    id SERIAL PRIMARY KEY,
    field_id INTEGER NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
    format_id INTEGER NOT NULL REFERENCES formats(id) ON DELETE CASCADE,
    name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('in_development', 'released', 'won', 'missed')),
    released_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_segment_hours (
    id SERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    minutes INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, segment_id)
);
"""


async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)

        for name in ["Build", "Script", "UI", "Thumbnail"]:
            await conn.execute(
                "INSERT INTO segments (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
                name,
            )


async def fetch_fields() -> List[asyncpg.Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT id, name FROM fields ORDER BY name ASC")


async def fetch_formats(field_id: int) -> List[asyncpg.Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT id, name FROM formats WHERE field_id = $1 ORDER BY name ASC",
            field_id,
        )


async def fetch_segments() -> List[asyncpg.Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT id, name FROM segments ORDER BY name ASC")


async def fetch_projects(field_id: int, format_id: int) -> List[asyncpg.Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT id, name, status
            FROM projects
            WHERE field_id = $1 AND format_id = $2
            ORDER BY name ASC
            """,
            field_id,
            format_id,
        )


async def fetch_project(project_id: int) -> Optional[asyncpg.Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT
                p.id,
                p.name,
                p.status,
                p.released_at,
                f.name AS field_name,
                fm.name AS format_name,
                p.field_id,
                p.format_id
            FROM projects p
            JOIN fields f ON f.id = p.field_id
            JOIN formats fm ON fm.id = p.format_id
            WHERE p.id = $1
            """,
            project_id,
        )


async def fetch_project_by_name(name: str) -> Optional[asyncpg.Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, name FROM projects WHERE LOWER(name) = LOWER($1)",
            name,
        )


async def fetch_project_segment_rows(project_id: int) -> List[asyncpg.Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT
                s.id AS segment_id,
                s.name AS segment_name,
                COALESCE(psh.minutes, 0) AS minutes
            FROM segments s
            LEFT JOIN project_segment_hours psh
                ON psh.segment_id = s.id
               AND psh.project_id = $1
            ORDER BY s.name ASC
            """,
            project_id,
        )


async def create_project(field_id: int, format_id: int, name: str) -> Optional[int]:
    async with db_pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO projects (field_id, format_id, name, status)
                VALUES ($1, $2, $3, 'in_development')
                RETURNING id
                """,
                field_id,
                format_id,
                name,
            )
            return row["id"]
        except asyncpg.UniqueViolationError:
            return None


async def add_project_minutes(project_id: int, segment_id: int, minutes: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO project_segment_hours (project_id, segment_id, minutes)
            VALUES ($1, $2, $3)
            ON CONFLICT (project_id, segment_id)
            DO UPDATE SET minutes = project_segment_hours.minutes + EXCLUDED.minutes
            """,
            project_id,
            segment_id,
            minutes,
        )
        await conn.execute(
            "UPDATE projects SET updated_at = NOW() WHERE id = $1",
            project_id,
        )


async def set_project_minutes(project_id: int, segment_id: int, minutes: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO project_segment_hours (project_id, segment_id, minutes)
            VALUES ($1, $2, $3)
            ON CONFLICT (project_id, segment_id)
            DO UPDATE SET minutes = EXCLUDED.minutes
            """,
            project_id,
            segment_id,
            minutes,
        )
        await conn.execute(
            "UPDATE projects SET updated_at = NOW() WHERE id = $1",
            project_id,
        )


async def release_project(project_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE projects
            SET status = 'released',
                released_at = NOW(),
                updated_at = NOW()
            WHERE id = $1
            """,
            project_id,
        )


async def set_project_status(project_id: int, status: str):
    async with db_pool.acquire() as conn:
        if status == "released":
            await conn.execute(
                """
                UPDATE projects
                SET status = 'released',
                    released_at = COALESCE(released_at, NOW()),
                    updated_at = NOW()
                WHERE id = $1
                """,
                project_id,
            )
        elif status == "in_development":
            await conn.execute(
                """
                UPDATE projects
                SET status = 'in_development',
                    released_at = NULL,
                    updated_at = NOW()
                WHERE id = $1
                """,
                project_id,
            )
        else:
            await conn.execute(
                """
                UPDATE projects
                SET status = $2,
                    updated_at = NOW()
                WHERE id = $1
                """,
                project_id,
                status,
            )


async def create_field(name: str) -> bool:
    async with db_pool.acquire() as conn:
        try:
            await conn.execute("INSERT INTO fields (name) VALUES ($1)", name)
            return True
        except asyncpg.UniqueViolationError:
            return False


async def delete_field(field_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM fields WHERE id = $1", field_id)


async def create_format(field_id: int, name: str) -> bool:
    async with db_pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO formats (field_id, name) VALUES ($1, $2)",
                field_id,
                name,
            )
            return True
        except asyncpg.UniqueViolationError:
            return False


async def delete_format(format_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM formats WHERE id = $1", format_id)


async def create_segment(name: str) -> bool:
    async with db_pool.acquire() as conn:
        try:
            await conn.execute("INSERT INTO segments (name) VALUES ($1)", name)
            return True
        except asyncpg.UniqueViolationError:
            return False


async def delete_segment(segment_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM segments WHERE id = $1", segment_id)


async def rename_project(project_id: int, new_name: str) -> bool:
    async with db_pool.acquire() as conn:
        try:
            await conn.execute(
                """
                UPDATE projects
                SET name = $2, updated_at = NOW()
                WHERE id = $1
                """,
                project_id,
                new_name,
            )
            return True
        except asyncpg.UniqueViolationError:
            return False


async def move_project(project_id: int, field_id: int, format_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE projects
            SET field_id = $2, format_id = $3, updated_at = NOW()
            WHERE id = $1
            """,
            project_id,
            field_id,
            format_id,
        )


async def fetch_winrate_overall():
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'won') AS won,
                COUNT(*) FILTER (WHERE status = 'missed') AS missed
            FROM projects
            WHERE status IN ('won', 'missed')
            """
        )


async def fetch_winrate_by_field(field_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'won') AS won,
                COUNT(*) FILTER (WHERE status = 'missed') AS missed
            FROM projects
            WHERE field_id = $1
              AND status IN ('won', 'missed')
            """,
            field_id,
        )


async def fetch_winrate_by_format(format_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'won') AS won,
                COUNT(*) FILTER (WHERE status = 'missed') AS missed
            FROM projects
            WHERE format_id = $1
              AND status IN ('won', 'missed')
            """,
            format_id,
        )


# -----------------------------
# Embed builders
# -----------------------------
def build_home_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Project Assistant",
        description="Hi, I’m **Nova**! How can I help with your **projects** today? 👋",
        color=EMBED_COLOR,
        timestamp=utcnow(),
    )
    return embed


def build_project_embed(project: asyncpg.Record, segment_rows: List[asyncpg.Record]) -> discord.Embed:
    status_map = {
        "in_development": "🛠️ In Development",
        "released": "🚀 Released",
        "won": "🏆 Won",
        "missed": "❌ Missed",
    }

    total_minutes = sum(int(row["minutes"]) for row in segment_rows)
    hours_text = "\n".join(
        f"**{row['segment_name']}** — {format_duration(int(row['minutes']))}"
        for row in segment_rows
    ) or "No hours added yet."

    release_date = "Not released yet"
    if project["released_at"]:
        release_dt = project["released_at"]
        if release_dt.tzinfo is None:
            release_dt = release_dt.replace(tzinfo=timezone.utc)
        release_date = discord.utils.format_dt(release_dt, style="F")

    embed = discord.Embed(
        title=f"{project['name']}",
        color=EMBED_COLOR,
        timestamp=utcnow(),
    )

    embed.add_field(name="📁 Field", value=project["field_name"], inline=True)
    embed.add_field(name="🧩 Format", value=project["format_name"], inline=True)
    embed.add_field(name="📌 Status", value=status_map.get(project["status"], project["status"]), inline=True)

    embed.add_field(name="⏱️ Hours by Segment", value=hours_text, inline=False)
    embed.add_field(name="🕒 Total Hours", value=format_duration(total_minutes), inline=True)
    embed.add_field(name="📅 Release Date", value=release_date, inline=True)

    return embed


def build_winrate_embed(title: str, won: int, missed: int) -> discord.Embed:
    total = won + missed
    winrate = (won / total * 100) if total > 0 else 0.0

    embed = discord.Embed(
        title=f"📈 {title}",
        description="Winrate is calculated only from projects marked as **Won** or **Missed**.",
        color=EMBED_COLOR,
        timestamp=utcnow(),
    )
    embed.add_field(name="🏆 Won", value=str(won), inline=True)
    embed.add_field(name="❌ Missed", value=str(missed), inline=True)
    embed.add_field(name="📦 Counted", value=str(total), inline=True)
    embed.add_field(name="📊 Winrate", value=f"**{winrate:.1f}%**", inline=False)
    return embed


def build_edit_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🛠️ Edit Panel",
        description=(
            "Manage fields, formats, segments, and project corrections.\n\n"
            "Choose an action from the menu below."
        ),
        color=EMBED_COLOR,
        timestamp=utcnow(),
    )
    embed.add_field(
        name="Actions",
        value=(
            "• Add/Delete Fields\n"
            "• Add/Delete Formats\n"
            "• Add/Delete Segments\n"
            "• Rename Projects\n"
            "• Move Projects\n"
            "• Change Status\n"
            "• Reopen Released Projects\n"
            "• Set Segment Hours"
        ),
        inline=False,
    )
    return embed


# -----------------------------
# Dynamic project action view
# -----------------------------
class ProjectActionView(discord.ui.View):
    def __init__(self, project_id: int, status: str):
        super().__init__(timeout=300)
        self.project_id = project_id

        if status == "in_development":
            self.add_item(ReleaseButton(project_id))
        elif status == "released":
            self.add_item(WonButton(project_id))
            self.add_item(MissedButton(project_id))


class ReleaseButton(discord.ui.Button):
    def __init__(self, project_id: int):
        super().__init__(label="Release", emoji="🚀", style=discord.ButtonStyle.primary)
        self.project_id = project_id

    async def callback(self, interaction: discord.Interaction):
        project = await fetch_project(self.project_id)
        if not project:
            return await interaction.response.send_message("Project not found.", ephemeral=True)
        if project["status"] != "in_development":
            return await interaction.response.send_message("This project can no longer be released.", ephemeral=True)

        await interaction.response.send_modal(ConfirmStatusModal(self.project_id, "released"))


class WonButton(discord.ui.Button):
    def __init__(self, project_id: int):
        super().__init__(label="Won", emoji="🏆", style=discord.ButtonStyle.success)
        self.project_id = project_id

    async def callback(self, interaction: discord.Interaction):
        project = await fetch_project(self.project_id)
        if not project:
            return await interaction.response.send_message("Project not found.", ephemeral=True)
        if project["status"] != "released":
            return await interaction.response.send_message("Only released projects can be marked as Won.", ephemeral=True)

        await interaction.response.send_modal(ConfirmStatusModal(self.project_id, "won"))


class MissedButton(discord.ui.Button):
    def __init__(self, project_id: int):
        super().__init__(label="Missed", emoji="❌", style=discord.ButtonStyle.danger)
        self.project_id = project_id

    async def callback(self, interaction: discord.Interaction):
        project = await fetch_project(self.project_id)
        if not project:
            return await interaction.response.send_message("Project not found.", ephemeral=True)
        if project["status"] != "released":
            return await interaction.response.send_message("Only released projects can be marked as Missed.", ephemeral=True)

        await interaction.response.send_modal(ConfirmStatusModal(self.project_id, "missed"))


class ConfirmStatusModal(discord.ui.Modal):
    def __init__(self, project_id: int, action: str):
        self.project_id = project_id
        self.action = action
        title_map = {
            "released": "Confirm Release",
            "won": "Confirm Won",
            "missed": "Confirm Missed",
        }
        super().__init__(title=title_map[action])

        self.confirm_input = discord.ui.TextInput(
            label="Type CONFIRM",
            placeholder="CONFIRM",
            required=True,
            max_length=20
        )
        self.add_item(self.confirm_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.confirm_input.value.strip() != "CONFIRM":
            return await interaction.response.send_message("Confirmation failed. Type exactly `CONFIRM`.", ephemeral=True)

        project = await fetch_project(self.project_id)
        if not project:
            return await interaction.response.send_message("Project not found.", ephemeral=True)

        if self.action == "released":
            if project["status"] != "in_development":
                return await interaction.response.send_message("This project is no longer in development.", ephemeral=True)
            await release_project(self.project_id)

        elif self.action == "won":
            if project["status"] != "released":
                return await interaction.response.send_message("Only released projects can be marked as Won.", ephemeral=True)
            await set_project_status(self.project_id, "won")

        elif self.action == "missed":
            if project["status"] != "released":
                return await interaction.response.send_message("Only released projects can be marked as Missed.", ephemeral=True)
            await set_project_status(self.project_id, "missed")

        updated = await fetch_project(self.project_id)
        rows = await fetch_project_segment_rows(self.project_id)
        view = ProjectActionView(self.project_id, updated["status"])
        await interaction.response.send_message(
            embed=build_project_embed(updated, rows),
            view=view,
            ephemeral=True,
        )


# -----------------------------
# /project flow
# -----------------------------
class ProjectHomeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Create New Project", emoji="📥", style=discord.ButtonStyle.secondary)
    async def create_project_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        fields = await fetch_fields()
        if not fields:
            return await interaction.response.send_message(
                "There are no fields yet. Ask someone with the edit role to add one.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Choose a field for the new project:",
            view=await FieldSelectView(mode="create").setup(),
            ephemeral=True,
        )

    @discord.ui.button(label="Track Project", emoji="📂", style=discord.ButtonStyle.secondary)
    async def track_project_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        fields = await fetch_fields()
        if not fields:
            return await interaction.response.send_message(
                "There are no fields yet.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Choose a field to track a project:",
            view=await FieldSelectView(mode="track").setup(),
            ephemeral=True,
        )


class FieldSelect(discord.ui.Select):
    def __init__(self, mode: str):
        self.mode = mode
        super().__init__(
            placeholder="Select a field...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        fields = await fetch_fields()
        self.options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]))
            for row in fields[:25]
        ]

    async def callback(self, interaction: discord.Interaction):
        field_id = int(self.values[0])

        formats = await fetch_formats(field_id)
        if not formats:
            return await interaction.response.send_message(
                "This field has no formats yet.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Choose a format:",
            view=await FormatSelectView(mode=self.mode, field_id=field_id).setup(),
            ephemeral=True,
        )


class FieldSelectView(discord.ui.View):
    def __init__(self, mode: str):
        super().__init__(timeout=300)
        self.select = FieldSelect(mode)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class FormatSelect(discord.ui.Select):
    def __init__(self, mode: str, field_id: int):
        self.mode = mode
        self.field_id = field_id
        super().__init__(
            placeholder="Select a format...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        formats = await fetch_formats(self.field_id)
        self.options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]))
            for row in formats[:25]
        ]

    async def callback(self, interaction: discord.Interaction):
        format_id = int(self.values[0])

        if self.mode == "create":
            await interaction.response.send_modal(CreateProjectModal(self.field_id, format_id))
            return

        projects = await fetch_projects(self.field_id, format_id)
        if not projects:
            return await interaction.response.send_message(
                "There are no projects in this format yet.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Choose a project:",
            view=await ProjectSelectView(self.field_id, format_id).setup(),
            ephemeral=True,
        )


class FormatSelectView(discord.ui.View):
    def __init__(self, mode: str, field_id: int):
        super().__init__(timeout=300)
        self.select = FormatSelect(mode, field_id)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class CreateProjectModal(discord.ui.Modal, title="Create New Project"):
    def __init__(self, field_id: int, format_id: int):
        super().__init__()
        self.field_id = field_id
        self.format_id = format_id

        self.project_name = discord.ui.TextInput(
            label="Project Name",
            placeholder="Example: Survive The Poppy Killer",
            required=True,
            max_length=100,
        )
        self.add_item(self.project_name)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.project_name.value.strip()
        if not name:
            return await interaction.response.send_message("Project name cannot be empty.", ephemeral=True)

        existing = await fetch_project_by_name(name)
        if existing:
            return await interaction.response.send_message(
                "A project with that name already exists.",
                ephemeral=True,
            )

        project_id = await create_project(self.field_id, self.format_id, name)
        if not project_id:
            return await interaction.response.send_message(
                "Failed to create project. That name may already exist.",
                ephemeral=True,
            )

        project = await fetch_project(project_id)
        rows = await fetch_project_segment_rows(project_id)

        await interaction.response.send_message(
            embed=build_project_embed(project, rows),
            view=ProjectActionView(project_id, project["status"]),
            ephemeral=True,
        )


class ProjectSelect(discord.ui.Select):
    def __init__(self, field_id: int, format_id: int):
        self.field_id = field_id
        self.format_id = format_id
        super().__init__(
            placeholder="Select a project...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        projects = await fetch_projects(self.field_id, self.format_id)
        self.options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]))
            for row in projects[:25]
        ]

    async def callback(self, interaction: discord.Interaction):
        project_id = int(self.values[0])
        project = await fetch_project(project_id)
        if not project:
            return await interaction.response.send_message("Project not found.", ephemeral=True)

        rows = await fetch_project_segment_rows(project_id)

        await interaction.response.send_message(
            embed=build_project_embed(project, rows),
            view=ProjectActionView(project_id, project["status"]),
            ephemeral=True,
        )


class ProjectSelectView(discord.ui.View):
    def __init__(self, field_id: int, format_id: int):
        super().__init__(timeout=300)
        self.select = ProjectSelect(field_id, format_id)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


# -----------------------------
# /add flow
# -----------------------------
class AddFieldSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.select = AddFieldSelect()
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class AddFieldSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Select a field...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        rows = await fetch_fields()
        self.options = [discord.SelectOption(label=r["name"][:100], value=str(r["id"])) for r in rows[:25]]

    async def callback(self, interaction: discord.Interaction):
        field_id = int(self.values[0])
        formats = await fetch_formats(field_id)
        if not formats:
            return await interaction.response.send_message("This field has no formats.", ephemeral=True)

        await interaction.response.send_message(
            "Choose a format:",
            view=await AddFormatSelectView(field_id).setup(),
            ephemeral=True,
        )


class AddFormatSelectView(discord.ui.View):
    def __init__(self, field_id: int):
        super().__init__(timeout=300)
        self.select = AddFormatSelect(field_id)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class AddFormatSelect(discord.ui.Select):
    def __init__(self, field_id: int):
        self.field_id = field_id
        super().__init__(
            placeholder="Select a format...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        rows = await fetch_formats(self.field_id)
        self.options = [discord.SelectOption(label=r["name"][:100], value=str(r["id"])) for r in rows[:25]]

    async def callback(self, interaction: discord.Interaction):
        format_id = int(self.values[0])
        projects = await fetch_projects(self.field_id, format_id)
        if not projects:
            return await interaction.response.send_message("There are no projects in this format.", ephemeral=True)

        await interaction.response.send_message(
            "Choose a project:",
            view=await AddProjectSelectView(self.field_id, format_id).setup(),
            ephemeral=True,
        )


class AddProjectSelectView(discord.ui.View):
    def __init__(self, field_id: int, format_id: int):
        super().__init__(timeout=300)
        self.select = AddProjectSelect(field_id, format_id)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class AddProjectSelect(discord.ui.Select):
    def __init__(self, field_id: int, format_id: int):
        self.field_id = field_id
        self.format_id = format_id
        super().__init__(
            placeholder="Select a project...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        rows = await fetch_projects(self.field_id, self.format_id)
        self.options = [discord.SelectOption(label=r["name"][:100], value=str(r["id"])) for r in rows[:25]]

    async def callback(self, interaction: discord.Interaction):
        project_id = int(self.values[0])
        project = await fetch_project(project_id)
        if not project:
            return await interaction.response.send_message("Project not found.", ephemeral=True)
        if project["status"] != "in_development":
            return await interaction.response.send_message(
                "You can only add hours to projects that are **In Development**.",
                ephemeral=True,
            )

        segments = await fetch_segments()
        if not segments:
            return await interaction.response.send_message("There are no segments configured.", ephemeral=True)

        await interaction.response.send_message(
            "Choose a segment:",
            view=await AddSegmentSelectView(project_id).setup(),
            ephemeral=True,
        )


class AddSegmentSelectView(discord.ui.View):
    def __init__(self, project_id: int):
        super().__init__(timeout=300)
        self.select = AddSegmentSelect(project_id)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class AddSegmentSelect(discord.ui.Select):
    def __init__(self, project_id: int):
        self.project_id = project_id
        super().__init__(
            placeholder="Select a segment...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        rows = await fetch_segments()
        self.options = [discord.SelectOption(label=r["name"][:100], value=str(r["id"])) for r in rows[:25]]

    async def callback(self, interaction: discord.Interaction):
        segment_id = int(self.values[0])
        await interaction.response.send_modal(AddTimeModal(self.project_id, segment_id))


class AddTimeModal(discord.ui.Modal, title="Add Time"):
    def __init__(self, project_id: int, segment_id: int):
        super().__init__()
        self.project_id = project_id
        self.segment_id = segment_id

        self.duration = discord.ui.TextInput(
            label="Time Spent",
            placeholder="Example: 2h 30m",
            required=True,
            max_length=20,
        )
        self.add_item(self.duration)

    async def on_submit(self, interaction: discord.Interaction):
        minutes = parse_duration(self.duration.value)
        if minutes is None:
            return await interaction.response.send_message(
                "Invalid time format. Use something like `2h 30m`, `2h`, or `30m`.",
                ephemeral=True,
            )

        project = await fetch_project(self.project_id)
        if not project:
            return await interaction.response.send_message("Project not found.", ephemeral=True)
        if project["status"] != "in_development":
            return await interaction.response.send_message("This project is no longer in development.", ephemeral=True)

        await add_project_minutes(self.project_id, self.segment_id, minutes)

        updated = await fetch_project(self.project_id)
        rows = await fetch_project_segment_rows(self.project_id)

        await interaction.response.send_message(
            content=f"Added **{format_duration(minutes)}** successfully.",
            embed=build_project_embed(updated, rows),
            view=ProjectActionView(self.project_id, updated["status"]),
            ephemeral=True,
        )


# -----------------------------
# /winrate flow
# -----------------------------
class WinrateMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(WinrateMenuSelect())


class WinrateMenuSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Overall", value="overall", emoji="📈"),
            discord.SelectOption(label="By Field", value="field", emoji="📁"),
            discord.SelectOption(label="By Format", value="format", emoji="🧩"),
        ]
        super().__init__(
            placeholder="Choose winrate type...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]

        if choice == "overall":
            row = await fetch_winrate_overall()
            won = int(row["won"] or 0)
            missed = int(row["missed"] or 0)
            return await interaction.response.send_message(
                embed=build_winrate_embed("Overall Winrate", won, missed),
                ephemeral=True,
            )

        fields = await fetch_fields()
        if not fields:
            return await interaction.response.send_message("There are no fields yet.", ephemeral=True)

        if choice == "field":
            return await interaction.response.send_message(
                "Choose a field:",
                view=await WinrateFieldSelectView(by_format=False).setup(),
                ephemeral=True,
            )

        return await interaction.response.send_message(
            "Choose a field first:",
            view=await WinrateFieldSelectView(by_format=True).setup(),
            ephemeral=True,
        )


class WinrateFieldSelectView(discord.ui.View):
    def __init__(self, by_format: bool):
        super().__init__(timeout=300)
        self.select = WinrateFieldSelect(by_format)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class WinrateFieldSelect(discord.ui.Select):
    def __init__(self, by_format: bool):
        self.by_format = by_format
        super().__init__(
            placeholder="Select a field...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        rows = await fetch_fields()
        self.options = [discord.SelectOption(label=r["name"][:100], value=str(r["id"])) for r in rows[:25]]

    async def callback(self, interaction: discord.Interaction):
        field_id = int(self.values[0])

        if not self.by_format:
            row = await fetch_winrate_by_field(field_id)
            won = int(row["won"] or 0)
            missed = int(row["missed"] or 0)

            fields = await fetch_fields()
            field_name = next((f["name"] for f in fields if f["id"] == field_id), "Field")

            return await interaction.response.send_message(
                embed=build_winrate_embed(f"Winrate — {field_name}", won, missed),
                ephemeral=True,
            )

        formats = await fetch_formats(field_id)
        if not formats:
            return await interaction.response.send_message("This field has no formats.", ephemeral=True)

        await interaction.response.send_message(
            "Choose a format:",
            view=await WinrateFormatSelectView(field_id).setup(),
            ephemeral=True,
        )


class WinrateFormatSelectView(discord.ui.View):
    def __init__(self, field_id: int):
        super().__init__(timeout=300)
        self.select = WinrateFormatSelect(field_id)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class WinrateFormatSelect(discord.ui.Select):
    def __init__(self, field_id: int):
        self.field_id = field_id
        super().__init__(
            placeholder="Select a format...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        rows = await fetch_formats(self.field_id)
        self.options = [discord.SelectOption(label=r["name"][:100], value=str(r["id"])) for r in rows[:25]]

    async def callback(self, interaction: discord.Interaction):
        format_id = int(self.values[0])
        row = await fetch_winrate_by_format(format_id)
        won = int(row["won"] or 0)
        missed = int(row["missed"] or 0)

        formats = await fetch_formats(self.field_id)
        format_name = next((f["name"] for f in formats if f["id"] == format_id), "Format")

        await interaction.response.send_message(
            embed=build_winrate_embed(f"Winrate — {format_name}", won, missed),
            ephemeral=True,
        )


# -----------------------------
# /edit flow
# -----------------------------
EDIT_ACTIONS = [
    ("Add Field", "add_field", "➕"),
    ("Delete Field", "delete_field", "🗑️"),
    ("Add Format", "add_format", "➕"),
    ("Delete Format", "delete_format", "🗑️"),
    ("Add Segment", "add_segment", "➕"),
    ("Delete Segment", "delete_segment", "🗑️"),
    ("Rename Project", "rename_project", "✏️"),
    ("Move Project", "move_project", "📦"),
    ("Change Project Status", "change_status", "🔁"),
    ("Reopen Project", "reopen_project", "↩️"),
    ("Set Segment Hours", "set_segment_hours", "⏱️"),
]


class EditMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(EditMenuSelect())


class EditMenuSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=label, value=value, emoji=emoji)
            for label, value, emoji in EDIT_ACTIONS
        ]
        super().__init__(
            placeholder="Choose an edit action...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if not has_maintenance_role(interaction):
            return await interaction.response.send_message("You do not have permission to use edit.", ephemeral=True)

        action = self.values[0]

        if action == "add_field":
            return await interaction.response.send_modal(SimpleNameModal("Add Field", "Field Name", "field"))
        if action == "add_segment":
            return await interaction.response.send_modal(SimpleNameModal("Add Segment", "Segment Name", "segment"))
        if action == "delete_field":
            return await interaction.response.send_message(
                "Choose a field to delete:",
                view=await EditFieldView("delete_field").setup(),
                ephemeral=True,
            )
        if action == "add_format":
            return await interaction.response.send_message(
                "Choose a field for the new format:",
                view=await EditFieldView("add_format").setup(),
                ephemeral=True,
            )
        if action == "delete_format":
            return await interaction.response.send_message(
                "Choose a field first:",
                view=await EditFieldView("delete_format").setup(),
                ephemeral=True,
            )
        if action == "delete_segment":
            return await interaction.response.send_message(
                "Choose a segment to delete:",
                view=await EditSegmentView("delete_segment").setup(),
                ephemeral=True,
            )
        if action in {"rename_project", "move_project", "change_status", "reopen_project", "set_segment_hours"}:
            return await interaction.response.send_message(
                "Choose a field first:",
                view=await EditFieldView(action).setup(),
                ephemeral=True,
            )


class SimpleNameModal(discord.ui.Modal):
    def __init__(self, title_text: str, label_text: str, mode: str):
        self.mode = mode
        super().__init__(title=title_text)
        self.name_input = discord.ui.TextInput(
            label=label_text,
            required=True,
            max_length=100,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        value = self.name_input.value.strip()
        if not value:
            return await interaction.response.send_message("Value cannot be empty.", ephemeral=True)

        if self.mode == "field":
            ok = await create_field(value)
            if not ok:
                return await interaction.response.send_message("That field already exists.", ephemeral=True)
            return await interaction.response.send_message(f"Added field **{value}**.", ephemeral=True)

        if self.mode == "segment":
            ok = await create_segment(value)
            if not ok:
                return await interaction.response.send_message("That segment already exists.", ephemeral=True)
            return await interaction.response.send_message(f"Added segment **{value}**.", ephemeral=True)

        await interaction.response.send_message("Unknown action.", ephemeral=True)


class AddFormatModal(discord.ui.Modal, title="Add Format"):
    def __init__(self, field_id: int):
        super().__init__()
        self.field_id = field_id
        self.name_input = discord.ui.TextInput(
            label="Format Name",
            required=True,
            max_length=100,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        value = self.name_input.value.strip()
        if not value:
            return await interaction.response.send_message("Format name cannot be empty.", ephemeral=True)

        ok = await create_format(self.field_id, value)
        if not ok:
            return await interaction.response.send_message("That format already exists.", ephemeral=True)

        await interaction.response.send_message(f"Added format **{value}**.", ephemeral=True)


class EditFieldView(discord.ui.View):
    def __init__(self, action: str):
        super().__init__(timeout=300)
        self.select = EditFieldSelect(action)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class EditFieldSelect(discord.ui.Select):
    def __init__(self, action: str):
        self.action = action
        super().__init__(
            placeholder="Select a field...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        rows = await fetch_fields()
        self.options = [discord.SelectOption(label=r["name"][:100], value=str(r["id"])) for r in rows[:25]]

    async def callback(self, interaction: discord.Interaction):
        field_id = int(self.values[0])

        if self.action == "delete_field":
            await delete_field(field_id)
            return await interaction.response.send_message("Field deleted. Everything inside it was also deleted.", ephemeral=True)

        if self.action == "add_format":
            return await interaction.response.send_modal(AddFormatModal(field_id))

        if self.action == "delete_format":
            formats = await fetch_formats(field_id)
            if not formats:
                return await interaction.response.send_message("This field has no formats.", ephemeral=True)
            return await interaction.response.send_message(
                "Choose a format:",
                view=await EditFormatView(field_id, self.action).setup(),
                ephemeral=True,
            )

        if self.action in {"rename_project", "move_project", "change_status", "reopen_project", "set_segment_hours"}:
            formats = await fetch_formats(field_id)
            if not formats:
                return await interaction.response.send_message("This field has no formats.", ephemeral=True)
            return await interaction.response.send_message(
                "Choose a format:",
                view=await EditFormatView(field_id, self.action).setup(),
                ephemeral=True,
            )


class EditFormatView(discord.ui.View):
    def __init__(self, field_id: int, action: str):
        super().__init__(timeout=300)
        self.select = EditFormatSelect(field_id, action)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class EditFormatSelect(discord.ui.Select):
    def __init__(self, field_id: int, action: str):
        self.field_id = field_id
        self.action = action
        super().__init__(
            placeholder="Select a format...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        rows = await fetch_formats(self.field_id)
        self.options = [discord.SelectOption(label=r["name"][:100], value=str(r["id"])) for r in rows[:25]]

    async def callback(self, interaction: discord.Interaction):
        format_id = int(self.values[0])

        if self.action == "delete_format":
            await delete_format(format_id)
            return await interaction.response.send_message("Format deleted. Everything inside it was also deleted.", ephemeral=True)

        projects = await fetch_projects(self.field_id, format_id)
        if not projects:
            return await interaction.response.send_message("There are no projects here.", ephemeral=True)

        await interaction.response.send_message(
            "Choose a project:",
            view=await EditProjectView(self.action, self.field_id, format_id).setup(),
            ephemeral=True,
        )


class EditProjectView(discord.ui.View):
    def __init__(self, action: str, field_id: int, format_id: int):
        super().__init__(timeout=300)
        self.select = EditProjectSelect(action, field_id, format_id)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class EditProjectSelect(discord.ui.Select):
    def __init__(self, action: str, field_id: int, format_id: int):
        self.action = action
        self.field_id = field_id
        self.format_id = format_id
        super().__init__(
            placeholder="Select a project...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        rows = await fetch_projects(self.field_id, self.format_id)
        self.options = [discord.SelectOption(label=r["name"][:100], value=str(r["id"])) for r in rows[:25]]

    async def callback(self, interaction: discord.Interaction):
        project_id = int(self.values[0])

        if self.action == "rename_project":
            return await interaction.response.send_modal(RenameProjectModal(project_id))

        if self.action == "move_project":
            return await interaction.response.send_message(
                "Choose the new field:",
                view=await MoveProjectFieldView(project_id).setup(),
                ephemeral=True,
            )

        if self.action == "change_status":
            return await interaction.response.send_message(
                "Choose the new status:",
                view=ChangeStatusView(project_id),
                ephemeral=True,
            )

        if self.action == "reopen_project":
            project = await fetch_project(project_id)
            if not project:
                return await interaction.response.send_message("Project not found.", ephemeral=True)
            if project["status"] != "released":
                return await interaction.response.send_message(
                    "Reopen is only for released projects.",
                    ephemeral=True,
                )
            await set_project_status(project_id, "in_development")
            return await interaction.response.send_message("Project reopened to **In Development**.", ephemeral=True)

        if self.action == "set_segment_hours":
            return await interaction.response.send_message(
                "Choose a segment:",
                view=await SetHoursSegmentView(project_id).setup(),
                ephemeral=True,
            )


class RenameProjectModal(discord.ui.Modal, title="Rename Project"):
    def __init__(self, project_id: int):
        super().__init__()
        self.project_id = project_id
        self.name_input = discord.ui.TextInput(
            label="New Project Name",
            required=True,
            max_length=100,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_name = self.name_input.value.strip()
        if not new_name:
            return await interaction.response.send_message("Name cannot be empty.", ephemeral=True)

        ok = await rename_project(self.project_id, new_name)
        if not ok:
            return await interaction.response.send_message("That project name already exists.", ephemeral=True)

        await interaction.response.send_message("Project renamed successfully.", ephemeral=True)


class MoveProjectFieldView(discord.ui.View):
    def __init__(self, project_id: int):
        super().__init__(timeout=300)
        self.select = MoveProjectFieldSelect(project_id)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class MoveProjectFieldSelect(discord.ui.Select):
    def __init__(self, project_id: int):
        self.project_id = project_id
        super().__init__(
            placeholder="Select the new field...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        rows = await fetch_fields()
        self.options = [discord.SelectOption(label=r["name"][:100], value=str(r["id"])) for r in rows[:25]]

    async def callback(self, interaction: discord.Interaction):
        field_id = int(self.values[0])
        formats = await fetch_formats(field_id)
        if not formats:
            return await interaction.response.send_message("That field has no formats.", ephemeral=True)

        await interaction.response.send_message(
            "Choose the new format:",
            view=await MoveProjectFormatView(self.project_id, field_id).setup(),
            ephemeral=True,
        )


class MoveProjectFormatView(discord.ui.View):
    def __init__(self, project_id: int, field_id: int):
        super().__init__(timeout=300)
        self.select = MoveProjectFormatSelect(project_id, field_id)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class MoveProjectFormatSelect(discord.ui.Select):
    def __init__(self, project_id: int, field_id: int):
        self.project_id = project_id
        self.field_id = field_id
        super().__init__(
            placeholder="Select the new format...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        rows = await fetch_formats(self.field_id)
        self.options = [discord.SelectOption(label=r["name"][:100], value=str(r["id"])) for r in rows[:25]]

    async def callback(self, interaction: discord.Interaction):
        format_id = int(self.values[0])
        await move_project(self.project_id, self.field_id, format_id)
        await interaction.response.send_message("Project moved successfully.", ephemeral=True)


class ChangeStatusView(discord.ui.View):
    def __init__(self, project_id: int):
        super().__init__(timeout=300)
        self.add_item(ChangeStatusSelect(project_id))


class ChangeStatusSelect(discord.ui.Select):
    def __init__(self, project_id: int):
        self.project_id = project_id
        options = [
            discord.SelectOption(label="In Development", value="in_development", emoji="🛠️"),
            discord.SelectOption(label="Released", value="released", emoji="🚀"),
            discord.SelectOption(label="Won", value="won", emoji="🏆"),
            discord.SelectOption(label="Missed", value="missed", emoji="🗑️"),
        ]
        super().__init__(
            placeholder="Select the new status...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        status = self.values[0]
        await set_project_status(self.project_id, status)
        await interaction.response.send_message(f"Project status changed to **{status}**.", ephemeral=True)


class EditSegmentView(discord.ui.View):
    def __init__(self, action: str):
        super().__init__(timeout=300)
        self.select = EditSegmentSelect(action)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class EditSegmentSelect(discord.ui.Select):
    def __init__(self, action: str):
        self.action = action
        super().__init__(
            placeholder="Select a segment...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        rows = await fetch_segments()
        self.options = [discord.SelectOption(label=r["name"][:100], value=str(r["id"])) for r in rows[:25]]

    async def callback(self, interaction: discord.Interaction):
        segment_id = int(self.values[0])

        if self.action == "delete_segment":
            await delete_segment(segment_id)
            return await interaction.response.send_message(
                "Segment deleted. Its saved hours were also deleted.",
                ephemeral=True,
            )

        await interaction.response.send_message("Unknown segment action.", ephemeral=True)


class SetHoursSegmentView(discord.ui.View):
    def __init__(self, project_id: int):
        super().__init__(timeout=300)
        self.select = SetHoursSegmentSelect(project_id)
        self.add_item(self.select)

    async def setup(self):
        await self.select.refresh_options()
        return self


class SetHoursSegmentSelect(discord.ui.Select):
    def __init__(self, project_id: int):
        self.project_id = project_id
        super().__init__(
            placeholder="Select a segment...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading...", value="0")]
        )

    async def refresh_options(self):
        rows = await fetch_segments()
        self.options = [discord.SelectOption(label=r["name"][:100], value=str(r["id"])) for r in rows[:25]]

    async def callback(self, interaction: discord.Interaction):
        segment_id = int(self.values[0])
        await interaction.response.send_modal(SetHoursModal(self.project_id, segment_id))


class SetHoursModal(discord.ui.Modal, title="Set Segment Hours"):
    def __init__(self, project_id: int, segment_id: int):
        super().__init__()
        self.project_id = project_id
        self.segment_id = segment_id
        self.duration = discord.ui.TextInput(
            label="New total time",
            placeholder="Example: 2h 30m",
            required=True,
            max_length=20,
        )
        self.add_item(self.duration)

    async def on_submit(self, interaction: discord.Interaction):
        minutes = parse_duration(self.duration.value)
        if minutes is None:
            return await interaction.response.send_message(
                "Invalid time format. Use `2h 30m`, `2h`, or `30m`.",
                ephemeral=True,
            )

        await set_project_minutes(self.project_id, self.segment_id, minutes)
        await interaction.response.send_message(
            f"Segment hours set to **{format_duration(minutes)}**.",
            ephemeral=True,
        )


# -----------------------------
# Commands
# -----------------------------
@tree.command(name="project", description="Open the project manager", guild=discord.Object(id=GUILD_ID))
async def project_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=build_home_embed(),
        view=ProjectHomeView(),
        ephemeral=True,
    )


@tree.command(name="add", description="Add time to a project segment", guild=discord.Object(id=GUILD_ID))
async def add_command(interaction: discord.Interaction):
    fields = await fetch_fields()
    if not fields:
        return await interaction.response.send_message(
            "There are no fields yet. Ask someone with the edit role to add one.",
            ephemeral=True,
        )

    await interaction.response.send_message(
        "Choose a field:",
        view=await AddFieldSelectView().setup(),
        ephemeral=True,
    )


@tree.command(name="winrate", description="Show project winrate", guild=discord.Object(id=GUILD_ID))
async def winrate_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📈 Winrate Panel",
        description="Choose how you want to calculate the winrate.",
        color=EMBED_COLOR,
        timestamp=utcnow(),
    )
    embed.add_field(name="Options", value="Overall, By Field, or By Format", inline=False)

    await interaction.response.send_message(
        embed=embed,
        view=WinrateMenuView(),
        ephemeral=True,
    )


@tree.command(name="edit", description="Open the edit panel", guild=discord.Object(id=GUILD_ID))
async def edit_command(interaction: discord.Interaction):
    if not has_maintenance_role(interaction):
        return await interaction.response.send_message(
            "You do not have permission to use this command.",
            ephemeral=True,
        )

    await interaction.response.send_message(
        embed=build_edit_embed(),
        view=EditMenuView(),
        ephemeral=True,
    )


# -----------------------------
# Events
# -----------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print("------")


@bot.event
async def setup_hook():
    await init_db()
    guild = discord.Object(id=GUILD_ID)
    synced = await tree.sync(guild=guild)
    print(f"Synced {len(synced)} command(s) to guild {GUILD_ID}")


# -----------------------------
# Run
# -----------------------------
bot.run(DISCORD_TOKEN)
