from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from aiogram.utils.keyboard import InlineKeyboardBuilder
from models import Equipment, Employee, Vehicle
# --- Inline клавиатуры ---
def confirm_cancel_keyboard(show_finish_button=False): # Функция остается
    keyboard_rows = [
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")
        ]
    ]
    if show_finish_button:
        # Добавляем кнопку завершения поездки отдельной строкой
        keyboard_rows.append([InlineKeyboardButton(text="✅ Завершить поездку", callback_data="finish_trip")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

# Клавиатура для подтверждения/отмены создания выезда
def confirm_cancel_dispatch_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить и отправить НК", callback_data="dispatch_confirm"),
                InlineKeyboardButton(text="❌ Отменить создание", callback_data="dispatch_cancel")
            ]
        ]
    )

def get_position_keyboard(): # Функция остается
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Водитель", callback_data="position_Водитель"),
                InlineKeyboardButton(text="Пожарный", callback_data="position_Пожарный")
            ],
            [
                InlineKeyboardButton(text="Диспетчер", callback_data="position_Диспетчер"),
                InlineKeyboardButton(text="Начальник караула", callback_data="position_Начальник караула")
            ],
            [
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_registration")
            ]
        ]
    )
    return keyboard

def get_rank_keyboard(): # Функция остается
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Рядовой", callback_data="rank_Рядовой"),
                InlineKeyboardButton(text="Сержант", callback_data="rank_Сержант")
            ],
            [
                InlineKeyboardButton(text="Лейтенант", callback_data="rank_Лейтенант"),
                InlineKeyboardButton(text="Капитан", callback_data="rank_Капитан")
            ],
            # Добавьте другие звания при необходимости
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_position"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_registration")
            ]
        ]
    )
    return keyboard

def get_equipment_log_main_keyboard():
    """Кнопки основного меню журнала снаряжения."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Новая запись", callback_data="log_new_entry")],
            # [InlineKeyboardButton(text="👀 Мои записи", callback_data="log_my_entries")], # Пока закомментируем
            [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="log_back_to_main")] # Кнопка выхода из раздела
        ]
    )
    return keyboard

def get_equipment_log_action_keyboard():
    """Кнопки для выбора действия в новой записи лога."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Взять", callback_data="log_action_taken"),
                InlineKeyboardButton(text="↩️ Вернуть", callback_data="log_action_returned")
            ],
            [
                InlineKeyboardButton(text="🔍 Проверить", callback_data="log_action_checked"),
                # InlineKeyboardButton(text="⚠️ Сообщить о проблеме", callback_data="log_action_issue") # Пока упростим
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="log_cancel")]
        ]
    )
    return keyboard

def get_equipment_selection_keyboard(equipment_list: list[Equipment], action: str):
    """Генерирует клавиатуру для выбора снаряжения."""
    builder = InlineKeyboardBuilder()
    if equipment_list:
        for item in equipment_list:
            # callback_data содержит префикс, action и ID снаряжения
            builder.button(
                text=f"{item.name} ({item.inventory_number or 'б/н'})",
                callback_data=f"log_select_{action}_{item.id}"
            )
        builder.adjust(1) # По одному элементу в строке
    else:
        # Если нет доступного снаряжения, показываем только отмену
        builder.button(text="Нет доступного снаряжения", callback_data="log_no_equipment") # Просто для информации

    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="log_cancel")) # Добавляем кнопку отмены всегда
    return builder.as_markup()

def get_readiness_toggle_keyboard(is_currently_ready: bool):
    """Создает клавиатуру для смены статуса готовности."""
    builder = InlineKeyboardBuilder()
    if is_currently_ready:
        # Если сейчас готов, предлагаем стать НЕ готовым
        builder.button(text="❌ Отметиться НЕ готовым", callback_data="set_ready_false")
    else:
        # Если сейчас НЕ готов, предлагаем стать готовым
        builder.button(text="✅ Отметиться ГОТОВЫМ", callback_data="set_ready_true")

    # Добавляем кнопку "Назад", чтобы закрыть без изменений
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="readiness_back"))
    return builder.as_markup()

