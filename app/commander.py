import json
from aiogram import F, types, Router, Bot
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext # Если не используется напрямую в этом файле, можно убрать
from aiogram.fsm.state import State, StatesGroup # Если не используется напрямую в этом файле, можно убрать
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker # async_sessionmaker нужен
from sqlalchemy.orm import selectinload
from datetime import datetime, date
from .dispatcher import show_full_dispatch_details 
from .shift_management import get_active_shift
# Импортируем модели и session_factory
from models import (
    Employee,
    Vehicle,
    DispatchOrder,
    Equipment,
    ShiftLog,
    AbsenceLog,
    EquipmentLog,
    async_session # Это ваш session_factory из models.py
)
from app.keyboards import (
    get_dispatch_approval_keyboard,
    get_cancel_keyboard,
    get_equipment_maintenance_action_keyboard,
    get_maintenance_confirmation_keyboard) # Убедитесь, что клавиатура импортируется
# Импортируем константы статусов и хелпер пагинации из dispatcher
from .dispatcher import (
    STATUS_TRANSLATIONS,
    ACTIVE_DISPATCH_STATUSES,
    _generate_dispatch_list_page # Если используется
)
import logging
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton # Для кнопки "Детали выезда"

class EquipmentMaintenanceStates(StatesGroup):
    CHOOSING_EQUIPMENT = State()      # НК выбирает снаряжение для обслуживания
    CHOOSING_ACTION = State()         # НК выбирает действие (в строй, в ремонт, списать)
    ENTERING_NOTES = State()          # (Опционально) НК вводит примечание
    CONFIRMING_ACTION = State()       # НК подтверждает действие

async def confirm_and_save_maintenance_action(callback: types.CallbackQuery, state: FSMContext, session_factory: async_sessionmaker):
    await callback.answer()
    data = await state.get_data()
    equipment_id = data.get("selected_equipment_id")
    equipment_name = data.get("selected_equipment_name", "Снаряжение")
    action_type = data.get("maintenance_action_type")
    notes_from_fsm = data.get("maintenance_notes", None)

    if not all([equipment_id, action_type]):
        await callback.message.edit_text("Ошибка: недостаточно данных для выполнения действия. Попробуйте снова.", reply_markup=None)
        await state.clear()
        return

    # callback_data для подтверждения: "maint_confirm_{action_type}_{equipment_id}"
    # callback_data для отмены этого действия: "maint_cancel_action_{equipment_id}"
    
    if callback.data.startswith("maint_confirm_"):
        new_status = ""
        log_action_description = ""
        # ... (определение new_status и log_action_description) ...
        if action_type == "available": new_status = "available"; log_action_description = "Поставлено в строй (исправно)"
        elif action_type == "maintenance": new_status = "maintenance"; log_action_description = "Отправлено на ТО/в ремонт"
        elif action_type == "decommission": new_status = "decommissioned"; log_action_description = "Списано"
        else:
            await callback.message.edit_text("Внутренняя ошибка: неизвестный тип действия для сохранения.", reply_markup=None)
            await state.clear()
            return

        nk_employee_id_for_log = None
        nk_employee_fullname_for_log = "НК не найден"
        active_nk_shift_id_for_log = None

        # --- Шаг 1: Получаем ID и ФИО НК в отдельной сессии ---
        try:
            async with session_factory() as prélim_session:
                nk_employee_prelim = await prélim_session.scalar(
                    select(Employee).where(Employee.telegram_id == callback.from_user.id)
                )
                if nk_employee_prelim:
                    nk_employee_id_for_log = nk_employee_prelim.id
                    nk_employee_fullname_for_log = nk_employee_prelim.full_name
                    # Проверяем активный караул НК тоже здесь, если это нужно для лога и не меняет БД
                    active_nk_shift_obj = await get_active_shift(session_factory, nk_employee_id_for_log)
                    if active_nk_shift_obj:
                        active_nk_shift_id_for_log = active_nk_shift_obj.id
                else:
                    await callback.message.edit_text("Ошибка идентификации Начальника Караула.", reply_markup=None)
                    await state.clear()
                    return
        except Exception as e_prelim:
            logging.exception(f"Ошибка на предварительном этапе получения данных НК: {e_prelim}")
            await callback.message.edit_text("Ошибка получения данных пользователя.", reply_markup=None)
            await state.clear()
            return
        # --- Конец Шага 1 ---

        try:
            # --- Шаг 2: Основная транзакция для изменения данных ---
            async with session_factory() as session: # Новая, "чистая" сессия для записи
                logging.info(f"SRV_DEBUG: confirm_maint (main block): Session CREATED. Is active? {session.in_transaction()}")
                # "Хак" на всякий случай, если и эта сессия почему-то будет в транзакции
                if session.in_transaction():
                    logging.warning(f"SRV_DEBUG: confirm_maint (main block): Transaction was unexpectedly active. Attempting to commit.")
                    try: await session.commit()
                    except: await session.rollback()
                
                logging.info(f"SRV_DEBUG: confirm_maint (main block): BEFORE session.begin() - Is transaction active? {session.in_transaction()}")
                async with session.begin():
                    equipment_to_update = await session.get(Equipment, equipment_id) # type: ignore
                    if not equipment_to_update:
                        raise ValueError(f"Снаряжение {equipment_name} не найдено для обновления.")

                    equipment_to_update.status = new_status
                    if new_status == "available" or new_status == "decommissioned":
                        equipment_to_update.current_holder_id = None
                    session.add(equipment_to_update)

                    log_notes = f"НК ({nk_employee_fullname_for_log}): {log_action_description}."
                    if notes_from_fsm: log_notes += f" Примечание НК: {notes_from_fsm}"
                    
                    new_equip_log = EquipmentLog(
                        employee_id=nk_employee_id_for_log, # Используем ID, полученное ранее
                        equipment_id=equipment_id,
                        action=f"maintenance_{action_type}", 
                        notes=log_notes,
                        shift_log_id=active_nk_shift_id_for_log # Используем ID смены, полученное ранее
                    )
                    session.add(new_equip_log)
                
            await callback.message.edit_text(
                f"✅ Статус снаряжения <b>{equipment_name}</b> успешно изменен на '<b>{new_status}</b>'.\n"
                f"Действие: {log_action_description}.",
                parse_mode="HTML", reply_markup=None
            )
            logging.info(f"НК ID {nk_employee_id_for_log} изменил статус снаряжения ID {equipment_id} на {new_status}.")

        except ValueError as ve:
            logging.error(f"Ошибка значения в confirm_and_save_maintenance_action: {ve}")
            await callback.message.edit_text(str(ve), reply_markup=None)
        except Exception as e:
            logging.exception(f"Ошибка при сохранении изменений статуса снаряжения ID {equipment_id}: {e}")
            await callback.message.edit_text("Произошла ошибка при сохранении изменений.", reply_markup=None)
        finally:
            await state.clear()
    
    elif callback.data.startswith("maint_cancel_action_"):
        # ... (код для возврата к выбору действия, как был) ...
        await callback.message.edit_text(
            f"Действие для <b>{equipment_name}</b> отменено.\nВыберите другое действие:",
            reply_markup=get_equipment_maintenance_action_keyboard(equipment_id), # type: ignore
            parse_mode="HTML"
        )
        await state.set_state(EquipmentMaintenanceStates.CHOOSING_ACTION)

