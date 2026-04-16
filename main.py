import os
import re
import logging
from datetime import datetime, timezone
from typing import Optional, List, Tuple

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("roblox_report_bot")

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

GUILD_OBJECT = discord.Object(id=int(DISCORD_GUILD_ID))
MAINTENANCE_ROLE_ID_INT = int(MAINTENANCE_ROLE_ID)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
bot.db: asyncpg.Pool = None  # type: ignore

STATUS_LABELS = {
    "in_development": "In Development",
    "released": "Released",
    "won": "Won",
    "missed": "Missed",
}

DURATION_RE = re.compile(r"^(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?$", re.IGNORECASE)


def is_maintenance(member: discord.Member) -> bool:
    return any(role.id == MAINTENANCE_ROLE_ID_INT for role in member.roles)


def parse_duration(value: str) -> Optional[int]:
    match = DURATION_RE.match(value.strip())
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    total = hours * 60 + minutes
    return total if total > 0 else None


def format_duration(total_minutes: int) -> str:
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


async def init_db(pool: asyncpg.Pool):
    schema = """
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
    async with pool.acquire() as con:
        await con.execute(schema)
        for segment in ["Build", "Script", "UI", "Thumbnail"]:
            await con.execute(
                "INSERT INTO segments(name) VALUES($1) ON CONFLICT(name) DO NOTHING",
                segment,
            )


async def get_fields() -> List[asyncpg.Record]:
    return await bot.db.fetch("SELECT id, name FROM fields ORDER BY name")


async def get_formats(field_id: int) -> List[asyncpg.Record]:
    return await bot.db.fetch(
        "SELECT id, name FROM formats WHERE field_id = $1 ORDER BY name", field_id
    )


async def get_segments() -> List[asyncpg.Record]:
    return await bot.db.fetch("SELECT id, name FROM segments ORDER BY name")


async def get_projects(field_id: int, format_id: int, statuses: Optional[List[str]] = None) -> List[asyncpg.Record]:
    if statuses:
        return await bot.db.fetch(
            "SELECT id, name, status FROM projects WHERE field_id = $1 AND format_id = $2 AND status = ANY($3::text[]) ORDER BY name",
            field_id,
            format_id,
            statuses,
        )
    return await bot.db.fetch(
        "SELECT id, name, status FROM projects WHERE field_id = $1 AND format_id = $2 ORDER BY name",
        field_id,
        format_id,
    )


async def create_project(field_id: int, format_id: int, name: str) -> Optional[int]:
    row = await bot.db.fetchrow(
        """
        INSERT INTO projects(field_id, format_id, name, status)
        VALUES($1, $2, $3, 'in_development')
        ON CONFLICT(name) DO NOTHING
        RETURNING id
        """,
        field_id,
        format_id,
        name,
    )
    return row["id"] if row else None


async def get_project_details(project_id: int) -> Optional[asyncpg.Record]:
    return await bot.db.fetchrow(
        """
        SELECT p.id, p.name, p.status, p.released_at, f.name AS field_name, fm.name AS format_name,
               COALESCE(SUM(psh.minutes), 0) AS total_minutes
        FROM projects p
        JOIN fields f ON f.id = p.field_id
        JOIN formats fm ON fm.id = p.format_id
        LEFT JOIN project_segment_hours psh ON psh.project_id = p.id
        WHERE p.id = $1
        GROUP BY p.id, f.name, fm.name
        """,
        project_id,
    )


async def get_project_segment_hours(project_id: int) -> List[asyncpg.Record]:
    return await bot.db.fetch(
        """
        SELECT s.id, s.name, COALESCE(psh.minutes, 0) AS minutes
        FROM segments s
        LEFT JOIN project_segment_hours psh ON psh.segment_id = s.id AND psh.project_id = $1
        ORDER BY s.name
        """,
        project_id,
    )


async def add_project_minutes(project_id: int, segment_id: int, minutes: int):
    await bot.db.execute(
        """
        INSERT INTO project_segment_hours(project_id, segment_id, minutes)
        VALUES($1, $2, $3)
        ON CONFLICT(project_id, segment_id)
        DO UPDATE SET minutes = project_segment_hours.minutes + EXCLUDED.minutes
        """,
        project_id,
        segment_id,
        minutes,
    )
    await bot.db.execute("UPDATE projects SET updated_at = NOW() WHERE id = $1", project_id)


async def set_project_minutes(project_id: int, segment_id: int, minutes: int):
    await bot.db.execute(
        """
        INSERT INTO project_segment_hours(project_id, segment_id, minutes)
        VALUES($1, $2, $3)
        ON CONFLICT(project_id, segment_id)
        DO UPDATE SET minutes = EXCLUDED.minutes
        """,
        project_id,
        segment_id,
        minutes,
    )
    await bot.db.execute("UPDATE projects SET updated_at = NOW() WHERE id = $1", project_id)


async def set_project_status(project_id: int, status: str):
    if status == "released":
        await bot.db.execute(
            "UPDATE projects SET status = 'released', released_at = NOW(), updated_at = NOW() WHERE id = $1",
            project_id,
        )
    elif status == "in_development":
        await bot.db.execute(
            "UPDATE projects SET status = 'in_development', released_at = NULL, updated_at = NOW() WHERE id = $1",
            project_id,
        )
    else:
        await bot.db.execute(
            "UPDATE projects SET status = $2, updated_at = NOW() WHERE id = $1",
            project_id,
            status,
        )


async def rename_project(project_id: int, new_name: str) -> bool:
    try:
        await bot.db.execute(
            "UPDATE projects SET name = $2, updated_at = NOW() WHERE id = $1",
            project_id,
            new_name,
        )
        return True
    except asyncpg.UniqueViolationError:
        return False


async def move_project(project_id: int, field_id: int, format_id: int):
    await bot.db.execute(
        "UPDATE projects SET field_id = $2, format_id = $3, updated_at = NOW() WHERE id = $1",
        project_id,
        field_id,
        format_id,
    )


async def delete_field(field_id: int):
    await bot.db.execute("DELETE FROM fields WHERE id = $1", field_id)


async def delete_format(format_id: int):
    await bot.db.execute("DELETE FROM formats WHERE id = $1", format_id)


async def delete_segment(segment_id: int):
    await bot.db.execute("DELETE FROM segments WHERE id = $1", segment_id)


async def add_field(name: str) -> bool:
    try:
        await bot.db.execute("INSERT INTO fields(name) VALUES($1)", name)
        return True
    except asyncpg.UniqueViolationError:
        return False


async def add_format(field_id: int, name: str) -> bool:
    try:
        await bot.db.execute("INSERT INTO formats(field_id, name) VALUES($1, $2)", field_id, name)
        return True
    except asyncpg.UniqueViolationError:
        return False


async def add_segment(name: str) -> bool:
    try:
        await bot.db.execute("INSERT INTO segments(name) VALUES($1)", name)
        return True
    except asyncpg.UniqueViolationError:
        return False


async def get_winrate_overall() -> Tuple[int, int]:
    row = await bot.db.fetchrow(
        "SELECT COUNT(*) FILTER (WHERE status='won') AS won, COUNT(*) FILTER (WHERE status='missed') AS missed FROM projects WHERE status IN ('won','missed')"
    )
    return row["won"], row["missed"]


async def get_winrate_field(field_id: int) -> Tuple[int, int]:
    row = await bot.db.fetchrow(
        "SELECT COUNT(*) FILTER (WHERE status='won') AS won, COUNT(*) FILTER (WHERE status='missed') AS missed FROM projects WHERE field_id = $1 AND status IN ('won','missed')",
        field_id,
    )
    return row["won"], row["missed"]


async def get_winrate_format(format_id: int) -> Tuple[int, int]:
    row = await bot.db.fetchrow(
        "SELECT COUNT(*) FILTER (WHERE status='won') AS won, COUNT(*) FILTER (WHERE status='missed') AS missed FROM projects WHERE format_id = $1 AND status IN ('won','missed')",
        format_id,
    )
    return row["won"], row["missed"]


def winrate_embed(title: str, won: int, missed: int) -> discord.Embed:
    total = won + missed
    rate = (won / total * 100) if total else 0.0
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    embed.add_field(name="Won", value=str(won), inline=True)
    embed.add_field(name="Missed", value=str(missed), inline=True)
    embed.add_field(name="Total Counted", value=str(total), inline=True)
    embed.add_field(name="Winrate", value=f"{rate:.1f}%", inline=False)
    return embed


async def build_project_embed(project_id: int) -> discord.Embed:
    details = await get_project_details(project_id)
    if not details:
        return discord.Embed(title="Project not found", color=discord.Color.red())

    embed = discord.Embed(title=details["name"], color=discord.Color.blurple())
    embed.add_field(name="Field", value=details["field_name"], inline=True)
    embed.add_field(name="Format", value=details["format_name"], inline=True)
    embed.add_field(name="Status", value=STATUS_LABELS[details["status"]], inline=True)

    hours_rows = await get_project_segment_hours(project_id)
    hours_text = "\n".join(
        f"**{row['name']}**: {format_duration(row['minutes'])}" for row in hours_rows
    ) or "No segments yet."
    embed.add_field(name="Hours by Segment", value=hours_text, inline=False)
    embed.add_field(name="Total Hours", value=format_duration(details["total_minutes"]), inline=False)

    if details["released_at"]:
        ts = details["released_at"]
        embed.add_field(name="Release Date", value=ts.strftime("%Y-%m-%d %H:%M UTC"), inline=False)

    return embed


class ConfirmModal(discord.ui.Modal):
    confirm = discord.ui.TextInput(label="Type CONFIRM", required=True, max_length=20)

    def __init__(self, title: str, callback_fn):
        super().__init__(title=title)
        self.callback_fn = callback_fn

    async def on_submit(self, interaction: discord.Interaction):
        if str(self.confirm).strip() != "CONFIRM":
            return await interaction.response.send_message(
                "Confirmation failed. You must type `CONFIRM` exactly.", ephemeral=True
            )
        await self.callback_fn(interaction)


class TextInputModal(discord.ui.Modal):
    def __init__(self, title: str, label: str, placeholder: str, callback_fn):
        super().__init__(title=title)
        self.callback_fn = callback_fn
        self.input = discord.ui.TextInput(label=label, placeholder=placeholder, required=True, max_length=100)
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.callback_fn(interaction, str(self.input).strip())


class DurationModal(discord.ui.Modal):
    def __init__(self, title: str, callback_fn):
        super().__init__(title=title)
        self.callback_fn = callback_fn
        self.duration = discord.ui.TextInput(label="Time", placeholder="Example: 2h 30m", required=True, max_length=20)
        self.add_item(self.duration)

    async def on_submit(self, interaction: discord.Interaction):
        minutes = parse_duration(str(self.duration))
        if minutes is None:
            return await interaction.response.send_message(
                "Invalid time format. Use something like `2h 30m`, `2h`, or `30m`.",
                ephemeral=True,
            )
        await self.callback_fn(interaction, minutes)


class ProjectStatusView(discord.ui.View):
    def __init__(self, project_id: int):
        super().__init__(timeout=600)
        self.project_id = project_id

    async def refresh(self, interaction: discord.Interaction):
        details = await get_project_details(self.project_id)
        if not details:
            self.clear_items()
            return await interaction.response.edit_message(content="Project not found.", embed=None, view=None)

        self.clear_items()
        if details["status"] == "in_development":
            self.add_item(ReleaseButton(self.project_id))
        elif details["status"] == "released":
            self.add_item(WonButton(self.project_id))
            self.add_item(MissedButton(self.project_id))

        embed = await build_project_embed(self.project_id)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)


class ReleaseButton(discord.ui.Button):
    def __init__(self, project_id: int):
        super().__init__(label="Release", style=discord.ButtonStyle.primary)
        self.project_id = project_id

    async def callback(self, interaction: discord.Interaction):
        async def do_release(modal_interaction: discord.Interaction):
            details = await get_project_details(self.project_id)
            if not details or details["status"] != "in_development":
                return await modal_interaction.response.send_message("Project is not in development.", ephemeral=True)
            await set_project_status(self.project_id, "released")
            view = ProjectStatusView(self.project_id)
            await view.refresh(modal_interaction)

        await interaction.response.send_modal(ConfirmModal("Confirm Release", do_release))


class WonButton(discord.ui.Button):
    def __init__(self, project_id: int):
        super().__init__(label="Won", style=discord.ButtonStyle.success)
        self.project_id = project_id

    async def callback(self, interaction: discord.Interaction):
        async def do_win(modal_interaction: discord.Interaction):
            details = await get_project_details(self.project_id)
            if not details or details["status"] != "released":
                return await modal_interaction.response.send_message("Project must be released first.", ephemeral=True)
            await set_project_status(self.project_id, "won")
            view = ProjectStatusView(self.project_id)
            await view.refresh(modal_interaction)

        await interaction.response.send_modal(ConfirmModal("Confirm Won", do_win))


class MissedButton(discord.ui.Button):
    def __init__(self, project_id: int):
        super().__init__(label="Missed", style=discord.ButtonStyle.danger)
        self.project_id = project_id

    async def callback(self, interaction: discord.Interaction):
        async def do_missed(modal_interaction: discord.Interaction):
            details = await get_project_details(self.project_id)
            if not details or details["status"] != "released":
                return await modal_interaction.response.send_message("Project must be released first.", ephemeral=True)
            await set_project_status(self.project_id, "missed")
            view = ProjectStatusView(self.project_id)
            await view.refresh(modal_interaction)

        await interaction.response.send_modal(ConfirmModal("Confirm Missed", do_missed))


class SimpleSelect(discord.ui.Select):
    def __init__(self, placeholder: str, options: List[discord.SelectOption], callback_fn):
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1)
        self.callback_fn = callback_fn

    async def callback(self, interaction: discord.Interaction):
        await self.callback_fn(interaction, self.values[0])


class SimpleView(discord.ui.View):
    def __init__(self, *items, timeout: float = 600):
        super().__init__(timeout=timeout)
        for item in items:
            self.add_item(item)


async def send_no_fields(interaction: discord.Interaction):
    if interaction.response.is_done():
        await interaction.followup.send("No fields exist yet. Use `/maintenance` first.", ephemeral=True)
    else:
        await interaction.response.send_message("No fields exist yet. Use `/maintenance` first.", ephemeral=True)


async def send_no_formats(interaction: discord.Interaction):
    if interaction.response.is_done():
        await interaction.followup.send("No formats exist for that field yet.", ephemeral=True)
    else:
        await interaction.response.send_message("No formats exist for that field yet.", ephemeral=True)


async def send_no_projects(interaction: discord.Interaction):
    if interaction.response.is_done():
        await interaction.followup.send("No projects found.", ephemeral=True)
    else:
        await interaction.response.send_message("No projects found.", ephemeral=True)


async def show_create_project_step(interaction: discord.Interaction):
    fields = await get_fields()
    if not fields:
        return await send_no_fields(interaction)

    async def on_field(inter: discord.Interaction, field_id_str: str):
        field_id = int(field_id_str)
        formats = await get_formats(field_id)
        if not formats:
            return await send_no_formats(inter)

        async def on_format(inter2: discord.Interaction, format_id_str: str):
            format_id = int(format_id_str)

            async def on_name(modal_inter: discord.Interaction, name: str):
                if not name:
                    return await modal_inter.response.send_message("Project name cannot be empty.", ephemeral=True)
                project_id = await create_project(field_id, format_id, name)
                if not project_id:
                    return await modal_inter.response.send_message("A project with that name already exists.", ephemeral=True)
                view = ProjectStatusView(project_id)
                if (await get_project_details(project_id))["status"] == "in_development":
                    view.add_item(ReleaseButton(project_id))
                embed = await build_project_embed(project_id)
                await modal_inter.response.send_message(embed=embed, view=view, ephemeral=True)

            await inter2.response.send_modal(TextInputModal("Create Project", "Project Name", "Enter the project name", on_name))

        options = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in formats[:25]]
        await inter.response.edit_message(
            content="Select a format:",
            view=SimpleView(SimpleSelect("Choose a format", options, on_format)),
        )

    options = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in fields[:25]]
    await interaction.response.edit_message(
        content="Select a field:",
        view=SimpleView(SimpleSelect("Choose a field", options, on_field)),
    )


async def show_track_project_step(interaction: discord.Interaction):
    fields = await get_fields()
    if not fields:
        return await send_no_fields(interaction)

    async def on_field(inter: discord.Interaction, field_id_str: str):
        field_id = int(field_id_str)
        formats = await get_formats(field_id)
        if not formats:
            return await send_no_formats(inter)

        async def on_format(inter2: discord.Interaction, format_id_str: str):
            format_id = int(format_id_str)
            projects = await get_projects(field_id, format_id)
            if not projects:
                return await send_no_projects(inter2)

            async def on_project(inter3: discord.Interaction, project_id_str: str):
                project_id = int(project_id_str)
                view = ProjectStatusView(project_id)
                await view.refresh(inter3)

            options = [discord.SelectOption(label=p["name"], value=str(p["id"])) for p in projects[:25]]
            await inter2.response.edit_message(
                content="Select a project:",
                view=SimpleView(SimpleSelect("Choose a project", options, on_project)),
            )

        options = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in formats[:25]]
        await inter.response.edit_message(
            content="Select a format:",
            view=SimpleView(SimpleSelect("Choose a format", options, on_format)),
        )

    options = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in fields[:25]]
    await interaction.response.edit_message(
        content="Select a field:",
        view=SimpleView(SimpleSelect("Choose a field", options, on_field)),
    )


async def show_add_time_flow(interaction: discord.Interaction):
    fields = await get_fields()
    if not fields:
        return await interaction.response.send_message("No fields exist yet.", ephemeral=True)

    async def on_field(inter: discord.Interaction, field_id_str: str):
        field_id = int(field_id_str)
        formats = await get_formats(field_id)
        if not formats:
            return await send_no_formats(inter)

        async def on_format(inter2: discord.Interaction, format_id_str: str):
            format_id = int(format_id_str)
            projects = await get_projects(field_id, format_id, ["in_development"])
            if not projects:
                return await inter2.response.edit_message(content="No in-development projects found.", view=None)

            async def on_project(inter3: discord.Interaction, project_id_str: str):
                project_id = int(project_id_str)
                segments = await get_segments()
                if not segments:
                    return await inter3.response.edit_message(content="No segments exist.", view=None)

                async def on_segment(inter4: discord.Interaction, segment_id_str: str):
                    segment_id = int(segment_id_str)

                    async def on_duration(modal_inter: discord.Interaction, minutes: int):
                        details = await get_project_details(project_id)
                        if not details or details["status"] != "in_development":
                            return await modal_inter.response.send_message(
                                "You can only add hours to projects in development.", ephemeral=True
                            )
                        await add_project_minutes(project_id, segment_id, minutes)
                        embed = await build_project_embed(project_id)
                        view = ProjectStatusView(project_id)
                        await modal_inter.response.send_message(
                            content="Time added successfully.", embed=embed, view=view, ephemeral=True
                        )

                    await inter4.response.send_modal(DurationModal("Add Time", on_duration))

                options = [discord.SelectOption(label=s["name"], value=str(s["id"])) for s in segments[:25]]
                await inter3.response.edit_message(
                    content="Select a segment:",
                    view=SimpleView(SimpleSelect("Choose a segment", options, on_segment)),
                )

            options = [discord.SelectOption(label=p["name"], value=str(p["id"])) for p in projects[:25]]
            await inter2.response.edit_message(
                content="Select a project:",
                view=SimpleView(SimpleSelect("Choose a project", options, on_project)),
            )

        options = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in formats[:25]]
        await inter.response.edit_message(
            content="Select a format:",
            view=SimpleView(SimpleSelect("Choose a format", options, on_format)),
        )

    options = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in fields[:25]]
    await interaction.response.send_message(
        "Select a field:",
        ephemeral=True,
        view=SimpleView(SimpleSelect("Choose a field", options, on_field)),
    )


async def show_winrate_flow(interaction: discord.Interaction):
    async def on_mode(inter: discord.Interaction, mode: str):
        if mode == "overall":
            won, missed = await get_winrate_overall()
            return await inter.response.edit_message(content=None, embed=winrate_embed("Overall Winrate", won, missed), view=None)

        fields = await get_fields()
        if not fields:
            return await inter.response.edit_message(content="No fields exist yet.", view=None)

        async def on_field(inter2: discord.Interaction, field_id_str: str):
            field_id = int(field_id_str)
            field_name = next((f["name"] for f in fields if f["id"] == field_id), "Field")
            if mode == "field":
                won, missed = await get_winrate_field(field_id)
                return await inter2.response.edit_message(content=None, embed=winrate_embed(f"Winrate: {field_name}", won, missed), view=None)

            formats = await get_formats(field_id)
            if not formats:
                return await inter2.response.edit_message(content="No formats exist for that field.", view=None)

            async def on_format(inter3: discord.Interaction, format_id_str: str):
                format_id = int(format_id_str)
                format_name = next((f["name"] for f in formats if f["id"] == format_id), "Format")
                won, missed = await get_winrate_format(format_id)
                return await inter3.response.edit_message(content=None, embed=winrate_embed(f"Winrate: {field_name} / {format_name}", won, missed), view=None)

            options = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in formats[:25]]
            await inter2.response.edit_message(
                content="Select a format:",
                view=SimpleView(SimpleSelect("Choose a format", options, on_format)),
            )

        options = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in fields[:25]]
        await inter.response.edit_message(
            content="Select a field:",
            view=SimpleView(SimpleSelect("Choose a field", options, on_field)),
        )

    options = [
        discord.SelectOption(label="Overall", value="overall"),
        discord.SelectOption(label="By Field", value="field"),
        discord.SelectOption(label="By Format", value="format"),
    ]
    await interaction.response.send_message(
        "What winrate do you want to track?",
        ephemeral=True,
        view=SimpleView(SimpleSelect("Choose a winrate view", options, on_mode)),
    )


async def maintenance_action_select(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_maintenance(interaction.user):
        return await interaction.response.send_message("You do not have permission to use `/maintenance`.", ephemeral=True)

    actions = [
        ("add_field", "Add field"),
        ("delete_field", "Delete field"),
        ("add_format", "Add format"),
        ("delete_format", "Delete format"),
        ("add_segment", "Add segment"),
        ("delete_segment", "Delete segment"),
        ("rename_project", "Rename project"),
        ("move_project", "Move project"),
        ("change_status", "Change project status"),
        ("reopen_project", "Reopen project"),
        ("set_segment_hours", "Set segment hours"),
    ]

    async def on_action(inter: discord.Interaction, value: str):
        if value == "add_field":
            async def submit(modal_inter: discord.Interaction, name: str):
                ok = await add_field(name)
                await modal_inter.response.send_message(
                    f"Field {'created' if ok else 'already exists'}: **{name}**", ephemeral=True
                )
            return await inter.response.send_modal(TextInputModal("Add Field", "Field Name", "Example: Survive", submit))

        if value == "delete_field":
            fields = await get_fields()
            if not fields:
                return await inter.response.edit_message(content="No fields to delete.", view=None)

            async def on_pick(inter2: discord.Interaction, field_id_str: str):
                field = next((f for f in fields if f["id"] == int(field_id_str)), None)
                async def confirmed(modal_inter: discord.Interaction):
                    await delete_field(int(field_id_str))
                    await modal_inter.response.send_message(f"Deleted field **{field['name']}** and everything inside it.", ephemeral=True)
                await inter2.response.send_modal(ConfirmModal("Confirm Delete Field", confirmed))

            options = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in fields[:25]]
            return await inter.response.edit_message(content="Select a field to delete:", view=SimpleView(SimpleSelect("Choose a field", options, on_pick)))

        if value == "add_format":
            fields = await get_fields()
            if not fields:
                return await inter.response.edit_message(content="No fields exist yet.", view=None)

            async def on_field(inter2: discord.Interaction, field_id_str: str):
                async def submit(modal_inter: discord.Interaction, name: str):
                    ok = await add_format(int(field_id_str), name)
                    await modal_inter.response.send_message(
                        f"Format {'created' if ok else 'already exists'}: **{name}**", ephemeral=True
                    )
                await inter2.response.send_modal(TextInputModal("Add Format", "Format Name", "Example: Survive The Killer", submit))

            options = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in fields[:25]]
            return await inter.response.edit_message(content="Select a field:", view=SimpleView(SimpleSelect("Choose a field", options, on_field)))

        if value == "delete_format":
            fields = await get_fields()
            if not fields:
                return await inter.response.edit_message(content="No fields exist yet.", view=None)

            async def on_field(inter2: discord.Interaction, field_id_str: str):
                formats = await get_formats(int(field_id_str))
                if not formats:
                    return await inter2.response.edit_message(content="No formats in that field.", view=None)

                async def on_format(inter3: discord.Interaction, format_id_str: str):
                    format_row = next((f for f in formats if f["id"] == int(format_id_str)), None)
                    async def confirmed(modal_inter: discord.Interaction):
                        await delete_format(int(format_id_str))
                        await modal_inter.response.send_message(f"Deleted format **{format_row['name']}** and everything inside it.", ephemeral=True)
                    await inter3.response.send_modal(ConfirmModal("Confirm Delete Format", confirmed))

                options = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in formats[:25]]
                await inter2.response.edit_message(content="Select a format to delete:", view=SimpleView(SimpleSelect("Choose a format", options, on_format)))

            options = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in fields[:25]]
            return await inter.response.edit_message(content="Select a field:", view=SimpleView(SimpleSelect("Choose a field", options, on_field)))

        if value == "add_segment":
            async def submit(modal_inter: discord.Interaction, name: str):
                ok = await add_segment(name)
                await modal_inter.response.send_message(
                    f"Segment {'created' if ok else 'already exists'}: **{name}**", ephemeral=True
                )
            return await inter.response.send_modal(TextInputModal("Add Segment", "Segment Name", "Example: Animation", submit))

        if value == "delete_segment":
            segments = await get_segments()
            if not segments:
                return await inter.response.edit_message(content="No segments exist.", view=None)

            async def on_pick(inter2: discord.Interaction, segment_id_str: str):
                segment = next((s for s in segments if s["id"] == int(segment_id_str)), None)
                async def confirmed(modal_inter: discord.Interaction):
                    await delete_segment(int(segment_id_str))
                    await modal_inter.response.send_message(f"Deleted segment **{segment['name']}** and all stored hours in it.", ephemeral=True)
                await inter2.response.send_modal(ConfirmModal("Confirm Delete Segment", confirmed))

            options = [discord.SelectOption(label=s["name"], value=str(s["id"])) for s in segments[:25]]
            return await inter.response.edit_message(content="Select a segment to delete:", view=SimpleView(SimpleSelect("Choose a segment", options, on_pick)))

        if value in {"rename_project", "move_project", "change_status", "reopen_project", "set_segment_hours"}:
            fields = await get_fields()
            if not fields:
                return await inter.response.edit_message(content="No fields exist yet.", view=None)

            async def on_field(inter2: discord.Interaction, field_id_str: str):
                field_id = int(field_id_str)
                formats = await get_formats(field_id)
                if not formats:
                    return await inter2.response.edit_message(content="No formats in that field.", view=None)

                async def on_format(inter3: discord.Interaction, format_id_str: str):
                    format_id = int(format_id_str)
                    statuses = ["released"] if value == "reopen_project" else None
                    projects = await get_projects(field_id, format_id, statuses)
                    if not projects:
                        return await inter3.response.edit_message(content="No matching projects found.", view=None)

                    async def on_project(inter4: discord.Interaction, project_id_str: str):
                        project_id = int(project_id_str)

                        if value == "rename_project":
                            async def submit(modal_inter: discord.Interaction, new_name: str):
                                ok = await rename_project(project_id, new_name)
                                await modal_inter.response.send_message(
                                    "Project renamed." if ok else "That project name already exists.", ephemeral=True
                                )
                            return await inter4.response.send_modal(TextInputModal("Rename Project", "New Project Name", "Enter the new project name", submit))

                        if value == "move_project":
                            dest_fields = await get_fields()
                            async def on_dest_field(inter5: discord.Interaction, dest_field_id_str: str):
                                dest_formats = await get_formats(int(dest_field_id_str))
                                if not dest_formats:
                                    return await inter5.response.edit_message(content="No formats in that destination field.", view=None)
                                async def on_dest_format(inter6: discord.Interaction, dest_format_id_str: str):
                                    await move_project(project_id, int(dest_field_id_str), int(dest_format_id_str))
                                    await inter6.response.edit_message(content="Project moved successfully.", view=None)
                                options2 = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in dest_formats[:25]]
                                await inter5.response.edit_message(content="Select destination format:", view=SimpleView(SimpleSelect("Choose a format", options2, on_dest_format)))
                            options1 = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in dest_fields[:25]]
                            return await inter4.response.edit_message(content="Select destination field:", view=SimpleView(SimpleSelect("Choose a field", options1, on_dest_field)))

                        if value == "change_status":
                            async def on_status(inter5: discord.Interaction, status: str):
                                await set_project_status(project_id, status)
                                await inter5.response.edit_message(content=f"Project status set to **{STATUS_LABELS[status]}**.", view=None)
                            status_options = [
                                discord.SelectOption(label="In Development", value="in_development"),
                                discord.SelectOption(label="Released", value="released"),
                                discord.SelectOption(label="Won", value="won"),
                                discord.SelectOption(label="Missed", value="missed"),
                            ]
                            return await inter4.response.edit_message(content="Select the new status:", view=SimpleView(SimpleSelect("Choose a status", status_options, on_status)))

                        if value == "reopen_project":
                            await set_project_status(project_id, "in_development")
                            return await inter4.response.edit_message(content="Project reopened and moved back to **In Development**.", view=None)

                        if value == "set_segment_hours":
                            segments = await get_segments()
                            if not segments:
                                return await inter4.response.edit_message(content="No segments exist.", view=None)
                            async def on_segment(inter5: discord.Interaction, segment_id_str: str):
                                async def submit(modal_inter: discord.Interaction, minutes: int):
                                    await set_project_minutes(project_id, int(segment_id_str), minutes)
                                    await modal_inter.response.send_message("Segment hours updated.", ephemeral=True)
                                await inter5.response.send_modal(DurationModal("Set Segment Hours", submit))
                            options3 = [discord.SelectOption(label=s["name"], value=str(s["id"])) for s in segments[:25]]
                            return await inter4.response.edit_message(content="Select a segment:", view=SimpleView(SimpleSelect("Choose a segment", options3, on_segment)))

                    options = [discord.SelectOption(label=p["name"], value=str(p["id"])) for p in projects[:25]]
                    await inter3.response.edit_message(content="Select a project:", view=SimpleView(SimpleSelect("Choose a project", options, on_project)))

                options = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in formats[:25]]
                await inter2.response.edit_message(content="Select a format:", view=SimpleView(SimpleSelect("Choose a format", options, on_format)))

            options = [discord.SelectOption(label=f["name"], value=str(f["id"])) for f in fields[:25]]
            return await inter.response.edit_message(content="Select a field:", view=SimpleView(SimpleSelect("Choose a field", options, on_field)))

    options = [discord.SelectOption(label=label, value=value) for value, label in actions]
    await interaction.response.send_message(
        "Select a maintenance action:",
        ephemeral=True,
        view=SimpleView(SimpleSelect("Choose an action", options, on_action), timeout=900),
    )


class ProjectRootMenu(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Create new project", value="create"),
            discord.SelectOption(label="Track project", value="track"),
        ]
        super().__init__(placeholder="What do you want to do?", options=options)

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        if value == "create":
            await show_create_project_step(interaction)
        elif value == "track":
            await show_track_project_step(interaction)


class ProjectRootView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)
        self.add_item(ProjectRootMenu())


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")


@bot.event
async def setup_hook():
    bot.db = await asyncpg.create_pool(DATABASE_URL)
    await init_db(bot.db)
    bot.tree.copy_global_to(guild=GUILD_OBJECT)
    synced = await bot.tree.sync(guild=GUILD_OBJECT)
    log.info("Synced %s guild command(s)", len(synced))


@bot.tree.command(name="project", description="Create or track a project", guild=GUILD_OBJECT)
async def project_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        "What do you want to do?",
        ephemeral=True,
        view=ProjectRootView(),
    )


@bot.tree.command(name="add", description="Add time to a project segment", guild=GUILD_OBJECT)
async def add_command(interaction: discord.Interaction):
    await show_add_time_flow(interaction)


@bot.tree.command(name="winrate", description="View project winrate", guild=GUILD_OBJECT)
async def winrate_command(interaction: discord.Interaction):
    await show_winrate_flow(interaction)


@bot.tree.command(name="maintenance", description="Manage fields, formats, segments, and projects", guild=GUILD_OBJECT)
async def maintenance_command(interaction: discord.Interaction):
    await maintenance_action_select(interaction)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
