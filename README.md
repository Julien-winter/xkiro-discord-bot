# xKiro Discord AI

A queued AI chat bot for your Discord server. It uses only models marked **free** in xKiro's live catalogue, supports coding, file review and roleplay, and gives every Discord user private conversation memory that expires after seven days of inactivity.

Built with [xKiro](https://xkiro.com/ref/D6J4DHK).

## Configured server

- Guild: `1480146210550579250`
- Chat channel: `1557471334228037855`

## Features

- Automatic free-model selection from xKiro's live catalogue: coding and reasoning requests prefer matching free-model capabilities; xKiro handles upstream provider routing and failover for each selected model. Paid-tier models are filtered out.
- Sequential queue with capacity limit, queue-position feedback, progress updates, failure feedback, and a single worker to prevent overlapping generations.
- Per-user, per-server private context, persisted in local SQLite and deleted after seven days of inactivity. Use `!reset` to clear, `!context` to DM a preview, or `!help` for tips.
- Handles text/code/Markdown/JSON/PDF uploads and HTTPS `file.io` links (download-only, capped size).
- Free-model-only is enforced from the live public xKiro model catalogue. At most three eligible free models are tried if unavailable; no paid-tier fallback.
- Moderator `/blacklist` and `/unblacklist` slash commands block/unblock a Discord user across servers sharing this bot's database. This is bot-wide only, not a Discord-wide or internet-wide ban.
- Safety defaults: image uploads are disabled. Never sexualize minors, child-like or uncertain-age people, or support exploitation. Adult text roleplay is opt-in, limited to clearly adult fictional characters and consent. This switch cannot enable image handling or disable the other safety rules.

## Discord setup

1. Create an app in the [Discord Developer Portal](https://discord.com/developers/applications) and add a bot.
2. Enable **Message Content Intent** under Bot → Privileged Gateway Intents.
3. Invite it to the configured guild with **View Channel**, **Send Messages**, and **Read Message History**. For the moderator slash commands, also grant **Use Application Commands**. It does not require Administrator.
4. The bot listens only to the configured guild and channel IDs.

## Run on Windows

Python 3.11+ is recommended.

```powershell
git clone https://github.com/Julien-winter/xkiro-discord-bot.git
cd xkiro-discord-bot
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python bot.py
```

Fill in `DISCORD_TOKEN` and `XKIRO_API_KEY` in `.env`. Server/channel IDs are prefilled. `.env` and the SQLite database are ignored by Git.

Set `ALLOW_ADULT_ROLEPLAY=true` only if the server permits adult text RP; adult text RP is then allowed only in a Discord channel marked NSFW. It requires clearly adult fictional characters and consent. The bot provides conversational coding advice and explains uploaded source files; it does not execute uploaded code. Images are rejected.

## Queue and limits

Only one queued request is processed at a time. Users receive updated queue positions and progress/status feedback. The queue rejects new requests when its configured capacity is reached. `DAILY_USER_LIMIT` is per user. All model candidates come from the current `free` tier catalogue; alternate free models are used only to recover when one is unavailable. xKiro handles upstream provider routing for each requested model.

## Privacy and moderation

- Prompts, extracted text from supported attachments, and eligible file.io downloads are sent to xKiro for inference.
- Memory is scoped by guild and Discord user ID. It expires after seven days without new activity; users can clear it with `!reset`.
- `!context` privately sends the user's recent memory to their Discord DMs.
- `.env` and `chats.sqlite3` contain secrets or user data; keep them private and never commit them.
- `/blacklist` adds a bot-wide blocklist entry in the local database. It cannot globally ban someone across Discord or other bot installations. Moderator actions are manual; the bot does not claim to report crimes or create an external global blacklist.
- Discord permissions and policies still apply; the bot isn't an automated legal or abuse-reporting service. Do not upload, download, or redistribute suspected illegal content.

## Test

```powershell
python -m py_compile bot.py
```

The workflow does not contain or upload private bot tests or any credentials.

## License

MIT; see `LICENSE`.
