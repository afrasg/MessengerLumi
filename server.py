import os
import sqlite3
import hashlib
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

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
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = BASE_DIR / "messenger.db"

STATIC_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

SECRET_KEY = os.environ.get(
    "SECRET_KEY",
    "CHANGE_THIS_SECRET_KEY_TO_SOMETHING_RANDOM"
)

ALGORITHM = "HS256"


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
# WEBSOCKET CONNECTIONS
# =========================================================

connections = defaultdict(set)


# =========================================================
# DATABASE
# =========================================================

def db():
    connection = sqlite3.connect(
        str(DB_PATH),
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    return connection


def column_exists(connection, table, column):
    rows = connection.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()

    return any(
        row["name"] == column
        for row in rows
    )


def add_column_if_missing(
    connection,
    table,
    column,
    definition,
):
    if not column_exists(
        connection,
        table,
        column,
    ):
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def init_db():

    connection = db()

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
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
            deleted INTEGER DEFAULT 0,
            is_read INTEGER DEFAULT 0
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

        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            UNIQUE(user_id, message_id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY,
            language TEXT DEFAULT 'ru',
            theme TEXT DEFAULT 'dark',
            notifications INTEGER DEFAULT 1,
            show_online INTEGER DEFAULT 1,
            show_last_seen INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS group_members (
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TEXT NOT NULL,
            UNIQUE(group_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT '',
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS channel_subscribers (
            channel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(channel_id, user_id)
        );
        """
    )

    # Старые базы автоматически получают новые поля.

    add_column_if_missing(
        connection,
        "users",
        "display_name",
        "TEXT",
    )

    add_column_if_missing(
        connection,
        "users",
        "bio",
        "TEXT DEFAULT ''",
    )

    add_column_if_missing(
        connection,
        "users",
        "avatar_url",
        "TEXT",
    )

    add_column_if_missing(
        connection,
        "messages",
        "edited_at",
        "TEXT",
    )

    add_column_if_missing(
        connection,
        "messages",
        "deleted",
        "INTEGER DEFAULT 0",
    )

    add_column_if_missing(
        connection,
        "comments",
        "parent_id",
        "INTEGER",
    )

    connection.execute(
        """
        UPDATE users
        SET display_name = username
        WHERE display_name IS NULL
           OR display_name = ''
        """
    )

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
        "exp": datetime.utcnow()
        + timedelta(days=30),
    }

    return jwt.encode(
        payload,
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def get_user_from_token(token):

    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
        )

        return int(
            payload["user_id"]
        )

    except Exception:
        return None


def get_auth_user(request: Request):

    auth = request.headers.get(
        "Authorization",
        "",
    )

    if not auth.startswith("Bearer "):
        raise HTTPException(
            401,
            "Не авторизован",
        )

    user_id = get_user_from_token(
        auth[7:]
    )

    if not user_id:
        raise HTTPException(
            401,
            "Недействительный токен",
        )

    return user_id


def user_dict(row):

    if not row:
        return None

    return dict(row)


async def send_ws(user_id, payload):

    dead = []

    for socket in list(
        connections.get(user_id, set())
    ):
        try:
            await socket.send_json(payload)
        except Exception:
            dead.append(socket)

    for socket in dead:
        connections[user_id].discard(socket)


# =========================================================
# MODELS
# =========================================================

class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
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


class PostRequest(BaseModel):
    text: str = ""


class CommentRequest(BaseModel):
    text: str
    parent_id: int | None = None


class SettingsRequest(BaseModel):
    language: str = "ru"
    theme: str = "dark"
    notifications: bool = True
    show_online: bool = True
    show_last_seen: bool = True


class GroupRequest(BaseModel):
    name: str
    description: str = ""


class ChannelRequest(BaseModel):
    name: str
    username: str
    description: str = ""


# =========================================================
# AUTH
# =========================================================

@app.post("/api/register")
def register(data: RegisterRequest):

    username = data.username.strip().lower()

    if len(username) < 3:
        raise HTTPException(
            400,
            "Username должен содержать минимум 3 символа",
        )

    if len(username) > 30:
        raise HTTPException(
            400,
            "Username слишком длинный",
        )

    if not username.replace("_", "").isalnum():
        raise HTTPException(
            400,
            "Username может содержать буквы, цифры и _",
        )

    if len(data.password) < 6:
        raise HTTPException(
            400,
            "Пароль должен содержать минимум 6 символов",
        )

    connection = db()

    exists = connection.execute(
        """
        SELECT id
        FROM users
        WHERE username = ?
        """,
        (username,),
    ).fetchone()

    if exists:
        connection.close()
        raise HTTPException(
            400,
            "Такой username уже занят",
        )

    created = now()

    cursor = connection.execute(
        """
        INSERT INTO users
        (
            username,
            password_hash,
            created_at,
            last_seen,
            display_name
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            username,
            hash_password(data.password),
            created,
            created,
            username,
        ),
    )

    user_id = cursor.lastrowid

    connection.execute(
        """
        INSERT OR IGNORE INTO settings
        (
            user_id
        )
        VALUES (?)
        """,
        (user_id,),
    )

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "token": create_token(user_id),
        "user": {
            "id": user_id,
            "username": username,
            "display_name": username,
        },
    }


@app.post("/api/login")
def login(data: LoginRequest):

    username = data.username.strip().lower()

    connection = db()

    user = connection.execute(
        """
        SELECT *
        FROM users
        WHERE username = ?
        """,
        (username,),
    ).fetchone()

    if not user:
        connection.close()
        raise HTTPException(
            401,
            "Неверный логин или пароль",
        )

    if user["password_hash"] != hash_password(
        data.password
    ):
        connection.close()
        raise HTTPException(
            401,
            "Неверный логин или пароль",
        )

    connection.execute(
        """
        UPDATE users
        SET last_seen = ?
        WHERE id = ?
        """,
        (
            now(),
            user["id"],
        ),
    )

    connection.execute(
        """
        INSERT OR IGNORE INTO settings
        (user_id)
        VALUES (?)
        """,
        (user["id"],),
    )

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "token": create_token(user["id"]),
    }


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
            created_at,
            last_seen
        FROM users
        WHERE id = ?
        """,
        (user_id,),
    ).fetchone()

    connection.close()

    if not user:
        raise HTTPException(
            404,
            "Пользователь не найден",
        )

    return dict(user)


# =========================================================
# PROFILE
# =========================================================

@app.put("/api/profile")
def update_profile(
    data: ProfileRequest,
    request: Request,
):

    user_id = get_auth_user(request)

    username = data.username.strip().lower()
    display_name = data.display_name.strip()
    bio = data.bio.strip()

    if len(username) < 3:
        raise HTTPException(
            400,
            "Username слишком короткий",
        )

    if not username.replace("_", "").isalnum():
        raise HTTPException(
            400,
            "Username может содержать буквы, цифры и _",
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
        (
            username,
            user_id,
        ),
    ).fetchone()

    if exists:
        connection.close()
        raise HTTPException(
            400,
            "Этот username уже занят",
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
            user_id,
        ),
    )

    connection.commit()
    connection.close()

    return {
        "ok": True,
    }


@app.post("/api/avatar")
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
):

    user_id = get_auth_user(request)

    allowed = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }

    if file.content_type not in allowed:
        raise HTTPException(
            400,
            "Разрешены JPG, PNG и WEBP",
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
            output,
        )

    url = "/uploads/" + filename

    connection = db()

    connection.execute(
        """
        UPDATE users
        SET avatar_url = ?
        WHERE id = ?
        """,
        (
            url,
            user_id,
        ),
    )

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "avatar_url": url,
    }


# =========================================================
# USERS
# =========================================================

@app.get("/api/users")
def search_users(
    request: Request,
    q: str = "",
):

    user_id = get_auth_user(request)

    q = q.strip()

    connection = db()

    users = connection.execute(
        """
        SELECT
            id,
            username,
            display_name,
            avatar_url,
            last_seen
        FROM users
        WHERE
            (
                username LIKE ?
                OR display_name LIKE ?
            )
            AND id != ?
        ORDER BY username
        LIMIT 50
        """,
        (
            "%" + q + "%",
            "%" + q + "%",
            user_id,
        ),
    ).fetchall()

    connection.close()

    return [
        dict(user)
        for user in users
    ]


@app.get("/api/users/{user_id}")
def get_user_profile(
    user_id: int,
    request: Request,
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
        (user_id,),
    ).fetchone()

    connection.close()

    if not user:
        raise HTTPException(
            404,
            "Пользователь не найден",
        )

    return dict(user)


# =========================================================
# MESSAGES
# =========================================================

@app.get("/api/messages/{other_user_id}")
def get_messages(
    other_user_id: int,
    request: Request,
):

    user_id = get_auth_user(request)

    connection = db()

    messages = connection.execute(
        """
        SELECT
            id,
            sender_id,
            receiver_id,
            CASE
                WHEN deleted = 1
                THEN ''
                ELSE text
            END AS text,
            created_at,
            edited_at,
            deleted,
            is_read
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
            other_user_id,
            other_user_id,
            user_id,
        ),
    ).fetchall()

    connection.execute(
        """
        UPDATE messages
        SET is_read = 1
        WHERE
            sender_id = ?
            AND receiver_id = ?
        """,
        (
            other_user_id,
            user_id,
        ),
    )

    connection.commit()
    connection.close()

    return [
        dict(message)
        for message in messages
    ]


