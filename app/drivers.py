from aiogram import F, types, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from models import async_session, Vehicle, TripSheet, Employee
# Убираем get_vehicles_keyboard из импорта:
from app.keyboards import confirm_cancel_keyboard
from aiogram.utils.keyboard import InlineKeyboardBuilder
import logging
import math # Оставляем, если пагинация используется

TRIPS_PER_PAGE = 5 # Оставляем, если пагинация используется

class CheckStatusStates(StatesGroup):
    CHOOSING_VEHICLE = State()

class TripSheetStates(StatesGroup):
    CHOOSING_VEHICLE = State()
    ENTERING_DESTINATION = State()
    ENTERING_MILEAGE = State()
    ENTERING_FUEL = State()
    CONFIRMATION = State()
    FINISHING_TRIP = State()
# Убрали ShiftStates/ShiftManagementStates

# --- Пагинация истории поездок (если она была) ---
async def _generate_trip_history_page(session: AsyncSession, user_id: int, page: int = 1):
    offset = (page - 1) * TRIPS_PER_PAGE
    # Считаем общее количество поездок
    total_trips_result = await session.execute(
        select(func.count(TripSheet.id))
        # Используем driver_id
        .where(TripSheet.driver_id == user_id)
    )
    total_trips = total_trips_result.scalar_one_or_none() or 0

    if total_trips == 0:
        return "🚗 У вас еще нет совершенных поездок", None

    total_pages = math.ceil(total_trips / TRIPS_PER_PAGE)
    page = max(1, min(page, total_pages))

    # Получаем поездки для страницы
    trips_result = await session.execute(
        select(TripSheet)
        # Используем driver_id
        .where(TripSheet.driver_id == user_id)
        .order_by(TripSheet.date.desc())
        .limit(TRIPS_PER_PAGE)
        .offset(offset)
    )
    trips_on_page = trips_result.scalars().all()

    response_text = [f"📅 Ваша история поездок (Страница {page}/{total_pages}):"]
    for trip in trips_on_page:
        # Связь vehicle осталась в TripSheet
        vehicle = await session.get(Vehicle, trip.vehicle_id)
        vehicle_info = f"{vehicle.number_plate} ({vehicle.model})" if vehicle else f"Автомобиль не найден (ID: {trip.vehicle_id})"
        response_text.append(
            f"\n🗓 {trip.date.strftime('%d.%m.%Y %H:%M')} | 🚗 {vehicle_info}\n"
            f"📍 Куда: {trip.destination}\n"
            f"🛣 Пробег: {trip.mileage} км | ⛽ Расход: {trip.fuel_consumption} л"
        )

    builder = InlineKeyboardBuilder()
    if page > 1:
        builder.button(text="⬅️ Назад", callback_data=f"trip_page_{page-1}")
    if page < total_pages:
        builder.button(text="➡️ Вперед", callback_data=f"trip_page_{page+1}")
    builder.adjust(2)

    return "\n".join(response_text), builder.as_markup() if total_pages > 1 else None

async def show_trip_history(message: types.Message):
    """Показ ПЕРВОЙ страницы истории поездок"""
    async with async_session() as session:
        # Используем message.from_user.id напрямую, если driver_id это telegram_id
        text, reply_markup = await _generate_trip_history_page(session, message.from_user.id, page=1)
        # ИЛИ если driver_id это Employee.id, нужно сначала получить Employee
        # employee = await session.execute(select(Employee).where(Employee.telegram_id == message.from_user.id))
        # employee = employee.scalar_one_or_none()
        # if employee:
        #    text, reply_markup = await _generate_trip_history_page(session, employee.id, page=1)
        # else: text, reply_markup = "Ошибка: не найден сотрудник.", None
        await message.answer(text, reply_markup=reply_markup)

