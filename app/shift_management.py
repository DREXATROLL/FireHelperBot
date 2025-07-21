from aiogram import F, types, Router, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
import logging
import asyncio
from models import async_session, Employee, ShiftLog, Vehicle, Equipment, EquipmentLog
from app.keyboards import get_cancel_keyboard, get_sizod_status_keyboard, get_vehicle_selection_for_shift_keyboard
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove  # Для клавиатуры "Пропустить"
from sqlalchemy.ext.asyncio import async_sessionmaker
# --- Состояния FSM для Заступления на Караул ---
# --- Состояния FSM (остаются без изменений) ---
class StartShiftStates(StatesGroup):
    ENTERING_KARAKUL_NUMBER = State()
    CHOOSING_VEHICLE = State()
    ENTERING_OPERATIONAL_PRIORITY = State()
    ENTERING_START_ODOMETER = State()
    ENTERING_START_FUEL_LEVEL = State()
    ENTERING_SIZOD_NUMBER = State()
    CHOOSING_SIZOD_STATUS_START = State()
    ENTERING_SIZOD_NOTES_START = State()

class EndShiftStates(StatesGroup):
    ENTERING_END_ODOMETER = State()
    ENTERING_END_FUEL_LEVEL = State()
    CHOOSING_SIZOD_STATUS_END = State()
    ENTERING_SIZOD_NOTES_END = State()

# --- Вспомогательная функция для проверки активного караула ---
async def get_active_shift(session_factory: async_sessionmaker, employee_id: int) -> ShiftLog | None:
    async with session_factory() as session: # Создаем сессию здесь
        stmt = select(ShiftLog).where(
            ShiftLog.employee_id == employee_id,
            ShiftLog.status == 'active'
        )
        active_shift = await session.scalar(stmt)
        logging.info(f"get_active_shift (shift_management) for employee {employee_id}: Found active shift: {bool(active_shift)}, Shift ID: {active_shift.id if active_shift else None}")
        return active_shift

async def handle_start_shift_request(message: types.Message, state: FSMContext, session_factory: async_sessionmaker):
    user_id = message.from_user.id
    logging.info(f"handle_start_shift_request: User {user_id} triggered.")
    
    employee_db_id = None # Для хранения ID сотрудника

    # Сначала получаем ID сотрудника из БД
    async with session_factory() as session:
        employee_obj = await session.scalar(select(Employee).where(Employee.telegram_id == user_id))
        if not employee_obj:
            await message.answer("Ошибка: Ваш профиль не найден. Пожалуйста, пройдите регистрацию /start.")
            return
        employee_db_id = employee_obj.id # Сохраняем ID сотрудника

    # Проверяем активный караул, используя ID сотрудника
    # get_active_shift теперь тоже принимает session_factory
    active_shift_obj = await get_active_shift(session_factory, employee_db_id)
    
    if active_shift_obj:
        await message.answer(
            f"Вы уже на карауле №{active_shift_obj.karakul_number}, заступили {active_shift_obj.start_time.strftime('%d.%m.%Y %H:%M')}.\n"
            "Сначала необходимо закончить текущий караул."
        )
        return

    # Если активного караула нет, начинаем процедуру
    await state.clear() # Очищаем предыдущее состояние FSM на всякий случай
    await message.answer(
        "Введите номер караула, на который заступаете:",
        reply_markup=get_cancel_keyboard() # Убедитесь, что get_cancel_keyboard импортирована правильно
    )
    await state.set_state(StartShiftStates.ENTERING_KARAKUL_NUMBER) # Устанавливаем начальное состояние FSM
    
    current_fsm_state = await state.get_state() # Логируем для отладки
    logging.info(f"handle_start_shift_request: User {user_id} FSM state set to {current_fsm_state}")
    # employee_obj.full_name здесь уже недоступен, если он был нужен для лога, нужно было передать его
    logging.info(f"Сотрудник с ID {employee_db_id} начал процедуру заступления на караул.")

# --- Обработчик кнопки "Заступить на караул" ---
async def handle_end_shift_request(message: types.Message, state: FSMContext, session_factory: async_sessionmaker): # Принимает session_factory
    user_id = message.from_user.id
    logging.info(f"SRV_DEBUG: handle_end_shift_request CALLED for user {user_id}")
    employee_db_id = None
    employee_position = None
    employee_full_name = None # Для лога

    async with session_factory() as session: # <--- СОЗДАЕМ СЕССИЮ ЗДЕСЬ ДЛЯ ПЕРВИЧНЫХ ДЕЙСТВИЙ
        logging.info(f"SRV_DEBUG: handle_end_shift_request: Session CREATED LOCALLY for user {user_id}")
        employee = await session.scalar(select(Employee).where(Employee.telegram_id == user_id))
        if not employee:
            await message.answer("Ошибка: Ваш профиль не найден.")
            return
        employee_db_id = employee.id
        employee_position = employee.position
        employee_full_name = employee.full_name # Сохраняем для лога

    # get_active_shift теперь тоже принимает session_factory
    active_shift_obj = await get_active_shift(session_factory, employee_db_id)
    if not active_shift_obj:
        await message.answer("Вы не числитесь на активном карауле.")
        return

    await state.clear()
    await state.update_data(active_shift_id=active_shift_obj.id, employee_db_id=employee_db_id)
    logging.info(f"Сотрудник {employee_db_id} ({employee_full_name}) начал процедуру окончания караула ID: {active_shift_obj.id}")

    # Для получения vehicle_info здесь снова нужна сессия, если active_shift_obj.vehicle_id не None
    vehicle_info_for_msg = ""
    if employee_position.lower() == "водитель" and active_shift_obj.vehicle_id is not None:
        async with session_factory() as session_for_vehicle:
            vehicle = await session_for_vehicle.get(Vehicle, active_shift_obj.vehicle_id)
            if vehicle:
                vehicle_info_for_msg = f" для автомобиля {vehicle.model} ({vehicle.number_plate})"


    if employee_position.lower() == "водитель":
        await message.answer(
            f"Завершение караула №{active_shift_obj.karakul_number}.\n"
            f"Введите конечные показания одометра{vehicle_info_for_msg} (км):", # Используем vehicle_info_for_msg
            reply_markup=get_cancel_keyboard()
        )
        await state.set_state(EndShiftStates.ENTERING_END_ODOMETER)

    elif employee_position.lower() == "пожарный":
        if active_shift_obj.sizod_number:
            await message.answer(
                f"Завершение караула №{active_shift_obj.karakul_number}.\n"
                f"Сдаете СИЗОД №{active_shift_obj.sizod_number}. Укажите его состояние:",
                reply_markup=get_sizod_status_keyboard(callback_prefix="sizod_status_end_")
            )
            await state.set_state(EndShiftStates.CHOOSING_SIZOD_STATUS_END)
        else: # Пожарный без СИЗОД (маловероятно, но обрабатываем)
            await finalize_generic_shift_end(message, state, session_factory) # Передаем session_factory
    else: # Диспетчер, Начальник Караула
        await finalize_generic_shift_end(message, state, session_factory) # Передаем session_factory


