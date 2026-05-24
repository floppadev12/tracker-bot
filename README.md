# Discord Productivity Tracker Bot

A Discord bot for tracking productivity time by Discord account.

## Commands

- `/startday` - start a work day and enter today's task list
- `/pause` - pause the active timer
- `/resume` - resume a paused timer by choosing a task
- `/closeday` - close the active day and show task totals
- `/profile` - show your own saved stats
- `/weekly` - choose a Discord user and show that user's weekly stats as text, daily embeds, or an image
- `/daystats` - show one of your saved days
- `/test` - run basic bot checks

## Behavior

- Identity is based only on the Discord account that runs the command.
- There is no separate profile creation or profile selector.
- Each Discord user can have one active day at a time.
- Slash-command responses are ephemeral, so only the person who ran the command can see them.
- Check-ins are sent by DM every 30 minutes while the timer is running.
- If a check-in is ignored through the retry window, the bot pauses the timer.
- Clicking a task in a pending check-in resumes counting from the click time.
- `/pause` and `/resume` can be used manually during the day.
- On startup, older duplicate active days for the same Discord user are closed automatically so only the newest remains active.

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

### 3. Environment Variables

Set these in Railway:

- `DISCORD_TOKEN`
- `DISCORD_GUILD_ID`
- `DATABASE_URL`

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

- Commands are synced to one guild only when `DISCORD_GUILD_ID` is set.
- `Self-care` is appended automatically to every day.
- Select menus support up to 25 options because that is Discord's limit.
