"""Automatic Discord chat bot using xKiro's provider-route selection and failover."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import discord
import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "").strip()
XKIRO_API_KEY = os.getenv("XKIRO_API_KEY", "").strip()
XKIRO_MODEL = os.getenv("XKIRO_MODEL", "openai/gpt-5.6-sol").strip()
XKIRO_FALLBACK_MODELS = list(dict.fromkeys(
    model.strip() for model in os.getenv("XKIRO_FALLBACK_MODELS", "").split(",")
    if model.strip() and model.strip() != XKIRO_MODEL
))
MAX_MODEL_FALLBACKS = min(3, max(0, int(os.getenv("MAX_MODEL_FALLBACKS", "1"))))
DAILY_USER_LIMIT = max(1, int(os.getenv("DAILY_USER_LIMIT", "30")))
HISTORY_TTL_HOURS = max(1, int(os.getenv("HISTORY_TTL_HOURS", "24")))
CHAT_CONTEXT_MODE = os.getenv("CHAT_CONTEXT_MODE", "shared").strip().lower()
XKIRO_URL = "https://api.xkiro.com/v1/chat/completions"
DB_PATH = Path(os.getenv("CHAT_DB_PATH", str(ROOT / "chats.sqlite3")))
MAX_PROMPT = 8_000
MAX_REPLY = 1_850
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CHARS = 18_000
SYSTEM_PROMPT = (
    "You are a helpful assistant in a shared Discord channel. The conversation history may "
    "include messages from multiple channel members; use it as shared room context and refer "
    "to speakers by their supplied display names. Keep answers readable and useful. Use "
    "Markdown sparingly. Do not claim to have performed real-world actions."
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("xkiro-discord")

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
channel_locks: dict[int, asyncio.Lock] = {}


@contextmanager
def database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS messages_chat_time ON messages(channel_id, user_id, created_at)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                user_id INTEGER NOT NULL,
                utc_day TEXT NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id, utc_day)
            )
        """)
        connection.commit()
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_history(channel_id: int, user_id: int) -> list[dict[str, str]]:
    cutoff = int(time.time()) - HISTORY_TTL_HOURS * 3600
    with database() as db:
        db.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        rows = db.execute(
            "SELECT role, content FROM messages WHERE channel_id = ? AND user_id = ? AND created_at >= ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (channel_id, user_id, cutoff, MAX_HISTORY_MESSAGES),
        ).fetchall()
    rows.reverse()
    result: list[dict[str, str]] = []
    total = 0
    for row in reversed(rows):
        content = row["content"][:MAX_PROMPT]
        if total + len(content) > MAX_HISTORY_CHARS:
            break
        result.append({"role": row["role"], "content": content})
        total += len(content)
    result.reverse()
    return result


def clear_history(channel_id: int, user_id: int) -> int:
    with database() as db:
        result = db.execute("DELETE FROM messages WHERE channel_id = ? AND user_id = ?", (channel_id, user_id))
    return result.rowcount


def history_preview(channel_id: int, user_id: int) -> str:
    rows = get_history(channel_id, user_id)
    lines = [f"**{'You' if row['role'] == 'user' else 'xKiro'}:** {row['content'][:700]}" for row in rows[-4:]]
    return "\n\n".join(lines)[:2_500] or "No saved context in this channel yet."


def save_exchange(channel_id: int, user_id: int, prompt: str, answer: str) -> None:
    now = int(time.time())
    with database() as db:
        db.executemany(
            "INSERT INTO messages(channel_id, user_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            [(channel_id, user_id, "user", prompt[:MAX_PROMPT], now),
             (channel_id, user_id, "assistant", answer[:MAX_HISTORY_CHARS], now + 1)],
        )
        db.execute("DELETE FROM messages WHERE created_at < ?", (now - HISTORY_TTL_HOURS * 3600,))


def consume_daily_quota(user_id: int) -> bool:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    cutoff_day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 90 * 86400))
    with database() as db:
        db.execute("DELETE FROM usage WHERE utc_day < ?", (cutoff_day,))
        row = db.execute("SELECT requests FROM usage WHERE user_id = ? AND utc_day = ?", (user_id, today)).fetchone()
        if row and row["requests"] >= DAILY_USER_LIMIT:
            return False
        db.execute(
            "INSERT INTO usage(user_id, utc_day, requests) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, utc_day) DO UPDATE SET requests = requests + 1",
            (user_id, today),
        )
    return True


