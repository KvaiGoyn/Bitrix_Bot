"""
Модуль для отправки уведомлений о бронированиях в Bitrix24 через REST API v2.

Использует методы:
- imbot.v2.Bot.register — регистрация бота (идемпотентна)
- imbot.v2.Chat.Message.send — отправка сообщений
"""

import logging
from datetime import datetime
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Маппинг статусов на русские названия
STATUS_NAMES = {
    "ACTIVE": "активная",
    "FINISHED": "завершённая",
    "CANCELED": "отменённая",
    "REDEEMED": "подтверждённая",
    "REDEEMED_AUTO": "подтверждённая (авто)",
}

STATUS_EMOJI = {
    "ACTIVE": "🟢",
    "FINISHED": "⏹️",
    "CANCELED": "❌",
    "REDEEMED": "✅",
    "REDEEMED_AUTO": "✅",
}


class Bitrix24Notifier:
    """Отправляет уведомления о бронированиях в чат Bitrix24."""

    def __init__(
        self,
        webhook_url: str,
        bot_code: str,
        bot_token: str,
        dialog_id: str,
        host_id_to_name: Optional[dict[str, str]] = None,
    ) -> None:
        """
        Args:
            webhook_url: Базовый URL вебхука, например
                         https://portal.bitrix24.ru/rest/1/xxxxx/
            bot_code:    Уникальный код бота в рамках приложения.
            bot_token:   Токен авторизации бота (макс. 40 символов).
            dialog_id:   ID диалога для отправки (chat{id} или {userId}).
            host_id_to_name: Опциональный словарь ID хоста -> название.
        """
        self.webhook_url = webhook_url.rstrip("/")
        self.bot_code = bot_code
        self.bot_token = bot_token
        self.dialog_id = dialog_id
        self.host_id_to_name = host_id_to_name or {}
        self.bot_id: Optional[int] = None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _method_url(self, method: str) -> str:
        return f"{self.webhook_url}/{method}"

    async def _post(
        self, session: aiohttp.ClientSession, method: str, payload: dict
    ) -> dict:
        """Универсальный POST-запрос к REST API."""
        url = self._method_url(method)
        async with session.post(url, json=payload) as resp:
            return await resp.json()

    # ------------------------------------------------------------------
    # Регистрация бота
    # ------------------------------------------------------------------

    async def register_bot(self, session: aiohttp.ClientSession) -> bool:
        """
        Регистрирует чат-бота в Битрикс24.
        Идемпотентно — повторный вызов с тем же code вернёт существующего бота.
        """
        payload = {
            "fields": {
                "code": self.bot_code,
                "botToken": self.bot_token,
                "properties": {
                    "name": "SmartShell Bot",
                    "workPosition": "Уведомления о бронированиях",
                },
                "type": "bot",
                "eventMode": "fetch",
            }
        }
        try:
            data = await self._post(session, "imbot.v2.Bot.register", payload)
            if "error" in data:
                logger.error(
                    "Ошибка регистрации бота Bitrix24: %s — %s",
                    data["error"],
                    data.get("error_description", ""),
                )
                return False

            bot_info = data.get("result", {}).get("bot", {})
            self.bot_id = bot_info.get("id")
            logger.info(
                "Бот Bitrix24 зарегистрирован. ID: %s, code: %s",
                self.bot_id,
                bot_info.get("code"),
            )
            return True
        except Exception as e:
            logger.exception("Исключение при регистрации бота Bitrix24: %s", e)
            return False

    # ------------------------------------------------------------------
    # Отправка сообщений
    # ------------------------------------------------------------------

    async def send_message(
        self,
        session: aiohttp.ClientSession,
        text: str,
        attach: Optional[list] = None,
    ) -> bool:
        """
        Отправляет текстовое сообщение от имени бота в заданный диалог.

        Args:
            session: aiohttp сессия.
            text:    Текст сообщения (до 20 000 символов).
            attach:  Опциональный список вложений (attach-блоки).

        Returns:
            True при успешной отправке.
        """
        if not self.bot_id:
            logger.error("Бот не зарегистрирован — отправка невозможна.")
            return False

        payload = {
            "botId": self.bot_id,
            "botToken": self.bot_token,
            "dialogId": self.dialog_id,
            "fields": {
                "message": text,
                "urlPreview": False,
            },
        }
        if attach:
            payload["fields"]["attach"] = attach

        try:
            data = await self._post(session, "imbot.v2.Chat.Message.send", payload)
            if "error" in data:
                logger.error(
                    "Ошибка отправки сообщения Bitrix24: %s — %s",
                    data["error"],
                    data.get("error_description", ""),
                )
                return False

            msg_id = data.get("result", {}).get("id")
            logger.info("Сообщение отправлено в Bitrix24. ID: %s", msg_id)
            return True
        except Exception as e:
            logger.exception("Исключение при отправке сообщения Bitrix24: %s", e)
            return False

    # ------------------------------------------------------------------
    # Форматирование уведомлений
    # ------------------------------------------------------------------

    @staticmethod
    def _format_dt(dt_str: Optional[str]) -> str:
        if not dt_str:
            return "—"
        try:
            dt = datetime.fromisoformat(dt_str)
            return dt.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return dt_str

    def _resolve_hosts(self, host_ids: list) -> str:
        names = []
        for hid in host_ids:
            name = self.host_id_to_name.get(str(hid), str(hid))
            names.append(name)
        return ", ".join(names) if names else "—"

    def _build_attach_grid(self, booking: dict) -> list:
        """
        Строит attach-блоки для красивого отображения в Bitrix24.
        """
        client_info = booking.get("client") or {}
        client_name = client_info.get("nickname", "Неизвестный клиент")
        phone = client_info.get("phone", "—")
        start_time = self._format_dt(booking.get("from"))
        end_time = self._format_dt(booking.get("to"))
        hosts_str = self._resolve_hosts(booking.get("hosts", []))
        status = booking.get("status", "—")
        status_emoji = STATUS_EMOJI.get(status, "🔄")
        status_name = STATUS_NAMES.get(status, status)
        comment = booking.get("comment")
        by_client = booking.get("byClient", False)
        by_client_str = "Клиент" if by_client else "Администратор"

        grid = [
            {"NAME": "👤 Клиент", "VALUE": client_name, "DISPLAY": "ROW"},
            {"NAME": "📞 Телефон", "VALUE": phone, "DISPLAY": "ROW"},
            {"NAME": "🖥️ Компьютеры", "VALUE": hosts_str, "DISPLAY": "ROW"},
            {"NAME": "📅 Начало", "VALUE": start_time, "DISPLAY": "ROW"},
            {"NAME": "📅 Конец", "VALUE": end_time, "DISPLAY": "ROW"},
            {"NAME": "📌 Статус", "VALUE": f"{status_emoji} {status_name}", "DISPLAY": "ROW"},
            {"NAME": "🔧 Создал", "VALUE": by_client_str, "DISPLAY": "ROW"},
        ]
        if comment:
            grid.append({"NAME": "📝 Комментарий", "VALUE": comment, "DISPLAY": "BLOCK"})

        return [
            {"DELIMITER": {"SIZE": 200, "COLOR": "#29619b"}},
            {"GRID": grid},
        ]

    def format_new_booking(self, booking: dict) -> tuple[str, list]:
        """
        Форматирует уведомление о новой брони.

        Returns:
            (текст сообщения, список attach-блоков)
        """
        bid = booking.get("id", "?")
        text = f"🟢 Новая бронь #{bid}"
        attach = self._build_attach_grid(booking)
        return text, attach

    def format_cancellation(self, booking: dict, old_status: str) -> tuple[str, list]:
        """
        Форматирует уведомление об отмене брони.

        Returns:
            (текст сообщения, список attach-блоков)
        """
        bid = booking.get("id", "?")
        old_status_name = STATUS_NAMES.get(old_status, old_status)
        text = f"❌ Бронь отменена #{bid} (была: {old_status_name})"
        attach = self._build_attach_grid(booking)
        return text, attach

    # ------------------------------------------------------------------
    # Высокоуровневые методы для вызова из main
    # ------------------------------------------------------------------

    async def notify_new_booking(
        self, session: aiohttp.ClientSession, booking: dict
    ) -> bool:
        """Сформировать и отправить уведомление о новой брони."""
        text, attach = self.format_new_booking(booking)
        return await self.send_message(session, text, attach)

    async def notify_cancellation(
        self, session: aiohttp.ClientSession, booking: dict, old_status: str
    ) -> bool:
        """Сформировать и отправить уведомление об отмене брони."""
        text, attach = self.format_cancellation(booking, old_status)
        return await self.send_message(session, text, attach)