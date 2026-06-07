import aiosqlite
import os
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "bot.db")


class Database:
    def __init__(self):
        self.db_path = DB_PATH

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS watches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    query TEXT NOT NULL,
                    max_price INTEGER NOT NULL,
                    active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    watch_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_notifications_lookup
                ON notifications(watch_id, item_id)
            """)
            await db.commit()
        logger.info("Database initialized")

    async def add_user(self, user_id: int, username: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (id, username) VALUES (?, ?)",
                (user_id, username)
            )
            await db.commit()

    async def get_all_users(self) -> list[int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT id FROM users") as cursor:
                rows = await cursor.fetchall()
                return [r[0] for r in rows]

    async def add_watch(self, user_id: int, query: str, max_price: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO watches (user_id, query, max_price) VALUES (?, ?, ?)",
                (user_id, query, max_price)
            )
            await db.commit()

    async def get_watches(self, user_id: int) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, query, max_price FROM watches WHERE user_id=? AND active=1",
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_watches_count(self, user_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM watches WHERE user_id=? AND active=1",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0]

    async def delete_watch(self, user_id: int, watch_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE watches SET active=0 WHERE id=? AND user_id=?",
                (watch_id, user_id)
            )
            await db.commit()

    async def get_all_active_watches(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, user_id, query, max_price FROM watches WHERE active=1"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def was_notified(self, watch_id: int, item_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM notifications WHERE watch_id=? AND item_id=?",
                (watch_id, item_id)
            ) as cursor:
                return await cursor.fetchone() is not None

    async def mark_notified(self, user_id: int, watch_id: int, item_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO notifications (user_id, watch_id, item_id) VALUES (?,?,?)",
                (user_id, watch_id, item_id)
            )
            await db.commit()

    async def get_stats(self) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as c:
                users = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM watches WHERE active=1") as c:
                watches = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM notifications") as c:
                notifications = (await c.fetchone())[0]
        return {"users": users, "watches": watches, "notifications": notifications}
