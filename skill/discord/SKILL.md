---
name: discord
description: Send messages, notifications, statuses, and file attachments to a Discord webhook.
---

# Discord Webhook Client

Sends plain-text messages, status updates, and file attachments to a configured Discord webhook.

## Setup & Credentials

Store the webhook in `pass` under `aiconfig/discord/webhook-url`:

```bash
pass insert aiconfig/discord/webhook-url
```

## Usage

### Send a Message (`scripts/discord.py`)
```bash
python scripts/discord.py "Build finished successfully."
```

### Send with a File Attachment
For small files under 10 MB, including tiny configs, logs, and text files, attach them directly to Discord with `--file` instead of uploading them to a separate file host. Only use external file hosting when needed (larger files, an explicitly requested download link, or Discord rejecting the attachment). Review attachments for secrets and personal information; send credential-bearing configs only when explicitly requested by the user.

```bash
python scripts/discord.py "Latest report" --file /path/to/report.pdf
```

### Custom Username
```bash
python scripts/discord.py "Task completed" --username "Agent"
```
