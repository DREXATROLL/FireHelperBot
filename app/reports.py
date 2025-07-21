import io
from datetime import datetime, timedelta
from aiogram import types, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment

from models import DispatchOrder, Employee, Vehicle # и другие нужные модели
from app.keyboards import get_cancel_keyboard # или своя клавиатура отмены
from app.dispatcher import STATUS_TRANSLATIONS
import logging
# Состояния FSM для генерации отчета по выездам
class DispatchReportStates(StatesGroup):
    CHOOSING_PERIOD = State()
    # Можно добавить выбор детализации или фильтров позже

async def start_dispatch_report(message: types.Message, state: FSMContext):
    await state.clear()
    # TODO: Добавить клавиатуру с выбором периода (сегодня, неделя, месяц, свой период)
    # Пока запросим ввод вручную
    await message.answer(
        "🗓 <b>Отчет по выездам</b> 🗓\n"
        "Введите период для отчета в формате <b>ДД.ММ.ГГГГ-ДД.ММ.ГГГГ</b>\n"
        "(например, 01.01.2024-31.01.2024).\n\n"
        "Или введите 'сегодня', 'вчера', 'неделя', 'месяц'.",
        reply_markup=get_cancel_keyboard("cancel_report_generation"), # Своя отмена
        parse_mode="HTML"
    )
    await state.set_state(DispatchReportStates.CHOOSING_PERIOD)

async def process_dispatch_report_period(message: types.Message, state: FSMContext, session_factory: async_sessionmaker):
    period_str = message.text.strip().lower()
    date_from = None
    date_to = None
    today = datetime.now()

    try:
        if period_str == "сегодня":
            date_from = today.replace(hour=0, minute=0, second=0, microsecond=0)
            date_to = today.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_str == "вчера":
            yesterday = today - timedelta(days=1)
            date_from = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
            date_to = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_str == "неделя": # Текущая неделя (с понедельника по сегодня)
            date_from = (today - timedelta(days=today.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            date_to = today.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_str == "месяц": # Текущий месяц
            date_from = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            date_to = today.replace(hour=23, minute=59, second=59, microsecond=999999) # Конец текущего дня этого месяца, для запроса будет <= date_to
            # Для корректного конца месяца, если нужен полный месяц:
            # next_month = today.replace(day=28) + timedelta(days=4)  # это гарантированно следующий месяц
            # date_to = (next_month - timedelta(days=next_month.day)).replace(hour=23, minute=59, second=59, microsecond=999999)

        else:
            parts = period_str.split('-')
            if len(parts) != 2:
                raise ValueError("Неверный формат периода. Используйте ДД.ММ.ГГГГ-ДД.ММ.ГГГГ")
            date_from_str, date_to_str = parts[0].strip(), parts[1].strip()
            date_from = datetime.strptime(date_from_str, "%d.%m.%Y").replace(hour=0, minute=0, second=0, microsecond=0)
            date_to = datetime.strptime(date_to_str, "%d.%m.%Y").replace(hour=23, minute=59, second=59, microsecond=999999)
        
        if date_from > date_to:
            raise ValueError("Дата начала периода не может быть позже даты окончания.")

    except ValueError as e:
        await message.answer(f"Ошибка ввода периода: {e}\nПожалуйста, попробуйте снова.",
                             reply_markup=get_cancel_keyboard("cancel_report_generation"))
        return

    await message.answer("⏳ Генерирую отчет по выездам, пожалуйста, подождите...")
    
    excel_file_bytes = await generate_dispatches_excel_report(session_factory, date_from, date_to)

    if excel_file_bytes:
        report_filename = f"Отчет_по_выездам_{date_from.strftime('%Y%m%d')}-{date_to.strftime('%Y%m%d')}.xlsx"
        input_file = types.BufferedInputFile(excel_file_bytes, filename=report_filename)
        await message.answer_document(input_file, caption=f"✅ Ваш отчет по выездам за период готов.")
    else:
        await message.answer("Не удалось сформировать отчет или за указанный период нет данных.")
    
    await state.clear()
    
async def generate_dispatches_excel_report(session_factory: async_sessionmaker, date_from: datetime, date_to: datetime) -> bytes | None:
    async with session_factory() as session:
        dispatches = await session.scalars(
            select(DispatchOrder)
            .options( # Жадная загрузка для уменьшения кол-ва запросов
                selectinload(DispatchOrder.creator),
                selectinload(DispatchOrder.approver)
            )
            .where(
                and_( # type: ignore
                    DispatchOrder.creation_time >= date_from,
                    DispatchOrder.creation_time <= date_to
                )
            )
            .order_by(DispatchOrder.creation_time.asc())
        )
        dispatches_list = dispatches.all()

        if not dispatches_list:
            return None

        wb = Workbook()
        ws = wb.active
        ws.title = "Список выездов"

        # Заголовки
        headers = [
            "ID выезда", "Дата создания", "Время создания", "Адрес", "Причина", 
            "Статус", "Дата утверждения НК", "ФИО НК", "Дата завершения", 
            "Кол-во пострадавших", "Кол-во погибших", 
            "Детали по пострадавшим/погибшим", "Общие примечания", "Диспетчер (создал)"
        ]
        ws.append(headers)
        for col_num, header_title in enumerate(headers, 1): # Стилизация заголовков
            cell = ws.cell(row=1, column=col_num)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center")

        # Данные
        for order in dispatches_list:
            row_data = [
                order.id,
                order.creation_time.strftime("%d.%m.%Y") if order.creation_time else "",
                order.creation_time.strftime("%H:%M:%S") if order.creation_time else "",
                order.address,
                order.reason,
                STATUS_TRANSLATIONS.get(order.status, order.status),
                order.approval_time.strftime("%d.%m.%Y %H:%M") if order.approval_time else "",
                order.approver.full_name if order.approver else "",
                order.completion_time.strftime("%d.%m.%Y %H:%M") if order.completion_time else "",
                order.victims_count if order.victims_count is not None else 0,
                order.fatalities_count if order.fatalities_count is not None else 0,
                order.details_on_casualties,
                order.notes,
                order.creator.full_name if order.creator else ""
            ]
            ws.append(row_data)
        
        # Автоподбор ширины колонок (примерный)
        for col in ws.columns:
            max_length = 0
            column = col[0].column_letter # Получаем букву колонки
            for cell in col:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = (max_length + 2)
            ws.column_dimensions[column].width = adjusted_width

        # Сохраняем в байтовый поток
        file_stream = io.BytesIO()
        wb.save(file_stream)
        file_stream.seek(0) # Перемещаем указатель в начало потока
        return file_stream.getvalue()

# Функция для регистрации хэндлеров этого модуля
def register_reports_handlers(router: Router):
    logging.info("Регистрируем обработчики отчетов...")
    router.message.register(start_dispatch_report, F.text == "📊 Отчет по выездам") # Пример текста кнопки
    
    async def process_dispatch_report_period_entry_point(message: types.Message, state: FSMContext):
        from models import async_session as default_session_factory # Локальный импорт
        await process_dispatch_report_period(message, state, default_session_factory)
    router.message.register(process_dispatch_report_period_entry_point, DispatchReportStates.CHOOSING_PERIOD)

    # Отмена генерации отчета
    async def cancel_report_generation_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Отменено")
        await callback.message.edit_text("Генерация отчета отменена.", reply_markup=None)
        await state.clear()
    router.callback_query.register(cancel_report_generation_handler, F.data == "cancel_report_generation", StateFilter(DispatchReportStates))

    logging.info("Обработчики отчетов зарегистрированы.")