# Новое меню для Диспетчера
def get_dispatcher_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Заступить на караул")], # <--- НОВАЯ КНОПКА
            [KeyboardButton(text="🔥 Создать новый выезд")],
            [KeyboardButton(text="📊 Активные выезды"), KeyboardButton(text="📂 Архив выездов")],
            [KeyboardButton(text="Отметить отсутствующих")], # <--- Добавим сразу кнопку для будущего функционала
        ],
        resize_keyboard=True
    )
    
def get_dispatch_approval_keyboard(dispatch_order_id: int):
    """Клавиатура для утверждения/отклонения выезда НК."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Утвердить",
        callback_data=f"dispatch_approve_{dispatch_order_id}" # callback_data содержит ID выезда
    )
    builder.button(
        text="❌ Отклонить",
        callback_data=f"dispatch_reject_{dispatch_order_id}" # callback_data содержит ID выезда
    )
    builder.adjust(2) # Две кнопки в ряд
    return builder.as_markup()

# Добавим базовое меню для НК
def get_commander_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Заступить на караул")], # <--- НОВАЯ КНОПКА
            [KeyboardButton(text="⏳ Выезды на утверждение")],
            [KeyboardButton(text="🔥 Активные выезды (все)")],
            [KeyboardButton(text="📋 Статус техники/ЛС")],
            [KeyboardButton(text="🔧 Обслуживание снаряжения")] # <--- Добавим сразу кнопку для будущего функционала (СИЗОД в строй)
        ],
        resize_keyboard=True
    )
    
def get_personnel_select_keyboard(employees: list[Employee], selected_ids: set[int]):
    """Клавиатура для множественного выбора сотрудников."""
    builder = InlineKeyboardBuilder()
    for emp in employees:
        is_selected = emp.id in selected_ids
        # Отмечаем выбранных галочкой
        text = f"{'✅' if is_selected else '⬜️'} {emp.full_name} ({emp.rank})"
        # callback_data содержит ID для добавления/удаления
        builder.button(text=text, callback_data=f"dispatch_toggle_personnel_{emp.id}")
    builder.adjust(1) # По одному сотруднику в строке
    # Добавляем кнопку "Готово" (переход к выбору техники)
    builder.row(InlineKeyboardButton(text="➡️ К выбору техники", callback_data="dispatch_personnel_done"))
    # Добавляем кнопку отмены всего процесса
    builder.row(InlineKeyboardButton(text="❌ Отменить создание выезда", callback_data="dispatch_create_cancel"))
    return builder.as_markup()

def get_vehicle_select_keyboard(vehicles: list[Vehicle], selected_ids: set[int]):
    """Клавиатура для множественного выбора техники."""
    builder = InlineKeyboardBuilder()
    for vhc in vehicles:
        is_selected = vhc.id in selected_ids
        text = f"{'✅' if is_selected else '⬜️'} {vhc.model} ({vhc.number_plate})"
        builder.button(text=text, callback_data=f"dispatch_toggle_vehicle_{vhc.id}")
    builder.adjust(1)
    # Добавляем кнопку "Готово" (переход к подтверждению)
    builder.row(InlineKeyboardButton(text="➡️ К подтверждению выезда", callback_data="dispatch_vehicles_done"))
    builder.row(InlineKeyboardButton(text="❌ Отменить создание выезда", callback_data="dispatch_create_cancel"))
    return builder.as_markup()

def get_cancel_keyboard(callback_data: str = "universal_cancel"): # <-- Стандартный callback_data
    """Универсальная клавиатура с кнопкой Отмена."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=callback_data))
    return builder.as_markup()