async def process_karakul_number(message: types.Message, state: FSMContext, session_factory: async_sessionmaker):
    logging.info(f"SRV_DEBUG: process_karakul_number CALLED for user {message.from_user.id} with text: '{message.text}'")
    karakul_number_str = message.text.strip()
    if not karakul_number_str.isdigit() or not (1 <= int(karakul_number_str) <= 4):
        logging.warning(f"SRV_DEBUG: process_karakul_number: Invalid karakul_number '{karakul_number_str}' for user {message.from_user.id}")
        await message.answer("Номер караула должен быть числом от 1 до 4...", reply_markup=get_cancel_keyboard())
        return

    karakul_number = karakul_number_str
    await state.update_data(karakul_number=karakul_number)
    logging.info(f"SRV_DEBUG: process_karakul_number: User {message.from_user.id} entered karakul_number: {karakul_number}. FSM data updated.")

    user_id = message.from_user.id
    
    async with session_factory() as session: # Локальная сессия
        logging.info(f"SRV_DEBUG: process_karakul_number: Session CREATED LOCALLY for user {message.from_user.id}")
        employee = await session.scalar(select(Employee).where(Employee.telegram_id == user_id))

        if not employee:
            logging.error(f"SRV_DEBUG: process_karakul_number: Employee not found for user_id {user_id}.")
            await message.answer("Ошибка: не удалось определить ваш профиль. Заступление отменено.")
            await state.clear()
            return

        await state.update_data(employee_db_id=employee.id)
        logging.info(f"SRV_DEBUG: process_karakul_number: Employee ID {employee.id} (tg: {user_id}) stored in FSM. Position: {employee.position.lower()}")
        logging.info(f"SRV_DEBUG: process_karakul_number: BEFORE BRANCHING (local session) - Is transaction active? {session.in_transaction()}")

        if session.in_transaction(): # <--- ДОБАВЛЯЕМ ПРОВЕРКУ И КОММИТ
            logging.warning(f"SRV_DEBUG: process_karakul_number: Transaction was unexpectedly active. Attempting to commit.")
            try:
                await session.commit() # Пытаемся закрыть существующую транзакцию
                logging.info(f"SRV_DEBUG: process_karakul_number: Pre-existing transaction committed. Is now active? {session.in_transaction()}")
            except Exception as e_commit:
                logging.error(f"SRV_DEBUG: process_karakul_number: Failed to commit pre-existing transaction: {e_commit}")
                await session.rollback() # Если коммит не удался, откатываем

        if employee.position.lower() == "водитель":
            logging.info(f"SRV_DEBUG: process_karakul_number: User {employee.id} is a DRIVER.")
            available_vehicles = await session.scalars(
                select(Vehicle).where(Vehicle.status == "available").order_by(Vehicle.model)
            )
            vehicles_list = available_vehicles.all()
            keyboard = get_vehicle_selection_for_shift_keyboard(vehicles_list)
            msg_text = (f"Выбран караул №{karakul_number}.\nТеперь выберите автомобиль..." if vehicles_list
                        else f"Выбран караул №{karakul_number}.\nК сожалению, нет доступных автомобилей...")
            await message.answer(text=msg_text, reply_markup=keyboard)
            if vehicles_list:
                await state.set_state(StartShiftStates.CHOOSING_VEHICLE)
                logging.info(f"SRV_DEBUG: Driver {employee.id}: FSM state set to StartShiftStates.CHOOSING_VEHICLE")

        elif employee.position.lower() == "пожарный":
            logging.info(f"SRV_DEBUG: process_karakul_number: User {employee.id} is a FIREFIGHTER.")
            await message.answer(
                f"Выбран караул №{karakul_number}.\nВведите инвентарный номер вашего СИЗОД:",
                reply_markup=get_cancel_keyboard()
            )
            await state.set_state(StartShiftStates.ENTERING_SIZOD_NUMBER)
            logging.info(f"SRV_DEBUG: Firefighter {employee.id}: FSM state set to StartShiftStates.ENTERING_SIZOD_NUMBER")
        
        else: # Диспетчер, Начальник Караула
            logging.info(f"SRV_DEBUG: process_karakul_number (OTHER): User {employee.id} is OTHER ({employee.position}).")
            logging.info(f"SRV_DEBUG: process_karakul_number (OTHER): ENTERING BRANCH (local session) - Is transaction active? {session.in_transaction()}")
            employee_id_for_menu = employee.id
            employee_position_for_menu = employee.position
            _start_time = datetime.now()

            try:
                async with session.begin(): # Транзакция на локальной сессии
                    logging.info(f"SRV_DEBUG: process_karakul_number (OTHER): Transaction block STARTED for employee {employee.id}.")
                    new_shift = ShiftLog(
                        employee_id=employee.id, # Используем полученный объект employee
                        karakul_number=karakul_number,
                        start_time=_start_time,
                        status='active'
                    )
                    logging.info(f"SRV_DEBUG: process_karakul_number (OTHER): ShiftLog object CREATED: {new_shift.__dict__}")
                    session.add(new_shift)
                    logging.info(f"SRV_DEBUG: process_karakul_number (OTHER): ShiftLog ADDED to session. Pending commit.")
                logging.info(f"SRV_DEBUG: process_karakul_number (OTHER): Transaction block COMMITTED/ROLLBACKED.")

                await message.answer(
                    f"✅ Вы успешно заступили на караул №{karakul_number} ({_start_time.strftime('%d.%m.%Y %H:%M')}).",
                    reply_markup=ReplyKeyboardRemove()
                )
                logging.info(f"Сотрудник {employee.id} ({employee.position}) заступил на караул №{karakul_number}.")

                # Импорт и вызов меню
                from app.menu import show_role_specific_menu # Импорт здесь, если есть риск циклического импорта
                await show_role_specific_menu(message, employee_id_for_menu, employee_position_for_menu)
                logging.info(f"SRV_DEBUG: Menu updated for {employee_id_for_menu} (dispatcher/nk).")

            except Exception as e:
                logging.exception(f"SRV_DEBUG: process_karakul_number (OTHER): EXCEPTION for employee {employee.id}")
                await message.answer("Произошла ошибка при заступлении на караул. Попробуйте позже.")
            finally:
                await state.clear() # Очищаем FSM для этой ветки

# Функции, которые ТОЛЬКО обновляют FSM или отправляют сообщения без прямого доступа к БД через session,
# могут не требовать session_factory. Если им нужен доступ к БД, они тоже должны его принимать.
# Пример: process_operational_priority_input, process_start_odometer_input
            
            
# --- Обработчики для Пожарного (Заступление) ---
async def process_sizod_number_input(message: types.Message, state: FSMContext): # Убрал session_factory, если не нужен
    sizod_number = message.text.strip()
    if not sizod_number:
        await message.answer("Номер СИЗОД не может быть пустым...", reply_markup=get_cancel_keyboard())
        return
    await state.update_data(sizod_number=sizod_number)
    logging.info(f"Пожарный {message.from_user.id} ввел номер СИЗОД: {sizod_number}")
    await message.answer("Укажите состояние полученного СИЗОД:", reply_markup=get_sizod_status_keyboard())
    await state.set_state(StartShiftStates.CHOOSING_SIZOD_STATUS_START)

# process_sizod_status_start_choice - аналогично
async def process_sizod_status_start_choice(callback: types.CallbackQuery, state: FSMContext, session_factory: async_sessionmaker): 
    await callback.answer()
    status_choice = callback.data.split('_')[-1].lower()
    await state.update_data(sizod_status_start=status_choice.capitalize())
    logging.info(f"Пожарный {callback.from_user.id} выбрал состояние СИЗОД: {status_choice}, cb: {callback.data}")

    if status_choice == "неисправен":
        # ... (код для неисправен)
        await callback.message.edit_text(
            "Пожалуйста, кратко опишите неисправность СИЗОД (или нажмите 'Пропустить', если описание не требуется):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➡️ Пропустить описание", callback_data="skip_sizod_notes_start")],
                [InlineKeyboardButton(text="❌ Отменить заступление", callback_data="universal_cancel")]
            ])
        )
        await state.set_state(StartShiftStates.ENTERING_SIZOD_NOTES_START)
    else:
        await state.update_data(sizod_notes_start=None)
        await finalize_firefighter_shift_start(
            state=state,
            session_factory=session_factory, # <--- Передаем session_factory
            bot_message_to_edit_or_reply_to=callback.message,
            is_from_callback=True
        )

