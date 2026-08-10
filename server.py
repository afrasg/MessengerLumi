import os
import sqlite3
import hashlib
import secrets
import shutil
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

import jwt

from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    HTTPException,
    Request,
    UploadFile,
    File,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# =========================================================
# CONFIG
# =========================================================

app = FastAPI(title="MyMessenger")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"

UPLOAD_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

DB = str(BASE_DIR / "messenger.db")

SECRET_KEY = os.environ.get(
    "SECRET_KEY",
    "CHANGE_THIS_TO_A_LONG_RANDOM_SECRET"
)

ALGORITHM = "HS256"

DEFAULT_AVATAR = "https://i.imgur.com/7NFHga3.png"


# =========================================================
# CORS
# =========================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# CONNECTIONS
# =========================================================

connections = defaultdict(list)


# =========================================================
# DATABASE
# =========================================================

def db():
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():

    connection = db()

    connection.executescript("""

    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT,
        bio TEXT DEFAULT '',
        avatar_url TEXT,
        language TEXT DEFAULT 'ru',
        theme TEXT DEFAULT 'dark',
        notifications INTEGER DEFAULT 1,
        privacy_last_seen TEXT DEFAULT 'everyone',
        created_at TEXT NOT NULL,
        last_seen TEXT
    );

    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL,
        receiver_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        edited_at TEXT,
        is_read INTEGER DEFAULT 0,
        is_deleted INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS favorites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        UNIQUE(user_id, message_id)
    );

    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        author_id INTEGER NOT NULL,
        text TEXT DEFAULT '',
        media_url TEXT,
        media_type TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS post_likes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        UNIQUE(post_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        parent_id INTEGER,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        description TEXT DEFAULT '',
        avatar_url TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS group_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        role TEXT DEFAULT 'member',
        UNIQUE(group_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        description TEXT DEFAULT '',
        avatar_url TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS channel_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        UNIQUE(channel_id, user_id)
    );

    """)

    connection.commit()

    # Миграции старых баз
    migrations = {
        "users": {
            "display_name": "TEXT",
            "bio": "TEXT DEFAULT ''",
            "avatar_url": "TEXT",
            "language": "TEXT DEFAULT 'ru'",
            "theme": "TEXT DEFAULT 'dark'",
            "notifications": "INTEGER DEFAULT 1",
            "privacy_last_seen": "TEXT DEFAULT 'everyone'",
        },
        "messages": {
            "edited_at": "TEXT",
            "is_deleted": "INTEGER DEFAULT 0",
        },
        "comments": {
            "parent_id": "INTEGER",
        }
    }

    for table, columns in migrations.items():

        existing = {
            row["name"]
            for row in connection.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }

        for column, definition in columns.items():

            if column not in existing:

                connection.execute(
                    f"ALTER TABLE {table} "
                    f"ADD COLUMN {column} {definition}"
                )

    connection.execute("""
        UPDATE users
        SET display_name = username
        WHERE display_name IS NULL
    """)

    connection.commit()
    connection.close()


init_db()


# =========================================================
# HELPERS
# =========================================================

def now():
    return datetime.utcnow().isoformat()


def hash_password(password):
    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


def create_token(user_id):

    payload = {
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(days=30)
    }

    return jwt.encode(
        payload,
        SECRET_KEY,
        algorithm=ALGORITHM
    )


def get_user_from_token(token):

    try:

        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )

        return int(payload["user_id"])

    except Exception:

        return None


def get_auth_user(request: Request):

    auth = request.headers.get(
        "Authorization",
        ""
    )

    if not auth.startswith("Bearer "):

        raise HTTPException(
            401,
            "Не авторизован"
        )

    user_id = get_user_from_token(
        auth[7:]
    )

    if not user_id:

        raise HTTPException(
            401,
            "Недействительный токен"
        )

    return user_id


def user_dict(user):

    if not user:
        return None

    data = dict(user)

    if not data.get("avatar_url"):
        data["avatar_url"] = DEFAULT_AVATAR

    return data


