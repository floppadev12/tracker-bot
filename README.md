# Roblox Report Bot

A Discord bot for tracking Roblox game projects with:
- `/project`
- `/add`
- `/winrate`
- `/maintenance`
- PostgreSQL storage
- Railway deployment

## Features implemented
- Shared projects for one server
- Ephemeral replies only
- Create and track projects
- Manual time tracking per segment
- Release / Won / Missed confirmation modals
- Overall / field / format winrate
- Maintenance actions for fields, formats, segments, project rename, move, status change, reopen, and set segment hours

## Setup

### 1. Create your Discord bot
In the Discord Developer Portal:
- create a new application
- add a bot
- copy the bot token
- enable these scopes when inviting:
  - `bot`
  - `applications.commands`
- permissions:
  - Send Messages
  - Use Slash Commands
  - Embed Links
  - Read Message History

### 2. Railway
Create a new Railway project and add:
- one Python service for the bot
- one PostgreSQL database

### 3. Environment variables
Set these in Railway:
- `DISCORD_TOKEN`
- `DISCORD_GUILD_ID`
- `DATABASE_URL`
- `MAINTENANCE_ROLE_ID`

### 4. Deploy
Railway build command:
```bash
pip install -r requirements.txt
```

Railway start command:
```bash
python main.py
```

## Notes
- Commands are synced to one guild only using `DISCORD_GUILD_ID`
- Default segments are auto-created if missing: Build, Script, UI, Thumbnail
- Project and format names are globally unique
- Select menus support up to 25 options because that is Discord's limit