async def process_skip_sizod_notes_start(callback: types.CallbackQuery, state: FSMContext, session_factory: async_sessionmaker): # Добавил session_factory
    await callback.answer()
    await state.update_data(sizod_notes_start="Описание пропущено")
    logging.info(f"Пожарный {callback.from_user.id} пропустил описание неисправности СИЗОД.")
    await finalize_firefighter_shift_start(
        state=state,
        session_factory=session_factory, # <--- Передаем session_factory
        bot_message_to_edit_or_reply_to=callback.message,
        is_from_callback=True
    )


async def process_sizod_notes_start_input(message: types.Message, state: FSMContext, session_factory: async_sessionmaker): # Добавил session_factory
    notes = message.text.strip()
    await state.update_data(sizod_notes_start=notes)
    logging.info(f"Пожарный {message.from_user.id} добавил примечания к СИЗОД: {notes}")
    await finalize_firefighter_shift_start(
        state=state,
        session_factory=session_factory, # <--- Передаем session_factory
        bot_message_to_edit_or_reply_to=message,
        is_from_callback=False
    )


async def finalize_firefighter_shift_start(
    state: FSMContext,
    session_factory: async_sessionmaker,
    bot_message_to_edit_or_reply_to: types.Message,
    is_from_callback: bool
):
    data = await state.get_data()
    employee_db_id = data.get('employee_db_id')
    _start_time = datetime.now()

    final_message_text_success_template = "✅ Вы успешно заступили на караул №{karakul_number} ({start_time}).\n" \
                                          "СИЗОД №{sizod_number} ({sizod_status}) зарегистрирован за вами."
    final_message_text_error = "Произошла ошибка при заступлении на караул. Попробуйте позже."
    
    logging.info(f"SRV_DEBUG: finalize_firefighter_shift_start CALLED for emp_db_id: {employee_db_id}. Data: {data}")

    async with session_factory() as session:
        logging.info(f"SRV_DEBUG: finalize_firefighter_shift_start: Session CREATED LOCALLY for emp_db_id: {employee_db_id}")
        employee_obj_for_menu = None # Для получения после транзакции

        try:
            async with session.begin():
                logging.info(f"SRV_DEBUG: finalize_firefighter_shift_start: Transaction block STARTED.")
                
                equipment = await session.scalar(
                    select(Equipment).where(Equipment.inventory_number == data['sizod_number'], Equipment.type == 'СИЗОД')
                )
                equipment_id_for_log = None
                error_message_for_user = None

                if equipment:
                    equipment_id_for_log = equipment.id
                    if equipment.status != 'available' and equipment.current_holder_id != employee_db_id and equipment.current_holder_id is not None:
                        holder = await session.get(Employee, equipment.current_holder_id)
                        holder_name = holder.full_name if holder else "неизвестным сотрудником"
                        error_message_for_user = f"❌ Ошибка: СИЗОД №{data['sizod_number']} уже используется {holder_name} (статус: {equipment.status}). Заступление отменено."
                    elif equipment.status not in ['available', 'in_use'] and equipment.current_holder_id is None: # СИЗОД свободен, но не 'available' (например, 'maintenance')
                        error_message_for_user = f"❌ Ошибка: СИЗОД №{data['sizod_number']} сейчас недоступен (статус: {equipment.status}). Заступление отменено."
                    
                    if error_message_for_user:
                        logging.error(f"SRV_DEBUG: finalize_firefighter_shift_start: {error_message_for_user}")
                        raise ValueError(error_message_for_user) # Вызовет откат транзакции

                    equipment.current_holder_id = employee_db_id
                    equipment.status = 'in_use' # СИЗОД взят
                    session.add(equipment)
                    logging.info(f"SRV_DEBUG: finalize_firefighter_shift_start: Equipment {equipment.id} status updated to 'in_use', holder set to {employee_db_id}.")
                else:
                    error_message_for_user = f"❌ Ошибка: СИЗОД с инвентарным номером '{data['sizod_number']}' не найден в базе. Обратитесь к администратору. Заступление отменено."
                    logging.error(f"SRV_DEBUG: finalize_firefighter_shift_start: {error_message_for_user}")
                    raise ValueError(error_message_for_user)

                new_shift_db_entry = ShiftLog(
                    employee_id=employee_db_id,
                    karakul_number=data['karakul_number'],
                    start_time=_start_time,
                    status='active',
                    sizod_number=data['sizod_number'],
                    sizod_status_start=data['sizod_status_start'],
                    sizod_notes_start=data.get('sizod_notes_start')
                )
                session.add(new_shift_db_entry)
                await session.flush() # Получаем ID для EquipmentLog
                logging.info(f"SRV_DEBUG: finalize_firefighter_shift_start: ShiftLog CREATED & FLUSHED, ID: {new_shift_db_entry.id}")

                if equipment_id_for_log:
                    equip_log_notes = f"Взят на караул №{data['karakul_number']}. Начальное состояние: {data['sizod_status_start']}. "
                    if data.get('sizod_notes_start') and data.get('sizod_notes_start') != "Описание пропущено":
                        equip_log_notes += f"Примечание: {data['sizod_notes_start']}"
                    else:
                        equip_log_notes += "Примечание: нет"
                    
                    equip_log = EquipmentLog(
                        employee_id=employee_db_id,
                        equipment_id=equipment_id_for_log,
                        action='taken',
                        notes=equip_log_notes,
                        shift_log_id=new_shift_db_entry.id
                    )
                    session.add(equip_log)
                    logging.info(f"SRV_DEBUG: finalize_firefighter_shift_start: EquipmentLog CREATED for shift {new_shift_db_entry.id}.")
            # --- КОММИТ/ОТКАТ ПРОИЗОШЕЛ ---
            logging.info(f"SRV_DEBUG: finalize_firefighter_shift_start: Transaction block COMMITTED (or rollbacked).")

            # Если мы здесь, транзакция успешна
            success_text = final_message_text_success_template.format(
                karakul_number=data['karakul_number'],
                start_time=_start_time.strftime('%d.%m.%Y %H:%M'),
                sizod_number=data['sizod_number'],
                sizod_status=data['sizod_status_start']
            )
            if is_from_callback:
                await bot_message_to_edit_or_reply_to.edit_text(success_text, reply_markup=None)
            else:
                await bot_message_to_edit_or_reply_to.answer(success_text, reply_markup=None)
            logging.info(f"Пожарный {employee_db_id} заступил на караул, СИЗОД {data['sizod_number']} ({data['sizod_status_start']}).")

            # Обновление меню
            async with session_factory() as menu_session: # Новая сессия для получения Employee
                employee_obj_for_menu = await menu_session.get(Employee, employee_db_id)
            
            if employee_obj_for_menu:
                from app.menu import show_role_specific_menu
                await show_role_specific_menu(bot_message_to_edit_or_reply_to, employee_obj_for_menu.id, employee_obj_for_menu.position)
                logging.info(f"SRV_DEBUG: Menu updated for firefighter {employee_db_id}.")
            else:
                logging.error(f"SRV_DEBUG: Could not get employee_obj_for_menu for firefighter {employee_db_id} to update menu.")

        except ValueError as ve: # Наши ожидаемые ошибки для отката
            error_text_to_show = str(ve)
            if is_from_callback:
                try: await bot_message_to_edit_or_reply_to.edit_text(error_text_to_show, reply_markup=None)
                except: await bot_message_to_edit_or_reply_to.answer(error_text_to_show, reply_markup=None)
            else:
                await bot_message_to_edit_or_reply_to.answer(error_text_to_show, reply_markup=None)
        except Exception as e: # Непредвиденные ошибки
            logging.exception(f"SRV_DEBUG: finalize_firefighter_shift_start: UNEXPECTED EXCEPTION for emp_db_id {employee_db_id}")
            error_text_to_show = final_message_text_error
            if is_from_callback:
                try: await bot_message_to_edit_or_reply_to.edit_text(error_text_to_show, reply_markup=None)
                except: await bot_message_to_edit_or_reply_to.answer(error_text_to_show, reply_markup=None)
            else:
                await bot_message_to_edit_or_reply_to.answer(error_text_to_show, reply_markup=None)
        finally:
            logging.info(f"SRV_DEBUG: finalize_firefighter_shift_start: Clearing FSM state for emp_db_id: {employee_db_id}.")
            await state.clear()