@app.post("/api/messages")
async def send_message(
    data: MessageRequest,
    request: Request,
):

    sender_id = get_auth_user(request)

    text = data.text.strip()

    if not text:
        raise HTTPException(
            400,
            "Пустое сообщение",
        )

    if len(text) > 5000:
        raise HTTPException(
            400,
            "Сообщение слишком длинное",
        )

    if sender_id == data.receiver_id:
        raise HTTPException(
            400,
            "Нельзя отправить сообщение самому себе",
        )

    connection = db()

    receiver = connection.execute(
        """
        SELECT id
        FROM users
        WHERE id = ?
        """,
        (data.receiver_id,),
    ).fetchone()

    if not receiver:
        connection.close()
        raise HTTPException(
            404,
            "Пользователь не найден",
        )

    created = now()

    # ВАЖНО:
    # Здесь ровно 4 колонки и ровно 4 значения.
    # Именно это исправляет:
    # sqlite3.OperationalError:
    # 5 values for 4 columns

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
            created,
        ),
    )

    message_id = cursor.lastrowid

    connection.commit()
    connection.close()

    message = {
        "id": message_id,
        "sender_id": sender_id,
        "receiver_id": data.receiver_id,
        "text": text,
        "created_at": created,
        "edited_at": None,
        "deleted": 0,
        "is_read": 0,
    }

    payload = {
        "type": "message",
        "message": message,
    }

    await send_ws(
        data.receiver_id,
        payload,
    )

    await send_ws(
        sender_id,
        payload,
    )

    return {
        "ok": True,
        "message": message,
    }