async def handle_trip_pagination(callback: types.CallbackQuery):
    """Обрабатывает нажатия кнопок пагинации истории поездок."""
    try:
        page = int(callback.data.split("_")[-1])
        async with async_session() as session:
            # Используем callback.from_user.id напрямую или получаем Employee.id
            text, reply_markup = await _generate_trip_history_page(session, callback.from_user.id, page=page)
            # if employee: text, reply_markup = await _generate_trip_history_page(session, employee.id, page=page) ...
            await callback.message.edit_text(text, reply_markup=reply_markup)
        await callback.answer()
    except (ValueError, IndexError, Exception) as e:
        logging.error(f"Ошибка обработки пагинации: {e}, data: {callback.data}")
        await callback.answer("Ошибка при переключении страницы.", show_alert=True)
# --- Конец пагинации ---

async def handle_new_trip_sheet(message: types.Message, state: FSMContext):
    logging.info(f"handle_new_trip_sheet вызвана пользователем {message.from_user.id}")
    await state.clear() # Очищаем состояние

    try:
        async with async_session() as session:
            # Запрос не изменился
            result = await session.execute(
                select(Vehicle).where(Vehicle.status == "available")
            )
            vehicles = result.scalars().all()

            if not vehicles:
                await message.answer("🚫 Нет доступных автомобилей в данный момент.")
                # Логирование статусов всех машин для отладки (можно оставить)
                all_vehicles_result = await session.execute(select(Vehicle))
                all_vehicles = all_vehicles_result.scalars().all()
                logging.warning(f"Статусы ВСЕХ автомобилей в БД: {[(v.number_plate, v.status) for v in all_vehicles]}")
                return

            builder = InlineKeyboardBuilder()
            for vehicle in vehicles:
                builder.button(
                    text=f"{vehicle.model} ({vehicle.number_plate})",
                    callback_data=f"vehicle_{vehicle.id}"
                )
            builder.adjust(1)

            await message.answer("Выберите автомобиль:", reply_markup=builder.as_markup())
            await state.set_state(TripSheetStates.CHOOSING_VEHICLE)

    except Exception as e:
        logging.exception(f"Ошибка в handle_new_trip_sheet: {e}")
        await message.answer(f"⚠️ Произошла ошибка при поиске автомобилей: {str(e)}")
        await state.clear()

async def process_vehicle_selection(callback: types.CallbackQuery, state: FSMContext):
    try:
        vehicle_id = int(callback.data.split('_')[1])
        await state.update_data(vehicle_id=vehicle_id)
        # Проверим выбранный авто для лога
        async with async_session() as session:
            vehicle = await session.get(Vehicle, vehicle_id)
            logging.info(f"Пользователь {callback.from_user.id} выбрал авто {vehicle.number_plate if vehicle else 'НЕ НАЙДЕНО'}")
        await callback.message.edit_text("Введите пункт назначения:") # Используем edit_text для inline-кнопки
        await state.set_state(TripSheetStates.ENTERING_DESTINATION)
    except Exception as e:
        logging.exception(f"Ошибка process_vehicle_selection: {e}")
        await callback.message.answer(f"⚠️ Ошибка: {str(e)}") # Отвечаем в чат, т.к. edit_text может не сработать
        await state.clear()
    await callback.answer()

async def process_destination(message: types.Message, state: FSMContext):
    destination = message.text.strip()
    if not destination:
        await message.answer("Пункт назначения не может быть пустым. Введите еще раз:")
        return
    await state.update_data(destination=destination)
    await message.answer("Введите пробег (км):")
    await state.set_state(TripSheetStates.ENTERING_MILEAGE)

async def process_mileage(message: types.Message, state: FSMContext):
    try:
        mileage = float(message.text.replace(',', '.')) # Заменяем запятую на точку
        if mileage <= 0:
            await message.answer("Пробег должен быть положительным числом. Введите еще раз:")
            return

        await state.update_data(mileage=mileage)
        data = await state.get_data()
        async with async_session() as session:
            vehicle = await session.get(Vehicle, data['vehicle_id'])
            if not vehicle:
                raise ValueError("Не найден автомобиль для расчета расхода.")
            recommended_fuel = round(mileage * vehicle.fuel_rate / 100, 1) if vehicle.fuel_rate else 0

        await message.answer(
            f"Рекомендуемый расход: {recommended_fuel} л\n"
            "Введите фактический расход топлива (л):"
        )
        await state.set_state(TripSheetStates.ENTERING_FUEL)
    except ValueError as ve:
        logging.warning(f"Ошибка ввода пробега user {message.from_user.id}: {message.text} ({ve})")
        await message.answer("Введите пробег числом (например, 120.5):")
    except Exception as e:
        logging.exception(f"Ошибка process_mileage: {e}")
        await message.answer("Произошла ошибка при обработке пробега.")
        await state.clear()


