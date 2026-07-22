import os
import logging
import asyncio
import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters.callback_data import CallbackData

# Загружаем переменные из .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", 0))
PORT = int(os.getenv("PORT", 8080))
APP_URL = os.getenv("APP_URL") # Внешний URL на Render (например, https://my-bot.onrender.com)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# ==========================================
# МАШИНА СОСТОЯНИЙ (FSM)
# ==========================================

class IdeaForm(StatesGroup):
    waiting_for_idea = State()

class AdminReplyForm(StatesGroup):
    waiting_for_reply_text = State()
    target_user_id = State() # Временное хранение ID пользователя, которому мы отвечаем

# CallbackData для удобной типизации данных в кнопках
class SendTypeCallback(CallbackData, prefix="send_type"):
    is_anonymous: bool

class ReplyCallback(CallbackData, prefix="reply"):
    user_id: int

# ==========================================
# ЛОГИКА ПОЛЬЗОВАТЕЛЕЙ (ШКОЛЬНИКОВ)
# ==========================================

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await message.answer(
        "Привет! Я бот для сбора инициатив по улучшению школы.\n"
        "Пожалуйста, напиши свою идею в одном сообщении:"
    )
    await state.set_state(IdeaForm.waiting_for_idea)

@router.message(IdeaForm.waiting_for_idea, F.text)
async def process_idea_text(message: Message, state: FSMContext):
    # Сохраняем текст идеи в память (user_data)
    await state.update_data(idea_text=message.text)
    
    # Формируем клавиатуру с выбором анонимности
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Анонимно 🥷", callback_data=SendTypeCallback(is_anonymous=True).pack()),
            InlineKeyboardButton(text="Не анонимно 👤", callback_data=SendTypeCallback(is_anonymous=False).pack())
        ]
    ])
    
    await message.answer(
        "Твоя идея принята! Как ты хочешь её отправить?",
        reply_markup=keyboard
    )
    # Состояние не сбрасываем, ждем нажатия кнопки!

@router.callback_query(SendTypeCallback.filter(), IdeaForm.waiting_for_idea)
async def process_idea_send_type(callback: CallbackQuery, callback_data: SendTypeCallback, state: FSMContext):
    # Достаем текст идеи из хранилища состояния
    data = await state.get_data()
    idea_text = data.get("idea_text")
    
    # Формируем сообщение для модераторов
    if callback_data.is_anonymous:
        admin_text = f"🚨 <b>Новая АНОНИМНАЯ идея:</b>\n\n{idea_text}"
        admin_keyboard = None
    else:
        user_name = callback.from_user.full_name
        username = f" (@{callback.from_user.username})" if callback.from_user.username else ""
        user_id = callback.from_user.id
        
        admin_text = f"💡 <b>Новая идея от {user_name}{username} (ID: {user_id}):</b>\n\n{idea_text}"
        # Кнопка для ответа автору
        admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Ответить автору", callback_data=ReplyCallback(user_id=user_id).pack())]
        ])

    # Отправляем в группу модераторов
    try:
        await bot.send_message(
            chat_id=ADMIN_GROUP_ID, 
            text=admin_text, 
            reply_markup=admin_keyboard,
            parse_mode="HTML"
        )
        await callback.message.edit_text("Спасибо! Ваша идея успешно отправлена модераторам.")
    except Exception as e:
        logging.error(f"Ошибка при отправке в админ-группу: {e}")
        await callback.message.edit_text("Произошла ошибка при отправке. Пожалуйста, обратитесь к администратору.")

    await state.clear() # Очищаем данные отправителя

# ==========================================
# ЛОГИКА МОДЕРАТОРОВ (АДМИНОВ)
# ==========================================

@router.callback_query(ReplyCallback.filter())
async def process_admin_reply_button(callback: CallbackQuery, callback_data: ReplyCallback, state: FSMContext):
    # Проверяем, что кнопка нажата в группе (необязательно, но полезно)
    if callback.message.chat.id != ADMIN_GROUP_ID:
        return await callback.answer("Эту кнопку можно нажимать только в чате модераторов.")

    user_id_to_reply = callback_data.user_id
    
    # Записываем, кому именно мы будем отвечать
    await state.update_data(target_user_id=user_id_to_reply)
    await state.set_state(AdminReplyForm.waiting_for_reply_text)
    
    await callback.message.reply(
        "Напишите текст ответа. Следующее ваше сообщение будет переслано автору идеи."
    )
    await callback.answer()

@router.message(AdminReplyForm.waiting_for_reply_text, F.text)
async def process_admin_reply_text(message: Message, state: FSMContext):
    # Достаем ID школьника, которому нужно ответить
    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    
    try:
        # Отправляем сообщение школьнику
        await bot.send_message(
            chat_id=target_user_id,
            text=f"✉️ <b>Ответ от модератора на вашу идею:</b>\n\n{message.text}",
            parse_mode="HTML"
        )
        await message.reply("✅ Ответ успешно доставлен автору!")
    except Exception as e:
        logging.error(f"Не удалось отправить ответ пользователю {target_user_id}: {e}")
        await message.reply("❌ Ошибка отправки! Возможно, пользователь заблокировал бота.")
    
    await state.clear()

# ==========================================
# WEB СЕРВЕР & KEEP-ALIVE ДЛЯ RENDER.COM
# ==========================================

async def web_health_handler(request):
    """Простой эндпоинт, чтобы Render понимал, что сервис жив."""
    return web.Response(text="Bot is running! (OK)", status=200)

async def start_web_server():
    """Запускает aiohttp web-сервер в фоне."""
    app = web.Application()
    app.router.add_get('/', web_health_handler)
    app.router.add_get('/health', web_health_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {PORT}")

async def keep_alive_task():
    """
    Раз в 5 минут отправляет GET-запрос на свой же URL.
    Это имитирует внешнюю активность и не дает Render усыпить бота.
    ВАЖНО: Должен использоваться внешний URL (https://...), а не localhost!
    """
    if not APP_URL:
        logging.warning("APP_URL не задан в .env. Keep-Alive пинг не будет работать корректно на Render.")
        return

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Пингуем сами себя
                async with session.get(APP_URL) as response:
                    logging.info(f"Keep-Alive пинг: статус {response.status}")
            except Exception as e:
                logging.error(f"Ошибка Keep-Alive пинга: {e}")
            
            # Ждем 5 минут (300 секунд) перед следующим пингом
            await asyncio.sleep(300)

# ==========================================
# ЗАПУСК БОТА
# ==========================================

async def main():
    dp.include_router(router)
    
    # Запускаем фоновый веб-сервер
    await start_web_server()
    
    # Запускаем фоновую задачу для Keep-Alive
    asyncio.create_task(keep_alive_task())
    
    # Удаляем старые вебхуки (на случай, если они были) и запускаем поллинг
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот начал поллинг (успешный запуск)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
        