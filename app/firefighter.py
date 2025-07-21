import json
from aiogram import F, types, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload
from models import async_session, Employee, Equipment, EquipmentLog, DispatchOrder, Vehicle
from app.keyboards import (
    get_equipment_log_main_keyboard,
    get_equipment_log_action_keyboard,
    get_equipment_selection_keyboard,
    get_readiness_toggle_keyboard
)
from app.shift_management import get_active_shift
from app.dispatcher import ACTIVE_DISPATCH_STATUSES, STATUS_TRANSLATIONS

import logging
from aiogram.filters import StateFilter
# --- Состояния FSM для журнала снаряжения ---
class EquipmentLogStates(StatesGroup):
    CHOOSING_LOG_MAIN_ACTION = State() # Ожидание выбора "Новая запись" / "Мои записи"
    CHOOSING_LOG_ACTION = State()      # Ожидание выбора действия (Взять/Вернуть...)
    SELECTING_EQUIPMENT = State()    # Ожидание выбора снаряжения
    # WAITING_FOR_NOTES = State() # Для будущих примечаний

# --- Обработчики ---

async def handle_equipment_log_button(message: types.Message, state: FSMContext):
    """Обработчик нажатия кнопки '🧯 Журнал снаряжения'."""
    await state.clear() # Очищаем предыдущее состояние FSM на всякий случай
    await message.answer(
        "Журнал учета снаряжения:",
        reply_markup=get_equipment_log_main_keyboard()
    )
    await state.set_state(EquipmentLogStates.CHOOSING_LOG_MAIN_ACTION)

