import os
import sqlite3
import qrcode
import io
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import ReplyKeyboardBuilder, ReplyKeyboardMarkup
from aiogram.types import (
    ReplyKeyboardRemove,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.filters import Command
from dotenv import load_dotenv
from PIL import Image
from pyzbar.pyzbar import decode

# Настройка путей
SCRIPT_DIR = Path(__file__).parent.absolute()
NEWS_IMAGES_DIR = SCRIPT_DIR / "news_images"
os.makedirs(NEWS_IMAGES_DIR, exist_ok=True)

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
ADMIN_IDS = [int(id_str) for id_str in os.getenv('ADMIN_IDS', '').split(',') if id_str]
SMM_IDS = [int(id_str) for id_str in os.getenv('SMM_IDS', '').split(',') if id_str]

# Инициализация бота
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Инициализация базы данных
DB_PATH = SCRIPT_DIR / 'coffee_bot.db'
conn = sqlite3.connect(DB_PATH)
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

cursor.execute('''
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id INTEGER,
    title TEXT,
    content TEXT,
    image_path TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (author_id) REFERENCES users (user_id)
)
''')
conn.commit()

# Состояния FSM
class RegistrationStates(StatesGroup):
    waiting_for_phone = State()

class AdminStates(StatesGroup):
    waiting_for_scan_or_id = State()
    waiting_for_points_action = State()
    waiting_for_points_amount = State()
    waiting_for_receipt_amount = State()

class SMMStates(StatesGroup):
    waiting_for_news_title = State()
    waiting_for_news_content = State()
    waiting_for_news_image = State()
    waiting_post_action = State()
    waiting_edit_field = State()
    waiting_new_value = State()

# ========== КЛАВИАТУРЫ ==========
def get_user_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Мой QR-код")],
            [types.KeyboardButton(text="Мои бонусы")],
            [types.KeyboardButton(text="История операций")],
            [types.KeyboardButton(text="Новости и акции")]
        ],
        resize_keyboard=True
    )

def get_admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Сканировать QR"), types.KeyboardButton(text="Ввести ID вручную")],
            [types.KeyboardButton(text="Назад")]
        ],
        resize_keyboard=True
    )

def get_smm_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Создать пост")],
            [types.KeyboardButton(text="Мои посты")]
        ],
        resize_keyboard=True
    )

def get_post_management_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="Редактировать заголовок"))
    builder.add(types.KeyboardButton(text="Редактировать текст"))
    builder.add(types.KeyboardButton(text="Заменить изображение"))
    builder.add(types.KeyboardButton(text="Удалить пост"))
    builder.add(types.KeyboardButton(text="Отмена"))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

# ========== ОСНОВНЫЕ КОМАНДЫ ==========
@dp.message(Command('start'))
async def start_command(message: types.Message):
    user_id = message.from_user.id
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    
    if user:
        if user_id in ADMIN_IDS:
            await message.answer("Добро пожаловать, администратор!", reply_markup=get_admin_keyboard())
        elif user_id in SMM_IDS:
            await message.answer("Добро пожаловать, SMM-менеджер!", reply_markup=get_smm_keyboard())
        else:
            await message.answer("Добро пожаловать обратно!", reply_markup=get_user_keyboard())
    else:
        await message.answer("Добро пожаловать! Для регистрации введите /register", reply_markup=ReplyKeyboardRemove())

# ========== РЕГИСТРАЦИЯ ==========
@dp.message(Command('register'))
async def register_command(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    if cursor.fetchone():
        keyboard = get_admin_keyboard() if user_id in ADMIN_IDS else get_user_keyboard()
        await message.answer("Вы уже зарегистрированы!", reply_markup=keyboard)
        return
    
    await message.answer("Введите ваш номер телефона для регистрации:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(RegistrationStates.waiting_for_phone)

@dp.message(RegistrationStates.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text
    user = message.from_user
    
    try:
        cursor.execute(
            'INSERT INTO users (user_id, username, full_name, phone) VALUES (?, ?, ?, ?)',
            (user.id, user.username, user.full_name, phone)
        )
        conn.commit()
        
        await state.clear()
        if user.id in ADMIN_IDS:
            await message.answer("Регистрация завершена!", reply_markup=get_admin_keyboard())
        elif user.id in SMM_IDS:
            await message.answer("Регистрация завершена!", reply_markup=get_smm_keyboard())
        else:
            await message.answer("Регистрация завершена!", reply_markup=get_user_keyboard())
    except Exception as e:
        await message.answer("Ошибка при регистрации. Попробуйте позже.", reply_markup=ReplyKeyboardRemove())

# ========== ПОЛЬЗОВАТЕЛЬСКИЕ ФУНКЦИИ ==========
@dp.message(F.text == "Мой QR-код")
async def handle_qr_request(message: types.Message):
    if message.from_user.id in (ADMIN_IDS + SMM_IDS):
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
        caption="Ваш QR-код для бонусной программы",
        reply_markup=get_user_keyboard()
    )

@dp.message(F.text == "Мои бонусы")
async def show_bonuses(message: types.Message):
    user_id = message.from_user.id
    cursor.execute('SELECT bonus_points FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    
    if result:
        if user_id in ADMIN_IDS:
            await message.answer(f"Ваш баланс: {result[0]}", reply_markup=get_admin_keyboard())
        elif user_id in SMM_IDS:
            await message.answer(f"Ваш баланс: {result[0]}", reply_markup=get_smm_keyboard())
        else:
            await message.answer(f"Ваш баланс: {result[0]}", reply_markup=get_user_keyboard())

@dp.message(F.text == "История операций")
async def show_history(message: types.Message):
    user_id = message.from_user.id
    cursor.execute('''
    SELECT amount, description, timestamp FROM transactions 
    WHERE user_id = ? ORDER BY timestamp DESC LIMIT 10
    ''', (user_id,))
    
    transactions = cursor.fetchall()
    if not transactions:
        await message.answer("У вас пока нет операций", reply_markup=get_user_keyboard())
        return
    
    response = "Последние 10 операций:\n\n"
    for amount, description, timestamp in transactions:
        response += f"{timestamp}: {description} - {amount} бонусов\n"
    
    await message.answer(response, reply_markup=get_user_keyboard())

# ========== АДМИН-ФУНКЦИИ ==========
@dp.message(F.text.in_(["Сканировать QR", "Ввести ID вручную"]))
async def handle_admin_commands(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    if message.text == "Сканировать QR":
        await message.answer("Отправьте фото QR-кода:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(AdminStates.waiting_for_scan_or_id)
    else:
        await message.answer("Введите ID пользователя:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(AdminStates.waiting_for_scan_or_id)

@dp.message(F.text == "Назад")
async def back_to_menu(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    await state.clear()
    
    if user_id in ADMIN_IDS:
        await message.answer("Главное меню администратора", reply_markup=get_admin_keyboard())
    elif user_id in SMM_IDS:
        await message.answer("Главное меню SMM", reply_markup=get_smm_keyboard())
    else:
        await message.answer("Главное меню", reply_markup=get_user_keyboard())

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
        await message.answer(f"Ошибка: {str(e)}", reply_markup=get_admin_keyboard())

@dp.message(AdminStates.waiting_for_scan_or_id)
async def process_manual_input(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await process_user_id(user_id, message, state)
    except ValueError:
        await message.answer("Некорректный ID", reply_markup=get_admin_keyboard())

async def process_user_id(user_id: int, message: types.Message, state: FSMContext):
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    if not user:
        await message.answer("Пользователь не найден", reply_markup=get_admin_keyboard())
        await state.clear()
        return
    
    await state.update_data(user_id=user_id)
    
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="Начислить бонусы"))
    builder.add(types.KeyboardButton(text="Списать бонусы"))
    builder.add(types.KeyboardButton(text="Отмена"))
    builder.adjust(2)
    
    await message.answer(
        f"Пользователь: {user[2]}\nБаланс: {user[4]} бонусов\nВыберите действие:",
        reply_markup=builder.as_markup(resize_keyboard=True)
    )
    await state.set_state(AdminStates.waiting_for_points_action)

@dp.message(AdminStates.waiting_for_points_action)
async def process_points_action(message: types.Message, state: FSMContext):
    if message.text == "Назад":
        await back_to_menu(message, state)
        return
    
    if message.text not in ["Начислить бонусы", "Списать бонусы"]:
        return
    
    await state.update_data(action=message.text)
    
    if message.text == "Начислить бонусы":
        await message.answer("Введите сумму чека (рубли):", reply_markup=ReplyKeyboardRemove())
        await state.set_state(AdminStates.waiting_for_receipt_amount)
    else:
        await message.answer("Введите количество бонусов:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(AdminStates.waiting_for_points_amount)

@dp.message(AdminStates.waiting_for_receipt_amount)
async def process_receipt_amount(message: types.Message, state: FSMContext):
    try:
        receipt_amount = float(message.text.replace(',', '.'))
        if receipt_amount <= 0:
            raise ValueError("Сумма должна быть положительной")
            
        points = int(round(receipt_amount * 0.15))
        data = await state.get_data()
        user_id = data['user_id']
        
        cursor.execute('UPDATE users SET bonus_points = bonus_points + ? WHERE user_id = ?', (points, user_id))
        cursor.execute(
            'INSERT INTO transactions (user_id, amount, description) VALUES (?, ?, ?)',
            (user_id, points, f"Начисление 10% от чека {receipt_amount} руб.")
        )
        conn.commit()
        
        cursor.execute('SELECT bonus_points FROM users WHERE user_id = ?', (user_id,))
        new_balance = cursor.fetchone()[0]
        
        await message.answer(
            f"✅ Бонусы:\n"
            f"Начислено {points}\n"
            f"Новый баланс: {new_balance}",
            reply_markup=get_admin_keyboard()
        )
        
        try:
            await bot.send_message(
                user_id,
                f"✅ Ваши бонусы:\n"
                f"📢Начислено {points}\n"
                f"Новый баланс: {new_balance}",
            )
        except:
            pass
        
        await state.clear()
    except ValueError:
        await message.answer("Введите корректную сумму", reply_markup=get_admin_keyboard())

@dp.message(AdminStates.waiting_for_points_amount)
async def process_points_amount(message: types.Message, state: FSMContext):
    try:
        points = int(message.text)
        if points <= 0:
            raise ValueError("Число должно быть положительным")
            
        data = await state.get_data()
        user_id = data['user_id']
        
        cursor.execute('SELECT bonus_points FROM users WHERE user_id = ?', (user_id,))
        current_points = cursor.fetchone()[0]
        
        if data['action'] == "Списать бонусы" and current_points < points:
            await message.answer("Недостаточно бонусов", reply_markup=get_admin_keyboard())
            return
        
        new_points = current_points + (points if data['action'] == "Начислить бонусы" else -points)
        
        cursor.execute('UPDATE users SET bonus_points = ? WHERE user_id = ?', (new_points, user_id))
        cursor.execute(
            'INSERT INTO transactions (user_id, amount, description) VALUES (?, ?, ?)',
            (user_id, points if data['action'] == "Начислить бонусы" else -points, 
             "Начисление" if data['action'] == "Начислить бонусы" else "Списание")
        )
        conn.commit()
        
        await message.answer(
            f"✅ Успешно!\n"
            f"Новый баланс: {new_points}",
            reply_markup=get_admin_keyboard()
        )
        
        try:
            await bot.send_message(
                user_id,
                f"📢 Вам {data['action'].lower()} {points} бонусов\n"
                f"Текущий баланс: {new_points}"
            )
        except:
            pass
        
        await state.clear()
    except ValueError:
        await message.answer("Введите целое число", reply_markup=get_admin_keyboard())

# ========== SMM ФУНКЦИИ ==========
@dp.message(F.text == "Создать пост")
async def start_news_creation(message: types.Message, state: FSMContext):
    if message.from_user.id not in SMM_IDS:
        return
    
    await message.answer("Введите заголовок поста:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(SMMStates.waiting_for_news_title)

@dp.message(SMMStates.waiting_for_news_title)
async def process_news_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    await message.answer("Введите текст поста:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(SMMStates.waiting_for_news_content)

@dp.message(SMMStates.waiting_for_news_content)
async def process_news_content(message: types.Message, state: FSMContext):
    await state.update_data(content=message.text)
    await message.answer("Отправьте изображение (или /skip чтобы пропустить):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(SMMStates.waiting_for_news_image)

@dp.message(SMMStates.waiting_for_news_image, F.content_type == 'photo')
async def process_news_image(message: types.Message, state: FSMContext):
    data = await state.get_data()
    
    if 'title' not in data or 'content' not in data:
        await message.answer("Ошибка: данные поста потеряны. Начните заново.", reply_markup=get_smm_keyboard())
        await state.clear()
        return
    
    # Сохраняем изображение
    file = await bot.get_file(message.photo[-1].file_id)
    img_filename = f"{message.from_user.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
    img_path = NEWS_IMAGES_DIR / img_filename
    
    await bot.download_file(file.file_path, img_path)
    
    await save_and_publish_news(message, data, str(img_path.relative_to(SCRIPT_DIR)))
    await state.clear()

@dp.message(SMMStates.waiting_for_news_image, Command('skip'))
async def skip_news_image(message: types.Message, state: FSMContext):
    data = await state.get_data()
    
    if 'title' not in data or 'content' not in data:
        await message.answer("Ошибка: данные поста потеряны. Начните заново.", reply_markup=get_smm_keyboard())
        await state.clear()
        return
    
    await save_and_publish_news(message, data, None)
    await state.clear()

async def save_and_publish_news(message: types.Message, data: dict, image_path: str):
    try:
        cursor.execute(
            'INSERT INTO news (author_id, title, content, image_path) VALUES (?, ?, ?, ?)',
            (message.from_user.id, data['title'], data['content'], image_path)
        )
        conn.commit()
        
        cursor.execute('SELECT user_id FROM users')
        users = [row[0] for row in cursor.fetchall()]
        
        for user_id in users:
            try:
                if image_path:
                    full_image_path = SCRIPT_DIR / image_path
                    with open(full_image_path, 'rb') as photo:
                        await bot.send_photo(
                            user_id,
                            photo=BufferedInputFile(photo.read(), filename="news.jpg"),
                            caption=f"<b>{data['title']}</b>\n\n{data['content']}"
                        )
                else:
                    await bot.send_message(
                        user_id,
                        f"<b>{data['title']}</b>\n\n{data['content']}"
                    )
            except Exception as e:
                logger.error(f"Failed to send news to {user_id}: {e}")
        
        await message.answer("Пост опубликован!", reply_markup=get_smm_keyboard())
    except Exception as e:
        logger.error(f"Ошибка при сохранении поста: {e}")
        await message.answer("Произошла ошибка при сохранении поста", reply_markup=get_smm_keyboard())

@dp.message(F.text == "Мои посты")
async def show_smm_posts(message: types.Message, state: FSMContext):
    if message.from_user.id not in SMM_IDS:
        return
    
    cursor.execute('''
    SELECT id, title, content, image_path 
    FROM news 
    WHERE author_id = ? 
    ORDER BY timestamp DESC
    LIMIT 10
    ''', (message.from_user.id,))
    
    posts = cursor.fetchall()
    if not posts:
        await message.answer("У вас нет постов", reply_markup=get_smm_keyboard())
        return
    
    await state.update_data(posts={post[0]: post[1:] for post in posts})
    
    for post_id, title, content, image_path in posts:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Управление постом", callback_data=f"manage_post_{post_id}")]
        ])
        
        try:
            if image_path:
                full_image_path = SCRIPT_DIR / image_path
                if full_image_path.exists():
                    with open(full_image_path, 'rb') as photo:
                        await message.answer_photo(
                            photo=BufferedInputFile(photo.read(), filename="post.jpg"),
                            caption=f"<b>{title}</b>\n\n{content}",
                            reply_markup=kb
                        )
                else:
                    await message.answer(
                        f"<b>{title}</b>\n\n{content}\n\n[Изображение недоступно]",
                        reply_markup=kb
                    )
            else:
                await message.answer(
                    f"<b>{title}</b>\n\n{content}",
                    reply_markup=kb
                )
        except Exception as e:
            logger.error(f"Ошибка при показе поста {post_id}: {e}")
            await message.answer(
                f"<b>{title}</b>\n\n{content}\n\n[Ошибка при загрузке изображения]",
                reply_markup=kb
            )

@dp.callback_query(F.data.startswith("manage_post_"))
async def manage_post(callback: types.CallbackQuery, state: FSMContext):
    post_id = int(callback.data.split("_")[-1])
    await state.update_data(current_post_id=post_id)
    await callback.message.answer(
        "Выберите действие с постом:",
        reply_markup=get_post_management_keyboard()
    )
    await state.set_state(SMMStates.waiting_post_action)
    await callback.answer()

@dp.message(SMMStates.waiting_post_action)
async def process_post_action(message: types.Message, state: FSMContext):
    data = await state.get_data()
    post_id = data['current_post_id']
    
    if message.text == "Удалить пост":
        cursor.execute('SELECT image_path FROM news WHERE id = ?', (post_id,))
        result = cursor.fetchone()
        if result and result[0]:
            try:
                image_path = SCRIPT_DIR / result[0]
                if image_path.exists():
                    os.remove(image_path)
            except Exception as e:
                logger.error(f"Ошибка при удалении изображения: {e}")
        
        cursor.execute('DELETE FROM news WHERE id = ?', (post_id,))
        conn.commit()
        await message.answer("Пост успешно удалён", reply_markup=get_smm_keyboard())
        await state.clear()
    
    elif message.text.startswith("Редактировать"):
        field = message.text.split()[-1]
        await state.update_data(edit_field=field.lower())
        await message.answer(f"Введите новый {field}:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(SMMStates.waiting_new_value)
    
    elif message.text == "Заменить изображение":
        # Получаем текущий пост из базы данных
        cursor.execute('SELECT title, content FROM news WHERE id = ?', (post_id,))
        post = cursor.fetchone()
        
        if not post:
            await message.answer("Ошибка: пост не найден", reply_markup=get_smm_keyboard())
            await state.clear()
            return
            
        title, content = post
        
        # Сохраняем данные поста в состоянии
        await state.update_data(
            current_post_id=post_id,
            title=title,
            content=content,
            is_image_replacement=True  # Флаг, что это замена изображения
        )
        
        await message.answer("Отправьте новое изображение:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(SMMStates.waiting_for_news_image)
    
    elif message.text == "Отмена":
        await message.answer("Действие отменено", reply_markup=get_smm_keyboard())
        await state.clear()

@dp.message(SMMStates.waiting_new_value)
async def process_new_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    post_id = data['current_post_id']
    field = data['edit_field']
    
    if field in ['заголовок', 'текст']:
        db_field = 'title' if field == 'заголовок' else 'content'
        cursor.execute(f'UPDATE news SET {db_field} = ? WHERE id = ?', 
                      (message.text, post_id))
        conn.commit()
        await message.answer(f"{field.capitalize()} успешно обновлён", reply_markup=get_smm_keyboard())
    await state.clear()

@dp.message(SMMStates.waiting_for_news_image, F.content_type == 'photo')
async def process_news_image(message: types.Message, state: FSMContext):
    data = await state.get_data()
    
    # Проверяем, это создание нового поста или замена изображения
    is_replacement = data.get('is_image_replacement', False)
    
    if is_replacement:
        # Это замена изображения в существующем посте
        post_id = data['current_post_id']
        title = data['title']
        content = data['content']
        
        # Получаем старое изображение
        cursor.execute('SELECT image_path FROM news WHERE id = ?', (post_id,))
        old_image_path = cursor.fetchone()[0]
        
        # Сохраняем новое изображение
        file = await bot.get_file(message.photo[-1].file_id)
        img_filename = f"{message.from_user.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
        img_path = NEWS_IMAGES_DIR / img_filename
        
        await bot.download_file(file.file_path, img_path)
        
        # Удаляем старое изображение, если оно существует
        if old_image_path:
            old_full_path = SCRIPT_DIR / old_image_path
            if old_full_path.exists():
                try:
                    os.remove(old_full_path)
                except Exception as e:
                    logger.error(f"Ошибка при удалении старого изображения: {e}")
        
        # Обновляем изображение в базе данных
        relative_img_path = str(img_path.relative_to(SCRIPT_DIR))
        cursor.execute('UPDATE news SET image_path = ? WHERE id = ?', (relative_img_path, post_id))
        conn.commit()
        
        # Показываем обновленный пост
        with open(img_path, 'rb') as photo:
            await message.answer_photo(
                photo=BufferedInputFile(photo.read(), filename="updated_post.jpg"),
                caption=f"<b>{title}</b>\n\n{content}",
                reply_markup=get_smm_keyboard()
            )
            
        await message.answer("Изображение в посте успешно обновлено!", reply_markup=get_smm_keyboard())
        await state.clear()
        
    else:
        # Это создание нового поста (существующий код)
        if 'title' not in data or 'content' not in data:
            await message.answer("Ошибка: данные поста потеряны. Начните заново.", reply_markup=get_smm_keyboard())
            await state.clear()
            return
        
        # Сохраняем изображение
        file = await bot.get_file(message.photo[-1].file_id)
        img_filename = f"{message.from_user.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
        img_path = NEWS_IMAGES_DIR / img_filename
        
        await bot.download_file(file.file_path, img_path)
        
        await save_and_publish_news(message, data, str(img_path.relative_to(SCRIPT_DIR)))
        await state.clear()

@dp.message(F.text == "Новости и акции")
async def show_news(message: types.Message):
    cursor.execute('''
    SELECT title, content, image_path FROM news 
    ORDER BY timestamp DESC 
    LIMIT 10
    ''')
    
    news_items = cursor.fetchall()
    if not news_items:
        await message.answer("Новостей пока нет", reply_markup=get_user_keyboard())
        return
    
    for title, content, image_path in news_items:
        try:
            if image_path:
                full_image_path = SCRIPT_DIR / image_path
                if full_image_path.exists():
                    with open(full_image_path, 'rb') as photo:
                        await message.answer_photo(
                            photo=BufferedInputFile(photo.read(), filename="news.jpg"),
                            caption=f"<b>{title}</b>\n\n{content}",
                            reply_markup=get_user_keyboard()
                        )
                else:
                    await message.answer(
                        f"<b>{title}</b>\n\n{content}\n\n[Изображение недоступно]",
                        reply_markup=get_user_keyboard()
                    )
            else:
                await message.answer(
                    f"<b>{title}</b>\n\n{content}",
                    reply_markup=get_user_keyboard()
                )
        except Exception as e:
            await message.answer(
                f"<b>{title}</b>\n\n{content}\n\n[Ошибка при загрузке изображения]",
                reply_markup=get_user_keyboard()
            )

@dp.message(F.text == "Отмена")
async def cancel_action(message: types.Message, state: FSMContext):
    await state.clear()
    await back_to_menu(message)

# ========== ЗАПУСК БОТА ==========
async def main():
    logger.info("Starting bot...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
    finally:
        conn.close()
        logger.info("Database connection closed")