async def choose_maintenance_action(callback: types.CallbackQuery, state: FSMContext, session_factory: async_sessionmaker): # Добавил session_factory
    await callback.answer()
    data = await state.get_data()
    equipment_id = data.get("selected_equipment_id")
    equipment_name = data.get("selected_equipment_name", "Неизвестное снаряжение")

    if not equipment_id:
        await callback.message.edit_text("Ошибка: ID снаряжения не найден в сессии. Попробуйте начать заново.", reply_markup=None)
        await state.clear()
        return

    action_data = callback.data # Например, "maint_action_available_{equipment_id}"
    
    # Извлекаем само действие (available, maintenance, decommission)
    try:
        action_type = action_data.split("_")[2] # Третий элемент после "maint_action_"
        # Проверяем, что ID в callback_data совпадает с тем, что в FSM (дополнительная защита)
        action_equip_id = int(action_data.split("_")[-1])
        if action_equip_id != equipment_id:
            raise ValueError("ID снаряжения в callback не совпадает с ID в FSM.")
    except (IndexError, ValueError) as e:
        logging.error(f"Ошибка разбора callback_data для действия обслуживания: {callback.data}, ошибка: {e}")
        await callback.message.edit_text("Произошла ошибка при выборе действия.", reply_markup=None)
        await state.clear()
        return

    await state.update_data(maintenance_action_type=action_type)

    confirmation_prompt = ""
    next_fsm_state = EquipmentMaintenanceStates.CONFIRMING_ACTION # По умолчанию сразу на подтверждение
    reply_markup_for_next_step = get_maintenance_confirmation_keyboard(equipment_id, action_type) # Передаем action_type для кнопки подтверждения

    if action_type == "available":
        confirmation_prompt = f"Вы уверены, что хотите поставить <b>{equipment_name}</b> в строй (статус 'исправен / available')?"
    elif action_type == "maintenance":
        confirmation_prompt = f"Вы уверены, что хотите отправить <b>{equipment_name}</b> на ТО/в ремонт (статус 'maintenance')?"
        # Можно добавить шаг ввода примечаний для ТО/ремонта
        # await state.set_state(EquipmentMaintenanceStates.ENTERING_NOTES)
        # await callback.message.edit_text(f"Введите причину отправки {equipment_name} на ТО/в ремонт (или '-' если нет):", 
        #                                reply_markup=get_cancel_keyboard(f"maint_cancel_notes_{equipment_id}"))
        # return # Выходим, так как перешли в другое состояние
    elif action_type == "decommission":
        confirmation_prompt = f"<b>ВНИМАНИЕ!</b> Вы уверены, что хотите СПИСАТЬ <b>{equipment_name}</b> (статус 'decommissioned')?\nЭто действие обычно необратимо."
        # Можно принудительно запросить примечание для списания
        # await state.set_state(EquipmentMaintenanceStates.ENTERING_NOTES)
        # await callback.message.edit_text(f"Введите причину списания {equipment_name}:", 
        #                                reply_markup=get_cancel_keyboard(f"maint_cancel_notes_{equipment_id}"))
        # return
    else:
        await callback.message.edit_text("Неизвестное действие.", reply_markup=None)
        await state.set_state(EquipmentMaintenanceStates.CHOOSING_ACTION) # Возврат к выбору действия
        return

    await callback.message.edit_text(confirmation_prompt, reply_markup=reply_markup_for_next_step, parse_mode="HTML")
    await state.set_state(next_fsm_state)

