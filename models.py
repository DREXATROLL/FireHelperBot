from sqlalchemy import Column, Integer, String, Float, ForeignKey, JSON, select, DateTime, Boolean, Text
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
import asyncio
from datetime import datetime

engine = create_async_engine(url='sqlite+aiosqlite:///database.db')
async_session = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

#таблица сотрудников
class Employee(Base):
    __tablename__ = 'employees'

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True)
    full_name = Column(String, nullable=False)
    position = Column(String, nullable=False)
    rank = Column(String, nullable=False)
    # shift = Column(String, nullable=False) # <-- УДАЛЕНО поле Основная смена
    contacts = Column(String, nullable=False)
    is_ready = Column(Boolean, default=False, nullable=False) # Готов ли к выезду? По умолчанию - нет.

    equipment_logs = relationship('EquipmentLog', back_populates='employee')
    held_equipment = relationship('Equipment', back_populates='current_holder')
    trip_sheets = relationship('TripSheet', back_populates='driver')
    # Связи для DispatchOrder
    created_dispatch_orders = relationship('DispatchOrder', foreign_keys='DispatchOrder.dispatcher_id', back_populates='creator')
    approved_dispatch_orders = relationship('DispatchOrder', foreign_keys='DispatchOrder.commander_id', back_populates='approver')
    # Добавим связь для редактора DispatchOrder
    edited_dispatch_orders = relationship('DispatchOrder', foreign_keys='DispatchOrder.last_edited_by_dispatcher_id', back_populates='editor')
    # Добавим связь для создателя записи об отсутствии
    reported_absences = relationship('AbsenceLog', back_populates='reporter')
    # Добавим связь для ShiftLog
    shift_logs = relationship('ShiftLog', back_populates='employee')

# Модель для таблицы техники и снаряжения
class Equipment(Base):
    __tablename__ = 'equipment'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True) # Добавим уникальность имени
    type = Column(String, nullable=False) # Тип (СИЗОД, каска, боевка и т.д.)
    inventory_number = Column(String, unique=True, nullable=True) # Инвентарный номер (может быть полезен)
    service_life = Column(String, nullable=True) # Срок службы (может быть дата или период)
    status = Column(String, nullable=False, default='available') # Статус самого снаряжения (available, in_use, maintenance, decommissioned)
    current_holder_id = Column(Integer, ForeignKey('employees.id'), nullable=True)
    
    # Связь с логами (один ко многим)
    logs = relationship('EquipmentLog', back_populates='equipment')
    current_holder = relationship('Employee', back_populates='held_equipment')

class EquipmentLog(Base):
    __tablename__ = 'equipment_logs'

    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey('employees.id'), nullable=False) # Кто взял/сдал
    equipment_id = Column(Integer, ForeignKey('equipment.id'), nullable=False) # Какое снаряжение
    action = Column(String, nullable=False) # Действие ('taken', 'returned', 'checked', 'reported_issue', 'maintenance_completed', 'marked_serviceable')
    timestamp = Column(DateTime, default=datetime.now, nullable=False) # Когда
    notes = Column(String, nullable=True) # Примечания
    shift_log_id = Column(Integer, ForeignKey('shift_logs.id'), nullable=True) # <-- ДОБАВЛЕНО поле

    # Связи многие к одному
    employee = relationship('Employee', back_populates='equipment_logs')
    equipment = relationship('Equipment', back_populates='logs')
    shift_log_entry = relationship('ShiftLog', back_populates='equipment_actions_in_shift') # <-- ДОБАВЛЕНА связь

# Модель для таблицы выездов
class Trip(Base):
    __tablename__ = 'trips'

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String, nullable=False)
    time = Column(String, nullable=False)
    address = Column(String, nullable=False)
    personnel = Column(JSON, nullable=False)
    result = Column(String, nullable=False)

# Модель для таблицы путевых листов
class TripSheet(Base):
    __tablename__ = "trip_sheets"

    id = Column(Integer, primary_key=True)
    # Возвращаем driver_id вместо employee_id
    # Важно: Убедитесь, что driver_id хранит то, что вы ожидаете (telegram_id или Employee.id)
    # В предыдущих версиях кода использовался callback.from_user.id (telegram_id)
    driver_id = Column(Integer, ForeignKey("employees.telegram_id")) # Связь по telegram_id, если так было задумано
    # ИЛИ если связь должна быть по employees.id:
    # driver_id = Column(Integer, ForeignKey("employees.id")) # Тогда нужно получать Employee.id при создании
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"))
    # Убрали shift_number
    date = Column(DateTime, default=datetime.now)
    destination = Column(String)
    mileage = Column(Float)
    fuel_consumption = Column(Float)
    status = Column(String) # 'completed' или другое

    # Обновляем связь - возвращаем 'driver'
    driver = relationship('Employee', back_populates='trip_sheets')
    vehicle = relationship('Vehicle') # Связь с Vehicle оставляем

# Модель для таблицы отчетов
class Report(Base):
    __tablename__ = 'reports'

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_type = Column(String, nullable=False)
    data = Column(JSON, nullable=False)
    created_at = Column(String, nullable=False)

class Vehicle(Base):
    __tablename__ = "vehicles"
    id = Column(Integer, primary_key=True)
    number_plate = Column(String, unique=True)
    model = Column(String)
    fuel_rate = Column(Float)
    status = Column(String) # "available", "in_use"
    last_check = Column(DateTime, nullable = True)

