import os
import sqlite3
import hashlib
import secrets
import shutil
import re
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    HTTPException,
    Request,
    Response,
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

SESSION_COOKIE = "mm_session"
BROWSER_COOKIE = "mm_browser"

SESSION_DAYS = 30

connections = defaultdict(set)


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
            last_seen TEXT,
            display_name TEXT,
            bio TEXT DEFAULT '',
            avatar_url TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT UNIQUE NOT NULL,
            browser_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            expires_at TEXT NOT NULL
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

        CREATE TABLE IF NOT EXISTS group_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            deleted INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS channel_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            deleted INTEGER DEFAULT 0
        );
        """
    )

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


def hash_token(token):
    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()


def browser_hash(value):
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def new_token():
    return secrets.token_urlsafe(48)


def valid_username(username):
    return (
        len(username) >= 3
        and len(username) <= 30
        and bool(
            re.fullmatch(
                r"[a-zA-Z0-9_]+",
                username,
            )
        )
    )


def set_auth_cookie(
    response: Response,
    token: str,
):
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def set_browser_cookie(
    response: Response,
    browser_id: str,
):
    response.set_cookie(
        key=BROWSER_COOKIE,
        value=browser_id,
        max_age=365 * 24 * 60 * 60,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def get_or_create_browser(
    request: Request,
    response: Response,
):
    value = request.cookies.get(
        BROWSER_COOKIE
    )

    if value:
        return value

    value = secrets.token_urlsafe(32)
    set_browser_cookie(
        response,
        value,
    )

    return value


def create_session(
    user_id,
    browser_id,
):
    token = new_token()
    created = datetime.utcnow()
    expires = created + timedelta(
        days=SESSION_DAYS
    )

    connection = db()

    connection.execute(
        """
        INSERT INTO sessions
        (
            user_id,
            token_hash,
            browser_hash,
            created_at,
            last_seen,
            expires_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            hash_token(token),
            browser_hash(browser_id),
            created.isoformat(),
            created.isoformat(),
            expires.isoformat(),
        ),
    )

    connection.commit()
    connection.close()

    return token


def get_auth_user(
    request: Request,
):
    token = request.cookies.get(
        SESSION_COOKIE
    )

    # Also accept Authorization: Bearer <token> (frontend localStorage)
    if not token:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()

    if not token:
        raise HTTPException(
            401,
            "Не авторизован",
        )

    connection = db()

    session = connection.execute(
        """
        SELECT
            s.id AS session_id,
            s.user_id,
            s.expires_at,
            u.username
        FROM sessions s
        JOIN users u
            ON u.id = s.user_id
        WHERE s.token_hash = ?
        """,
        (
            hash_token(token),
        ),
    ).fetchone()

    if not session:
        connection.close()

        raise HTTPException(
            401,
            "Сессия недействительна",
        )

    try:
        expires = datetime.fromisoformat(
            session["expires_at"]
        )
    except Exception:
        expires = datetime.utcnow()

    if expires < datetime.utcnow():
        connection.execute(
            """
            DELETE FROM sessions
            WHERE id = ?
            """,
            (
                session["session_id"],
            ),
        )

        connection.commit()
        connection.close()

        raise HTTPException(
            401,
            "Сессия истекла",
        )

    connection.execute(
        """
        UPDATE sessions
        SET last_seen = ?
        WHERE id = ?
        """,
        (
            now(),
            session["session_id"],
        ),
    )

    connection.execute(
        """
        UPDATE users
        SET last_seen = ?
        WHERE id = ?
        """,
        (
            now(),
            session["user_id"],
        ),
    )

    connection.commit()
    connection.close()

    return session["user_id"]


def get_browser_id(request):
    value = request.cookies.get(
        BROWSER_COOKIE
    )

    if not value:
        raise HTTPException(
            400,
            "Браузер не определён",
        )

    return value