async def choose_equipment_for_maintenance(callback: types.CallbackQuery, state: FSMContext, session_factory: async_sessionmaker):
    await callback.answer()
    try:
        equipment_id = int(callback.data.split("_")[-1])
    except (IndexError, ValueError):
        await callback.message.edit_text("Ошибка: неверный ID снаряжения.", reply_markup=None)
        await state.clear()
        return

    async with session_factory() as session:
        equipment = await session.get(Equipment, equipment_id)
        if not equipment:
            await callback.message.edit_text("Ошибка: снаряжение не найдено.", reply_markup=None)
            await state.clear()
            return

    await state.update_data(selected_equipment_id=equipment_id, selected_equipment_name=equipment.name)
    
    await callback.message.edit_text(
        f"Выбрано: <b>{equipment.name}</b> ({equipment.inventory_number or 'б/н'}), текущий статус: <i>{equipment.status}</i>.\n"
        "Какое действие выполнить?",
        reply_markup=get_equipment_maintenance_action_keyboard(equipment_id),
        parse_mode="HTML"
    )
    await state.set_state(EquipmentMaintenanceStates.CHOOSING_ACTION)

# Хэндлер для кнопки "Назад к выбору снаряжения"
async def back_to_equipment_list_for_maintenance(callback: types.CallbackQuery, state: FSMContext, session_factory: async_sessionmaker):
    # Эта функция фактически заново вызывает start_equipment_maintenance, но как callback
    # Для этого нужно, чтобы start_equipment_maintenance мог принимать и message, и callback.query
    # Пока сделаем проще:
    await callback.answer()
    # Поскольку start_equipment_maintenance принимает message, мы не можем его напрямую вызвать.
    # Нужно либо переделать start_equipment_maintenance, либо создать "псевдо-сообщение".
    # Самый простой вариант - просто отправить новое сообщение с тем же содержимым.
    # Это вызовет дублирование, но для начала сойдет.
    # ИЛИ: мы можем просто перегенерировать список и отредактировать сообщение.
    # Это будет лучше.
    
    # Удаляем выбранное снаряжение из FSM, чтобы не было путаницы
    await state.update_data(selected_equipment_id=None, selected_equipment_name=None) 
    
    # Повторно показываем список снаряжения (как в start_equipment_maintenance)
    async with session_factory() as session:
        equipment_to_service = await session.scalars(
            select(Equipment).where(Equipment.status.notin_(['available', 'decommissioned'])).order_by(Equipment.name) # type: ignore
        )
        equipment_list = equipment_to_service.all()

    if not equipment_list:
        await callback.message.edit_text("✅ Всё снаряжение в порядке или уже списано.", reply_markup=None)
        await state.clear() # Выходим из FSM
        return

    builder = InlineKeyboardBuilder()
    for item in equipment_list:
        status_emoji = {'maintenance': '🛠️', 'repair': '⚠️', 'in_use': '👨‍🚒'}.get(item.status, '❓')
        builder.button(
            text=f"{status_emoji} {item.name} ({item.inventory_number or 'б/н'}) - {item.status}",
            callback_data=f"maint_select_equip_{item.id}"
        )
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="maint_cancel_fsm"))

    await callback.message.edit_text(
        "Выберите снаряжение для изменения статуса/обслуживания:",
        reply_markup=builder.as_markup()
    )
    await state.set_state(EquipmentMaintenanceStates.CHOOSING_EQUIPMENT) # Возвращаем в состояние выбора