@app.put("/api/messages/{message_id}")
async def edit_message(
    message_id: int,
    data: EditMessageRequest,
    request: Request,
):

    user_id = get_auth_user(request)

    text = data.text.strip()

    if not text:
        raise HTTPException(
            400,
            "Пустое сообщение",
        )

    connection = db()

    message = connection.execute(
        """
        SELECT *
        FROM messages
        WHERE id = ?
        """,
        (message_id,),
    ).fetchone()

    if not message:
        connection.close()
        raise HTTPException(
            404,
            "Сообщение не найдено",
        )

    if message["sender_id"] != user_id:
        connection.close()
        raise HTTPException(
            403,
            "Можно изменять только свои сообщения",
        )

    if message["deleted"]:
        connection.close()
        raise HTTPException(
            400,
            "Сообщение уже удалено",
        )

    edited = now()

    connection.execute(
        """
        UPDATE messages
        SET
            text = ?,
            edited_at = ?
        WHERE id = ?
        """,
        (
            text,
            edited,
            message_id,
        ),
    )

    connection.commit()
    connection.close()

    updated = {
        "id": message_id,
        "sender_id": message["sender_id"],
        "receiver_id": message["receiver_id"],
        "text": text,
        "created_at": message["created_at"],
        "edited_at": edited,
        "deleted": 0,
    }

    payload = {
        "type": "message_updated",
        "message": updated,
    }

    await send_ws(
        message["sender_id"],
        payload,
    )

    await send_ws(
        message["receiver_id"],
        payload,
    )

    return {
        "ok": True,
        "message": updated,
    }


