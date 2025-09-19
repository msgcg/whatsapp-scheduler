# --- START OF FILE bot.py ---
import re
import glob
import asyncio
import os
import shlex
import functools
import logging
import io
import datetime
import time
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Document
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)
from playwright.async_api import async_playwright, Page, TimeoutError

# --- CONFIGURATION ---
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

SUPPORT_REQUEST_INTERVAL = 5  # Запрашивать поддержку каждые команд
SUPPORT_URL = "https://rest-check.onrender.com/"

PLAYWRIGHT_STATE_DIR = "playwright_states"
os.makedirs(PLAYWRIGHT_STATE_DIR, exist_ok=True)

def get_user_state_path(user_id: int) -> str:
    return os.path.join(PLAYWRIGHT_STATE_DIR, f"{user_id}.json")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG #замените на DEBUG, чтобы увидеть все сообщения
)
logger = logging.getLogger(__name__)


# --- НОВЫЙ БЛОК: ЛОГИКА ЗАПРОСА ПОДДЕРЖКИ ---

async def check_and_request_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Проверяет, нужно ли запросить поддержку. Если да, отправляет сообщение и возвращает True.
    """
    user_id = update.effective_user.id
    request_count = context.user_data.get('request_count', 0)

    if request_count >= SUPPORT_REQUEST_INTERVAL:
        keyboard = [
            [
                InlineKeyboardButton("🔗 Перейти на сайт", url=SUPPORT_URL),
            ],
            [
                InlineKeyboardButton("✅ Я перешел и поддерживаю проект", callback_data="reset_support_counter"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.effective_message.reply_text(
            "🙏 **Пожалуйста, поддержите проект!**\n\n"
            "Чтобы я мог и дальше работать, пожалуйста, перейдите на сайт нашего партнера. "
            "Там вы найдете полезный инструмент для разделения счета в ресторане и посмотрите рекламу, которая помогает мне существовать.\n\n"
            "Нажмите кнопку ниже, когда будете готовы продолжить.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return True  # Блокируем выполнение команды
    return False # Разрешаем выполнение команды

async def reset_support_counter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Сбрасывает счетчик после того, как пользователь подтвердил переход.
    """
    query = update.callback_query
    await query.answer()
    
    context.user_data['request_count'] = 0
    logger.info(f"Счетчик сброшен для пользователя {update.effective_user.id}")
    
    await query.edit_message_text("😊 **Спасибо за вашу поддержку!**\n\nВы снова можете использовать команды.")

def command_wrapper(func):
    """Декоратор для увеличения счетчика и проверки лимита."""
    @functools.wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if await check_and_request_support(update, context):
            return
        
        # Выполняем команду
        await func(update, context, *args, **kwargs)
        
        # Увеличиваем счетчик только после успешного вызова
        context.user_data['request_count'] = context.user_data.get('request_count', 0) + 1
        logger.info(f"Счетчик команд для {update.effective_user.id} увеличен до {context.user_data['request_count']}")

    return wrapped


# Функция для дебага:
async def take_screenshot(page: Page, name: str):
     if not page or page.is_closed():
         logger.warning(f"Не удалось сделать скриншот '{name}': страница закрыта.")
         return
     try:
         screenshots_dir = "debug_screenshots"
         os.makedirs(screenshots_dir, exist_ok=True)
         timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
         safe_name = "".join(c for c in name if c.isalnum() or c in ('_', '-')).rstrip()
         path = os.path.join(screenshots_dir, f"{safe_name}_{timestamp}.png")
         await page.screenshot(path=path)
         logger.info(f"Скриншот для отладки сохранен: {path}")
     except Exception as e:
         logger.error(f"Не удалось сохранить скриншот '{name}': {e}")


