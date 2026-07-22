import asyncio
import logging
import os
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", 0))
PORT = int(os.getenv("PORT", 8080))
APP_URL = os.getenv("APP_URL")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# Словарь для связи: ID топика -> ID автора (user_id)
topic_authors = {}

# ==========================================
# МАШИНА СОСТОЯНИЙ (FSM)
# ==========================================


class IdeaForm(StatesGroup):
  waiting_for_idea = State()


class SendTypeCallback(CallbackData, prefix="send_type"):
  is_anonymous: bool


# ==========================================
# ЛОГИКА ПОЛЬЗОВАТЕЛЕЙ (ШКОЛЬНИКОВ)
# ==========================================


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
  await message.answer(
      "Привет! Я бот для сбора инициатив по улучшению школы.\nПожалуйста,"
      " напиши свою идею в одном сообщении:"
  )
  await state.set_state(IdeaForm.waiting_for_idea)


@router.message(IdeaForm.waiting_for_idea, F.text)
async def process_idea_text(message: Message, state: FSMContext):
  await state.update_data(idea_text=message.text)

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[[
          InlineKeyboardButton(
              text="Анонимно 🥷",
              callback_data=SendTypeCallback(is_anonymous=True).pack(),
          ),
          InlineKeyboardButton(
              text="Не анонимно 👤",
              callback_data=SendTypeCallback(is_anonymous=False).pack(),
          ),
      ]]
  )

  await message.answer(
      "Твоя идея принята! Как ты хочешь её отправить?", reply_markup=keyboard
  )


@router.callback_query(
    SendTypeCallback.filter(), IdeaForm.waiting_for_idea
)
async def process_idea_send_type(
    callback: CallbackQuery,
    callback_data: SendTypeCallback,
    state: FSMContext,
):
  data = await state.get_data()
  idea_text = data.get("idea_text")

  if callback_data.is_anonymous:
    topic_name = "🥷 Анонимная идея"
    admin_text = f"🚨 <b>Новая АНОНИМНАЯ идея:</b>\n\n{idea_text}"
  else:
    user_name = callback.from_user.full_name
    username = (
        f" (@{callback.from_user.username})"
        if callback.from_user.username
        else ""
    )
    user_id = callback.from_user.id

    topic_name = f"💡 Идея от {user_name}"
    admin_text = (
        f"💡 <b>Новая идея от {user_name}{username}:</b>\n\n{idea_text}\n\n📌"
        " <i>Чтобы ответить автору, просто пишите сообщения прямо в эту"
        " тему!</i>"
    )

  try:
    # 1. Создаем топик
    new_topic = await bot.create_forum_topic(
        chat_id=ADMIN_GROUP_ID, name=topic_name
    )

    # 2. Если не анонимно — запоминаем ID автора для этого топика
    if not callback_data.is_anonymous:
      topic_authors[new_topic.message_thread_id] = callback.from_user.id

    # 3. Отправляем текст в топик
    await bot.send_message(
        chat_id=ADMIN_GROUP_ID,
        message_thread_id=new_topic.message_thread_id,
        text=admin_text,
        parse_mode="HTML",
    )

    await callback.message.edit_text(
        "Спасибо! Ваша идея успешно отправлена модераторам."
    )
  except Exception as e:
    logging.error(f"Ошибка при создании темы: {e}")
    await callback.message.edit_text(
        "Произошла ошибка при отправке. Убедитесь, что в группе включены темы."
    )

  await state.clear()


# ==========================================
# ЛОГИКА МОДЕРАТОРОВ (ОТВЕТ ИЗ ТЕМЫ)
# ==========================================


@router.message(F.chat.id == ADMIN_GROUP_ID, F.message_thread_id)
async def reply_from_topic(message: Message):
  """Ловит сообщения админов в теме и пересылает их ученику."""
  if not message.text or message.from_user.is_bot:
    return

  thread_id = message.message_thread_id

  # Проверяем, есть ли записанный ID автора для этой темы
  if thread_id in topic_authors:
    target_user_id = topic_authors[thread_id]
    try:
      await bot.send_message(
          chat_id=target_user_id,
          text=f"✉️ <b>Ответ от администрации школы:</b>\n\n{message.text}",
          parse_mode="HTML",
      )
      # Подтверждаем отправку реакцией
      await message.react([{"type": "emoji", "emoji": "👍"}])
    except Exception as e:
      logging.error(f"Ошибка при отправке сообщения пользователю: {e}")
  else:
    logging.info("Тема анонимная или перезапускался бот.")


# ==========================================
# WEB СЕРВЕР & KEEP-ALIVE ДЛЯ RENDER.COM
# ==========================================


async def web_health_handler(request):
  return web.Response(text="Bot is running! (OK)", status=200)


async def start_web_server():
  app = web.Application()
  app.router.add_get("/", web_health_handler)
  app.router.add_get("/health", web_health_handler)

  runner = web.AppRunner(app)
  await runner.setup()
  site = web.TCPSite(runner, "0.0.0.0", PORT)
  await site.start()


async def keep_alive_task():
  if not APP_URL:
    return
  async with aiohttp.ClientSession() as session:
    while True:
      try:
        async with session.get(APP_URL) as response:
          pass
      except Exception as e:
        logging.error(f"Keep-alive error: {e}")
      await asyncio.sleep(300)


# ==========================================
# ЗАПУСК БОТА
# ==========================================


async def main():
  dp.include_router(router)
  await start_web_server()
  asyncio.create_task(keep_alive_task())
  await bot.delete_webhook(drop_pending_updates=True)
  await dp.start_polling(bot)


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except (KeyboardInterrupt, SystemExit):
    pass