async def broadcast(user_id, data):

    dead = []

    for socket in list(
        connections.get(user_id, [])
    ):

        try:
            await socket.send_json(data)

        except Exception:
            dead.append(socket)

    for socket in dead:

        if socket in connections[user_id]:
            connections[user_id].remove(socket)


# =========================================================
# MODELS
# =========================================================

class AuthRequest(BaseModel):
    username: str
    password: str


class MessageRequest(BaseModel):
    receiver_id: int
    text: str


class EditMessageRequest(BaseModel):
    text: str


class ProfileRequest(BaseModel):
    username: str
    display_name: str
    bio: str = ""


class SettingsRequest(BaseModel):
    language: str = "ru"
    theme: str = "dark"
    notifications: bool = True
    privacy_last_seen: str = "everyone"


class PostRequest(BaseModel):
    text: str = ""


class CommentRequest(BaseModel):
    text: str
    parent_id: int | None = None


class GroupRequest(BaseModel):
    title: str
    description: str = ""


class ChannelRequest(BaseModel):
    title: str
    username: str
    description: str = ""


# =========================================================
# AUTH
# =========================================================

@app.post("/api/register")
def register(data: AuthRequest):

    username = data.username.strip().lower()

    if len(username) < 3:
        raise HTTPException(
            400,
            "Username должен содержать минимум 3 символа"
        )

    if len(username) > 30:
        raise HTTPException(
            400,
            "Username слишком длинный"
        )

    if not username.replace("_", "").isalnum():
        raise HTTPException(
            400,
            "Username может содержать буквы, цифры и _"
        )

    if len(data.password) < 6:
        raise HTTPException(
            400,
            "Пароль минимум 6 символов"
        )

    connection = db()

    exists = connection.execute(
        "SELECT id FROM users WHERE username = ?",
        (username,)
    ).fetchone()

    if exists:

        connection.close()

        raise HTTPException(
            400,
            "Такой username уже существует"
        )

    timestamp = now()

    cursor = connection.execute(
        """
        INSERT INTO users
        (
            username,
            password_hash,
            display_name,
            bio,
            avatar_url,
            created_at,
            last_seen
        )
        VALUES (?, ?, ?, '', ?, ?, ?)
        """,
        (
            username,
            hash_password(data.password),
            username,
            DEFAULT_AVATAR,
            timestamp,
            timestamp
        )
    )

    user_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "token": create_token(user_id),
        "user_id": user_id
    }


@app.post("/api/login")
def login(data: AuthRequest):

    username = data.username.strip().lower()

    connection = db()

    user = connection.execute(
        "SELECT * FROM users WHERE username = ?",
        (username,)
    ).fetchone()

    if not user:

        connection.close()

        raise HTTPException(
            401,
            "Неверный логин или пароль"
        )

    if user["password_hash"] != hash_password(
        data.password
    ):

        connection.close()

        raise HTTPException(
            401,
            "Неверный логин или пароль"
        )

    connection.execute(
        """
        UPDATE users
        SET last_seen = ?
        WHERE id = ?
        """,
        (now(), user["id"])
    )

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "token": create_token(user["id"]),
        "user_id": user["id"]
    }


# =========================================================
# ME
# =========================================================

@app.get("/api/me")
def me(request: Request):

    user_id = get_auth_user(request)

    connection = db()

    user = connection.execute(
        """
        SELECT
            id,
            username,
            display_name,
            bio,
            avatar_url,
            language,
            theme,
            notifications,
            privacy_last_seen,
            created_at,
            last_seen
        FROM users
        WHERE id = ?
        """,
        (user_id,)
    ).fetchone()

    connection.close()

    if not user:
        raise HTTPException(
            404,
            "Пользователь не найден"
        )

    return user_dict(user)


# =========================================================
# PROFILE
# =========================================================

@app.get("/api/users/{user_id}")
def get_profile(
    user_id: int,
    request: Request
):

    get_auth_user(request)

    connection = db()

    user = connection.execute(
        """
        SELECT
            id,
            username,
            display_name,
            bio,
            avatar_url,
            created_at,
            last_seen
        FROM users
        WHERE id = ?
        """,
        (user_id,)
    ).fetchone()

    connection.close()

    if not user:
        raise HTTPException(
            404,
            "Пользователь не найден"
        )

    return user_dict(user)


