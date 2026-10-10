---
name: discord
description: Send messages, notifications, statuses, and file attachments to a Discord webhook.
---

# Discord Webhook Client

Sends plain-text messages, status updates, and file attachments to a configured Discord webhook.

## Setup & Credentials

Place your webhook URL in `config.toml` (git-ignored):
```toml
[discord]
webhook_url = "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN"
```
Or export `DISCORD_WEBHOOK_URL`.

## Usage

### Send a Message (`scripts/discord.py`)
```bash
python scripts/discord.py "Build finished successfully."
```

### Send with a File Attachment
```bash
python scripts/discord.py "Latest report" --file /path/to/report.pdf
```

### Custom Username
```bash
python scripts/discord.py "Task completed" --username "Agent"
```