# --- Обработчики для Водителя (Заступление) ---
async def process_vehicle_choice_for_shift(callback: types.CallbackQuery, state: FSMContext, session_factory: async_sessionmaker):
    await callback.answer()
    
    if callback.data == "no_vehicles_for_shift":
        logging.info(f"SRV_DEBUG: process_vehicle_choice_for_shift: No vehicles available, process cancelled by user {callback.from_user.id}.")
        await callback.message.edit_text(
            "Нет доступных автомобилей. Заступление на караул невозможно без автомобиля. Процесс отменен.",
            reply_markup=None # Убираем кнопки
        )
        await state.clear() # Очищаем состояние FSM
        return

    try:
        vehicle_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        logging.error(f"SRV_DEBUG: process_vehicle_choice_for_shift: Error extracting vehicle_id from callback_data: {callback.data}")
        await callback.message.edit_text("Произошла ошибка выбора автомобиля. Попробуйте снова или отмените.", reply_markup=get_cancel_keyboard())
        # Не очищаем состояние, чтобы пользователь мог попробовать отменить через кнопку
        return

    async with session_factory() as session:
        logging.info(f"SRV_DEBUG: process_vehicle_choice_for_shift: Session CREATED LOCALLY for user {callback.from_user.id}.")
        vehicle = await session.get(Vehicle, vehicle_id)
        if not vehicle:
            logging.warning(f"SRV_DEBUG: process_vehicle_choice_for_shift: Vehicle ID {vehicle_id} not found for user {callback.from_user.id}.")
            await callback.message.edit_text("Выбранный автомобиль не найден. Процесс отменен.", reply_markup=None)
            await state.clear()
            return
        if vehicle.status != "available":
            logging.warning(f"SRV_DEBUG: process_vehicle_choice_for_shift: Vehicle ID {vehicle_id} ({vehicle.model}) is not available (status: {vehicle.status}) for user {callback.from_user.id}.")
            await callback.message.edit_text(
                f"Автомобиль {vehicle.model} ({vehicle.number_plate}) уже занят или недоступен. Выберите другой или отмените. Процесс отменен.",
                reply_markup=None # Можно предложить заново выбрать авто, если список был длинный, или просто отменить.
            )
            await state.clear() # Очищаем, т.к. выбор невалиден
            return
        
        # Если все хорошо, сохраняем vehicle_id в FSM
        await state.update_data(vehicle_id=vehicle_id)
        vehicle_info = f"{vehicle.model} ({vehicle.number_plate})" # vehicle здесь доступен
    
    # Сообщение и смена состояния вне блока сессии, если сессия больше не нужна
    logging.info(f"SRV_DEBUG: Водитель {callback.from_user.id} выбрал автомобиль: {vehicle_info} (ID: {vehicle_id}) для заступления.")
    await callback.message.edit_text(
        f"Выбран автомобиль: {vehicle_info}.\n"
        "Укажите ваш оперативный ход (например, 1 для первого хода, 2 для второго):",
        reply_markup=get_cancel_keyboard() # Кнопка отмены для текущего шага
    )
    await state.set_state(StartShiftStates.ENTERING_OPERATIONAL_PRIORITY)