@app.put("/api/profile")
def update_profile(
    data: ProfileRequest,
    request: Request
):

    user_id = get_auth_user(request)

    username = data.username.strip().lower()
    display_name = data.display_name.strip()
    bio = data.bio.strip()

    if len(username) < 3:
        raise HTTPException(
            400,
            "Username слишком короткий"
        )

    if not username.replace("_", "").isalnum():
        raise HTTPException(
            400,
            "Username может содержать буквы, цифры и _"
        )

    if not display_name:
        display_name = username

    connection = db()

    exists = connection.execute(
        """
        SELECT id
        FROM users
        WHERE username = ?
        AND id != ?
        """,
        (username, user_id)
    ).fetchone()

    if exists:

        connection.close()

        raise HTTPException(
            400,
            "Username уже занят"
        )

    connection.execute(
        """
        UPDATE users
        SET
            username = ?,
            display_name = ?,
            bio = ?
        WHERE id = ?
        """,
        (
            username,
            display_name,
            bio,
            user_id
        )
    )

    connection.commit()
    connection.close()

    return {"ok": True}


@app.post("/api/avatar")
async def avatar(
    request: Request,
    file: UploadFile = File(...)
):

    user_id = get_auth_user(request)

    allowed = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp"
    }

    if file.content_type not in allowed:
        raise HTTPException(
            400,
            "Разрешены JPG, PNG и WEBP"
        )

    filename = (
        "avatar_"
        + str(user_id)
        + "_"
        + secrets.token_hex(8)
        + allowed[file.content_type]
    )

    path = UPLOAD_DIR / filename

    with open(path, "wb") as output:

        shutil.copyfileobj(
            file.file,
            output
        )

    url = "/uploads/" + filename

    connection = db()

    connection.execute(
        """
        UPDATE users
        SET avatar_url = ?
        WHERE id = ?
        """,
        (url, user_id)
    )

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "avatar_url": url
    }


# =========================================================
# SETTINGS
# =========================================================

@app.put("/api/settings")
def update_settings(
    data: SettingsRequest,
    request: Request
):

    user_id = get_auth_user(request)

    if data.language not in ["ru", "en"]:
        raise HTTPException(
            400,
            "Неподдерживаемый язык"
        )

    if data.theme not in [
        "dark",
        "light",
        "blue",
        "midnight"
    ]:
        raise HTTPException(
            400,
            "Неподдерживаемая тема"
        )

    if data.privacy_last_seen not in [
        "everyone",
        "nobody"
    ]:
        raise HTTPException(
            400,
            "Неверная настройка приватности"
        )

    connection = db()

    connection.execute(
        """
        UPDATE users
        SET
            language = ?,
            theme = ?,
            notifications = ?,
            privacy_last_seen = ?
        WHERE id = ?
        """,
        (
            data.language,
            data.theme,
            1 if data.notifications else 0,
            data.privacy_last_seen,
            user_id
        )
    )

    connection.commit()
    connection.close()

    return {"ok": True}


# =========================================================
# USERS SEARCH
# =========================================================

@app.get("/api/users")
def users(
    request: Request,
    q: str = ""
):

    current_id = get_auth_user(request)

    q = q.strip()

    connection = db()

    result = connection.execute(
        """
        SELECT
            id,
            username,
            display_name,
            avatar_url,
            last_seen
        FROM users
        WHERE
            id != ?
            AND (
                username LIKE ?
                OR display_name LIKE ?
            )
        ORDER BY username
        LIMIT 100
        """,
        (
            current_id,
            f"%{q}%",
            f"%{q}%"
        )
    ).fetchall()

    connection.close()

    return [
        user_dict(row)
        for row in result
    ]


# =========================================================
# MESSAGES
# =========================================================