async def process_fuel(message: types.Message, state: FSMContext):
    try:
        fuel = float(message.text.replace(',', '.'))
        if fuel < 0:
            await message.answer("Расход не может быть отрицательным. Введите еще раз:")
            return

        await state.update_data(fuel_consumption=fuel)
        data = await state.get_data()
        async with async_session() as session:
            vehicle = await session.get(Vehicle, data['vehicle_id'])
            if not vehicle:
                raise ValueError("Не найден автомобиль для подтверждения.")

        text = (
            "Проверьте данные:\n"
            f"Автомобиль: {vehicle.number_plate} ({vehicle.model})\n"
            f"Пункт назначения: {data['destination']}\n"
            f"Пробег: {data['mileage']} км\n"
            f"Расход топлива: {data['fuel_consumption']} л"
        )

        await message.answer(
            text,
            # Клавиатура без кнопки завершения поездки
            reply_markup=confirm_cancel_keyboard(show_finish_button=False)
        )
        await state.set_state(TripSheetStates.CONFIRMATION)
    except ValueError as ve:
        logging.warning(f"Ошибка ввода топлива user {message.from_user.id}: {message.text} ({ve})")
        await message.answer("Введите расход числом (например, 15.3):")
    except Exception as e:
        logging.exception(f"Ошибка process_fuel: {e}")
        await message.answer("Произошла ошибка при обработке расхода.")
        await state.clear()


async def save_trip_sheet(callback: types.CallbackQuery, state: FSMContext):
    """Сохранение путевого листа"""
    user_id = callback.from_user.id # telegram_id
    if callback.data == "confirm":
        data = await state.get_data()

        try:
            async with async_session() as session:
                trip = TripSheet(
                    driver_id=user_id, # Предполагаем, что это telegram_id
                    vehicle_id=data['vehicle_id'],
                    destination=data['destination'],
                    mileage=data['mileage'],
                    fuel_consumption=data['fuel_consumption'],
                    status="completed" 
                )
                session.add(trip)

                vehicle = await session.get(Vehicle, data['vehicle_id'])
                if vehicle:
                    vehicle.status = "in_use"
                    session.add(vehicle)
                else:
                    logging.error(f"Не найден автомобиль {data['vehicle_id']} при сохранении путевого листа!")

                await session.commit()
                logging.info(f"Путевой лист сохранен для user {user_id}, авто {data['vehicle_id']}")

            await callback.message.edit_text(
                "✅ Путевой лист сохранен.",
                # Предлагаем завершить ПОЕЗДКУ
                reply_markup=confirm_cancel_keyboard(show_finish_button=True)
            )
            await state.set_state(TripSheetStates.FINISHING_TRIP)

        except Exception as e:
            logging.exception(f"Ошибка сохранения путевого листа: {e}")
            await callback.message.edit_text("🚫 Произошла ошибка при сохранении.")
            await state.clear()


    elif callback.data == "cancel":
        await callback.message.edit_text("❌ Создание путевого листа отменено", reply_markup=None)
        await state.clear()

    await callback.answer()


