import asyncio
import os
import logging
import json
from datetime import datetime
from json import JSONDecodeError
import aiofiles
import aiohttp
from telegram import Bot
from telegram.error import TelegramError
from dotenv import load_dotenv

from bitrix24_notifier import Bitrix24Notifier

load_dotenv()

# ---------- ЗАГРУЗКА ТАБЛИЦЫ СООТВЕТСТВИЯ ----------
HOST_MAP_FILE = "map.json"
try:
    with open(HOST_MAP_FILE, "r", encoding="utf-8") as f:
        HOST_MAP = json.load(f)
    # Создаём обратное отображение ID -> название
    HOST_ID_TO_NAME = {str(v): k for k, v in HOST_MAP.items()}
    logger = logging.getLogger(__name__)
    logger.info("Загружена таблица соответствия хостов из %s (%d записей)",
                HOST_MAP_FILE, len(HOST_MAP))
except FileNotFoundError:
    logging.error("Файл %s не найден. Будут использоваться ID хостов.", HOST_MAP_FILE)
    HOST_MAP = {}
    HOST_ID_TO_NAME = {}
except json.JSONDecodeError as e:
    logging.error("Ошибка чтения JSON из %s: %s", HOST_MAP_FILE, e)
    HOST_MAP = {}
    HOST_ID_TO_NAME = {}

# ---------- НАСТРОЙКИ ----------
SMARTSHELL_LOGIN = os.getenv('SMARTSHELL_LOGIN')
SMARTSHELL_PASSWORD = os.getenv('SMARTSHELL_PASSWORD')
SMARTSHELL_COMPANY_ID = os.getenv('SMARTSHELL_COMPANY_ID')
SMARTSHELL_HOST_IDS = os.getenv('SMARTSHELL_HOST_IDS')
SMARTSHELL_API_URL = os.getenv('SMARTSHELL_API_URL')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '60'))
LAST_ID_FILE = os.getenv('LAST_ID_FILE', 'last_booking_id.txt')
STATUS_HISTORY_FILE = os.getenv('STATUS_HISTORY_FILE', 'status_history.json')

# ---------- BITRIX24 ----------
BITRIX24_WEBHOOK_URL = os.getenv('BITRIX24_WEBHOOK_URL')
BITRIX24_BOT_CODE = os.getenv('BITRIX24_BOT_CODE', 'smartshell_notifier')
BITRIX24_BOT_TOKEN = os.getenv('BITRIX24_BOT_TOKEN')
BITRIX24_DIALOG_ID = os.getenv('BITRIX24_DIALOG_ID')

# Статусы, для которых отправляются уведомления
ALLOWED_STATUSES = {"ACTIVE", "REDEEMED"}

# Маппинг статусов на русские названия
STATUS_NAMES = {
    "ACTIVE": "активная",
    "FINISHED": "завершенная",
    "CANCELED": "отмененная",
    "REDEEMED": "подтвержденная",
    "REDEEMED_AUTO": "подтвержденная (авто)"
}

# ------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class TokenExpiredError(Exception):
    pass


def check_env_vars():
    """Проверяет наличие всех обязательных переменных окружения."""
    required = {
        'SMARTSHELL_LOGIN': SMARTSHELL_LOGIN,
        'SMARTSHELL_PASSWORD': SMARTSHELL_PASSWORD,
        'SMARTSHELL_COMPANY_ID': SMARTSHELL_COMPANY_ID,
        'SMARTSHELL_HOST_IDS': SMARTSHELL_HOST_IDS,
        'SMARTSHELL_API_URL': SMARTSHELL_API_URL,
        'TELEGRAM_BOT_TOKEN': TELEGRAM_BOT_TOKEN,
        'TELEGRAM_CHAT_ID': TELEGRAM_CHAT_ID,
    }
    # Bitrix24 — опционально, проверяем только если указан хотя бы один параметр
    b24_configured = bool(BITRIX24_WEBHOOK_URL and BITRIX24_BOT_TOKEN and BITRIX24_DIALOG_ID)
    if b24_configured:
        logger.info("Bitrix24 уведомления включены (dialog: %s)", BITRIX24_DIALOG_ID)
    else:
        logger.info("Bitrix24 уведомления отключены — не указаны все переменные BITRIX24_*")

    missing = [k for k, v in required.items() if not v]
    if missing:
        raise EnvironmentError(f"Отсутствуют обязательные переменные окружения: {', '.join(missing)}")


