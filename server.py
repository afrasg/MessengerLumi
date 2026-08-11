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
    Form,
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

        CREATE TABLE IF NOT EXISTS communities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS community_members (
            community_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TEXT NOT NULL,
            UNIQUE(community_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS community_chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS community_chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            deleted INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS blocks (
            user_id INTEGER NOT NULL,
            blocked_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, blocked_id)
        );

        CREATE TABLE IF NOT EXISTS contact_aliases (
            user_id INTEGER NOT NULL,
            contact_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            UNIQUE(user_id, contact_id)
        );

        CREATE TABLE IF NOT EXISTS chat_settings (
            user_id INTEGER NOT NULL,
            peer_id INTEGER NOT NULL,
            wallpaper_url TEXT,
            wallpaper_blur INTEGER DEFAULT 0,
            deleted_for_me INTEGER DEFAULT 0,
            UNIQUE(user_id, peer_id)
        );

        CREATE TABLE IF NOT EXISTS login_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            from_user_id INTEGER NOT NULL,
            to_user_id INTEGER NOT NULL,
            message_id INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS channel_mutes (
            channel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            muted INTEGER DEFAULT 1,
            UNIQUE(channel_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS group_mutes (
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            muted INTEGER DEFAULT 1,
            UNIQUE(group_id, user_id)
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


    add_column_if_missing(
        connection,
        "messages",
        "media_url",
        "TEXT",
    )

    add_column_if_missing(
        connection,
        "messages",
        "media_type",
        "TEXT",
    )

    add_column_if_missing(
        connection,
        "users",
        "is_bot",
        "INTEGER DEFAULT 0",
    )

    add_column_if_missing(
        connection,
        "users",
        "is_verified",
        "INTEGER DEFAULT 0",
    )

    add_column_if_missing(
        connection,
        "groups",
        "avatar_url",
        "TEXT",
    )

    add_column_if_missing(
        connection,
        "channels",
        "avatar_url",
        "TEXT",
    )

    add_column_if_missing(
        connection,
        "messages",
        "invite_id",
        "INTEGER",
    )

    add_column_if_missing(
        connection,
        "messages",
        "invite_status",
        "TEXT",
    )

    connection.execute(
        """
        UPDATE users
        SET display_name = username
        WHERE display_name IS NULL
           OR display_name = ''
        """
    )

    # Ensure Lumi bot exists
    import hashlib as _hl
    from datetime import datetime as _dt
    _ts = _dt.utcnow().isoformat()
    _ph = _hl.sha256(b'__lumi_bot_internal__').hexdigest()
    bot = connection.execute(
        "SELECT id FROM users WHERE username = 'lumi'"
    ).fetchone()
    if not bot:
        connection.execute(
            """
            INSERT INTO users
            (username, password_hash, created_at, last_seen, display_name, bio, is_bot, is_verified)
            VALUES ('lumi', ?, ?, ?, 'Lumi', 'Официальный бот Messenger Lumi', 1, 1)
            """,
            (_ph, _ts, _ts),
        )
    else:
        connection.execute(
            "UPDATE users SET is_bot = 1, is_verified = 1, display_name = 'Lumi' WHERE username = 'lumi'"
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
    secure: bool = False,
):
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def set_browser_cookie(
    response: Response,
    browser_id: str,
    secure: bool = False,
):
    response.set_cookie(
        key=BROWSER_COOKIE,
        value=browser_id,
        max_age=365 * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def is_https(request: Request) -> bool:
    # Respect reverse-proxy headers
    proto = (
        request.headers.get("x-forwarded-proto")
        or request.url.scheme
        or ""
    ).lower()
    return proto == "https"


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
    try:
        user_id = int(user_id)
    except Exception:
        return

    dead = []
    sockets = list(connections.get(user_id, set()))
    for socket in sockets:
        try:
            await socket.send_json(payload)
        except Exception:
            dead.append(socket)

    for socket in dead:
        connections[user_id].discard(socket)


def user_public(connection, user_id):
    row = connection.execute(
        """
        SELECT id, username, display_name, avatar_url, is_bot, is_verified
        FROM users WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    return dict(row) if row else {}


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


class CommunityRequest(BaseModel):
    name: str
    description: str = ""


class CommunityChatRequest(BaseModel):
    name: str
    description: str = ""


class InviteRequest(BaseModel):
    username: str


class GroupMessageRequest(BaseModel):
    text: str


class ChannelMessageRequest(BaseModel):
    text: str


class DeleteAccountRequest(BaseModel):
    password: str


class CodeLoginRequest(BaseModel):
    username: str
    code: str


class RequestCodeRequest(BaseModel):
    username: str


class AliasRequest(BaseModel):
    alias: str


class WallpaperRequest(BaseModel):
    wallpaper_url: str = ""
    wallpaper_blur: bool = False


class RenameEntityRequest(BaseModel):
    name: str


class InviteActionRequest(BaseModel):
    action: str  # accept | decline


class CallSignalRequest(BaseModel):
    target_id: int
    signal_type: str
    payload: dict = {}


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
            last_seen,
            is_bot,
            is_verified
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
            last_seen,
            is_bot,
            is_verified
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
            last_seen,
            is_bot,
            is_verified
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
            is_read,
            media_url,
            media_type,
            invite_id,
            invite_status
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
        SELECT id, username, is_bot
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

    if receiver["is_bot"] or receiver["username"] == "lumi":
        connection.close()
        raise HTTPException(403, "Боту нельзя писать")

    blocked = connection.execute(
        """
        SELECT 1 FROM blocks
        WHERE (user_id = ? AND blocked_id = ?)
           OR (user_id = ? AND blocked_id = ?)
        """,
        (sender_id, data.receiver_id, data.receiver_id, sender_id),
    ).fetchone()
    if blocked:
        connection.close()
        raise HTTPException(403, "Пользователь заблокирован")

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

    connection = db()
    sender_info = user_public(connection, sender_id)
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
        "sender_username": sender_info.get("username"),
        "sender_name": sender_info.get("display_name") or sender_info.get("username"),
        "chat_kind": "private",
    }

    payload = {
        "type": "message",
        "message": message,
    }

    await send_ws(sender_id, payload)
    await send_ws(data.receiver_id, payload)

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
    group_row = connection.execute(
        "SELECT name FROM groups WHERE id = ?", (group_id,)
    ).fetchone()
    sender_info = user_public(connection, user_id)
    connection.commit()
    connection.close()

    message = {
        "id": message_id,
        "group_id": group_id,
        "group_name": group_row["name"] if group_row else "Группа",
        "sender_id": user_id,
        "text": text,
        "created_at": created,
        "deleted": 0,
        "sender_username": sender_info.get("username"),
        "sender_name": sender_info.get("display_name") or sender_info.get("username"),
        "chat_kind": "group",
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
    ch_row = connection.execute(
        "SELECT name FROM channels WHERE id = ?", (channel_id,)
    ).fetchone()
    sender_info = user_public(connection, user_id)
    connection.commit()
    connection.close()

    message = {
        "id": message_id,
        "channel_id": channel_id,
        "channel_name": ch_row["name"] if ch_row else "Канал",
        "sender_id": user_id,
        "text": text,
        "created_at": created,
        "deleted": 0,
        "sender_username": sender_info.get("username"),
        "sender_name": sender_info.get("display_name") or sender_info.get("username"),
        "chat_kind": "channel",
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


@app.get("/api/channels/search")
def search_channels(
    request: Request,
    q: str = "",
):
    user_id = get_auth_user(request)
    q = q.strip()

    if not q:
        return []

    connection = db()

    rows = connection.execute(
        """
        SELECT
            c.id,
            c.name,
            c.username,
            c.description,
            c.owner_id,
            c.created_at,
            CASE WHEN s.user_id IS NOT NULL THEN 1 ELSE 0 END AS joined
        FROM channels c
        LEFT JOIN channel_subscribers s
            ON s.channel_id = c.id
           AND s.user_id = ?
        WHERE
            c.name LIKE ?
            OR c.username LIKE ?
            OR c.description LIKE ?
        ORDER BY c.name
        LIMIT 30
        """,
        (
            user_id,
            "%" + q + "%",
            "%" + q + "%",
            "%" + q + "%",
        ),
    ).fetchall()

    connection.close()

    result = []
    for row in rows:
        item = dict(row)
        item["is_owner"] = item["owner_id"] == user_id
        item["joined"] = bool(item["joined"])
        result.append(item)

    return result


@app.post("/api/channels/{channel_id}/join")
def join_channel(
    channel_id: int,
    request: Request,
):
    user_id = get_auth_user(request)

    connection = db()

    channel = connection.execute(
        "SELECT id FROM channels WHERE id = ?",
        (channel_id,),
    ).fetchone()

    if not channel:
        connection.close()
        raise HTTPException(404, "Канал не найден")

    existing = connection.execute(
        """
        SELECT 1 FROM channel_subscribers
        WHERE channel_id = ? AND user_id = ?
        """,
        (channel_id, user_id),
    ).fetchone()

    if existing:
        connection.close()
        return {"ok": True, "joined": True}

    connection.execute(
        """
        INSERT INTO channel_subscribers
        (channel_id, user_id, created_at)
        VALUES (?, ?, ?)
        """,
        (channel_id, user_id, now()),
    )
    connection.commit()
    connection.close()

    return {"ok": True, "joined": True}


@app.get("/api/search")
def global_search(
    request: Request,
    q: str = "",
):
    """Unified search: users, channels, groups, communities."""
    user_id = get_auth_user(request)
    q = q.strip()

    if not q:
        return {
            "users": [],
            "channels": [],
            "groups": [],
            "communities": [],
        }

    like = "%" + q + "%"
    connection = db()

    users = connection.execute(
        """
        SELECT id, username, display_name, avatar_url, last_seen, is_bot, is_verified
        FROM users
        WHERE (username LIKE ? OR display_name LIKE ?)
          AND id != ?
        ORDER BY username
        LIMIT 20
        """,
        (like, like, user_id),
    ).fetchall()

    channels = connection.execute(
        """
        SELECT
            c.id, c.name, c.username, c.description, c.owner_id,
            CASE WHEN s.user_id IS NOT NULL THEN 1 ELSE 0 END AS joined
        FROM channels c
        LEFT JOIN channel_subscribers s
            ON s.channel_id = c.id AND s.user_id = ?
        WHERE c.name LIKE ? OR c.username LIKE ? OR c.description LIKE ?
        ORDER BY c.name
        LIMIT 20
        """,
        (user_id, like, like, like),
    ).fetchall()

    groups = connection.execute(
        """
        SELECT g.id, g.name, g.description, g.owner_id
        FROM groups g
        JOIN group_members gm ON gm.group_id = g.id
        WHERE gm.user_id = ?
          AND (g.name LIKE ? OR g.description LIKE ?)
        ORDER BY g.name
        LIMIT 20
        """,
        (user_id, like, like),
    ).fetchall()

    communities = connection.execute(
        """
        SELECT c.id, c.name, c.description, c.owner_id
        FROM communities c
        JOIN community_members m ON m.community_id = c.id
        WHERE m.user_id = ?
          AND (c.name LIKE ? OR c.description LIKE ?)
        ORDER BY c.name
        LIMIT 20
        """,
        (user_id, like, like),
    ).fetchall()

    connection.close()

    return {
        "users": [dict(u) for u in users],
        "channels": [
            {
                **dict(c),
                "is_owner": c["owner_id"] == user_id,
                "joined": bool(c["joined"]),
            }
            for c in channels
        ],
        "groups": [dict(g) for g in groups],
        "communities": [dict(c) for c in communities],
    }


# =========================================================
# COMMUNITIES (сообщества: все пишут, внутри чаты)
# =========================================================

@app.post("/api/communities")
def create_community(
    data: CommunityRequest,
    request: Request,
):
    user_id = get_auth_user(request)
    name = data.name.strip()

    if not name:
        raise HTTPException(400, "Название сообщества обязательно")

    connection = db()

    cursor = connection.execute(
        """
        INSERT INTO communities
        (name, description, owner_id, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (name, data.description.strip(), user_id, now()),
    )
    community_id = cursor.lastrowid

    connection.execute(
        """
        INSERT INTO community_members
        (community_id, user_id, joined_at)
        VALUES (?, ?, ?)
        """,
        (community_id, user_id, now()),
    )

    # Default general chat
    connection.execute(
        """
        INSERT INTO community_chats
        (community_id, name, description, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (community_id, "Общий", "Основной чат сообщества", now()),
    )

    connection.commit()
    connection.close()

    return {"ok": True, "id": community_id}


@app.get("/api/communities")
def get_communities(request: Request):
    user_id = get_auth_user(request)

    connection = db()
    rows = connection.execute(
        """
        SELECT
            c.id,
            c.name,
            c.description,
            c.owner_id,
            c.created_at
        FROM communities c
        JOIN community_members m
            ON m.community_id = c.id
        WHERE m.user_id = ?
        ORDER BY c.id DESC
        """,
        (user_id,),
    ).fetchall()
    connection.close()

    result = []
    for row in rows:
        item = dict(row)
        item["is_owner"] = item["owner_id"] == user_id
        result.append(item)
    return result


@app.post("/api/communities/{community_id}/invite")
def invite_to_community(
    community_id: int,
    data: InviteRequest,
    request: Request,
):
    user_id = get_auth_user(request)
    username = data.username.strip().lower()

    if not username:
        raise HTTPException(400, "Укажите username")

    connection = db()

    member = connection.execute(
        """
        SELECT 1 FROM community_members
        WHERE community_id = ? AND user_id = ?
        """,
        (community_id, user_id),
    ).fetchone()

    if not member:
        connection.close()
        raise HTTPException(403, "Вы не состоите в этом сообществе")

    target = connection.execute(
        "SELECT id FROM users WHERE username = ?",
        (username,),
    ).fetchone()

    if not target:
        connection.close()
        raise HTTPException(404, "Пользователь не найден")

    existing = connection.execute(
        """
        SELECT 1 FROM community_members
        WHERE community_id = ? AND user_id = ?
        """,
        (community_id, target["id"]),
    ).fetchone()

    if existing:
        connection.close()
        raise HTTPException(400, "Пользователь уже в сообществе")

    connection.execute(
        """
        INSERT INTO community_members
        (community_id, user_id, joined_at)
        VALUES (?, ?, ?)
        """,
        (community_id, target["id"], now()),
    )
    connection.commit()
    connection.close()

    return {"ok": True}


@app.get("/api/communities/{community_id}/chats")
def get_community_chats(
    community_id: int,
    request: Request,
):
    user_id = get_auth_user(request)

    connection = db()

    member = connection.execute(
        """
        SELECT 1 FROM community_members
        WHERE community_id = ? AND user_id = ?
        """,
        (community_id, user_id),
    ).fetchone()

    if not member:
        connection.close()
        raise HTTPException(403, "Вы не состоите в этом сообществе")

    chats = connection.execute(
        """
        SELECT id, community_id, name, description, created_at
        FROM community_chats
        WHERE community_id = ?
        ORDER BY id ASC
        """,
        (community_id,),
    ).fetchall()

    connection.close()
    return [dict(c) for c in chats]


@app.post("/api/communities/{community_id}/chats")
def create_community_chat(
    community_id: int,
    data: CommunityChatRequest,
    request: Request,
):
    user_id = get_auth_user(request)
    name = data.name.strip()

    if not name:
        raise HTTPException(400, "Название чата обязательно")

    connection = db()

    member = connection.execute(
        """
        SELECT 1 FROM community_members
        WHERE community_id = ? AND user_id = ?
        """,
        (community_id, user_id),
    ).fetchone()

    if not member:
        connection.close()
        raise HTTPException(403, "Вы не состоите в этом сообществе")

    cursor = connection.execute(
        """
        INSERT INTO community_chats
        (community_id, name, description, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (community_id, name, data.description.strip(), now()),
    )
    chat_id = cursor.lastrowid
    connection.commit()
    connection.close()

    return {"ok": True, "id": chat_id}


@app.get("/api/community-chats/{chat_id}/messages")
def get_community_chat_messages(
    chat_id: int,
    request: Request,
):
    user_id = get_auth_user(request)

    connection = db()

    chat = connection.execute(
        """
        SELECT cc.id, cc.community_id
        FROM community_chats cc
        WHERE cc.id = ?
        """,
        (chat_id,),
    ).fetchone()

    if not chat:
        connection.close()
        raise HTTPException(404, "Чат не найден")

    member = connection.execute(
        """
        SELECT 1 FROM community_members
        WHERE community_id = ? AND user_id = ?
        """,
        (chat["community_id"], user_id),
    ).fetchone()

    if not member:
        connection.close()
        raise HTTPException(403, "Нет доступа")

    messages = connection.execute(
        """
        SELECT
            m.id,
            m.chat_id,
            m.sender_id,
            CASE WHEN m.deleted = 1 THEN '' ELSE m.text END AS text,
            m.created_at,
            m.deleted,
            u.username,
            u.display_name AS sender_name
        FROM community_chat_messages m
        JOIN users u ON u.id = m.sender_id
        WHERE m.chat_id = ?
        ORDER BY m.id ASC
        """,
        (chat_id,),
    ).fetchall()

    connection.close()
    return [dict(m) for m in messages]


@app.post("/api/community-chats/{chat_id}/messages")
async def send_community_chat_message(
    chat_id: int,
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

    chat = connection.execute(
        """
        SELECT cc.id, cc.community_id
        FROM community_chats cc
        WHERE cc.id = ?
        """,
        (chat_id,),
    ).fetchone()

    if not chat:
        connection.close()
        raise HTTPException(404, "Чат не найден")

    member = connection.execute(
        """
        SELECT 1 FROM community_members
        WHERE community_id = ? AND user_id = ?
        """,
        (chat["community_id"], user_id),
    ).fetchone()

    if not member:
        connection.close()
        raise HTTPException(403, "Нет доступа")

    created = now()
    cursor = connection.execute(
        """
        INSERT INTO community_chat_messages
        (chat_id, sender_id, text, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (chat_id, user_id, text, created),
    )
    message_id = cursor.lastrowid

    members = connection.execute(
        """
        SELECT user_id FROM community_members
        WHERE community_id = ?
        """,
        (chat["community_id"],),
    ).fetchall()
    community = connection.execute(
        "SELECT name FROM communities WHERE id = ?",
        (chat["community_id"],),
    ).fetchone()
    chat_row = connection.execute(
        "SELECT name FROM community_chats WHERE id = ?",
        (chat_id,),
    ).fetchone()
    sender_info = user_public(connection, user_id)
    connection.commit()
    connection.close()

    message = {
        "id": message_id,
        "chat_id": chat_id,
        "community_id": chat["community_id"],
        "community_name": community["name"] if community else "Сообщество",
        "chat_name": chat_row["name"] if chat_row else "Чат",
        "sender_id": user_id,
        "text": text,
        "created_at": created,
        "deleted": 0,
        "sender_username": sender_info.get("username"),
        "sender_name": sender_info.get("display_name") or sender_info.get("username"),
        "chat_kind": "community",
    }

    payload = {
        "type": "community_message",
        "message": message,
    }

    for row in members:
        await send_ws(row["user_id"], payload)

    return {"ok": True, "message": message}



# =========================================================
# LUMI BOT / CODE LOGIN
# =========================================================

def get_lumi_id(connection=None):
    own = connection is None
    if own:
        connection = db()
    row = connection.execute(
        "SELECT id FROM users WHERE username = 'lumi'"
    ).fetchone()
    if own:
        connection.close()
    if not row:
        return None
    return row["id"]


def bot_send_message(to_user_id, text, invite_id=None, invite_status=None):
    lumi_id = get_lumi_id()
    if not lumi_id:
        return None
    connection = db()
    created = now()
    cursor = connection.execute(
        """
        INSERT INTO messages
        (sender_id, receiver_id, text, created_at, invite_id, invite_status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (lumi_id, to_user_id, text, created, invite_id, invite_status),
    )
    mid = cursor.lastrowid
    connection.commit()
    connection.close()
    return {
        "id": mid,
        "sender_id": lumi_id,
        "receiver_id": to_user_id,
        "text": text,
        "created_at": created,
        "edited_at": None,
        "deleted": 0,
        "is_read": 0,
        "media_url": None,
        "media_type": None,
        "invite_id": invite_id,
        "invite_status": invite_status,
    }


@app.post("/api/auth/request-code")
async def request_login_code(data: RequestCodeRequest, request: Request):
    username = data.username.strip().lower()
    connection = db()
    user = connection.execute(
        "SELECT id, username FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if not user:
        connection.close()
        raise HTTPException(404, "Пользователь не найден")
    if user["username"] == "lumi":
        connection.close()
        raise HTTPException(400, "Нельзя")

    import random
    code = f"{random.randint(100000, 999999)}"
    created = datetime.utcnow()
    expires = created + timedelta(minutes=10)

    connection.execute(
        """
        INSERT INTO login_codes (user_id, code, created_at, expires_at, used)
        VALUES (?, ?, ?, ?, 0)
        """,
        (user["id"], code, created.isoformat(), expires.isoformat()),
    )
    connection.commit()
    connection.close()

    msg = bot_send_message(
        user["id"],
        f"🔐 Ваш код для входа: {code}\nКод действует 10 минут.",
    )
    if msg:
        await send_ws(user["id"], {"type": "message", "message": msg})

    return {"ok": True, "detail": "Код отправлен ботом Lumi в личные сообщения"}


@app.post("/api/auth/login-code")
def login_by_code(data: CodeLoginRequest, request: Request, response: Response):
    username = data.username.strip().lower()
    code = data.code.strip()

    connection = db()
    user = connection.execute(
        "SELECT * FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if not user:
        connection.close()
        raise HTTPException(401, "Неверный username или код")

    row = connection.execute(
        """
        SELECT * FROM login_codes
        WHERE user_id = ? AND code = ? AND used = 0
        ORDER BY id DESC LIMIT 1
        """,
        (user["id"], code),
    ).fetchone()

    if not row:
        connection.close()
        raise HTTPException(401, "Неверный username или код")

    try:
        exp = datetime.fromisoformat(row["expires_at"])
    except Exception:
        exp = datetime.utcnow()

    if exp < datetime.utcnow():
        connection.close()
        raise HTTPException(401, "Код истёк")

    connection.execute(
        "UPDATE login_codes SET used = 1 WHERE id = ?",
        (row["id"],),
    )
    connection.commit()
    connection.close()

    browser_id = get_or_create_browser(request, response)
    token = create_session(user["id"], browser_id)
    set_auth_cookie(response, token)
    return {"ok": True, "token": token}


# =========================================================
# BLOCKS / CHAT SETTINGS / ALIAS
# =========================================================

@app.post("/api/users/{other_id}/block")
def block_user(other_id: int, request: Request):
    user_id = get_auth_user(request)
    if other_id == user_id:
        raise HTTPException(400, "Нельзя заблокировать себя")
    connection = db()
    connection.execute(
        """
        INSERT OR IGNORE INTO blocks (user_id, blocked_id, created_at)
        VALUES (?, ?, ?)
        """,
        (user_id, other_id, now()),
    )
    connection.commit()
    connection.close()
    return {"ok": True}


@app.delete("/api/users/{other_id}/block")
def unblock_user(other_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute(
        "DELETE FROM blocks WHERE user_id = ? AND blocked_id = ?",
        (user_id, other_id),
    )
    connection.commit()
    connection.close()
    return {"ok": True}


@app.get("/api/blocks")
def list_blocks(request: Request):
    user_id = get_auth_user(request)
    connection = db()
    rows = connection.execute(
        "SELECT blocked_id FROM blocks WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    connection.close()
    return [r["blocked_id"] for r in rows]


@app.put("/api/contacts/{contact_id}/alias")
def set_alias(contact_id: int, data: AliasRequest, request: Request):
    user_id = get_auth_user(request)
    alias = data.alias.strip()
    connection = db()
    if not alias:
        connection.execute(
            "DELETE FROM contact_aliases WHERE user_id = ? AND contact_id = ?",
            (user_id, contact_id),
        )
    else:
        connection.execute(
            """
            INSERT INTO contact_aliases (user_id, contact_id, alias)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, contact_id) DO UPDATE SET alias = excluded.alias
            """,
            (user_id, contact_id, alias),
        )
    connection.commit()
    connection.close()
    return {"ok": True, "alias": alias}


@app.get("/api/chats/{peer_id}/settings")
def get_chat_settings(peer_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    row = connection.execute(
        """
        SELECT wallpaper_url, wallpaper_blur, deleted_for_me
        FROM chat_settings
        WHERE user_id = ? AND peer_id = ?
        """,
        (user_id, peer_id),
    ).fetchone()
    alias = connection.execute(
        "SELECT alias FROM contact_aliases WHERE user_id = ? AND contact_id = ?",
        (user_id, peer_id),
    ).fetchone()
    blocked = connection.execute(
        "SELECT 1 FROM blocks WHERE user_id = ? AND blocked_id = ?",
        (user_id, peer_id),
    ).fetchone()
    connection.close()
    return {
        "wallpaper_url": row["wallpaper_url"] if row else None,
        "wallpaper_blur": bool(row["wallpaper_blur"]) if row else False,
        "deleted_for_me": bool(row["deleted_for_me"]) if row else False,
        "alias": alias["alias"] if alias else None,
        "blocked": bool(blocked),
    }


@app.put("/api/chats/{peer_id}/wallpaper")
def set_wallpaper(peer_id: int, data: WallpaperRequest, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute(
        """
        INSERT INTO chat_settings (user_id, peer_id, wallpaper_url, wallpaper_blur)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, peer_id) DO UPDATE SET
            wallpaper_url = excluded.wallpaper_url,
            wallpaper_blur = excluded.wallpaper_blur
        """,
        (user_id, peer_id, data.wallpaper_url or None, int(data.wallpaper_blur)),
    )
    connection.commit()
    connection.close()
    return {"ok": True}


@app.delete("/api/chats/{peer_id}")
def delete_chat(peer_id: int, request: Request, for_both: bool = False):
    user_id = get_auth_user(request)
    connection = db()
    if for_both:
        connection.execute(
            """
            DELETE FROM messages
            WHERE (sender_id = ? AND receiver_id = ?)
               OR (sender_id = ? AND receiver_id = ?)
            """,
            (user_id, peer_id, peer_id, user_id),
        )
        connection.execute(
            "DELETE FROM chat_settings WHERE (user_id = ? AND peer_id = ?) OR (user_id = ? AND peer_id = ?)",
            (user_id, peer_id, peer_id, user_id),
        )
    else:
        connection.execute(
            """
            INSERT INTO chat_settings (user_id, peer_id, deleted_for_me)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, peer_id) DO UPDATE SET deleted_for_me = 1
            """,
            (user_id, peer_id),
        )
    connection.commit()
    connection.close()
    return {"ok": True}


# =========================================================
# MESSAGE MEDIA / VOICE
# =========================================================

@app.post("/api/messages/media")
async def send_message_media(
    request: Request,
    receiver_id: int,
    file: UploadFile = File(...),
    text: str = Form(""),
):
    sender_id = get_auth_user(request)

    connection = db()
    blocked = connection.execute(
        """
        SELECT 1 FROM blocks
        WHERE (user_id = ? AND blocked_id = ?)
           OR (user_id = ? AND blocked_id = ?)
        """,
        (sender_id, receiver_id, receiver_id, sender_id),
    ).fetchone()
    peer = connection.execute(
        "SELECT is_bot, username FROM users WHERE id = ?",
        (receiver_id,),
    ).fetchone()
    connection.close()

    if blocked:
        raise HTTPException(403, "Пользователь заблокирован")
    if peer and (peer["is_bot"] or peer["username"] == "lumi"):
        raise HTTPException(403, "Боту нельзя писать")

    ctype = (file.content_type or "application/octet-stream").split(";")[0].strip().lower()
    name = file.filename or "file.bin"
    ext = Path(name).suffix.lower() or ".bin"

    if ctype.startswith("image/") or ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        media_type = "image"
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            ext = ".jpg"
    elif ctype.startswith("audio/") or ext in {".webm", ".ogg", ".mp3", ".m4a", ".wav", ".opus"}:
        media_type = "voice"
        if ext not in {".webm", ".ogg", ".mp3", ".m4a", ".wav", ".opus"}:
            ext = ".webm"
    else:
        media_type = "file"

    filename = f"msg_{sender_id}_{secrets.token_hex(10)}{ext}"
    path = UPLOAD_DIR / filename
    data_bytes = await file.read()
    if not data_bytes:
        raise HTTPException(400, "Пустой файл")
    with open(path, "wb") as out:
        out.write(data_bytes)
    url = "/uploads/" + filename

    connection = db()
    created = now()
    cursor = connection.execute(
        """
        INSERT INTO messages
        (sender_id, receiver_id, text, created_at, media_url, media_type)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (sender_id, receiver_id, (text or "").strip(), created, url, media_type),
    )
    mid = cursor.lastrowid
    connection.commit()
    connection.close()

    connection = db()
    sender_info = user_public(connection, sender_id)
    connection.close()
    message = {
        "id": mid,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "text": (text or "").strip(),
        "created_at": created,
        "edited_at": None,
        "deleted": 0,
        "is_read": 0,
        "media_url": url,
        "media_type": media_type,
        "sender_username": sender_info.get("username"),
        "sender_name": sender_info.get("display_name") or sender_info.get("username"),
        "chat_kind": "private",
    }
    payload = {"type": "message", "message": message}
    await send_ws(sender_id, payload)
    await send_ws(receiver_id, payload)
    return {"ok": True, "message": message}


# Block writing to bot in normal send - patch by wrapping is hard; add check via modifying send is done below through new endpoint usage
# Intercept existing send_message: we'll add middleware-like check by redefining - actually edit send to check bot

# =========================================================
# CHANNEL LEAVE / MUTE / RENAME / AVATAR
# =========================================================

@app.post("/api/channels/{channel_id}/leave")
def leave_channel(channel_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute(
        "DELETE FROM channel_subscribers WHERE channel_id = ? AND user_id = ?",
        (channel_id, user_id),
    )
    connection.execute(
        "DELETE FROM channel_mutes WHERE channel_id = ? AND user_id = ?",
        (channel_id, user_id),
    )
    connection.commit()
    connection.close()
    return {"ok": True}


@app.post("/api/channels/{channel_id}/mute")
def mute_channel(channel_id: int, request: Request, muted: bool = True):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute(
        """
        INSERT INTO channel_mutes (channel_id, user_id, muted)
        VALUES (?, ?, ?)
        ON CONFLICT(channel_id, user_id) DO UPDATE SET muted = excluded.muted
        """,
        (channel_id, user_id, int(muted)),
    )
    connection.commit()
    connection.close()
    return {"ok": True, "muted": muted}


@app.put("/api/channels/{channel_id}")
def rename_channel(channel_id: int, data: RenameEntityRequest, request: Request):
    user_id = get_auth_user(request)
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Название обязательно")
    connection = db()
    ch = connection.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if not ch:
        connection.close()
        raise HTTPException(404, "Канал не найден")
    if ch["owner_id"] != user_id:
        connection.close()
        raise HTTPException(403, "Только создатель")
    connection.execute("UPDATE channels SET name = ? WHERE id = ?", (name, channel_id))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.post("/api/channels/{channel_id}/avatar")
async def channel_avatar(channel_id: int, request: Request, file: UploadFile = File(...)):
    user_id = get_auth_user(request)
    connection = db()
    ch = connection.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if not ch or ch["owner_id"] != user_id:
        connection.close()
        raise HTTPException(403, "Только создатель")
    connection.close()

    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if file.content_type not in allowed:
        raise HTTPException(400, "Только изображения")
    filename = f"ch_{channel_id}_{secrets.token_hex(8)}{allowed[file.content_type]}"
    path = UPLOAD_DIR / filename
    with open(path, "wb") as out:
        shutil.copyfileobj(file.file, out)
    url = "/uploads/" + filename
    connection = db()
    connection.execute("UPDATE channels SET avatar_url = ? WHERE id = ?", (url, channel_id))
    connection.commit()
    connection.close()
    return {"ok": True, "avatar_url": url}


@app.post("/api/groups/{group_id}/leave")
def leave_group(group_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute(
        "DELETE FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )
    connection.commit()
    connection.close()
    return {"ok": True}


@app.put("/api/groups/{group_id}")
def rename_group(group_id: int, data: RenameEntityRequest, request: Request):
    user_id = get_auth_user(request)
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Название обязательно")
    connection = db()
    g = connection.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    if not g:
        connection.close()
        raise HTTPException(404, "Группа не найдена")
    if g["owner_id"] != user_id:
        connection.close()
        raise HTTPException(403, "Только создатель")
    connection.execute("UPDATE groups SET name = ? WHERE id = ?", (name, group_id))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.post("/api/groups/{group_id}/avatar")
async def group_avatar(group_id: int, request: Request, file: UploadFile = File(...)):
    user_id = get_auth_user(request)
    connection = db()
    g = connection.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    if not g or g["owner_id"] != user_id:
        connection.close()
        raise HTTPException(403, "Только создатель")
    connection.close()
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if file.content_type not in allowed:
        raise HTTPException(400, "Только изображения")
    filename = f"gr_{group_id}_{secrets.token_hex(8)}{allowed[file.content_type]}"
    path = UPLOAD_DIR / filename
    with open(path, "wb") as out:
        shutil.copyfileobj(file.file, out)
    url = "/uploads/" + filename
    connection = db()
    connection.execute("UPDATE groups SET avatar_url = ? WHERE id = ?", (url, group_id))
    connection.commit()
    connection.close()
    return {"ok": True, "avatar_url": url}


# =========================================================
# INVITES via Lumi bot
# =========================================================

@app.post("/api/invites/{invite_id}/respond")
async def respond_invite(invite_id: int, data: InviteActionRequest, request: Request):
    user_id = get_auth_user(request)
    action = data.action.strip().lower()
    if action not in ("accept", "decline"):
        raise HTTPException(400, "action: accept|decline")

    connection = db()
    inv = connection.execute(
        "SELECT * FROM invites WHERE id = ? AND to_user_id = ?",
        (invite_id, user_id),
    ).fetchone()
    if not inv:
        connection.close()
        raise HTTPException(404, "Приглашение не найдено")
    if inv["status"] != "pending":
        connection.close()
        raise HTTPException(400, "Уже отвечено")

    status = "accepted" if action == "accept" else "declined"
    connection.execute(
        "UPDATE invites SET status = ? WHERE id = ?",
        (status, invite_id),
    )

    if action == "accept":
        if inv["type"] == "group":
            connection.execute(
                """
                INSERT OR IGNORE INTO group_members (group_id, user_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (inv["target_id"], user_id, now()),
            )
        elif inv["type"] == "community":
            connection.execute(
                """
                INSERT OR IGNORE INTO community_members (community_id, user_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (inv["target_id"], user_id, now()),
            )

    if inv["message_id"]:
        connection.execute(
            "UPDATE messages SET invite_status = ? WHERE id = ?",
            (status, inv["message_id"]),
        )

    connection.commit()
    connection.close()

    reply = bot_send_message(user_id, "Ваш выбор был учтён")
    if reply:
        await send_ws(user_id, {"type": "message", "message": reply})

    return {"ok": True, "status": status}


# Override group invite to also notify via Lumi - patch existing invite_to_group by adding after it is complex
# We'll enhance invite endpoints by replacing invite functions - for now add helper used from new path

@app.post("/api/groups/{group_id}/invite-bot")
async def invite_group_via_bot(group_id: int, data: InviteRequest, request: Request):
    user_id = get_auth_user(request)
    username = data.username.strip().lower()
    connection = db()
    group = connection.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    member = connection.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    ).fetchone()
    if not group or not member:
        connection.close()
        raise HTTPException(403, "Нет доступа")
    target = connection.execute("SELECT id, username FROM users WHERE username = ?", (username,)).fetchone()
    inviter = connection.execute("SELECT username, display_name FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        connection.close()
        raise HTTPException(404, "Пользователь не найден")
    # create invite
    cursor = connection.execute(
        """
        INSERT INTO invites (type, target_id, from_user_id, to_user_id, status, created_at)
        VALUES ('group', ?, ?, ?, 'pending', ?)
        """,
        (group_id, user_id, target["id"], now()),
    )
    invite_id = cursor.lastrowid
    connection.commit()
    connection.close()

    text = f'Вам пришло приглашение в группу «{group["name"]}» от @{inviter["username"]}'
    msg = bot_send_message(target["id"], text, invite_id=invite_id, invite_status="pending")
    if msg:
        connection = db()
        connection.execute("UPDATE invites SET message_id = ? WHERE id = ?", (msg["id"], invite_id))
        connection.commit()
        connection.close()
        await send_ws(target["id"], {"type": "message", "message": msg})
    return {"ok": True}


@app.post("/api/communities/{community_id}/invite-bot")
async def invite_community_via_bot(community_id: int, data: InviteRequest, request: Request):
    user_id = get_auth_user(request)
    username = data.username.strip().lower()
    connection = db()
    community = connection.execute("SELECT * FROM communities WHERE id = ?", (community_id,)).fetchone()
    member = connection.execute(
        "SELECT 1 FROM community_members WHERE community_id = ? AND user_id = ?",
        (community_id, user_id),
    ).fetchone()
    if not community or not member:
        connection.close()
        raise HTTPException(403, "Нет доступа")
    target = connection.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    inviter = connection.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        connection.close()
        raise HTTPException(404, "Пользователь не найден")
    cursor = connection.execute(
        """
        INSERT INTO invites (type, target_id, from_user_id, to_user_id, status, created_at)
        VALUES ('community', ?, ?, ?, 'pending', ?)
        """,
        (community_id, user_id, target["id"], now()),
    )
    invite_id = cursor.lastrowid
    connection.commit()
    connection.close()

    text = f'Вам пришло приглашение в сообщество «{community["name"]}» от @{inviter["username"]}'
    msg = bot_send_message(target["id"], text, invite_id=invite_id, invite_status="pending")
    if msg:
        connection = db()
        connection.execute("UPDATE invites SET message_id = ? WHERE id = ?", (msg["id"], invite_id))
        connection.commit()
        connection.close()
        await send_ws(target["id"], {"type": "message", "message": msg})
    return {"ok": True}


# =========================================================
# CALL SIGNALING (WebRTC)
# =========================================================

@app.post("/api/calls/signal")
async def call_signal(data: CallSignalRequest, request: Request):
    user_id = get_auth_user(request)
    target_id = int(data.target_id)

    if target_id == user_id:
        raise HTTPException(400, "Нельзя звонить себе")

    connection = db()
    me = user_public(connection, user_id)
    online = target_id in connections and len(connections[target_id]) > 0
    connection.close()

    payload = {
        "type": "call_signal",
        "from_id": int(user_id),
        "from_name": me.get("display_name") or me.get("username") or "User",
        "from_username": me.get("username") or "",
        "signal_type": data.signal_type,
        "payload": data.payload or {},
    }
    await send_ws(target_id, payload)
    return {"ok": True, "online": online}



@app.get("/api/dialogs")
def get_dialogs(request: Request):
    """Recent private chats like Telegram left list."""
    user_id = get_auth_user(request)
    connection = db()

    rows = connection.execute(
        """
        SELECT
            CASE WHEN m.sender_id = ? THEN m.receiver_id ELSE m.sender_id END AS peer_id,
            MAX(m.id) AS last_id
        FROM messages m
        LEFT JOIN chat_settings cs
            ON cs.user_id = ?
           AND cs.peer_id = CASE WHEN m.sender_id = ? THEN m.receiver_id ELSE m.sender_id END
        WHERE (m.sender_id = ? OR m.receiver_id = ?)
          AND IFNULL(cs.deleted_for_me, 0) = 0
        GROUP BY peer_id
        ORDER BY last_id DESC
        LIMIT 50
        """,
        (user_id, user_id, user_id, user_id, user_id),
    ).fetchall()

    result = []
    for row in rows:
        peer_id = row["peer_id"]
        user = connection.execute(
            """
            SELECT id, username, display_name, avatar_url, is_bot, is_verified, last_seen
            FROM users WHERE id = ?
            """,
            (peer_id,),
        ).fetchone()
        if not user:
            continue
        last = connection.execute(
            """
            SELECT id, text, created_at, sender_id, media_type, deleted, is_read
            FROM messages WHERE id = ?
            """,
            (row["last_id"],),
        ).fetchone()
        alias = connection.execute(
            "SELECT alias FROM contact_aliases WHERE user_id = ? AND contact_id = ?",
            (user_id, peer_id),
        ).fetchone()
        unread = connection.execute(
            """
            SELECT COUNT(*) AS c FROM messages
            WHERE sender_id = ? AND receiver_id = ? AND is_read = 0 AND deleted = 0
            """,
            (peer_id, user_id),
        ).fetchone()["c"]
        item = dict(user)
        item["alias"] = alias["alias"] if alias else None
        item["last_message"] = dict(last) if last else None
        item["unread"] = unread
        result.append(item)

    connection.close()
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

    user_id = int(session["user_id"])

    await websocket.accept()

    connections[user_id].add(websocket)

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
