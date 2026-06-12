# Discord Productivity Tracker Bot

A Discord bot for tracking productivity time by Discord account.

## Commands

- `/startday` - start a work day and enter today's task list
- `/pause` - pause the active timer
- `/resume` - resume a paused timer by choosing a task
- `/addtask` - add a task to the active day
- `/removetask` - remove a task from future check-ins for the active day
- `/closeday` - close the active day and show task totals
- `/profile` - show your own saved stats
- `/weekly` - choose a Discord user and show that user's weekly stats as text, daily embeds, or an image
- `/daystats` - show one of your saved days
- `/addhours` - add tracked hours to a task on a finished day
- `/removehours` - remove tracked hours from a task on a finished day
- `/clearstats` - admin-only reset for one user's stats or everyone
- `/test` - run basic bot checks

## Google Docs Sync

When configured, the bot syncs each productivity day to the Google document assigned to that Discord user:

- dooly (`334414804477411339`) syncs to `GOOGLE_DOCS_DOCUMENT_ID_DOOLY`
- koszan (`695348265289383967`) syncs to `GOOGLE_DOCS_DOCUMENT_ID_KOSZAN`
- `/startday` creates a new tab in that user's configured Google Doc
- `/addtask` updates that day's tab with the new task
- `/closeday` asks for an end day recap, then updates the same tab with task times and the recap

The bot still uses Postgres as the source of truth. Google Docs is treated as a synced report.

## Behavior

- Identity is based only on the Discord account that runs the command.
- There is no separate profile creation or profile selector.
- Each Discord user can have one active day at a time.
- Slash-command responses are ephemeral, so only the person who ran the command can see them.
- `/clearstats` requires server administrator permissions.
- Check-ins are sent by DM every 30 minutes while the timer is running.
- If a check-in is ignored through the retry window, the bot keeps the current task running and asks again later.
- Clicking a task in a pending check-in resumes counting from the click time.
- `/pause` and `/resume` can be used manually during the day.
- `/removetask` hides a task from future check-ins without deleting tracked history for that task.
- `/addhours` edits a closed day by adding a manual time segment to the selected task.
- `/removehours` edits a closed day by subtracting time from the selected task's latest saved segments.
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
- `GOOGLE_DOCS_DOCUMENT_ID_DOOLY`
- `GOOGLE_DOCS_DOCUMENT_ID_KOSZAN`
- `GOOGLE_SERVICE_ACCOUNT_JSON`

For Google Docs:

- create a Google Cloud service account
- enable the Google Docs API
- copy the service account JSON into `GOOGLE_SERVICE_ACCOUNT_JSON`
- share both target Google Docs with the service account email as an editor

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