@app.delete("/api/messages/{message_id}")
async def delete_message(
    message_id: int,
    request: Request,
):

    user_id = get_auth_user(request)

    connection = db()

    message = connection.execute(
        """
        SELECT *
        FROM messages
        WHERE id = ?
        """,
        (message_id,),
    ).fetchone()

    if not message:
        connection.close()
        raise HTTPException(
            404,
            "Сообщение не найдено",
        )

    if message["sender_id"] != user_id:
        connection.close()
        raise HTTPException(
            403,
            "Можно удалять только свои сообщения",
        )

    connection.execute(
        """
        UPDATE messages
        SET
            deleted = 1,
            text = ''
        WHERE id = ?
        """,
        (message_id,),
    )

    connection.commit()
    connection.close()

    deleted_message = {
        "id": message_id,
        "sender_id": message["sender_id"],
        "receiver_id": message["receiver_id"],
        "deleted": 1,
        "text": "",
    }

    payload = {
        "type": "message_deleted",
        "message": deleted_message,
    }

    await send_ws(
        message["sender_id"],
        payload,
    )

    await send_ws(
        message["receiver_id"],
        payload,
    )

    return {
        "ok": True,
    }


# =========================================================
# FAVORITES
# =========================================================

@app.post("/api/messages/{message_id}/favorite")
def favorite_message(
    message_id: int,
    request: Request,
):

    user_id = get_auth_user(request)

    connection = db()

    message = connection.execute(
        """
        SELECT id
        FROM messages
        WHERE id = ?
          AND (
              sender_id = ?
              OR receiver_id = ?
          )
        """,
        (
            message_id,
            user_id,
            user_id,
        ),
    ).fetchone()

    if not message:
        connection.close()
        raise HTTPException(
            404,
            "Сообщение не найдено",
        )

    existing = connection.execute(
        """
        SELECT id
        FROM favorites
        WHERE user_id = ?
          AND message_id = ?
        """,
        (
            user_id,
            message_id,
        ),
    ).fetchone()

    if existing:

        connection.execute(
            """
            DELETE FROM favorites
            WHERE user_id = ?
              AND message_id = ?
            """,
            (
                user_id,
                message_id,
            ),
        )

        favorite = False

    else:

        connection.execute(
            """
            INSERT INTO favorites
            (
                user_id,
                message_id
            )
            VALUES (?, ?)
            """,
            (
                user_id,
                message_id,
            ),
        )

        favorite = True

    connection.commit()
    connection.close()

    return {
        "favorite": favorite,
    }


@app.get("/api/favorites")
def get_favorites(
    request: Request,
):

    user_id = get_auth_user(request)

    connection = db()

    rows = connection.execute(
        """
        SELECT
            m.id,
            m.sender_id,
            m.receiver_id,
            m.text,
            m.created_at,
            m.edited_at,
            m.deleted
        FROM favorites f
        JOIN messages m
            ON m.id = f.message_id
        WHERE f.user_id = ?
        ORDER BY f.id DESC
        """,
        (user_id,),
    ).fetchall()

    connection.close()

    return [
        dict(row)
        for row in rows
    ]


# =========================================================
# FEED
# =========================================================

