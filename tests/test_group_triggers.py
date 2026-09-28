"""In a group the bot only answers messages meant for it.

Run from the repo root: .venv/bin/python -m unittest tests.test_group_triggers -v

Group privacy is a Telegram-side setting, so "both privacy states" means:
with privacy ON Telegram only delivers commands, mentions and replies to the
bot; with privacy OFF it also delivers ordinary group chatter. The code must
answer the first kind and ignore the chatter either way.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram import Chat, Message, MessageEntity, User  # noqa: E402

import bot  # noqa: E402

BOT_ID = 999
BOT_USERNAME = "deskmate_test_2_bot"
STAFF = User(id=5, first_name="Staff", is_bot=False)
BOT_USER = User(id=BOT_ID, first_name="Deskmate", is_bot=True, username=BOT_USERNAME)
OTHER_BOT = User(id=777, first_name="Other", is_bot=True, username="other_bot")
GROUP = Chat(id=-1001, type=Chat.SUPERGROUP)
PRIVATE = Chat(id=5, type=Chat.PRIVATE)


def message(text, chat=GROUP, entities=(), reply_to=None) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=STAFF,
        text=text,
        entities=list(entities),
        reply_to_message=reply_to,
    )


def mention(text: str, handle: str) -> MessageEntity:
    # Offsets are in UTF-16 code units, which is what Telegram sends.
    prefix = text[: text.index(handle)]
    offset = len(prefix.encode("utf-16-le")) // 2
    return MessageEntity(type=MessageEntity.MENTION, offset=offset, length=len(handle.encode("utf-16-le")) // 2)


def bot_message(text="Answer: 30 minutes.") -> Message:
    return Message(message_id=0, date=datetime.now(timezone.utc), chat=GROUP, from_user=BOT_USER, text=text)


def question_of(msg: Message):
    return bot.extract_question(msg, BOT_ID, BOT_USERNAME)


class ExtractQuestionTests(unittest.TestCase):
    def test_private_chat_answers_everything_as_before(self):
        self.assertEqual(question_of(message("How long is probation?", chat=PRIVATE)), "How long is probation?")

    def test_plain_group_chatter_is_ignored(self):
        # Only delivered when privacy is off; must never be answered.
        for text in ("ok thanks", "who wants coffee?", "How long is probation?"):
            self.assertIsNone(question_of(message(text)))

    def test_mention_at_start_is_answered_without_the_mention(self):
        text = "@deskmate_test_2_bot How long is probation?"
        self.assertEqual(question_of(message(text, entities=[mention(text, "@deskmate_test_2_bot")])), "How long is probation?")

    def test_mention_anywhere_and_in_any_case(self):
        text = "Quick one @Deskmate_Test_2_Bot, how long is probation?"
        self.assertEqual(
            question_of(message(text, entities=[mention(text, "@Deskmate_Test_2_Bot")])), "Quick one , how long is probation?"
        )

    def test_mention_after_emoji_uses_utf16_offsets(self):
        text = "👋 @deskmate_test_2_bot how long is probation?"
        self.assertEqual(
            question_of(message(text, entities=[mention(text, "@deskmate_test_2_bot")])), "👋 how long is probation?"
        )

    def test_mentioning_another_bot_is_ignored(self):
        text = "@other_bot How long is probation?"
        self.assertIsNone(question_of(message(text, entities=[mention(text, "@other_bot")])))

    def test_username_text_without_a_mention_entity_is_ignored(self):
        # e.g. inside a code block Telegram doesn't mark it as a mention
        self.assertIsNone(question_of(message("see @deskmate_test_2_bot docs")))

    def test_text_mention_of_the_bot_counts(self):
        text = "Deskmate how long is probation?"
        entity = MessageEntity(type=MessageEntity.TEXT_MENTION, offset=0, length=8, user=BOT_USER)
        self.assertEqual(question_of(message(text, entities=[entity])), "how long is probation?")

    def test_reply_to_the_bot_is_answered(self):
        self.assertEqual(question_of(message("And for casuals?", reply_to=bot_message())), "And for casuals?")

    def test_reply_to_someone_else_is_ignored(self):
        other = Message(message_id=0, date=datetime.now(timezone.utc), chat=GROUP, from_user=STAFF, text="hi")
        self.assertIsNone(question_of(message("And for casuals?", reply_to=other)))
        not_us = Message(message_id=0, date=datetime.now(timezone.utc), chat=GROUP, from_user=OTHER_BOT, text="hi")
        self.assertIsNone(question_of(message("And for casuals?", reply_to=not_us)))

    def test_bare_mention_gives_an_empty_question(self):
        text = "@deskmate_test_2_bot"
        self.assertEqual(question_of(message(text, entities=[mention(text, text)])), "")


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.state = mock.Mock()
        self.state.config.allowed_chat_ids = frozenset()
        self.state.pending_buffers = {}
        self.answered = []

        async def fake_answer(update, context, state, question, previous=None):
            self.answered.append(question)
            self.previous.append(previous)

        self.previous = []
        self.state.answer_questions = __import__("collections").OrderedDict()
        self.state.question_prompts = __import__("collections").OrderedDict()

        patches = [
            mock.patch.object(bot, "_answer_and_reply", side_effect=fake_answer),
            mock.patch.object(bot, "DEBOUNCE_SECONDS", 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def context(self):
        context = mock.Mock()
        context.bot_data = {"state": self.state}
        context.bot.id = BOT_ID
        context.bot.username = BOT_USERNAME
        return context

    def deliver(self, handler, msg):
        update = mock.Mock()
        update.message = mock.Mock(wraps=msg)
        update.message.text = msg.text
        update.message.reply_text = mock.AsyncMock()
        update.effective_chat = msg.chat
        update.effective_user = msg.from_user

        async def run():
            await handler(update, self.context())
            await asyncio.sleep(0.05)  # let the debounce task fire

        asyncio.run(run())
        return update.message.reply_text

    def deliver_message(self, msg):
        # handle_message needs the real Message for extract_question.
        update = mock.Mock()
        update.message = msg
        update.effective_chat = msg.chat
        update.effective_user = msg.from_user
        replies = mock.AsyncMock()

        async def run():
            with mock.patch.object(Message, "reply_text", replies):
                await bot.handle_message(update, self.context())
                await asyncio.sleep(0.05)

        asyncio.run(run())
        return replies

    def test_group_chatter_never_reaches_anthropic(self):
        for text in ("morning all", "anyone seen the roster?", "thanks!"):
            self.deliver_message(message(text))
        self.assertEqual(self.answered, [])

    def test_mention_in_group_is_answered(self):
        text = "@deskmate_test_2_bot How long is probation?"
        self.deliver_message(message(text, entities=[mention(text, "@deskmate_test_2_bot")]))
        self.assertEqual(self.answered, ["How long is probation?"])

    def test_reply_in_group_is_answered(self):
        self.deliver_message(message("And for casuals?", reply_to=bot_message()))
        self.assertEqual(self.answered, ["And for casuals?"])

    def test_bare_mention_gets_a_hint_not_an_api_call(self):
        text = "@deskmate_test_2_bot"
        replies = self.deliver_message(message(text, entities=[mention(text, text)]))
        self.assertEqual(self.answered, [])
        self.assertEqual(replies.call_args.args[0], bot.GROUP_EMPTY_QUESTION_MESSAGE)
        self.assertIn("/ask", bot.GROUP_EMPTY_QUESTION_MESSAGE)

    def test_reply_to_a_remembered_answer_carries_the_exchange(self):
        answer = bot_message("Answer: Probation is 6 months.\nSource: employee-handbook.md")
        self.state.answer_questions[(GROUP.id, answer.message_id)] = "How long is probation?"
        self.deliver_message(message("And for casuals?", reply_to=answer))
        self.assertEqual(self.answered, ["And for casuals?"])
        self.assertEqual(self.previous[0].question, "How long is probation?")
        self.assertTrue(self.previous[0].answer.startswith("Answer: Probation is 6 months."))

    def test_reply_to_an_answer_from_before_a_restart_keeps_the_answer_text(self):
        answer = bot_message("Answer: Probation is 6 months.\nSource: employee-handbook.md")
        self.deliver_message(message("And for casuals?", reply_to=answer))
        self.assertIsNone(self.previous[0].question)
        self.assertTrue(self.previous[0].answer.startswith("Answer:"))

    def test_reply_to_a_greeting_or_error_is_a_fresh_question(self):
        for text in ("Hi, I'm Test Co.'s policy assistant.", "I could not reach the language model."):
            self.previous.clear()
            self.deliver_message(message("How long is probation?", reply_to=bot_message(text)))
            self.assertEqual(self.previous, [None])

    def test_group_messages_log_the_trigger_but_never_the_text(self):
        with self.assertLogs("deskmate.bot", level="INFO") as logs:
            self.deliver_message(message("secret salary chat"))
            self.deliver_message(message("And for casuals?", reply_to=bot_message()))
        joined = "\n".join(logs.output)
        self.assertIn("trigger: none", joined)
        self.assertIn("trigger: reply", joined)
        self.assertNotIn("secret salary chat", joined)
        self.assertNotIn("casuals", joined)

    def test_private_chat_unchanged(self):
        self.deliver_message(message("How long is probation?", chat=PRIVATE))
        self.assertEqual(self.answered, ["How long is probation?"])

    def test_ask_command_in_group(self):
        self.deliver(bot.handle_ask, message("/ask@deskmate_test_2_bot How long is probation?"))
        self.assertEqual(self.answered, ["How long is probation?"])

    def test_ask_without_question_asks_for_it_with_a_forced_reply(self):
        # What picking /ask from the menu sends in a group.
        replies = self.deliver(bot.handle_ask, message("/ask@deskmate_test_2_bot"))
        self.assertEqual(self.answered, [])
        self.assertEqual(replies.call_args.args[0], bot.ASK_PROMPT_MESSAGE)
        markup = replies.call_args.kwargs["reply_markup"]
        self.assertTrue(markup.force_reply)
        self.assertTrue(markup.selective)
        self.assertTrue(replies.call_args.kwargs["do_quote"])

    def test_ask_prompt_explains_how_to_reply(self):
        self.assertIn("Reply to this message", bot.ASK_PROMPT_MESSAGE)
        self.assertIn("tap and hold", bot.ASK_PROMPT_MESSAGE)

    def test_reply_to_the_ask_prompt_is_a_fresh_question(self):
        replies = self.deliver(bot.handle_ask, message("/ask@deskmate_test_2_bot"))
        prompt = bot_message(bot.ASK_PROMPT_MESSAGE)
        # Record the prompt the way handle_ask does with a real message id.
        self.state.question_prompts[(GROUP.id, prompt.message_id)] = None
        self.deliver_message(message("How long is probation?", reply_to=prompt))
        self.assertEqual(self.answered, ["How long is probation?"])
        self.assertEqual(self.previous, [None])
        self.assertTrue(replies.await_count)

    def test_ask_respects_allowed_chats(self):
        self.state.config.allowed_chat_ids = frozenset({-42})
        self.deliver(bot.handle_ask, message("/ask How long is probation?"))
        self.assertEqual(self.answered, [])


class AnswerRecordingTests(unittest.TestCase):
    def run_reply_flow(self, answer_text, answer_side_effect=None):
        state = mock.Mock()
        state.corpus = [1]
        state.config.admin_user_id = 111
        state.admin_alert_sent_at = {}
        state.answer_questions = __import__("collections").OrderedDict()
        state.answer_engine.answer = mock.AsyncMock(return_value=answer_text, side_effect=answer_side_effect)
        context = mock.Mock()
        context.bot.send_chat_action = mock.AsyncMock()
        context.bot.send_message = mock.AsyncMock(return_value=mock.Mock(message_id=42))
        update = mock.Mock()
        update.effective_chat.id = GROUP.id
        asyncio.run(bot._answer_and_reply(update, context, state, "How long is probation?"))
        return state

    def test_real_answers_are_remembered(self):
        state = self.run_reply_flow("Answer: 6 months.")
        self.assertEqual(state.answer_questions[(GROUP.id, 42)], "How long is probation?")

    def test_failures_are_not_remembered_as_answers(self):
        state = self.run_reply_flow(bot.API_UNREACHABLE_MESSAGE)
        self.assertEqual(dict(state.answer_questions), {})

    def test_memory_is_capped(self):
        store = __import__("collections").OrderedDict()
        for i in range(bot.REMEMBERED_MESSAGES + 10):
            bot._remember(store, (1, i), "q")
        self.assertEqual(len(store), bot.REMEMBERED_MESSAGES)
        self.assertNotIn((1, 0), store)


class GreetingAndMenuTests(unittest.TestCase):
    def test_greeting_explains_group_use_without_mentions(self):
        # Telegram doesn't deliver mentions to a bot with group privacy on
        # (verified on the test bot 2026-09-28), so the greeting mustn't
        # suggest them.
        text = bot.GREETING.format(company_name="Test Co.", bot_username=BOT_USERNAME)
        self.assertNotIn("@", text)
        self.assertIn("choose /ask, then reply to my question with yours", text)
        self.assertIn("reply to one of my answers", text)
        self.assertIn("I only read messages sent directly to me", text)

    def test_menus(self):
        self.assertEqual([c.command for c in bot.STAFF_COMMANDS], ["ask", "docs"])
        self.assertEqual([c.command for c in bot.ADMIN_COMMANDS], ["ask", "docs", "remove", "reset_demo", "doctor"])

    def test_register_commands_survives_failures(self):
        application = mock.Mock()
        application.bot.set_my_commands = mock.AsyncMock(side_effect=RuntimeError("chat not found"))
        asyncio.run(bot._register_commands(application, 111))
        self.assertEqual(application.bot.set_my_commands.await_count, 2)


if __name__ == "__main__":
    unittest.main()
