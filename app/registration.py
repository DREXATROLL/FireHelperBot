from aiogram import types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import async_sessionmaker # <--- ИМПОРТИРУЕМ async_sessionmaker
from models import Employee, is_user_registered_v2 # async_session здесь больше не нужен напрямую
from app.keyboards import get_position_keyboard, get_rank_keyboard
from app.menu import show_role_specific_menu
from sqlalchemy import select
import logging
from aiogram.types import ReplyKeyboardRemove

class RegistrationStates(StatesGroup):
    WAITING_FOR_NAME = State()
    WAITING_FOR_POSITION = State()
    WAITING_FOR_RANK = State()
    WAITING_FOR_SHIFT_AND_CONTACTS = State()

# --- ИЗМЕНЯЕМ СИГНАТУРУ ФУНКЦИИ ---
async def start_bot(message: types.Message, state: FSMContext, session_factory: async_sessionmaker):
    await state.clear()
    user_id = message.from_user.id

    # is_user_registered теперь тоже должен принимать session_factory
    if await is_user_registered_v2(user_id, session_factory): # Используем новую версию is_user_registered_v2
        logging.info(f"Зарегистрированный пользователь {user_id} запустил /start")
        async with session_factory() as session: # <--- СОЗДАЕМ СЕССИЮ ЗДЕСЬ
            employee = await session.execute(
                select(Employee).where(Employee.telegram_id == user_id)
            )
            employee = employee.scalar_one_or_none()
            if employee:
                await show_role_specific_menu(message, employee.id, employee.position)
            else:
                logging.error(f"Не найден Employee в БД для зарегистрированного user_id {user_id}")
                await message.answer("Произошла ошибка при получении ваших данных. Попробуйте позже.")
    else:
        logging.info(f"Незарегистрированный пользователь {user_id} запустил /start")
        await message.answer(
            "🔐 Для начала работы необходимо пройти регистрацию.\n"
            "Введите ваше ФИО (например: Иванов Иван Иванович):",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.set_state(RegistrationStates.WAITING_FOR_NAME)

async def start_registration(message: types.Message, state: FSMContext):
    # Эта команда может быть избыточной, если /start ведет на регистрацию
    await message.answer("Введите ФИО сотрудника:")
    await state.set_state(RegistrationStates.WAITING_FOR_NAME)

async def process_name(message: types.Message, state: FSMContext):
    name_parts = message.text.split()
    # Простая проверка на количество слов, можно улучшить
    if len(name_parts) < 2 or len(name_parts) > 4 : # Допускаем 2-4 слова
        await message.answer("ФИО должно состоять из 2-4 слов. Попробуйте ещё раз:")
        return

    await state.update_data(full_name=message.text)
    await message.answer(
        "Выберите должность:",
        reply_markup=get_position_keyboard()
    )
    await state.set_state(RegistrationStates.WAITING_FOR_POSITION)

async def process_position(callback: types.CallbackQuery, state: FSMContext):
    position = callback.data.split('_')[1]
    await state.update_data(position=position)
    await callback.message.edit_text(
        f"Выбрана должность: {position}\nТеперь выберите звание:",
        reply_markup=get_rank_keyboard()
    )
    await state.set_state(RegistrationStates.WAITING_FOR_RANK)

async def process_rank(callback: types.CallbackQuery, state: FSMContext):
    rank = callback.data.split('_')[1]
    await state.update_data(rank=rank)
    await callback.message.edit_text(
        f"Выбрано звание: {rank}\nТеперь введите ваши контакты (например: +79991234567):" # Убрали упоминание смены
    )
    await state.set_state(RegistrationStates.WAITING_FOR_SHIFT_AND_CONTACTS) # Оставляем это состояние, но его обработчик изменится

async def process_contacts(message: types.Message, state: FSMContext, session_factory: async_sessionmaker):
    try:
        contacts = message.text.strip()
        if not contacts.startswith('+') or not contacts[1:].isdigit() or len(contacts) < 11:
            raise ValueError("Неверный формат контактов. Пример: +79991234567")

        data = await state.get_data()

        async with session_factory() as session: # <--- СОЗДАЕМ СЕССИЮ ЗДЕСЬ
            async with session.begin(): # <--- НАЧИНАЕМ ТРАНЗАКЦИЮ
                employee = Employee(
                    telegram_id=message.from_user.id,
                    full_name=data['full_name'],
                    position=data['position'],
                    rank=data['rank'],
                    contacts=contacts
                )
                session.add(employee)
                # Коммит произойдет автоматически при выходе из session.begin()
            
            # employee.id будет доступен здесь после коммита (или после session.flush() внутри транзакции)
            # Для show_role_specific_menu нам нужен employee.id.
            # Если employee.id не устанавливается до коммита (зависит от БД и SQLAlchemy),
            # можно либо сделать flush, либо перечитать employee после коммита,
            # либо передать employee объект, если он будет иметь ID.
            # После session.begin() объект employee должен содержать ID.

            logging.info(f"Новый сотрудник зарегистрирован: {employee.telegram_id} - {employee.full_name}, ID: {employee.id}")
            await message.answer(f"✅ Регистрация успешно завершена, {data['full_name']}!")
            await show_role_specific_menu(message, employee.id, data['position']) # Используем employee.id
            await state.clear()

    except ValueError as ve:
        await message.answer(f"🚫 Ошибка формата: {ve}\nПопробуйте ещё раз (например: +79991234567)")
    except Exception as e:
        logging.exception(f"Ошибка сохранения сотрудника: {e}")
        await message.answer(f"🚫 Произошла ошибка при регистрации: {e}\nПопробуйте ещё раз.")

async def cancel_registration(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("❌ Регистрация отменена.")
    await state.clear()
    await callback.answer()

async def back_to_position(callback: types.CallbackQuery, state: FSMContext):
    # Убедимся, что мы в правильном состоянии
    current_state = await state.get_state()
    if current_state == RegistrationStates.WAITING_FOR_RANK:
        await callback.message.edit_text(
            "Выберите должность:",
            reply_markup=get_position_keyboard()
        )
        await state.set_state(RegistrationStates.WAITING_FOR_POSITION)
    else:
        await callback.answer("Действие недоступно.", show_alert=True)
        logging.warning(f"Попытка back_to_position из неверного состояния {current_state}")
    await callback.answer()

# Убрали process_new_shift_number