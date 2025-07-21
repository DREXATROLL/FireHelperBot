from aiogram import Router, F, Bot, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State # Если какие-то состояния объявлены прямо здесь

from app.reports import register_reports_handlers

# Импорты ваших состояний и функций из модулей
from .registration import (
    RegistrationStates,
    start_bot, # Импортируем обновленную start_bot
    process_name,
    process_position,
    process_rank,
    process_contacts, # Импортируем обновленную process_contacts
    cancel_registration,
    back_to_position,
)
from .shift_management import (
    StartShiftStates, EndShiftStates,
    handle_start_shift_request,
    process_karakul_number,
    process_sizod_number_input, # Если не работает с БД, session_factory не нужен
    process_sizod_status_start_choice,
    process_skip_sizod_notes_start,
    process_sizod_notes_start_input,
    process_vehicle_choice_for_shift,
    process_operational_priority_input, # Если не работает с БД, session_factory не нужен
    process_start_odometer_input,       # Если не работает с БД, session_factory не нужен
    process_start_fuel_level_input,
    handle_end_shift_request,
    process_end_odometer_input,
    process_end_fuel_level_input,
    process_sizod_status_end_choice,
    process_skip_sizod_notes_end,
    process_sizod_notes_end_input,
    # finalize_... функции обычно вызываются из предыдущих шагов и не регистрируются напрямую
)
from .firefighter import EquipmentLogStates # и другие релевантные импорты из firefighter

from .dispatcher import (
    DispatchCreationStates,
    AbsenceRegistrationStates,
    handle_mark_absent_request,
    process_absent_employee_rank,
    process_absent_employee_fullname,
    process_absent_employee_position,
    process_absence_reason,
    process_dispatch_confirmation as dispatcher_process_dispatch_confirmation,
    process_absence_confirmation,
    cancel_absence_registration_handler,
    )

from .drivers import TripSheetStates, CheckStatusStates # и другие релевантные импорты из drivers

from .commander import (
    register_commander_handlers,
)

from models import async_session # Это ваш async_sessionmaker

# Импорты функций регистрации хэндлеров из модулей ролей
from app.drivers import register_driver_handlers
from app.firefighter import register_firefighter_handlers
from app.dispatcher import register_dispatcher_handlers

import logging

