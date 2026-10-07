# Security

- Keep Discord bot tokens and xKiro API keys in `.env`; never commit them.
- The bot only processes messages in the channel ID configured in `DISCORD_CHANNEL_ID`.
- Each user's model history is separated by both Discord channel ID and user ID.
- The selected channel's prompts are sent to xKiro for inference. Tell channel members before enabling the bot.
- Local SQLite chat history expires after `HISTORY_TTL_HOURS`; deleting `chats.sqlite3` clears it immediately.
- Report vulnerabilities privately to the maintainer and do not include secrets or private messages in issue reports.