@app.get("/api/messages/{other_id}")
def messages(
    other_id: int,
    request: Request
):

    user_id = get_auth_user(request)

    connection = db()

    rows = connection.execute(
        """
        SELECT *
        FROM messages
        WHERE
            (
                sender_id = ?
                AND receiver_id = ?
            )
            OR
            (
                sender_id = ?
                AND receiver_id = ?
            )
        ORDER BY id ASC
        """,
        (
            user_id,
            other_id,
            other_id,
            user_id
        )
    ).fetchall()

    connection.execute(
        """
        UPDATE messages
        SET is_read = 1
        WHERE
            sender_id = ?
            AND receiver_id = ?
        """,
        (other_id, user_id)
    )

    connection.commit()
    connection.close()

    return [dict(row) for row in rows]


@app.post("/api/messages")
async def send_message(
    data: MessageRequest,
    request: Request
):

    sender_id = get_auth_user(request)

    text = data.text.strip()

    if not text:
        raise HTTPException(
            400,
            "Пустое сообщение"
        )

    if len(text) > 5000:
        raise HTTPException(
            400,
            "Сообщение слишком длинное"
        )

    connection = db()

    receiver = connection.execute(
        "SELECT id FROM users WHERE id = ?",
        (data.receiver_id,)
    ).fetchone()

    if not receiver:

        connection.close()

        raise HTTPException(
            404,
            "Пользователь не найден"
        )

    timestamp = now()

    cursor = connection.execute(
        """
        INSERT INTO messages
        (
            sender_id,
            receiver_id,
            text,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            sender_id,
            data.receiver_id,
            text,
            timestamp
        )
    )

    message_id = cursor.lastrowid

    connection.commit()
    connection.close()

    message = {
        "id": message_id,
        "sender_id": sender_id,
        "receiver_id": data.receiver_id,
        "text": text,
        "created_at": timestamp,
        "edited_at": None,
        "is_read": 0,
        "is_deleted": 0
    }

    payload = {
        "type": "message",
        "message": message
    }

    await broadcast(
        sender_id,
        payload
    )

    await broadcast(
        data.receiver_id,
        payload
    )

    return {
        "ok": True,
        "message": message
    }


@app.put("/api/messages/{message_id}")
async def edit_message(
    message_id: int,
    data: EditMessageRequest,
    request: Request
):

    user_id = get_auth_user(request)

    text = data.text.strip()

    if not text:
        raise HTTPException(
            400,
            "Пустое сообщение"
        )

    connection = db()

    message = connection.execute(
        """
        SELECT *
        FROM messages
        WHERE id = ?
        """,
        (message_id,)
    ).fetchone()

    if not message:

        connection.close()

        raise HTTPException(
            404,
            "Сообщение не найдено"
        )

    if message["sender_id"] != user_id:

        connection.close()

        raise HTTPException(
            403,
            "Можно изменять только свои сообщения"
        )

    edited = now()

    connection.execute(
        """
        UPDATE messages
        SET text = ?, edited_at = ?
        WHERE id = ?
        """,
        (
            text,
            edited,
            message_id
        )
    )

    connection.commit()
    connection.close()

    updated = dict(message)

    updated["text"] = text
    updated["edited_at"] = edited

    payload = {
        "type": "message_edited",
        "message": updated
    }

    await broadcast(
        message["sender_id"],
        payload
    )

    await broadcast(
        message["receiver_id"],
        payload
    )

    return {
        "ok": True,
        "message": updated
    }


@app.delete("/api/messages/{message_id}")
async def delete_message(
    message_id: int,
    request: Request
):

    user_id = get_auth_user(request)

    connection = db()

    message = connection.execute(
        """
        SELECT *
        FROM messages
        WHERE id = ?
        """,
        (message_id,)
    ).fetchone()

    if not message:

        connection.close()

        raise HTTPException(
            404,
            "Сообщение не найдено"
        )

    if message["sender_id"] != user_id:

        connection.close()

        raise HTTPException(
            403,
            "Можно удалить только своё сообщение"
        )

    connection.execute(
        """
        UPDATE messages
        SET
            text = 'Сообщение удалено',
            is_deleted = 1
        WHERE id = ?
        """,
        (message_id,)
    )

    connection.commit()
    connection.close()

    payload = {
        "type": "message_deleted",
        "message_id": message_id
    }

    await broadcast(
        message["sender_id"],
        payload
    )

    await broadcast(
        message["receiver_id"],
        payload
    )

    return {"ok": True}


# =========================================================
# FAVORITES
# =========================================================

@app.get("/api/favorites")
def get_favorites(
    request: Request
):

    user_id = get_auth_user(request)

    connection = db()

    rows = connection.execute(
        """
        SELECT
            m.*,
            f.id AS favorite_id
        FROM favorites f
        JOIN messages m
            ON m.id = f.message_id
        WHERE f.user_id = ?
        ORDER BY f.id DESC
        """,
        (user_id,)
    ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


@app.post("/api/messages/{message_id}/favorite")
def favorite(
    message_id: int,
    request: Request
):

    user_id = get_auth_user(request)

    connection = db()

    existing = connection.execute(
        """
        SELECT id
        FROM favorites
        WHERE user_id = ?
        AND message_id = ?
        """,
        (user_id, message_id)
    ).fetchone()

    if existing:

        connection.execute(
            "DELETE FROM favorites WHERE id = ?",
            (existing["id"],)
        )

        saved = False

    else:

        connection.execute(
            """
            INSERT INTO favorites
            (user_id, message_id)
            VALUES (?, ?)
            """,
            (user_id, message_id)
        )

        saved = True

    connection.commit()
    connection.close()

    return {
        "saved": saved
    }


# =========================================================
# FEED
# =========================================================

@app.get("/api/feed")
def feed(request: Request):

    get_auth_user(request)

    connection = db()

    rows = connection.execute(
        """
        SELECT
            p.*,
            u.username,
            u.display_name,
            u.avatar_url,

            (
                SELECT COUNT(*)
                FROM post_likes
                WHERE post_id = p.id
            ) AS likes,

            (
                SELECT COUNT(*)
                FROM comments
                WHERE post_id = p.id
            ) AS comments

        FROM posts p

        JOIN users u
            ON u.id = p.author_id

        ORDER BY p.id DESC

        LIMIT 100
        """
    ).fetchall()

    connection.close()

    result = []

    for row in rows:

        item = dict(row)

        if not item["avatar_url"]:
            item["avatar_url"] = DEFAULT_AVATAR

        result.append(item)

    return result


@app.post("/api/posts")
def create_post(
    data: PostRequest,
    request: Request
):

    user_id = get_auth_user(request)

    text = data.text.strip()

    if not text:
        raise HTTPException(
            400,
            "Пустой пост"
        )

    connection = db()

    cursor = connection.execute(
        """
        INSERT INTO posts
        (
            author_id,
            text,
            created_at
        )
        VALUES (?, ?, ?)
        """,
        (
            user_id,
            text,
            now()
        )
    )

    post_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "id": post_id
    }


@app.post("/api/posts/media")
async def media_post(
    request: Request,
    text: str = "",
    file: UploadFile = File(...)
):

    user_id = get_auth_user(request)

    allowed = {
        "image/jpeg": (".jpg", "image"),
        "image/png": (".png", "image"),
        "image/webp": (".webp", "image"),
        "video/mp4": (".mp4", "video"),
        "video/webm": (".webm", "video"),
        "video/quicktime": (".mov", "video")
    }

    if file.content_type not in allowed:
        raise HTTPException(
            400,
            "Формат не поддерживается"
        )

    extension, media_type = allowed[
        file.content_type
    ]

    filename = (
        "post_"
        + secrets.token_hex(12)
        + extension
    )

    path = UPLOAD_DIR / filename

    with open(path, "wb") as output:
        shutil.copyfileobj(
            file.file,
            output
        )

    url = "/uploads/" + filename

    connection = db()

    cursor = connection.execute(
        """
        INSERT INTO posts
        (
            author_id,
            text,
            media_url,
            media_type,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            user_id,
            text.strip(),
            url,
            media_type,
            now()
        )
    )

    post_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "id": post_id
    }


