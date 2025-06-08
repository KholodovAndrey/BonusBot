import os
import sqlite3
import qrcode
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.types import ReplyKeyboardRemove, BufferedInputFile
from aiogram.types import Message, ContentType
from aiogram.filters import Command
import io
import asyncio
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
    waiting_for_scan = State()
    waiting_for_user_id = State()
    waiting_for_points = State()

# Команда старта
@dp.message(Command('start'))
async def start_command(message: types.Message):
    user_id = message.from_user.id
    
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    
    if user:
        await message.answer("Добро пожаловать обратно! Используйте /menu для просмотра возможностей.")
    else:
        await message.answer("Добро пожаловать в нашу кофейню! Для регистрации введите /register")

# Регистрация пользователя
@dp.message(Command('register'))
async def register_command(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    if cursor.fetchone():
        await message.answer("Вы уже зарегистрированы!")
        return
    
    await message.answer("Пожалуйста, введите ваш номер телефона для регистрации:")
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
    await message.answer("Регистрация завершена! Теперь вы можете использовать /menu для просмотра возможностей.")

# Меню пользователя
@dp.message(Command('menu'))
async def user_menu(message: types.Message):
    user_id = message.from_user.id
    
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    
    if not user:
        await message.answer("Вы не зарегистрированы. Введите /register для регистрации.")
        return
    
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="Мой QR-код"))
    builder.add(types.KeyboardButton(text="Мои бонусы"))
    builder.add(types.KeyboardButton(text="История операций"))
    builder.adjust(2)
    
    await message.answer("Выберите действие:", reply_markup=builder.as_markup(resize_keyboard=True))

# Показать QR-код
@dp.message(lambda message: message.text == "Мой QR-код")
async def show_qr_code(message: types.Message):
    user_id = message.from_user.id
    
    # Генерируем QR-код с ID пользователя
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(str(user_id))
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Сохраняем изображение в буфер
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    
    # Создаем BufferedInputFile
    photo = BufferedInputFile(buf.getvalue(), filename="qrcode.png")
    
    # Отправляем изображение
    await message.answer_photo(photo, caption="Ваш QR-код для бонусной программы")

# Показать бонусы
@dp.message(lambda message: message.text == "Мои бонусы")
async def show_bonuses(message: types.Message):
    user_id = message.from_user.id
    
    cursor.execute('SELECT bonus_points FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    
    if result:
        await message.answer(f"Ваш текущий баланс бонусов: {result[0]}")
    else:
        await message.answer("Вы не зарегистрированы в системе.")

# Показать историю операций
@dp.message(lambda message: message.text == "История операций")
async def show_history(message: types.Message):
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
        await message.answer("У вас пока нет операций.")
        return
    
    response = "Последние 10 операций:\n\n"
    for amount, description, timestamp in transactions:
        response += f"{timestamp}: {description} - {amount} бонусов\n"
    
    await message.answer(response)

# Админ-панель
@dp.message(Command('admin'))
async def admin_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещен")
        return
    
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="Начислить бонусы"))
    builder.add(types.KeyboardButton(text="Списать бонусы"))
    builder.add(types.KeyboardButton(text="Сканировать QR"))
    builder.add(types.KeyboardButton(text="Отмена"))
    builder.adjust(2)
    
    await message.answer("Админ-панель. Выберите действие:", reply_markup=builder.as_markup(resize_keyboard=True))