async def send_ws(
    user_id,
    payload,
):
    dead = []

    for socket in list(
        connections.get(
            user_id,
            set(),
        )
    ):
        try:
            await socket.send_json(
                payload
            )
        except Exception:
            dead.append(socket)

    for socket in dead:
        connections[user_id].discard(
            socket
        )


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


class InviteRequest(BaseModel):
    username: str


class GroupMessageRequest(BaseModel):
    text: str


class ChannelMessageRequest(BaseModel):
    text: str


class DeleteAccountRequest(BaseModel):
    password: str


# =========================================================
# AUTH
# =========================================================

@app.post("/api/register")
def register(
    data: RegisterRequest,
    request: Request,
    response: Response,
):
    username = data.username.strip().lower()

    if not valid_username(username):
        raise HTTPException(
            400,
            "Username: 3-30 символов, только буквы, цифры и _",
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
            hash_password(
                data.password
            ),
            created,
            created,
            username,
        ),
    )

    user_id = cursor.lastrowid

    connection.execute(
        """
        INSERT OR IGNORE INTO settings
        (user_id)
        VALUES (?)
        """,
        (user_id,),
    )

    connection.commit()
    connection.close()

    browser_id = get_or_create_browser(
        request,
        response,
    )

    token = create_session(
        user_id,
        browser_id,
    )

    set_auth_cookie(
        response,
        token,
    )

    return {
        "ok": True,
        "token": token,
    }


@app.post("/api/login")
def login(
    data: LoginRequest,
    request: Request,
    response: Response,
):
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
        INSERT OR IGNORE INTO settings
        (user_id)
        VALUES (?)
        """,
        (user["id"],),
    )

    connection.commit()
    connection.close()

    browser_id = get_or_create_browser(
        request,
        response,
    )

    token = create_session(
        user["id"],
        browser_id,
    )

    set_auth_cookie(
        response,
        token,
    )

    return {
        "ok": True,
        "token": token,
    }


@app.post("/api/logout")
def logout(
    request: Request,
    response: Response,
):
    token = request.cookies.get(
        SESSION_COOKIE
    )

    if token:
        connection = db()

        connection.execute(
            """
            DELETE FROM sessions
            WHERE token_hash = ?
            """,
            (
                hash_token(token),
            ),
        )

        connection.commit()
        connection.close()

    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )

    return {
        "ok": True,
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
# ACCOUNT / SESSIONS
# =========================================================

@app.get("/api/accounts")
def accounts(request: Request):
    user_id = get_auth_user(request)
    browser_id = get_browser_id(request)

    connection = db()

    rows = connection.execute(
        """
        SELECT
            s.id AS session_id,
            s.user_id,
            s.last_seen,
            u.username,
            u.display_name,
            u.avatar_url
        FROM sessions s
        JOIN users u
            ON u.id = s.user_id
        WHERE s.browser_hash = ?
        ORDER BY s.last_seen DESC
        """,
        (
            browser_hash(browser_id),
        ),
    ).fetchall()

    current_token = request.cookies.get(
        SESSION_COOKIE
    )

    current_hash = (
        hash_token(current_token)
        if current_token
        else ""
    )

    current = connection.execute(
        """
        SELECT id
        FROM sessions
        WHERE token_hash = ?
        """,
        (current_hash,),
    ).fetchone()

    connection.close()

    current_session_id = (
        current["id"]
        if current
        else None
    )

    return [
        {
            **dict(row),
            "current": (
                row["session_id"]
                == current_session_id
            ),
        }
        for row in rows
    ]


@app.post("/api/accounts/switch/{session_id}")
def switch_account(
    session_id: int,
    request: Request,
    response: Response,
):
    current_user = get_auth_user(request)
    browser_id = get_browser_id(request)

    connection = db()

    target = connection.execute(
        """
        SELECT
            id,
            user_id,
            token_hash
        FROM sessions
        WHERE id = ?
          AND browser_hash = ?
        """,
        (
            session_id,
            browser_hash(browser_id),
        ),
    ).fetchone()

    if not target:
        connection.close()

        raise HTTPException(
            404,
            "Аккаунт не найден на этом устройстве",
        )

    token = connection.execute(
        """
        SELECT token_hash
        FROM sessions
        WHERE id = ?
        """,
        (
            target["id"],
        ),
    ).fetchone()

    connection.close()

    if not token:
        raise HTTPException(
            404,
            "Сессия не найдена",
        )

    connection = db()

    # Сам токен не хранится в открытом виде,
    # поэтому при переключении создаём новую
    # сессию для этого же аккаунта.
    new_session_token = new_token()

    created = now()
    expires = (
        datetime.utcnow()
        + timedelta(days=SESSION_DAYS)
    ).isoformat()

    connection.execute(
        """
        INSERT INTO sessions
        (
            user_id,
            token_hash,
            browser_hash,
            created_at,
            last_seen,
            expires_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            target["user_id"],
            hash_token(
                new_session_token
            ),
            browser_hash(browser_id),
            created,
            created,
            expires,
        ),
    )

    connection.commit()
    connection.close()

    set_auth_cookie(
        response,
        new_session_token,
    )

    return {
        "ok": True,
        "user_id": target["user_id"],
        "old_user_id": current_user,
    }