async def xkiro_chat(messages: list[dict[str, str]], *, max_tokens: int = 700) -> tuple[str, str]:
    if not XKIRO_API_KEY:
        raise RuntimeError("The bot owner needs to configure XKIRO_API_KEY.")
    candidates = list(dict.fromkeys([XKIRO_MODEL, *XKIRO_FALLBACK_MODELS]))[:1 + MAX_MODEL_FALLBACKS]

    timeout = httpx.Timeout(90, connect=15)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for index, model_id in enumerate(candidates):
            try:
                response = await client.post(
                    XKIRO_URL,
                    headers={"Authorization": f"Bearer {XKIRO_API_KEY}"},
                    json={"model": model_id, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
                          "max_tokens": max_tokens, "temperature": 0.7},
                )
            except httpx.TimeoutException:
                if index + 1 < len(candidates):
                    continue
                raise RuntimeError("xKiro timed out while generating a response.")
            except httpx.HTTPError as exc:
                raise RuntimeError("Could not connect to xKiro.") from exc

            if response.status_code in {403, 404, 408, 500, 502, 503, 529} and index + 1 < len(candidates):
                log.info("Configured model fallback from %s after HTTP %s", model_id, response.status_code)
                continue
            if response.status_code == 401:
                raise RuntimeError("xKiro rejected the API key (HTTP 401). Check the bot owner's key.")
            if response.status_code == 402:
                raise RuntimeError("xKiro reports insufficient balance (HTTP 402).")
            if response.status_code == 403:
                raise RuntimeError(f"No eligible model is available to this xKiro account (HTTP 403; {model_id}).")
            if response.status_code == 429:
                raise RuntimeError("xKiro rate limit reached. Wait a little and try again.")
            try:
                response.raise_for_status()
                answer = response.json()["choices"][0]["message"]["content"]
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"xKiro request failed (HTTP {response.status_code}).") from exc
            if not isinstance(answer, str) or not answer.strip():
                raise RuntimeError("xKiro returned an empty answer.")
            log.info("Requested xKiro model=%s", model_id)
            return answer.strip(), model_id
    raise RuntimeError("No xKiro model route succeeded.")


async def send_long_reply(message: discord.Message, text: str, model_id: str) -> None:
    chunks = [text[i:i + MAX_REPLY] for i in range(0, len(text), MAX_REPLY)] or ["(Empty response)"]
    await message.reply(f"{chunks[0]}\n\n*via `{model_id}`*", mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())
    for chunk in chunks[1:]:
        await message.channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())


async def handle_message(message: discord.Message) -> None:
    if message.author.bot or message.webhook_id or not DISCORD_CHANNEL_ID:
        return
    if message.channel.id != int(DISCORD_CHANNEL_ID):
        return
    prompt = message.content.strip()
    if not prompt:
        return
    if len(prompt) > MAX_PROMPT:
        await message.reply(f"Please keep messages under {MAX_PROMPT:,} characters.", mention_author=False)
        return
    if not consume_daily_quota(message.author.id):
        await message.reply(f"This account's daily limit of {DAILY_USER_LIMIT} messages has been reached.", mention_author=False)
        return

    channel_id = message.channel.id
    context_user_id = 0 if CHAT_CONTEXT_MODE == "shared" else message.author.id
    async with channel_locks.setdefault(channel_id, asyncio.Lock()):
        try:
            async with message.channel.typing():
                context = get_history(channel_id, context_user_id)
                prompt_with_name = f"{message.author.display_name}: {prompt}"
                context.append({"role": "user", "content": prompt_with_name})
                answer, selected_model = await xkiro_chat(context)
            save_exchange(channel_id, context_user_id, prompt_with_name, answer)
            log.info("Answered channel=%s user=%s model=%s", channel_id, message.author.id, selected_model)
            await send_long_reply(message, answer, selected_model)
        except (RuntimeError, httpx.HTTPError) as exc:
            log.warning("xKiro request failed channel=%s user=%s: %s", channel_id, message.author.id, exc)
            await message.reply(f"I couldn't get a response right now: {exc}", mention_author=False)
        except discord.HTTPException:
            log.exception("Could not send Discord reply channel=%s", channel_id)
        except sqlite3.Error:
            log.exception("Chat storage failed channel=%s", channel_id)
            await message.reply("I couldn't access chat history right now. Please try again.", mention_author=False)


@bot.event
async def on_ready() -> None:
    log.info("Ready as %s; channel=%s; context=%s; model=%s", bot.user, DISCORD_CHANNEL_ID, CHAT_CONTEXT_MODE, XKIRO_MODEL)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.webhook_id or not DISCORD_CHANNEL_ID:
        return
    if message.channel.id != int(DISCORD_CHANNEL_ID):
        return
    command = message.content.strip().lower()
    if command == "!reset":
        user_id = 0 if CHAT_CONTEXT_MODE == "shared" else message.author.id
        count = clear_history(message.channel.id, user_id)
        await message.reply(f"Cleared this chat's saved context ({count} messages).", mention_author=False, delete_after=15)
        return
    if command == "!context":
        user_id = 0 if CHAT_CONTEXT_MODE == "shared" else message.author.id
        try:
            await message.author.send(f"Saved context in <#{message.channel.id}>:\n\n{history_preview(message.channel.id, user_id)}")
            await message.reply("Sent the saved context preview to your DMs.", mention_author=False, delete_after=15)
        except discord.Forbidden:
            await message.reply("I couldn't DM you. Enable DMs and try `!context` again.", mention_author=False)
        return
    await handle_message(message)


async def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env")
    if not XKIRO_API_KEY:
        raise SystemExit("Set XKIRO_API_KEY in .env")
    try:
        channel_id = int(DISCORD_CHANNEL_ID)
        if channel_id <= 0:
            raise ValueError
    except ValueError as exc:
        raise SystemExit("Set DISCORD_CHANNEL_ID to the numeric ID of one dedicated text channel.") from exc
    if len(DISCORD_TOKEN) < 30:
        raise SystemExit("DISCORD_TOKEN looks too short; copy the bot token from Discord Developer Portal.")
    if CHAT_CONTEXT_MODE not in {"shared", "per_user"}:
        raise SystemExit("CHAT_CONTEXT_MODE must be 'shared' or 'per_user'.")
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
