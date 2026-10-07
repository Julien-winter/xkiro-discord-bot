"""Automatic, channel-bound Discord chat bot powered by xKiro."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
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
XKIRO_MODEL = os.getenv("XKIRO_MODEL", "auto").strip()
XKIRO_DEFAULT_MODEL = os.getenv("XKIRO_DEFAULT_MODEL", "openai/gpt-5.6-sol").strip()
XKIRO_FALLBACK_MODELS = [
    model.strip()
    for model in os.getenv("XKIRO_FALLBACK_MODELS", "").split(",")
    if model.strip()
]
MAX_ROUTING_ATTEMPTS = min(6, max(1, int(os.getenv("MAX_ROUTING_ATTEMPTS", "3"))))
DAILY_USER_LIMIT = max(1, int(os.getenv("DAILY_USER_LIMIT", "30")))
HISTORY_TTL_HOURS = max(1, int(os.getenv("HISTORY_TTL_HOURS", "24")))
CHAT_CONTEXT_MODE = os.getenv("CHAT_CONTEXT_MODE", "shared").strip().lower()
XKIRO_URL = "https://api.xkiro.com/v1/chat/completions"
XKIRO_MODELS_URL = "https://api.xkiro.com/v1/models"
DB_PATH = Path(os.getenv("CHAT_DB_PATH", str(ROOT / "chats.sqlite3")))
AUTO_ROUTING = XKIRO_MODEL.lower() == "auto"
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
model_catalog: list[dict] = []
model_catalog_fetched_at = 0.0
catalog_lock = asyncio.Lock()
model_cooldowns: dict[str, float] = {}


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
        connection.execute("""
            CREATE INDEX IF NOT EXISTS messages_chat_time
            ON messages(channel_id, user_id, created_at)
        """)
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
            "SELECT role, content FROM messages "
            "WHERE channel_id = ? AND user_id = ? AND created_at >= ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (channel_id, user_id, cutoff, MAX_HISTORY_MESSAGES),
        ).fetchall()
    rows.reverse()
    # Bound prompt size even when users paste very long Discord messages.
    history: list[dict[str, str]] = []
    total = 0
    for row in reversed(rows):
        content = row["content"][:MAX_PROMPT]
        if total + len(content) > MAX_HISTORY_CHARS:
            break
        history.append({"role": row["role"], "content": content})
        total += len(content)
    history.reverse()
    return history


def history_preview(channel_id: int, user_id: int) -> str:
    """Return a short context-only preview for an ephemeral privacy command."""
    rows = get_history(channel_id, user_id)
    lines = []
    for item in rows[-4:]:
        label = "You" if item["role"] == "user" else "xKiro"
        lines.append(f"**{label}:** {item['content'][:700]}")
    text = "\n\n".join(lines)
    return text[:MAX_REPLY_CONTEXT_CHARS] or "No saved context in this channel yet."


def clear_history(channel_id: int, user_id: int) -> int:
    with database() as db:
        result = db.execute(
            "DELETE FROM messages WHERE channel_id = ? AND user_id = ?",
            (channel_id, user_id),
        )
    return result.rowcount


def save_exchange(channel_id: int, user_id: int, prompt: str, answer: str) -> None:
    now = int(time.time())
    with database() as db:
        db.executemany(
            "INSERT INTO messages(channel_id, user_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                (channel_id, user_id, "user", prompt[:MAX_PROMPT], now),
                (channel_id, user_id, "assistant", answer[:MAX_HISTORY_CHARS], now + 1),
            ],
        )
        cutoff = now - HISTORY_TTL_HOURS * 3600
        db.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))


def consume_daily_quota(user_id: int) -> bool:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    with database() as db:
        db.execute("DELETE FROM usage WHERE utc_day < ?", (time.strftime("%Y-%m-%d", time.gmtime(time.time() - 90 * 86400)),))
        row = db.execute(
            "SELECT requests FROM usage WHERE user_id = ? AND utc_day = ?",
            (user_id, today),
        ).fetchone()
        if row and row["requests"] >= DAILY_USER_LIMIT:
            return False
        db.execute(
            "INSERT INTO usage(user_id, utc_day, requests) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, utc_day) DO UPDATE SET requests = requests + 1",
            (user_id, today),
        )
    return True


async def fetch_model_catalog(*, force: bool = False) -> list[dict]:
    """Fetch xKiro's public chat catalogue and keep it cached for five minutes."""
    global model_catalog, model_catalog_fetched_at
    if not force and model_catalog and time.time() - model_catalog_fetched_at < 300:
        return model_catalog
    async with catalog_lock:
        if not force and model_catalog and time.time() - model_catalog_fetched_at < 300:
            return model_catalog
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=8)) as client:
                response = await client.get(XKIRO_MODELS_URL, params={"modality": "chat"})
                response.raise_for_status()
        except httpx.HTTPError:
            if model_catalog:
                return model_catalog
            raise
        items = response.json().get("data", [])
        model_catalog = [item for item in items if isinstance(item, dict) and item.get("id")]
        model_catalog_fetched_at = time.time()
        return model_catalog