async def handle_log_main_action(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора в главном меню журнала ('Новая запись'/'Назад')."""
    await callback.answer()
    if callback.data == "log_new_entry":
        await callback.message.edit_text(
            "Выберите действие:",
            reply_markup=get_equipment_log_action_keyboard()
        )
        # --- ЛОГИРОВАНИЕ ---
        await state.set_state(EquipmentLogStates.CHOOSING_LOG_ACTION)
        logging.info(f"Установлено состояние: {await state.get_state()} для user {callback.from_user.id} (ожидание выбора действия)")
        # --- КОНЕЦ ЛОГИРОВАНИЯ ---
    elif callback.data == "log_back_to_main":
        await callback.message.delete()
        await state.clear()

async def process_equipment_log_action(callback: types.CallbackQuery, state: FSMContext, session_factory: async_sessionmaker):
    """Обработка выбора действия (Взять/Вернуть...). Фильтрует снаряжение."""
    await callback.answer()
    action = callback.data.split('_')[-1] 
    await state.update_data(log_action=action)
    user_id = callback.from_user.id

    async with session_factory() as session: # <--- Создаем сессию из session_factory
        try:
            employee = await session.scalar( # Используем scalar для одного объекта
                select(Employee).where(Employee.telegram_id == user_id)
            )
            if not employee:
                await callback.message.edit_text("Ошибка: Ваш профиль не найден.")
                await state.clear()
                return

            # --- Фильтрация снаряжения ---
            stmt = select(Equipment)
            if action == 'taken':
                # Показываем только доступное
                stmt = stmt.where(Equipment.status == 'available')
                action_description = "взять"
            elif action == 'returned':
                # Показываем только то, что числится за этим сотрудником
                stmt = stmt.where(Equipment.status == 'in_use', Equipment.current_holder_id == employee.id)
                action_description = "вернуть"
            elif action == 'checked':
                # Показываем всё, кроме списанного (например)
                stmt = stmt.where(Equipment.status != 'decommissioned')
                action_description = "проверить"
            else:
                # На случай других действий - пока показываем всё доступное
                stmt = stmt.where(Equipment.status == 'available')
                action_description = action # Используем само название действия

            stmt = stmt.order_by(Equipment.name)
            result = await session.execute(stmt)
            equipment_list = result.scalars().all()

            # Клавиатура генерируется даже если список пуст (покажет "Нет доступного...")
            keyboard = get_equipment_selection_keyboard(equipment_list, action)

            if not equipment_list:
                 # Сообщаем, почему список пуст
                 if action == 'taken':
                     msg = "Нет доступного для получения снаряжения."
                 elif action == 'returned':
                     msg = "Нет снаряжения, числящегося за вами, которое можно вернуть."
                 else:
                     msg = "Нет снаряжения для выбранного действия."
                 await callback.message.edit_text(msg, reply_markup=keyboard) # Показываем клавиатуру с кнопкой отмены
                 await state.set_state(EquipmentLogStates.SELECTING_EQUIPMENT) # Остаемся в состоянии выбора
                 logging.info(f"Установлено состояние: {await state.get_state()} для user {callback.from_user.id} (список снаряжения пуст, ожидание отмены)")
                 return

            await callback.message.edit_text(
                f"Выберите снаряжение, которое хотите {action_description}:",
                reply_markup=keyboard
            )
            await state.set_state(EquipmentLogStates.SELECTING_EQUIPMENT)
            logging.info(f"Установлено состояние: {await state.get_state()} для user {callback.from_user.id} (ожидание выбора снаряжения)")

        except Exception as e:
            logging.exception(f"Ошибка при получении списка снаряжения для действия {action}: {e}")
            await callback.message.edit_text("Не удалось получить список снаряжения.")
            await state.clear()

async def process_equipment_selection(callback: types.CallbackQuery, state: FSMContext, session_factory: async_sessionmaker):
    await callback.answer()
    log_message_text = "Произошла ошибка."
    try:
        # ... (извлечение action, equipment_id и т.д. из callback.data) ...
        parts = callback.data.split('_')
        action = parts[-2]
        equipment_id = int(parts[-1])
        user_telegram_id = callback.from_user.id
        fsm_data = await state.get_data()
        stored_action = fsm_data.get('log_action')

        if action != stored_action: # ... обработка ошибки ...
            await state.clear(); return

        active_shift_id_for_log = None
        employee_db_id_for_shift_check = None # Нужен ID для get_active_shift ДО основного begin()

        # Сначала получим employee_db_id, чтобы можно было вызвать get_active_shift
        async with session_factory() as temp_session:
            employee_check = await temp_session.scalar(
                select(Employee.id).where(Employee.telegram_id == user_telegram_id)
            )
            if not employee_check:
                log_message_text = "Ошибка: Ваш профиль не найден в системе (предварительная проверка)."
                await callback.message.edit_text(log_message_text, reply_markup=None)
                await state.clear()
                return
            employee_db_id_for_shift_check = employee_check

        # Теперь проверяем активный караул, используя отдельную логику get_active_shift
        current_active_shift = await get_active_shift(session_factory, employee_db_id_for_shift_check)
        if current_active_shift:
            active_shift_id_for_log = current_active_shift.id
            logging.info(f"Сотрудник {employee_db_id_for_shift_check} на активном карауле ID: {active_shift_id_for_log}.")
        else:
            logging.info(f"Сотрудник {employee_db_id_for_shift_check} не на активном карауле.")


        # Основная логика теперь вся внутри одного блока session и session.begin
        async with session_factory() as session:
            logging.info(f"SRV_DEBUG: process_equipment_selection: Main session CREATED. Is active? {session.in_transaction()}")
            
            # "Хак" оставляем на всякий случай, но он не должен срабатывать, если теория верна
            if session.in_transaction():
                logging.warning(f"SRV_DEBUG: process_equipment_selection (main block): Transaction was unexpectedly active. Attempting to commit.")
                try:
                    await session.commit()
                    logging.info(f"SRV_DEBUG: process_equipment_selection (main block): Pre-existing transaction committed. Is now active? {session.in_transaction()}")
                except Exception as e_commit_main:
                    logging.error(f"SRV_DEBUG: process_equipment_selection (main block): Failed to commit pre-existing transaction: {e_commit_main}")
                    await session.rollback()
            
            logging.info(f"SRV_DEBUG: process_equipment_selection: BEFORE main session.begin() - Is transaction active? {session.in_transaction()}")
            async with session.begin(): # Начинаем основную транзакцию
                # 1. Получаем ПОЛНЫЙ объект сотрудника ВНУТРИ транзакции
                employee = await session.scalar(
                    select(Employee).where(Employee.telegram_id == user_telegram_id)
                )
                if not employee: # Повторная проверка на всякий случай, хотя уже делали
                    log_message_text = "Ошибка: Ваш профиль не найден в системе."
                    raise ValueError(log_message_text) # Откатит транзакцию
                
                employee_db_id = employee.id # Используем этот ID для EquipmentLog

                # 2. Логика работы со снаряжением
                equipment = await session.get(Equipment, equipment_id)
                if not equipment:
                    log_message_text = "Ошибка: Выбранное снаряжение не найдено."
                    raise ValueError(log_message_text)

                action_successful = False
                if action == 'taken':
                    # ... (логика для 'taken')
                    if equipment.status == 'available':
                        equipment.status = 'in_use'; equipment.current_holder_id = employee_db_id
                        session.add(equipment); action_successful = True
                        log_message_text = f"✅ Вы взяли: {equipment.name}"
                    else: log_message_text = f"❌ Ошибка: Снаряжение '{equipment.name}' уже используется."
                elif action == 'returned':
                    # ... (логика для 'returned')
                    if equipment.status == 'in_use' and equipment.current_holder_id == employee_db_id:
                        equipment.status = 'available'; equipment.current_holder_id = None
                        session.add(equipment); action_successful = True
                        log_message_text = f"✅ Вы вернули: {equipment.name}"
                    elif equipment.current_holder_id != employee_db_id: log_message_text = f"❌ Ошибка: Вы не можете вернуть '{equipment.name}'..."
                    else: log_message_text = f"❌ Ошибка: Снаряжение '{equipment.name}' не числится как используемое."
                elif action == 'checked':
                    # ... (логика для 'checked')
                    action_successful = True; log_message_text = f"✅ Вы проверили: {equipment.name}"

                if action_successful:
                    notes_for_log = f"Действие через журнал."
                    if active_shift_id_for_log: notes_for_log += f" Караул ID: {active_shift_id_for_log}"
                    else: notes_for_log += " Вне караула"
                    
                    new_log_entry = EquipmentLog(
                        employee_id=employee_db_id, equipment_id=equipment_id, action=action,
                        notes=notes_for_log, shift_log_id=active_shift_id_for_log
                    )
                    session.add(new_log_entry)
                    logging.info(f"Лог снаряжения: ... shift_id={active_shift_id_for_log}")
                else:
                    logging.warning(f"Действие с снаряжением не выполнено (action_successful=False)... Причина: {log_message_text}")
            
            # Сообщение пользователю после транзакции
            await callback.message.edit_text(log_message_text, reply_markup=None)

    # ... (обработка ValueError, Exception, finally state.clear()) ...
    except ValueError as ve:
        logging.error(f"Ошибка значения в process_equipment_selection: {ve}, callback_data: '{callback.data}'")
        if log_message_text == "Произошла ошибка.": log_message_text = str(ve) if str(ve) else "Ошибка обработки данных."
        try: await callback.message.edit_text(log_message_text, reply_markup=None)
        except Exception: await callback.message.answer(log_message_text, reply_markup=None)
    except Exception as e:
        logging.exception(f"Непредвиденная ошибка при сохранении лога снаряжения: {e}")
        await callback.message.edit_text("Не удалось сохранить запись в журнал. Системная ошибка.", reply_markup=None)
    finally:
        await state.clear()
        
async def handle_log_cancel(callback: types.CallbackQuery, state: FSMContext):
    """Обработка нажатия кнопки отмены в FSM журнала."""
    await callback.answer("Действие отменено", show_alert=False) # Можно убрать show_alert
    current_state_str = await state.get_state() # Получаем строковое представление состояния
    logging.info(f"Отмена действия журнала снаряжения из состояния {current_state_str} пользователем {callback.from_user.id}")
    try:
        # Пытаемся отредактировать сообщение, убирая кнопки
        await callback.message.edit_text("Операция с журналом снаряжения отменена.", reply_markup=None)
    except Exception as e:
        # Если редактирование не удалось (например, сообщение слишком старое), просто логируем
        logging.warning(f"Не удалось отредактировать сообщение при отмене журнала: {e}")

    await state.clear() # Очищаем состояние

async def handle_readiness_check(message: types.Message, state: FSMContext, session_factory: async_sessionmaker): # <--- ДОБАВЛЕН session_factory
    """Обработчик кнопки '🚨 Готовность к выезду'. Показывает статус и кнопки смены."""
    await state.clear() # На всякий случай, хотя эта функция не использует FSM для своих целей
    user_id = message.from_user.id

    async with session_factory() as session: # <--- СОЗДАЕМ СЕССИЮ ИЗ SESSION_FACTORY
        # Используем session.scalar, так как ожидаем один или ноль объектов Employee
        employee = await session.scalar(
            select(Employee).where(Employee.telegram_id == user_id)
        )

        if not employee:
            await message.answer("Не удалось найти ваш профиль.")
            return

        status_text = "✅ Вы отмечены как ГОТОВЫ к выезду." if employee.is_ready else "❌ Вы отмечены как НЕ ГОТОВЫ к выезду."
        keyboard = get_readiness_toggle_keyboard(employee.is_ready)

        await message.answer(
            f"Ваш текущий статус готовности:\n{status_text}\n\nВыберите действие:",
            reply_markup=keyboard
        )

# --- Новый обработчик для смены статуса ---
async def handle_set_readiness(callback: types.CallbackQuery, session_factory: async_sessionmaker):
    """Обрабатывает нажатие кнопок смены статуса готовности."""
    await callback.answer()
    user_id = callback.from_user.id
    set_ready_to = None

    if callback.data == "set_ready_true":
        set_ready_to = True
    elif callback.data == "set_ready_false":
        set_ready_to = False
    elif callback.data == "readiness_back":
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception as e:
            logging.warning(f"Не удалось убрать кнопки готовности: {e}")
        return

    if set_ready_to is None:
        logging.warning(f"Неизвестный callback_data в handle_set_readiness: {callback.data}")
        return

    try:
        async with session_factory() as session: # <--- СОЗДАЕМ СЕССИЮ ИЗ SESSION_FACTORY
            async with session.begin(): # Используем транзакцию
                # Используем session.scalar, так как ожидаем один или ноль объектов Employee
                employee = await session.scalar(
                    select(Employee).where(Employee.telegram_id == user_id)
                )

                if not employee:
                    await callback.message.edit_text("Не удалось найти ваш профиль.")
                    return

                if employee.is_ready == set_ready_to:
                    status_text = "✅ Вы уже отмечены как ГОТОВЫ." if set_ready_to else "❌ Вы уже отмечены как НЕ ГОТОВЫ."
                    await callback.message.edit_text(f"{status_text} Статус не изменен.", reply_markup=None)
                    return

                employee.is_ready = set_ready_to
                session.add(employee)
                # Коммит произойдет автоматически при выходе из session.begin()
            
            # Сообщение после успешного коммита
            new_status_text = "✅ Статус изменен: Вы отмечены как ГОТОВЫ." if set_ready_to else "❌ Статус изменен: Вы отмечены как НЕ ГОТОВЫ."
            logging.info(f"Пользователь {user_id} изменил статус готовности на {set_ready_to}")
            await callback.message.edit_text(new_status_text, reply_markup=None)

    except Exception as e:
        logging.exception(f"Ошибка при смене статуса готовности для {user_id}: {e}")
        await callback.message.edit_text("Не удалось изменить статус готовности.")

async def handle_shift_schedule_view(message: types.Message):
    """Обработчик кнопки '📅 График смен'. Показывает основную смену."""
    user_id = message.from_user.id
    async with async_session() as session:
        shift = await session.scalar(
            select(Employee.shift).where(Employee.telegram_id == user_id)
        )

        if shift is not None:
            await message.answer(
                f"Ваша основная закрепленная смена: <b>{shift}</b>.\n\n" # Выделим жирным
                f"<i>Детальный график на ближайшие дни будет доступен в будущих версиях.</i>", # Уточнение
                parse_mode='HTML' # <--- Добавляем эту строку
            )
        else:
            await message.answer("Не удалось найти информацию о вашей основной смене.")

async def show_my_active_dispatches(
    event: types.Message | types.CallbackQuery,
    session_factory: async_sessionmaker,
    target_dispatch_id: int | None = None
):
    user_telegram_id = event.from_user.id
    
    if isinstance(event, types.Message):
        logging.info(f"Пожарный {user_telegram_id} запросил свои активные выезда (через кнопку меню).")
        reply_target_message = event
    elif isinstance(event, types.CallbackQuery):
        logging.info(f"Пожарный {user_telegram_id} запросил детали выезда ID {target_dispatch_id} (через callback).")
        await event.answer()
        reply_target_message = event.message
    else:
        logging.error(f"Неизвестный тип события в show_my_active_dispatches: {type(event)}")
        return

    active_dispatches_to_show = []

    async with session_factory() as session:
        employee = await session.scalar(
            select(Employee).where(Employee.telegram_id == user_telegram_id)
        )
        if not employee:
            await reply_target_message.answer("Ошибка: Ваш профиль не найден.")
            return
        employee_id = employee.id

        statuses_for_firefighter = ['approved', 'dispatched', 'in_progress']
        
        query = select(DispatchOrder).where(DispatchOrder.status.in_(statuses_for_firefighter))
        if target_dispatch_id is not None:
            query = query.where(DispatchOrder.id == target_dispatch_id)
        
        relevant_dispatches_result = await session.scalars(query.order_by(DispatchOrder.creation_time.desc()))
        
        for dispatch in relevant_dispatches_result.all():
            assigned_ids_list = []
            if dispatch.assigned_personnel_ids:
                try:
                    if isinstance(dispatch.assigned_personnel_ids, str):
                        assigned_ids_list = json.loads(dispatch.assigned_personnel_ids)
                    elif isinstance(dispatch.assigned_personnel_ids, list):
                        assigned_ids_list = dispatch.assigned_personnel_ids 
                    else:
                        logging.warning(f"Неожиданный тип для assigned_personnel_ids: {type(dispatch.assigned_personnel_ids)} для выезда {dispatch.id}")
                    
                    if isinstance(assigned_ids_list, list) and employee_id in assigned_ids_list:
                        active_dispatches_to_show.append(dispatch)
                except json.JSONDecodeError:
                    logging.error(f"Ошибка декодирования assigned_personnel_ids для выезда {dispatch.id} при фильтрации: {dispatch.assigned_personnel_ids}")
        
        if not active_dispatches_to_show:
            msg_text = f"Выезд №{target_dispatch_id} не найден в списке ваших активных назначений, либо он уже завершен." if target_dispatch_id else "У вас нет назначенных активных выездов."
            if isinstance(event, types.CallbackQuery):
                try: await event.message.edit_text(msg_text, reply_markup=None)
                except Exception: await reply_target_message.answer(msg_text)
            else:
                await reply_target_message.answer(msg_text)
            return

        response_parts = []
        if isinstance(event, types.Message) and not target_dispatch_id:
             response_parts.append("<b>ℹ️ Ваши активные выезда:</b>")

        for dispatch_order_obj in active_dispatches_to_show:
            dispatch_details = [
                f"\n<b>Выезд № {dispatch_order_obj.id}</b> (Статус: {STATUS_TRANSLATIONS.get(dispatch_order_obj.status, dispatch_order_obj.status)})",
                f"<b>Адрес:</b> {dispatch_order_obj.address}",
                f"<b>Причина:</b> {dispatch_order_obj.reason}",
                f"<b>Создан:</b> {dispatch_order_obj.creation_time.strftime('%d.%m.%Y %H:%M')}"
            ]
            if dispatch_order_obj.approval_time:
                dispatch_details.append(f"<b>Утвержден:</b> {dispatch_order_obj.approval_time.strftime('%d.%m.%Y %H:%M')}")

            # Получаем информацию о назначенном ЛС
            if dispatch_order_obj.assigned_personnel_ids:
                try:
                    personnel_ids_data = dispatch_order_obj.assigned_personnel_ids
                    personnel_ids_list = []
                    # ИСПРАВЛЕНИЕ ЗДЕСЬ
                    if isinstance(personnel_ids_data, str):
                        personnel_ids_list = json.loads(personnel_ids_data)
                    elif isinstance(personnel_ids_data, list):
                        personnel_ids_list = personnel_ids_data
                    
                    if personnel_ids_list:
                        personnel_on_dispatch_result = await session.execute(
                            select(Employee.full_name, Employee.position, Employee.rank)
                            .where(Employee.id.in_(personnel_ids_list))
                            .order_by(Employee.full_name)
                        )
                        personnel_str_list = ", ".join([f"{name} ({pos}, {rank or 'б/з'})" for name, pos, rank in personnel_on_dispatch_result.all()])
                        dispatch_details.append(f"<b>ЛС на выезде:</b> {personnel_str_list if personnel_str_list else 'не указан'}")
                    else:
                        dispatch_details.append("<b>ЛС на выезде:</b> не назначен")
                except json.JSONDecodeError:
                    dispatch_details.append("<b>ЛС на выезде:</b> ошибка чтения данных (JSON)")
            else:
                dispatch_details.append("<b>ЛС на выезде:</b> не назначен")
            
            # Получаем информацию о назначенной технике
            if dispatch_order_obj.assigned_vehicle_ids:
                try:
                    vehicle_ids_data = dispatch_order_obj.assigned_vehicle_ids
                    vehicle_ids_list = []
                    # ИСПРАВЛЕНИЕ ЗДЕСЬ
                    if isinstance(vehicle_ids_data, str):
                        vehicle_ids_list = json.loads(vehicle_ids_data)
                    elif isinstance(vehicle_ids_data, list):
                        vehicle_ids_list = vehicle_ids_data
                        
                    if vehicle_ids_list:
                        vehicles_on_dispatch_result = await session.execute(
                            select(Vehicle.model, Vehicle.number_plate)
                            .where(Vehicle.id.in_(vehicle_ids_list))
                            .order_by(Vehicle.model)
                        )
                        vehicle_str_list = ", ".join([f"{model} ({plate})" for model, plate in vehicles_on_dispatch_result.all()])
                        dispatch_details.append(f"<b>Техника:</b> {vehicle_str_list if vehicle_str_list else 'не указана'}")
                    else:
                        dispatch_details.append("<b>Техника:</b> не назначена")
                except json.JSONDecodeError:
                    dispatch_details.append("<b>Техника:</b> ошибка чтения данных (JSON)")
            else:
                dispatch_details.append("<b>Техника:</b> не назначена")

            if dispatch_order_obj.victims_count is not None and dispatch_order_obj.victims_count > 0:
                dispatch_details.append(f"<b>Пострадавших:</b> {dispatch_order_obj.victims_count}")
            if dispatch_order_obj.fatalities_count is not None and dispatch_order_obj.fatalities_count > 0:
                dispatch_details.append(f"<b>Погибших:</b> {dispatch_order_obj.fatalities_count}")
            if dispatch_order_obj.details_on_casualties:
                 dispatch_details.append(f"<b>Детали по пострадавшим:</b> {dispatch_order_obj.details_on_casualties}")
            
            response_parts.append("\n".join(dispatch_details))
        
        final_response = "\n\n".join(response_parts) # Используем двойной перенос строки между выездами для лучшей читаемости
        
        if isinstance(event, types.CallbackQuery) and target_dispatch_id:
            try:
                await event.message.edit_text(final_response, parse_mode="HTML", reply_markup=None)
            except Exception as e_edit:
                logging.warning(f"Не удалось отредактировать сообщение для деталей выезда ID {target_dispatch_id}: {e_edit}. Отправляем новым.")
                await reply_target_message.answer(final_response, parse_mode="HTML")
        else:
            MAX_MESSAGE_LENGTH = 4096
            if len(final_response) > MAX_MESSAGE_LENGTH:
                logging.info(f"Сообщение для пожарного {user_telegram_id} слишком длинное, разбиваем.")
                for i in range(0, len(final_response), MAX_MESSAGE_LENGTH):
                    await reply_target_message.answer(final_response[i:i + MAX_MESSAGE_LENGTH], parse_mode="HTML")
            else:
                await reply_target_message.answer(final_response, parse_mode="HTML")

# --- Регистрация обработчиков ---
def register_firefighter_handlers(router: Router):
    logging.info("Регистрируем обработчики пожарного...")

    # Кнопка "🧯 Журнал снаряжения"
    router.message.register(handle_equipment_log_button, F.text == "🧯 Журнал снаряжения")

    # Кнопка "🚨 Готовность к выезду"
    async def handle_readiness_check_entry_point(message: types.Message, state: FSMContext):
        await handle_readiness_check(message, state, async_session)
    router.message.register(handle_readiness_check_entry_point, F.text == "🚨 Готовность к выезду")

    # Callbacks для смены статуса готовности
    async def handle_set_readiness_entry_point(callback: types.CallbackQuery):
        await handle_set_readiness(callback, async_session)
    router.callback_query.register(handle_set_readiness_entry_point, F.data.in_(['set_ready_true', 'set_ready_false', 'readiness_back']))

    # Кнопка "📅 График смен"
    async def handle_shift_schedule_view_entry_point(message: types.Message):
        await handle_shift_schedule_view(message, async_session)
    router.message.register(handle_shift_schedule_view_entry_point, F.text == "📅 График смен")

    # Кнопка "🔥 Мои активные выезда"
    async def show_my_active_dispatches_menu_entry_point(message: types.Message, state: FSMContext):
        await show_my_active_dispatches(message, async_session)
    router.message.register(show_my_active_dispatches_menu_entry_point, F.text == "🔥 Мои активные выезда")

    # Callback для "Детали выезда"
    async def show_dispatch_details_callback_entry_point(callback: types.CallbackQuery, state: FSMContext):
        try: dispatch_id = int(callback.data.split("_")[-1])
        except (IndexError, ValueError): await callback.answer("Ошибка.", show_alert=True); return
        await show_my_active_dispatches(callback, async_session, target_dispatch_id=dispatch_id)
    router.callback_query.register(show_dispatch_details_callback_entry_point, F.data.startswith("dispatch_view_details_"))

    # FSM для журнала снаряжения
    router.callback_query.register(handle_log_main_action, EquipmentLogStates.CHOOSING_LOG_MAIN_ACTION, F.data.in_(['log_new_entry', 'log_back_to_main']))
    
    async def process_equipment_log_action_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await process_equipment_log_action(callback, state, async_session)
    router.callback_query.register(process_equipment_log_action_entry_point, EquipmentLogStates.CHOOSING_LOG_ACTION, F.data.startswith("log_action_"))

    async def process_equipment_selection_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await process_equipment_selection(callback, state, async_session)
    router.callback_query.register(process_equipment_selection_entry_point, EquipmentLogStates.SELECTING_EQUIPMENT, F.data.startswith("log_select_"))

    router.callback_query.register(handle_log_cancel, StateFilter(EquipmentLogStates.CHOOSING_LOG_ACTION, EquipmentLogStates.SELECTING_EQUIPMENT), F.data == "log_cancel")
    router.callback_query.register(lambda cb: cb.answer("Нет доступного снаряжения.", show_alert=True), F.data == "log_no_equipment", EquipmentLogStates.SELECTING_EQUIPMENT)
    
        # --- ЛОВУШКА ---
    # Регистрируем этот обработчик ПОСЛЕДНИМ внутри этого роутера
    async def catch_firefighter_callbacks(callback: types.CallbackQuery, state: FSMContext):
        current_state = await state.get_state()
        logging.warning(
            f"!!! НЕОБРАБОТАННЫЙ FIREFIGHTER Callback: "
            f"Data='{callback.data}', State='{current_state}', "
            f"User='{callback.from_user.id}', MsgID='{callback.message.message_id}'"
        )
        # Можно раскомментировать, чтобы пользователь видел ошибку
        await callback.answer("Команда не распознана.", show_alert=True)
        # router.callback_query.register(catch_firefighter_callbacks) # Без фильтров!
        
    logging.info("Обработчики пожарного зарегистрированы.")