@app.post("/api/posts/{post_id}/like")
def like_post(
    post_id: int,
    request: Request
):

    user_id = get_auth_user(request)

    connection = db()

    exists = connection.execute(
        """
        SELECT id
        FROM post_likes
        WHERE post_id = ?
        AND user_id = ?
        """,
        (post_id, user_id)
    ).fetchone()

    if exists:

        connection.execute(
            "DELETE FROM post_likes WHERE id = ?",
            (exists["id"],)
        )

        liked = False

    else:

        connection.execute(
            """
            INSERT INTO post_likes
            (post_id, user_id)
            VALUES (?, ?)
            """,
            (post_id, user_id)
        )

        liked = True

    connection.commit()

    count = connection.execute(
        """
        SELECT COUNT(*)
        FROM post_likes
        WHERE post_id = ?
        """,
        (post_id,)
    ).fetchone()[0]

    connection.close()

    return {
        "liked": liked,
        "likes": count
    }


@app.delete("/api/posts/{post_id}")
def delete_post(
    post_id: int,
    request: Request
):

    user_id = get_auth_user(request)

    connection = db()

    post = connection.execute(
        "SELECT * FROM posts WHERE id = ?",
        (post_id,)
    ).fetchone()

    if not post:

        connection.close()

        raise HTTPException(
            404,
            "Пост не найден"
        )

    if post["author_id"] != user_id:

        connection.close()

        raise HTTPException(
            403,
            "Это не твой пост"
        )

    connection.execute(
        "DELETE FROM comments WHERE post_id = ?",
        (post_id,)
    )

    connection.execute(
        "DELETE FROM post_likes WHERE post_id = ?",
        (post_id,)
    )

    connection.execute(
        "DELETE FROM posts WHERE id = ?",
        (post_id,)
    )

    connection.commit()
    connection.close()

    return {"ok": True}