def classify_request(messages: list[dict[str, str]]) -> str:
    latest_user = next((item.get("content", "") for item in reversed(messages) if item.get("role") == "user"), "")
    text = latest_user.split(": ", 1)[-1].lower()
    if re.search(r"\b(translate|übersetz|tradu|翻译|переведи)\b", text):
        return "translation"
    if re.search(r"\b(code|coding|python|javascript|typescript|debug|stack trace|bug|script|program)\b", text):
        return "coding"
    if re.search(r"\b(analy[sz]|compare|reason|step by step|architecture|trade-?off|why|warum|beweise|prove)\b", text):
        return "reasoning"
    if re.search(r"\b(story|creative|poem|lyrics|brainstorm|write|schreib|geschichte|idee|ideen)\b", text):
        return "creative"
    return "general"


def rank_models(catalog: list[dict], messages: list[dict[str, str]]) -> list[str]:
    """Rank accessible chat models for the request type, favoring free tiers first."""
    task = classify_request(messages)
    scored: list[tuple[int, str]] = []
    for item in catalog:
        model_id = str(item.get("id", ""))
        if not model_id or item.get("modality", "chat") != "chat":
            continue
        identifier = model_id.lower()
        capabilities = item.get("capabilities") or {}
        score = 0
        if task == "coding" and any(part in identifier for part in ("coder", "code", "codestral", "devstral")):
            score += 12
        elif task == "reasoning" and capabilities.get("reasoning"):
            score += 12
        elif task == "translation" and "translat" in identifier:
            score += 12
        elif task == "creative" and any(part in identifier for part in ("creative", "writer", "muse")):
            score += 8
        elif task == "general" and capabilities.get("tools"):
            score += 2

        tier = str(item.get("access_tier", "")).lower()
        if tier == "free" or identifier.endswith(":free"):
            score += 4
        elif tier in {"premium", "paid"}:
            score -= 1
        pricing = item.get("pricing") or {}
        try:
            estimated_cost = float(pricing.get("input") or 0) + 2 * float(pricing.get("output") or 0)
            score -= min(math.ceil(estimated_cost * 3), 8)
        except (TypeError, ValueError):
            pass
        score += min(int(item.get("context_length") or 0) // 100_000, 3)
        scored.append((score, model_id))

    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    ordered = [model_id for _, model_id in scored]
    preferred = [XKIRO_DEFAULT_MODEL, *XKIRO_FALLBACK_MODELS]
    candidates = ordered[:1]
    candidates.extend(model for model in preferred if model not in candidates)
    candidates.extend(model for model in ordered[1:] if model not in candidates)
    now = time.time()
    available = [model for model in candidates if model_cooldowns.get(model, 0) <= now]
    if not available:
        model_cooldowns.clear()
        available = candidates
    return list(dict.fromkeys(available))[:MAX_ROUTING_ATTEMPTS]


async def model_candidates(messages: list[dict[str, str]]) -> list[str]:
    if not AUTO_ROUTING:
        return [XKIRO_MODEL, *[m for m in XKIRO_FALLBACK_MODELS if m != XKIRO_MODEL]][:MAX_ROUTING_ATTEMPTS]
    try:
        catalog = await fetch_model_catalog()
        candidates = rank_models(catalog, messages)
        if candidates:
            return candidates
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log.warning("Model catalogue unavailable; using configured fallback models (%s)", type(exc).__name__)
    return list(dict.fromkeys([XKIRO_DEFAULT_MODEL, *XKIRO_FALLBACK_MODELS]))[:MAX_ROUTING_ATTEMPTS]


async def xkiro_chat(messages: list[dict[str, str]], *, max_tokens: int = 700) -> tuple[str, str]:
    if not XKIRO_API_KEY:
        raise RuntimeError("The bot owner needs to configure XKIRO_API_KEY.")

    candidates = await model_candidates(messages)
    fallback_statuses = {403, 404, 408, 500, 502, 503, 529}
    last_status: int | None = None
    timeout = httpx.Timeout(90, connect=15)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for index, model_id in enumerate(candidates):
            try:
                response = await client.post(
                    XKIRO_URL,
                    headers={"Authorization": f"Bearer {XKIRO_API_KEY}"},
                    json={
                        "model": model_id,
                        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
                        "max_tokens": max_tokens,
                        "temperature": 0.7,
                    },
                )
            except httpx.TimeoutException:
                last_status = 408
                if index + 1 < len(candidates):
                    continue
                raise RuntimeError("xKiro timed out while generating a response.")
            except httpx.HTTPError as exc:
                raise RuntimeError("Could not connect to xKiro.") from exc

            if response.status_code in fallback_statuses and index + 1 < len(candidates):
                last_status = response.status_code
                model_cooldowns[model_id] = time.time() + (600 if response.status_code in {403, 404} else 60)
                log.info("Auto-router falling back after HTTP %s (model=%s)", response.status_code, model_id)
                continue
            if response.status_code == 401:
                raise RuntimeError("xKiro rejected the API key (HTTP 401). Check the bot owner's key.")
            if response.status_code == 402:
                raise RuntimeError("xKiro reports insufficient balance (HTTP 402).")
            if response.status_code == 403:
                raise RuntimeError(f"No routed model is available to this xKiro key (HTTP 403; last model: {model_id}).")
            if response.status_code == 429:
                raise RuntimeError("xKiro rate limit reached across the available fallback models.")
            try:
                response.raise_for_status()
                result = response.json()["choices"][0]["message"]["content"]
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"xKiro request failed (HTTP {response.status_code}).") from exc
            if not isinstance(result, str) or not result.strip():
                raise RuntimeError("xKiro returned an empty answer.")
            return result.strip(), model_id
    raise RuntimeError(f"xKiro could not serve any routed model (last HTTP status: {last_status}).")


