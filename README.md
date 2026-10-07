# xKiro Discord Auto-Chat

A hands-free Discord chatbot powered by xKiro. Configure one text channel by **channel ID**; people just talk normally—no slash commands, bot mentions, or prefix required. Choose a model; xKiro automatically routes requests across providers and handles route failover.

Built with [xKiro](https://xkiro.com), one API for leading AI models.

## How the chat works

- `DISCORD_CHANNEL_ID` routes messages from exactly one channel to this bot.
- By default, the channel is one shared group conversation: everyone sees the same replies and the bot can use recent messages from everyone in that channel as context. Set `CHAT_CONTEXT_MODE=per_user` for separate per-user context instead.
- Context is stored in a local SQLite file so it survives bot restarts; it expires after 24 hours by default.
- `!reset` clears this channel's saved context (or only the caller's context in `per_user` mode); deleting `chats.sqlite3` clears all history and usage counters.
- Other channels are ignored. Bot messages are ignored, preventing loops.
- A daily per-user request cap protects the bot owner's xKiro balance.
- The `XKIRO_MODEL` value is the model you want. xKiro handles automatic provider-route selection and failover for that requested model behind its gateway. Optionally configure a different model ID as a fallback if that model itself is unavailable; that's a separate model change.
- `!reset` is the only control message; otherwise chat is automatic.

## Create the Discord bot

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications), add a bot, and copy its bot token.
2. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**. Discord requires this to read normal messages; the code ignores every channel except the one configured by ID.
3. Invite it with the `bot` scope and only these channel permissions: **View Channel**, **Send Messages**, and **Read Message History**. It does not need Administrator.
4. In Discord, enable Developer Mode, right-click the dedicated channel, and choose **Copy Channel ID**.

## Run it

Python 3.10+:

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

Set `DISCORD_TOKEN`, `XKIRO_API_KEY`, and `DISCORD_CHANNEL_ID` in `.env`. Choose one exact `XKIRO_MODEL`, for example `openai/gpt-5.6-sol`. xKiro automatically routes requests for that model across provider routes and performs its own failover. The bot can optionally try comma-separated `XKIRO_FALLBACK_MODELS` if that model itself is unavailable. Browse IDs at <https://api.xkiro.com/v1/models>.

`CHAT_CONTEXT_MODE=shared` is best for a public community lounge. All channel participants are included in the same conversation, so clearly disclose this and do not use it for private support. `CHAT_CONTEXT_MODE=per_user` keeps contexts separate if users need privacy.

The bot itself must have its own API key on the machine/server it runs on; it cannot read an API key saved inside your OpenCode installation. This lets others self-host using their own keys. Never commit `.env`, the SQLite database, or bot/API credentials.

## Run continuously / invite others

Keep the process running on a computer or VPS. If publishing the source, other people create their own Discord application and set their own three required `.env` values. Do not operate a shared public instance using an unbounded owner-funded key: each message is a billable xKiro request. Keep the daily cap low and monitor usage.

Chats from the configured channel go to xKiro for model inference. Tell server members the channel is processed by an AI service. The bot stores user IDs and chat messages locally until the configured history TTL expires; usage totals are retained by UTC day for quota enforcement.

## xKiro model routing

Set the model ID you want in `XKIRO_MODEL`. xKiro's gateway automatically selects the provider route for that model and fails over to another route for the same model when available. The response still identifies the model you requested. Optionally, `XKIRO_FALLBACK_MODELS` lists different model IDs for the bot to try if your selected model itself is unavailable. This is a separate model fallback, not xKiro's provider auto-routing. Authentication, balance, and rate-limit errors are not retried by the bot.

## Test

```powershell
python -m unittest -v
```

Tests use a temporary SQLite database and mock Discord/xKiro calls. No credentials or paid requests are needed.

## xKiro referral

This project uses [xKiro](https://xkiro.com), an AI model gateway. If you want to try it, [sign up through the author's referral link](https://xkiro.com/ref/D6J4DHK).

## License

MIT; see `LICENSE`.