async def get_whatsapp_page(context: ContextTypes.DEFAULT_TYPE, user_id: int, force_new: bool = False, force_no_locale: bool = False) -> Page | None:
    user_data = context.user_data

    # Флаг для определения, нужна ли полная переинициализация Playwright
    needs_full_reinitialization = force_new or \
                                  'browser' not in user_data or \
                                  not user_data['browser'].is_connected() or \
                                  'playwright_context' not in user_data or \
                                  user_data['playwright_context'] is None

    if needs_full_reinitialization:
        if 'browser' in user_data and user_data['browser'].is_connected():
            logger.info("Закрытие существующего экземпляра браузера для пользователя %s перед полной переинициализацией.", user_id)
            await user_data['browser'].close()
        
        logger.info("Полная инициализация нового экземпляра Playwright для пользователя %s...", user_id)
        # Очищаем все связанные с Playwright данные пользователя
        for key in ['browser', 'playwright_context', 'whatsapp_page']:
            user_data.pop(key, None)

        try:
            p = await async_playwright().start()
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-blink-features=AutomationControlled'])
            user_data['browser'] = browser
            
            user_state_path = get_user_state_path(user_id)
            user_state_exists = os.path.exists(user_state_path)

            context_options = {
                "storage_state": user_state_path if user_state_exists else None,
                "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }

            # --- ИЗМЕНЕННАЯ ЛОГИКА ---
            # Устанавливаем локаль, только если сессия существует И НЕТ флага принудительного отключения
            if user_state_exists and not force_no_locale:
                logger.info(f"Найден файл сессии для {user_id}. Установка локали 'ru-RU'.")
                context_options["locale"] = "ru-RU"
            else:
                if force_no_locale:
                    logger.warning(f"Принудительный запуск без локали для пользователя {user_id}.")
                else:
                    logger.info(f"Файл сессии для {user_id} не найден. Контекст создается без локали.")

            pw_context = await browser.new_context(**context_options)
            user_data['playwright_context'] = pw_context

            page = await pw_context.new_page()
            user_data['whatsapp_page'] = page
            return page
        except Exception as e:
            logger.error(f"Не удалось запустить Playwright для пользователя {user_id}: {e}")
            return None
    else:
        # Этот блок остается без изменений
        pw_context = user_data['playwright_context']
        page = user_data.get('whatsapp_page')

        if page and not page.is_closed():
            logger.info("Используется существующая страница WhatsApp для пользователя %s.", user_id)
            return page
        
        logger.info("Страница закрыта для пользователя %s, создаем новую.", user_id)
        try:
            page = await pw_context.new_page()
            user_data['whatsapp_page'] = page
            return page
        except Exception as e:
            logger.error(f"Не удалось создать новую страницу Playwright для пользователя {user_id} в существующем контексте: {e}")
            logger.info("Попытка полной переинициализации Playwright для пользователя %s из-за ошибки создания страницы.", user_id)
            return await get_whatsapp_page(context, user_id, force_new=True)

async def smart_check_and_get_page(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> Page | None:
    """
    "Умная" функция, которая сначала пытается получить страницу с русской локалью (если есть сессия),
    а в случае неудачи (не найден элемент чатов), перезапускает браузер без локали.
    """
    # --- Попытка №1: Оптимистичная, с локалью ---
    logger.info(f"Умная проверка для {user_id}: Попытка №1 (с локалью, если есть сессия).")
    page = await get_whatsapp_page(context, user_id)
    if not page:
        return None # Если браузер вообще не запустился

    try:
        await page.goto("https://web.whatsapp.com/", timeout=45000)
        if await check_login_status(page):
            logger.info(f"Умная проверка для {user_id}: Попытка №1 успешна, сессия активна.")
            return page # Все хорошо, сессия активна, возвращаем страницу
    except TimeoutError:
        logger.warning(f"Умная проверка для {user_id}: Не удалось загрузить страницу за 45 секунд.")
    except Exception as e:
        logger.error(f"Умная проверка для {user_id}: Ошибка на Попытке №1: {e}")


    # --- Попытка №2: Восстановление, без локали ---
    logger.warning(f"Умная проверка для {user_id}: Попытка №1 не удалась. Запускаю Попытку №2 (принудительно без локали).")
    await take_screenshot(page, "smart_check_failed_attempt_1")

    # Перезапускаем браузер с флагом force_no_locale
    page = await get_whatsapp_page(context, user_id, force_new=True, force_no_locale=True)
    if not page:
        return None

    try:
        await page.goto("https://web.whatsapp.com/", timeout=60000)
        logger.info(f"Умная проверка для {user_id}: Попытка №2 (без локали) завершена. Возвращаем страницу для дальнейшей обработки.")
        return page # Возвращаем страницу, какой бы она ни была (с QR или уже залогиненной)
    except Exception as e:
        logger.error(f"Умная проверка для {user_id}: Критическая ошибка на Попытке №2: {e}")
        await take_screenshot(page, "smart_check_failed_attempt_2")
        return None

async def check_login_status(page: Page) -> bool:
    try:
        search_box_selector = 'div[aria-placeholder="Поиск или новый чат"], div[aria-placeholder="Search or start a new chat"]'
        await page.wait_for_selector(search_box_selector, timeout=60000)
        await take_screenshot(page, "login_success")      
        logger.info("Сессия WhatsApp активна.")
        return True
    except TimeoutError:
        logger.info("Сессия WhatsApp неактивна или не успела загрузиться.")
        await take_screenshot(page,"login_timeout")
        return False

async def find_and_click_chat(page: Page, chat_name: str) -> bool:
    search_box_selector = 'div[aria-placeholder="Поиск или новый чат"], div[aria-placeholder="Search or start a new chat"]'
    try:
        logger.info(f"Поиск чата: '{chat_name}'")
        await page.locator(search_box_selector).fill(chat_name)
        await asyncio.sleep(1)
        
        chat_title_selector = f'span[title="{chat_name}"]'
        await page.wait_for_selector(chat_title_selector, timeout=60000)
        
        chat_container = page.locator(f'div[role="listitem"]:has({chat_title_selector})')
        await chat_container.click()

        logger.info(f"Чат '{chat_name}' найден и открыт.")
        await asyncio.sleep(0.5)
        await take_screenshot(page, f"chat_opened_{chat_name.replace(' ', '_')}")
        return True
    except TimeoutError:
        logger.warning(f"Чат '{chat_name}' не найден после поиска.")
        await page.locator(search_box_selector).fill("") # Очищаем поиск
        return False

# --- TELEGRAM COMMAND HANDLERS ---

@command_wrapper
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 **Привет! Я бот для отправки сообщений в WhatsApp.**\n\n"
        "**Как пользоваться:**\n"
        "1️⃣ `/login` - привяжите свой WhatsApp.\n"
        "2️⃣ `/send \"Имя чата\" \"Текст\"` - для отправки текста.\n"
        "3️⃣ Прикрепите файл и в подписи напишите `/send \"Имя чата\"` для отправки файла.\n\n"
        "💡 **Отложенная отправка:**\n"
        "Чтобы отправить сообщение или файл в нужное время, просто зажмите кнопку отправки в Telegram и выберите 'Отправить позже'. Бот обработает команду, когда она придет.",
        parse_mode='Markdown'
    )

@command_wrapper
async def login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("🔄 Запускаю умную проверку сессии...")

    page = await smart_check_and_get_page(context, update.effective_user.id)
    if not page:
        await msg.edit_text("❌ Не удалось запустить браузер после нескольких попыток. Попробуйте снова.")
        return

    try:
        # Проверяем, может после всех манипуляций мы уже залогинены
        if await check_login_status(page):
            await msg.edit_text("✅ Вы уже вошли в WhatsApp. Сессия активна.")
            await take_screenshot(page, "login_already_logged_in")
            # Сохраним сессию на всякий случай, если она обновилась
            user_state_path = get_user_state_path(update.effective_user.id)
            await context.user_data['playwright_context'].storage_state(path=user_state_path)
            return

        # Если мы здесь, значит, сессия неактивна и мы должны искать QR-код
        await msg.edit_text("Сессия неактивна. Ищу QR-код...")
        qr_selector = 'canvas[aria-label="Scan this QR code to link a device!"]'
        
        try:
            # Ждем QR-код
            await page.wait_for_selector(qr_selector, timeout=30000)
            await msg.edit_text("📷 Найден QR-код. Отправляю...")

            qr_element = page.locator(qr_selector)
            await take_screenshot(page, "login_qr_found")
            qr_code_screenshot = await qr_element.screenshot()

            # Отправляем QR-картинку
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg.message_id)
            await update.message.reply_photo(
                photo=io.BytesIO(qr_code_screenshot),
                caption="Отсканируйте QR-код с помощью приложения WhatsApp. У вас есть 60 секунд."
            )

            # Ждем входа в систему после сканирования
            chat_list_selector = 'div[aria-placeholder="Поиск или новый чат"], div[aria-placeholder="Search or start a new chat"]'
            await page.wait_for_selector(chat_list_selector, timeout=60000)
            await take_screenshot(page, "login_success")
            
            # Сохраняем успешную сессию
            user_state_path = get_user_state_path(update.effective_user.id)
            await context.user_data['playwright_context'].storage_state(path=user_state_path)
            logger.info(f"Состояние сессии сохранено для пользователя {update.effective_user.id}.")
            await update.message.reply_text("✅ Вход выполнен успешно! Сессия сохранена.")

        except TimeoutError:
            await take_screenshot(page, "login_qr_timeout")
            await msg.edit_text("❌ Не удалось найти QR-код. Возможно, страница не загрузилась. Попробуйте /login снова.")

    except Exception as e:
        logger.error(f"Произошла ошибка при входе: {e}")
        await take_screenshot(page, "login_unhandled_exception")
        await update.message.reply_text(f"❌ Произошла ошибка: {e}\nПопробуйте /login снова.")