async def finish_trip(callback: types.CallbackQuery, state: FSMContext):
    """Завершение поездки (не смены)"""
    # Код этой функции остается без изменений по сравнению с предыдущей версией
    data = await state.get_data()
    vehicle_id = data.get('vehicle_id')

    if not vehicle_id:
        logging.warning(f"Не найден vehicle_id в state при завершении поездки для user {callback.from_user.id}")
        await callback.message.edit_text("🚫 Произошла ошибка (нет ID авто).")
        await state.clear()
        return

    logging.info(f"Пользователь {callback.from_user.id} завершает поездку для авто ID: {vehicle_id}")
    try:
        async with async_session() as session:
            vehicle = await session.get(Vehicle, vehicle_id)
            if vehicle:
                vehicle.status = "available"
                session.add(vehicle)
                await session.commit()
                logging.info(f"Статус автомобиля {vehicle_id} изменен на 'available'.")
            else:
                logging.warning(f"Не найден автомобиль {vehicle_id} для завершения поездки.")

        await callback.message.edit_text("✅ Поездка завершена, автомобиль снова доступен.", reply_markup=None)

    except Exception as e:
        logging.exception(f"Ошибка finish_trip: {e}")
        await callback.message.edit_text("🚫 Произошла ошибка при завершении поездки.")

    await state.clear()
    await callback.answer()


async def show_fuel_stats(message: types.Message):
    """Статистика расхода топлива"""
    user_id = message.from_user.id # telegram_id
    # Если driver_id это Employee.id, нужно получить employee_db_id
    try:
        async with async_session() as session:
            # Используем user_id или employee_db_id
            avg_fuel_result = await session.execute(
                select(func.avg(TripSheet.fuel_consumption / TripSheet.mileage * 100)) # Расход л/100км
                .where(TripSheet.driver_id == user_id)
                .where(TripSheet.mileage > 0) # Избегаем деления на ноль
            )
            avg_fuel = round(avg_fuel_result.scalar() or 0, 1)

            total_mileage_result = await session.execute(
                select(func.sum(TripSheet.mileage))
                .where(TripSheet.driver_id == user_id)
            )
            total_mileage = round(total_mileage_result.scalar() or 0, 1)

            total_trips = await get_trip_count(user_id) # Передаем ID

            await message.answer(
                "⛽ Ваша статистика ГСМ:\n"
                f"• Средний расход: {avg_fuel} л/100 км\n"
                f"• Общий пробег: {total_mileage} км\n"
                f"• Всего поездок: {total_trips}"
            )
    except Exception as e:
        logging.exception(f"Ошибка show_fuel_stats: {e}")
        await message.answer("Не удалось получить статистику ГСМ.")


