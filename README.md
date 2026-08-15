# Deskmate

Deskmate is a Telegram bot that answers staff questions from your company's own
policy documents. Ask it something in your team's Telegram group, and it
answers from the documents you uploaded, with the source cited. If the answer
isn't in your documents, it says so instead of guessing.

This guide assumes you have never used a terminal, git, or Python. You won't
need any of them.

## What you'll need

- A Telegram account
- An Anthropic API key (a few minutes to get, see step 2 below)
- A credit card for Railway (hosting) and Anthropic (the AI model) — you pay
  both of these directly, not through us. Typical cost for a small team is a
  few dollars a month.

## Setup

### 1. Create your Telegram bot

1. Open Telegram and message **@BotFather**.
2. Send `/newbot` and follow the prompts to name your bot.
3. BotFather gives you a **bot token** — a long string like
   `123456789:AAExampleTokenDoNotUseThisOne`. Copy it, you'll need it in
   step 4.

### 2. Get an Anthropic API key

1. Go to [console.anthropic.com](https://console.anthropic.com) and sign up.
2. Create an API key and add billing details. Copy the key (starts with
   `sk-ant-`).

### 3. Find your Telegram user ID

1. Message **@userinfobot** on Telegram.
2. It replies with your numeric ID. Copy it — this makes you the
   administrator, the only person who can upload documents to your bot.

### 4. Deploy on Railway

1. Click: **[Deploy on Railway](https://railway.app/template/deskmate)**
   *(replace with your published template link)*
2. When asked for environment variables, fill in:

   | Variable | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | the key from step 2 |
   | `TELEGRAM_BOT_TOKEN` | the token from step 1 |
   | `COMPANY_NAME` | your company's name, e.g. `Acme Pty Ltd` |
   | `ADMIN_USER_ID` | your numeric ID from step 3 |

3. Click Deploy. Railway builds and starts the bot — this takes a couple of
   minutes.

### 5. Talk to your bot

1. Find your bot on Telegram (search the username you gave it in step 1) and
   send `/start`.
2. Add it to your team's group chat if you want it answering there too.
3. As the admin, send it a message and try `/docs` — it should list the demo
   documents that ship with Deskmate, so you have something to test with
   right away.

### 6. Upload your real documents

1. In a direct message with the bot (you must be the admin), send your
   policy documents as file attachments — `.md`, `.txt`, `.docx`, or `.pdf`,
   up to 10MB each.
2. The bot confirms each file with the number of words it extracted. If that
   number looks too low, the file may not have extracted properly — try
   re-saving it and uploading again.
3. Once your real documents are in, send `/reset_demo` to remove the demo
   documents that shipped with Deskmate.

Your staff can now ask questions in the group, and the bot answers from your
documents.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/start` | everyone | A short greeting |
| `/docs` | everyone | Lists the documents the bot can answer from |
| `/doctor` | admin only | A diagnostic report — paste this if you need support |
| `/reset_demo` | admin only | Removes the demo documents after you've uploaded your own |

## If something's not working

Message your bot `/doctor` (you must be the admin). It prints a report of
its configuration and document status. Paste that report when asking for
help — it's designed to show what's wrong at a glance.

## Billing

You pay Railway and Anthropic directly with your own accounts. Neither we
nor your documents ever touch our infrastructure — everything runs on the
Railway project you just created, using your own API key.