@app.get("/api/feed")
def feed(request: Request):

    get_auth_user(request)

    connection = db()

    posts = connection.execute(
        """
        SELECT
            p.id,
            p.author_id,
            p.text,
            p.media_url,
            p.media_type,
            p.created_at,
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

    return [
        dict(post)
        for post in posts
    ]


@app.post("/api/posts")
def create_post(
    data: PostRequest,
    request: Request,
):

    user_id = get_auth_user(request)

    text = data.text.strip()

    if not text:
        raise HTTPException(
            400,
            "Напиши текст поста",
        )

    if len(text) > 5000:
        raise HTTPException(
            400,
            "Пост слишком длинный",
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
            now(),
        ),
    )

    post_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "id": post_id,
    }


@app.post("/api/posts/media")
async def create_media_post(
    request: Request,
    text: str = "",
    file: UploadFile = File(...),
):

    user_id = get_auth_user(request)

    allowed = {
        "image/jpeg": (".jpg", "image"),
        "image/png": (".png", "image"),
        "image/webp": (".webp", "image"),
        "video/mp4": (".mp4", "video"),
        "video/webm": (".webm", "video"),
        "video/quicktime": (".mov", "video"),
    }

    if file.content_type not in allowed:
        raise HTTPException(
            400,
            "Формат файла не поддерживается",
        )

    extension, media_type = allowed[
        file.content_type
    ]

    filename = (
        "post_"
        + str(user_id)
        + "_"
        + secrets.token_hex(10)
        + extension
    )

    path = UPLOAD_DIR / filename

    with open(path, "wb") as output:
        shutil.copyfileobj(
            file.file,
            output,
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
            now(),
        ),
    )

    post_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "id": post_id,
        "media_url": url,
    }


@app.post("/api/posts/{post_id}/like")
def like_post(
    post_id: int,
    request: Request,
):

    user_id = get_auth_user(request)

    connection = db()

    existing = connection.execute(
        """
        SELECT id
        FROM post_likes
        WHERE post_id = ?
          AND user_id = ?
        """,
        (
            post_id,
            user_id,
        ),
    ).fetchone()

    if existing:

        connection.execute(
            """
            DELETE FROM post_likes
            WHERE post_id = ?
              AND user_id = ?
            """,
            (
                post_id,
                user_id,
            ),
        )

        liked = False

    else:

        connection.execute(
            """
            INSERT INTO post_likes
            (
                post_id,
                user_id
            )
            VALUES (?, ?)
            """,
            (
                post_id,
                user_id,
            ),
        )

        liked = True

    count = connection.execute(
        """
        SELECT COUNT(*)
        FROM post_likes
        WHERE post_id = ?
        """,
        (post_id,),
    ).fetchone()[0]

    connection.commit()
    connection.close()

    return {
        "liked": liked,
        "likes": count,
    }


# =========================================================
# COMMENTS + REPLIES
# =========================================================

@app.get("/api/posts/{post_id}/comments")
def get_comments(
    post_id: int,
    request: Request,
):

    get_auth_user(request)

    connection = db()

    comments = connection.execute(
        """
        SELECT
            c.id,
            c.post_id,
            c.user_id,
            c.parent_id,
            c.text,
            c.created_at,
            u.username,
            u.display_name,
            u.avatar_url,
            puser.username AS reply_to_username
        FROM comments c
        JOIN users u
            ON u.id = c.user_id
        LEFT JOIN comments parent
            ON parent.id = c.parent_id
        LEFT JOIN users puser
            ON puser.id = parent.user_id
        WHERE c.post_id = ?
        ORDER BY c.id ASC
        """,
        (post_id,),
    ).fetchall()

    connection.close()

    return [
        dict(comment)
        for comment in comments
    ]


@app.post("/api/posts/{post_id}/comments")
def create_comment(
    post_id: int,
    data: CommentRequest,
    request: Request,
):

    user_id = get_auth_user(request)

    text = data.text.strip()

    if not text:
        raise HTTPException(
            400,
            "Пустой комментарий",
        )

    connection = db()

    post = connection.execute(
        """
        SELECT id
        FROM posts
        WHERE id = ?
        """,
        (post_id,),
    ).fetchone()

    if not post:
        connection.close()
        raise HTTPException(
            404,
            "Пост не найден",
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
                post_id,
            ),
        ).fetchone()

        if not parent:
            connection.close()
            raise HTTPException(
                400,
                "Комментарий для ответа не найден",
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
            now(),
        ),
    )

    comment_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "id": comment_id,
    }


@app.delete("/api/comments/{comment_id}")
def delete_comment(
    comment_id: int,
    request: Request,
):

    user_id = get_auth_user(request)

    connection = db()

    comment = connection.execute(
        """
        SELECT *
        FROM comments
        WHERE id = ?
        """,
        (comment_id,),
    ).fetchone()

    if not comment:
        connection.close()
        raise HTTPException(
            404,
            "Комментарий не найден",
        )

    if comment["user_id"] != user_id:
        connection.close()
        raise HTTPException(
            403,
            "Можно удалять только свои комментарии",
        )

    connection.execute(
        """
        DELETE FROM comments
        WHERE id = ?
           OR parent_id = ?
        """,
        (
            comment_id,
            comment_id,
        ),
    )

    connection.commit()
    connection.close()

    return {
        "ok": True,
    }


# =========================================================
# DELETE POST
# =========================================================

@app.delete("/api/posts/{post_id}")
def delete_post(
    post_id: int,
    request: Request,
):

    user_id = get_auth_user(request)

    connection = db()

    post = connection.execute(
        """
        SELECT *
        FROM posts
        WHERE id = ?
        """,
        (post_id,),
    ).fetchone()

    if not post:
        connection.close()
        raise HTTPException(
            404,
            "Пост не найден",
        )

    if post["author_id"] != user_id:
        connection.close()
        raise HTTPException(
            403,
            "Это не твой пост",
        )

    connection.execute(
        "DELETE FROM comments WHERE post_id = ?",
        (post_id,),
    )

    connection.execute(
        "DELETE FROM post_likes WHERE post_id = ?",
        (post_id,),
    )

    connection.execute(
        "DELETE FROM posts WHERE id = ?",
        (post_id,),
    )

    connection.commit()
    connection.close()

    if post["media_url"]:

        filename = Path(
            post["media_url"]
        ).name

        path = UPLOAD_DIR / filename

        if path.exists():
            try:
                path.unlink()
            except Exception:
                pass

    return {
        "ok": True,
    }


# =========================================================
# SETTINGS
# =========================================================

@app.get("/api/settings")
def get_settings(
    request: Request,
):

    user_id = get_auth_user(request)

    connection = db()

    connection.execute(
        """
        INSERT OR IGNORE INTO settings
        (user_id)
        VALUES (?)
        """,
        (user_id,),
    )

    connection.commit()

    settings = connection.execute(
        """
        SELECT
            language,
            theme,
            notifications,
            show_online,
            show_last_seen
        FROM settings
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    connection.close()

    return dict(settings)


@app.put("/api/settings")
def update_settings(
    data: SettingsRequest,
    request: Request,
):

    user_id = get_auth_user(request)

    allowed_languages = {
        "ru",
        "en",
        "be",
        "kk",
    }

    allowed_themes = {
        "dark",
        "light",
        "blue",
    }

    if data.language not in allowed_languages:
        raise HTTPException(
            400,
            "Язык не поддерживается",
        )

    if data.theme not in allowed_themes:
        raise HTTPException(
            400,
            "Тема не поддерживается",
        )

    connection = db()

    connection.execute(
        """
        INSERT OR REPLACE INTO settings
        (
            user_id,
            language,
            theme,
            notifications,
            show_online,
            show_last_seen
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            data.language,
            data.theme,
            int(data.notifications),
            int(data.show_online),
            int(data.show_last_seen),
        ),
    )

    connection.commit()
    connection.close()

    return {
        "ok": True,
    }


# =========================================================
# GROUPS
# =========================================================

@app.post("/api/groups")
def create_group(
    data: GroupRequest,
    request: Request,
):

    user_id = get_auth_user(request)

    name = data.name.strip()

    if not name:
        raise HTTPException(
            400,
            "Название группы обязательно",
        )

    connection = db()

    cursor = connection.execute(
        """
        INSERT INTO groups
        (
            name,
            description,
            owner_id,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            name,
            data.description.strip(),
            user_id,
            now(),
        ),
    )

    group_id = cursor.lastrowid

    connection.execute(
        """
        INSERT INTO group_members
        (
            group_id,
            user_id,
            joined_at
        )
        VALUES (?, ?, ?)
        """,
        (
            group_id,
            user_id,
            now(),
        ),
    )

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "id": group_id,
    }


