# 🛍️ WB Deals Bot — Бот мониторинга скидок Wildberries

Telegram-бот, который следит за ценами на WB и уведомляет пользователей,
когда товар дешевеет ниже указанной цены. Монетизация через CPA-партнёрку WB.

---

## 🚀 Пошаговый деплой на Render.com (бесплатно)

### Шаг 1 — Создай бота в Telegram

1. Открой @BotFather в Telegram
2. Отправь `/newbot`
3. Придумай имя, например: `WB Deals Bot`
4. Придумай username, например: `wb_deals_monitor_bot`
5. Скопируй **токен** — он выглядит как `123456789:AAFxxxxxxxx`

---

### Шаг 2 — Загрузи код на GitHub

1. Зарегистрируйся на [github.com](https://github.com)
2. Создай новый репозиторий (New repository), назови `wb-deals-bot`, **Public**
3. Загрузи все файлы проекта:

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/ТВО_ИМЯ/wb-deals-bot.git
git push -u origin main
```

Или через сайт GitHub — кнопка "uploading an existing file".

---

### Шаг 3 — Зарегистрируйся на Render.com

1. Зайди на [render.com](https://render.com)
2. Sign Up → через GitHub аккаунт
3. Подтверди email

---

### Шаг 4 — Создай сервис на Render

1. Нажми **New → Background Worker**
2. Выбери свой репозиторий `wb-deals-bot`
3. Настройки:
   - **Name**: wb-deals-bot
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python src/bot.py`
4. Нажми **Advanced** → **Add Disk**:
   - Name: `bot-data`
   - Mount Path: `/data`
   - Size: 1 GB (бесплатно)
5. Нажми **Create Background Worker**

---

### Шаг 5 — Добавь переменные окружения

В разделе **Environment** добавь:

| Key | Value |
|-----|-------|
| `BOT_TOKEN` | Токен от BotFather |
| `ADMIN_ID` | Твой Telegram ID (узнай у @userinfobot) |
| `DB_PATH` | `/data/bot.db` |
| `WB_AFFILIATE_ID` | (опционально, см. шаг 6) |

---

### Шаг 6 — Подключи партнёрскую программу WB (для заработка)

1. Зарегистрируйся на [affiliate.wildberries.ru](https://affiliate.wildberries.ru)
2. После одобрения получи свой **affiliate ID**
3. Добавь его в переменную `WB_AFFILIATE_ID` на Render

💰 Ты будешь получать **до 10% от каждой покупки**, совершённой по ссылке из бота.

---

### Шаг 7 — Запуск!

1. Render автоматически соберёт и запустит бота
2. Зайди в Telegram, найди своего бота
3. Отправь `/start`

---

## 📁 Структура проекта

```
wb-deals-bot/
├── src/
│   ├── bot.py          # Основной файл бота (хендлеры, меню)
│   ├── database.py     # SQLite база данных (пользователи, отслеживания)
│   ├── parser.py       # Парсер Wildberries API
│   └── scheduler.py    # Планировщик проверки цен (каждые 30 мин)
├── requirements.txt
├── render.yaml
└── README.md
```

---

## ⚙️ Функционал

| Функция | Описание |
|---------|----------|
| `/start` | Главное меню |
| Добавить товар | Указываешь запрос и макс. цену → бот следит |
| Мои отслеживания | Список активных, можно удалить |
| Горящие скидки | Случайная подборка товаров со скидкой 25%+ |
| `/stats` | Статистика (только для admin) |
| `/broadcast` | Рассылка всем пользователям (только admin) |

---

## 💰 Монетизация

1. **CPA WB** — % от покупок по партнёрской ссылке (пассивно)
2. **Премиум подписка** — добавь платёж через Telegram Stars или YooKassa
3. **Реклама** — при 1000+ пользователях размещай рекламу в боте

---

## 🔧 Локальный запуск (для разработки)

```bash
pip install -r requirements.txt
export BOT_TOKEN="твой_токен"
export ADMIN_ID="твой_telegram_id"
export DB_PATH="bot.db"
python src/bot.py
```

---

## 📈 Масштабирование

При росте аудитории можно:
- Перейти на PostgreSQL (заменить aiosqlite на asyncpg)
- Добавить Redis для кэша
- Разделить бота и шедулер на отдельные воркеры
- Добавить парсинг Ozon (аналогичный API)