# Обработчик кнопки "Сканировать QR"
@dp.message(lambda message: message.text == "Сканировать QR")
async def scan_qr_command(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещен")
        return
    
    await message.answer("Пожалуйста, отправьте фото QR-кода или введите ID пользователя вручную:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(AdminStates.waiting_for_scan)


@dp.message(AdminStates.waiting_for_scan, F.content_type == 'photo')
async def process_qr_scan(message: types.Message, state: FSMContext):
    try:
        # Получаем файл изображения
        file_id = message.photo[-1].file_id
        file = await bot.get_file(file_id)
        
        # Создаем временный буфер для изображения
        img_buffer = io.BytesIO()
        await bot.download_file(file.file_path, destination=img_buffer)
        img_buffer.seek(0)
        
        # Декодируем QR-код
        img = Image.open(img_buffer)
        decoded = decode(img)
        
        if not decoded:
            await message.answer("Не удалось распознать QR-код. Попробуйте еще раз или введите ID вручную.")
            return
            
        user_id = int(decoded[0].data.decode())
        await state.update_data(user_id=user_id)
        
        # Проверяем существование пользователя
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        if not cursor.fetchone():
            await message.answer(f"Пользователь с ID {user_id} не найден.")
            await state.clear()
            return
        
        # Предлагаем выбрать действие
        builder = ReplyKeyboardBuilder()
        builder.add(types.KeyboardButton(text="Начислить бонусы"))
        builder.add(types.KeyboardButton(text="Списать бонусы"))
        builder.add(types.KeyboardButton(text="Отмена"))
        builder.adjust(2)
        
        await message.answer(
            f"Найден пользователь ID: {user_id}. Выберите действие:",
            reply_markup=builder.as_markup(resize_keyboard=True)
        )
        await state.set_state(AdminStates.waiting_for_action)
        
    except ValueError:
        await message.answer("Ошибка: QR-код содержит некорректный ID. Попробуйте еще раз.")
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}. Попробуйте еще раз.")
    finally:
        if 'img_buffer' in locals():
            img_buffer.close()


@dp.message(lambda message: message.text in ["Начислить бонусы", "Списать бонусы"])
async def admin_action(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещен")
        return
    
    action = "add" if message.text == "Начислить бонусы" else "subtract"
    
    await state.set_state(AdminStates.waiting_for_user_id)
    await state.update_data(action=action)
    await message.answer("Введите ID пользователя:", reply_markup=ReplyKeyboardRemove())

# Модифицируем обработчик ручного ввода ID
@dp.message(AdminStates.waiting_for_scan)
async def process_manual_user_id(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await state.update_data(user_id=user_id)
        
        # Проверяем существование пользователя
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        if not cursor.fetchone():
            await message.answer(f"Пользователь с ID {user_id} не найден.")
            await state.clear()
            return
        
        await message.answer(f"Найден пользователь ID: {user_id}. Выберите действие:", 
                           reply_markup=types.ReplyKeyboardMarkup(
                               keyboard=[
                                   [types.KeyboardButton(text="Начислить бонусы")],
                                   [types.KeyboardButton(text="Списать бонусы")],
                                   [types.KeyboardButton(text="Отмена")]
                               ],
                               resize_keyboard=True
                           ))
        await state.set_state(AdminStates.waiting_for_action)
        
    except ValueError:
        await message.answer("Пожалуйста, введите корректный ID пользователя (число) или отправьте фото QR-кода.")

@dp.message(AdminStates.waiting_for_points)
async def process_points(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещен")
        await state.clear()
        return
    
    try:
        points = int(message.text)
    except ValueError:
        await message.answer("Пожалуйста, введите корректное количество бонусов (число).")
        return
    
    if points <= 0:
        await message.answer("Количество бонусов должно быть положительным числом.")
        return
    
    data = await state.get_data()
    user_id = data['user_id']
    action = data['action']
    
    cursor.execute('SELECT bonus_points FROM users WHERE user_id = ?', (user_id,))
    current_points = cursor.fetchone()[0]
    
    if action == "add":
        new_points = current_points + points
        description = "Начисление администратором"
    else:
        if current_points < points:
            await message.answer("У пользователя недостаточно бонусов.")
            await state.clear()
            return
        new_points = current_points - points
        description = "Списание администратором"
    
    cursor.execute('UPDATE users SET bonus_points = ? WHERE user_id = ?', (new_points, user_id))
    cursor.execute(
        'INSERT INTO transactions (user_id, amount, description) VALUES (?, ?, ?)',
        (user_id, points if action == "add" else -points, description)
    )
    conn.commit()
    
    await message.answer(f"Операция успешно выполнена. Новый баланс пользователя: {new_points}")
    await state.clear()

# Отмена действий
@dp.message(lambda message: message.text == "Отмена")
async def cancel_action(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещен")
        return
    
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())