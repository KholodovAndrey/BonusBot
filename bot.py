import os
import sqlite3
import qrcode
import io
import asyncio
import logging
import csv
from datetime import datetime, timedelta
from pathlib import Path
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.types import (
    ReplyKeyboardRemove,
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    FSInputFile
)
from aiogram.filters import Command
from dotenv import load_dotenv
from PIL import Image
from pyzbar.pyzbar import decode

# Настройка путей
SCRIPT_DIR = Path(__file__).parent.absolute()
os.makedirs(SCRIPT_DIR / "news_images", exist_ok=True)
os.makedirs(SCRIPT_DIR / "exports", exist_ok=True)

# Настройка логгирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(SCRIPT_DIR / "coffee_bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
OWNER_IDS = [int(id_str) for id_str in os.getenv('OWNER_IDS', '').split(',') if id_str]
ADMIN_IDS = [int(id_str) for id_str in os.getenv('ADMIN_IDS', '').split(',') if id_str]
CHANNEL_LINK = os.getenv('CHANNEL_LINK', 'https://t.me/your_channel')

# Инициализация бота
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Инициализация базы данных
DB_PATH = SCRIPT_DIR / 'coffee_bot.db'

def init_database():
    """Инициализация базы данных с нуля"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        phone TEXT,
        bonus_points INTEGER DEFAULT 0,
        registered_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Таблица транзакций
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        description TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        check_amount REAL,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')
    
    # Таблица настроек
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Вставка начальных настроек, если их нет
    default_settings = {
        'bonus_threshold': '2000',
        'bonus_percent_below': '5',
        'bonus_percent_above': '7',
        'spend_min_amount': '10',
        'spend_max_amount': '5000',
        'spend_max_percent_of_check': '50',
        'channel_link': CHANNEL_LINK
    }
    
    for key, value in default_settings.items():
        cursor.execute('''
        INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)
        ''', (key, value))
    
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

# Вызов инициализации БД
init_database()

# Состояния FSM
class RegistrationStates(StatesGroup):
    waiting_for_phone = State()

class AdminStates(StatesGroup):
    waiting_for_scan_or_id = State()
    waiting_for_points_action = State()
    waiting_for_points_amount = State()
    waiting_for_receipt_amount = State()

class OwnerStates(StatesGroup):
    waiting_for_bonus_threshold = State()
    waiting_for_bonus_percent_below = State()
    waiting_for_bonus_percent_above = State()
    waiting_for_spend_min = State()
    waiting_for_spend_max = State()
    waiting_for_spend_percent = State()
    waiting_for_export_period = State()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def get_setting(key: str) -> str:
    """Получить значение настройки из БД"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def update_setting(key: str, value: str):
    """Обновить настройку"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    UPDATE settings SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?
    ''', (value, key))
    conn.commit()
    conn.close()

def calculate_bonus_points(check_amount: float) -> int:
    """Расчет бонусов в зависимости от суммы чека"""
    threshold = float(get_setting('bonus_threshold'))
    percent_below = float(get_setting('bonus_percent_below'))
    percent_above = float(get_setting('bonus_percent_above'))
    
    if check_amount >= threshold:
        percent = percent_above
    else:
        percent = percent_below
    
    points = int(round(check_amount * percent / 100))
    return max(points, 1)  # Минимум 1 бонус

def check_spend_limits(points: int, check_amount: float = None) -> tuple:
    """Проверка лимитов списания. Возвращает (можно_списать, сообщение_об_ошибке)"""
    min_amount = int(get_setting('spend_min_amount'))
    max_amount = int(get_setting('spend_max_amount'))
    max_percent = int(get_setting('spend_max_percent_of_check'))
    
    if points < min_amount:
        return False, f"Минимальная сумма списания: {min_amount} бонусов"
    
    if points > max_amount:
        return False, f"Максимальная сумма списания: {max_amount} бонусов за раз"
    
    if check_amount and check_amount > 0:
        max_allowed = int(check_amount * max_percent / 100)
        if points > max_allowed:
            return False, f"Нельзя списать более {max_percent}% от суммы чека (макс. {max_allowed} бонусов)"
    
    return True, "OK"

def get_owner_keyboard():
    """Клавиатура для владельца"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="📷 Сканировать QR"), types.KeyboardButton(text="✏️ Ввести ID вручную")],
            [types.KeyboardButton(text="⚙️ Настройка бонусов"), types.KeyboardButton(text="🔒 Лимиты списания")],
            [types.KeyboardButton(text="📊 Статистика"), types.KeyboardButton(text="📤 Экспорт истории")]
        ],
        resize_keyboard=True
    )