async def start_equipment_maintenance(message: types.Message, state: FSMContext, session_factory: async_sessionmaker):
    await state.clear()
    logging.info(f"НК {message.from_user.id} инициировал обслуживание снаряжения.")

    async with session_factory() as session:
        # Выбираем снаряжение, которое НЕ доступно и НЕ списано (т.е. требует внимания)
        # Или можно выбрать конкретные статусы, например, 'maintenance', 'repair'
        equipment_to_service = await session.scalars(
            select(Equipment)
            .where(
                Equipment.status.notin_(['available', 'decommissioned']) # type: ignore
            )
            .order_by(Equipment.name)
        )
        equipment_list = equipment_to_service.all()

    if not equipment_list:
        await message.answer("✅ Всё снаряжение в порядке или уже списано. Нет объектов для обслуживания.", reply_markup=None)
        return

    builder = InlineKeyboardBuilder()
    for item in equipment_list:
        status_emoji = {'maintenance': '🛠️', 'repair': '⚠️', 'in_use': '👨‍🚒'}.get(item.status, '❓')
        builder.button(
            text=f"{status_emoji} {item.name} ({item.inventory_number or 'б/н'}) - {item.status}",
            callback_data=f"maint_select_equip_{item.id}"
        )
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="maint_cancel_fsm"))

    await message.answer(
        "Выберите снаряжение для изменения статуса/обслуживания:",
        reply_markup=builder.as_markup()
    )
    await state.set_state(EquipmentMaintenanceStates.CHOOSING_EQUIPMENT)