async def process_operational_priority_input(message: types.Message, state: FSMContext):
    priority_str = message.text.strip()
    if not priority_str.isdigit() or int(priority_str) < 1:
        await message.answer(
            "Оперативный ход должен быть положительным числом. Пожалуйста, введите корректный номер:",
            reply_markup=get_cancel_keyboard()
        )
        return
    
    operational_priority = int(priority_str)
    await state.update_data(operational_priority=operational_priority)
    logging.info(f"Водитель {message.from_user.id} указал оперативный ход: {operational_priority}")

    await message.answer(
        "Введите начальные показания одометра (км):",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(StartShiftStates.ENTERING_START_ODOMETER)


async def process_start_odometer_input(message: types.Message, state: FSMContext):
    try:
        odometer = float(message.text.strip().replace(',', '.'))
        if odometer < 0:
            raise ValueError("Показания одометра не могут быть отрицательными.")
        await state.update_data(start_odometer=odometer)
        logging.info(f"Водитель {message.from_user.id} ввел начальный одометр: {odometer}")

        await message.answer(
            "Введите текущий остаток топлива в баке (л):",
            reply_markup=get_cancel_keyboard()
        )
        await state.set_state(StartShiftStates.ENTERING_START_FUEL_LEVEL)
    except ValueError as e:
        await message.answer(
            f"Ошибка ввода: {e}. Пожалуйста, введите числовое значение для одометра (например, 12345.6):",
            reply_markup=get_cancel_keyboard()
        )


async def process_start_fuel_level_input(message: types.Message, state: FSMContext, session_factory: async_sessionmaker):
    try:
        fuel = float(message.text.strip().replace(',', '.'))
        if fuel < 0:
            raise ValueError("Остаток топлива не может быть отрицательным.")
        
        await state.update_data(start_fuel_level=fuel)
        logging.info(f"SRV_DEBUG: Водитель {message.from_user.id} ввел начальный остаток топлива: {fuel}")
        
        # Все данные для водителя собраны, вызываем финализирующую функцию
        await finalize_driver_shift_start(message, state, session_factory)

    except ValueError as e:
        logging.warning(f"SRV_DEBUG: process_start_fuel_level_input: Invalid fuel input '{message.text}' by user {message.from_user.id}. Error: {e}")
        await message.answer(
            f"Ошибка ввода: {e}. Пожалуйста, введите числовое значение для остатка топлива (например, 60.5):",
            reply_markup=get_cancel_keyboard()
        )
    except Exception as e: # На случай других ошибок
        logging.exception(f"SRV_DEBUG: process_start_fuel_level_input: Unexpected error for user {message.from_user.id}")
        await message.answer("Произошла непредвиденная ошибка. Попробуйте снова или отмените.", reply_markup=get_cancel_keyboard())


async def finalize_driver_shift_start(message: types.Message, state: FSMContext, session_factory: async_sessionmaker):
    data = await state.get_data()
    employee_db_id = data.get('employee_db_id')
    _start_time = datetime.now()

    final_message_text_success_template = "✅ Вы успешно заступили на караул №{karakul_number} ({start_time}).\n" \
                                          "Автомобиль: {vehicle_info}\n" \
                                          "Оперативный ход: {op_priority}\n" \
                                          "Начальный одометр: {odo} км, Топливо: {fuel} л."
    final_message_text_error = "Произошла ошибка при заступлении на караул. Попробуйте позже."
    
    logging.info(f"SRV_DEBUG: finalize_driver_shift_start CALLED for emp_db_id: {employee_db_id}. Data: {data}")

    async with session_factory() as session:
        logging.info(f"SRV_DEBUG: finalize_driver_shift_start: Session CREATED LOCALLY for emp_db_id: {employee_db_id}")
        vehicle_obj_for_message = None # Для использования в сообщении после транзакции
        employee_obj_for_menu = None   # Для получения после транзакции

        try:
            async with session.begin():
                logging.info(f"SRV_DEBUG: finalize_driver_shift_start: Transaction block STARTED.")
                
                # Получаем автомобиль внутри транзакции, чтобы убедиться в его актуальном состоянии
                vehicle_in_transaction = await session.get(Vehicle, data['vehicle_id'])
                if not vehicle_in_transaction:
                    error_msg = f"Ошибка: выбранный автомобиль (ID: {data['vehicle_id']}) не найден в базе. Заступление отменено."
                    logging.error(f"SRV_DEBUG: finalize_driver_shift_start: {error_msg}")
                    raise ValueError(error_msg) # Вызовет откат
                
                if vehicle_in_transaction.status != 'available':
                    error_msg = f"Ошибка: автомобиль {vehicle_in_transaction.model} ({vehicle_in_transaction.number_plate}) уже занят или недоступен (статус: {vehicle_in_transaction.status}). Заступление отменено."
                    logging.error(f"SRV_DEBUG: finalize_driver_shift_start: {error_msg}")
                    raise ValueError(error_msg) # Вызовет откат
                
                vehicle_obj_for_message = vehicle_in_transaction # Сохраняем для использования в сообщении

                new_shift_db_entry = ShiftLog(
                    employee_id=employee_db_id,
                    karakul_number=data['karakul_number'],
                    start_time=_start_time,
                    status='active',
                    vehicle_id=data['vehicle_id'],
                    operational_priority=data['operational_priority'],
                    start_odometer=data['start_odometer'],
                    start_fuel_level=data['start_fuel_level']
                )
                session.add(new_shift_db_entry)
                logging.info(f"SRV_DEBUG: finalize_driver_shift_start: ShiftLog CREATED and ADDED: {new_shift_db_entry.__dict__}")

                vehicle_in_transaction.status = 'in_use' # Автомобиль занят
                session.add(vehicle_in_transaction)
                logging.info(f"SRV_DEBUG: finalize_driver_shift_start: Vehicle {vehicle_in_transaction.id} status updated to 'in_use'.")
            # --- КОММИТ/ОТКАТ ПРОИЗОШЕЛ ---
            logging.info(f"SRV_DEBUG: finalize_driver_shift_start: Transaction block COMMITTED (or rollbacked).")

            # Если мы здесь, транзакция успешна
            vehicle_info_str = f"{vehicle_obj_for_message.model} ({vehicle_obj_for_message.number_plate})"
            success_text = final_message_text_success_template.format(
                karakul_number=data['karakul_number'],
                start_time=_start_time.strftime('%d.%m.%Y %H:%M'),
                vehicle_info=vehicle_info_str,
                op_priority=data['operational_priority'],
                odo=data['start_odometer'],
                fuel=data['start_fuel_level']
            )
            await message.answer(success_text, reply_markup=ReplyKeyboardRemove())
            logging.info(f"Водитель {employee_db_id} заступил на караул. Данные: {data}")

            # Обновление меню
            async with session_factory() as menu_session: # Новая сессия для получения Employee
                employee_obj_for_menu = await menu_session.get(Employee, employee_db_id)

            if employee_obj_for_menu:
                from app.menu import show_role_specific_menu
                await show_role_specific_menu(message, employee_obj_for_menu.id, employee_obj_for_menu.position)
                logging.info(f"SRV_DEBUG: Menu updated for driver {employee_db_id}.")
            else:
                logging.error(f"SRV_DEBUG: Could not get employee_obj_for_menu for driver {employee_db_id} to update menu.")

        except ValueError as ve:
            await message.answer(str(ve), reply_markup=None)
        except Exception as e:
            logging.exception(f"SRV_DEBUG: finalize_driver_shift_start: UNEXPECTED EXCEPTION for emp_db_id {employee_db_id}")
            await message.answer(final_message_text_error, reply_markup=None)
        finally:
            logging.info(f"SRV_DEBUG: finalize_driver_shift_start: Clearing FSM state for emp_db_id: {employee_db_id}.")
            await state.clear()

# app/shift_management.py
async def finalize_generic_shift_end(message: types.Message, state: FSMContext, session_factory: async_sessionmaker):
    data = await state.get_data()
    active_shift_id = data.get('active_shift_id')
    employee_db_id = data.get('employee_db_id')

    if not active_shift_id or not employee_db_id:
        logging.error(f"finalize_generic_shift_end: Отсутствует active_shift_id или employee_db_id в FSM для user {message.from_user.id}")
        await message.answer("Произошла внутренняя ошибка. Не удалось завершить караул.")
        await state.clear()
        return

    # Импортируем show_role_specific_menu здесь, чтобы избежать циклического импорта на уровне модуля
    from app.menu import show_role_specific_menu

    async with session_factory() as session: # <--- СОЗДАЕМ СЕССИЮ
        try:
            async with session.begin():
                shift_to_end = await session.get(ShiftLog, active_shift_id)
                if shift_to_end and shift_to_end.employee_id == employee_db_id and shift_to_end.status == 'active':
                    # ... (логика обновления shift_to_end) ...
                    shift_to_end.end_time = datetime.now()
                    shift_to_end.status = 'completed'
                    session.add(shift_to_end)
                    
                    _karakul_number = shift_to_end.karakul_number # Сохраняем для сообщения
                    _end_time_str = shift_to_end.end_time.strftime('%d.%m.%Y %H:%M')
                else:
                    await message.answer("Не удалось найти ваш активный караул для завершения или он уже завершен.")
                    await state.clear() # Очищаем состояние, так как операция не удалась
                    return
            # Коммит произошел

            await message.answer(
                f"✅ Караул №{_karakul_number} успешно завершен ({_end_time_str}).",
                reply_markup=ReplyKeyboardRemove()
            )
            logging.info(f"Сотрудник {employee_db_id} завершил караул ID {active_shift_id} (общий сценарий).")
            
            # Для обновления меню нужен объект Employee
            async with session_factory() as menu_session: # Новая сессия для получения Employee
                employee_obj_for_menu = await menu_session.get(Employee, employee_db_id)

            if employee_obj_for_menu:
                await show_role_specific_menu(message, employee_obj_for_menu.id, employee_obj_for_menu.position)
            else:
                logging.error(f"Не удалось получить employee_obj для {employee_db_id} при обновлении меню (generic end).")

        except Exception as e:
            logging.exception(f"Ошибка при общем завершении караула {active_shift_id} для {employee_db_id}: {e}")
            await message.answer("Произошла ошибка при завершении караула.")
        finally:
            await state.clear()


# В app/shift_management.py

# ... (все импорты, StartShiftStates, EndShiftStates, все функции для заступления,
# handle_end_shift_request, finalize_generic_shift_end) ...

# --- Обработчики для Окончания Караула Водителя ---
async def process_end_odometer_input(message: types.Message, state: FSMContext, session_factory: async_sessionmaker):
    try:
        end_odometer = float(message.text.strip().replace(',', '.'))
        data = await state.get_data()
        
        async with session_factory() as session: # <--- СОЗДАЕМ СЕССИЮ
            active_shift = await session.get(ShiftLog, data.get('active_shift_id'))
            if not active_shift or active_shift.start_odometer is None:
                await message.answer("Ошибка: не найдены начальные данные по одометру...")
                await state.clear()
                return
            if end_odometer < active_shift.start_odometer:
                await message.answer(f"Конечные показания одометра ({end_odometer} км) не могут быть меньше начальных ({active_shift.start_odometer} км)...", reply_markup=get_cancel_keyboard())
                return
        
        await state.update_data(end_odometer=end_odometer)
        logging.info(f"Водитель {message.from_user.id} ввел конечный одометр: {end_odometer}")

        await message.answer(
            "Введите конечный остаток топлива в баке (л):",
            reply_markup=get_cancel_keyboard()
        )
        await state.set_state(EndShiftStates.ENTERING_END_FUEL_LEVEL)

    except ValueError:
        await message.answer(
            "Пожалуйста, введите числовое значение для одометра (например, 123456.7):",
            reply_markup=get_cancel_keyboard()
        )
    except Exception as e:
        logging.exception(f"Ошибка в process_end_odometer_input для {message.from_user.id}: {e}")
        await message.answer("Произошла ошибка при обработке данных. Попробуйте снова.")
        # Можно не очищать состояние, чтобы пользователь мог попробовать еще раз тот же шаг
        # await state.clear()


async def process_end_fuel_level_input(message: types.Message, state: FSMContext, session_factory: async_sessionmaker):
    try:
        end_fuel_level = float(message.text.strip().replace(',', '.'))
        if end_fuel_level < 0: raise ValueError("Остаток топлива не может быть отрицательным.")
        await state.update_data(end_fuel_level=end_fuel_level)
        logging.info(f"Водитель {message.from_user.id} ввел конечный остаток топлива: {end_fuel_level}")

        # Все данные для окончания караула водителя собраны
        await finalize_driver_shift_end(message, state, session_factory)

    except ValueError as e:
        await message.answer(
            f"Ошибка ввода: {e}. Пожалуйста, введите числовое значение для остатка топлива (например, 50.2):",
            reply_markup=get_cancel_keyboard()
        )
    except Exception as e:
        logging.exception(f"Ошибка в process_end_fuel_level_input для {message.from_user.id}: {e}")
        await message.answer("Произошла ошибка при обработке данных. Попробуйте снова.")


async def finalize_driver_shift_end(message: types.Message, state: FSMContext, session_factory: async_sessionmaker):
    data = await state.get_data()
    active_shift_id = data.get('active_shift_id')
    employee_db_id = data.get('employee_db_id') # Этот ID должен быть в FSM data

    logging.info(f"SRV_DEBUG: finalize_driver_shift_end CALLED for employee_db_id: {employee_db_id}, shift_id: {active_shift_id}. Data: {data}")

    if not active_shift_id or not employee_db_id:
        logging.error(f"SRV_DEBUG: finalize_driver_shift_end: Missing active_shift_id or employee_db_id in FSM for user {message.from_user.id}")
        await message.answer("Произошла внутренняя ошибка (отсутствуют данные о карауле). Не удалось завершить караул.")
        await state.clear()
        return

    # Импортируем show_role_specific_menu здесь, чтобы избежать циклического импорта
    from app.menu import show_role_specific_menu
    
    employee_obj_for_menu = None # Для обновления меню в конце
    # Переменные для сообщения пользователю
    ended_karakul_number = "N/A"
    ended_time_str = "N/A"
    mileage_for_shift_str = "N/A"
    fuel_consumed_str = "N/A"

    async with session_factory() as session: # Создаем сессию локально
        logging.info(f"SRV_DEBUG: finalize_driver_shift_end: Session CREATED LOCALLY.")
        try:
            async with session.begin(): # Начинаем транзакцию
                logging.info(f"SRV_DEBUG: finalize_driver_shift_end: Transaction block STARTED.")
                shift_to_end = await session.get(ShiftLog, active_shift_id)

                if not (shift_to_end and shift_to_end.employee_id == employee_db_id and shift_to_end.status == 'active'):
                    error_msg = "Не удалось найти ваш активный караул для завершения или он уже завершен."
                    logging.warning(f"SRV_DEBUG: finalize_driver_shift_end: {error_msg} (emp_id: {employee_db_id}, shift_id: {active_shift_id})")
                    # Вызываем ValueError, чтобы откатить транзакцию и показать сообщение
                    raise ValueError(error_msg)

                shift_to_end.end_time = datetime.now()
                shift_to_end.status = 'completed'
                shift_to_end.end_odometer = data.get('end_odometer')
                shift_to_end.end_fuel_level = data.get('end_fuel_level')

                # Расчеты
                mileage_for_shift = 0.0
                fuel_consumed_for_shift = 0.0
                if shift_to_end.start_odometer is not None and shift_to_end.end_odometer is not None:
                    mileage_for_shift = round(shift_to_end.end_odometer - shift_to_end.start_odometer, 2)
                if shift_to_end.start_fuel_level is not None and shift_to_end.end_fuel_level is not None:
                    fuel_consumed_for_shift = round(shift_to_end.start_fuel_level - shift_to_end.end_fuel_level, 2)
                
                mileage_for_shift_str = f"{mileage_for_shift} км"
                fuel_consumed_str = f"{fuel_consumed_for_shift} л"

                # Обновление статуса автомобиля
                if shift_to_end.vehicle_id is not None:
                    vehicle = await session.get(Vehicle, shift_to_end.vehicle_id)
                    if vehicle:
                        # TODO: Добавить более сложную логику, если необходимо (проверка других активных караулов на этом авто)
                        vehicle.status = 'available' # Предполагаем, что авто становится доступным
                        session.add(vehicle)
                        logging.info(f"SRV_DEBUG: finalize_driver_shift_end: Vehicle {vehicle.id} status updated to 'available'.")
                    else:
                        logging.warning(f"SRV_DEBUG: finalize_driver_shift_end: Vehicle ID {shift_to_end.vehicle_id} not found for status update.")
                
                session.add(shift_to_end)
                logging.info(f"SRV_DEBUG: finalize_driver_shift_end: ShiftLog {active_shift_id} updated and added to session.")
                
                # Сохраняем данные для сообщения перед коммитом
                ended_karakul_number = shift_to_end.karakul_number
                ended_time_str = shift_to_end.end_time.strftime('%d.%m.%Y %H:%M')

            # --- КОММИТ ПРОИЗОШЕЛ (или rollback при ошибке внутри блока session.begin()) ---
            logging.info(f"SRV_DEBUG: finalize_driver_shift_end: Transaction block COMMITTED (or rollbacked).")

            # Сообщение пользователю (если транзакция прошла успешно)
            await message.answer(
                f"✅ Караул №{ended_karakul_number} успешно завершен ({ended_time_str}).\n"
                f"Пробег за караул: {mileage_for_shift_str}.\n"
                f"Расход топлива за караул (разница остатков): {fuel_consumed_str}.",
                reply_markup=ReplyKeyboardRemove()
            )
            logging.info(f"Водитель {employee_db_id} завершил караул ID {active_shift_id}. Данные: {data}")

            # Получаем объект Employee для обновления меню в новой сессии, чтобы избежать проблем с detached instance
            async with session_factory() as menu_session:
                employee_obj_for_menu = await menu_session.get(Employee, employee_db_id)
            
            if employee_obj_for_menu:
                await show_role_specific_menu(message, employee_obj_for_menu.id, employee_obj_for_menu.position)
                logging.info(f"SRV_DEBUG: Menu updated for driver {employee_db_id} after shift end.")
            else:
                logging.error(f"SRV_DEBUG: Could not get employee_obj_for_menu for driver {employee_db_id} to update menu.")

        except ValueError as ve: # Перехватываем ошибки, которые мы сами вызываем для отката
            logging.warning(f"SRV_DEBUG: finalize_driver_shift_end: ValueError caught: {ve}")
            await message.answer(str(ve), reply_markup=None)
        except Exception as e: # Другие непредвиденные ошибки
            logging.exception(f"SRV_DEBUG: finalize_driver_shift_end: UNEXPECTED EXCEPTION for employee_db_id {employee_db_id}")
            await message.answer("Произошла ошибка при завершении караула водителя.", reply_markup=None)
        finally:
            logging.info(f"SRV_DEBUG: finalize_driver_shift_end: Clearing FSM state for employee_db_id: {employee_db_id}.")
            await state.clear()

# --- Обертки для передачи сессии в обработчики этого модуля ---
async def firefighter_sizod_number_wrapper(message: types.Message, state: FSMContext):
    async with async_session() as session:
        await process_sizod_number_input(message, state, session)

async def firefighter_sizod_status_wrapper(callback: types.CallbackQuery, state: FSMContext):
    async with async_session() as session:
        await process_sizod_status_start_choice(callback, state, session)

async def firefighter_skip_notes_wrapper(callback: types.CallbackQuery, state: FSMContext):
    async with async_session() as session:
        await process_skip_sizod_notes_start(callback, state, session)

async def firefighter_sizod_notes_wrapper(message: types.Message, state: FSMContext):
    async with async_session() as session:
        await process_sizod_notes_start_input(message, state, session)

async def driver_vehicle_choice_wrapper(callback: types.CallbackQuery, state: FSMContext):
    async with async_session() as session:
        await process_vehicle_choice_for_shift(callback, state, session)

async def driver_start_fuel_wrapper(message: types.Message, state: FSMContext):
    async with async_session() as session:
        await process_start_fuel_level_input(message, state, session)

async def driver_end_odometer_wrapper(message: types.Message, state: FSMContext):
    async with async_session() as session:
        await process_end_odometer_input(message, state, session)

async def driver_end_fuel_wrapper(message: types.Message, state: FSMContext):
    async with async_session() as session:
        await process_end_fuel_level_input(message, state, session)

# --- Регистрация обработчиков для этого модуля ---
def register_shift_management_handlers(router: Router):
    # Обертки для передачи сессии регистрируются в app/__init__.py

    # --- Обработчики для Заступления Пожарного ---
    router.message.register(firefighter_sizod_number_wrapper, StartShiftStates.ENTERING_SIZOD_NUMBER) # Обертка из app/__init__.py
    router.callback_query.register(
        firefighter_sizod_status_wrapper, # Обертка из app/__init__.py
        F.data.startswith("sizod_status_start_"),
        StartShiftStates.CHOOSING_SIZOD_STATUS_START
    )
    router.callback_query.register(
        firefighter_skip_notes_wrapper, # Обертка из app/__init__.py
        F.data == "skip_sizod_notes_start",
        StartShiftStates.ENTERING_SIZOD_NOTES_START
    )
    router.message.register(firefighter_sizod_notes_wrapper, StartShiftStates.ENTERING_SIZOD_NOTES_START) # Обертка из app/__init__.py

    # --- Обработчики для Заступления Водителя ---
    router.callback_query.register(
        driver_vehicle_choice_wrapper, # Обертка из app/__init__.py
        F.data.startswith("start_shift_vehicle_") | (F.data == "no_vehicles_for_shift"),
        StartShiftStates.CHOOSING_VEHICLE
    )
    router.message.register(
        process_operational_priority_input, # Сессия не нужна, обертка не обязательна
        StartShiftStates.ENTERING_OPERATIONAL_PRIORITY
    )
    router.message.register(
        process_start_odometer_input, # Сессия не нужна, обертка не обязательна
        StartShiftStates.ENTERING_START_ODOMETER
    )
    router.message.register(driver_start_fuel_wrapper, StartShiftStates.ENTERING_START_FUEL_LEVEL) # Обертка из app/__init__.py

        # --- Обработчики для Окончания Караула Водителя ---
    async def driver_end_odometer_wrapper(message: types.Message, state: FSMContext):
        async with async_session() as session:
            await process_end_odometer_input(message, state, session)
    router.message.register(driver_end_odometer_wrapper, EndShiftStates.ENTERING_END_ODOMETER)

    async def driver_end_fuel_wrapper(message: types.Message, state: FSMContext):
        async with async_session() as session:
            await process_end_fuel_level_input(message, state, session)
    router.message.register(driver_end_fuel_wrapper, EndShiftStates.ENTERING_END_FUEL_LEVEL)

    async def firefighter_end_sizod_status_wrapper(callback: types.CallbackQuery, state: FSMContext):
        async with async_session() as session:
            await process_sizod_status_end_choice(callback, state, session)

    async def firefighter_end_skip_notes_wrapper(callback: types.CallbackQuery, state: FSMContext):
        async with async_session() as session:
            await process_skip_sizod_notes_end(callback, state, session)

    async def firefighter_end_sizod_notes_wrapper(message: types.Message, state: FSMContext):
        async with async_session() as session:
            await process_sizod_notes_end_input(message, state, session)

    # --- Регистрация обработчиков для ОКОНЧАНИЯ караула ПОЖАРНЫМ ---
    router.callback_query.register(
        firefighter_end_sizod_status_wrapper,
        F.data.startswith("sizod_status_end_"), # <--- Правильный префикс
        EndShiftStates.CHOOSING_SIZOD_STATUS_END
    )
    router.callback_query.register(
        firefighter_end_skip_notes_wrapper,
        F.data == "skip_sizod_notes_end",
        EndShiftStates.ENTERING_SIZOD_NOTES_END
    )
    router.message.register(
        firefighter_end_sizod_notes_wrapper,
        EndShiftStates.ENTERING_SIZOD_NOTES_END
    )

# --- Обработчики для Окончания Караула Пожарного ---
async def process_sizod_status_end_choice(callback: types.CallbackQuery, state: FSMContext, session_factory: async_sessionmaker):
    await callback.answer()
    status_choice = callback.data.split('_')[-1].lower() # исправен или неисправен
    await state.update_data(sizod_status_end=status_choice.capitalize()) # 'Исправен' или 'Неисправен'
    logging.info(f"SRV_DEBUG: Пожарный {callback.from_user.id} (окончание) выбрал состояние СИЗОД: {status_choice}, callback: {callback.data}")

    if status_choice == "неисправен":
        await callback.message.edit_text(
            "Пожалуйста, кратко опишите неисправность СИЗОД при сдаче (или нажмите 'Пропустить', если описание не требуется):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➡️ Пропустить описание", callback_data="skip_sizod_notes_end")],
                [InlineKeyboardButton(text="❌ Отменить окончание", callback_data="universal_cancel")] # Используем универсальную отмену
            ])
        )
        await state.set_state(EndShiftStates.ENTERING_SIZOD_NOTES_END)
    else: # Исправен
        await state.update_data(sizod_notes_end=None)
        await finalize_firefighter_shift_end( # <--- Передаем session_factory
            state=state,
            session_factory=session_factory,
            bot_message_to_edit_or_reply_to=callback.message,
            is_from_callback=True
        )

