from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import httpx

import config


@dataclass
class SendResult:
    ok: bool
    message_id: int | None = None
    error: str | None = None
    status_code: int | None = None
    retryable: bool = False
    retry_after: float | None = None


class TelegramSenderError(Exception):
    """Ошибка отправки в Telegram (без утечки token)."""


class MessageSender(Protocol):
    def send_message(self, text: str) -> SendResult: ...


def _parse_retry_after(data: dict[str, Any] | None, headers: Any = None) -> float | None:
    if isinstance(data, dict):
        params = data.get("parameters") or {}
        if isinstance(params, dict) and params.get("retry_after") is not None:
            try:
                return float(params["retry_after"])
            except (TypeError, ValueError):
                pass
        # Sometimes description embeds seconds; prefer parameters.
    if headers is not None:
        raw = headers.get("Retry-After") if hasattr(headers, "get") else None
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
    return None


class TelegramSender:
    """Тонкий клиент Telegram Bot API (sendMessage) через httpx."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        min_interval_seconds: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not bot_token or not chat_id:
            raise ValueError("bot_token and chat_id are required")
        self._token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self._min_interval = (
            float(config.TELEGRAM_MIN_SEND_INTERVAL_SECONDS)
            if min_interval_seconds is None
            else float(min_interval_seconds)
        )
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_send_at: float | None = None

    @property
    def api_url(self) -> str:
        # Token только внутри URL клиента; наружу не логируем.
        return f"https://api.telegram.org/bot{self._token}/sendMessage"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> TelegramSender:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _pace(self) -> None:
        if self._last_send_at is None:
            return
        elapsed = self._monotonic() - self._last_send_at
        remaining = self._min_interval - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def send_message(
        self,
        text: str,
        *,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = False,
    ) -> SendResult:
        """
        Одна попытка sendMessage.

        На 429 возвращает retry_after; sleep/retry делает вызывающий код
        (deliver), чтобы attempts учитывались в delivery state.
        Pacing между вызовами — здесь.
        """
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        self._pace()
        result = self._do_post(payload)
        self._last_send_at = self._monotonic()
        return result

    def _do_post(self, payload: dict[str, Any]) -> SendResult:
        try:
            response = self._client.post(self.api_url, json=payload)
        except httpx.TimeoutException as exc:
            return SendResult(
                ok=False,
                error=f"Telegram timeout: {exc.__class__.__name__}",
                retryable=True,
            )
        except httpx.HTTPError as exc:
            return SendResult(
                ok=False,
                error=f"Telegram HTTP error: {exc.__class__.__name__}",
                retryable=True,
            )

        status = response.status_code
        data: dict[str, Any] | None
        try:
            parsed = response.json()
            data = parsed if isinstance(parsed, dict) else None
        except ValueError:
            data = None

        if status == 429 or (data and data.get("error_code") == 429):
            retry_after = _parse_retry_after(data, response.headers)
            description = (
                (data or {}).get("description")
                if data
                else "Telegram rate limit (429)"
            )
            return SendResult(
                ok=False,
                error=f"Telegram rate limit (429): {description}",
                status_code=429,
                retryable=True,
                retry_after=retry_after,
            )

        if data is None:
            return SendResult(
                ok=False,
                error=f"Telegram invalid JSON (HTTP {status})",
                status_code=status,
                retryable=status >= 500,
            )

        if not data.get("ok"):
            description = data.get("description") or "unknown Telegram error"
            err_code = data.get("error_code")
            retry_after = _parse_retry_after(data, response.headers)
            return SendResult(
                ok=False,
                error=f"Telegram API error: {description}",
                status_code=int(err_code) if err_code is not None else status,
                retryable=status >= 500 or err_code == 429,
                retry_after=retry_after,
            )

        result = data.get("result") or {}
        message_id = result.get("message_id")
        return SendResult(
            ok=True,
            message_id=int(message_id) if message_id is not None else None,
            status_code=status,
        )


class FakeTelegramSender:
    """Тестовый transport: ничего не шлёт в сеть."""

    def __init__(
        self,
        *,
        fail_times: int = 0,
        error: str = "fake failure",
        status_code: int | None = None,
        retry_after: float | None = None,
        responses: list[SendResult] | None = None,
    ) -> None:
        self.fail_times = fail_times
        self.error = error
        self.status_code = status_code
        self.retry_after = retry_after
        self.responses = list(responses) if responses is not None else None
        self.sent: list[str] = []
        self.calls = 0
        self.call_times: list[float] = []

    def send_message(self, text: str) -> SendResult:
        self.calls += 1
        self.call_times.append(time.monotonic())
        if self.responses is not None:
            idx = self.calls - 1
            if idx < len(self.responses):
                result = self.responses[idx]
                if result.ok:
                    self.sent.append(text)
                return result
            self.sent.append(text)
            return SendResult(ok=True, message_id=1000 + self.calls)
        if self.calls <= self.fail_times:
            return SendResult(
                ok=False,
                error=self.error,
                status_code=self.status_code,
                retryable=True,
                retry_after=self.retry_after,
            )
        self.sent.append(text)
        return SendResult(ok=True, message_id=1000 + self.calls)