async def smartshell_login(session: aiohttp.ClientSession) -> str | None:
    """Авторизация в SmartShell API через GraphQL-переменные."""
    mutation = """
    mutation login($login: String!, $password: String!, $company_id: Int!) {
      login(input: { login: $login, password: $password, company_id: $company_id }) {
        access_token
        token_type
        expires_in
      }
    }
    """
    variables = {
        "login": SMARTSHELL_LOGIN,
        "password": SMARTSHELL_PASSWORD,
        "company_id": int(SMARTSHELL_COMPANY_ID)
    }
    payload = {"query": mutation, "variables": variables}
    headers = {"Content-Type": "application/json"}

    try:
        async with session.post(SMARTSHELL_API_URL, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
            token = data.get("data", {}).get("login", {}).get("access_token")
            if token:
                logger.info("Успешная авторизация в SmartShell")
                return token
            else:
                logger.error("Ошибка авторизации: %s", data)
                return None
    except (aiohttp.ClientError, JSONDecodeError) as e:
        logger.error("Ошибка при авторизации: %s", e)
        return None


async def get_last_booking_id() -> int:
    """Читает последний обработанный ID из файла."""
    try:
        async with aiofiles.open(LAST_ID_FILE, "r") as f:
            content = await f.read()
            return int(content.strip())
    except (FileNotFoundError, ValueError):
        return 0


async def save_last_booking_id(booking_id: int) -> None:
    """Сохраняет последний обработанный ID в файл."""
    async with aiofiles.open(LAST_ID_FILE, "w") as f:
        await f.write(str(booking_id))


async def load_status_history() -> dict:
    """Загружает историю статусов броней из файла."""
    try:
        async with aiofiles.open(STATUS_HISTORY_FILE, "r", encoding="utf-8") as f:
            content = await f.read()
            return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


async def save_status_history(history: dict) -> None:
    """Сохраняет историю статусов броней в файл."""
    async with aiofiles.open(STATUS_HISTORY_FILE, "w", encoding="utf-8") as f:
        await f.write(json.dumps(history, ensure_ascii=False, indent=2))


async def fetch_all_bookings(session: aiohttp.ClientSession, token: str, last_id: int) -> tuple[list[dict], int]:
    """
    Запрашивает все бронирования через пагинацию.
    Возвращает (список всех броней за период, максимальный ID среди них).
    """
    query = """
    query bookingsV2($from: DateTime, $to: DateTime, $page: Int, $first: Int) {
      getBookings(from: $from, to: $to, page: $page, first: $first) {
        data {
          id
          group
          hosts
          byClient
          client {
            uuid
            nickname
            phone
          }
          from
          to
          status
          comment
        }
      }
    }
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }

    all_bookings = []
    page = 1
    per_page = 100
    max_id = last_id

    # Устанавливаем временной период: последние 7 дней и будущие 7 дней
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    date_to = (now + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    while True:
        variables = {
            "from": date_from,
            "to": date_to,
            "page": page,
            "first": per_page
        }
        payload = {"query": query, "variables": variables}

        try:
            async with session.post(SMARTSHELL_API_URL, json=payload, headers=headers, timeout=30) as resp:
                if resp.status == 401:
                    raise TokenExpiredError()
                
                # Читаем тело ответа для диагностики
                response_text = await resp.text()
                
                if resp.status != 200:
                    logger.error("Ошибка HTTP %d при запросе броней. Тело ответа: %s", resp.status, response_text[:500])
                    break
                
                try:
                    data = json.loads(response_text)
                except json.JSONDecodeError as e:
                    logger.error("Ошибка парсинга JSON ответа: %s. Тело: %s", e, response_text[:500])
                    break
                
                # Проверяем GraphQL-ошибки
                if "errors" in data:
                    logger.error("GraphQL ошибка при запросе броней: %s", json.dumps(data["errors"], ensure_ascii=False)[:500])
                    break
                
                bookings = data.get("data", {}).get("getBookings", {}).get("data", [])
                if not bookings:
                    break

                for b in bookings:
                    bid = b.get("id")
                    if bid is not None:
                        all_bookings.append(b)
                        if bid > max_id:
                            max_id = bid

                if len(bookings) < per_page:
                    break
                page += 1

        except TokenExpiredError:
            raise
        except (aiohttp.ClientError, JSONDecodeError) as e:
            logger.error("Ошибка при запросе броней: %s", e)
            break

    # Сортируем по ID, чтобы обрабатывать последовательно
    all_bookings.sort(key=lambda x: x.get("id", 0))
    return all_bookings, max_id


def merge_group_bookings(bookings: list[dict]) -> list[dict]:
    """
    Группирует брони по полю 'group' и объединяет hosts.
    Если group отсутствует — каждая бронь считается отдельной.
    """
    groups: dict[str, dict] = {}
    for b in bookings:
        g = str(b.get("group") or b.get("id", ""))
        if g in groups:
            existing = groups[g]
            # Объединяем hosts без дубликатов
            existing_hosts = existing.get("hosts", [])
            for h in b.get("hosts", []):
                if h not in existing_hosts:
                    existing_hosts.append(h)
            existing["hosts"] = existing_hosts
            # Собираем все ID группы для отображения
            existing_ids = existing.get("_group_ids", [existing.get("id")])
            if b.get("id") not in existing_ids:
                existing_ids.append(b.get("id"))
            existing["_group_ids"] = existing_ids
        else:
            groups[g] = dict(b)
            groups[g]["_group_ids"] = [b.get("id")]
    return list(groups.values())


def format_booking(booking: dict, html: bool = False) -> str:
    """Форматирует информацию о бронировании для лога или Telegram."""
    group_ids = booking.get("_group_ids")
    if group_ids and len(group_ids) > 1:
        bid = f"{group_ids[0]}-{group_ids[-1]}"
    else:
        bid = booking.get("id", "?")
    client_info = booking.get("client") or {}
    client_name = client_info.get("nickname", "Неизвестный клиент")
    phone = client_info.get("phone", "—")
    start_time = booking.get("from")
    end_time = booking.get("to")

    def format_dt(dt_str):
        if not dt_str:
            return "—"
        try:
            dt = datetime.fromisoformat(dt_str)
            return dt.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return dt_str

    host_ids = booking.get("hosts", [])
    # Преобразуем ID хостов в названия из таблицы соответствия
    host_names = []
    for host_id in host_ids:
        host_id_str = str(host_id)
        name = HOST_ID_TO_NAME.get(host_id_str, host_id_str)  # если нет в таблице, оставляем ID
        host_names.append(name)
    hosts_str = ", ".join(host_names) if host_names else "—"
    status = booking.get("status", "—")
    status_emoji = {
        "ACTIVE": "🟢",
        "FINISHED": "⏹️",
        "CANCELED": "❌",
        "REDEEMED": "✅",
        "REDEEMED_AUTO": "✅"
    }
    emoji = status_emoji.get(status, "🔄")
    status_name = STATUS_NAMES.get(status, status)  # русское название или оригинал
    comment = booking.get("comment")
    comment_str = f"\n📝 Комментарий: {comment}" if comment else ""
    by_client = booking.get("byClient")
    by_client_str = "✅ Клиент сам создал бронь" if by_client else "❌ Ручная бронь"

    if html:
        message = (
            f"🟢 Новая бронь #{bid}\n"
            f"👤 Клиент: {client_name}\n"
            f"📞 Телефон: {phone}\n"
            f"🖥️ Компьютеры: {hosts_str}\n"
            f"📅 С {format_dt(start_time)} до {format_dt(end_time)}\n"
            f"📌 Статус: {emoji} {status_name}\n"
            f"🔧 {by_client_str}"
            f"{comment_str}"
        )
    else:
        message = (
            f"🟢 Новая бронь #{bid}\n"
            f"👤 Клиент: {client_name}\n"
            f"📞 Телефон: {phone}\n"
            f"🖥️ Компьютеры: {hosts_str}\n"
            f"📅 С {format_dt(start_time)} до {format_dt(end_time)}\n"
            f"📌 Статус: {emoji} {status_name}\n"
            f"🔧 {by_client_str}"
            f"{comment_str}"
        )
    return message


async def send_telegram_notification(bot: Bot, booking: dict) -> bool:
    """Отправляет уведомление в Telegram. Возвращает True в случае успеха."""
    message = format_booking(booking, html=True)
    group_ids = booking.get("_group_ids", [booking.get("id")])
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="HTML"
        )
        logger.info("Отправлено уведомление о брони №%s (ПК: %d)", group_ids[0], len(booking.get("hosts", [])))
        return True
    except TelegramError as e:
        logger.error("Ошибка отправки в Telegram: %s", e)
        return False


def format_cancellation_notification(booking: dict, old_status: str, html: bool = False) -> str:
    """Форматирует уведомление об отмене брони."""
    group_ids = booking.get("_group_ids")
    if group_ids and len(group_ids) > 1:
        bid = f"{group_ids[0]}-{group_ids[-1]}"
    else:
        bid = booking.get("id", "?")
    client_info = booking.get("client") or {}
    client_name = client_info.get("nickname", "Неизвестный клиент")
    phone = client_info.get("phone", "—")
    start_time = booking.get("from")
    end_time = booking.get("to")

    def format_dt(dt_str):
        if not dt_str:
            return "—"
        try:
            dt = datetime.fromisoformat(dt_str)
            return dt.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return dt_str

    host_ids = booking.get("hosts", [])
    host_names = []
    for host_id in host_ids:
        host_id_str = str(host_id)
        name = HOST_ID_TO_NAME.get(host_id_str, host_id_str)
        host_names.append(name)
    hosts_str = ", ".join(host_names) if host_names else "—"
    
    old_status_name = STATUS_NAMES.get(old_status, old_status)
    new_status_name = STATUS_NAMES.get("CANCELED", "CANCELED")
    
    by_client = booking.get("byClient")
    by_client_str = "✅ Клиент сам создал бронь" if by_client else "❌ Ручная бронь"
    comment = booking.get("comment")
    comment_str = f"\n📝 Комментарий: {comment}" if comment else ""

    if html:
        message = (
            f"❌ Бронь отменена #{bid}\n"
            f"👤 Клиент: {client_name}\n"
            f"📞 Телефон: {phone}\n"
            f"🖥️ Компьютеры: {hosts_str}\n"
            f"📅 С {format_dt(start_time)} до {format_dt(end_time)}\n"
            f"📌 Статус изменился: {old_status_name} → ❌ {new_status_name}\n"
            f"🔧 {by_client_str}"
            f"{comment_str}"
        )
    else:
        message = (
            f"❌ Бронь отменена #{bid}\n"
            f"👤 Клиент: {client_name}\n"
            f"📞 Телефон: {phone}\n"
            f"🖥️ Компьютеры: {hosts_str}\n"
            f"📅 С {format_dt(start_time)} до {format_dt(end_time)}\n"
            f"📌 Статус изменился: {old_status_name} → ❌ {new_status_name}\n"
            f"🔧 {by_client_str}"
            f"{comment_str}"
        )
    return message


async def send_cancellation_notification(bot: Bot, booking: dict, old_status: str) -> bool:
    """Отправляет уведомление об отмене брони в Telegram."""
    message = format_cancellation_notification(booking, old_status, html=True)
    group_ids = booking.get("_group_ids", [booking.get("id")])
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="HTML"
        )
        logger.info("Отправлено уведомление об отмене брони №%s", group_ids[0])
        return True
    except TelegramError as e:
        logger.error("Ошибка отправки уведомления об отмене в Telegram: %s", e)
        return False


async def main():
    check_env_vars()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    # Инициализация Bitrix24 (опционально)
    b24_configured = bool(BITRIX24_WEBHOOK_URL and BITRIX24_BOT_TOKEN and BITRIX24_DIALOG_ID)
    b24: Bitrix24Notifier | None = None
    if b24_configured:
        b24 = Bitrix24Notifier(
            webhook_url=BITRIX24_WEBHOOK_URL,
            bot_code=BITRIX24_BOT_CODE,
            bot_token=BITRIX24_BOT_TOKEN,
            dialog_id=BITRIX24_DIALOG_ID,
            host_id_to_name=HOST_ID_TO_NAME,
        )

    async with aiohttp.ClientSession() as session:
        token = await smartshell_login(session)
        if not token:
            logger.critical("Не удалось авторизоваться. Бот завершает работу.")
            return

        # Регистрация бота Bitrix24 при старте
        if b24:
            b24_registered = await b24.register_bot(session)
            if not b24_registered:
                logger.warning("Не удалось зарегистрировать бота Bitrix24. Уведомления в B24 отключены.")
                b24 = None  # отключаем B24, чтобы не сыпать ошибками

        last_id = await get_last_booking_id()
        status_history = await load_status_history()
        logger.info("Бот запущен. Последний обработанный ID: %d. Интервал: %d сек.", last_id, CHECK_INTERVAL)

        # Защита от повторной отправки внутри одной сессии
        session_notified_ids = set()
        failed_attempts = 0

        while True:
            try:
                all_bookings, max_id = await fetch_all_bookings(session, token, last_id)

                if not all_bookings:
                    logger.info("Новых броней (ID > %d) нет", last_id)
                else:
                    # Группируем брони по group (один заказ на несколько ПК)
                    grouped_bookings = merge_group_bookings(all_bookings)

                    # Обрабатываем брони строго по порядку
                    last_processed_id = last_id
                    for booking in grouped_bookings:
                        group_ids = booking.get("_group_ids", [booking.get("id")])
                        first_id = group_ids[0]
                        status = booking.get("status")

                        # Проверяем статус по первому ID группы
                        old_status = status_history.get(str(first_id))

                        # Логируем всегда
                        log_text = format_booking(booking, html=False)
                        if status in ALLOWED_STATUSES:
                            logger.info("Найдена АКТИВНАЯ бронь ID %s (ПК: %d):\n%s", group_ids, len(booking.get("hosts", [])), log_text)
                        else:
                            logger.info("Найдена НЕАКТИВНАЯ бронь ID %s:\n%s", group_ids, log_text)

                        # Отслеживание изменений статуса (по первому ID группы)
                        if old_status is not None and old_status != status:
                            logger.info("Статус брони ID %s изменился: %s → %s", group_ids, old_status, status)
                            # Если статус изменился на CANCELED, отправляем уведомление об отмене
                            if status == "CANCELED":
                                # Проверяем, что бронь создана клиентом
                                if not booking.get("byClient"):
                                    logger.info("Пропускаем уведомление об отмене для ID %s (byClient=false)", group_ids)
                                else:
                                    logger.info("Бронь ID %s отменена, отправляем уведомление об отмене", group_ids)
                                    # Telegram
                                    success = await send_cancellation_notification(bot, booking, old_status)
                                    if not success:
                                        logger.warning("Уведомление об отмене (TG) для ID %s не отправлено", group_ids)
                                    # Bitrix24
                                    if b24:
                                        b24_ok = await b24.notify_cancellation(session, booking, old_status)
                                        if not b24_ok:
                                            logger.warning("Уведомление об отмене (B24) для ID %s не отправлено", group_ids)

                        # Отправляем уведомление для разрешённых статусов, только если бронь создана клиентом
                        if status in ALLOWED_STATUSES:
                            # Проверяем byClient
                            if not booking.get("byClient"):
                                logger.info("Пропускаем уведомление для ID %s (byClient=false)", group_ids)
                            elif first_id not in session_notified_ids:
                                # Telegram
                                success = await send_telegram_notification(bot, booking)
                                if not success:
                                    logger.warning("Уведомление (TG) для ID %s не отправлено. Остановка пачки.", group_ids)
                                    break
                                # Bitrix24
                                if b24:
                                    b24_ok = await b24.notify_new_booking(session, booking)
                                    if not b24_ok:
                                        logger.warning("Уведомление (B24) для ID %s не отправлено", group_ids)

                                session_notified_ids.add(first_id)
                            else:
                                logger.info("Уведомление для ID %s уже было отправлено ранее в этой сессии, пропускаем", group_ids)

                        # Обновляем историю статусов для всех ID в группе
                        for gid in group_ids:
                            status_history[str(gid)] = status

                        # Бронь успешно обработана
                        last_processed_id = max(group_ids)

                    # Обновляем last_id до последнего успешно обработанного ID
                    if last_processed_id > last_id:
                        last_id = last_processed_id
                        await save_last_booking_id(last_id)
                        logger.info("Обновлён last_id: %d", last_id)

                    # Сохраняем историю статусов после обработки всех броней
                    await save_status_history(status_history)

                failed_attempts = 0

            except TokenExpiredError:
                logger.info("Токен истёк, переавторизация...")
                token = await smartshell_login(session)
                if not token:
                    logger.warning("Переавторизация не удалась, ждём 30 сек.")
                    await asyncio.sleep(30)
                else:
                    failed_attempts = 0
                continue

            except Exception as e:
                logger.exception("Необработанная ошибка в цикле: %s", e)
                failed_attempts += 1
                if failed_attempts >= 3:
                    logger.warning("Слишком много ошибок. Переавторизация.")
                    token = await smartshell_login(session)
                    if not token:
                        logger.warning("Переавторизация не удалась, ждём 30 сек.")
                        await asyncio.sleep(30)
                    failed_attempts = 0

            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")