# Хэндлер для общей отмены FSM обслуживания
async def cancel_equipment_maintenance_fsm(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Отменено")
    await callback.message.edit_text("Обслуживание снаряжения отменено.", reply_markup=None)
    await state.clear()

# --- Обработчик утверждения/отклонения выезда Начальником Караула (НК) ---
async def handle_dispatch_approval(callback: types.CallbackQuery, bot: Bot, session_factory: async_sessionmaker):
    await callback.answer() 

    action_parts = callback.data.split('_')
    if len(action_parts) != 3 or action_parts[0] != 'dispatch' or action_parts[1] not in ['approve', 'reject']:
        logging.error(f"Invalid approval callback data: {callback.data}")
        try:
            await callback.message.edit_text("❌ Ошибка: Некорректные данные для обработки.")
        except Exception: pass
        return

    action = action_parts[1] # 'approve' or 'reject'
    try:
        dispatch_id = int(action_parts[2])
    except ValueError:
        logging.error(f"Invalid dispatch_id in callback data: {callback.data}")
        try:
            await callback.message.edit_text("❌ Ошибка: Некорректный ID выезда.")
        except Exception: pass
        return

    commander_telegram_id = callback.from_user.id

    try:
        async with session_factory() as session: # Используем переданный session_factory
            # Блок транзакции для обновления DispatchOrder и получения данных НК
            async with session.begin():
                dispatch_order = await session.get(DispatchOrder, dispatch_id)
                if not dispatch_order:
                    await callback.message.edit_text("❌ Ошибка: Выезд не найден.")
                    return

                commander = await session.scalar(
                    select(Employee).where(Employee.telegram_id == commander_telegram_id)
                )
                if not commander:
                    await callback.message.edit_text("❌ Ошибка: Не удалось идентифицировать ваш профиль НК.")
                    return

                if dispatch_order.status != 'pending_approval':
                    current_status_ru = STATUS_TRANSLATIONS.get(dispatch_order.status, dispatch_order.status)
                    await callback.message.edit_text(f"❌ Этот выезд уже обработан (статус: {current_status_ru}). Действие отменено.")
                    return

                new_status = ''
                result_text_for_nk = ''
                assigned_personnel_ids_json = dispatch_order.assigned_personnel_ids # Для уведомлений

                if action == 'approve':
                    new_status = 'approved'
                    result_text_for_nk = f"✅ Выезд №{dispatch_id} УТВЕРЖДЕН вами."
                    dispatch_order.status = new_status
                    dispatch_order.commander_id = commander.id
                    dispatch_order.approval_time = datetime.now()
                    session.add(dispatch_order)
                    logging.info(f"НК {commander.full_name} ({commander_telegram_id}) утвердил выезд ID {dispatch_id}")
                    
                elif action == 'reject':
                    new_status = 'rejected'
                    result_text_for_nk = f"❌ Выезд №{dispatch_id} ОТКЛОНЕН вами."
                    dispatch_order.status = new_status
                    dispatch_order.commander_id = commander.id
                    dispatch_order.approval_time = datetime.now()
                    session.add(dispatch_order)
                    logging.info(f"НК {commander.full_name} ({commander_telegram_id}) отклонил выезд ID {dispatch_id}")
            
            # --- КОММИТ ПРОИЗОШЕЛ АВТОМАТИЧЕСКИ ПРИ ВЫХОДЕ ИЗ session.begin() ---

            # Уведомление Диспетчеру (используем ту же сессию, т.к. это чтение)
            try:
                # dispatch_order здесь уже имеет обновленные данные после коммита,
                # но для получения dispatcher нужен только dispatch_order.dispatcher_id, который не менялся.
                dispatcher = await session.get(Employee, dispatch_order.dispatcher_id)
                if dispatcher and dispatcher.telegram_id:
                    dispatcher_notification = (
                        f"ℹ️ Начальник караула ({commander.full_name if commander else 'НК'}) "
                        f"принял решение по выезду №{dispatch_id}:\n"
                        f"Статус: {STATUS_TRANSLATIONS.get(new_status, new_status)}"
                    )
                    await bot.send_message(
                        chat_id=dispatcher.telegram_id,
                        text=dispatcher_notification
                    )
                    logging.info(f"Уведомление о решении по выезду {dispatch_id} отправлено диспетчеру {dispatcher.telegram_id}")
                else:
                    logging.warning(f"Не удалось найти диспетчера ({dispatch_order.dispatcher_id}) для уведомления о решении по выезду {dispatch_id}")
            except Exception as notify_err:
                logging.exception(f"Ошибка отправки уведомления диспетчеру о решении по выезду {dispatch_id}: {notify_err}")

            # Уведомление назначенному персоналу, если выезд УТВЕРЖДЕН
            if action == 'approve' and new_status == 'approved' and assigned_personnel_ids_json:
                try:
                    personnel_ids_list = json.loads(assigned_personnel_ids_json) # Распарсить ID персонала
                    if isinstance(personnel_ids_list, list) and personnel_ids_list:
                        logging.info(f"Подготовка к отправке уведомлений о выезде ID {dispatch_id} персоналу: {personnel_ids_list}")
                        
                        # Получаем telegram_id всех назначенных сотрудников одним запросом
                        # Используем ту же сессию, так как это операция чтения
                        assigned_employees_query = select(Employee.telegram_id).where(
                            Employee.id.in_(personnel_ids_list), 
                            Employee.telegram_id.isnot(None) # type: ignore
                        )
                        assigned_employees_tg_ids_result = await session.scalars(assigned_employees_query)
                        
                        notification_text_personnel = (
                            f"📢 <b>ВНИМАНИЕ! Новый выезд!</b> 📢\n\n"
                            f"<b>Выезд №:</b> {dispatch_order.id}\n"
                            f"<b>Адрес:</b> {dispatch_order.address}\n"
                            f"<b>Причина:</b> {dispatch_order.reason}\n\n"
                            f"<i>Утвержден НК: {commander.full_name if commander else 'НК'}</i>"
                        )
                        
                        # Клавиатура для уведомления персонала
                        builder = InlineKeyboardBuilder()
                        builder.button(text="📋 Детали выезда", callback_data=f"dispatch_view_details_{dispatch_order.id}")
                        # Можно добавить кнопку "Принял", если нужна такая логика:
                        # builder.button(text="✅ Принял", callback_data=f"dispatch_ack_{dispatch_order.id}")
                        notification_markup = builder.as_markup()

                        for tg_id in assigned_employees_tg_ids_result.all():
                            try:
                                await bot.send_message(
                                    chat_id=tg_id,
                                    text=notification_text_personnel,
                                    parse_mode="HTML",
                                    reply_markup=notification_markup
                                )
                                logging.info(f"Уведомление о выезде {dispatch_id} отправлено сотруднику с Telegram ID {tg_id}")
                            except Exception as e_send_personnel:
                                logging.error(f"Не удалось отправить уведомление о выезде {dispatch_id} сотруднику с Telegram ID {tg_id}: {e_send_personnel}")
                    else:
                        logging.info(f"Список персонала для уведомления по выезду {dispatch_id} пуст или некорректен.")
                except json.JSONDecodeError:
                    logging.error(f"Ошибка декодирования JSON assigned_personnel_ids для выезда {dispatch_id}: {assigned_personnel_ids_json}")
                except Exception as e_notify_personnel:
                    logging.exception(f"Общая ошибка при уведомлении персонала о выезде {dispatch_id}: {e_notify_personnel}")
            
            # Редактируем сообщение НК, убирая кнопки
            await callback.message.edit_text(result_text_for_nk, reply_markup=None)

    except Exception as e:
        logging.exception(f"Непредвиденная ошибка в handle_dispatch_approval для выезда {dispatch_id}: {e}")
        try:
            await callback.message.edit_text("❌ Произошла серьезная ошибка при обработке вашего решения.")
        except Exception: pass



async def show_pending_approvals(message: types.Message):
    """Показывает список выездов, ожидающих утверждения НК."""
    logging.info(f"НК {message.from_user.id} запросил список выездов на утверждение.")
    async with async_session() as session:
        pending_orders = await session.scalars(
            select(DispatchOrder)
            .where(DispatchOrder.status == 'pending_approval')
            .order_by(DispatchOrder.creation_time.asc()) # Сначала самые старые
        )
        pending_orders_list = pending_orders.all()

        if not pending_orders_list:
            await message.answer("✅ Нет выездов, ожидающих вашего утверждения.")
            return

        response_text = "⏳ **Выезды на утверждение:**\n"
        # Отправляем каждый выезд отдельным сообщением с кнопками
        for order in pending_orders_list:
            # Можно добавить больше деталей при желании
            order_text = (
                f"🆔 Выезд №{order.id} от {order.creation_time.strftime('%d.%m %H:%M')}\n"
                f"📍 **Адрес:** {order.address}\n"
                f"📄 **Причина:** {order.reason}"
                # Можно добавить ЛС/Технику
            )
            keyboard = get_dispatch_approval_keyboard(order.id)
            await message.answer(order_text, reply_markup=keyboard)

        # Можно добавить пагинацию, если ожидается много ожидающих выездов,
        # но для утверждения часто удобнее видеть всё сразу или отправлять по одному.


async def show_all_active_dispatches_nk(message: types.Message):
    """Показывает НК первую страницу всех активных выездов (не только его)."""
    # Используем ту же функцию, что и диспетчер
    logging.info(f"НК {message.from_user.id} запросил список всех активных выездов.")
    async with async_session() as session:
        # Вызываем хелпер из dispatcher.py
        text, reply_markup = await _generate_dispatch_list_page(session, page=1, list_type='active')
        await message.answer(text, reply_markup=reply_markup)
        # Пагинация будет обрабатываться тем же хендлером handle_dispatch_list_pagination

async def show_personnel_vehicle_status_nk(message: types.Message, session_factory: async_sessionmaker):
    user_id = message.from_user.id
    logging.info(f"НК {user_id} запросил расширенный статус ЛС, техники и караулов.")
    
    response_parts = []
    current_date_obj = date.today() # Для фильтрации отсутствующих
    current_date_str = current_date_obj.strftime('%d.%m.%Y')

    all_active_shifts_list = [] # Сохраним все активные смены для последующего использования

    async with session_factory() as session:
        # 0. Определяем, на каком карауле НК (если на карауле)
        nk_employee = await session.scalar(select(Employee).where(Employee.telegram_id == user_id))
        nk_shift_karakul_number = None
        if nk_employee:
            # Локальный импорт, чтобы избежать циклических зависимостей, если они возможны
            from app.shift_management import get_active_shift 
            active_nk_shift = await get_active_shift(session_factory, nk_employee.id)
            if active_nk_shift:
                nk_shift_karakul_number = active_nk_shift.karakul_number
                response_parts.append(f"<b>Информация по вашему караулу №{nk_shift_karakul_number} на {current_date_str}:</b>")
            else:
                response_parts.append(f"<b>Общая сводка (вы не на активном карауле) на {current_date_str}:</b>")
        else:
            await message.answer("Ошибка: не удалось идентифицировать ваш профиль НК.")
            return

        # 1. Заступившие на караул (либо на караул НК, либо на все активные)
        response_parts.append("\n👨‍🚒 <b>Заступили на караул:</b>")
        shift_log_query = (
            select(ShiftLog)
            .options(
                selectinload(ShiftLog.employee), # Загружаем связанный объект Employee
                selectinload(ShiftLog.vehicle)   # Загружаем связанный объект Vehicle
            )
            .where(ShiftLog.status == 'active')
            .order_by(ShiftLog.karakul_number) # Сначала сортируем по номеру караула
        )
        if nk_shift_karakul_number:
            shift_log_query = shift_log_query.where(ShiftLog.karakul_number == nk_shift_karakul_number)
        
        all_active_shifts_result = await session.scalars(shift_log_query)
        all_active_shifts_list = all_active_shifts_result.all()

        # Сортируем по ФИО сотрудника в Python, так как это проще с загруженными объектами
        all_active_shifts_list.sort(key=lambda s: (s.karakul_number, s.employee.full_name if s.employee else ""))
        
        found_on_shift = False
        for shift in all_active_shifts_list:
            found_on_shift = True
            emp = shift.employee
            if not emp: continue # Пропускаем, если сотрудник почему-то не загрузился

            emp_info = f"- <b>{emp.full_name}</b> ({emp.position}, {emp.rank if emp.rank else 'б/з'})"
            if nk_shift_karakul_number is None: # Если показываем все караулы, добавляем номер караула
                emp_info += f" (Караул №{shift.karakul_number})"

            if emp.position.lower() == "водитель" and shift.vehicle:
                emp_info += f"\n  Авто: {shift.vehicle.model} ({shift.vehicle.number_plate}), ход: {shift.operational_priority or 'N/A'}"
            elif emp.position.lower() == "пожарный" and shift.sizod_number:
                emp_info += f"\n  СИЗОД: №{shift.sizod_number} (Сост. прием: {shift.sizod_status_start or 'N/A'})"
                if shift.sizod_notes_start and shift.sizod_notes_start.lower() != 'описание пропущено':
                    emp_info += f" <i>Прим: {shift.sizod_notes_start}</i>"
            response_parts.append(emp_info)
        if not found_on_shift:
            response_parts.append("  <i>Нет сотрудников на активных караулах (или на вашем карауле).</i>")

        # 2. Отсутствующие сотрудники (на сегодня)
        response_parts.append("\n🚫 <b>Отсутствующие сегодня:</b>")
        # Фильтруем отсутствующих либо для караула НК (если он на смене), либо всех на сегодня
        absence_query = select(AbsenceLog).where(func.date(AbsenceLog.absence_date) == current_date_obj)
        if nk_shift_karakul_number:
            # Показываем отсутствующих, отмеченных для этого караула ИЛИ тех, у кого караул не указан (общие отсутствующие)
            absence_query = absence_query.where(
                or_(
                    AbsenceLog.karakul_number_reported_for == nk_shift_karakul_number,
                    AbsenceLog.karakul_number_reported_for.is_(None) # type: ignore
                )
            )
        
        absences_result = await session.scalars(absence_query.order_by(AbsenceLog.absent_employee_fullname))
        absences_list = absences_result.all()
        
        found_absent = False
        for absence in absences_list:
            found_absent = True
            absence_info = (f"- <b>{absence.absent_employee_fullname}</b> ({absence.absent_employee_position}, {absence.absent_employee_rank or 'б/з'})"
                            f"\n  Причина: {absence.reason or 'не указана'}")
            if absence.karakul_number_reported_for and nk_shift_karakul_number is None : # Показываем для какого караула отмечен, если смотрим общую сводку
                 absence_info += f" (отм. для караула №{absence.karakul_number_reported_for})"
            response_parts.append(absence_info)
        if not found_absent:
            response_parts.append("  <i>Нет отмеченных отсутствующих на сегодня (или для вашего караула).</i>")

        # 3. Статус всей техники
        response_parts.append("\n🚒 <b>Статус всей техники:</b>")
        all_vehicles_result = await session.scalars(select(Vehicle).order_by(Vehicle.model))
        all_vehicles_list = all_vehicles_result.all()
        found_vehicles = False
        for vhc in all_vehicles_list:
            found_vehicles = True
            status_msg = {
                'available': '✅ Доступен', 'in_use': '🅿️ На карауле/выезде',
                'maintenance': '🛠 На ТО', 'repair': '⚠️ В ремонте'
            }.get(vhc.status, f'❓({vhc.status})')
            response_parts.append(f"- {vhc.model} ({vhc.number_plate}): {status_msg}")
        if not found_vehicles:
            response_parts.append("  <i>Нет данных о технике.</i>")

        # 4. Общий статус готовности личного состава (все сотрудники из Employee)
        response_parts.append("\n🧑‍🤝‍🧑 <b>Общая готовность ЛС (всего):</b>")
        all_personnel_result = await session.scalars(
            select(Employee).options(selectinload(Employee.held_equipment)).order_by(Employee.position, Employee.full_name)
        )
        all_personnel_list = all_personnel_result.all()
        
        ready_count = 0
        not_ready_count = 0
        personnel_details_parts = [] # Собираем сюда детали по каждому
        
        # Создаем set ID сотрудников, которые на активных сменах, для быстрой проверки
        employee_ids_on_active_shifts = {s.employee_id for s in all_active_shifts_list}

        for emp in all_personnel_list:
            ready_status_icon = "✅" if emp.is_ready else "❌"
            
            held_items_count = len(emp.held_equipment) # Используем загруженные данные
            held_str = f" (снаряж: {held_items_count} ед.)" if held_items_count > 0 else ""
            
            is_on_active_shift = emp.id in employee_ids_on_active_shifts
            shift_status_str = " (На карауле)" if is_on_active_shift else ""

            personnel_details_parts.append(f"- {ready_status_icon} {emp.full_name} ({emp.position}, {emp.rank or 'б/з'}){held_str}{shift_status_str}")
            if emp.is_ready:
                ready_count += 1
            else:
                not_ready_count += 1
        
        response_parts.append(f"  <b>Готовы: {ready_count}</b> | <b>Не готовы: {not_ready_count}</b>")
        # Раскомментируйте следующую строку, если нужен детальный список по каждому сотруднику:
        # response_parts.extend(personnel_details_parts)

    final_message = "\n".join(response_parts)
    
    # Отправка сообщения (с разбивкой, если слишком длинное)
    MAX_MESSAGE_LENGTH = 4096
    if len(final_message) > MAX_MESSAGE_LENGTH:
        logging.warning(f"НК {user_id}: Сообщение о статусе ЛС/техники слишком длинное ({len(final_message)} символов). Разбиваем...")
        for i in range(0, len(final_message), MAX_MESSAGE_LENGTH):
            try:
                await message.answer(final_message[i:i + MAX_MESSAGE_LENGTH], parse_mode="HTML")
            except Exception as e_send:
                logging.error(f"Ошибка отправки части сообщения НК: {e_send}")
                if i == 0: # Если даже первая часть не ушла
                    await message.answer("Не удалось отобразить статус: ошибка при отправке.")
                break # Прерываем отправку остальных частей
    else:
        try:
            await message.answer(final_message, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Ошибка отправки статуса ЛС/техники НК: {e}.")
            await message.answer("Не удалось отобразить статус: произошла ошибка.")


# --- Регистрация обработчиков ---
def register_commander_handlers(router: Router, bot: Bot): # <-- Принимаем bot
    """Регистрирует все обработчики для роли Начальник караула."""
    logging.info("Регистрируем обработчики начальника караула...")

    # --- Обработчики текстовых кнопок меню ---
    router.message.register(
        show_pending_approvals,
        F.text == "⏳ Выезды на утверждение"
        # Фильтр по роли НК
    )
    router.message.register(
        show_all_active_dispatches_nk,
        F.text == "🔥 Активные выезды (все)"
        # Фильтр по роли НК
    )
    
    # --- Обслуживание снаряжения FSM ---
    async def start_equipment_maintenance_entry_point(message: types.Message, state: FSMContext):
        await start_equipment_maintenance(message, state, async_session)
    router.message.register(start_equipment_maintenance_entry_point, F.text == "🔧 Обслуживание снаряжения")

    async def choose_equipment_for_maintenance_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await choose_equipment_for_maintenance(callback, state, async_session)
    router.callback_query.register(
        choose_equipment_for_maintenance_entry_point,
        F.data.startswith("maint_select_equip_"),
        EquipmentMaintenanceStates.CHOOSING_EQUIPMENT
    )

    async def back_to_equipment_list_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await back_to_equipment_list_for_maintenance(callback, state, async_session)
    router.callback_query.register(
        back_to_equipment_list_entry_point,
        F.data == "maint_back_to_list", # Кнопка "Назад к выбору снаряжения"
        EquipmentMaintenanceStates.CHOOSING_ACTION # Из состояния выбора действия
    )
    
    # Общая отмена FSM обслуживания
    router.callback_query.register(
        cancel_equipment_maintenance_fsm,
        F.data == "maint_cancel_fsm",
        StateFilter(EquipmentMaintenanceStates) # Для всех состояний этого FSM
    )
    
    async def handle_dispatch_approval_entry_point(callback: types.CallbackQuery):
        # async_session здесь - это ваш session_factory, импортированный в models.py
        # и затем импортированный в этот файл (app/commander.py)
        from models import async_session as default_session_factory # Можно импортировать так
        await handle_dispatch_approval(callback, bot, default_session_factory)

    router.callback_query.register(
        handle_dispatch_approval_entry_point,
        F.data.startswith("dispatch_approve_") | F.data.startswith("dispatch_reject_")
    )
    
    async def show_personnel_vehicle_status_nk_entry_point(message: types.Message, state: FSMContext): # state может передаваться aiogram, но не использоваться
        # Вызываем нашу функцию и передаем ей async_session (session_factory)
        await show_personnel_vehicle_status_nk(message, async_session) 
        
    router.message.register(
        show_personnel_vehicle_status_nk_entry_point, # <--- ИСПРАВЛЕНО: вызываем обертку
        F.text == "📋 Статус техники/ЛС"
    )

    async def commander_full_dispatch_details_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await show_full_dispatch_details(callback, async_session) # async_session - ваш session_factory
    
    router.callback_query.register(
        commander_full_dispatch_details_entry_point, 
        F.data.startswith("dispatch_full_details_")
    )

    # Хэндлер для выбора действия по обслуживанию
    async def choose_maintenance_action_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await choose_maintenance_action(callback, state, async_session) # Передаем session_factory
    router.callback_query.register(
        choose_maintenance_action_entry_point,
        F.data.startswith("maint_action_"),
        EquipmentMaintenanceStates.CHOOSING_ACTION
    )

    # TODO: Если вы реализуете ENTERING_NOTES, зарегистрируйте хэндлер для него здесь
    # async def process_maintenance_notes_entry_point(message: types.Message, state: FSMContext):
    #     await process_maintenance_notes(message, state) # session_factory может не понадобиться
    # router.message.register(process_maintenance_notes_entry_point, EquipmentMaintenanceStates.ENTERING_NOTES)


    # Хэндлер для подтверждения и сохранения действия
    async def confirm_and_save_maintenance_action_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await confirm_and_save_maintenance_action(callback, state, async_session)
    router.callback_query.register(
        confirm_and_save_maintenance_action_entry_point,
        F.data.startswith("maint_confirm_") | F.data.startswith("maint_cancel_action_"), # Ловим и подтверждение, и отмену на этом шаге
        EquipmentMaintenanceStates.CONFIRMING_ACTION
    )

    # --- Обработчик inline-кнопок утверждения/отклонения ---
    router.callback_query.register(
        handle_dispatch_approval, # <--- Просто имя функции
        F.data.startswith("dispatch_approve_") | F.data.startswith("dispatch_reject_")
    )

    # Пагинация активных выездов будет обрабатываться хендлером из dispatcher.py
    # router.callback_query.register(handle_dispatch_list_pagination, F.data.startswith("dispatch_list_"))

    logging.info("Обработчики начальника караула зарегистрированы.")