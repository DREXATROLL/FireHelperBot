import asyncio
import os
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher,Router
from aiogram.fsm.storage.memory import MemoryStorage
from app import register_handlers
from models import create_tables
load_dotenv()

async def main():
    
    await create_tables()
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher(storage=MemoryStorage())
    
    router = Router()
    register_handlers(router, bot)
    dp.include_router(router)
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped")