import os
import time
import math
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, session
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

# Облако для картинок
import cloudinary
import cloudinary.uploader
import cloudinary.api

# ЗАЩИТА ОТ CSRF
from flask_wtf.csrf import CSRFProtect

app = Flask(__name__)

# --- БЕРЕМ НАСТРОЙКИ ИЗ СЕКРЕТОВ (SECRETS) ---
app.secret_key = os.environ.get('SECRET_KEY')

# Включаем защиту!
csrf = CSRFProtect(app)

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# =================================================================
# 👇 ПОДКЛЮЧЕНИЕ БАЗЫ И CLOUDINARY ЧЕРЕЗ СЕКРЕТЫ 👇
# =================================================================

# 1. Получаем ссылку на базу данных
DB_URL = os.environ.get('DATABASE_URL')

# Исправляем ссылку, если она начинается на postgres:// (для совместимости)
if DB_URL and DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

# 2. Настраиваем Cloudinary
cloudinary.config(
  cloud_name = os.environ.get('CLOUDINARY_CLOUD_NAME'),
  api_key = os.environ.get('CLOUDINARY_API_KEY'),
  api_secret = os.environ.get('CLOUDINARY_API_SECRET')
)
# =================================================================

def get_db_connection():
    try:
        conn = psycopg2.connect(DB_URL)
        return conn
    except Exception as e:
        print(f"❌ Ошибка подключения к базе Neon: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    cursor = conn.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                      (login text PRIMARY KEY, 
                       password text, 
                       nickname text, 
                       is_admin INTEGER DEFAULT 0, 
                       is_banned INTEGER DEFAULT 0,
                       is_moderator INTEGER DEFAULT 0,
                       can_ban INTEGER DEFAULT 0,
                       can_chat INTEGER DEFAULT 0)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS items 
                      (id SERIAL PRIMARY KEY, 
                       owner_login text, owner_name text, title text, price text, description text, contact text, category text,
                       region text, city text,
                       image1 text, image2 text, image3 text, image4 text, image5 text,
                       vip_expiry REAL DEFAULT 0,
                       views INTEGER DEFAULT 0,
                       created_at REAL DEFAULT 0)''') 

    cursor.execute('''CREATE TABLE IF NOT EXISTS reviews 
                      (id SERIAL PRIMARY KEY, item_id INTEGER, author text, text text, stars INTEGER, date text)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS messages 
                      (id SERIAL PRIMARY KEY, sender text, receiver text, text text, date text)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS favorites 
                      (user_login text, item_id INTEGER)''')

    conn.commit()
    conn.close()

init_db()

def get_seller_rating(seller_login):
    conn = get_db_connection()
    if not conn: return 0, 0
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT id FROM items WHERE owner_login = %s", (seller_login,))
    items = cursor.fetchall()

    if not items:
        conn.close()
        return 0, 0

    total_stars = 0
    count = 0
    for item in items:
        cursor.execute("SELECT stars FROM reviews WHERE item_id = %s", (item['id'],))
        reviews = cursor.fetchall()
        for review in reviews:
            total_stars += review['stars']
            count += 1
    conn.close()
    if count == 0: return 0, 0
    return round(total_stars / count, 1), count

def get_search_variants(query):
    query = query.lower().strip()
    variants = {query}
    synonyms = {
        'bmw': 'бмв', 'бмв': 'bmw', 'mercedes': 'мерседес', 'мерседес': 'mercedes', 'benz': 'бенц',
        'audi': 'ауди', 'ауди': 'audi', 'vw': 'фольксваген', 'volkswagen': 'фольксваген', 'фольксваген': 'vw',
        'toyota': 'тойота', 'тойота': 'toyota', 'lexus': 'лексус', 'лексус': 'lexus',
        'kia': 'киа', 'киа': 'kia', 'hyundai': 'хендай', 'хендай': 'hyundai',
        'ford': 'форд', 'форд': 'ford', 'mazda': 'мазда', 'мазда': 'mazda',
        'honda': 'хонда', 'хонда': 'honda', 'nissan': 'ниссан', 'ниссан': 'nissan',
        'tesla': 'тесла', 'тесла': 'tesla', 'chevrolet': 'шевроле', 'шевроле': 'chevrolet',
        'porsche': 'порш', 'порш': 'porsche', 'skoda': 'шкода', 'шкода': 'skoda',
        'volvo': 'вольво', 'вольво': 'volvo'
    }
    words = query.split()
    translated_words = []
    for word in words:
        if word in synonyms:
            translated_words.append(synonyms[word])
        else:
            table = str.maketrans("abcehkmoptxy", "авсенкмортху")
            translated_words.append(word.translate(table))
    variants.add(" ".join(translated_words))
    for w in translated_words:
        variants.add(w)
    return list(variants)


# --- ROUTES ---
@app.route('/')
def home():
    if 'user' in session:
        current_user_login = session['user']
        current_user_name = session.get('nickname')
        user_is_admin = session.get('is_admin')
    else:
        current_user_login = None
        current_user_name = None
        user_is_admin = 0

    page = request.args.get('page', 1, type=int)
    search_query = request.args.get('q', '')
    category_filter = request.args.get('cat', '')
    country_filter = request.args.get('country', '')

    conn = get_db_connection()
    if not conn: return "Ошибка подключения к базе данных"
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    liked_ids = []
    if current_user_login:
        cursor.execute("SELECT item_id FROM favorites WHERE user_login = %s", (current_user_login,))
        likes = cursor.fetchall()
        liked_ids = [row['item_id'] for row in likes]

    sql = "SELECT * FROM items WHERE 1=1"
    params = []

    if search_query:
        variants = get_search_variants(search_query)
        search_conditions = []
        for v in variants:
            search_conditions.append("title ILIKE %s")
            search_conditions.append("city ILIKE %s")
            params.append(f"%{v}%")
            params.append(f"%{v}%")
        if search_conditions:
            sql += " AND (" + " OR ".join(search_conditions) + ")"

    if category_filter and category_filter != 'Все':
        sql += " AND category = %s"
        params.append(category_filter)

    if country_filter:
        sql += " AND region = %s"
        params.append(country_filter)

    sql += " ORDER BY id DESC"

    cursor.execute(sql, tuple(params))
    all_items = cursor.fetchall()
    conn.close()

    vips = []
    regulars = []
    current_time = time.time()

    for item in all_items:
        if item['vip_expiry'] > current_time:
            vips.append(item)
        else:
            regulars.append(item)

    final_items = []
    vip_index = 0
    for i, item in enumerate(regulars):
        final_items.append(item)
        if (i + 1) % 5 == 0:
            if vip_index < len(vips):
                final_items.append(vips[vip_index])
                vip_index += 1
    while vip_index < len(vips):
        final_items.append(vips[vip_index])
        vip_index += 1
    if not regulars and vips:
        final_items = vips

    LIMIT = 15
    total_items = len(final_items)
    total_pages = math.ceil(total_items / LIMIT)
    offset = (page - 1) * LIMIT
    items_to_show = final_items[offset : offset + LIMIT]

    return render_template('index.html', user_login=current_user_login, user_name=current_user_name, is_admin=user_is_admin, items=items_to_show, search_query=search_query, category_filter=category_filter, page=page, total_pages=total_pages, time=time, liked_ids=liked_ids)

@app.route('/fav/<int:item_id>')
def toggle_fav(item_id):
    if 'user' not in session: return redirect('/login')
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM favorites WHERE user_login = %s AND item_id = %s", (session['user'], item_id))
    exists = cursor.fetchone()
    if exists:
        cursor.execute("DELETE FROM favorites WHERE user_login = %s AND item_id = %s", (session['user'], item_id))
    else:
        cursor.execute("INSERT INTO favorites (user_login, item_id) VALUES (%s, %s)", (session['user'], item_id))
    conn.commit()
    conn.close()
    return redirect(request.referrer or '/')

@app.route('/favorites')
def favorites_page():
    if 'user' not in session: return redirect('/login')
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("""
        SELECT items.* FROM items 
        JOIN favorites ON items.id = favorites.item_id 
        WHERE favorites.user_login = %s
        ORDER BY favorites.item_id DESC
    """, (session['user'],))
    items = cursor.fetchall()
    liked_ids = [item['id'] for item in items]
    conn.close()
    return render_template('favorites.html', items=items, liked_ids=liked_ids, time=time)

@app.route('/my_ads')
def my_ads():
    if 'user' not in session: return redirect('/login')
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM items WHERE owner_login = %s ORDER BY id DESC", (session['user'],))
    items = cursor.fetchall()
    conn.close()
    return render_template('index.html', items=items, user_login=session['user'], user_name=session.get('nickname'), is_admin=session.get('is_admin'), search_query="", category_filter="", page=1, total_pages=1, time=time, liked_ids=[])

@app.route('/support')
def support_chat():
    if 'user' not in session: return redirect('/login')
    my_login = session['user']
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("""
        SELECT * FROM messages 
        WHERE (sender = %s AND receiver = 'admin') 
           OR (sender = 'admin' AND receiver = %s)
        ORDER BY id ASC
    """, (my_login, my_login))
    messages = cursor.fetchall()
    conn.close()
    return render_template('support.html', messages=messages)

@app.route('/send_support', methods=['POST'])
def send_support():
    if 'user' not in session: return redirect('/login')
    text = request.form.get('text')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO messages (sender, receiver, text, date) VALUES (%s, 'admin', %s, %s)", 
                   (session['user'], text, time.strftime("%d.%m %H:%M")))
    conn.commit()
    conn.close()
    return redirect('/support')

@app.route('/admin/chats')
def admin_chats():
    if session.get('is_admin') != 1 and session.get('can_chat') != 1: return "Нет прав"
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT DISTINCT sender FROM messages WHERE receiver = 'admin'")
    senders = cursor.fetchall()
    conn.close()
    return render_template('admin_chats.html', senders=senders)

@app.route('/admin/chat/<user_login>')
def admin_chat_detail(user_login):
    if session.get('is_admin') != 1 and session.get('can_chat') != 1: return "Нет прав"
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("""
        SELECT * FROM messages 
        WHERE (sender = %s AND receiver = 'admin') 
           OR (sender = 'admin' AND receiver = %s)
        ORDER BY id ASC
    """, (user_login, user_login))
    messages = cursor.fetchall()
    conn.close()
    return render_template('admin_chat_detail.html', messages=messages, client_login=user_login)

@app.route('/admin/send_reply', methods=['POST'])
def admin_send_reply():
    if session.get('is_admin') != 1 and session.get('can_chat') != 1: return "Нет прав"
    client_login = request.form.get('client_login')
    text = request.form.get('text')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO messages (sender, receiver, text, date) VALUES ('admin', %s, %s, %s)", 
                   (client_login, text, time.strftime("%d.%m %H:%M")))
    conn.commit()
    conn.close()
    return redirect(f'/admin/chat/{client_login}')

@app.route('/make_vip/<int:item_id>/<int:days>')
def make_vip(item_id, days):
    if session.get('is_admin') != 1: return "Плати деньги!"
    duration_seconds = days * 24 * 60 * 60
    expiry_time = time.time() + duration_seconds
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE items SET vip_expiry = %s WHERE id = %s", (expiry_time, item_id))
    conn.commit()
    conn.close()
    return redirect(f'/item/{item_id}')

@app.route('/remove_vip/<int:item_id>')
def remove_vip(item_id):
    if session.get('is_admin') != 1: return "Плати деньги!"
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE items SET vip_expiry = 0 WHERE id = %s", (item_id,))
    conn.commit()
    conn.close()
    return redirect(f'/item/{item_id}')

@app.route('/item/<int:item_id>')
def item_detail(item_id):
    current_user_login = session.get('user')
    user_is_admin = session.get('is_admin', 0)
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("UPDATE items SET views = views + 1 WHERE id = %s", (item_id,))
    conn.commit()

    cursor.execute("SELECT * FROM items WHERE id = %s", (item_id,))
    item = cursor.fetchone()

    if not item: return "Товар не найден!"

    is_liked = False
    if current_user_login:
        cursor.execute("SELECT * FROM favorites WHERE user_login = %s AND item_id = %s", (current_user_login, item_id))
        if cursor.fetchone(): is_liked = True

    cursor.execute("SELECT * FROM reviews WHERE item_id = %s ORDER BY id DESC", (item_id,))
    reviews = cursor.fetchall()
    conn.close()
    seller_rating, reviews_count = get_seller_rating(item['owner_login'])
    return render_template('detail.html', item=item, reviews=reviews, user_login=current_user_login, is_admin=user_is_admin, rating=seller_rating, reviews_count=reviews_count, time=time, is_liked=is_liked)

@app.route('/edit/<int:item_id>', methods=['GET', 'POST'])
def edit_item(item_id):
    if 'user' not in session: return "Сначала войдите!"
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM items WHERE id = %s", (item_id,))
    item = cursor.fetchone()
    if not item or (item['owner_login'] != session['user'] and session.get('is_admin') != 1):
        conn.close()
        return "Нельзя редактировать чужое!"
    if request.method == 'POST':
        title = request.form.get('title')
        price = request.form.get('price')
        description = request.form.get('text')
        contact = request.form.get('contact')
        category = request.form.get('category')
        region = request.form.get('region')
        city = request.form.get('city')
        cursor.execute("""UPDATE items SET title=%s, price=%s, description=%s, contact=%s, category=%s, region=%s, city=%s WHERE id=%s""", 
             (title, price, description, contact, category, region, city, item_id))
        conn.commit()
        conn.close()
        return redirect(f'/item/{item_id}')
    conn.close()
    return render_template('edit.html', item=item)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        login = request.form.get('login')
        password = request.form.get('password')
        nickname = request.form.get('nickname')
        is_admin_val = 1 if login == 'admin' else 0
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM users WHERE login = %s", (login,))
        if cursor.fetchone():
            conn.close()
            return "Занят!"

        # ЗАЩИТА: Хешируем пароль
        hash_password = generate_password_hash(password)

        cursor.execute("INSERT INTO users (login, password, nickname, is_admin, is_banned, is_moderator, can_ban, can_chat) VALUES (%s, %s, %s, %s, 0, 0, 0, 0)", 
                       (login, hash_password, nickname, is_admin_val))
        conn.commit()
        conn.close()
        return redirect('/login')
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        login = request.form.get('login')
        password = request.form.get('password')
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute("SELECT * FROM users WHERE login = %s", (login,))
        user_data = cursor.fetchone()
        conn.close()

        # Сверяем хеш пароля
        if user_data and check_password_hash(user_data['password'], password):
            if user_data['is_banned'] == 1: return "ВЫ ЗАБАНЕНЫ!"

            session['user'] = login
            session['nickname'] = user_data['nickname']

            if user_data['is_admin'] == 1:
                session['is_admin'] = 1
                session['is_moderator'] = 1
                session['can_ban'] = 1
                session['can_chat'] = 1
            else:
                session['is_admin'] = 0
                session['is_moderator'] = user_data['is_moderator']
                session['can_ban'] = user_data['can_ban']
                session['can_chat'] = user_data['can_chat']

            return redirect('/')
        else: return "Неверно!"
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

# --- СОЗДАНИЕ С ЗАГРУЗКОЙ В CLOUDINARY ---
@app.route('/create', methods=['GET', 'POST'])
def create():
    if 'user' not in session: return "Сначала войдите!"

    if request.method == 'POST':
        if session.get('is_admin') != 1:
            conn = get_db_connection()
            cursor = conn.cursor()
            day_ago = time.time() - (24 * 60 * 60)
            cursor.execute("SELECT count(*) FROM items WHERE owner_login = %s AND created_at > %s", (session['user'], day_ago))
            count = cursor.fetchone()[0]
            conn.close()

            LIMIT = 3
            if count >= LIMIT:
                return f"<h1>🚫 Ошибка!</h1><p>Вы исчерпали лимит ({LIMIT} объявления в сутки).</p><a href='/'>На главную</a>"

        title = request.form.get('title')
        price = request.form.get('price')
        description = request.form.get('text')
        contact = request.form.get('contact')
        category = request.form.get('category')
        region = request.form.get('region')
        city = request.form.get('city')

        image_paths = []
        for i in range(1, 6):
            file = request.files.get(f'image{i}')
            if file and file.filename != '':
                try:
                    # ОТПРАВЛЯЕМ В ОБЛАКО CLOUDINARY
                    upload_result = cloudinary.uploader.upload(file)
                    image_paths.append(upload_result['secure_url'])
                except Exception as e:
                    print(f"Ошибка загрузки фото: {e}")
                    image_paths.append("")
            else: 
                image_paths.append("")

        if image_paths[0] == "": image_paths[0] = "https://placehold.co/400x300/EEE/31343C?text=Нет+фото"

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""INSERT INTO items (owner_login, owner_name, title, price, description, contact, category, region, city, image1, image2, image3, image4, image5, vip_expiry, views, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0, %s)""", 
            (session['user'], session['nickname'], title, price, description, contact, category, region, city, image_paths[0], image_paths[1], image_paths[2], image_paths[3], image_paths[4], time.time()))
        conn.commit()
        conn.close()
        return redirect('/')
    return render_template('create.html')

@app.route('/add_review/<int:item_id>', methods=['POST'])
def add_review(item_id):
    if 'user' not in session: return "Войдите!"
    text = request.form.get('text')
    stars = request.form.get('stars')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO reviews (item_id, author, text, stars, date) VALUES (%s, %s, %s, %s, %s)", (item_id, session.get('nickname'), text, stars, time.strftime("%d.%m.%Y")))
    conn.commit()
    conn.close()
    return redirect(f'/item/{item_id}')

@app.route('/delete/<int:item_id>')
def delete_item(item_id):
    if 'user' not in session: return "Вход не выполнен"
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT owner_login FROM items WHERE id = %s", (item_id,))
    item = cursor.fetchone()
    if item and (item['owner_login'] == session['user'] or session.get('is_admin') == 1 or session.get('can_ban') == 1):
        cursor.execute("DELETE FROM items WHERE id = %s", (item_id,))
        conn.commit()
    conn.close()
    return redirect('/')

@app.route('/admin')
def admin_panel():
    if session.get('is_admin') != 1 and session.get('is_moderator') != 1: return "Нет прав!"

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # 1. Берем всех пользователей
    cursor.execute("SELECT * FROM users ORDER BY is_admin DESC, is_moderator DESC")
    users = cursor.fetchall()

    # 2. СЧИТАЕМ СТАТИСТИКУ
    # Всего юзеров
    total_users = len(users)

    # Всего объявлений
    cursor.execute("SELECT count(*) as cnt FROM items")
    res_items = cursor.fetchone()
    total_items = res_items['cnt'] if res_items else 0

    # Общие просмотры
    cursor.execute("SELECT sum(views) as total_views FROM items")
    res_views = cursor.fetchone()['total_views']
    total_views = res_views if res_views else 0

    conn.close()

    # ИСПРАВЛЕНО ЗДЕСЬ: используем total_items вместо items
    return render_template('admin.html', users=users, stats={
        'users': total_users,
        'total_items': total_items, 
        'views': int(total_views)
    })

@app.route('/set_right/<user_login>/<right_name>/<int:value>')
def set_right(user_login, right_name, value):
    if session.get('is_admin') != 1: return "Только Босс может это делать!"
    if user_login == 'admin': return "Нельзя менять права босса!"
    allowed_rights = ['is_moderator', 'can_ban', 'can_chat']
    if right_name not in allowed_rights: return "Ошибка"

    conn = get_db_connection()
    cursor = conn.cursor()
    query = f"UPDATE users SET {right_name} = %s WHERE login = %s"
    cursor.execute(query, (value, user_login))
    conn.commit()
    conn.close()
    return redirect('/admin')

@app.route('/ban/<login_to_ban>')
def ban_user(login_to_ban):
    if (session.get('is_admin') != 1 and session.get('can_ban') != 1) or login_to_ban == 'admin': return "Нельзя!"
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_banned = 1 WHERE login = %s", (login_to_ban,))
    conn.commit()
    conn.close()
    return redirect('/admin')

@app.route('/unban/<login_to_unban>')
def unban_user(login_to_unban):
    if session.get('is_admin') != 1 and session.get('can_ban') != 1: return "Нельзя!"
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_banned = 0 WHERE login = %s", (login_to_unban,))
    conn.commit()
    conn.close()
    return redirect('/admin')

@app.route('/policy')
def policy():
    return render_template('policy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)