@app.get("/api/groups")
def get_groups(request: Request):

    user_id = get_auth_user(request)

    connection = db()

    groups = connection.execute(
        """
        SELECT
            g.id,
            g.name,
            g.description,
            g.owner_id,
            g.created_at
        FROM groups g
        JOIN group_members gm
            ON gm.group_id = g.id
        WHERE gm.user_id = ?
        ORDER BY g.id DESC
        """,
        (user_id,),
    ).fetchall()

    connection.close()

    return [
        dict(group)
        for group in groups
    ]


# =========================================================
# CHANNELS
# =========================================================

@app.post("/api/channels")
def create_channel(
    data: ChannelRequest,
    request: Request,
):

    user_id = get_auth_user(request)

    name = data.name.strip()
    username = data.username.strip().lower()

    if not name:
        raise HTTPException(
            400,
            "Название канала обязательно",
        )

    if not username:
        raise HTTPException(
            400,
            "Username канала обязателен",
        )

    if not username.replace("_", "").isalnum():
        raise HTTPException(
            400,
            "Username канала может содержать буквы, цифры и _",
        )

    connection = db()

    try:

        cursor = connection.execute(
            """
            INSERT INTO channels
            (
                name,
                username,
                description,
                owner_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name,
                username,
                data.description.strip(),
                user_id,
                now(),
            ),
        )

        channel_id = cursor.lastrowid

        connection.execute(
            """
            INSERT INTO channel_subscribers
            (
                channel_id,
                user_id,
                created_at
            )
            VALUES (?, ?, ?)
            """,
            (
                channel_id,
                user_id,
                now(),
            ),
        )

        connection.commit()

    except sqlite3.IntegrityError:

        connection.rollback()
        connection.close()

        raise HTTPException(
            400,
            "Такой username канала уже существует",
        )

    connection.close()

    return {
        "ok": True,
        "id": channel_id,
    }


@app.get("/api/channels")
def get_channels(request: Request):

    user_id = get_auth_user(request)

    connection = db()

    channels = connection.execute(
        """
        SELECT
            c.id,
            c.name,
            c.username,
            c.description,
            c.owner_id,
            c.created_at
        FROM channels c
        JOIN channel_subscribers s
            ON s.channel_id = c.id
        WHERE s.user_id = ?
        ORDER BY c.id DESC
        """,
        (user_id,),
    ).fetchall()

    connection.close()

    return [
        dict(channel)
        for channel in channels
    ]


# =========================================================
# WEBSOCKET
# =========================================================

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    user_id: int,
):

    token = websocket.query_params.get(
        "token"
    )

    authenticated_id = (
        get_user_from_token(token)
        if token
        else None
    )

    if authenticated_id != user_id:
        await websocket.close(
            code=1008
        )
        return

    await websocket.accept()

    connections[user_id].add(
        websocket
    )

    connection = db()

    connection.execute(
        """
        UPDATE users
        SET last_seen = ?
        WHERE id = ?
        """,
        (
            now(),
            user_id,
        ),
    )

    connection.commit()
    connection.close()

    try:

        while True:

            data = await websocket.receive_json()

            if data.get("type") == "ping":

                await websocket.send_json({
                    "type": "pong",
                })

    except WebSocketDisconnect:
        pass

    except Exception:
        pass

    finally:

        connections[user_id].discard(
            websocket
        )

        if not connections[user_id]:
            connections.pop(
                user_id,
                None
            )

            connection = db()

            connection.execute(
                """
                UPDATE users
                SET last_seen = ?
                WHERE id = ?
                """,
                (
                    now(),
                    user_id,
                ),
            )

            connection.commit()
            connection.close()


# =========================================================
# STATIC FILES
# =========================================================

app.mount(
    "/uploads",
    StaticFiles(
        directory=str(UPLOAD_DIR)
    ),
    name="uploads",
)


@app.get("/")
def index():

    return FileResponse(
        str(
            STATIC_DIR / "index.html"
        )
    )
