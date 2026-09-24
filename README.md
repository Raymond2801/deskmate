# Deskmate

A Telegram bot that answers staff questions from a company's own policy
documents, citing the source, and saying so when the answer isn't in the
documents rather than guessing. Single-tenant: one deployment serves one
company, using that company's own Anthropic and Telegram credentials.

## Requirements

- Python 3.11+
- A Telegram bot token ([@BotFather](https://t.me/BotFather))
- An Anthropic API key ([console.anthropic.com](https://console.anthropic.com))
- A host that can run a long-lived process (deploys as-is on Railway via
  `railway.json`/`Procfile`)

## Configuration

All configuration is environment variables, validated at startup in
`config.py`.

| Variable | Required | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | From console.anthropic.com |
| `TELEGRAM_BOT_TOKEN` | yes | From @BotFather |
| `COMPANY_NAME` | yes | Inserted into the bot's answers |
| `ADMIN_USER_ID` | yes | Numeric Telegram user ID; only this account can upload documents |
| `ALLOWED_CHAT_IDS` | no | Comma-separated chat IDs to restrict responses to; blank = any chat |
| `MODEL` | no | Overrides the default Claude model |
| `LOG_LEVEL` | no | `DEBUG`/`INFO`/`WARNING`/`ERROR`, default `INFO` |
| `HEALTHCHECK_PING_URL` | no | healthchecks.io (or compatible) ping URL for uptime monitoring |

See `.env.example`.

## Running locally

```
pip install -r requirements.txt
cp .env.example .env   # fill in values
python bot.py
```

## Storage

Uploaded documents and the corpus index persist under `data/`. Mount a
persistent volume at this path in production — without one, documents are
lost on redeploy. Accepts `.md`, `.txt`, `.docx`, `.pdf`, up to 10MB each,
50 files max.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/start` | everyone | A short greeting |
| `/docs` | everyone | Lists the documents the bot can answer from |
| `/doctor` | admin only | Diagnostic report: config status, document count, last error |
| `/reset_demo` | admin only | Removes the demo documents after real ones are uploaded |
