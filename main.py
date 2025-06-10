import os
import sqlite3
import qrcode
import io
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import ReplyKeyboardBuilder, ReplyKeyboardMarkup
from aiogram.types import ReplyKeyboardRemove, BufferedInputFile
from aiogram.filters import Command
from dotenv import load_dotenv
from PIL import Image
from pyzbar.pyzbar import decode

load_dotenv()

# Конфигурация
BOT_TOKEN = os.getenv('TELEGRAM_TOKEN')
ADMIN_IDS = [336076029]  # Замените на ID администраторов

# Инициализация бота
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Инициализация базы данных
conn = sqlite3.connect('coffee_bot.db')
cursor = conn.cursor()

# Создание таблиц
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    phone TEXT,
    bonus_points INTEGER DEFAULT 0
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount INTEGER,
    description TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
)
''')
conn.commit()

# Состояния для FSM
class RegistrationStates(StatesGroup):
    waiting_for_phone = State()

class AdminStates(StatesGroup):
    waiting_for_scan_or_id = State()
    waiting_for_points_action = State()
    waiting_for_points_amount = State()

# ========== КЛАВИАТУРЫ ==========

def get_user_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Мой QR-код")],
            [types.KeyboardButton(text="Мои бонусы")],
            [types.KeyboardButton(text="История операций")]
        ],
        resize_keyboard=True
    )

def get_admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Сканировать QR")],
            [types.KeyboardButton(text="Ввести ID вручную")]
        ],
        resize_keyboard=True
    )

# ========== ОСНОВНЫЕ КОМАНДЫ ==========

@dp.message(Command('start'))
async def start_command(message: types.Message):
    user_id = message.from_user.id
    
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    
    if user:
        if user_id in ADMIN_IDS:
            await message.answer(
                "Добро пожаловать, администратор!",
                reply_markup=get_admin_keyboard()
            )
        else:
            await message.answer(
                "Добро пожаловать обратно!",
                reply_markup=get_user_keyboard()
            )
    else:
        await message.answer(
            "Добро пожаловать в нашу кофейню! Для регистрации введите /register",
            reply_markup=ReplyKeyboardRemove()
        )

# ========== КЛИЕНТСКИЕ ФУНКЦИИ ==========

@dp.message(Command('register'))
async def register_command(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    if cursor.fetchone():
        keyboard = get_admin_keyboard() if user_id in ADMIN_IDS else get_user_keyboard()
        await message.answer("Вы уже зарегистрированы!", reply_markup=keyboard)
        return
    
    await message.answer("Пожалуйста, введите ваш номер телефона для регистрации:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(RegistrationStates.waiting_for_phone)

@dp.message(RegistrationStates.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text
    user = message.from_user
    
    cursor.execute(
        'INSERT INTO users (user_id, username, full_name, phone, bonus_points) VALUES (?, ?, ?, ?, 0)',
        (user.id, user.username, user.full_name, phone)
    )
    conn.commit()
    
    await state.clear()
    keyboard = get_admin_keyboard() if user.id in ADMIN_IDS else get_user_keyboard()
    await message.answer(
        "Регистрация завершена!",
        reply_markup=keyboard
    )

@dp.message(F.text == "Мой QR-код")
async def handle_qr_request(message: types.Message):
    if message.from_user.id in ADMIN_IDS:
        await message.answer("Пожалуйста, используйте админское меню", reply_markup=get_admin_keyboard())
        return
    await show_qr_code(message)

async def show_qr_code(message: types.Message):
    user_id = message.from_user.id
    
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(str(user_id))
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    
    photo = BufferedInputFile(buf.getvalue(), filename="qrcode.png")
    await message.answer_photo(photo, caption="Ваш QR-код для бонусной программы", reply_markup=get_user_keyboard())

@dp.message(F.text == "Мои бонусы")
async def show_bonuses(message: types.Message):
    user_id = message.from_user.id
    
    cursor.execute('SELECT bonus_points FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    
    if result:
        reply_markup = get_admin_keyboard() if user_id in ADMIN_IDS else get_user_keyboard()
        await message.answer(
            f"Ваш текущий баланс бонусов: {result[0]}",
            reply_markup=reply_markup
        )
    else:
        await message.answer(
            "Вы не зарегистрированы в системе.",
            reply_markup=ReplyKeyboardRemove()
        )

@dp.message(F.text == "История операций")
async def show_history(message: types.Message):
    if message.from_user.id in ADMIN_IDS:
        await message.answer("Пожалуйста, используйте админское меню", reply_markup=get_admin_keyboard())
        return
    
    user_id = message.from_user.id
    
    cursor.execute('''
    SELECT amount, description, timestamp 
    FROM transactions 
    WHERE user_id = ? 
    ORDER BY timestamp DESC 
    LIMIT 10
    ''', (user_id,))
    
    transactions = cursor.fetchall()
    
    if not transactions:
        await message.answer(
            "У вас пока нет операций.",
            reply_markup=get_user_keyboard()
        )
        return
    
    response = "Последние 10 операций:\n\n"
    for amount, description, timestamp in transactions:
        response += f"{timestamp}: {description} - {amount} бонусов\n"
    
    await message.answer(response, reply_markup=get_user_keyboard())

# ========== АДМИН-ФУНКЦИИ ==========

@dp.message(F.text == "Сканировать QR")
async def request_qr_scan(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещен", reply_markup=get_user_keyboard())
        return
    
    await message.answer(
        "Пожалуйста, отправьте фото QR-кода:",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(AdminStates.waiting_for_scan_or_id)

@dp.message(F.text == "Ввести ID вручную")
async def request_manual_id(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещен", reply_markup=get_user_keyboard())
        return
    
    await message.answer(
        "Введите ID пользователя:",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(AdminStates.waiting_for_scan_or_id)

@dp.message(AdminStates.waiting_for_scan_or_id, F.content_type == 'photo')
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
        await message.answer(
            f"Ошибка: {str(e)}. Попробуйте еще раз.",
            reply_markup=get_admin_keyboard()
        )
    finally:
        if 'img_buffer' in locals():
            img_buffer.close()

@dp.message(AdminStates.waiting_for_scan_or_id)
async def process_manual_input(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await process_user_id(user_id, message, state)
    except ValueError:
        await message.answer(
            "Некорректный ID. Введите число или отправьте QR-код.",
            reply_markup=get_admin_keyboard()
        )

async def process_user_id(user_id: int, message: types.Message, state: FSMContext):
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    if not user:
        await message.answer(
            f"Пользователь с ID {user_id} не найден.",
            reply_markup=get_admin_keyboard()
        )
        await state.clear()
        return
    
    await state.update_data(user_id=user_id)
    
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="Начислить бонусы"))
    builder.add(types.KeyboardButton(text="Списать бонусы"))
    builder.add(types.KeyboardButton(text="Отмена"))
    builder.adjust(2)
    
    await message.answer(
        f"Пользователь: {user[2]} (ID: {user_id})\n"
        f"Текущий баланс: {user[4]} бонусов\n"
        "Выберите действие:",
        reply_markup=builder.as_markup(resize_keyboard=True)
    )
    await state.set_state(AdminStates.waiting_for_points_action)

@dp.message(AdminStates.waiting_for_points_action, F.text.in_(["Начислить бонусы", "Списать бонусы"]))
async def process_points_action(message: types.Message, state: FSMContext):
    await state.update_data(action=message.text)
    await message.answer(
        "Введите количество бонусов:",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(AdminStates.waiting_for_points_amount)

@dp.message(AdminStates.waiting_for_points_amount)
async def process_points_amount(message: types.Message, state: FSMContext):
    try:
        points = int(message.text)
        if points <= 0:
            raise ValueError("Число должно быть положительным")
            
        data = await state.get_data()
        user_id = data['user_id']
        action = data['action']
        
        cursor.execute('SELECT bonus_points FROM users WHERE user_id = ?', (user_id,))
        current_points = cursor.fetchone()[0]
        
        if action == "Начислить бонусы":
            new_points = current_points + points
            description = "Начисление администратором"
            operation_type = "начислено"
        else:
            if current_points < points:
                await message.answer(
                    "Недостаточно бонусов для списания.",
                    reply_markup=get_admin_keyboard()
                )
                return
            new_points = current_points - points
            description = "Списание администратором"
            operation_type = "списано"
        
        # Обновляем баланс и записываем транзакцию
        cursor.execute('UPDATE users SET bonus_points = ? WHERE user_id = ?', (new_points, user_id))
        cursor.execute(
            'INSERT INTO transactions (user_id, amount, description) VALUES (?, ?, ?)',
            (user_id, points if action == "Начислить бонусы" else -points, description)
        )
        conn.commit()
        
        # Уведомление администратора
        await message.answer(
            f"✅ Успешно!\n"
            f"Пользователю ID: {user_id}\n"
            f"{operation_type.capitalize()}: {points} бонусов\n"
            f"Новый баланс: {new_points} бонусов",
            reply_markup=get_admin_keyboard()
        )
        
        # Уведомление пользователя
        try:
            await bot.send_message(
                user_id,
                f"📢 Уведомление о бонусах\n"
                f"Вам {operation_type} {points} бонусов\n"
                f"Текущий баланс: {new_points} бонусов\n"
                f"Операция: {description}"
            )
        except Exception as e:
            print(f"Не удалось отправить уведомление пользователю {user_id}: {e}")
        
        await state.clear()
        
    except ValueError:
        await message.answer(
            "Введите корректное число бонусов (целое положительное число).",
            reply_markup=get_admin_keyboard()
        )

@dp.message(F.text == "Отмена")
async def cancel_action(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id in ADMIN_IDS:
        await message.answer(
            "Действие отменено.",
            reply_markup=get_admin_keyboard()
        )
    else:
        await message.answer(
            "Действие отменено.",
            reply_markup=get_user_keyboard()
        )

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())