async def process_skip_sizod_notes_end(callback: types.CallbackQuery, state: FSMContext, session_factory: async_sessionmaker): # Принимает session_factory
    await callback.answer()
    await state.update_data(sizod_notes_end="Описание пропущено при сдаче")
    logging.info(f"SRV_DEBUG: Пожарный {callback.from_user.id} (окончание) пропустил описание неисправности СИЗОД.")
    
    # Вызываем finalize_firefighter_shift_end, передавая session_factory
    await finalize_firefighter_shift_end(
        state=state,
        session_factory=session_factory, # <--- Передаем session_factory
        bot_message_to_edit_or_reply_to=callback.message,
        is_from_callback=True
    )

async def process_sizod_notes_end_input(message: types.Message, state: FSMContext, session_factory: async_sessionmaker): # Принимает session_factory
    notes = message.text.strip()
    if not notes: # Добавим простую проверку на пустые заметки, если нужно
        await message.answer("Заметки не могут быть пустыми. Пожалуйста, введите описание или отмените действие.")
        # Можно добавить кнопку отмены или предложить пропустить, если такая логика нужна
        return

    await state.update_data(sizod_notes_end=notes)
    logging.info(f"SRV_DEBUG: Пожарный {message.from_user.id} (окончание) добавил примечания к СИЗОД: {notes}")

    # Вызываем finalize_firefighter_shift_end, передавая session_factory
    await finalize_firefighter_shift_end(
        state=state,
        session_factory=session_factory, # <--- Передаем session_factory
        bot_message_to_edit_or_reply_to=message, # Это message, а не callback.message
        is_from_callback=False
    )

