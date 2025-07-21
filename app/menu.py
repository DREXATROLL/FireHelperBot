from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram import types
from sqlalchemy import select
from models import async_session, ShiftLog
from app.keyboards import (
    get_dispatcher_menu,
    get_commander_menu
)
import logging
async def get_driver_menu_dynamic(employee_id: int):
    is_on_shift = await get_active_shift_for_menu(employee_id) is not None
    shift_button_text = "Закончить караул" if is_on_shift else "Заступить на караул"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=shift_button_text)], # <--- ДИНАМИЧЕСКАЯ КНОПКА
            [KeyboardButton(text="Новый путевой лист")],
            [KeyboardButton(text="📊 История поездок"), KeyboardButton(text="⛽ Учет ГСМ")],
            [KeyboardButton(text="🛠 Тех. состояние")]
        ],
        resize_keyboard=True
    )

async def get_firefighter_menu_dynamic(employee_id: int): # Уже принимает employee_id
    # Проверка get_active_shift_for_menu уже есть и работает для кнопки "Заступить/Закончить караул"
    is_on_shift = await get_active_shift_for_menu(employee_id) is not None 
    shift_button_text = "Закончить караул" if is_on_shift else "Заступить на караул"
    
    keyboard_buttons = [
        [KeyboardButton(text=shift_button_text)],
        [KeyboardButton(text="🔥 Мои активные выезда")],
        [KeyboardButton(text="🧯 Журнал снаряжения")],
        [KeyboardButton(text="🚨 Готовность к выезду")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True)

async def get_dispatcher_menu_dynamic(employee_id: int): # Если эта функция у тебя в keyboards.py, модифицируй там
    is_on_shift = await get_active_shift_for_menu(employee_id) is not None
    shift_button_text = "Закончить караул" if is_on_shift else "Заступить на караул"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=shift_button_text)],
            [KeyboardButton(text="🔥 Создать новый выезд")],
            [KeyboardButton(text="📊 Активные выезды"), KeyboardButton(text="📂 Архив выездов")],
            [KeyboardButton(text="Отметить отсутствующих")],
            [KeyboardButton(text="📊 Отчет по выездам")],
        ],
        resize_keyboard=True
    )
    
async def get_commander_menu_dynamic(employee_id: int): # Если эта функция у тебя в keyboards.py, модифицируй там
    is_on_shift = await get_active_shift_for_menu(employee_id) is not None
    shift_button_text = "Закончить караул" if is_on_shift else "Заступить на караул"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=shift_button_text)],
            [KeyboardButton(text="⏳ Выезды на утверждение")],
            [KeyboardButton(text="🔥 Активные выезды (все)")],
            [KeyboardButton(text="📋 Статус техники/ЛС")],
            [KeyboardButton(text="🔧 Обслуживание снаряжения")]
        ],
        resize_keyboard=True
    )

async def show_role_specific_menu(message: types.Message, employee_id: int, position: str): # Принимаем employee_id
    """Показывает меню в зависимости от должности с учетом статуса караула."""
    position_lower = position.lower()
    reply_markup = ReplyKeyboardRemove() # По умолчанию убираем клавиатуру

    if "водитель" in position_lower:
        reply_markup = await get_driver_menu_dynamic(employee_id)
        await message.answer("🚛 Меню водителя:", reply_markup=reply_markup)
    elif "пожарный" in position_lower:
        reply_markup = await get_firefighter_menu_dynamic(employee_id)
        await message.answer("🧑‍🚒 Меню пожарного:", reply_markup=reply_markup)
    elif "диспетчер" in position_lower:
        reply_markup = await get_dispatcher_menu_dynamic(employee_id) # Предполагаем, что эта функция теперь асинхронная
        await message.answer("📡 Меню диспетчера:", reply_markup=reply_markup)
    elif "начальник караула" in position_lower:
        reply_markup = await get_commander_menu_dynamic(employee_id) # Предполагаем, что эта функция теперь асинхронная
        await message.answer("👨‍✈️ Меню начальника караула:", reply_markup=reply_markup)
    else:
        await message.answer(f"👨‍💼 Основное меню для роли '{position}':", reply_markup=reply_markup)
        
async def get_active_shift_for_menu(employee_id: int) -> ShiftLog | None:
    async with async_session() as session: # Новая сессия здесь
        stmt = select(ShiftLog).where(
            ShiftLog.employee_id == employee_id,
            ShiftLog.status == 'active'
        )
        active_shift = await session.scalar(stmt)
        logging.info(f"get_active_shift_for_menu (menu.py) for employee {employee_id}: Found active shift: {bool(active_shift)}, Shift ID: {active_shift.id if active_shift else None}")
        return active_shift
    
