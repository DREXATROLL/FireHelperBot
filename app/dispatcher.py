import json
from aiogram import F, types, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload
from models import async_session, Employee, Vehicle, DispatchOrder, AbsenceLog
from app.keyboards import ( # Добавляем новые клавиатуры
    confirm_cancel_dispatch_keyboard,
    get_dispatch_approval_keyboard,
    get_personnel_select_keyboard,
    get_vehicle_select_keyboard,
    get_cancel_keyboard,
    confirm_cancel_absence_keyboard,
    get_dispatch_edit_field_keyboard,
    get_confirm_cancel_edit_keyboard
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton
from aiogram import Bot
from datetime import datetime
import logging
import math

# --- Константы ---
DISPATCHES_PER_PAGE = 5 # Выездов на страницу

# Статусы для списков
ACTIVE_DISPATCH_STATUSES = ['pending_approval', 'approved', 'dispatched', 'in_progress']
ARCHIVED_DISPATCH_STATUSES = ['completed', 'rejected', 'canceled']

# --- Словарь для перевода статусов ---
STATUS_TRANSLATIONS = {
    'pending_approval': 'Ожидает утверждения',
    'approved': 'Утверждено',
    'rejected': 'Отклонено',
    'dispatched': 'Отправлено расчёту',
    'in_progress': 'В работе',
    'completed': 'Завершено',
    'canceled': 'Отменено'
}

# --- Состояния FSM для создания выезда ---
# --- Обновляем состояния FSM ---
class DispatchCreationStates(StatesGroup):
    ENTERING_ADDRESS = State()
    ENTERING_REASON = State()
    SELECTING_PERSONNEL = State() # Заменяем ENTERING_PERSONNEL
    SELECTING_VEHICLES = State()  # Заменяем ENTERING_VEHICLES
    CONFIRMATION = State()

class DispatchEditStates(StatesGroup):
    CHOOSING_FIELD_TO_EDIT = State()    # Ожидание выбора поля (Пострадавшие, Погибшие, Примечания и т.д.)
    ENTERING_VICTIMS_COUNT = State()
    ENTERING_FATALITIES_COUNT = State()
    ENTERING_CASUALTIES_DETAILS = State()
    ENTERING_GENERAL_NOTES = State()
    CONFIRM_DISPATCH_EDIT = State()     # Ожидание подтверждения изменений

class AbsenceRegistrationStates(StatesGroup):
    WAITING_FOR_ABSENT_EMPLOYEE_FULLNAME = State()
    WAITING_FOR_ABSENT_EMPLOYEE_POSITION = State()
    WAITING_FOR_ABSENT_EMPLOYEE_RANK = State() # Сделаем пока обязательным
    WAITING_FOR_ABSENCE_REASON = State()
    CONFIRM_ABSENCE_ENTRY = State()

async def handle_field_to_edit_choice(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    dispatch_id = data.get("editing_dispatch_id")

    if not dispatch_id:
        await callback.message.edit_text("Ошибка: ID выезда не найден. Попробуйте начать заново.", reply_markup=None)
        await state.clear()
        return

    # Определяем, какое поле выбрано
    field_action = callback.data # Например, "edit_dispatch_field_victims_ID"
    
    # Общий текст запроса ввода
    prompt_text = "Введите новое значение. Для отмены текущего ввода нажмите кнопку."
    cancel_cb_data = f"edit_dispatch_cancel_change_{dispatch_id}" # Для возврата к выбору поля

    if field_action.startswith("edit_dispatch_field_victims_"):
        await state.update_data(field_being_edited="victims_count")
        current_val = data.get('current_victims', 0)
        await callback.message.edit_text(
            f"Редактирование кол-ва пострадавших (выезд №{dispatch_id}).\n"
            f"Текущее значение: {current_val if current_val is not None else 'не указано'}.\n{prompt_text}",
            reply_markup=get_cancel_keyboard(callback_data=cancel_cb_data)
        )
        await state.set_state(DispatchEditStates.ENTERING_VICTIMS_COUNT)
        
    elif field_action.startswith("edit_dispatch_field_fatalities_"):
        await state.update_data(field_being_edited="fatalities_count")
        current_val = data.get('current_fatalities', 0)
        await callback.message.edit_text(
            f"Редактирование кол-ва погибших (выезд №{dispatch_id}).\n"
            f"Текущее значение: {current_val if current_val is not None else 'не указано'}.\n{prompt_text}",
            reply_markup=get_cancel_keyboard(callback_data=cancel_cb_data)
        )
        await state.set_state(DispatchEditStates.ENTERING_FATALITIES_COUNT)

    elif field_action.startswith("edit_dispatch_field_casualties_details_"):
        await state.update_data(field_being_edited="details_on_casualties")
        current_val = data.get('current_casualties_details', '')
        await callback.message.edit_text(
            f"Редактирование деталей по пострадавшим/погибшим (выезд №{dispatch_id}).\n"
            f"Текущее значение: \n<code>{current_val if current_val else 'не указано'}</code>\n\n{prompt_text}",
            reply_markup=get_cancel_keyboard(callback_data=cancel_cb_data),
            parse_mode="HTML"
        )
        await state.set_state(DispatchEditStates.ENTERING_CASUALTIES_DETAILS)

    elif field_action.startswith("edit_dispatch_field_notes_"):
        await state.update_data(field_being_edited="notes")
        current_val = data.get('current_notes', '')
        await callback.message.edit_text(
            f"Редактирование общих примечаний к выезду (выезд №{dispatch_id}).\n"
            f"Текущее значение: \n<code>{current_val if current_val else 'не указано'}</code>\n\n{prompt_text}",
            reply_markup=get_cancel_keyboard(callback_data=cancel_cb_data),
            parse_mode="HTML"
        )
        await state.set_state(DispatchEditStates.ENTERING_GENERAL_NOTES)

    elif field_action.startswith("edit_dispatch_cancel_"): # Отмена всего редактирования
        await callback.message.edit_text(f"Редактирование выезда №{dispatch_id} отменено.", reply_markup=None)
        await state.clear()

async def process_victims_count_input(message: types.Message, state: FSMContext, session_factory: async_sessionmaker):
    try:
        new_value = int(message.text.strip())
        if new_value < 0:
            await message.answer("Количество пострадавших не может быть отрицательным. Введите корректное число:",
                                 # reply_markup=get_cancel_keyboard(...) # Можно добавить отмену и здесь
                                 )
            return
    except ValueError:
        await message.answer("Пожалуйста, введите число (например, 0, 1, 2 и т.д.):",
                             # reply_markup=get_cancel_keyboard(...)
                             )
        return

    data = await state.get_data()
    dispatch_id = data.get("editing_dispatch_id")
    field_being_edited = data.get("field_being_edited") # Должно быть "victims_count"

    if not dispatch_id or field_being_edited != "victims_count":
        await message.answer("Произошла ошибка сессии редактирования. Попробуйте начать заново.")
        await state.clear()
        return
    
    # Сохраняем новое значение временно в FSM для подтверждения
    await state.update_data(new_value_for_field=new_value)

    # Запрос подтверждения
    await message.answer(
        f"Вы уверены, что хотите изменить 'Кол-во пострадавших' для выезда №{dispatch_id} на <b>{new_value}</b>?",
        reply_markup=get_confirm_cancel_edit_keyboard(dispatch_id), # Передаем dispatch_id для callback'ов
        parse_mode="HTML"
    )
    await state.set_state(DispatchEditStates.CONFIRM_DISPATCH_EDIT) # Переходим в состояние подтверждения

# Хэндлер для кнопки "Отменить это изменение" (возврат к выбору поля)
async def cancel_specific_field_edit(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    dispatch_id = data.get("editing_dispatch_id")
    if not dispatch_id: # На всякий случай
        await callback.message.edit_text("Ошибка сессии редактирования. Начните заново.", reply_markup=None)
        await state.clear()
        return

    # Возвращаемся к выбору поля
    await callback.message.edit_text(
        f"Редактирование выезда №{dispatch_id}.\nВыберите поле для изменения:",
        reply_markup=get_dispatch_edit_field_keyboard(dispatch_id)
    )
    await state.set_state(DispatchEditStates.CHOOSING_FIELD_TO_EDIT)

# Общий хэндлер для сохранения подтвержденного изменения
async def process_dispatch_field_save(callback: types.CallbackQuery, state: FSMContext, session_factory: async_sessionmaker):
    await callback.answer()
    data = await state.get_data()
    dispatch_id = data.get("editing_dispatch_id")
    field_to_update = data.get("field_being_edited")
    new_value = data.get("new_value_for_field") # Новое значение, которое мы сохранили

    if not all([dispatch_id, field_to_update]): # new_value может быть 0 или None
        await callback.message.edit_text("Ошибка: недостаточно данных для сохранения. Попробуйте снова.", reply_markup=None)
        await state.clear()
        return

    if callback.data.startswith("edit_dispatch_save_change_"):
        try:
            async with session_factory() as session:
                async with session.begin():
                    dispatch_order = await session.get(DispatchOrder, dispatch_id)
                    if not dispatch_order:
                        await callback.message.edit_text(f"Ошибка: Выезд №{dispatch_id} не найден для обновления.", reply_markup=None)
                        await state.clear()
                        return
                    
                    # Обновляем соответствующее поле
                    if field_to_update == "victims_count":
                        dispatch_order.victims_count = int(new_value) if new_value is not None else 0
                    elif field_to_update == "fatalities_count":
                        dispatch_order.fatalities_count = int(new_value) if new_value is not None else 0
                    elif field_to_update == "details_on_casualties":
                        dispatch_order.details_on_casualties = str(new_value) if new_value else None
                    elif field_to_update == "notes":
                        dispatch_order.notes = str(new_value) if new_value else None
                    else:
                        await callback.message.edit_text("Ошибка: неизвестное поле для обновления.", reply_markup=None)
                        await state.clear()
                        return
                        
                    # Обновляем информацию о редактировании
                    current_user_employee = await session.scalar(
                        select(Employee).where(Employee.telegram_id == callback.from_user.id)
                    )
                    if current_user_employee:
                        dispatch_order.last_edited_by_dispatcher_id = current_user_employee.id
                    dispatch_order.last_edited_at = datetime.now() # Импортируйте datetime из datetime
                    
                    session.add(dispatch_order)
                    logging.info(f"Диспетчер {callback.from_user.id} обновил поле {field_to_update} для выезда {dispatch_id} на значение: {new_value}")
                
                await callback.message.edit_text(
                    f"✅ Поле '{field_to_update}' для выезда №{dispatch_id} успешно обновлено на <b>'{new_value}'</b>.\n"
                    "Хотите отредактировать что-нибудь еще для этого выезда?",
                    reply_markup=get_dispatch_edit_field_keyboard(dispatch_id), # Возвращаем к выбору полей
                    parse_mode="HTML"
                )
                # Возвращаемся к выбору поля, но сохраняем dispatch_id в FSM
                # Обновляем current_values в FSM
                if field_to_update == "victims_count": await state.update_data(current_victims=new_value)
                elif field_to_update == "fatalities_count": await state.update_data(current_fatalities=new_value)
                # и т.д. для других полей
                await state.set_state(DispatchEditStates.CHOOSING_FIELD_TO_EDIT)

        except Exception as e:
            logging.exception(f"Ошибка при сохранении изменений для выезда {dispatch_id}: {e}")
            await callback.message.edit_text("Произошла ошибка при сохранении изменений.", reply_markup=None)
            await state.clear()
    
    elif callback.data.startswith("edit_dispatch_cancel_change_"): # Отмена изменения конкретного поля
        # Это обрабатывается функцией cancel_specific_field_edit, которую мы уже определили.
        # Но если мы попали сюда, значит, это отмена на этапе CONFIRM_DISPATCH_EDIT
        await callback.message.edit_text(
            f"Редактирование поля '{field_to_update}' для выезда №{dispatch_id} отменено.\n"
            "Выберите другое поле для изменения:",
            reply_markup=get_dispatch_edit_field_keyboard(dispatch_id)
        )
        await state.set_state(DispatchEditStates.CHOOSING_FIELD_TO_EDIT)

async def show_full_dispatch_details(callback: types.CallbackQuery, session_factory: async_sessionmaker):
    await callback.answer()
    try:
        dispatch_id = int(callback.data.split("_")[-1])
    except (IndexError, ValueError):
        logging.error(f"Ошибка извлечения dispatch_id из callback_data для деталей: {callback.data}")
        await callback.message.answer("Ошибка: неверный формат ID выезда.")
        return

    async with session_factory() as session:
        dispatch = await session.get(
            DispatchOrder, 
            dispatch_id,
            options=[ # Жадная загрузка связанных объектов Employee
                selectinload(DispatchOrder.creator),
                selectinload(DispatchOrder.approver),
                selectinload(DispatchOrder.editor)
            ]
        )

        if not dispatch:
            try:
                await callback.message.edit_text(f"❌ Выезд №{dispatch_id} не найден.", reply_markup=None)
            except Exception:
                await callback.message.answer(f"❌ Выезд №{dispatch_id} не найден.")
            return

        details = [
            f"<b>Детальная информация по выезду №{dispatch.id}</b>",
            f"<b>Статус:</b> {STATUS_TRANSLATIONS.get(dispatch.status, dispatch.status)}",
            f"<b>Адрес:</b> {dispatch.address}",
            f"<b>Причина:</b> {dispatch.reason}",
            f"<b>Время создания:</b> {dispatch.creation_time.strftime('%d.%m.%Y %H:%M')}",
        ]

        if dispatch.creator:
            details.append(f"<b>Создал диспетчер:</b> {dispatch.creator.full_name}")

        if dispatch.approver:
            details.append(
                f"<b>Решение НК ({dispatch.approver.full_name}):</b> "
                f"{STATUS_TRANSLATIONS.get(dispatch.status, dispatch.status).capitalize()} " # Используем текущий статус, который отражает решение
                f"в {dispatch.approval_time.strftime('%H:%M %d.%m.%Y') if dispatch.approval_time else 'время не указано'}"
            )
        
        # Информация о назначенном ЛС
        if dispatch.assigned_personnel_ids:
            try:
                personnel_ids_data = dispatch.assigned_personnel_ids
                personnel_ids_list = []
                # ИСПРАВЛЕНИЕ ЗДЕСЬ
                if isinstance(personnel_ids_data, str):
                    personnel_ids_list = json.loads(personnel_ids_data)
                elif isinstance(personnel_ids_data, list):
                    personnel_ids_list = personnel_ids_data
                else:
                    logging.warning(f"Неожиданный тип для assigned_personnel_ids в show_full_dispatch_details: {type(personnel_ids_data)} для выезда {dispatch.id}")

                if personnel_ids_list:
                    personnel_on_dispatch_result = await session.execute(
                        select(Employee.full_name, Employee.position, Employee.rank)
                        .where(Employee.id.in_(personnel_ids_list))
                        .order_by(Employee.full_name)
                    )
                    personnel_str_list = "\n  - ".join(
                        [f"{name} ({pos}, {rank or 'б/з'})" for name, pos, rank in personnel_on_dispatch_result.all()]
                    )
                    details.append(f"<b>Назначенный ЛС:</b>\n  - {personnel_str_list if personnel_str_list else 'список пуст'}")
                else:
                    details.append("<b>Назначенный ЛС:</b> не назначен")
            except json.JSONDecodeError:
                details.append("<b>Назначенный ЛС:</b> ошибка чтения данных (JSON)")
        else:
            details.append("<b>Назначенный ЛС:</b> не назначен")

        # Информация о назначенной технике
        if dispatch.assigned_vehicle_ids:
            try:
                vehicle_ids_data = dispatch.assigned_vehicle_ids
                vehicle_ids_list = []
                # ИСПРАВЛЕНИЕ ЗДЕСЬ
                if isinstance(vehicle_ids_data, str):
                    vehicle_ids_list = json.loads(vehicle_ids_data)
                elif isinstance(vehicle_ids_data, list):
                    vehicle_ids_list = vehicle_ids_data
                else:
                    logging.warning(f"Неожиданный тип для assigned_vehicle_ids в show_full_dispatch_details: {type(vehicle_ids_data)} для выезда {dispatch.id}")
                    
                if vehicle_ids_list:
                    vehicles_on_dispatch_result = await session.execute(
                        select(Vehicle.model, Vehicle.number_plate)
                        .where(Vehicle.id.in_(vehicle_ids_list))
                        .order_by(Vehicle.model)
                    )
                    vehicle_str_list = "\n  - ".join(
                        [f"{model} ({plate})" for model, plate in vehicles_on_dispatch_result.all()]
                    )
                    details.append(f"<b>Назначенная техника:</b>\n  - {vehicle_str_list if vehicle_str_list else 'список пуст'}")
                else:
                    details.append("<b>Назначенная техника:</b> не назначена")
            except json.JSONDecodeError:
                details.append("<b>Назначенная техника:</b> ошибка чтения данных (JSON)")
        else:
            details.append("<b>Назначенная техника:</b> не назначена")
            
        # Пострадавшие/погибшие
        if dispatch.victims_count is not None and dispatch.victims_count > 0:
            details.append(f"<b>Пострадавших:</b> {dispatch.victims_count}")
        else: # Если 0 или None, можно явно указать "нет" для полноты
            details.append(f"<b>Пострадавших:</b> 0") 

        if dispatch.fatalities_count is not None and dispatch.fatalities_count > 0:
            details.append(f"<b>Погибших:</b> {dispatch.fatalities_count}")
        else: # Если 0 или None
            details.append(f"<b>Погибших:</b> 0")

        if dispatch.details_on_casualties:
            details.append(f"<b>Детали по пострадавшим/погибшим:</b> {dispatch.details_on_casualties}")
        
        if dispatch.notes:
            details.append(f"<b>Общие примечания:</b> {dispatch.notes}")

        # НОВОЕ: Время завершения выезда
        if dispatch.status == 'completed' and dispatch.completion_time:
            details.append(f"<b>Время завершения:</b> {dispatch.completion_time.strftime('%d.%m.%Y %H:%M')}")
        
        if dispatch.editor and dispatch.last_edited_at:
            details.append(
                f"<i>Последнее изменение: {dispatch.editor.full_name} "
                f"в {dispatch.last_edited_at.strftime('%H:%M %d.%m.%Y')}</i>" # Добавил год
            )
        
        response_text = "\n".join(details)
        
        edit_markup_builder = InlineKeyboardBuilder()
        editable_statuses = ['pending_approval', 'approved', 'dispatched', 'in_progress']
        
        # Проверка прав на редактирование (для кнопки "Редактировать")
        current_user_employee = await session.scalar(
            select(Employee).where(Employee.telegram_id == callback.from_user.id)
        )
        can_edit = False
        if current_user_employee and \
           current_user_employee.id == dispatch.dispatcher_id and \
           dispatch.status in editable_statuses:
            can_edit = True
        
        if can_edit:
            edit_markup_builder.button(
                text="✏️ Редактировать выезд", 
                callback_data=f"dispatch_edit_start_{dispatch.id}"
            )
        
        final_markup = edit_markup_builder.as_markup()
        
        try:
            await callback.message.edit_text(response_text, parse_mode="HTML", reply_markup=final_markup)
        except Exception as e:
            logging.error(f"Не удалось отредактировать сообщение для деталей выезда {dispatch_id}: {e}")
            await callback.message.answer(response_text, parse_mode="HTML", reply_markup=final_markup)
            
# --- Обработчики для отметки отсутствующих ---
async def handle_mark_absent_request(message: types.Message, state: FSMContext, session_factory: async_sessionmaker): # Принимаем session_factory
    await state.clear() # Очищаем предыдущее состояние FSM
    
    # Определяем, на каком карауле диспетчер (если на карауле)
    # Это понадобится для поля karakul_number_reported_for в AbsenceLog
    # и для отображения в сообщении
    current_karakul_number = "N/A"
    dispatcher_employee_id = None

    async with session_factory() as session:
        dispatcher = await session.scalar(
            select(Employee).where(Employee.telegram_id == message.from_user.id)
        )
        if not dispatcher:
            await message.answer("Ошибка: ваш профиль не найден. Невозможно отметить отсутствующего.")
            return
        dispatcher_employee_id = dispatcher.id

        # Проверяем активный караул диспетчера
        # Используем get_active_shift из shift_management, передавая ему session_factory
        from app.shift_management import get_active_shift # Локальный импорт для избежания цикличности
        active_shift = await get_active_shift(session_factory, dispatcher.id)
        if active_shift:
            current_karakul_number = active_shift.karakul_number
            logging.info(f"Диспетчер {dispatcher.id} отмечает отсутствующего для караула №{current_karakul_number}")
        else:
            logging.info(f"Диспетчер {dispatcher.id} отмечает отсутствующего (не на активном карауле, будет привязано к дате).")
    
    await state.update_data(
        reporter_employee_id=dispatcher_employee_id,
        karakul_number_reported_for=current_karakul_number if active_shift else None # Сохраняем номер караула или None
    )

    await message.answer(
        f"Отметка отсутствующего сотрудника (для караула №{current_karakul_number if active_shift else 'текущая дата'}).\n"
        "Введите ФИО отсутствующего сотрудника (например, Петров Петр Петрович):",
        reply_markup=get_cancel_keyboard(callback_data="cancel_absence_registration") # Нужна своя кнопка отмены
    )
    await state.set_state(AbsenceRegistrationStates.WAITING_FOR_ABSENT_EMPLOYEE_FULLNAME)

async def _generate_dispatch_list_page(session: AsyncSession, page: int, list_type: str):
    """Генерирует текст и клавиатуру для страницы списка выездов."""

    if list_type == 'active':
        statuses_to_select = ACTIVE_DISPATCH_STATUSES
        title = "📊 Активные выезды"
    elif list_type == 'archived':
        statuses_to_select = ARCHIVED_DISPATCH_STATUSES
        title = "📂 Архив выездов"
    else:
        return "Неизвестный тип списка.", None

    offset = (page - 1) * DISPATCHES_PER_PAGE

    total_items_result = await session.execute(
        select(func.count(DispatchOrder.id))
        .where(DispatchOrder.status.in_(statuses_to_select))
    )
    total_items = total_items_result.scalar_one_or_none() or 0

    if total_items == 0:
        empty_message = "Нет активных выездов." if list_type == 'active' else "Архив выездов пуст."
        return empty_message, None

    total_pages = math.ceil(total_items / DISPATCHES_PER_PAGE)
    page = max(1, min(page, total_pages)) # Корректируем номер страницы

    dispatch_orders_result = await session.execute(
        select(DispatchOrder)
        .where(DispatchOrder.status.in_(statuses_to_select))
        .order_by(DispatchOrder.creation_time.desc())
        .limit(DISPATCHES_PER_PAGE)
        .offset(offset)
    )
    dispatch_orders = dispatch_orders_result.scalars().all()

    response_lines = [f"{title} (Страница {page}/{total_pages}):"]
    builder = InlineKeyboardBuilder() # Инициализируем билдер клавиатуры здесь

    for order in dispatch_orders:
        status_emoji = {
            'pending_approval': '⏳', 'approved': '✅', 'rejected': '❌',
            'dispatched': '➡️', 'in_progress': '🔥', 'completed': '🏁',
            'canceled': '🚫'
        }.get(order.status, '❓')
        
        status_russian = STATUS_TRANSLATIONS.get(order.status, order.status)
        
        casualties_info = [] # Собираем информацию о пострадавших/погибших
        if order.victims_count is not None and order.victims_count > 0:
            casualties_info.append(f"Пострадавших: {order.victims_count}")
        if order.fatalities_count is not None and order.fatalities_count > 0:
            casualties_info.append(f"Погибших: {order.fatalities_count}")
        
        casualties_str = ""
        if casualties_info:
            casualties_str = f" ({', '.join(casualties_info)})"

        # Формируем текст для одного выезда
        dispatch_text = (
            f"\n🆔 {order.id} | {status_emoji} {status_russian}\n"
            f"📍 {order.address}\n"
            f"📄 {order.reason} ({order.creation_time.strftime('%d.%m %H:%M')}){casualties_str}" # <--- ДОБАВИЛИ ИНФО
        )
        response_lines.append(dispatch_text)
        
        # Добавляем инлайн-кнопку "Детали" для каждого выезда
        builder.row(InlineKeyboardButton(
            text=f"🔍 Детали выезда №{order.id}", 
            callback_data=f"dispatch_full_details_{order.id}" # Новый callback_data
        ))

    response_text = "\n".join(response_lines)

    # Кнопки пагинации (остаются ниже списка выездов)
    pagination_buttons = []
    if page > 1:
        pagination_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"dispatch_list_{list_type}_{page-1}"))
    if page < total_pages:
        pagination_buttons.append(InlineKeyboardButton(text="➡️ Вперед", callback_data=f"dispatch_list_{list_type}_{page+1}"))
    
    if pagination_buttons:
        builder.row(*pagination_buttons) # Добавляем кнопки пагинации в билдер

    # Собираем клавиатуру, если есть какие-либо кнопки (детали или пагинация)
    has_buttons = any(row for row in builder.export()) 
    final_markup = builder.as_markup() if has_buttons else None

    return response_text, final_markup
# --- Общий обработчик отмены для FSM создания ---
async def cancel_dispatch_creation(callback: types.CallbackQuery, state: FSMContext):
    """Отменяет процесс создания выезда."""
    await callback.answer("Отменено")
    current_state = await state.get_state()
    logging.info(f"Диспетчер {callback.from_user.id} отменил создание выезда из состояния {current_state}")
    await state.clear()
    try: # Пытаемся отредактировать сообщение
        await callback.message.edit_text("Создание нового выезда отменено.", reply_markup=None)
    except Exception as e: # Если не вышло (старое сообщение)
        logging.warning(f"Не удалось отредактировать сообщение при отмене создания выезда: {e}")
        # Можно отправить новое сообщение, если нужно
        await callback.message.answer("Создание нового выезда отменено.")

# --- Обработчики ---

async def handle_new_dispatch_request(message: types.Message, state: FSMContext):
    await state.clear()
    # Отправляем с кнопкой отмены
    await message.answer("Введите адрес выезда:", reply_markup=get_cancel_keyboard())
    await state.set_state(DispatchCreationStates.ENTERING_ADDRESS)
    logging.info(f"Диспетчер {message.from_user.id} начал создание выезда...")

async def process_address(message: types.Message, state: FSMContext):
    address = message.text.strip()
    if not address: # <-- Простая проверка адреса (не пустой)
        await message.answer("Адрес не может быть пустым. Пожалуйста, введите адрес:", reply_markup=get_cancel_keyboard())
        return
    # -- Более сложная проверка адреса (пример, можно расширить) --
    if len(address) < 10: # Условно, адрес короче 10 символов - подозрительно
        await message.answer("Пожалуйста, введите более полный адрес:", reply_markup=get_cancel_keyboard())
        return
    # -- Конец проверки адреса --
    await state.update_data(address=address)
    await message.answer("Введите причину вызова:", reply_markup=get_cancel_keyboard())
    await state.set_state(DispatchCreationStates.ENTERING_REASON)
    logging.info(f"Диспетчер {message.from_user.id}, адрес: '{address}'...")

# --- Изменяем process_reason ---
async def process_reason(message: types.Message, state: FSMContext):
    reason = message.text.strip()
    if not reason:
        await message.answer("Причина вызова не может быть пустой:", reply_markup=get_cancel_keyboard())
        return
    await state.update_data(reason=reason, selected_personnel_ids=set()) # Инициализируем пустой сет для ЛС
    logging.info(f"Диспетчер {message.from_user.id}, причина: '{reason}'...")

    # --- Переходим к выбору ЛС ---
    async with async_session() as session:
        # Ищем доступных и ГОТОВЫХ пожарных/водителей (пример)
        # TODO: Уточнить, кого именно можно выбирать
        available_personnel = await session.scalars(
            select(Employee).where(
                Employee.position.in_(['Пожарный', 'Водитель']), # Пример выбора
                Employee.is_ready == True # Только готовых
            ).order_by(Employee.full_name)
        )
        personnel_list = available_personnel.all()

    if not personnel_list:
        await message.answer("Нет доступного и готового личного состава для назначения. Создание выезда отменено.")
        await state.clear()
        return

    keyboard = get_personnel_select_keyboard(personnel_list, set())
    await message.answer(
        "Выберите личный состав (нажмите на имя для выбора/отмены):",
        reply_markup=keyboard
    )
    await state.set_state(DispatchCreationStates.SELECTING_PERSONNEL)
    logging.info(f"Состояние: SELECTING_PERSONNEL")

async def handle_personnel_toggle(callback: types.CallbackQuery, state: FSMContext):
    """Обрабатывает выбор/отмену выбора сотрудника."""
    await callback.answer()
    try:
        personnel_id = int(callback.data.split('_')[-1])
        data = await state.get_data()
        selected_ids = data.get('selected_personnel_ids', set())

        # Переключаем ID в сете
        if personnel_id in selected_ids:
            selected_ids.remove(personnel_id)
        else:
            selected_ids.add(personnel_id)

        await state.update_data(selected_personnel_ids=selected_ids)

        # Обновляем клавиатуру
        async with async_session() as session:
            available_personnel = await session.scalars(
                select(Employee).where(
                    Employee.position.in_(['Пожарный', 'Водитель']),
                    Employee.is_ready == True
                ).order_by(Employee.full_name)
            )
            personnel_list = available_personnel.all()

        keyboard = get_personnel_select_keyboard(personnel_list, selected_ids)
        # Редактируем сообщение с обновленной клавиатурой
        await callback.message.edit_reply_markup(reply_markup=keyboard)

    except (ValueError, IndexError) as e:
        logging.error(f"Ошибка обработки выбора ЛС: {e}, data: {callback.data}")
    except Exception as e:
        logging.exception(f"Ошибка в handle_personnel_toggle: {e}")

# --- Обработчик кнопки "К выбору техники" ---
async def handle_personnel_done(callback: types.CallbackQuery, state: FSMContext):
    """Переход к выбору техники."""
    await callback.answer()
    data = await state.get_data()
    selected_personnel_ids = data.get('selected_personnel_ids', set())

    if not selected_personnel_ids:
        await callback.answer("Вы не выбрали ни одного сотрудника!", show_alert=True)
        return

    logging.info(f"Диспетчер {callback.from_user.id} выбрал ЛС: {selected_personnel_ids}")
    await state.update_data(selected_vehicle_ids=set()) # Инициализируем сет для техники

    # --- Переходим к выбору техники ---
    async with async_session() as session:
        available_vehicles = await session.scalars(
            select(Vehicle).where(Vehicle.status == 'available').order_by(Vehicle.model)
        )
        vehicle_list = available_vehicles.all()

    if not vehicle_list:
        # Если нет техники, сразу переходим к подтверждению (ЛС уже выбран)
        logging.warning("Нет доступной техники для выбора, переходим к подтверждению.")
        await show_confirmation_summary(callback.message, state) # Вызываем функцию показа сводки
        return

    keyboard = get_vehicle_select_keyboard(vehicle_list, set())
    # Редактируем предыдущее сообщение (или отправляем новое)
    await callback.message.edit_text(
        "Выберите технику (нажмите для выбора/отмены):",
        reply_markup=keyboard
    )
    await state.set_state(DispatchCreationStates.SELECTING_VEHICLES)
    logging.info(f"Состояние: SELECTING_VEHICLES")


# --- Новый обработчик выбора Техники ---
async def handle_vehicle_toggle(callback: types.CallbackQuery, state: FSMContext):
    """Обрабатывает выбор/отмену выбора техники."""
    await callback.answer()
    try:
        vehicle_id = int(callback.data.split('_')[-1])
        data = await state.get_data()
        selected_ids = data.get('selected_vehicle_ids', set())

        if vehicle_id in selected_ids:
            selected_ids.remove(vehicle_id)
        else:
            selected_ids.add(vehicle_id)

        await state.update_data(selected_vehicle_ids=selected_ids)

        async with async_session() as session:
            available_vehicles = await session.scalars(
                select(Vehicle).where(Vehicle.status == 'available').order_by(Vehicle.model)
            )
            vehicle_list = available_vehicles.all()

        keyboard = get_vehicle_select_keyboard(vehicle_list, selected_ids)
        await callback.message.edit_reply_markup(reply_markup=keyboard)

    except (ValueError, IndexError) as e:
        logging.error(f"Ошибка обработки выбора техники: {e}, data: {callback.data}")
    except Exception as e:
        logging.exception(f"Ошибка в handle_vehicle_toggle: {e}")

# --- Вспомогательная функция для показа сводки ---
async def show_confirmation_summary(message_or_callback: types.Message | types.CallbackQuery, state: FSMContext):
    """Формирует и показывает сводку перед подтверждением."""
    data = await state.get_data()
    selected_personnel_ids = list(data.get('selected_personnel_ids', set()))
    selected_vehicle_ids = list(data.get('selected_vehicle_ids', set()))

    personnel_names = ["Не выбран"]
    vehicle_names = ["Не выбрана"]

    async with async_session() as session:
        if selected_personnel_ids:
            pers_result = await session.scalars(
                select(Employee.full_name).where(Employee.id.in_(selected_personnel_ids)).order_by(Employee.full_name)
            )
            personnel_names = pers_result.all() or ["Не найдены"]
        if selected_vehicle_ids:
            veh_result = await session.scalars(
                select(Vehicle.number_plate).where(Vehicle.id.in_(selected_vehicle_ids)).order_by(Vehicle.number_plate)
            )
            vehicle_names = veh_result.all() or ["Не найдены"]

    confirmation_text = (
        "🚨 **Новый выезд (проверьте данные):**\n\n"
        f"**Адрес:** {data['address']}\n"
        f"**Причина:** {data['reason']}\n"
        f"**Личный состав:** {', '.join(personnel_names)}\n"
        f"**Техника:** {', '.join(vehicle_names)}\n\n"
        "Отправить на утверждение начальнику караула?"
    )

    if isinstance(message_or_callback, types.CallbackQuery):
        await message_or_callback.message.edit_text(
            confirmation_text,
            reply_markup=confirm_cancel_dispatch_keyboard(),
            parse_mode="Markdown"
        )
    else: # Если это types.Message
        await message_or_callback.answer(
            confirmation_text,
            reply_markup=confirm_cancel_dispatch_keyboard(),
            parse_mode="Markdown"
        )
    await state.set_state(DispatchCreationStates.CONFIRMATION)
    logging.info(f"Состояние: CONFIRMATION")
    
# --- Обработчик кнопки "К подтверждению" ---
async def handle_vehicles_done(callback: types.CallbackQuery, state: FSMContext):
    """Переход к финальному подтверждению."""
    await callback.answer()
    # Можно добавить проверку, выбрана ли хотя бы одна машина, если это обязательно
    data = await state.get_data()
    if not data.get('selected_vehicle_ids'):
        await callback.answer("Вы не выбрали технику!", show_alert=True)
        return
    logging.info(f"Диспетчер {callback.from_user.id} завершил выбор техники.")
    await show_confirmation_summary(callback, state) # Показываем сводку

async def process_personnel(message: types.Message, state: FSMContext):
    personnel_text = message.text.strip()
    if not personnel_text:
        await message.answer("Список личного состава не может быть пустым:", reply_markup=get_cancel_keyboard())
        return
    await state.update_data(personnel_text=personnel_text)
    await message.answer(
        "Перечислите гос. номера назначаемой техники через запятую:",
        reply_markup=get_cancel_keyboard() # Добавляем кнопку
    )
    await state.set_state(DispatchCreationStates.ENTERING_VEHICLES)
    logging.info(f"Диспетчер {message.from_user.id}, ЛС: '{personnel_text}'...")

async def process_vehicles(message: types.Message, state: FSMContext):
    """Обработка списка техники и вывод на подтверждение."""
    vehicles_text = message.text.strip()
    if not vehicles_text:
        await message.answer("Список техники не может быть пустым. Перечислите гос. номера через запятую:")
        return
    await state.update_data(vehicles_text=vehicles_text)
    logging.info(f"Диспетчер {message.from_user.id}, техника: '{vehicles_text}'. Состояние: CONFIRMATION")

    # Показ сводки для подтверждения
    data = await state.get_data()
    confirmation_text = (
        "🚨 **Новый выезд (проверьте данные):**\n\n"
        f"**Адрес:** {data['address']}\n"
        f"**Причина:** {data['reason']}\n"
        f"**Личный состав:** {data['personnel_text']}\n"
        f"**Техника:** {data['vehicles_text']}\n\n"
        "Отправить на утверждение начальнику караула?"
    )

    await message.answer(
        confirmation_text,
        reply_markup=confirm_cancel_dispatch_keyboard(),
        parse_mode="Markdown" # Используем Markdown для выделения
    )
    await state.set_state(DispatchCreationStates.CONFIRMATION)


async def process_dispatch_confirmation(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    """Обработка подтверждения или отмены создания выезда."""
    await callback.answer() # Отвечаем на callback
    user_id = callback.from_user.id # telegram_id диспетчера

    if callback.data == "dispatch_confirm":
        data = await state.get_data()
        logging.info(f"Диспетчер {user_id} подтвердил создание выезда.")

        # --- Получаем ID из state ---
        selected_personnel_ids = list(data.get('selected_personnel_ids', []))
        selected_vehicle_ids = list(data.get('selected_vehicle_ids', []))
        # --- Конец получения ID ---

        try:
            async with async_session() as session:
                # --- Получаем ПОЛНЫЙ объект диспетчера ---
                dispatcher_result = await session.execute(
                    select(Employee).where(Employee.telegram_id == user_id)
                )
                dispatcher = dispatcher_result.scalar_one_or_none()

                if not dispatcher:
                    await callback.message.edit_text("❌ Ошибка: Не удалось идентифицировать вас как диспетчера.")
                    await state.clear()
                    return
                # --- Конец получения объекта диспетчера ---
                
                dispatcher_id = dispatcher.id # Теперь у нас есть ID для сохранения
                # Преобразуем строки ЛС и техники в JSON списки
                #personnel_list = [p.strip() for p in data['personnel_text'].split(',') if p.strip()]
                #vehicle_list = [v.strip() for v in data['vehicles_text'].split(',') if v.strip()]

                # Создаем запись в БД
                # --- Сохраняем списки ID ---
                new_dispatch = DispatchOrder(
                    dispatcher_id=dispatcher_id,
                    address=data['address'],
                    reason=data['reason'],
                    assigned_personnel_ids=json.dumps(selected_personnel_ids), # Сохраняем ID
                    assigned_vehicle_ids=json.dumps(selected_vehicle_ids),     # Сохраняем ID
                    status='pending_approval'
                )
                session.add(new_dispatch)
                # Важно: коммитим ЗДЕСЬ, чтобы получить new_dispatch.id для отправки
                await session.commit()
                dispatch_id = new_dispatch.id
                logging.info(f"Выезд ID {dispatch_id} сохранен в БД со статусом 'pending_approval'.")

                # --- Отправка уведомления Начальнику Караула ---
                try:
                    # --- ИЗМЕНЕНИЕ ЗДЕСЬ ---
                    # Ищем точное совпадение с названием на кнопке, но регистронезависимо все равно
                    search_position_term = "Начальник караула"
                    logging.info(f"Ищем НК с должностью '{search_position_term}' (через ilike)")
                    commander_result = await session.execute(
                        select(Employee)
                        # Используем ilike на случай, если в будущем появятся вариации,
                        # но ищем теперь строку с большой буквы
                        .where(Employee.position.ilike(search_position_term))
                        .limit(1)
                    )
                    # --- КОНЕЦ ИЗМЕНЕНИЯ ---
                    commander = commander_result.scalar_one_or_none()

                    if commander and commander.telegram_id:
                        commander_telegram_id = commander.telegram_id
                        logging.info(f"Найден НК: {commander.full_name} (Telegram ID: {commander_telegram_id})")

                        # Формируем текст уведомления для НК
                        nk_notification_text = (
                            f"❗️ Поступил новый выезд №{dispatch_id} на утверждение:\n\n"
                            f"**Адрес:** {new_dispatch.address}\n"
                            f"**Причина:** {new_dispatch.reason}\n"
                            # Можно добавить ЛС и Технику при желании
                            f"**(Создан диспетчером:** {dispatcher.full_name})" # Добавим, кто создал
                        )
                        # Генерируем клавиатуру для НК
                        nk_keyboard = get_dispatch_approval_keyboard(dispatch_id)

                        # Отправляем сообщение НК с помощью объекта bot
                        await bot.send_message(
                            chat_id=commander_telegram_id,
                            text=nk_notification_text,
                            reply_markup=nk_keyboard,
                            parse_mode="Markdown"
                        )
                        logging.info(f"Уведомление о выезде ID {dispatch_id} отправлено НК {commander_telegram_id}")
                        dispatcher_confirm_text = f"✅ Выезд №{dispatch_id} создан и отправлен на утверждение НК ({commander.full_name})."

                    else:
                        logging.warning(f"Не найден НК для отправки уведомления о выезде ID {dispatch_id}.")
                        dispatcher_confirm_text = f"✅ Выезд №{dispatch_id} создан, но не удалось найти НК для отправки уведомления."

                except Exception as notify_err:
                    logging.exception(f"Ошибка при отправке уведомления НК о выезде ID {dispatch_id}: {notify_err}")
                    dispatcher_confirm_text = f"✅ Выезд №{dispatch_id} создан, но произошла ошибка при отправке уведомления НК."
                
                # --- Конец уведомления ---

                # Сообщаем диспетчеру результат
                await callback.message.edit_text(dispatcher_confirm_text, reply_markup=None)

        except Exception as e:
            logging.exception(f"Ошибка сохранения выезда в БД: {e}")
            await callback.message.edit_text("❌ Произошла ошибка при сохранении выезда.")

        await state.clear() # Очищаем состояние в любом случае

    elif callback.data == "dispatch_cancel":
        logging.info(f"Диспетчер {user_id} отменил создание выезда.")
        await callback.message.edit_text("❌ Создание выезда отменено.", reply_markup=None)
        await state.clear()

async def process_absent_employee_fullname(message: types.Message, state: FSMContext):
    fullname = message.text.strip()
    if len(fullname.split()) < 2: # Простая проверка
        await message.answer(
            "ФИО должно состоять хотя бы из двух слов. Попробуйте еще раз:",
            reply_markup=get_cancel_keyboard(callback_data="cancel_absence_registration")
        )
        return
    await state.update_data(absent_employee_fullname=fullname)
    await message.answer(
        "Введите должность отсутствующего сотрудника (например, Пожарный):",
        reply_markup=get_cancel_keyboard(callback_data="cancel_absence_registration")
    )
    await state.set_state(AbsenceRegistrationStates.WAITING_FOR_ABSENT_EMPLOYEE_POSITION)

async def process_absent_employee_position(message: types.Message, state: FSMContext):
    position = message.text.strip()
    if not position:
        await message.answer(
            "Должность не может быть пустой. Введите должность:",
            reply_markup=get_cancel_keyboard(callback_data="cancel_absence_registration")
        )
        return
    await state.update_data(absent_employee_position=position)
    await message.answer(
        "Введите звание отсутствующего сотрудника (например, Сержант):",
        reply_markup=get_cancel_keyboard(callback_data="cancel_absence_registration")
    )
    await state.set_state(AbsenceRegistrationStates.WAITING_FOR_ABSENT_EMPLOYEE_RANK)

async def process_absent_employee_rank(message: types.Message, state: FSMContext):
    rank = message.text.strip()
    # Можно сделать звание необязательным, если раскомментировать и добавить кнопку "Пропустить"
    # if not rank:
    #     await message.answer("Звание не может быть пустым. Введите звание:", reply_markup=get_cancel_keyboard(callback_data="cancel_absence_registration"))
    #     return
    await state.update_data(absent_employee_rank=rank if rank else "б/з") # б/з - без звания
    await message.answer(
        "Введите причину отсутствия (например, Болезнь, Отпуск):",
        reply_markup=get_cancel_keyboard(callback_data="cancel_absence_registration")
    )
    await state.set_state(AbsenceRegistrationStates.WAITING_FOR_ABSENCE_REASON)

async def process_absence_reason(message: types.Message, state: FSMContext):
    reason = message.text.strip()
    if not reason:
        await message.answer(
            "Причина отсутствия не может быть пустой. Введите причину:",
            reply_markup=get_cancel_keyboard(callback_data="cancel_absence_registration")
        )
        return
    await state.update_data(absence_reason=reason)
    
    # Показываем сводку для подтверждения
    data = await state.get_data()
    summary_text = (
        f"<b>Проверьте данные об отсутствующем:</b>\n\n"
        f"<b>Караул:</b> №{data.get('karakul_number_reported_for', 'не указан (текущая дата)')}\n"
        f"<b>ФИО:</b> {data.get('absent_employee_fullname')}\n"
        f"<b>Должность:</b> {data.get('absent_employee_position')}\n"
        f"<b>Звание:</b> {data.get('absent_employee_rank', 'б/з')}\n"
        f"<b>Причина:</b> {data.get('absence_reason')}\n\n"
        f"Подтверждаете запись?"
    )
    await message.answer(summary_text, reply_markup=confirm_cancel_absence_keyboard(), parse_mode="HTML")
    await state.set_state(AbsenceRegistrationStates.CONFIRM_ABSENCE_ENTRY)

async def process_absence_confirmation(callback: types.CallbackQuery, state: FSMContext, session_factory: async_sessionmaker):
    await callback.answer()
    data = await state.get_data()

    if callback.data == "absence_confirm":
        try:
            async with session_factory() as session:
                async with session.begin():
                    new_absence_log = AbsenceLog(
                        reporter_employee_id=data['reporter_employee_id'],
                        karakul_number_reported_for=data.get('karakul_number_reported_for'), # Может быть None
                        # absence_date по умолчанию datetime.now в модели
                        absent_employee_fullname=data['absent_employee_fullname'],
                        absent_employee_position=data['absent_employee_position'],
                        absent_employee_rank=data.get('absent_employee_rank'), # Может быть "б/з" или None если модель позволяет
                        reason=data['absence_reason']
                        # reported_at по умолчанию datetime.now в модели
                    )
                    session.add(new_absence_log)
                    # Коммит при выходе из session.begin()
                
                await callback.message.edit_text(
                    f"✅ Запись об отсутствии сотрудника {data['absent_employee_fullname']} успешно сохранена.",
                    reply_markup=None
                )
                logging.info(f"Диспетчер {data['reporter_employee_id']} сохранил запись об отсутствии: {new_absence_log.id}")

        except Exception as e:
            logging.exception(f"Ошибка сохранения записи об отсутствующем: {e}")
            await callback.message.edit_text("❌ Произошла ошибка при сохранении записи.", reply_markup=None)
        finally:
            await state.clear()

    elif callback.data == "absence_edit": # TODO: Реализовать редактирование
        await callback.message.edit_text("Функция редактирования будет добавлена позже. Пока запись отменена.", reply_markup=None)
        await state.clear() # Пока просто отменяем
    
    elif callback.data == "absence_cancel_final":
        await callback.message.edit_text("❌ Создание записи об отсутствующем отменено.", reply_markup=None)
        await state.clear()

# Хэндлер отмены для этого FSM
async def cancel_absence_registration_handler(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Отменено")
    await callback.message.edit_text("Действие по отметке отсутствующего отменено.", reply_markup=None)
    await state.clear()

async def show_active_dispatches(message: types.Message):
    """Показывает первую страницу активных выездов."""
    async with async_session() as session:
        text, reply_markup = await _generate_dispatch_list_page(session, page=1, list_type='active')
        await message.answer(text, reply_markup=reply_markup)

async def show_archived_dispatches(message: types.Message):
    """Показывает первую страницу архивных выездов."""
    async with async_session() as session:
        text, reply_markup = await _generate_dispatch_list_page(session, page=1, list_type='archived')
        await message.answer(text, reply_markup=reply_markup)

# --- Обработчик для пагинации списков выездов ---

async def handle_dispatch_list_pagination(callback: types.CallbackQuery):
    """Обрабатывает нажатия кнопок пагинации списков выездов."""
    try:
        # Извлекаем тип списка и номер страницы из callback_data (формат: dispatch_list_{list_type}_{page})
        parts = callback.data.split('_')
        if len(parts) != 4 or parts[0] != 'dispatch' or parts[1] != 'list':
            raise ValueError("Invalid callback data format")

        list_type = parts[2] # 'active' or 'archived'
        page = int(parts[3])

        async with async_session() as session:
            text, reply_markup = await _generate_dispatch_list_page(session, page=page, list_type=list_type)

            # Используем edit_text для изменения существующего сообщения
            await callback.message.edit_text(text, reply_markup=reply_markup)
        await callback.answer() # Отвечаем на callback

    except (ValueError, IndexError) as e:
        logging.error(f"Ошибка обработки пагинации списка выездов: {e}, data: {callback.data}")
        await callback.answer("Ошибка при переключении страницы.", show_alert=True)
    except Exception as e:
        logging.exception(f"Непредвиденная ошибка при пагинации списка выездов: {e}")
        await callback.answer("Произошла ошибка.", show_alert=True)

async def start_dispatch_edit(callback: types.CallbackQuery, state: FSMContext, session_factory: async_sessionmaker): # Добавил session_factory
    await callback.answer()
    try:
        dispatch_id = int(callback.data.split("_")[-1])
    except (IndexError, ValueError):
        logging.error(f"Ошибка извлечения dispatch_id для начала редактирования: {callback.data}")
        await callback.message.answer("Ошибка: неверный ID выезда для редактирования.")
        return

    # Сохраняем ID выезда и текущие значения (если хотим их показывать при редактировании)
    async with session_factory() as session:
        dispatch = await session.get(DispatchOrder, dispatch_id)
        if not dispatch:
            await callback.message.edit_text(f"❌ Выезд №{dispatch_id} не найден для редактирования.", reply_markup=None)
            return
        # Проверка статуса и прав на редактирование (можно повторить, если нужно)
        editable_statuses = ['pending_approval', 'approved', 'dispatched', 'in_progress']
        current_user_employee = await session.scalar(
            select(Employee).where(Employee.telegram_id == callback.from_user.id)
        )
        if not (current_user_employee and current_user_employee.id == dispatch.dispatcher_id and dispatch.status in editable_statuses):
            await callback.message.answer("Вы не можете редактировать этот выезд или он в нередактируемом статусе.", reply_markup=None)
            return

    await state.update_data(
        editing_dispatch_id=dispatch_id,
        # Можно сохранить текущие значения, чтобы потом предлагать их для изменения или показывать
        current_victims=dispatch.victims_count,
        current_fatalities=dispatch.fatalities_count,
        current_casualties_details=dispatch.details_on_casualties,
        current_notes=dispatch.notes
    )
    
    await callback.message.edit_text(
        f"Редактирование выезда №{dispatch_id}.\nВыберите поле для изменения:",
        reply_markup=get_dispatch_edit_field_keyboard(dispatch_id)
    )
    await state.set_state(DispatchEditStates.CHOOSING_FIELD_TO_EDIT)

async def process_fatalities_count_input(message: types.Message, state: FSMContext, session_factory: async_sessionmaker): # session_factory здесь не используется, но для единообразия можно оставить
    try:
        new_value = int(message.text.strip())
        if new_value < 0:
            await message.answer("Количество погибших не может быть отрицательным. Введите корректное число:")
            return
    except ValueError:
        await message.answer("Пожалуйста, введите число (например, 0, 1, 2 и т.д.):")
        return

    data = await state.get_data()
    dispatch_id = data.get("editing_dispatch_id")
    field_being_edited = data.get("field_being_edited") # Должно быть "fatalities_count"

    if not dispatch_id or field_being_edited != "fatalities_count":
        await message.answer("Произошла ошибка сессии редактирования (ожидалось поле 'fatalities_count'). Попробуйте начать заново.")
        await state.clear()
        return
    
    await state.update_data(new_value_for_field=new_value)

    await message.answer(
        f"Вы уверены, что хотите изменить 'Кол-во погибших' для выезда №{dispatch_id} на <b>{new_value}</b>?",
        reply_markup=get_confirm_cancel_edit_keyboard(dispatch_id),
        parse_mode="HTML"
    )
    await state.set_state(DispatchEditStates.CONFIRM_DISPATCH_EDIT)

async def process_casualties_details_input(message: types.Message, state: FSMContext, session_factory: async_sessionmaker): # session_factory здесь не используется
    new_value = message.text.strip()
    # Для текстового поля можно не делать строгую валидацию на непустоту,
    # так как пользователь может захотеть очистить поле.
    # if not new_value:
    #     await message.answer("Описание не может быть пустым. Введите текст:")
    #     return

    data = await state.get_data()
    dispatch_id = data.get("editing_dispatch_id")
    field_being_edited = data.get("field_being_edited") # "details_on_casualties"

    if not dispatch_id or field_being_edited != "details_on_casualties":
        await message.answer("Произошла ошибка сессии редактирования (ожидалось поле 'details_on_casualties'). Попробуйте начать заново.")
        await state.clear()
        return
    
    await state.update_data(new_value_for_field=new_value)

    await message.answer(
        f"Вы уверены, что хотите изменить 'Детали по пострадавшим/погибшим' для выезда №{dispatch_id} на:\n"
        f"<code>{new_value if new_value else '(очистить поле)'}</code>?",
        reply_markup=get_confirm_cancel_edit_keyboard(dispatch_id),
        parse_mode="HTML"
    )
    await state.set_state(DispatchEditStates.CONFIRM_DISPATCH_EDIT)

async def process_general_notes_input(message: types.Message, state: FSMContext, session_factory: async_sessionmaker): # session_factory здесь не используется
    new_value = message.text.strip()

    data = await state.get_data()
    dispatch_id = data.get("editing_dispatch_id")
    field_being_edited = data.get("field_being_edited") # "notes"

    if not dispatch_id or field_being_edited != "notes":
        await message.answer("Произошла ошибка сессии редактирования (ожидалось поле 'notes'). Попробуйте начать заново.")
        await state.clear()
        return
    
    await state.update_data(new_value_for_field=new_value)

    await message.answer(
        f"Вы уверены, что хотите изменить 'Общие примечания' для выезда №{dispatch_id} на:\n"
        f"<code>{new_value if new_value else '(очистить поле)'}</code>?",
        reply_markup=get_confirm_cancel_edit_keyboard(dispatch_id),
        parse_mode="HTML"
    )
    await state.set_state(DispatchEditStates.CONFIRM_DISPATCH_EDIT)

# --- Регистрация обработчиков ---
def register_dispatcher_handlers(router: Router):
    """Регистрирует все обработчики для роли Диспетчер."""
    logging.info("Регистрируем обработчики диспетчера...")
    
    # --- Обработчики текстовых кнопок меню ---
    router.message.register(
        handle_new_dispatch_request,
        F.text == "🔥 Создать новый выезд"
    )
    router.message.register(
        show_active_dispatches,
        F.text == "📊 Активные выезды"
    )
    router.message.register(
        show_archived_dispatches,
        F.text == "📂 Архив выездов"
    )
    async def full_dispatch_details_entry_point(callback: types.CallbackQuery, state: FSMContext): # state здесь может не понадобиться
        await show_full_dispatch_details(callback, async_session) # async_session - ваш session_factory
    
    router.callback_query.register(
        full_dispatch_details_entry_point, 
        F.data.startswith("dispatch_full_details_")
    )

    # Хэндлер для ввода кол-ва погибших
    async def process_fatalities_input_entry_point(message: types.Message, state: FSMContext):
        await process_fatalities_count_input(message, state, async_session) # async_session здесь не используется, но для единообразия
    router.message.register(process_fatalities_input_entry_point, DispatchEditStates.ENTERING_FATALITIES_COUNT)

    # Хэндлер для ввода деталей по пострадавшим/погибшим
    async def process_casualties_details_input_entry_point(message: types.Message, state: FSMContext):
        await process_casualties_details_input(message, state, async_session)
    router.message.register(process_casualties_details_input_entry_point, DispatchEditStates.ENTERING_CASUALTIES_DETAILS)

    # Хэндлер для ввода общих примечаний
    async def process_general_notes_input_entry_point(message: types.Message, state: FSMContext):
        await process_general_notes_input(message, state, async_session)
    router.message.register(process_general_notes_input_entry_point, DispatchEditStates.ENTERING_GENERAL_NOTES)

    # Регистрация хэндлера для начала редактирования выезда
    async def start_dispatch_edit_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await start_dispatch_edit(callback, state, async_session) # async_session - ваш session_factory
    
    router.callback_query.register(
        start_dispatch_edit_entry_point, 
        F.data.startswith("dispatch_edit_start_")
        # Можно добавить StateFilter(None), если эта кнопка может быть нажата вне любого состояния
    )

    # --- Обработчик пагинации списков ---
    router.callback_query.register(
        handle_dispatch_list_pagination,
        F.data.startswith("dispatch_list_") # Фильтр по префиксу callback_data
    )
    
    # Обработчики состояний FSM
    router.message.register(process_address, DispatchCreationStates.ENTERING_ADDRESS)
    router.message.register(process_reason, DispatchCreationStates.ENTERING_REASON)

    # Новые обработчики выбора
    router.callback_query.register(handle_personnel_toggle, DispatchCreationStates.SELECTING_PERSONNEL, F.data.startswith("dispatch_toggle_personnel_"))
    router.callback_query.register(handle_personnel_done, DispatchCreationStates.SELECTING_PERSONNEL, F.data == "dispatch_personnel_done")
    router.callback_query.register(handle_vehicle_toggle, DispatchCreationStates.SELECTING_VEHICLES, F.data.startswith("dispatch_toggle_vehicle_"))
    router.callback_query.register(handle_vehicles_done, DispatchCreationStates.SELECTING_VEHICLES, F.data == "dispatch_vehicles_done")

    # Хэндлер для выбора поля для редактирования И для отмены всего редактирования из этого же меню
    router.callback_query.register(
        handle_field_to_edit_choice,
        # Ловит и выбор поля (edit_dispatch_field_...) 
        # И общую отмену редактирования (edit_dispatch_cancel_...)
        F.data.startswith("edit_dispatch_field_") | F.data.startswith("edit_dispatch_cancel_"), 
        DispatchEditStates.CHOOSING_FIELD_TO_EDIT
    )

    # Хэндлер для ввода кол-ва пострадавших
    async def process_victims_input_entry_point(message: types.Message, state: FSMContext):
        await process_victims_count_input(message, state, async_session)
    router.message.register(process_victims_input_entry_point, DispatchEditStates.ENTERING_VICTIMS_COUNT)

    # TODO: Создать и зарегистрировать аналогичные хэндлеры (и entry_point обертки) для:
    # - ENTERING_FATALITIES_COUNT -> process_fatalities_count_input
    # - ENTERING_CASUALTIES_DETAILS -> process_casualties_details_input
    # - ENTERING_GENERAL_NOTES -> process_general_notes_input

    # Хэндлер для подтверждения/отмены сохранения конкретного изменения
    async def process_dispatch_field_save_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await process_dispatch_field_save(callback, state, async_session)
    router.callback_query.register(
        process_dispatch_field_save_entry_point,
        F.data.startswith("edit_dispatch_save_change_") | F.data.startswith("edit_dispatch_cancel_change_"), # Ловим и сохранение, и отмену изменения
        DispatchEditStates.CONFIRM_DISPATCH_EDIT
    )

    # Хэндлер для кнопки "Отменить это изменение" (которая возвращает к выбору поля)
    # Это callback_data=f"edit_dispatch_cancel_change_{dispatch_id}"
    # Он будет срабатывать из разных состояний ввода (ENTERING_VICTIMS_COUNT и т.д.)
    # Поэтому его нужно зарегистрировать для этих состояний или использовать StateFilter(*)
    router.callback_query.register(
        cancel_specific_field_edit, # Эта функция возвращает к CHOOSING_FIELD_TO_EDIT
        F.data.startswith("edit_dispatch_cancel_change_"),
        StateFilter(
            DispatchEditStates.ENTERING_VICTIMS_COUNT,
            DispatchEditStates.ENTERING_FATALITIES_COUNT,
            DispatchEditStates.ENTERING_CASUALTIES_DETAILS,
            DispatchEditStates.ENTERING_GENERAL_NOTES
            # Не добавляем CONFIRM_DISPATCH_EDIT, так как для него уже есть обработка в process_dispatch_field_save
        )
    )

    # Обработчик отмены для FSM
    router.callback_query.register(
        cancel_dispatch_creation,
        # Добавляем новые состояния
        DispatchCreationStates.ENTERING_ADDRESS,
        DispatchCreationStates.ENTERING_REASON,
        DispatchCreationStates.SELECTING_PERSONNEL,
        DispatchCreationStates.SELECTING_VEHICLES,
        F.data == "dispatch_create_cancel"
    )

    # Обработчик inline-кнопок подтверждения/отмены
    router.callback_query.register(
        process_dispatch_confirmation,
        DispatchCreationStates.CONFIRMATION,
        F.data.in_(['dispatch_confirm', 'dispatch_cancel'])
    )

    # TODO: Добавить обработчики для кнопок "Активные выезды", "Архив выездов" и т.д.

    logging.info("Обработчики диспетчера зарегистрированы.")