# =========================================================
# DELETE ACCOUNT
# =========================================================

@app.delete("/api/account")
def delete_account(
    data: DeleteAccountRequest,
    request: Request,
    response: Response,
):
    user_id = get_auth_user(request)

    connection = db()

    user = connection.execute(
        """
        SELECT *
        FROM users
        WHERE id = ?
        """,
        (user_id,),
    ).fetchone()

    if not user:
        connection.close()

        raise HTTPException(
            404,
            "Аккаунт не найден",
        )

    if user["password_hash"] != hash_password(
        data.password
    ):
        connection.close()

        raise HTTPException(
            403,
            "Неверный пароль",
        )

    # Удаляем всё, что принадлежит аккаунту.
    message_ids = connection.execute(
        """
        SELECT id
        FROM messages
        WHERE sender_id = ?
           OR receiver_id = ?
        """,
        (
            user_id,
            user_id,
        ),
    ).fetchall()

    ids = [
        row["id"]
        for row in message_ids
    ]

    if ids:
        placeholders = ",".join(
            "?" * len(ids)
        )

        connection.execute(
            f"""
            DELETE FROM favorites
            WHERE message_id IN ({placeholders})
            """,
            ids,
        )

    connection.execute(
        """
        DELETE FROM favorites
        WHERE user_id = ?
        """,
        (user_id,),
    )

    connection.execute(
        """
        DELETE FROM messages
        WHERE sender_id = ?
           OR receiver_id = ?
        """,
        (
            user_id,
            user_id,
        ),
    )

    posts = connection.execute(
        """
        SELECT id, media_url
        FROM posts
        WHERE author_id = ?
        """,
        (user_id,),
    ).fetchall()

    post_ids = [
        row["id"]
        for row in posts
    ]

    if post_ids:
        placeholders = ",".join(
            "?" * len(post_ids)
        )

        connection.execute(
            f"""
            DELETE FROM comments
            WHERE post_id IN ({placeholders})
            """,
            post_ids,
        )

        connection.execute(
            f"""
            DELETE FROM post_likes
            WHERE post_id IN ({placeholders})
            """,
            post_ids,
        )

    connection.execute(
        """
        DELETE FROM comments
        WHERE user_id = ?
        """,
        (user_id,),
    )

    connection.execute(
        """
        DELETE FROM post_likes
        WHERE user_id = ?
        """,
        (user_id,),
    )

    connection.execute(
        """
        DELETE FROM posts
        WHERE author_id = ?
        """,
        (user_id,),
    )

    connection.execute(
        """
        DELETE FROM group_members
        WHERE user_id = ?
        """,
        (user_id,),
    )

    connection.execute(
        """
        DELETE FROM channel_subscribers
        WHERE user_id = ?
        """,
        (user_id,),
    )

    connection.execute(
        """
        DELETE FROM sessions
        WHERE user_id = ?
        """,
        (user_id,),
    )

    connection.execute(
        """
        DELETE FROM settings
        WHERE user_id = ?
        """,
        (user_id,),
    )

    connection.execute(
        """
        DELETE FROM groups
        WHERE owner_id = ?
        """,
        (user_id,),
    )

    connection.execute(
        """
        DELETE FROM channels
        WHERE owner_id = ?
        """,
        (user_id,),
    )

    connection.execute(
        """
        DELETE FROM users
        WHERE id = ?
        """,
        (user_id,),
    )

    connection.commit()
    connection.close()

    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )

    return {
        "ok": True,
    }


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

    if not valid_username(username):
        raise HTTPException(
            400,
            "Некорректный username",
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

    # Users appear only via search — empty query returns nothing
    if not q:
        return []

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

    # При открытии чата сообщения собеседника
    # становятся прочитанными.
    connection.execute(
        """
        UPDATE messages
        SET is_read = 1
        WHERE
            sender_id = ?
            AND receiver_id = ?
            AND is_read = 0
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

    # Отправителю тоже отправляем WS,
    # но фронтенд проверяет ID и не создаёт дубль.
    await send_ws(
        sender_id,
        payload,
    )

    await send_ws(
        data.receiver_id,
        payload,
    )

    return {
        "ok": True,
        "message": message,
    }


@app.post("/api/messages/read/{other_user_id}")
async def mark_messages_read(
    other_user_id: int,
    request: Request,
):
    user_id = get_auth_user(request)

    connection = db()

    connection.execute(
        """
        UPDATE messages
        SET is_read = 1
        WHERE
            sender_id = ?
            AND receiver_id = ?
            AND is_read = 0
        """,
        (
            other_user_id,
            user_id,
        ),
    )

    connection.commit()
    connection.close()

    await send_ws(
        other_user_id,
        {
            "type": "messages_read",
            "reader_id": user_id,
        },
    )

    return {
        "ok": True,
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
            "Сообщение удалено",
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

    payload = {
        "type": "message_deleted",
        "message": {
            "id": message_id,
            "sender_id": message["sender_id"],
            "receiver_id": message["receiver_id"],
            "deleted": 1,
            "text": "",
        },
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
            ) AS comments_count
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


@app.get("/api/posts/{post_id}")
def get_post(
    post_id: int,
    request: Request,
):
    get_auth_user(request)

    connection = db()

    post = connection.execute(
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
            ) AS comments_count
        FROM posts p
        JOIN users u
            ON u.id = p.author_id
        WHERE p.id = ?
        """,
        (post_id,),
    ).fetchone()

    connection.close()

    if not post:
        raise HTTPException(
            404,
            "Пост не найден",
        )

    return dict(post)


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
# COMMENTS
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
        """
        DELETE FROM comments
        WHERE post_id = ?
        """,
        (post_id,),
    )

    connection.execute(
        """
        DELETE FROM post_likes
        WHERE post_id = ?
        """,
        (post_id,),
    )

    connection.execute(
        """
        DELETE FROM posts
        WHERE id = ?
        """,
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

    if data.language not in {
        "ru",
        "en",
        "be",
        "kk",
    }:
        raise HTTPException(
            400,
            "Язык не поддерживается",
        )

    if data.theme not in {
        "dark",
        "light",
        "blue",
    }:
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
def get_groups(
    request: Request,
):
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


@app.post("/api/groups/{group_id}/invite")
def invite_to_group(
    group_id: int,
    data: InviteRequest,
    request: Request,
):
    user_id = get_auth_user(request)
    username = data.username.strip().lower()

    if not username:
        raise HTTPException(400, "Укажите username")

    connection = db()

    group = connection.execute(
        """
        SELECT *
        FROM groups
        WHERE id = ?
        """,
        (group_id,),
    ).fetchone()

    if not group:
        connection.close()
        raise HTTPException(404, "Группа не найдена")

    # Only members can invite (or only owner — keep simple: any member)
    member = connection.execute(
        """
        SELECT 1
        FROM group_members
        WHERE group_id = ?
          AND user_id = ?
        """,
        (group_id, user_id),
    ).fetchone()

    if not member:
        connection.close()
        raise HTTPException(403, "Вы не состоите в этой группе")

    target = connection.execute(
        """
        SELECT id
        FROM users
        WHERE username = ?
        """,
        (username,),
    ).fetchone()

    if not target:
        connection.close()
        raise HTTPException(404, "Пользователь не найден")

    if target["id"] == user_id:
        connection.close()
        raise HTTPException(400, "Нельзя пригласить себя")

    existing = connection.execute(
        """
        SELECT 1
        FROM group_members
        WHERE group_id = ?
          AND user_id = ?
        """,
        (group_id, target["id"]),
    ).fetchone()

    if existing:
        connection.close()
        raise HTTPException(400, "Пользователь уже в группе")

    connection.execute(
        """
        INSERT INTO group_members
        (group_id, user_id, joined_at)
        VALUES (?, ?, ?)
        """,
        (group_id, target["id"], now()),
    )

    connection.commit()
    connection.close()

    return {"ok": True}


@app.get("/api/groups/{group_id}/messages")
def get_group_messages(
    group_id: int,
    request: Request,
):
    user_id = get_auth_user(request)

    connection = db()

    member = connection.execute(
        """
        SELECT 1
        FROM group_members
        WHERE group_id = ?
          AND user_id = ?
        """,
        (group_id, user_id),
    ).fetchone()

    if not member:
        connection.close()
        raise HTTPException(403, "Вы не состоите в этой группе")

    messages = connection.execute(
        """
        SELECT
            m.id,
            m.group_id,
            m.sender_id,
            CASE WHEN m.deleted = 1 THEN '' ELSE m.text END AS text,
            m.created_at,
            m.deleted,
            u.username,
            u.display_name AS sender_name
        FROM group_messages m
        JOIN users u ON u.id = m.sender_id
        WHERE m.group_id = ?
        ORDER BY m.id ASC
        """,
        (group_id,),
    ).fetchall()

    connection.close()

    return [dict(m) for m in messages]


@app.post("/api/groups/{group_id}/messages")
async def send_group_message(
    group_id: int,
    data: GroupMessageRequest,
    request: Request,
):
    user_id = get_auth_user(request)
    text = data.text.strip()

    if not text:
        raise HTTPException(400, "Пустое сообщение")

    if len(text) > 5000:
        raise HTTPException(400, "Сообщение слишком длинное")

    connection = db()

    member = connection.execute(
        """
        SELECT 1
        FROM group_members
        WHERE group_id = ?
          AND user_id = ?
        """,
        (group_id, user_id),
    ).fetchone()

    if not member:
        connection.close()
        raise HTTPException(403, "Вы не состоите в этой группе")

    created = now()

    cursor = connection.execute(
        """
        INSERT INTO group_messages
        (group_id, sender_id, text, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (group_id, user_id, text, created),
    )

    message_id = cursor.lastrowid

    # Notify all members via WS
    members = connection.execute(
        """
        SELECT user_id
        FROM group_members
        WHERE group_id = ?
        """,
        (group_id,),
    ).fetchall()

    connection.commit()
    connection.close()

    message = {
        "id": message_id,
        "group_id": group_id,
        "sender_id": user_id,
        "text": text,
        "created_at": created,
        "deleted": 0,
    }

    payload = {
        "type": "group_message",
        "message": message,
    }

    for row in members:
        await send_ws(row["user_id"], payload)

    return {"ok": True, "message": message}


@app.get("/api/channels/{channel_id}/messages")
def get_channel_messages(
    channel_id: int,
    request: Request,
):
    user_id = get_auth_user(request)

    connection = db()

    sub = connection.execute(
        """
        SELECT 1
        FROM channel_subscribers
        WHERE channel_id = ?
          AND user_id = ?
        """,
        (channel_id, user_id),
    ).fetchone()

    if not sub:
        connection.close()
        raise HTTPException(403, "Вы не подписаны на канал")

    messages = connection.execute(
        """
        SELECT
            m.id,
            m.channel_id,
            m.sender_id,
            CASE WHEN m.deleted = 1 THEN '' ELSE m.text END AS text,
            m.created_at,
            m.deleted,
            u.username,
            u.display_name AS sender_name
        FROM channel_messages m
        JOIN users u ON u.id = m.sender_id
        WHERE m.channel_id = ?
        ORDER BY m.id ASC
        """,
        (channel_id,),
    ).fetchall()

    connection.close()

    return [dict(m) for m in messages]


@app.post("/api/channels/{channel_id}/messages")
async def send_channel_message(
    channel_id: int,
    data: ChannelMessageRequest,
    request: Request,
):
    user_id = get_auth_user(request)
    text = data.text.strip()

    if not text:
        raise HTTPException(400, "Пустое сообщение")

    if len(text) > 5000:
        raise HTTPException(400, "Сообщение слишком длинное")

    connection = db()

    channel = connection.execute(
        """
        SELECT *
        FROM channels
        WHERE id = ?
        """,
        (channel_id,),
    ).fetchone()

    if not channel:
        connection.close()
        raise HTTPException(404, "Канал не найден")

    # Only the owner (creator) can post in the channel
    if channel["owner_id"] != user_id:
        connection.close()
        raise HTTPException(
            403,
            "В канал может писать только создатель",
        )

    created = now()

    cursor = connection.execute(
        """
        INSERT INTO channel_messages
        (channel_id, sender_id, text, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (channel_id, user_id, text, created),
    )

    message_id = cursor.lastrowid

    subscribers = connection.execute(
        """
        SELECT user_id
        FROM channel_subscribers
        WHERE channel_id = ?
        """,
        (channel_id,),
    ).fetchall()

    connection.commit()
    connection.close()

    message = {
        "id": message_id,
        "channel_id": channel_id,
        "sender_id": user_id,
        "text": text,
        "created_at": created,
        "deleted": 0,
    }

    payload = {
        "type": "channel_message",
        "message": message,
    }

    for row in subscribers:
        await send_ws(row["user_id"], payload)

    return {"ok": True, "message": message}


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

    if not valid_username(username):
        raise HTTPException(
            400,
            "Некорректный username канала",
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
def get_channels(
    request: Request,
):
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

    result = []
    for ch in channels:
        item = dict(ch)
        item["is_owner"] = item["owner_id"] == user_id
        result.append(item)

    return result


# =========================================================
# WEBSOCKET
# =========================================================

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
):
    token = websocket.query_params.get(
        "token"
    )

    if not token:
        await websocket.close(
            code=1008
        )
        return

    connection = db()

    session = connection.execute(
        """
        SELECT
            id,
            user_id,
            expires_at
        FROM sessions
        WHERE token_hash = ?
        """,
        (
            hash_token(token),
        ),
    ).fetchone()

    connection.close()

    if not session:
        await websocket.close(
            code=1008
        )
        return

    try:
        expires = datetime.fromisoformat(
            session["expires_at"]
        )
    except Exception:
        expires = datetime.utcnow()

    if expires < datetime.utcnow():
        await websocket.close(
            code=1008
        )
        return

    user_id = session["user_id"]

    await websocket.accept()

    connections[user_id].add(
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
                None,
            )


# =========================================================
# STATIC
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