async def send_command_internal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message: return
    command_text = message.text or message.caption
    attachment = message.effective_attachment

    if not command_text:
        return

    try:
        args = shlex.split(command_text)
        chat_name = None
        message_text = None

        # --- ИЗМЕНЕНИЕ: Раздельная проверка для файла и текста ---
        if attachment: # Логика для файла
            if isinstance(attachment, tuple):  # фото
                await message.reply_text(
                    "⚠️ Фото можно отправлять **только как файлы**.\n"
                    "Пожалуйста, используйте опцию 'Отправить как файл' в Telegram и подпишите:\n"
                    "`/send \"Имя чата\"`",
                    parse_mode='Markdown'
                )
                return

            if not isinstance(attachment, Document):
                await message.reply_text(
                    "⚠️ Неподдерживаемый тип файла. Отправляйте только документы.",
                    parse_mode='Markdown'
                )
                return
            chat_name = args[1].strip()
            message_text = None # Подпись к файлу не поддерживается
        else: # Логика для текста
            if len(args) != 3:
                await message.reply_text(
                    "Неверный формат для текста. Используйте:\n"
                    "`/send \"Имя чата\" \"Текст сообщения\"`",
                    parse_mode='Markdown'
                )
                return
            chat_name = args[1].strip()
            message_text = args[2].strip()

    except (ValueError, IndexError):
        await message.reply_text("Ошибка в команде. Убедитесь, что имя чата и текст заключено в двойные кавычки.")
        return

    # Если проверка прошла, увеличиваем счетчик
    context.user_data['request_count'] = context.user_data.get('request_count', 0) + 1
    logger.info(f"Счетчик команд для {update.effective_user.id} увеличен до {context.user_data['request_count']}")

    msg_status = await update.effective_message.reply_text("🔄 Проверяю сессию WhatsApp...")
    page = await get_whatsapp_page(context, update.effective_user.id) # Здесь используем обычный get, т.к. не хотим перезапускать для отправки
    
    if not page:
        await msg_status.edit_text("❌ Не удалось инициализировать браузер. Попробуйте /login.")
        return

    try:
        await page.goto("https://web.whatsapp.com/", wait_until="domcontentloaded", timeout=30000)
    except TimeoutError:
        # Если страница даже не грузится, то сессии точно нет
        logger.warning("Таймаут при загрузке страницы в send_command_internal.")
        # Ниже check_login_status вернет False

    if not await check_login_status(page):
        await msg_status.edit_text("❌ Вы не вошли в WhatsApp. Пожалуйста, используйте команду /login.")
        # Можно даже принудительно перезапустить браузер без локали, чтобы следующий /login сработал быстрее
        await get_whatsapp_page(context, update.effective_user.id, force_new=True, force_no_locale=True)
        return

    await msg_status.edit_text(f"Ищу чат '{chat_name}'...")
    if not await find_and_click_chat(page, chat_name):
        await msg_status.edit_text(f"❌ Чат с именем '{chat_name}' не найден. Проверьте название и попробуйте снова.")
        return

    download_path=None
    try:
        if attachment:
            await msg_status.edit_text("Подготовка файла к отправке...")

            # --- Получаем правильный объект файла ---
            file_to_download = attachment[-1] if isinstance(attachment, tuple) else attachment
            file_id = file_to_download.file_id
            tg_file = await context.bot.get_file(file_id)
            file_name = getattr(file_to_download, 'file_name', f"{file_to_download.file_unique_id}.jpg")

            download_path = os.path.join("temp_files", file_name)
            os.makedirs("temp_files", exist_ok=True)
            await tg_file.download_to_drive(custom_path=download_path)

            # Считаем количество сообщений "доставлено" до отправки
            delivered_selector = 'span[aria-label=" Доставлено "], span[aria-label=" Delivered "]'
            before_count = await page.locator(delivered_selector).count()
            logger.info(f"Количество сообщений до отправки: {before_count}")

            await msg_status.edit_text(f"Отправляю файл '{file_name}' в '{chat_name}'...")

            # Нажимаем «Прикрепить»
            attach_button_selector = '[aria-label="Прикрепить"], [aria-label="Attach"]'
            await page.locator(attach_button_selector).click()
            await asyncio.sleep(2)  # ждём анимацию меню

            # Жмём «Документ»
            button_container = page.get_by_role("button", name=re.compile("^(Документ|Document)$"))
            span_to_click = button_container.locator('span:has-text("Документ"), span:has-text("Document")')

            async with page.expect_file_chooser() as fc_info:
                await span_to_click.nth(1).click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(download_path)

            # Жмём «Отправить»
            send_button_selector = '[aria-label="Отправить"], [aria-label="Send"]'
            await page.locator(send_button_selector).click(timeout=60000)

            # Ждём доставки (увеличение delivered-иконок)
            try:
                await page.wait_for_function(
                    """([selector, before]) => {
                        const elements = document.querySelectorAll(selector);
                        return elements.length > before;
                    }""",
                    arg=(delivered_selector, before_count),
                    timeout=60000
                )
                await msg_status.edit_text(
                    "✅ Файл успешно отправлен и доставлен! "
                    "Из-за ограничений сервера придётся подождать перед отправкой следующего файла."
                )
            except TimeoutError:
                await msg_status.edit_text("⚠️ Файл отправлен, но не удалось дождаться подтверждения доставки.")

            if os.path.exists(download_path):
                os.remove(download_path)

        elif message_text:
            await msg_status.edit_text(f"Отправляю сообщение в '{chat_name}'...")
            msg_box_selector = 'div[aria-placeholder="Введите сообщение"], div[aria-placeholder="Type a message"]'
            await page.locator(msg_box_selector).fill(message_text)

            send_button_selector = '[aria-label="Отправить"], [aria-label="Send"]'
            await page.locator(send_button_selector).click()

            # Для текста можно сделать такую же проверку доставки
            try:
                delivered_selector = 'span[aria-label=" Доставлено "], span[aria-label=" Delivered "]'
                before_count = await page.locator(delivered_selector).count()
                await page.wait_for_function(
                    """([selector, before]) => {
                        const elements = document.querySelectorAll(selector);
                        return elements.length > before;
                    }""",
                    arg=(delivered_selector, before_count),
                    timeout=60000
                )
                await msg_status.edit_text(
                    "✅ Сообщение успешно отправлено и доставлено! "
                    "Из-за ограничений сервера придётся подождать перед отправкой следующего сообщения."
                )
            except TimeoutError:
                await msg_status.edit_text("⚠️ Сообщение отправлено, но не удалось дождаться подтверждения доставки.")


    except Exception as e:
        logger.error(f"Ошибка при отправке: {e}")
        await msg_status.edit_text(f"❌ Не удалось отправить: {e}")
        await take_screenshot(page, "send_universal_error")
    finally:
        logger.info("Сброс состояния: возврат на главную страницу WhatsApp.")
        
        timer_msg = None # Инициализируем переменную для сообщения с таймером
        try:

            total_duration = 10
            update_interval = 2

            start_time = time.monotonic()  # реальное время старта
            end_time = start_time + total_duration

            timer_msg = await update.effective_message.reply_text(
                f"⏳ Сбрасываю состояние WhatsApp для отправки нового сообщения через {total_duration} сек..."
            )

            while True:
                remaining = int(end_time - time.monotonic())
                if remaining <= 0:
                    await timer_msg.edit_text("🔄 Выполняю сброс состояния...")
                    await take_screenshot(page, "wap_before_updating")
                    await page.goto("https://web.whatsapp.com/", wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(1)
                    await timer_msg.delete()
                    break

                # обновляем каждые update_interval секунд
                if remaining % update_interval == 0:
                    try:
                        await timer_msg.edit_text(
                            f"⏳ Сбрасываю состояние WhatsApp для отправки нового сообщения через {remaining} сек..."
                        )
                    except Exception as e:
                        if "message is not modified" not in str(e).lower():
                            logger.warning(f"Не удалось обновить таймер: {e}")

                await asyncio.sleep(1)


        except Exception as e:
            logger.error(f"Не удалось вернуться на главную страницу: {e}")
            # Если даже это не удалось, возможно, браузер "умер", лучше перезапустить
            await get_whatsapp_page(context, update.effective_user.id, force_new=True)
            if timer_msg:
                await timer_msg.edit_text("❌ Ошибка при сбросе состояния.")

        if download_path and os.path.exists(download_path):
            os.remove(download_path)
            logger.info(f"Временный файл удален: {download_path}")


# Обертка для send_command, чтобы сначала проверить лимит
async def send_command_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await check_and_request_support(update, context):
        return
    await send_command_internal(update, context)


# --- MAIN ---

def main() -> None:

    for file in glob.glob(os.path.join(PLAYWRIGHT_STATE_DIR, "*.json")):
        try:
            os.remove(file)
        except Exception as e:
            logger.warning(f"Не удалось удалить старый state файл {file}: {e}")

    if not TELEGRAM_TOKEN:
        logger.critical("TELEGRAM_TOKEN не задан в .env файле.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    send_handler = MessageHandler(
        (filters.TEXT & filters.Regex(r'^/send')) | 
        (filters.ATTACHMENT & filters.CaptionRegex(r'^/send')),
        send_command_wrapper # Используем обертку
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(send_handler)
    # --- НОВОЕ: Добавляем обработчик для кнопки сброса счетчика ---
    application.add_handler(CallbackQueryHandler(reset_support_counter_callback, pattern='^reset_support_counter$'))
    
    logger.info("Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()

# --- END OF FILE bot.py ---