# =========================================================
# COMMENTS + REPLIES
# =========================================================

@app.get("/api/posts/{post_id}/comments")
def comments(
    post_id: int,
    request: Request
):

    get_auth_user(request)

    connection = db()

    rows = connection.execute(
        """
        SELECT
            c.*,
            u.username,
            u.display_name,
            u.avatar_url,
            parent_user.username AS reply_to_username

        FROM comments c

        JOIN users u
            ON u.id = c.user_id

        LEFT JOIN comments parent_comment
            ON parent_comment.id = c.parent_id

        LEFT JOIN users parent_user
            ON parent_user.id = parent_comment.user_id

        WHERE c.post_id = ?

        ORDER BY c.id ASC
        """,
        (post_id,)
    ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


@app.post("/api/posts/{post_id}/comments")
def create_comment(
    post_id: int,
    data: CommentRequest,
    request: Request
):

    user_id = get_auth_user(request)

    text = data.text.strip()

    if not text:
        raise HTTPException(
            400,
            "Пустой комментарий"
        )

    connection = db()

    post = connection.execute(
        "SELECT id FROM posts WHERE id = ?",
        (post_id,)
    ).fetchone()

    if not post:

        connection.close()

        raise HTTPException(
            404,
            "Пост не найден"
        )

    if data.parent_id:

        parent = connection.execute(
            """
            SELECT id
            FROM comments
            WHERE id = ?
            AND post_id = ?
            """,
            (
                data.parent_id,
                post_id
            )
        ).fetchone()

        if not parent:

            connection.close()

            raise HTTPException(
                400,
                "Комментарий для ответа не найден"
            )

    cursor = connection.execute(
        """
        INSERT INTO comments
        (
            post_id,
            user_id,
            parent_id,
            text,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            post_id,
            user_id,
            data.parent_id,
            text,
            now()
        )
    )

    comment_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "id": comment_id
    }


@app.delete("/api/comments/{comment_id}")
def delete_comment(
    comment_id: int,
    request: Request
):

    user_id = get_auth_user(request)

    connection = db()

    comment = connection.execute(
        "SELECT * FROM comments WHERE id = ?",
        (comment_id,)
    ).fetchone()

    if not comment:

        connection.close()

        raise HTTPException(
            404,
            "Комментарий не найден"
        )

    if comment["user_id"] != user_id:

        connection.close()

        raise HTTPException(
            403,
            "Можно удалить только свой комментарий"
        )

    connection.execute(
        """
        DELETE FROM comments
        WHERE id = ?
        OR parent_id = ?
        """,
        (
            comment_id,
            comment_id
        )
    )

    connection.commit()
    connection.close()

    return {"ok": True}


# =========================================================
# GROUPS
# =========================================================

@app.post("/api/groups")
def create_group(
    data: GroupRequest,
    request: Request
):

    user_id = get_auth_user(request)

    title = data.title.strip()

    if not title:
        raise HTTPException(
            400,
            "Название группы обязательно"
        )

    connection = db()

    cursor = connection.execute(
        """
        INSERT INTO groups
        (
            owner_id,
            title,
            description,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            title,
            data.description.strip(),
            now()
        )
    )

    group_id = cursor.lastrowid

    connection.execute(
        """
        INSERT INTO group_members
        (group_id, user_id, role)
        VALUES (?, ?, 'owner')
        """,
        (
            group_id,
            user_id
        )
    )

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "id": group_id
    }