async def send_long_reply(message: discord.Message, text: str, model_id: str) -> None:
    chunks = [text[i:i + MAX_REPLY] for i in range(0, len(text), MAX_REPLY)] or ["(Empty response)"]
    first = f"{chunks[0]}\n\n*via `{model_id}`*"
    await message.reply(first, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    for chunk in chunks[1:]:
        await message.channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())


async def handle_message(message: discord.Message) -> None:
    if message.author.bot or message.webhook_id:
        return
    if not DISCORD_CHANNEL_ID or message.channel.id != int(DISCORD_CHANNEL_ID):
        return

    prompt = message.content.strip()
    if not prompt:
        return
    if len(prompt) > MAX_PROMPT:
        await message.reply(f"Please keep messages under {MAX_PROMPT:,} characters.", mention_author=False)
        return
    if not consume_daily_quota(message.author.id):
        await message.reply(
            f"This account's daily limit of {DAILY_USER_LIMIT} messages has been reached. Try again tomorrow.",
            mention_author=False,
        )
        return

    channel_id = message.channel.id
    lock = channel_locks.setdefault(channel_id, asyncio.Lock())
    context_user_id = 0 if CHAT_CONTEXT_MODE == "shared" else message.author.id
    async with lock:
        try:
            async with message.channel.typing():
                messages = get_history(channel_id, context_user_id)
                room_prompt = f"{message.author.display_name}: {prompt}"
                messages.append({"role": "user", "content": room_prompt})
                answer, selected_model = await xkiro_chat(messages)
            save_exchange(channel_id, context_user_id, room_prompt, answer)
            log.info("Answered channel=%s user=%s model=%s", channel_id, message.author.id, selected_model)
            await send_long_reply(message, answer, selected_model)
        except (RuntimeError, httpx.HTTPError) as exc:
            # Avoid putting raw request data, provider response bodies, or secrets in Discord/logs.
            log.warning("xKiro request failed (channel_id=%s, user_id=%s): %s", channel_id, message.author.id, exc)
            await message.reply(f"I couldn't get a response right now: {exc}", mention_author=False)
        except discord.HTTPException:
            log.exception("Could not send Discord reply (channel_id=%s)", channel_id)
        except sqlite3.Error:
            log.exception("Chat storage failed (channel_id=%s)", channel_id)
            await message.reply("I couldn't access chat history right now. Please try again.", mention_author=False)


@bot.event
async def on_ready() -> None:
    log.info(
        "Automatic chat ready as %s; channel_id=%s context=%s routing=%s",
        bot.user, DISCORD_CHANNEL_ID, CHAT_CONTEXT_MODE, XKIRO_MODEL,
    )


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.webhook_id:
        return
    if not DISCORD_CHANNEL_ID or message.channel.id != int(DISCORD_CHANNEL_ID):
        return
    if message.content.strip().lower() == "!reset":
        user_id = 0 if CHAT_CONTEXT_MODE == "shared" else message.author.id
        count = clear_history(message.channel.id, user_id)
        await message.reply(
            f"Cleared this chat's saved context ({count} messages).",
            mention_author=False,
            delete_after=15,
        )
        return
    await handle_message(message)


async def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in discord_bot/.env")
    if not XKIRO_API_KEY:
        raise SystemExit("Set XKIRO_API_KEY in discord_bot/.env")
    try:
        channel_id = int(DISCORD_CHANNEL_ID)
        if channel_id <= 0:
            raise ValueError
    except ValueError as exc:
        raise SystemExit("Set DISCORD_CHANNEL_ID to the numeric ID of one dedicated text channel.") from exc
    if len(DISCORD_TOKEN) < 30:
        raise SystemExit("DISCORD_TOKEN looks too short; copy the bot token from the Discord Developer Portal.")
    if CHAT_CONTEXT_MODE not in {"shared", "per_user"}:
        raise SystemExit("CHAT_CONTEXT_MODE must be 'shared' or 'per_user'.")
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
