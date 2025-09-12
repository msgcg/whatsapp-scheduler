# --- START OF FILE bot.py ---
import re
import asyncio
import os
import shlex
import json
import functools
import logging
import io
import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from playwright.async_api import async_playwright, Page, TimeoutError

# --- CONFIGURATION ---
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

PLAYWRIGHT_STATE_PATH = "playwright_state.json"

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- PLAYWRIGHT HELPERS ---

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

async def get_whatsapp_page(context: ContextTypes.DEFAULT_TYPE, force_new: bool = False) -> Page | None:
    if force_new and 'browser' in context.bot_data and context.bot_data['browser'].is_connected():
        logger.info("Принудительное закрытие существующего экземпляра браузера...")
        await context.bot_data['browser'].close()
        for key in ['browser', 'playwright_context', 'whatsapp_page']:
            context.bot_data.pop(key, None)

    if 'browser' in context.bot_data and context.bot_data['browser'].is_connected():
        page = context.bot_data.get('whatsapp_page')
        if page and not page.is_closed():
            logger.info("Используется существующая страница WhatsApp.")
            return page
        
        logger.info("Страница закрыта, создаем новую.")
        pw_context = context.bot_data['playwright_context']
        page = await pw_context.new_page()
        context.bot_data['whatsapp_page'] = page
        return page

    logger.info("Запуск нового экземпляра Playwright...")
    try:
        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-blink-features=AutomationControlled'])
        context.bot_data['browser'] = browser
        storage_state = PLAYWRIGHT_STATE_PATH if os.path.exists(PLAYWRIGHT_STATE_PATH) else None
        
        pw_context = await browser.new_context(
            storage_state=storage_state,
            locale="ru-RU",  # Указываем предпочитаемые языки
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        context.bot_data['playwright_context'] = pw_context
        
        page = await pw_context.new_page()
        context.bot_data['whatsapp_page'] = page
        return page
    except Exception as e:
        logger.error(f"Не удалось запустить Playwright: {e}")
        return None

async def check_login_status(page: Page) -> bool:
    try:
        # --- ИЗМЕНЕНИЕ: Добавлена проверка на английский и русский язык ---
        search_box_selector = 'div[aria-placeholder="Поиск или новый чат"], div[aria-placeholder="Search or start a new chat"]'
        await page.wait_for_selector(search_box_selector, timeout=5000)
        await take_screenshot(page, "login_success")
        logger.info("Сессия WhatsApp активна.")
        return True
    except TimeoutError:
        logger.info("Сессия WhatsApp неактивна или не успела загрузиться.")
        await page.screenshot(path="login_timeout.png")
        return False

async def find_and_click_chat(page: Page, chat_name: str) -> bool:
    # --- ИЗМЕНЕНИЕ: Добавлена проверка на английский и русский язык ---
    search_box_selector = 'div[aria-placeholder="Поиск или новый чат"], div[aria-placeholder="Search or start a new chat"]'
    try:
        logger.info(f"Поиск чата: '{chat_name}'")
        await page.locator(search_box_selector).fill(chat_name)
        await asyncio.sleep(1)
        
        chat_title_selector = f'span[title="{chat_name}"]'
        await page.wait_for_selector(chat_title_selector, timeout=5000)
        
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

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    force_new = 'new' in (context.args or [])
    msg = await update.message.reply_text("🔄 Инициализация браузера...")
    page = await get_whatsapp_page(context, force_new=force_new)
    if not page:
        await msg.edit_text("❌ Не удалось запустить браузер. Попробуйте снова.")
        return

    try:
        await msg.edit_text("🔄 Переход на WhatsApp Web...")
        await page.goto("https://web.whatsapp.com/", timeout=15000)
        await take_screenshot(page, "login_goto")

        qr_selector = 'canvas[aria-label="Scan this QR code to link a device!"]'
        # --- ИЗМЕНЕНИЕ: Добавлена проверка на английский и русский язык ---
        chat_list_selector = 'div[aria-placeholder="Поиск или новый чат"], div[aria-placeholder="Search or start a new chat"]'

        try:
            # Ждем QR
            await page.wait_for_selector(qr_selector, timeout=15000)
            await msg.edit_text("📷 Найден QR-код. Отправляю...")

            qr_element = page.locator(qr_selector)
            await take_screenshot(page, "login_qr_found")
            qr_code_screenshot = await qr_element.screenshot()

            # Удаляем старое сообщение и отправляем QR-картинку
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg.message_id)
            qr_msg = await update.message.reply_photo(
                photo=io.BytesIO(qr_code_screenshot),
                caption="Отсканируйте QR-код с помощью приложения WhatsApp. У вас есть 60 секунд."
            )

            # Ждем появления списка чатов
            try:
                await page.wait_for_selector(chat_list_selector, timeout=60000)
                await take_screenshot(page, "login_success")

                await context.bot_data['playwright_context'].storage_state(path=PLAYWRIGHT_STATE_PATH)
                logger.info("Состояние сессии сохранено.")
                await update.message.reply_text("✅ Вход выполнен успешно! Сессия сохранена.")

            except TimeoutError:
                await take_screenshot(page, "login_timeout")
                await update.message.reply_text("❌ Не удалось найти список чатов. Сессия может быть неактивной.")
                await update.message.reply_document(document=open("debug_screenshots/login_timeout.png", "rb"))

        except TimeoutError:
            # Если QR не появился — проверяем, может уже вошли
            if await check_login_status(page):
                await msg.edit_text("✅ Вы уже вошли в WhatsApp. Сессия активна.")
                await take_screenshot(page, "login_already_logged_in")
            else:
                await take_screenshot(page, "login_error")
                await msg.edit_text("❌ Не удалось найти QR-код или вход не удался. Попробуйте снова.")
                await update.message.reply_document(document=open("debug_screenshots/login_error.png", "rb"))

    except Exception as e:
        logger.error(f"Произошла ошибка при входе: {e}")
        await take_screenshot(page, "login_unhandled_exception")
        await update.message.reply_text(f"❌ Произошла ошибка: {e}\nПопробуйте /login снова.")
        await update.message.reply_document(document=open("debug_screenshots/login_unhandled_exception.png", "rb"))


