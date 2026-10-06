from __future__ import annotations

import unittest
from unittest.mock import patch

from control_bot import (
    BOT_COMMANDS,
    BTN_HISTORY,
    BTN_MARKETS,
    BTN_RECOMMEND,
    BTN_RUN,
    BTN_STATUS,
    BTN_TOP,
    BTN_VERSION,
    format_help_text,
    parse_bot_command,
    process_update,
)


def _message(text: str, chat_id: int = 1) -> dict:
    return {"message": {"chat": {"id": chat_id}, "text": text}}


class ParserTests(unittest.TestCase):
    def test_bot_username_suffix(self) -> None:
        self.assertEqual(parse_bot_command("/top@LaptopMonitorBot"), ("top", ""))

    def test_spaces_and_case(self) -> None:
        self.assertEqual(parse_bot_command("  /TOP  "), ("top", ""))
        self.assertEqual(parse_bot_command("/recommend@Bot extra"), ("recommend", "extra"))

    def test_plain_text_is_not_a_command(self) -> None:
        self.assertIsNone(parse_bot_command("привет"))
        self.assertIsNone(parse_bot_command(""))


class CatalogTests(unittest.TestCase):
    def test_names_are_unique_lowercase_and_described(self) -> None:
        names = [name for name, _desc in BOT_COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        for name, description in BOT_COMMANDS:
            self.assertEqual(name, name.lower())
            self.assertNotIn("/", name)
            self.assertTrue(description.strip())

    def test_help_comes_from_the_same_list(self) -> None:
        text = format_help_text()
        self.assertIn("Laptop Monitor — команды", text)
        for name, description in BOT_COMMANDS:
            self.assertIn(f"/{name} —", text)
            self.assertIn(description[:1].lower() + description[1:], text)


class DispatchTests(unittest.TestCase):
    def _run(self, text: str, **patches):
        sent: list[str] = []

        def _send(_c, _t, _chat, body, **_kwargs):
            sent.append(body)
            return True

        with patch("control_bot._is_admin", return_value=True), patch(
            "control_bot.send_message", side_effect=_send
        ):
            ctx = {key: patch(target) for key, target in patches.items()}
            managers = {key: ctx[key].__enter__() for key in ctx}
            try:
                process_update(object(), "token", _message(text))
            finally:
                for item in ctx.values():
                    item.__exit__(None, None, None)
            return sent, managers

    def test_start_and_menu_open_the_main_menu(self) -> None:
        for text, expected in (("/start", "Выберите действие:"), ("/menu", "Главное меню:")):
            sent, _ = self._run(text)
            self.assertTrue(sent)
            self.assertIn(expected, sent[0])

    def test_help_lists_commands(self) -> None:
        sent, _ = self._run("/help")
        self.assertIn("/recommend —", sent[0])
        self.assertIn("/run —", sent[0])

    def test_run_uses_existing_handler(self) -> None:
        with patch("control_bot._is_admin", return_value=True), patch(
            "control_bot.handle_run"
        ) as run:
            process_update(object(), "token", _message("/run"))
        run.assert_called_once()

    def test_top_uses_existing_top_text(self) -> None:
        with patch("control_bot._is_admin", return_value=True), patch(
            "control_bot.build_top_text", return_value="TOP-BODY"
        ) as top, patch("control_bot.send_message") as send:
            process_update(object(), "token", _message("/top@LaptopMonitorBot"))
        top.assert_called_once()
        self.assertEqual(send.call_args.args[3], "TOP-BODY")

    def test_history_uses_existing_handler(self) -> None:
        with patch("control_bot._is_admin", return_value=True), patch(
            "control_bot.handle_history_list"
        ) as history:
            process_update(object(), "token", _message(" /History "))
        history.assert_called_once()

    def test_recommend_opens_selector_without_enqueue(self) -> None:
        with patch("control_bot._is_admin", return_value=True), patch(
            "control_bot.handle_recommendation_menu"
        ) as menu, patch("buy_thailand_flow.enqueue_recommendation_job") as enq:
            process_update(object(), "token", _message("/recommend"))
        menu.assert_called_once()
        enq.assert_not_called()

    def test_markets_opens_menu_without_scan(self) -> None:
        with patch("control_bot._is_admin", return_value=True), patch(
            "control_bot.handle_markets_menu"
        ) as menu, patch("thailand.scanner.run_thailand_scan") as scan:
            process_update(object(), "token", _message("/markets"))
        menu.assert_called_once()
        scan.assert_not_called()

    def test_status_and_version_use_current_text(self) -> None:
        with patch("control_bot._is_admin", return_value=True), patch(
            "control_bot.build_status_text", return_value="STATUS-BODY"
        ), patch("control_bot.send_message") as send:
            process_update(object(), "token", _message("/status"))
        self.assertEqual(send.call_args.args[3], "STATUS-BODY")
        with patch("control_bot._is_admin", return_value=True), patch(
            "control_bot.send_message"
        ) as send:
            process_update(object(), "token", _message("/version"))
        self.assertIn("Laptop Monitor", send.call_args.args[3])

    def test_unknown_slash_hints_help(self) -> None:
        sent, _ = self._run("/foo")
        self.assertIn("Неизвестная команда", sent[0])
        self.assertIn("/help", sent[0])

    def test_ordinary_text_is_silent(self) -> None:
        with patch("control_bot._is_admin", return_value=True), patch(
            "control_bot.send_message"
        ) as send:
            process_update(object(), "token", _message("просто текст"))
        send.assert_not_called()

    def test_non_admin_cannot_run_or_read(self) -> None:
        with patch("control_bot._is_admin", return_value=False), patch(
            "control_bot.handle_run"
        ) as run, patch("control_bot.send_message") as send, patch(
            "control_bot.handle_recommendation_menu"
        ) as rec:
            process_update(object(), "token", _message("/run", chat_id=99))
            process_update(object(), "token", _message("/top", chat_id=99))
            process_update(object(), "token", _message("/recommend", chat_id=99))
        run.assert_not_called()
        send.assert_not_called()
        rec.assert_not_called()

    def test_inline_callback_names_unchanged(self) -> None:
        self.assertEqual(BTN_RUN, "ctrl:run")
        self.assertEqual(BTN_TOP, "ctrl:top")
        self.assertEqual(BTN_HISTORY, "ctrl:hist")
        self.assertEqual(BTN_RECOMMEND, "ctrl:recommend")
        self.assertEqual(BTN_MARKETS, "ctrl:markets")
        self.assertEqual(BTN_STATUS, "ctrl:status")
        self.assertEqual(BTN_VERSION, "ctrl:version")