# --- Новая модель для Задания/Выезда ---
class DispatchOrder(Base):
    __tablename__ = 'dispatch_orders'

    id = Column(Integer, primary_key=True)
    dispatcher_id = Column(Integer, ForeignKey('employees.id'), nullable=False) # ID диспетчера, создавшего задание
    address = Column(String, nullable=False) # Адрес выезда
    reason = Column(String, nullable=False) # Причина вызова
    creation_time = Column(DateTime, default=datetime.now, nullable=False) # Время создания

    status = Column(String, default='pending_approval', nullable=False)

    # --- Необязательные поля ---
    commander_id = Column(Integer, ForeignKey('employees.id'), nullable=True) # ID НК, который утвердил/отклонил
    approval_time = Column(DateTime, nullable=True) # Время утверждения/отклонения
    completion_time = Column(DateTime, nullable=True) # Время завершения выезда
    notes = Column(Text, nullable=True) # Дополнительные примечания

    # --- Назначенные силы и средства (храним как JSON со списком ID) ---
    assigned_personnel_ids = Column(JSON, nullable=True) # Список [employee.id, employee.id, ...]
    assigned_vehicle_ids = Column(JSON, nullable=True)   # Список [vehicle.id, vehicle.id, ...]

    # --- Поля для пострадавших/погибших ---
    victims_count = Column(Integer, nullable=True, default=0)
    fatalities_count = Column(Integer, nullable=True, default=0)
    details_on_casualties = Column(Text, nullable=True)

    # --- Поля для аудита редактирования диспетчером ---
    last_edited_by_dispatcher_id = Column(Integer, ForeignKey('employees.id'), nullable=True)
    last_edited_at = Column(DateTime, nullable=True)

    # --- Связи ---
    creator = relationship('Employee', foreign_keys=[dispatcher_id], back_populates='created_dispatch_orders')
    approver = relationship('Employee', foreign_keys=[commander_id], back_populates='approved_dispatch_orders')
    editor = relationship('Employee', foreign_keys=[last_edited_by_dispatcher_id], back_populates='edited_dispatch_orders')

# --- Новая модель для Журнала Караулов/Смен ---
class ShiftLog(Base):
    __tablename__ = 'shift_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey('employees.id'), nullable=False)
    karakul_number = Column(String, nullable=False) # Номер караула
    start_time = Column(DateTime, default=datetime.now, nullable=False)
    end_time = Column(DateTime, nullable=True)
    status = Column(String, default='active', nullable=False) # 'active', 'completed'

    # --- Поля для Водителя ---
    vehicle_id = Column(Integer, ForeignKey('vehicles.id'), nullable=True)
    operational_priority = Column(Integer, nullable=True) # 1 - первый ход, 2 - второй и т.д.
    start_odometer = Column(Float, nullable=True)
    start_fuel_level = Column(Float, nullable=True)
    end_odometer = Column(Float, nullable=True)
    end_fuel_level = Column(Float, nullable=True)

    # --- Поля для Пожарного ---
    sizod_number = Column(String, nullable=True) # Номер СИЗОД
    sizod_status_start = Column(String, nullable=True) # "Исправен", "Неисправен"
    sizod_notes_start = Column(Text, nullable=True)    # Примечания к СИЗОД при получении
    sizod_status_end = Column(String, nullable=True)   # Статус СИЗОД при сдаче
    sizod_notes_end = Column(Text, nullable=True)      # Примечания к СИЗОД при сдаче

    # Связи
    employee = relationship('Employee', back_populates='shift_logs')
    vehicle = relationship('Vehicle') # Односторонняя связь, если Vehicle не нужно знать о ShiftLog
    equipment_actions_in_shift = relationship('EquipmentLog', back_populates='shift_log_entry') # Связь с EquipmentLog

# --- Новая модель для Журнала Отсутствующих ---
class AbsenceLog(Base):
    __tablename__ = 'absence_logs'
    id = Column(Integer, primary_key=True, autoincrement=True)
    reporter_employee_id = Column(Integer, ForeignKey('employees.id'), nullable=False) # ID диспетчера
    karakul_number_reported_for = Column(String, nullable=True) # Номер караула (может быть nullable)
    absence_date = Column(DateTime, default=datetime.now, nullable=False) # Дата отсутствия (или дата отметки)
    absent_employee_fullname = Column(String, nullable=False)
    absent_employee_position = Column(String, nullable=False)
    absent_employee_rank = Column(String, nullable=True) # Звание может быть необязательным
    reason = Column(Text, nullable=True)
    reported_at = Column(DateTime, default=datetime.now, nullable=False)
    reporter = relationship('Employee', back_populates='reported_absences', foreign_keys=[reporter_employee_id])

async def get_db():
    async with async_session() as session:
        yield session

async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

if __name__ == "__main__":
    asyncio.run(create_tables())

# Новая версия, принимающая session_factory:
async def is_user_registered_v2(user_id: int, session_factory: async_sessionmaker) -> bool:
    async with session_factory() as session:
        result = await session.execute(
            select(Employee).where(Employee.telegram_id == user_id)
        )
        return result.scalar_one_or_none() is not None # scalar_one_or_none() безопаснее