def get_admin_keyboard():
    """Клавиатура для администратора"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="📷 Сканировать QR"), types.KeyboardButton(text="✏️ Ввести ID вручную")]
        ],
        resize_keyboard=True
    )

def get_back_keyboard():
    """Клавиатура с кнопкой Назад"""
    return ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="🔙 Назад")]],
        resize_keyboard=True
    )

def get_user_keyboard():
    """Клавиатура пользователя"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="🎫 Мой QR-код")],
            [types.KeyboardButton(text="⭐️ Мои бонусы")],
            [types.KeyboardButton(text="📜 История операций")],
            [types.KeyboardButton(text="📢 Наш канал")]
        ],
        resize_keyboard=True
    )

def get_stats_keyboard():
    """Клавиатура для статистики"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="🏪 По заведению"), types.KeyboardButton(text="👥 По пользователям")],
            [types.KeyboardButton(text="📅 За день"), types.KeyboardButton(text="📆 За неделю")],
            [types.KeyboardButton(text="📊 За месяц"), types.KeyboardButton(text="🔙 Назад")]
        ],
        resize_keyboard=True
    )

def get_export_keyboard():
    """Клавиатура для экспорта"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="📅 За день"), types.KeyboardButton(text="📆 За неделю")],
            [types.KeyboardButton(text="📊 За месяц"), types.KeyboardButton(text="📂 Всё время")],
            [types.KeyboardButton(text="🔙 Назад")]
        ],
        resize_keyboard=True
    )

def get_cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )

# ========== ОСНОВНЫЕ КОМАНДЫ ==========
@dp.message(Command('start'))
async def start_command(message: types.Message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    conn.close()
    
    if user:
        if user_id in OWNER_IDS:
            await message.answer("👑 Добро пожаловать, Владелец!", reply_markup=get_owner_keyboard())
        elif user_id in ADMIN_IDS:
            await message.answer("🛡 Добро пожаловать, Администратор!", reply_markup=get_admin_keyboard())
        else:
            await message.answer("☕️ Добро пожаловать обратно!", reply_markup=get_user_keyboard())
    else:
        await message.answer("☕️ Добро пожаловать! Для регистрации введите /register", reply_markup=ReplyKeyboardRemove())

# ========== РЕГИСТРАЦИЯ ==========
@dp.message(Command('register'))
async def register_command(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    conn.close()
    
    if user:
        keyboard = None
        if user_id in OWNER_IDS:
            keyboard = get_owner_keyboard()
        elif user_id in ADMIN_IDS:
            keyboard = get_admin_keyboard()
        else:
            keyboard = get_user_keyboard()
        await message.answer("Вы уже зарегистрированы!", reply_markup=keyboard)
        return
    
    await message.answer("📱 Введите ваш номер телефона для регистрации:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(RegistrationStates.waiting_for_phone)

@dp.message(RegistrationStates.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text
    user = message.from_user
    
    # Простая валидация телефона
    if len(phone) < 5:
        await message.answer("❌ Некорректный номер телефона. Попробуйте еще раз:")
        return
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO users (user_id, username, full_name, phone) VALUES (?, ?, ?, ?)',
            (user.id, user.username, user.full_name, phone)
        )
        conn.commit()
        conn.close()
        
        await state.clear()
        
        if user.id in OWNER_IDS:
            await message.answer("✅ Регистрация завершена!", reply_markup=get_owner_keyboard())
        elif user.id in ADMIN_IDS:
            await message.answer("✅ Регистрация завершена!", reply_markup=get_admin_keyboard())
        else:
            await message.answer("✅ Регистрация завершена!", reply_markup=get_user_keyboard())
    except Exception as e:
        logger.error(f"Ошибка регистрации: {e}")
        await message.answer("❌ Ошибка при регистрации. Попробуйте позже.", reply_markup=ReplyKeyboardRemove())

# ========== ПОЛЬЗОВАТЕЛЬСКИЕ ФУНКЦИИ ==========
@dp.message(F.text == "🎫 Мой QR-код")
async def handle_qr_request(message: types.Message):
    if message.from_user.id in OWNER_IDS or message.from_user.id in ADMIN_IDS:
        await message.answer("Пожалуйста, используйте соответствующее меню")
        return
    
    user_id = message.from_user.id
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(str(user_id))
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    
    await message.answer_photo(
        BufferedInputFile(buf.getvalue(), filename="qrcode.png"),
        caption="🎫 Ваш QR-код для бонусной программы.\nПокажите его бариста для начисления бонусов!",
        reply_markup=get_user_keyboard()
    )

@dp.message(F.text == "⭐️ Мои бонусы")
async def show_bonuses(message: types.Message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT bonus_points FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        keyboard = get_user_keyboard()
        if user_id in OWNER_IDS:
            keyboard = get_owner_keyboard()
        elif user_id in ADMIN_IDS:
            keyboard = get_admin_keyboard()
        
        await message.answer(f"⭐️ Ваш баланс: {result[0]} бонусов", reply_markup=keyboard)

@dp.message(F.text == "📜 История операций")
async def show_history(message: types.Message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    SELECT amount, description, timestamp FROM transactions 
    WHERE user_id = ? ORDER BY timestamp DESC LIMIT 10
    ''', (user_id,))
    
    transactions = cursor.fetchall()
    conn.close()
    
    if not transactions:
        keyboard = get_user_keyboard()
        if user_id in OWNER_IDS:
            keyboard = get_owner_keyboard()
        elif user_id in ADMIN_IDS:
            keyboard = get_admin_keyboard()
        await message.answer("У вас пока нет операций", reply_markup=keyboard)
        return
    
    response = "📜 Последние 10 операций:\n\n"
    for amount, description, timestamp in transactions:
        sign = "+" if amount > 0 else ""
        response += f"{timestamp}: {description} - {sign}{amount} бонусов\n"
    
    keyboard = get_user_keyboard()
    if user_id in OWNER_IDS:
        keyboard = get_owner_keyboard()
    elif user_id in ADMIN_IDS:
        keyboard = get_admin_keyboard()
    
    await message.answer(response, reply_markup=keyboard)

@dp.message(F.text == "📢 Наш канал")
async def show_channel(message: types.Message):
    channel_link = get_setting('channel_link')
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=channel_link)]
    ])
    await message.answer(
        "📢 Подпишитесь на наш Telegram-канал, чтобы быть в курсе акций и новостей!",
        reply_markup=keyboard
    )

# ========== АДМИН-ФУНКЦИИ (QR, ID, бонусы) ==========
@dp.message(F.text.in_(["📷 Сканировать QR", "✏️ Ввести ID вручную"]))
async def handle_admin_commands(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in OWNER_IDS and user_id not in ADMIN_IDS:
        return
    
    if message.text == "📷 Сканировать QR":
        await message.answer("📸 Отправьте фото QR-кода:", reply_markup=get_back_keyboard())
    else:
        await message.answer("🆔 Введите ID пользователя:", reply_markup=get_back_keyboard())
    
    await state.set_state(AdminStates.waiting_for_scan_or_id)

@dp.message(F.text == "🔙 Назад")
async def back_to_menu(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    await state.clear()
    
    if user_id in OWNER_IDS:
        await message.answer("👑 Главное меню владельца", reply_markup=get_owner_keyboard())
    elif user_id in ADMIN_IDS:
        await message.answer("🛡 Главное меню администратора", reply_markup=get_admin_keyboard())
    else:
        await message.answer("☕️ Главное меню", reply_markup=get_user_keyboard())

@dp.message(AdminStates.waiting_for_scan_or_id, F.photo)
async def process_qr_code(message: types.Message, state: FSMContext):
    try:
        file = await bot.get_file(message.photo[-1].file_id)
        img_buffer = io.BytesIO()
        await bot.download_file(file.file_path, img_buffer)
        img_buffer.seek(0)
        
        decoded = decode(Image.open(img_buffer))
        if not decoded:
            raise ValueError("QR не распознан")
            
        user_id = int(decoded[0].data.decode())
        await process_user_id(user_id, message, state)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}", reply_markup=get_back_keyboard())

@dp.message(AdminStates.waiting_for_scan_or_id, F.text.func(lambda x: x and x != "🔙 Назад"))
async def process_manual_input(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        await process_user_id(user_id, message, state)
    except ValueError:
        await message.answer("❌ Некорректный ID. Введите число:", reply_markup=get_back_keyboard())

async def process_user_id(user_id: int, message: types.Message, state: FSMContext):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    conn.close()
    
    if not user:
        await message.answer("❌ Пользователь не найден", reply_markup=get_back_keyboard())
        await state.clear()
        return
    
    await state.update_data(user_id=user_id)
    
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="➕ Начислить бонусы"))
    builder.add(types.KeyboardButton(text="➖ Списать бонусы"))
    builder.add(types.KeyboardButton(text="🔙 Назад"))
    builder.adjust(2)
    
    await message.answer(
        f"👤 Пользователь: {user[2]}\n"
        f"🆔 ID: {user[0]}\n"
        f"⭐️ Баланс: {user[4]} бонусов\n\n"
        f"Выберите действие:",
        reply_markup=builder.as_markup(resize_keyboard=True)
    )
    await state.set_state(AdminStates.waiting_for_points_action)

@dp.message(AdminStates.waiting_for_points_action, F.text == "🔙 Назад")
async def admin_back_to_menu(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()
    if user_id in OWNER_IDS:
        await message.answer("👑 Главное меню владельца", reply_markup=get_owner_keyboard())
    else:
        await message.answer("🛡 Главное меню администратора", reply_markup=get_admin_keyboard())

@dp.message(AdminStates.waiting_for_points_action, F.text.in_(["➕ Начислить бонусы", "➖ Списать бонусы"]))
async def process_points_action(message: types.Message, state: FSMContext):
    await state.update_data(action=message.text)
    
    if message.text == "➕ Начислить бонусы":
        await message.answer("💰 Введите сумму чека (в рублях):", reply_markup=get_cancel_keyboard())
        await state.set_state(AdminStates.waiting_for_receipt_amount)
    else:
        await message.answer("🔢 Введите количество бонусов для списания:", reply_markup=get_cancel_keyboard())
        await state.set_state(AdminStates.waiting_for_points_amount)

@dp.message(AdminStates.waiting_for_receipt_amount)
async def process_receipt_amount(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    try:
        receipt_amount = float(message.text.replace(',', '.'))
        if receipt_amount <= 0:
            raise ValueError("Сумма должна быть положительной")
        
        points = calculate_bonus_points(receipt_amount)
        data = await state.get_data()
        user_id = data['user_id']
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('UPDATE users SET bonus_points = bonus_points + ? WHERE user_id = ?', (points, user_id))
        cursor.execute(
            'INSERT INTO transactions (user_id, amount, description, check_amount) VALUES (?, ?, ?, ?)',
            (user_id, points, f"Начисление за чек {receipt_amount} руб.", receipt_amount)
        )
        conn.commit()
        
        cursor.execute('SELECT bonus_points FROM users WHERE user_id = ?', (user_id,))
        new_balance = cursor.fetchone()[0]
        conn.close()
        
        await message.answer(
            f"✅ Бонусы начислены!\n"
            f"💰 Сумма чека: {receipt_amount} руб.\n"
            f"⭐️ Начислено: {points} бонусов\n"
            f"📊 Новый баланс: {new_balance}",
            reply_markup=get_admin_keyboard() if message.from_user.id in ADMIN_IDS else get_owner_keyboard()
        )
        
        # Уведомление пользователю
        try:
            await bot.send_message(
                user_id,
                f"✅ Вам начислены бонусы!\n"
                f"💰 Сумма чека: {receipt_amount} руб.\n"
                f"⭐️ Начислено: {points} бонусов\n"
                f"📊 Новый баланс: {new_balance}",
            )
        except:
            pass
        
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Введите корректную сумму (число):", reply_markup=get_cancel_keyboard())

@dp.message(AdminStates.waiting_for_points_amount)
async def process_points_amount(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    try:
        points = int(message.text)
        if points <= 0:
            raise ValueError("Число должно быть положительным")
            
        data = await state.get_data()
        user_id = data['user_id']
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT bonus_points FROM users WHERE user_id = ?', (user_id,))
        current_points = cursor.fetchone()[0]
        
        # Проверка лимитов списания
        can_spend, error_msg = check_spend_limits(points)
        if not can_spend:
            await message.answer(f"❌ {error_msg}", reply_markup=get_cancel_keyboard())
            conn.close()
            return
        
        if current_points < points:
            await message.answer(f"❌ Недостаточно бонусов. Доступно: {current_points}", reply_markup=get_cancel_keyboard())
            conn.close()
            return
        
        new_points = current_points - points
        
        cursor.execute('UPDATE users SET bonus_points = ? WHERE user_id = ?', (new_points, user_id))
        cursor.execute(
            'INSERT INTO transactions (user_id, amount, description) VALUES (?, ?, ?)',
            (user_id, -points, f"Списание {points} бонусов")
        )
        conn.commit()
        conn.close()
        
        keyboard = get_admin_keyboard() if message.from_user.id in ADMIN_IDS else get_owner_keyboard()
        await message.answer(
            f"✅ Бонусы списаны!\n"
            f"➖ Списано: {points}\n"
            f"📊 Новый баланс: {new_points}",
            reply_markup=keyboard
        )
        
        # Уведомление пользователю
        try:
            await bot.send_message(
                user_id,
                f"📢 С вашего счета списаны бонусы\n"
                f"➖ Списано: {points}\n"
                f"📊 Текущий баланс: {new_points}"
            )
        except:
            pass
        
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Введите целое число:", reply_markup=get_cancel_keyboard())

@dp.message(F.text == "❌ Отмена")
async def cancel_action(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()
    
    if user_id in OWNER_IDS:
        await message.answer("Действие отменено", reply_markup=get_owner_keyboard())
    elif user_id in ADMIN_IDS:
        await message.answer("Действие отменено", reply_markup=get_admin_keyboard())
    else:
        await message.answer("Действие отменено", reply_markup=get_user_keyboard())

# ========== ФУНКЦИИ ВЛАДЕЛЬЦА ==========
@dp.message(F.text == "⚙️ Настройка бонусов")
async def owner_bonus_settings(message: types.Message, state: FSMContext):
    if message.from_user.id not in OWNER_IDS:
        return
    
    threshold = get_setting('bonus_threshold')
    percent_below = get_setting('bonus_percent_below')
    percent_above = get_setting('bonus_percent_above')
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✏️ Изменить", callback_data="edit_bonus"))
    
    await message.answer(
        f"⚙️ Текущие настройки начисления бонусов:\n\n"
        f"• До {threshold} ₽ → {percent_below}%\n"
        f"• От {threshold} ₽ → {percent_above}%\n\n"
        f"Формула: сумма чека * процент / 100",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "edit_bonus")
async def edit_bonus_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in OWNER_IDS:
        await callback.answer("Нет прав")
        return
    
    await callback.message.answer("📝 Введите новую сумму порога (в рублях):", reply_markup=get_cancel_keyboard())
    await state.set_state(OwnerStates.waiting_for_bonus_threshold)
    await callback.answer()

@dp.message(OwnerStates.waiting_for_bonus_threshold)
async def set_bonus_threshold(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    try:
        threshold = float(message.text.replace(',', '.'))
        if threshold <= 0:
            raise ValueError
        await state.update_data(threshold=threshold)
        await message.answer("📝 Введите процент для суммы ДО порога (0-100):", reply_markup=get_cancel_keyboard())
        await state.set_state(OwnerStates.waiting_for_bonus_percent_below)
    except ValueError:
        await message.answer("❌ Введите положительное число:", reply_markup=get_cancel_keyboard())

@dp.message(OwnerStates.waiting_for_bonus_percent_below)
async def set_bonus_percent_below(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    try:
        percent = float(message.text.replace(',', '.'))
        if percent < 0 or percent > 100:
            raise ValueError
        await state.update_data(percent_below=percent)
        await message.answer("📝 Введите процент для суммы ОТ порога (0-100):", reply_markup=get_cancel_keyboard())
        await state.set_state(OwnerStates.waiting_for_bonus_percent_above)
    except ValueError:
        await message.answer("❌ Введите число от 0 до 100:", reply_markup=get_cancel_keyboard())

@dp.message(OwnerStates.waiting_for_bonus_percent_above)
async def set_bonus_percent_above(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    try:
        percent = float(message.text.replace(',', '.'))
        if percent < 0 or percent > 100:
            raise ValueError
        
        data = await state.get_data()
        
        update_setting('bonus_threshold', str(data['threshold']))
        update_setting('bonus_percent_below', str(data['percent_below']))
        update_setting('bonus_percent_above', str(percent))
        
        await state.clear()
        await message.answer(
            f"✅ Настройки бонусов обновлены!\n\n"
            f"• До {data['threshold']} ₽ → {data['percent_below']}%\n"
            f"• От {data['threshold']} ₽ → {percent}%",
            reply_markup=get_owner_keyboard()
        )
    except ValueError:
        await message.answer("❌ Введите число от 0 до 100:", reply_markup=get_cancel_keyboard())

@dp.message(F.text == "🔒 Лимиты списания")
async def owner_spend_limits(message: types.Message, state: FSMContext):
    if message.from_user.id not in OWNER_IDS:
        return
    
    min_amount = get_setting('spend_min_amount')
    max_amount = get_setting('spend_max_amount')
    max_percent = get_setting('spend_max_percent_of_check')
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✏️ Изменить", callback_data="edit_limits"))
    
    await message.answer(
        f"🔒 Текущие лимиты списания:\n\n"
        f"• Минимальная сумма: {min_amount} бонусов\n"
        f"• Максимальная сумма: {max_amount} бонусов за раз\n"
        f"• Максимум от чека: {max_percent}%\n\n"
        f"При списании проверяются все три лимита.",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "edit_limits")
async def edit_limits_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in OWNER_IDS:
        await callback.answer("Нет прав")
        return
    
    await callback.message.answer("📝 Введите минимальную сумму списания (бонусов):", reply_markup=get_cancel_keyboard())
    await state.set_state(OwnerStates.waiting_for_spend_min)
    await callback.answer()

@dp.message(OwnerStates.waiting_for_spend_min)
async def set_spend_min(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    try:
        min_amount = int(message.text)
        if min_amount < 0:
            raise ValueError
        await state.update_data(spend_min=min_amount)
        await message.answer("📝 Введите максимальную сумму списания за раз (бонусов):", reply_markup=get_cancel_keyboard())
        await state.set_state(OwnerStates.waiting_for_spend_max)
    except ValueError:
        await message.answer("❌ Введите целое неотрицательное число:", reply_markup=get_cancel_keyboard())

@dp.message(OwnerStates.waiting_for_spend_max)
async def set_spend_max(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    try:
        max_amount = int(message.text)
        if max_amount < 0:
            raise ValueError
        await state.update_data(spend_max=max_amount)
        await message.answer("📝 Введите максимальный процент от чека для списания (0-100):", reply_markup=get_cancel_keyboard())
        await state.set_state(OwnerStates.waiting_for_spend_percent)
    except ValueError:
        await message.answer("❌ Введите целое неотрицательное число:", reply_markup=get_cancel_keyboard())

@dp.message(OwnerStates.waiting_for_spend_percent)
async def set_spend_percent(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    try:
        percent = int(message.text)
        if percent < 0 or percent > 100:
            raise ValueError
        
        data = await state.get_data()
        
        update_setting('spend_min_amount', str(data['spend_min']))
        update_setting('spend_max_amount', str(data['spend_max']))
        update_setting('spend_max_percent_of_check', str(percent))
        
        await state.clear()
        await message.answer(
            f"✅ Лимиты списания обновлены!\n\n"
            f"• Минимальная сумма: {data['spend_min']} бонусов\n"
            f"• Максимальная сумма: {data['spend_max']} бонусов\n"
            f"• Максимум от чека: {percent}%",
            reply_markup=get_owner_keyboard()
        )
    except ValueError:
        await message.answer("❌ Введите число от 0 до 100:", reply_markup=get_cancel_keyboard())

# ========== СТАТИСТИКА ==========
@dp.message(F.text == "📊 Статистика")
async def show_stats_menu(message: types.Message):
    if message.from_user.id not in OWNER_IDS:
        return
    
    await message.answer("📊 Выберите тип статистики:", reply_markup=get_stats_keyboard())

@dp.message(F.text == "🏪 По заведению")
async def stats_by_venue(message: types.Message):
    if message.from_user.id not in OWNER_IDS:
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Общая статистика
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]
    
    cursor.execute('SELECT SUM(bonus_points) FROM users')
    total_bonuses = cursor.fetchone()[0] or 0
    
    cursor.execute('SELECT SUM(amount) FROM transactions WHERE amount > 0')
    total_earned = cursor.fetchone()[0] or 0
    
    cursor.execute('SELECT SUM(amount) FROM transactions WHERE amount < 0')
    total_spent = abs(cursor.fetchone()[0] or 0)
    
    cursor.execute('SELECT COUNT(*) FROM transactions WHERE amount > 0')
    total_checks = cursor.fetchone()[0] or 0
    
    cursor.execute('SELECT AVG(check_amount) FROM transactions WHERE check_amount IS NOT NULL AND check_amount > 0')
    avg_check = cursor.fetchone()[0] or 0
    
    conn.close()
    
    await message.answer(
        f"🏪 <b>Статистика заведения</b>\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"⭐️ Всего бонусов на счетах: {total_bonuses}\n"
        f"📈 Всего начислено: {total_earned}\n"
        f"📉 Всего списано: {total_spent}\n"
        f"🧾 Всего чеков: {total_checks}\n"
        f"💰 Средний чек: {avg_check:.2f} ₽",
        reply_markup=get_stats_keyboard()
    )

@dp.message(F.text == "👥 По пользователям")
async def stats_by_users(message: types.Message):
    if message.from_user.id not in OWNER_IDS:
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Топ по накопленным бонусам
    cursor.execute('''
    SELECT full_name, bonus_points FROM users 
    ORDER BY bonus_points DESC LIMIT 5
    ''')
    top_by_points = cursor.fetchall()
    
    # Топ по начислениям
    cursor.execute('''
    SELECT u.full_name, SUM(t.amount) as total
    FROM users u
    JOIN transactions t ON u.user_id = t.user_id
    WHERE t.amount > 0
    GROUP BY u.user_id
    ORDER BY total DESC LIMIT 5
    ''')
    top_by_earned = cursor.fetchall()
    
    # Топ по списаниям
    cursor.execute('''
    SELECT u.full_name, ABS(SUM(t.amount)) as total
    FROM users u
    JOIN transactions t ON u.user_id = t.user_id
    WHERE t.amount < 0
    GROUP BY u.user_id
    ORDER BY total DESC LIMIT 5
    ''')
    top_by_spent = cursor.fetchall()
    
    conn.close()
    
    response = "👥 <b>Статистика по пользователям</b>\n\n"
    
    response += "🏆 <b>Топ-5 по балансу:</b>\n"
    for i, (name, points) in enumerate(top_by_points, 1):
        response += f"{i}. {name[:20]} — {points} бонусов\n"
    
    response += "\n📈 <b>Топ-5 по начислениям:</b>\n"
    for i, (name, total) in enumerate(top_by_earned, 1):
        response += f"{i}. {name[:20]} — {total} бонусов\n"
    
    response += "\n📉 <b>Топ-5 по списаниям:</b>\n"
    for i, (name, total) in enumerate(top_by_spent, 1):
        response += f"{i}. {name[:20]} — {total} бонусов\n"
    
    await message.answer(response, reply_markup=get_stats_keyboard())

@dp.message(F.text == "📅 За день")
async def stats_day(message: types.Message):
    if message.from_user.id not in OWNER_IDS:
        return
    
    await show_stats_period(message, 'day')

@dp.message(F.text == "📆 За неделю")
async def stats_week(message: types.Message):
    if message.from_user.id not in OWNER_IDS:
        return
    
    await show_stats_period(message, 'week')

@dp.message(F.text == "📊 За месяц")
async def stats_month(message: types.Message):
    if message.from_user.id not in OWNER_IDS:
        return
    
    await show_stats_period(message, 'month')

async def show_stats_period(message: types.Message, period: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    if period == 'day':
        start_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        period_name = "сегодня"
    elif period == 'week':
        start_date = datetime.now() - timedelta(days=7)
        period_name = "за неделю"
    else:  # month
        start_date = datetime.now() - timedelta(days=30)
        period_name = "за месяц"
    
    cursor.execute('''
    SELECT 
        COUNT(DISTINCT user_id) as active_users,
        SUM(CASE WHEN amount > 0 THEN 1 ELSE 0 END) as checks_count,
        SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) as total_earned,
        SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) as total_spent,
        AVG(CASE WHEN check_amount > 0 THEN check_amount END) as avg_check
    FROM transactions 
    WHERE timestamp >= ?
    ''', (start_date,))
    
    result = cursor.fetchone()
    active_users = result[0] or 0
    checks_count = result[1] or 0
    total_earned = result[2] or 0
    total_spent = abs(result[3] or 0)
    avg_check = result[4] or 0
    
    conn.close()
    
    await message.answer(
        f"📊 <b>Статистика {period_name}</b>\n\n"
        f"👥 Активных пользователей: {active_users}\n"
        f"🧾 Количество чеков: {checks_count}\n"
        f"📈 Начислено бонусов: {total_earned}\n"
        f"📉 Списано бонусов: {total_spent}\n"
        f"💰 Средний чек: {avg_check:.2f} ₽",
        reply_markup=get_stats_keyboard()
    )

# ========== ЭКСПОРТ ИСТОРИИ ==========
@dp.message(F.text == "📤 Экспорт истории")
async def export_history_menu(message: types.Message, state: FSMContext):
    if message.from_user.id not in OWNER_IDS:
        return
    
    await message.answer("📤 Выберите период для экспорта:", reply_markup=get_export_keyboard())
    await state.set_state(OwnerStates.waiting_for_export_period)

@dp.message(OwnerStates.waiting_for_export_period, F.text.in_(["📅 За день", "📆 За неделю", "📊 За месяц", "📂 Всё время"]))
async def process_export_period(message: types.Message, state: FSMContext):
    period_map = {
        "📅 За день": 1,
        "📆 За неделю": 7,
        "📊 За месяц": 30,
        "📂 Всё время": None
    }
    
    days = period_map[message.text]
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    if days:
        start_date = datetime.now() - timedelta(days=days)
        cursor.execute('''
        SELECT t.id, t.user_id, u.full_name, t.amount, t.description, t.timestamp, t.check_amount
        FROM transactions t
        JOIN users u ON t.user_id = u.user_id
        WHERE t.timestamp >= ?
        ORDER BY t.timestamp DESC
        ''', (start_date,))
    else:
        cursor.execute('''
        SELECT t.id, t.user_id, u.full_name, t.amount, t.description, t.timestamp, t.check_amount
        FROM transactions t
        JOIN users u ON t.user_id = u.user_id
        ORDER BY t.timestamp DESC
        ''')
    
    transactions = cursor.fetchall()
    conn.close()
    
    if not transactions:
        await message.answer("❌ Нет данных за выбранный период", reply_markup=get_owner_keyboard())
        await state.clear()
        return
    
    # Создание CSV файла
    filename = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    filepath = SCRIPT_DIR / "exports" / filename
    
    with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['ID', 'ID пользователя', 'Пользователь', 'Сумма бонусов', 'Описание', 'Дата', 'Сумма чека'])
        
        for trans in transactions:
            writer.writerow([
                trans[0], trans[1], trans[2], trans[3], trans[4], trans[5], trans[6] if trans[6] else ''
            ])
    
    # Отправка файла
    document = FSInputFile(filepath, filename=filename)
    await message.answer_document(
        document,
        caption=f"📊 Экспорт транзакций ({len(transactions)} записей)",
        reply_markup=get_owner_keyboard()
    )
    
    # Удаление файла после отправки
    os.remove(filepath)
    await state.clear()

# ========== ЗАПУСК БОТА ==========
async def main():
    logger.info("Starting coffee bonus bot...")
    logger.info(f"Owners: {OWNER_IDS}")
    logger.info(f"Admins: {ADMIN_IDS}")
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
    finally:
        logger.info("Shutdown complete")