async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    command_text = message.text or message.caption
    attachment = message.effective_attachment

    if not command_text:
        return

    try:
        args = shlex.split(command_text)
        if not (2 <= len(args) <= 3):
            await message.reply_text(
                "Неверный формат. Используйте:\n"
                "`/send \"Имя чата\" \"Текст сообщения\"`\n"
                "Или прикрепите файл и в подписи укажите:\n"
                "`/send \"Имя чата\"`",
                parse_mode='Markdown'
            )
            return
            
        chat_name = args[1].strip()
        message_text = args[2].strip() if len(args) > 2 else None

        if not attachment and not message_text:
            await message.reply_text("❌ Нечего отправлять. Укажите текст сообщения или прикрепите файл.")
            return

    except (ValueError, IndexError):
        await message.reply_text("Ошибка в команде. Убедитесь, что имя чата заключено в двойные кавычки.")
        return

    msg_status = await message.reply_text("🔄 Проверяю сессию WhatsApp...")
    page = await get_whatsapp_page(context)
    if not page or not await check_login_status(page):
        await msg_status.edit_text("❌ Вы не вошли в WhatsApp. Пожалуйста, используйте команду /login.")
        return

    await msg_status.edit_text(f"Ищу чат '{chat_name}'...")
    if not await find_and_click_chat(page, chat_name):
        await msg_status.edit_text(f"❌ Чат с именем '{chat_name}' не найден. Проверьте название и попробуйте снова.")
        return

    download_path=None
    try:
        if attachment:
            await msg_status.edit_text("Подготовка файла к отправке...")
            file_id = attachment.file_id
            tg_file = await context.bot.get_file(file_id)
            file_name = getattr(attachment, 'file_name', f"{attachment.file_unique_id}.jpg")
            
            download_path = os.path.join("temp_files", file_name)
            os.makedirs("temp_files", exist_ok=True)
            await tg_file.download_to_drive(custom_path=download_path)

            await msg_status.edit_text(f"Отправляю файл '{file_name}' в '{chat_name}'...")
            
            # --- ИЗМЕНЕНИЕ: Добавлена проверка на английский и русский язык для кнопки "Прикрепить" ---
            attach_button_selector = '[aria-label="Прикрепить"], [aria-label="Attach"]'
            await page.locator(attach_button_selector).click()
            
            logger.info("Ожидание анимации меню вложения...")
            await asyncio.sleep(2)
            await take_screenshot(page, "sendfile_attach_menu_fully_visible")
            
            # --- НОВЫЙ ДВУЯЗЫЧНЫЙ КОД (ВОССТАНОВЛЕННАЯ ЛОГИКА) ---
            file_type = getattr(attachment, 'mime_type', '').lower()

            # 1. Определяем русские и английские имена кнопок
            if 'image' in file_type or 'video' in file_type:
                button_name_ru = "Фото и видео"
                button_name_en = "Photos & videos"
            else:
                button_name_ru = "Документ"
                button_name_en = "Document"

            # 2. Находим родительский контейнер кнопки, используя регулярное выражение для поиска по обоим языкам.
            #    Это аналог вашего page.get_by_role("button", name=button_name)
            button_container = page.get_by_role("button", name=re.compile(f"^({button_name_ru}|{button_name_en})$"))

            # 3. Внутри этого контейнера находим ТОЧНЫЙ SPAN с текстом, как вы и делали.
            #    Селектор ищет span, содержащий либо русский, либо английский текст.
            span_selector = f'span:has-text("{button_name_ru}"), span:has-text("{button_name_en}")'
            span_to_click = button_container.locator(span_selector)

            # 4. Выполняем клик по span и ожидаем окно выбора файла.
            async with page.expect_file_chooser() as fc_info:
                await span_to_click.nth(1).click()

            file_chooser = await fc_info.value
            await file_chooser.set_files(download_path)
            
            if message_text:
                # --- ИЗМЕНЕНИЕ: Добавлена проверка на английский и русский язык для поля подписи ---
                caption_selector = 'div[aria-label*="Добавить подпись"], div[aria-label*="Add a caption"]'
                await page.wait_for_selector(caption_selector, timeout=15000)
                await page.locator(caption_selector).fill(message_text)
                await take_screenshot(page, "sendfile_caption_filled")

            # --- ИЗМЕНЕНИЕ: Добавлена проверка на английский и русский язык для кнопки "Отправить" ---
            send_button_selector = '[aria-label="Отправить"], [aria-label="Send"]'
            await page.locator(send_button_selector).click(timeout=60000)
            
            await msg_status.edit_text("✅ Файл успешно отправлен!")

            if os.path.exists(download_path):
                os.remove(download_path)

        elif message_text:
            await msg_status.edit_text(f"Отправляю сообщение в '{chat_name}'...")
            # --- ИЗМЕНЕНИЕ: Добавлена проверка на английский и русский язык для поля ввода сообщения ---
            msg_box_selector = 'div[aria-placeholder="Введите сообщение"], div[aria-placeholder="Type a message"]'
            await page.locator(msg_box_selector).fill(message_text)
            await take_screenshot(page, "send_message_filled")
            
            # --- ИЗМЕНЕНИЕ: Добавлена проверка на английский и русский язык для кнопки "Отправить" ---
            send_button_selector = '[aria-label="Отправить"], [aria-label="Send"]'
            await page.locator(send_button_selector).click()
            await msg_status.edit_text("✅ Сообщение успешно отправлено!")

    except Exception as e:
        logger.error(f"Ошибка при отправке: {e}")
        await msg_status.edit_text(f"❌ Не удалось отправить: {e}")
        await take_screenshot(page, "send_universal_error")
    finally:
        # --- ИЗМЕНЕНИЕ: Блок для сброса состояния и очистки ---
        logger.info("Сброс состояния: возврат на главную страницу WhatsApp.")
        try:
            await asyncio.sleep(5)
            await take_screenshot(page, "wap_before_updating")
            # Возвращаемся на главный экран, чтобы следующая команда началась с чистого листа
            await page.goto("https://web.whatsapp.com/", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(1) # Даем странице мгновение на прогрузку
        except Exception as e:
            logger.error(f"Не удалось вернуться на главную страницу: {e}")
            # Если даже это не удалось, возможно, браузер "умер", лучше перезапустить
            await get_whatsapp_page(context, force_new=True)

        if download_path and os.path.exists(download_path):
            os.remove(download_path)
            logger.info(f"Временный файл удален: {download_path}")

# --- MAIN ---

def main() -> None:
    if not TELEGRAM_TOKEN:
        logger.critical("TELEGRAM_TOKEN не задан в .env файле.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    send_handler = MessageHandler(
        (filters.TEXT & filters.Regex(r'^/send')) | 
        (filters.ATTACHMENT & filters.CaptionRegex(r'^/send')),
        send_command
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(send_handler)
    
    logger.info("Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()

# --- END OF FILE bot.py ---