def get_sizod_status_keyboard(callback_prefix: str = "sizod_status_start_"):
    """Клавиатура для выбора состояния СИЗОД (Исправен/Неисправен)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Исправен", callback_data=f"{callback_prefix}исправен"),
        InlineKeyboardButton(text="⚠️ Неисправен", callback_data=f"{callback_prefix}неисправен")
    )
    # Добавляем общую кнопку отмены текущего процесса (заступления на караул)
    builder.row(InlineKeyboardButton(text="❌ Отменить заступление", callback_data="universal_cancel"))
    return builder.as_markup()

def get_vehicle_selection_for_shift_keyboard(vehicles: list[Vehicle]):
    """Генерирует клавиатуру для выбора автомобиля при заступлении на смену."""
    builder = InlineKeyboardBuilder()
    if vehicles:
        for vhc in vehicles:
            builder.button(
                text=f"{vhc.model} ({vhc.number_plate})",
                callback_data=f"start_shift_vehicle_{vhc.id}" # Префикс для этого FSM
            )
        builder.adjust(1) # По одному автомобилю в строке
    else:
        builder.button(text="Нет доступных автомобилей", callback_data="no_vehicles_for_shift")

    # Добавляем общую кнопку отмены текущего процесса (заступления на караул)
    builder.row(InlineKeyboardButton(text="❌ Отменить заступление", callback_data="universal_cancel"))
    return builder.as_markup()

def confirm_cancel_absence_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить запись", callback_data="absence_confirm"),
                InlineKeyboardButton(text="✏️ Редактировать", callback_data="absence_edit"), # Для будущего
                InlineKeyboardButton(text="❌ Отменить запись", callback_data="absence_cancel_final")
            ]
        ]
    )
    
def get_dispatch_edit_field_keyboard(dispatch_id: int):
    builder = InlineKeyboardBuilder()
    # Кнопки для каждого поля, которое можно редактировать
    builder.button(text="Кол-во пострадавших", callback_data=f"edit_dispatch_field_victims_{dispatch_id}")
    builder.button(text="Кол-во погибших", callback_data=f"edit_dispatch_field_fatalities_{dispatch_id}")
    builder.button(text="Детали по пострадавшим/погибшим", callback_data=f"edit_dispatch_field_casualties_details_{dispatch_id}")
    builder.button(text="Общие примечания к выезду", callback_data=f"edit_dispatch_field_notes_{dispatch_id}")
    builder.adjust(1) # Каждая кнопка на новой строке
    builder.row(InlineKeyboardButton(text="❌ Отменить редактирование", callback_data=f"edit_dispatch_cancel_{dispatch_id}"))
    return builder.as_markup()

def get_confirm_cancel_edit_keyboard(dispatch_id: int): # Для подтверждения конкретного изменения
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Сохранить изменение", callback_data=f"edit_dispatch_save_change_{dispatch_id}")
    builder.button(text="❌ Отменить это изменение", callback_data=f"edit_dispatch_cancel_change_{dispatch_id}") # Вернуться к выбору поля
    builder.adjust(2)
    return builder.as_markup()

def get_equipment_maintenance_action_keyboard(equipment_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Поставить в строй (исправен)", callback_data=f"maint_action_available_{equipment_id}")
    builder.button(text="🛠 Отправить на ТО/в ремонт", callback_data=f"maint_action_maintenance_{equipment_id}") # Общий статус для ТО/Ремонта
    builder.button(text="🗑 Списать снаряжение", callback_data=f"maint_action_decommission_{equipment_id}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="⬅️ Назад к выбору снаряжения", callback_data=f"maint_back_to_list")) # Вернуться, если передумал
    builder.row(InlineKeyboardButton(text="❌ Отменить всё", callback_data="maint_cancel_fsm")) # Полная отмена
    return builder.as_markup()

def get_maintenance_confirmation_keyboard(equipment_id: int, action_to_confirm: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить действие", callback_data=f"maint_confirm_{action_to_confirm}_{equipment_id}")
    builder.button(text="❌ Отмена", callback_data=f"maint_cancel_action_{equipment_id}") # Вернуться к выбору действия для этого снаряжения
    builder.adjust(1)
    return builder.as_markup()