async def finalize_firefighter_shift_end(
    state: FSMContext,
    session_factory: async_sessionmaker, # <--- Убедитесь, что здесь ПРИНИМАЕТСЯ session_factory
    bot_message_to_edit_or_reply_to: types.Message,
    is_from_callback: bool
):
    data = await state.get_data()
    active_shift_id = data.get('active_shift_id')
    employee_db_id = data.get('employee_db_id')
    _end_time = datetime.now()

    logging.info(f"SRV_DEBUG: finalize_firefighter_shift_end CALLED for employee_db_id: {employee_db_id}, shift_id: {active_shift_id}. Data: {data}")

    final_message_text_success_template = "✅ Караул №{karakul_number} успешно завершен ({end_time}).\n" \
                                          "СИЗОД №{sizod_number} сдан в состоянии: {sizod_status_end}."
    final_message_text_error = "Произошла ошибка при завершении караула пожарным."
    employee_obj_for_menu = None
    # Переменные для использования после транзакции, если нужно
    _karakul_number_for_msg = "N/A"
    _sizod_number_for_msg = "N/A"


    async with session_factory() as session: # <--- СОЗДАЕМ СЕССИЮ ЗДЕСЬ
        logging.info(f"SRV_DEBUG: finalize_firefighter_shift_end: Session CREATED LOCALLY.")
        try:
            async with session.begin(): # Начинаем транзакцию на созданной сессии
                logging.info(f"SRV_DEBUG: finalize_firefighter_shift_end: Transaction block STARTED.")
                shift_to_end = await session.get(ShiftLog, active_shift_id) # <--- Теперь session это AsyncSession

                if not (shift_to_end and shift_to_end.employee_id == employee_db_id and shift_to_end.status == 'active'):
                    error_msg = "Не удалось найти ваш активный караул для завершения или он уже завершен."
                    logging.error(f"SRV_DEBUG: finalize_firefighter_shift_end: {error_msg}")
                    raise ValueError(error_msg)

                shift_to_end.end_time = _end_time
                shift_to_end.status = 'completed'
                shift_to_end.sizod_status_end = data.get('sizod_status_end')
                shift_to_end.sizod_notes_end = data.get('sizod_notes_end')
                session.add(shift_to_end)
                logging.info(f"SRV_DEBUG: finalize_firefighter_shift_end: ShiftLog {active_shift_id} updated.")

                # Сохраняем значения для сообщения перед выходом из транзакции
                _karakul_number_for_msg = shift_to_end.karakul_number
                _sizod_number_for_msg = shift_to_end.sizod_number

                # Обновляем Equipment и создаем EquipmentLog
                if shift_to_end.sizod_number:
                    equipment = await session.scalar(
                        select(Equipment).where(Equipment.inventory_number == shift_to_end.sizod_number, Equipment.type == 'СИЗОД')
                    )
                    if equipment:
                        if equipment.current_holder_id == employee_db_id:
                            equipment.current_holder_id = None
                            equipment.status = 'available' if data.get('sizod_status_end') == 'Исправен' else 'maintenance'
                            session.add(equipment)
                            logging.info(f"SRV_DEBUG: finalize_firefighter_shift_end: Equipment {equipment.id} status set to {equipment.status}, holder removed.")

                            equip_log_notes = f"Сдан с караула №{shift_to_end.karakul_number}. Конечное состояние: {data.get('sizod_status_end', 'N/A')}. "
                            if data.get('sizod_notes_end') and data.get('sizod_notes_end') != "Описание пропущено при сдаче":
                                equip_log_notes += f"Примечание: {data['sizod_notes_end']}"
                            else:
                                equip_log_notes += "Примечание: нет"
                            
                            equip_log = EquipmentLog(
                                employee_id=employee_db_id, equipment_id=equipment.id, action='returned',
                                notes=equip_log_notes, shift_log_id=active_shift_id
                            )
                            session.add(equip_log)
                            logging.info(f"SRV_DEBUG: finalize_firefighter_shift_end: EquipmentLog created for SIZOD return.")
                        else:
                            logging.warning(f"SRV_DEBUG: finalize_firefighter_shift_end: SIZOD {shift_to_end.sizod_number} was not held by employee {employee_db_id}...")
                    else:
                        logging.warning(f"SRV_DEBUG: finalize_firefighter_shift_end: SIZOD {shift_to_end.sizod_number} not found in Equipment table...")
            # --- Транзакция завершена (commit или rollback) ---
            logging.info(f"SRV_DEBUG: finalize_firefighter_shift_end: Transaction block COMMITTED (or rollbacked).")

            # Если мы здесь, значит транзакция (вероятно) прошла успешно
            success_text = final_message_text_success_template.format(
                karakul_number=_karakul_number_for_msg,
                end_time=_end_time.strftime('%d.%m.%Y %H:%M'),
                sizod_number=_sizod_number_for_msg,
                sizod_status_end=data.get('sizod_status_end', 'N/A')
            )
            if is_from_callback:
                await bot_message_to_edit_or_reply_to.edit_text(success_text, reply_markup=None)
            else:
                await bot_message_to_edit_or_reply_to.answer(success_text, reply_markup=None)
            logging.info(f"Пожарный {employee_db_id} завершил караул ID {active_shift_id} со сдачей СИЗОД.")

            # Обновление меню (сессия для employee_obj_for_menu)
            async with session_factory() as menu_session: # Новая сессия для получения Employee
                employee_obj_for_menu = await menu_session.get(Employee, employee_db_id)

            if employee_obj_for_menu:
                from app.menu import show_role_specific_menu # Локальный импорт
                await show_role_specific_menu(bot_message_to_edit_or_reply_to, employee_obj_for_menu.id, employee_obj_for_menu.position)
            else:
                logging.error(f"SRV_DEBUG: finalize_firefighter_shift_end: Could not get employee_obj_for_menu to update menu.")

        except ValueError as ve: # Ошибки, которые мы сами вызываем для отката
            error_text_to_show = str(ve)
            if is_from_callback:
                try: await bot_message_to_edit_or_reply_to.edit_text(error_text_to_show, reply_markup=None)
                except: await bot_message_to_edit_or_reply_to.answer(error_text_to_show, reply_markup=None)
            else:
                await bot_message_to_edit_or_reply_to.answer(error_text_to_show, reply_markup=None)
        except Exception as e: # Непредвиденные ошибки
            logging.exception(f"SRV_DEBUG: finalize_firefighter_shift_end: UNEXPECTED EXCEPTION.")
            error_text_to_show = final_message_text_error
            if is_from_callback:
                try: await bot_message_to_edit_or_reply_to.edit_text(error_text_to_show, reply_markup=None)
                except: await bot_message_to_edit_or_reply_to.answer(error_text_to_show, reply_markup=None)
            else:
                await bot_message_to_edit_or_reply_to.answer(error_text_to_show, reply_markup=None)
        finally:
            logging.info(f"SRV_DEBUG: finalize_firefighter_shift_end: Clearing FSM state.")
            await state.clear()