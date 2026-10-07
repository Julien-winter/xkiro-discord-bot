# Security

- Keep Discord bot tokens and xKiro API keys in `.env`; never commit them.
- The bot only processes configured guild/channel IDs.
- Each user's memory is separated by guild and Discord user ID and deleted after seven days of inactivity.
- The selected channel's prompts are sent to xKiro for inference. Tell channel members before enabling the bot.
- Local SQLite memory stores recent prompts and responses; deleting `chats.sqlite3` clears it immediately.
- Report vulnerabilities privately to the maintainer and do not include secrets or private messages in issue reports.
- Uploaded files and user-provided links are untrusted. The bot restricts remote-link fetching to HTTPS `file.io`, validates redirects, and enforces a streamed size limit.
- Image uploads are rejected. Free model requests use the xKiro public model catalogue and cannot fall back to paid-tier models.
- Adult text roleplay requires both explicit configuration and a Discord channel marked NSFW; user instructions cannot override the safety system prompt.
- The blocklist is local to bot instances using the same database; it is not a global Discord enforcement system.