async def get_trip_count(driver_identifier: int) -> int:
    """Количество поездок водителя по его идентификатору (telegram_id или Employee.id)."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(func.count(TripSheet.id))
                .where(TripSheet.driver_id == driver_identifier)
            )
            return result.scalar_one_or_none() or 0
    except Exception as e:
        logging.exception(f"Ошибка get_trip_count для {driver_identifier}: {e}")
        return 0


async def check_vehicle_status(message: types.Message, state: FSMContext):
    """Запрашивает выбор автомобиля для проверки статуса."""
    logging.info(f"Пользователь {message.from_user.id} запросил проверку тех. состояния.")
    await state.clear() # Очищаем предыдущее состояние на всякий случай

    try:
        async with async_session() as session:
            # Получаем ВСЕ автомобили из базы данных
            result = await session.execute(select(Vehicle))
            vehicles = result.scalars().all()

            if not vehicles:
                await message.answer("🚫 В базе данных нет автомобилей.")
                return

            # Создаем клавиатуру выбора автомобиля
            builder = InlineKeyboardBuilder()
            for vehicle in vehicles:
                # Можно добавить текущий статус прямо в кнопку для информативности
                status_icon = {
                    'available': '✅', 'in_use': '🅿️',
                    'maintenance': '🛠️', 'repair': '⚠️'
                }.get(vehicle.status, '❓')
                builder.button(
                    # Текст кнопки: Иконка Модель (Номер)
                    text=f"{status_icon} {vehicle.model} ({vehicle.number_plate})",
                    # Callback data содержит префикс и ID автомобиля
                    callback_data=f"check_status_{vehicle.id}"
                )
            # Располагаем по одной кнопке в строке для наглядности
            builder.adjust(1)

            await message.answer(
                "Выберите автомобиль для просмотра состояния:",
                reply_markup=builder.as_markup()
            )
            # Устанавливаем состояние ожидания выбора автомобиля
            await state.set_state(CheckStatusStates.CHOOSING_VEHICLE)

    except Exception as e:
        logging.exception(f"Ошибка в check_vehicle_status при получении списка авто: {e}")
        await message.answer("⚠️ Произошла ошибка при получении списка автомобилей.")
        await state.clear()

async def process_vehicle_status_selection(callback: types.CallbackQuery, state: FSMContext):
    """Обрабатывает выбор автомобиля и показывает его статус."""
    try:
        # Извлекаем ID автомобиля из callback_data
        vehicle_id = int(callback.data.split('_')[-1])
        logging.info(f"Пользователь {callback.from_user.id} выбрал авто ID {vehicle_id} для проверки статуса.")

        async with async_session() as session:
            vehicle = await session.get(Vehicle, vehicle_id)

            if not vehicle:
                await callback.message.edit_text("🚫 Автомобиль не найден в базе данных.")
                await state.clear()
                await callback.answer()
                return

            # Формируем сообщение о статусе (как в старой версии check_vehicle_status)
            status_msg = {
                'available': '✅ Доступен',
                'in_use': '🅿️ В рейсе',
                'maintenance': '🛠 На обслуживании',
                'repair': '⚠️ В ремонте'
            }.get(vehicle.status, f'❓ Неизвестный статус ({vehicle.status})')

            status_text = (
                f"Техническое состояние:\n"
                f"Авто: {vehicle.model} ({vehicle.number_plate})\n"
                f"Статус: {status_msg}\n"
                f"Последний осмотр: {vehicle.last_check.strftime('%d.%m.%Y') if vehicle.last_check else 'нет данных'}"
            )

            # Редактируем исходное сообщение, показывая статус и убирая кнопки
            await callback.message.edit_text(status_text, reply_markup=None)
            await state.clear() # Очищаем состояние после показа результата

    except (ValueError, IndexError) as e:
        logging.error(f"Ошибка извлечения vehicle_id из callback_data '{callback.data}': {e}")
        await callback.message.edit_text("🚫 Произошла ошибка при обработке выбора.")
        await state.clear()
    except Exception as e:
        logging.exception(f"Ошибка в process_vehicle_status_selection: {e}")
        await callback.message.edit_text("⚠️ Произошла ошибка при получении статуса автомобиля.")
        await state.clear()

    await callback.answer() # Отвечаем на callback в любом случае

def register_driver_handlers(router: Router):
    """Регистрация всех обработчиков для водителей"""
    logging.info("Регистрируем обработчики водителя")
    router.message.register(handle_new_trip_sheet, F.text == "Новый путевой лист")
    router.message.register(show_trip_history, F.text == "📊 История поездок")
    router.message.register(show_fuel_stats, F.text == "⛽ Учет ГСМ")
    # Этот обработчик теперь инициирует выбор авто
    router.message.register(check_vehicle_status, F.text == "🛠 Тех. состояние")

    # Пагинация истории
    router.callback_query.register(handle_trip_pagination, F.data.startswith("trip_page_"))

    # FSM для создания путевого листа (без изменений)
    router.callback_query.register(process_vehicle_selection, F.data.startswith("vehicle_"), TripSheetStates.CHOOSING_VEHICLE)
    router.message.register(process_destination, TripSheetStates.ENTERING_DESTINATION)
    router.message.register(process_mileage, TripSheetStates.ENTERING_MILEAGE)
    router.message.register(process_fuel, TripSheetStates.ENTERING_FUEL)
    router.callback_query.register(save_trip_sheet, F.data.in_(['confirm', 'cancel']), TripSheetStates.CONFIRMATION)
    router.callback_query.register(finish_trip, F.data == "finish_trip", TripSheetStates.FINISHING_TRIP)

    # Новый обработчик для выбора авто при проверке статуса
    router.callback_query.register(
        process_vehicle_status_selection,
        CheckStatusStates.CHOOSING_VEHICLE, # Работает только в этом состоянии
        F.data.startswith("check_status_") # Фильтр по callback_data
    )

    # Убрали регистрацию обработчиков завершения смены
