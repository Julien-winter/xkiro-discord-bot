import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

import bot


class DiscordAutoChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "test.sqlite3"
        self.db_patch = patch.object(bot, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_history_is_saved_and_isolated_by_channel_and_user(self):
        bot.save_exchange(111, 10, "my private question", "private reply")
        own_history = bot.get_history(111, 10)
        other_user = bot.get_history(111, 20)
        other_channel = bot.get_history(222, 10)
        self.assertEqual([item["content"] for item in own_history], ["my private question", "private reply"])
        self.assertEqual(other_user, [])
        self.assertEqual(other_channel, [])

    def test_daily_quota_is_persistent_and_per_user(self):
        with patch.object(bot, "DAILY_USER_LIMIT", 1):
            self.assertTrue(bot.consume_daily_quota(10))
            self.assertFalse(bot.consume_daily_quota(10))
            self.assertTrue(bot.consume_daily_quota(20))

    def test_expired_history_is_removed(self):
        with patch.object(bot, "HISTORY_TTL_HOURS", 1):
            bot.save_exchange(111, 10, "old question", "old answer")
            with bot.database() as db:
                db.execute("UPDATE messages SET created_at = 1")
            self.assertEqual(bot.get_history(111, 10), [])

    def test_reset_deletes_only_callers_channel_history(self):
        bot.save_exchange(111, 10, "user one", "answer one")
        bot.save_exchange(111, 20, "user two", "answer two")
        self.assertEqual(bot.clear_history(111, 10), 2)
        self.assertEqual(bot.get_history(111, 10), [])
        self.assertEqual(len(bot.get_history(111, 20)), 2)

    def test_shared_channel_history_can_be_loaded_for_next_speaker(self):
        bot.save_exchange(111, 0, "Alex: hello", "Hi Alex")
        bot.save_exchange(111, 0, "Sam: follow up", "Hi Sam")
        history = bot.get_history(111, 0)
        self.assertEqual(len(history), 4)
        self.assertIn("Alex", history[0]["content"])
        self.assertEqual(history[2]["content"], "Hi Alex")
        self.assertEqual(history[3]["content"], "Hi Sam")

    def test_task_router_prioritizes_code_and_reasoning_models(self):
        catalog = [
            {"id": "plain/general", "capabilities": {"reasoning": False}, "access_tier": "paid"},
            {"id": "qwen/coder-free:free", "capabilities": {"reasoning": False}, "access_tier": "free"},
            {"id": "reason/deep", "capabilities": {"reasoning": True}, "access_tier": "paid"},
        ]
        with patch.object(bot, "XKIRO_DEFAULT_MODEL", "plain/general"), patch.object(bot, "XKIRO_FALLBACK_MODELS", []), patch.dict(bot.model_cooldowns, {}, clear=True):
            coding = bot.rank_models(catalog, [{"role": "user", "content": "Please debug this python code"}])
            reasoning = bot.rank_models(catalog, [{"role": "user", "content": "Compare these and explain why step by step"}])
        self.assertEqual(coding[0], "qwen/coder-free:free")
        self.assertEqual(reasoning[0], "reason/deep")

    def test_router_prefers_selected_task_model_then_default_fallback(self):
        catalog = [
            {"id": "code/model", "capabilities": {}, "access_tier": "free"},
            {"id": "general/model", "capabilities": {"tools": True}, "access_tier": "paid"},
        ]
        with patch.object(bot, "XKIRO_DEFAULT_MODEL", "preferred/default"), patch.object(bot, "XKIRO_FALLBACK_MODELS", ["backup/one"]), patch.dict(bot.model_cooldowns, {}, clear=True):
            result = bot.rank_models(catalog, [{"role": "user", "content": "debug this python"}])
        self.assertEqual(result[:3], ["code/model", "preferred/default", "backup/one"])

    def test_auto_router_falls_back_when_a_model_is_unavailable(self):
        class Response:
            def __init__(self, status, content=None):
                self.status_code = status
                self._content = content

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise httpx.HTTPStatusError("failed", request=Mock(), response=Mock(status_code=self.status_code))

            def json(self):
                return {"choices": [{"message": {"content": self._content}}]}

        class Client:
            def __init__(self, **kwargs):
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, headers, json):
                self.calls.append(json["model"])
                return Response(403 if len(self.calls) == 1 else 200, "fallback answer")

        async def scenario():
            client = Client()
            with patch.object(bot, "XKIRO_API_KEY", "unit-test-key"), patch.object(bot, "model_candidates", new=AsyncMock(return_value=["unavailable/model", "working/model"])), patch.object(bot.httpx, "AsyncClient", return_value=client):
                return await bot.xkiro_chat([{"role": "user", "content": "hello"}]), client.calls

        (answer, model), calls = asyncio.run(scenario())
        self.assertEqual(answer, "fallback answer")
        self.assertEqual(model, "working/model")
        self.assertEqual(calls, ["unavailable/model", "working/model"])

    def test_auto_router_does_not_fallback_on_invalid_credentials(self):
        class Response:
            status_code = 401

        class Client:
            def __init__(self, **kwargs):
                self.calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, *args, **kwargs):
                self.calls += 1
                return Response()

        async def scenario():
            client = Client()
            with patch.object(bot, "XKIRO_API_KEY", "unit-test-key"), patch.object(bot, "model_candidates", new=AsyncMock(return_value=["model/one", "model/two"])), patch.object(bot.httpx, "AsyncClient", return_value=client):
                with self.assertRaisesRegex(RuntimeError, "rejected the API key"):
                    await bot.xkiro_chat([{"role": "user", "content": "hello"}])
            return client.calls

        self.assertEqual(asyncio.run(scenario()), 1)

    def test_bot_messages_and_wrong_channel_are_ignored(self):
        async def scenario():
            message = type("Message", (), {})()
            message.author = type("Author", (), {"bot": False, "id": 10})()
            message.webhook_id = None
            message.channel = type("Channel", (), {"id": 123, "typing": lambda self: None})()
            message.content = "hello"
            message.reply = AsyncMock()
            with patch.object(bot, "DISCORD_CHANNEL_ID", "456"):
                await bot.handle_message(message)
            message.reply.assert_not_called()
        import asyncio
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