@app.get("/api/groups")
def get_groups(request: Request):

    user_id = get_auth_user(request)

    connection = db()

    rows = connection.execute(
        """
        SELECT
            g.*
        FROM groups g

        JOIN group_members gm
            ON gm.group_id = g.id

        WHERE gm.user_id = ?

        ORDER BY g.id DESC
        """,
        (user_id,)
    ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


# =========================================================
# CHANNELS
# =========================================================

@app.post("/api/channels")
def create_channel(
    data: ChannelRequest,
    request: Request
):

    user_id = get_auth_user(request)

    title = data.title.strip()
    username = data.username.strip().lower().lstrip("@")

    if len(username) < 3:
        raise HTTPException(
            400,
            "Username канала слишком короткий"
        )

    if not username.replace("_", "").isalnum():
        raise HTTPException(
            400,
            "Username может содержать буквы, цифры и _"
        )

    connection = db()

    exists = connection.execute(
        """
        SELECT id
        FROM channels
        WHERE username = ?
        """,
        (username,)
    ).fetchone()

    if exists:

        connection.close()

        raise HTTPException(
            400,
            "Такой username канала уже занят"
        )

    cursor = connection.execute(
        """
        INSERT INTO channels
        (
            owner_id,
            title,
            username,
            description,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            user_id,
            title,
            username,
            data.description.strip(),
            now()
        )
    )

    channel_id = cursor.lastrowid

    connection.execute(
        """
        INSERT INTO channel_members
        (channel_id, user_id)
        VALUES (?, ?)
        """,
        (
            channel_id,
            user_id
        )
    )

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "id": channel_id
    }


@app.get("/api/channels")
def get_channels(request: Request):

    user_id = get_auth_user(request)

    connection = db()

    rows = connection.execute(
        """
        SELECT
            c.*
        FROM channels c

        JOIN channel_members cm
            ON cm.channel_id = c.id

        WHERE cm.user_id = ?

        ORDER BY c.id DESC
        """,
        (user_id,)
    ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


# =========================================================
# WEBSOCKET
# =========================================================

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    user_id: int
):

    await websocket.accept()

    connections[user_id].append(
        websocket
    )

    try:

        while True:

            data = await websocket.receive_json()

            if data.get("type") == "ping":

                await websocket.send_json({
                    "type": "pong"
                })

    except WebSocketDisconnect:

        if websocket in connections[user_id]:
            connections[user_id].remove(
                websocket
            )

        if not connections[user_id]:
            connections.pop(
                user_id,
                None
            )


# =========================================================
# STATIC
# =========================================================

app.mount(
    "/uploads",
    StaticFiles(
        directory=str(UPLOAD_DIR)
    ),
    name="uploads"
)

app.mount(
    "/static",
    StaticFiles(
        directory=str(STATIC_DIR)
    ),
    name="static"
)


@app.get("/")
def index():

    return FileResponse(
        str(STATIC_DIR / "index.html")
    )