# --- Универсальный обработчик отмены ---
async def universal_cancel_handler(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    current_fsm_state = await state.get_state()
    logging.info(
        f"Universal cancel handler triggered for user {user_id}. "
        f"Callback data: '{callback.data}'. Current FSM state: {current_fsm_state}"
    )
    await state.clear()
    try:
        await callback.message.edit_text("Действие отменено.", reply_markup=None)
    except Exception as e:
        logging.warning(f"Universal cancel: Could not edit message for user {user_id}. Error: {e}")
        await callback.message.answer("Действие отменено.")
    await callback.answer("Отменено")


def register_handlers(router: Router, bot: Bot):
    logging.info("Регистрируем обработчики...")

    # Регистрация хэндлеров по ролям (эти функции сами регистрируют свои хэндлеры на переданный router)
    register_driver_handlers(router) # Предполагается, что эта функция корректно настроена
    register_firefighter_handlers(router)
    register_dispatcher_handlers(router)
    register_commander_handlers(router, bot) 
    register_reports_handlers(router)
    # --- Команды ---
    # start_bot теперь должен принимать session_factory, если он лезет в БД для проверки регистрации
    # Команды
    async def start_bot_entry_point(message: types.Message, state: FSMContext): # Обертка для start_bot
        await start_bot(message, state, async_session) # Передаем session_factory
    router.message.register(start_bot_entry_point, Command("start"))

    async def mark_absent_entry_point(message: types.Message, state: FSMContext):
        await handle_mark_absent_request(message, state, async_session)
    router.message.register(mark_absent_entry_point, F.text == "Отметить отсутствующих") # Убедитесь, что текст совпадает с кнопкой
    
    router.message.register(process_absent_employee_fullname, AbsenceRegistrationStates.WAITING_FOR_ABSENT_EMPLOYEE_FULLNAME)
    router.message.register(process_absent_employee_position, AbsenceRegistrationStates.WAITING_FOR_ABSENT_EMPLOYEE_POSITION)
    router.message.register(process_absent_employee_rank, AbsenceRegistrationStates.WAITING_FOR_ABSENT_EMPLOYEE_RANK)
    router.message.register(process_absence_reason, AbsenceRegistrationStates.WAITING_FOR_ABSENCE_REASON)
    
    async def absence_confirmation_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await process_absence_confirmation(callback, state, async_session) # Передаем session_factory
    router.callback_query.register(
        absence_confirmation_entry_point,
        F.data.in_(['absence_confirm', 'absence_edit', 'absence_cancel_final']),
        AbsenceRegistrationStates.CONFIRM_ABSENCE_ENTRY
        )

    router.callback_query.register(
        cancel_absence_registration_handler,
        F.data == "cancel_absence_registration", # Используем свой callback_data
        StateFilter(AbsenceRegistrationStates) # Для всех состояний этой группы
    )
    # --- Заступление и Окончание Караула (основные кнопки) ---
    async def start_shift_entry_point(message: types.Message, state: FSMContext):
        await handle_start_shift_request(message, state, async_session)
    router.message.register(start_shift_entry_point, F.text == "Заступить на караул")

    async def end_shift_entry_point(message: types.Message, state: FSMContext):
        await handle_end_shift_request(message, state, async_session)
    router.message.register(end_shift_entry_point, F.text == "Закончить караул")

    # --- FSM для ЗАСТУПЛЕНИЯ на караул ---
    async def process_karakul_number_entry_point(message: types.Message, state: FSMContext):
        await process_karakul_number(message, state, async_session)
    router.message.register(process_karakul_number_entry_point, StartShiftStates.ENTERING_KARAKUL_NUMBER)

    # Пожарный - заступление
    router.message.register(process_sizod_number_input, StartShiftStates.ENTERING_SIZOD_NUMBER) # Не требует session_factory, если только FSM
    async def firefighter_sizod_status_start_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await process_sizod_status_start_choice(callback, state, async_session)
    router.callback_query.register(firefighter_sizod_status_start_entry_point, F.data.startswith("sizod_status_start_"), StartShiftStates.CHOOSING_SIZOD_STATUS_START)
    async def firefighter_skip_notes_start_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await process_skip_sizod_notes_start(callback, state, async_session)
    router.callback_query.register(firefighter_skip_notes_start_entry_point, F.data == "skip_sizod_notes_start", StartShiftStates.ENTERING_SIZOD_NOTES_START)
    async def firefighter_sizod_notes_start_entry_point(message: types.Message, state: FSMContext):
        await process_sizod_notes_start_input(message, state, async_session)
    router.message.register(firefighter_sizod_notes_start_entry_point, StartShiftStates.ENTERING_SIZOD_NOTES_START)

    # Водитель - заступление
    async def driver_vehicle_choice_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await process_vehicle_choice_for_shift(callback, state, async_session)
    router.callback_query.register(driver_vehicle_choice_entry_point, F.data.startswith("start_shift_vehicle_") | (F.data == "no_vehicles_for_shift"), StartShiftStates.CHOOSING_VEHICLE)
    
    # Эти два не требуют session_factory, если они только обновляют FSM и не лезут в БД
    router.message.register(process_operational_priority_input, StartShiftStates.ENTERING_OPERATIONAL_PRIORITY)
    router.message.register(process_start_odometer_input, StartShiftStates.ENTERING_START_ODOMETER)
    
    async def driver_start_fuel_entry_point(message: types.Message, state: FSMContext):
        await process_start_fuel_level_input(message, state, async_session)
    router.message.register(driver_start_fuel_entry_point, StartShiftStates.ENTERING_START_FUEL_LEVEL)

    # --- FSM для ОКОНЧАНИЯ караула ---
    # Водитель - окончание
    async def driver_end_odometer_entry_point(message: types.Message, state: FSMContext):
        await process_end_odometer_input(message, state, async_session)
    router.message.register(driver_end_odometer_entry_point, EndShiftStates.ENTERING_END_ODOMETER)
    async def driver_end_fuel_entry_point(message: types.Message, state: FSMContext):
        await process_end_fuel_level_input(message, state, async_session)
    router.message.register(driver_end_fuel_entry_point, EndShiftStates.ENTERING_END_FUEL_LEVEL)

    # Пожарный - окончание
    async def firefighter_end_sizod_status_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await process_sizod_status_end_choice(callback, state, async_session)
    router.callback_query.register(firefighter_end_sizod_status_entry_point, F.data.startswith("sizod_status_end_"), EndShiftStates.CHOOSING_SIZOD_STATUS_END)
    async def firefighter_end_skip_notes_entry_point(callback: types.CallbackQuery, state: FSMContext):
        await process_skip_sizod_notes_end(callback, state, async_session)
    router.callback_query.register(firefighter_end_skip_notes_entry_point, F.data == "skip_sizod_notes_end", EndShiftStates.ENTERING_SIZOD_NOTES_END)
    async def firefighter_end_sizod_notes_entry_point(message: types.Message, state: FSMContext):
        await process_sizod_notes_end_input(message, state, async_session)
    router.message.register(firefighter_end_sizod_notes_entry_point, EndShiftStates.ENTERING_SIZOD_NOTES_END)

    # FSM-хэндлеры Регистрации
    router.message.register(process_name, RegistrationStates.WAITING_FOR_NAME) # Не требует session_factory
    router.callback_query.register(process_position, F.data.startswith("position_"), RegistrationStates.WAITING_FOR_POSITION) # Не требует session_factory
    router.callback_query.register(process_rank, F.data.startswith("rank_"), RegistrationStates.WAITING_FOR_RANK) # Не требует session_factory
    
    async def process_contacts_entry_point(message: types.Message, state: FSMContext): # Обертка для process_contacts
        await process_contacts(message, state, async_session) # Передаем session_factory
    router.message.register(process_contacts_entry_point, RegistrationStates.WAITING_FOR_SHIFT_AND_CONTACTS)

    # Отмена и Назад в регистрации (эти обычно не требуют БД)
    router.callback_query.register(
        cancel_registration, F.data == "cancel_registration",
        RegistrationStates.WAITING_FOR_NAME, RegistrationStates.WAITING_FOR_POSITION,
        RegistrationStates.WAITING_FOR_RANK, RegistrationStates.WAITING_FOR_SHIFT_AND_CONTACTS,
    )
    router.callback_query.register(back_to_position, F.data == "back_to_position", RegistrationStates.WAITING_FOR_RANK)
    
    # Подтверждение создания выезда диспетчером
    # dispatcher_process_dispatch_confirmation принимает bot, state, и должен принимать session_factory
    async def dispatcher_confirm_entry_point(callback: types.CallbackQuery, state: FSMContext):
        # Передаем bot из замыкания register_handlers
        await dispatcher_process_dispatch_confirmation(callback, state, bot, async_session)
    router.callback_query.register(
        dispatcher_confirm_entry_point,
        DispatchCreationStates.CONFIRMATION,
        F.data.in_(['dispatch_confirm', 'dispatch_cancel'])
    )
    
    # --- Универсальный обработчик отмены ---
    logging.info("Регистрируем универсальный обработчик отмены...")
    router.callback_query.register(
        universal_cancel_handler,
        F.data == "universal_cancel",
        StateFilter( # Перечисляем ВСЕ группы состояний, для которых эта отмена актуальна
            RegistrationStates,
            EquipmentLogStates,
            StartShiftStates, EndShiftStates, # Добавил EndShiftStates
            DispatchCreationStates,
            TripSheetStates,
            CheckStatusStates
            # Добавьте другие группы состояний по мере необходимости
        )
    )
    logging.info("Универсальный обработчик отмены зарегистрирован.")

    logging.info("Регистрация обработчиков завершена.")