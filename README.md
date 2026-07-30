# Telegram Welcome & Anti-Spam Math CAPTCHA Bot

A Telegram group management bot that mutes new members on join and requires them to solve a math CAPTCHA within 5 minutes to gain posting rights.

## Features

- **Auto-Mute**: New members are instantly muted upon joining
- **Math CAPTCHA**: Random addition problem with 4 multiple-choice buttons
- **GIF Welcome**: Animated GIF with cached `file_id` for fast subsequent sends
- **5-Minute Timeout**: Unverified users are automatically kicked
- **Error Handling**: Graceful handling of rate limits, missing permissions, and deleted messages

## Prerequisites

- Docker & Docker Compose installed on your server/VPS
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)

## Required Bot Permissions

The bot **must** be a group admin with these rights:

1. **Delete Messages** — for cleaning up verification messages and timeouts
2. **Restrict / Ban Members** — for muting on join and kicking on timeout

## Setup

### 1. Clone the project

```bash
git clone <repository-url>
cd bot
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and add your bot token:

```
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
```

### 3. Build and run

```bash
docker compose up -d --build
```

### 4. Add the bot to your group

1. Add the bot to your Telegram group
2. Promote it to admin
3. Grant it **Delete Messages** and **Restrict / Ban Members** permissions

## Usage

The bot works automatically:

1. A new user joins the group
2. They are immediately muted and shown a math CAPTCHA
3. If they solve it → unmuted, message deleted after 10 seconds
4. If they fail or ignore → kicked after 5 minutes

## Commands

| Action | Command |
|--------|---------|
| Start (background) | `docker compose up -d` |
| Stop | `docker compose down` |
| View logs | `docker compose logs -f` |
| Restart | `docker compose restart` |
| Rebuild | `docker compose up -d --build` |

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Bot doesn't respond | Check logs with `docker compose logs -f`. Verify `BOT_TOKEN` is correct. |
| Mute fails | Ensure the bot has **Restrict Members** admin permission in the group. |
| Kick fails | Ensure the bot has **Ban Members** admin permission in the group. |
| Message delete fails | Ensure the bot has **Delete Messages** admin permission in the group. |
| Rate limiting (429) | The bot handles this automatically — it sleeps for the required duration. |

## Tech Stack

- Python 3.11
- python-telegram-bot v21.9 (async)
- Docker & Docker Compose

## License

MIT
