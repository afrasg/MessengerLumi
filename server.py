import os
import sqlite3
import hashlib
import secrets
import shutil
import re
import json
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
    Query,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# =========================================================
# CONFIG
# =========================================================

app = FastAPI(title="Messenger Lumi")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = Path("/tmp/messenger_lumi.db")

STATIC_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

SESSION_COOKIE = "mm_session"
BROWSER_COOKIE = "mm_browser"
SESSION_DAYS = 30

connections = defaultdict(set)

LUMI_AVATAR_URL = "https://is1-ssl.mzstatic.com/image/thumb/PurpleSource221/v4/5c/e5/f3/5ce5f3be-c924-0649-5dba-309206c42ba6/Placeholder.mill/1200x630wa.jpg"


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
    connection = sqlite3.connect(str(DB_PATH), timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def column_exists(connection, table, column):
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def add_column_if_missing(connection, table, column, definition):
    if not column_exists(connection, table, column):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    connection = db()

    connection.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seen TEXT,
        display_name TEXT,
        bio TEXT DEFAULT '',
        avatar_url TEXT,
        is_bot INTEGER DEFAULT 0,
        is_verified INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token_hash TEXT UNIQUE NOT NULL,
        browser_hash TEXT NOT NULL,
        device_info TEXT,
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
        is_read INTEGER DEFAULT 0,
        media_url TEXT,
        media_type TEXT,
        invite_id INTEGER,
        invite_status TEXT,
        forwarded_from INTEGER
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

    CREATE TABLE IF NOT EXISTS favorite_reels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        post_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(user_id, post_id)
    );

    CREATE TABLE IF NOT EXISTS settings (
        user_id INTEGER PRIMARY KEY,
        language TEXT DEFAULT 'ru',
        theme TEXT DEFAULT 'light',
        notifications INTEGER DEFAULT 1,
        show_online INTEGER DEFAULT 1,
        show_last_seen INTEGER DEFAULT 1,
        auto_answer INTEGER DEFAULT 0,
        mute_on_join INTEGER DEFAULT 0,
        camera_on_join INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        owner_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        avatar_url TEXT,
        slow_mode_seconds INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS group_members (
        group_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        joined_at TEXT NOT NULL,
        UNIQUE(group_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS group_admins (
        group_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        can_delete_messages INTEGER DEFAULT 1,
        can_ban_users INTEGER DEFAULT 1,
        can_invite INTEGER DEFAULT 1,
        can_pin INTEGER DEFAULT 1,
        can_change_info INTEGER DEFAULT 0,
        UNIQUE(group_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        description TEXT DEFAULT '',
        owner_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        avatar_url TEXT
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
        deleted INTEGER DEFAULT 0,
        media_url TEXT,
        media_type TEXT
    );

    CREATE TABLE IF NOT EXISTS channel_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER NOT NULL,
        sender_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deleted INTEGER DEFAULT 0,
        media_url TEXT,
        media_type TEXT
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

    CREATE TABLE IF NOT EXISTS polls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_id INTEGER NOT NULL,
        chat_type TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        question TEXT NOT NULL,
        options TEXT NOT NULL,
        created_at TEXT NOT NULL,
        message_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS poll_votes (
        poll_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        option_idx INTEGER NOT NULL,
        PRIMARY KEY (poll_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS stickers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        image_url TEXT NOT NULL,
        created_at TEXT NOT NULL,
        is_animated INTEGER DEFAULT 0,
        lottie_json TEXT
    );

    CREATE TABLE IF NOT EXISTS message_hides (
        user_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, message_id)
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

    CREATE TABLE IF NOT EXISTS privacy_settings (
        user_id INTEGER PRIMARY KEY,
        phone_visibility TEXT DEFAULT 'all',
        avatar_visibility TEXT DEFAULT 'all',
        last_seen_visibility TEXT DEFAULT 'all'
    );

    CREATE TABLE IF NOT EXISTS message_reactions (
        message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        emoji TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (message_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS message_edit_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER NOT NULL,
        old_text TEXT NOT NULL,
        edited_at TEXT NOT NULL,
        editor_id INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS pinned_chats (
        user_id INTEGER NOT NULL,
        peer_id INTEGER NOT NULL,
        peer_type TEXT NOT NULL DEFAULT 'user',
        pinned_at TEXT NOT NULL,
        UNIQUE(user_id, peer_id, peer_type)
    );

    CREATE TABLE IF NOT EXISTS muted_chats (
        user_id INTEGER NOT NULL,
        peer_id INTEGER NOT NULL,
        peer_type TEXT NOT NULL DEFAULT 'user',
        muted_until TEXT,
        UNIQUE(user_id, peer_id, peer_type)
    );

    CREATE TABLE IF NOT EXISTS archived_chats (
        user_id INTEGER NOT NULL,
        peer_id INTEGER NOT NULL,
        peer_type TEXT NOT NULL DEFAULT 'user',
        archived_at TEXT NOT NULL,
        UNIQUE(user_id, peer_id, peer_type)
    );

    CREATE TABLE IF NOT EXISTS group_slow_mode_log (
        group_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        last_message_at TEXT NOT NULL,
        PRIMARY KEY (group_id, user_id)
    );
    """)

    add_column_if_missing(connection, "users", "display_name", "TEXT")
    add_column_if_missing(connection, "users", "bio", "TEXT DEFAULT ''")
    add_column_if_missing(connection, "users", "avatar_url", "TEXT")
    add_column_if_missing(connection, "users", "is_bot", "INTEGER DEFAULT 0")
    add_column_if_missing(connection, "users", "is_verified", "INTEGER DEFAULT 0")
    add_column_if_missing(connection, "messages", "edited_at", "TEXT")
    add_column_if_missing(connection, "messages", "deleted", "INTEGER DEFAULT 0")
    add_column_if_missing(connection, "messages", "media_url", "TEXT")
    add_column_if_missing(connection, "messages", "media_type", "TEXT")
    add_column_if_missing(connection, "messages", "invite_id", "INTEGER")
    add_column_if_missing(connection, "messages", "invite_status", "TEXT")
    add_column_if_missing(connection, "messages", "forwarded_from", "INTEGER")
    add_column_if_missing(connection, "comments", "parent_id", "INTEGER")
    add_column_if_missing(connection, "groups", "avatar_url", "TEXT")
    add_column_if_missing(connection, "groups", "slow_mode_seconds", "INTEGER DEFAULT 0")
    add_column_if_missing(connection, "channels", "avatar_url", "TEXT")
    add_column_if_missing(connection, "group_messages", "media_url", "TEXT")
    add_column_if_missing(connection, "group_messages", "media_type", "TEXT")
    add_column_if_missing(connection, "channel_messages", "media_url", "TEXT")
    add_column_if_missing(connection, "channel_messages", "media_type", "TEXT")
    add_column_if_missing(connection, "sessions", "device_info", "TEXT")
    add_column_if_missing(connection, "posts", "repost_of", "INTEGER")
    add_column_if_missing(connection, "stickers", "is_animated", "INTEGER DEFAULT 0")
    add_column_if_missing(connection, "stickers", "lottie_json", "TEXT")

    for col in ("auto_answer", "mute_on_join", "camera_on_join"):
        try:
            connection.execute(f"ALTER TABLE settings ADD COLUMN {col} INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

    connection.execute("""
        UPDATE users SET display_name = username
        WHERE display_name IS NULL OR display_name = ''
    """)

    import hashlib as _hl
    from datetime import datetime as _dt
    _ts = _dt.utcnow().isoformat()
    _ph = _hl.sha256(b'__lumi_bot_internal__').hexdigest()

    bot = connection.execute("SELECT id FROM users WHERE username = 'lumi'").fetchone()
    if not bot:
        connection.execute("""
            INSERT INTO users
            (username, password_hash, created_at, last_seen, display_name, bio, is_bot, is_verified, avatar_url)
            VALUES ('lumi', ?, ?, ?, 'Lumi', 'Официальный бот Messenger Lumi', 1, 1, ?)
        """, (_ph, _ts, _ts, LUMI_AVATAR_URL))
    else:
        connection.execute("""
            UPDATE users
            SET is_bot = 1, is_verified = 1, display_name = 'Lumi', avatar_url = ?
            WHERE username = 'lumi'
        """, (LUMI_AVATAR_URL,))

    connection.commit()
    connection.close()


init_db()


# =========================================================
# HELPERS
# =========================================================

def now():
    return datetime.utcnow().isoformat() + "Z"


def hash_password(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def browser_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_token():
    return secrets.token_urlsafe(48)


def valid_username(username):
    return (
        len(username) >= 3 and len(username) <= 30 and
        bool(re.fullmatch(r"[a-zA-Z0-9_]+", username))
    )


def set_auth_cookie(response: Response, token: str, secure: bool = False):
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def set_browser_cookie(response: Response, browser_id: str, secure: bool = False):
    response.set_cookie(
        key=BROWSER_COOKIE,
        value=browser_id,
        max_age=365 * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def get_or_create_browser(request: Request, response: Response):
    value = request.cookies.get(BROWSER_COOKIE)
    if value:
        return value
    value = secrets.token_urlsafe(32)
    set_browser_cookie(response, value)
    return value


def create_session(user_id, browser_id, device_info=None):
    token = new_token()
    created = datetime.utcnow()
    expires = created + timedelta(days=SESSION_DAYS)
    connection = db()
    connection.execute("""
        INSERT INTO sessions
        (user_id, token_hash, browser_hash, device_info, created_at, last_seen, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, hash_token(token), browser_hash(browser_id),
          (device_info or "")[:200],
          created.isoformat(), created.isoformat(), expires.isoformat()))
    connection.commit()
    connection.close()
    return token


def get_auth_user(request: Request, update_last_seen: bool = True):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        raise HTTPException(401, "Не авторизован")

    connection = db()
    session = connection.execute("""
        SELECT s.id AS session_id, s.user_id, s.expires_at, u.username
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ?
    """, (hash_token(token),)).fetchone()

    if not session:
        connection.close()
        raise HTTPException(401, "Сессия недействительна")

    try:
        expires = datetime.fromisoformat(session["expires_at"])
    except Exception:
        expires = datetime.utcnow()

    if expires < datetime.utcnow():
        connection.execute("DELETE FROM sessions WHERE id = ?", (session["session_id"],))
        connection.commit()
        connection.close()
        raise HTTPException(401, "Сессия истекла")

    connection.execute("UPDATE sessions SET last_seen = ? WHERE id = ?",
                       (now(), session["session_id"]))
    if update_last_seen:
        connection.execute("UPDATE users SET last_seen = ? WHERE id = ?",
                           (now(), session["user_id"]))
    connection.commit()
    connection.close()
    return session["user_id"]


async def send_ws(user_id, payload):
    dead = []
    for socket in list(connections.get(user_id, set())):
        try:
            await socket.send_json(payload)
        except Exception:
            dead.append(socket)
    for socket in dead:
        connections[user_id].discard(socket)


def clear_deleted_for_me(connection, user_a, user_b):
    connection.execute("""
        UPDATE chat_settings SET deleted_for_me = 0
        WHERE (user_id = ? AND peer_id = ?) OR (user_id = ? AND peer_id = ?)
    """, (user_a, user_b, user_b, user_a))


def user_public(connection, user_id):
    row = connection.execute(
        "SELECT id, username, display_name, avatar_url, is_bot, is_verified, last_seen FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    return dict(row) if row else {}


def get_group_admin_perms(connection, group_id, user_id):
    g = connection.execute("SELECT owner_id FROM groups WHERE id = ?", (group_id,)).fetchone()
    if g and g["owner_id"] == user_id:
        return {
            "can_delete_messages": True,
            "can_ban_users": True,
            "can_invite": True,
            "can_pin": True,
            "can_change_info": True,
            "is_owner": True
        }
    adm = connection.execute(
        "SELECT * FROM group_admins WHERE group_id = ? AND user_id = ?",
        (group_id, user_id)
    ).fetchone()
    if not adm:
        return None
    return {
        "can_delete_messages": bool(adm["can_delete_messages"]),
        "can_ban_users": bool(adm["can_ban_users"]),
        "can_invite": bool(adm["can_invite"]),
        "can_pin": bool(adm["can_pin"]),
        "can_change_info": bool(adm["can_change_info"]),
        "is_owner": False
    }


# =========================================================
# MODELS
# =========================================================

class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""

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

class SettingsRequest(BaseModel):
    language: str = "ru"
    theme: str = "light"
    notifications: bool = True
    show_online: bool = True
    show_last_seen: bool = True
    auto_answer: bool = False
    mute_on_join: bool = False
    camera_on_join: bool = False

class GroupRequest(BaseModel):
    name: str
    description: str = ""

class ChannelRequest(BaseModel):
    name: str
    username: str
    description: str = ""

class GroupMessageRequest(BaseModel):
    text: str

class ChannelMessageRequest(BaseModel):
    text: str

class DeleteAccountRequest(BaseModel):
    password: str

class AliasRequest(BaseModel):
    alias: str

class WallpaperRequest(BaseModel):
    wallpaper_url: str = ""
    wallpaper_blur: bool = False

class RenameEntityRequest(BaseModel):
    name: str

class InviteActionRequest(BaseModel):
    action: str

class CallSignalRequest(BaseModel):
    target_id: int
    signal_type: str
    payload: dict = {}

class ReactionRequest(BaseModel):
    emoji: str

class MuteRequest(BaseModel):
    muted_until: str | None = None
    peer_type: str = "user"

class PinRequest(BaseModel):
    peer_type: str = "user"

class ArchiveRequest(BaseModel):
    peer_type: str = "user"

class SlowModeRequest(BaseModel):
    seconds: int

class AdminRequest(BaseModel):
    user_id: int
    can_delete_messages: bool = True
    can_ban_users: bool = True
    can_invite: bool = True
    can_pin: bool = True
    can_change_info: bool = False


# =========================================================
# AUTH
# =========================================================

@app.post("/api/register")
def register(data: RegisterRequest, request: Request, response: Response):
    username = data.username.strip().lower()
    if not valid_username(username):
        raise HTTPException(400, "Username: 3-30 символов, только буквы, цифры и _")
    if len(data.password) < 6:
        raise HTTPException(400, "Пароль должен содержать минимум 6 символов")

    connection = db()
    exists = connection.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if exists:
        connection.close()
        raise HTTPException(400, "Такой username уже занят")

    created = now()
    dn = (data.display_name or "").strip() or username
    cursor = connection.execute("""
        INSERT INTO users (username, password_hash, created_at, last_seen, display_name)
        VALUES (?, ?, ?, ?, ?)
    """, (username, hash_password(data.password), created, created, dn))
    user_id = cursor.lastrowid
    connection.execute("INSERT OR IGNORE INTO settings (user_id) VALUES (?)", (user_id,))
    connection.execute("INSERT OR IGNORE INTO privacy_settings (user_id) VALUES (?)", (user_id,))
    connection.commit()
    connection.close()

    browser_id = get_or_create_browser(request, response)
    ua = (request.headers.get("user-agent") or "")[:200]
    token = create_session(user_id, browser_id, ua)
    set_auth_cookie(response, token)
    return {"ok": True, "token": token}


@app.post("/api/login")
def login(data: LoginRequest, request: Request, response: Response):
    username = data.username.strip().lower()
    connection = db()
    user = connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user or user["password_hash"] != hash_password(data.password):
        connection.close()
        raise HTTPException(401, "Неверный логин или пароль")
    connection.execute("INSERT OR IGNORE INTO settings (user_id) VALUES (?)", (user["id"],))
    connection.execute("INSERT OR IGNORE INTO privacy_settings (user_id) VALUES (?)", (user["id"],))
    connection.commit()
    connection.close()

    browser_id = get_or_create_browser(request, response)
    ua = (request.headers.get("user-agent") or "")[:200]
    token = create_session(user["id"], browser_id, ua)
    set_auth_cookie(response, token)
    return {"ok": True, "token": token}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        connection = db()
        connection.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))
        connection.commit()
        connection.close()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    user_id = get_auth_user(request)
    connection = db()
    user = connection.execute("""
        SELECT id, username, display_name, bio, avatar_url, created_at, last_seen, is_bot, is_verified
        FROM users WHERE id = ?
    """, (user_id,)).fetchone()
    connection.close()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    return dict(user)


@app.delete("/api/account")
def delete_account(data: DeleteAccountRequest, request: Request, response: Response):
    user_id = get_auth_user(request, update_last_seen=False)
    connection = db()
    user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        connection.close()
        raise HTTPException(404, "Аккаунт не найден")
    if user["password_hash"] != hash_password(data.password):
        connection.close()
        raise HTTPException(403, "Неверный пароль")

    connection.execute("DELETE FROM messages WHERE sender_id = ? OR receiver_id = ?", (user_id, user_id))
    connection.execute("DELETE FROM favorites WHERE user_id = ?", (user_id,))
    connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    connection.execute("DELETE FROM settings WHERE user_id = ?", (user_id,))
    connection.execute("DELETE FROM privacy_settings WHERE user_id = ?", (user_id,))
    connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
    connection.commit()
    connection.close()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return {"ok": True}


# =========================================================
# PROFILE
# =========================================================

@app.put("/api/profile")
def update_profile(data: ProfileRequest, request: Request):
    user_id = get_auth_user(request)
    username = data.username.strip().lower()
    display_name = data.display_name.strip() or username
    bio = data.bio.strip()
    if not valid_username(username):
        raise HTTPException(400, "Некорректный username")
    connection = db()
    exists = connection.execute(
        "SELECT id FROM users WHERE username = ? AND id != ?", (username, user_id)
    ).fetchone()
    if exists:
        connection.close()
        raise HTTPException(400, "Этот username уже занят")
    connection.execute("""
        UPDATE users SET username = ?, display_name = ?, bio = ? WHERE id = ?
    """, (username, display_name, bio, user_id))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.post("/api/avatar")
async def upload_avatar(request: Request, file: UploadFile = File(...)):
    user_id = get_auth_user(request)
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if file.content_type not in allowed:
        raise HTTPException(400, "Разрешены JPG, PNG и WEBP")
    filename = f"avatar_{user_id}_{secrets.token_hex(8)}{allowed[file.content_type]}"
    path = UPLOAD_DIR / filename
    with open(path, "wb") as output:
        shutil.copyfileobj(file.file, output)
    url = "/uploads/" + filename
    connection = db()
    connection.execute("UPDATE users SET avatar_url = ? WHERE id = ?", (url, user_id))
    connection.commit()
    connection.close()
    return {"ok": True, "avatar_url": url}


# =========================================================
# USERS
# =========================================================

@app.get("/api/users/{user_id}")
def get_user_profile(user_id: int, request: Request):
    current_user_id = get_auth_user(request)
    connection = db()
    user = connection.execute("""
        SELECT id, username, display_name, bio, avatar_url, created_at, last_seen, is_bot, is_verified
        FROM users WHERE id = ?
    """, (user_id,)).fetchone()
    connection.close()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    result = dict(user)
    result["is_online"] = user_id in connections and len(connections.get(user_id, set())) > 0
    if user["is_bot"] or user["username"] == "lumi":
        result["created_at"] = None
        result["last_seen"] = None
        result["is_online"] = True
        return result
    if user_id == current_user_id:
        return result
    connection = db()
    blocked_me = connection.execute(
        "SELECT 1 FROM blocks WHERE user_id = ? AND blocked_id = ?",
        (user_id, current_user_id)
    ).fetchone()
    if blocked_me:
        result["avatar_url"] = None
        result["is_online"] = False
        result["last_seen"] = "1970-01-01T00:00:00Z"
        result["blocked_me"] = True
        result["created_at"] = None
        connection.close()
        return result
    result["created_at"] = None
    result["is_online"] = user_id in connections and len(connections.get(user_id, set())) > 0
    connection.close()
    return result


# =========================================================
# MESSAGES + REACTIONS + EDIT HISTORY + MEDIA GALLERY
# =========================================================

@app.get("/api/messages/{other_user_id}")
async def get_messages(other_user_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    messages = connection.execute("""
        SELECT
            m.id, m.sender_id, m.receiver_id,
            CASE WHEN m.deleted = 1 THEN '' ELSE m.text END AS text,
            m.created_at, m.edited_at, m.deleted, m.is_read,
            m.media_url, m.media_type, m.invite_id, m.invite_status, m.forwarded_from
        FROM messages m
        LEFT JOIN message_hides h ON h.message_id = m.id AND h.user_id = ?
        WHERE ((m.sender_id = ? AND m.receiver_id = ?) OR (m.sender_id = ? AND m.receiver_id = ?))
          AND h.message_id IS NULL
        ORDER BY m.id ASC
    """, (user_id, user_id, other_user_id, other_user_id, user_id)).fetchall()

    mark_read = True
    try:
        mr = request.query_params.get("mark_read", "1")
        mark_read = str(mr) not in ("0", "false", "False", "no")
    except Exception:
        pass

    unread_ids = []
    if mark_read:
        unread = connection.execute("""
            SELECT id FROM messages
            WHERE sender_id = ? AND receiver_id = ? AND is_read = 0
        """, (other_user_id, user_id)).fetchall()
        unread_ids = [r["id"] for r in unread]
        connection.execute("""
            UPDATE messages SET is_read = 1
            WHERE sender_id = ? AND receiver_id = ? AND is_read = 0
        """, (other_user_id, user_id))

    msg_ids = [m["id"] for m in messages]
    reactions_map = {}
    if msg_ids:
        placeholders = ",".join("?" * len(msg_ids))
        rows = connection.execute(f"""
            SELECT message_id, emoji, COUNT(*) as cnt, GROUP_CONCAT(user_id) as users
            FROM message_reactions
            WHERE message_id IN ({placeholders})
            GROUP BY message_id, emoji
        """, msg_ids).fetchall()
        for r in rows:
            mid = r["message_id"]
            if mid not in reactions_map:
                reactions_map[mid] = []
            reactions_map[mid].append({
                "emoji": r["emoji"],
                "count": r["cnt"],
                "users": [int(x) for x in (r["users"] or "").split(",") if x]
            })

    connection.commit()
    connection.close()

    if unread_ids:
        await send_ws(other_user_id, {
            "type": "messages_read",
            "reader_id": user_id,
            "message_ids": unread_ids
        })

    result = []
    for m in messages:
        d = dict(m)
        d["reactions"] = reactions_map.get(m["id"], [])
        result.append(d)
    return result


@app.post("/api/messages")
async def send_message(data: MessageRequest, request: Request):
    sender_id = get_auth_user(request)
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if len(text) > 5000:
        raise HTTPException(400, "Сообщение слишком длинное")

    is_self = sender_id == data.receiver_id
    connection = db()
    receiver = connection.execute(
        "SELECT id, username, is_bot FROM users WHERE id = ?", (data.receiver_id,)
    ).fetchone()
    if not receiver:
        connection.close()
        raise HTTPException(404, "Пользователь не найден")

    if not is_self and (receiver["is_bot"] or receiver["username"] == "lumi"):
        connection.close()
        raise HTTPException(403, "Боту нельзя писать")

    if not is_self:
        blocked = connection.execute("""
            SELECT 1 FROM blocks
            WHERE (user_id = ? AND blocked_id = ?) OR (user_id = ? AND blocked_id = ?)
        """, (sender_id, data.receiver_id, data.receiver_id, sender_id)).fetchone()
        if blocked:
            connection.close()
            raise HTTPException(403, "Пользователь заблокирован")

    created = now()
    cursor = connection.execute("""
        INSERT INTO messages (sender_id, receiver_id, text, created_at)
        VALUES (?, ?, ?, ?)
    """, (sender_id, data.receiver_id, text, created))
    message_id = cursor.lastrowid
    if not is_self:
        clear_deleted_for_me(connection, sender_id, data.receiver_id)
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
        "reactions": [],
    }
    payload = {"type": "message", "message": message}
    await send_ws(sender_id, payload)
    if not is_self:
        await send_ws(data.receiver_id, payload)
    return {"ok": True, "message": message}


@app.put("/api/messages/{message_id}")
async def edit_message(message_id: int, data: EditMessageRequest, request: Request):
    user_id = get_auth_user(request)
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "Пустой текст")

    connection = db()
    msg = connection.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not msg:
        connection.close()
        raise HTTPException(404, "Сообщение не найдено")
    if msg["sender_id"] != user_id:
        connection.close()
        raise HTTPException(403, "Можно редактировать только свои сообщения")

    connection.execute("""
        INSERT INTO message_edit_history (message_id, old_text, edited_at, editor_id)
        VALUES (?, ?, ?, ?)
    """, (message_id, msg["text"], now(), user_id))
    connection.execute("""
        UPDATE messages SET text = ?, edited_at = ? WHERE id = ?
    """, (text, now(), message_id))
    connection.commit()
    connection.close()

    payload = {
        "type": "message_edited",
        "message_id": message_id,
        "text": text,
        "edited_at": now(),
        "sender_id": user_id,
        "receiver_id": msg["receiver_id"]
    }
    await send_ws(msg["sender_id"], payload)
    await send_ws(msg["receiver_id"], payload)
    return {"ok": True, "text": text, "edited_at": now()}


@app.get("/api/messages/{message_id}/history")
def get_edit_history(message_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    msg = connection.execute("SELECT sender_id, receiver_id FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not msg or (msg["sender_id"] != user_id and msg["receiver_id"] != user_id):
        connection.close()
        raise HTTPException(403, "Нет доступа")
    rows = connection.execute("""
        SELECT id, old_text, edited_at, editor_id
        FROM message_edit_history
        WHERE message_id = ?
        ORDER BY id DESC
    """, (message_id,)).fetchall()
    connection.close()
    return [dict(r) for r in rows]


@app.post("/api/messages/{message_id}/reactions")
async def set_reaction(message_id: int, data: ReactionRequest, request: Request):
    user_id = get_auth_user(request)
    emoji = data.emoji.strip()
    allowed = {"👍", "😂", "😮", "❤️", "😡", "👏", "🔥", "😢", "🎉", "💩"}
    if emoji not in allowed:
        raise HTTPException(400, "Недопустимая реакция")

    connection = db()
    msg = connection.execute("SELECT sender_id, receiver_id FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not msg:
        connection.close()
        raise HTTPException(404, "Сообщение не найдено")

    existing = connection.execute(
        "SELECT emoji FROM message_reactions WHERE message_id = ? AND user_id = ?",
        (message_id, user_id)
    ).fetchone()

    if existing and existing["emoji"] == emoji:
        connection.execute(
            "DELETE FROM message_reactions WHERE message_id = ? AND user_id = ?",
            (message_id, user_id)
        )
        action = "removed"
    else:
        connection.execute("""
            INSERT INTO message_reactions (message_id, user_id, emoji, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(message_id, user_id) DO UPDATE SET emoji = excluded.emoji, created_at = excluded.created_at
        """, (message_id, user_id, emoji, now()))
        action = "added"

    rows = connection.execute("""
        SELECT emoji, COUNT(*) as cnt, GROUP_CONCAT(user_id) as users
        FROM message_reactions WHERE message_id = ?
        GROUP BY emoji
    """, (message_id,)).fetchall()
    reactions = [{"emoji": r["emoji"], "count": r["cnt"], "users": [int(x) for x in (r["users"] or "").split(",") if x]} for r in rows]
    connection.commit()
    connection.close()

    payload = {
        "type": "reaction",
        "message_id": message_id,
        "user_id": user_id,
        "emoji": emoji,
        "action": action,
        "reactions": reactions
    }
    await send_ws(msg["sender_id"], payload)
    await send_ws(msg["receiver_id"], payload)
    return {"ok": True, "reactions": reactions}


@app.get("/api/chats/{peer_id}/media")
def get_chat_media(peer_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    rows = connection.execute("""
        SELECT id, media_url, media_type, created_at, sender_id, text
        FROM messages
        WHERE ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?))
          AND media_url IS NOT NULL AND deleted = 0
        ORDER BY id DESC
        LIMIT 200
    """, (user_id, peer_id, peer_id, user_id)).fetchall()
    connection.close()
    return [dict(r) for r in rows]


# =========================================================
# PIN / MUTE / ARCHIVE
# =========================================================

@app.post("/api/chats/{peer_id}/pin")
def pin_chat(peer_id: int, data: PinRequest, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute("""
        INSERT INTO pinned_chats (user_id, peer_id, peer_type, pinned_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, peer_id, peer_type) DO UPDATE SET pinned_at = excluded.pinned_at
    """, (user_id, peer_id, data.peer_type, now()))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.delete("/api/chats/{peer_id}/pin")
def unpin_chat(peer_id: int, peer_type: str = "user", request: Request = None):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute(
        "DELETE FROM pinned_chats WHERE user_id = ? AND peer_id = ? AND peer_type = ?",
        (user_id, peer_id, peer_type)
    )
    connection.commit()
    connection.close()
    return {"ok": True}


@app.post("/api/chats/{peer_id}/mute")
def mute_chat(peer_id: int, data: MuteRequest, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute("""
        INSERT INTO muted_chats (user_id, peer_id, peer_type, muted_until)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, peer_id, peer_type) DO UPDATE SET muted_until = excluded.muted_until
    """, (user_id, peer_id, data.peer_type, data.muted_until))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.delete("/api/chats/{peer_id}/mute")
def unmute_chat(peer_id: int, peer_type: str = "user", request: Request = None):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute(
        "DELETE FROM muted_chats WHERE user_id = ? AND peer_id = ? AND peer_type = ?",
        (user_id, peer_id, peer_type)
    )
    connection.commit()
    connection.close()
    return {"ok": True}


@app.post("/api/chats/{peer_id}/archive")
def archive_chat(peer_id: int, data: ArchiveRequest, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute("""
        INSERT INTO archived_chats (user_id, peer_id, peer_type, archived_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, peer_id, peer_type) DO UPDATE SET archived_at = excluded.archived_at
    """, (user_id, peer_id, data.peer_type, now()))
    connection.execute(
        "DELETE FROM pinned_chats WHERE user_id = ? AND peer_id = ? AND peer_type = ?",
        (user_id, peer_id, data.peer_type)
    )
    connection.commit()
    connection.close()
    return {"ok": True}


@app.delete("/api/chats/{peer_id}/archive")
def unarchive_chat(peer_id: int, peer_type: str = "user", request: Request = None):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute(
        "DELETE FROM archived_chats WHERE user_id = ? AND peer_id = ? AND peer_type = ?",
        (user_id, peer_id, peer_type)
    )
    connection.commit()
    connection.close()
    return {"ok": True}


# =========================================================
# DIALOGS
# =========================================================

@app.get("/api/dialogs")
def get_dialogs(request: Request, archived: bool = False):
    user_id = get_auth_user(request)
    connection = db()

    arch_join = """
        LEFT JOIN archived_chats ac ON ac.user_id = ? AND ac.peer_id = CASE WHEN m.sender_id = ? THEN m.receiver_id ELSE m.sender_id END AND ac.peer_type = 'user'
    """
    arch_where = "AND ac.peer_id IS NOT NULL" if archived else "AND ac.peer_id IS NULL"

    rows = connection.execute(f"""
        SELECT
            CASE WHEN m.sender_id = ? THEN m.receiver_id ELSE m.sender_id END AS peer_id,
            MAX(m.id) AS last_id
        FROM messages m
        LEFT JOIN chat_settings cs ON cs.user_id = ?
            AND cs.peer_id = CASE WHEN m.sender_id = ? THEN m.receiver_id ELSE m.sender_id END
        {arch_join}
        WHERE (m.sender_id = ? OR m.receiver_id = ?)
            AND IFNULL(cs.deleted_for_me, 0) = 0
            {arch_where}
        GROUP BY peer_id
        ORDER BY last_id DESC
        LIMIT 100
    """, (user_id, user_id, user_id, user_id, user_id, user_id, user_id, user_id)).fetchall()

    self_last = connection.execute("""
        SELECT MAX(id) AS last_id FROM messages
        WHERE sender_id = ? AND receiver_id = ?
    """, (user_id, user_id)).fetchone()
    if self_last and self_last["last_id"]:
        if not any(r["peer_id"] == user_id for r in rows):
            rows = list(rows)
            rows.insert(0, {"peer_id": user_id, "last_id": self_last["last_id"]})

    result = []
    for row in rows:
        peer_id = row["peer_id"]
        user = connection.execute("""
            SELECT id, username, display_name, avatar_url, is_bot, is_verified, last_seen
            FROM users WHERE id = ?
        """, (peer_id,)).fetchone()
        if not user:
            continue
        last = connection.execute("""
            SELECT id, text, created_at, sender_id, media_type, deleted, is_read
            FROM messages WHERE id = ?
        """, (row["last_id"],)).fetchone()
        alias = connection.execute("""
            SELECT alias FROM contact_aliases
            WHERE user_id = ? AND contact_id = ?
        """, (user_id, peer_id)).fetchone()
        unread = connection.execute("""
            SELECT COUNT(*) AS c FROM messages
            WHERE sender_id = ? AND receiver_id = ? AND is_read = 0 AND deleted = 0
        """, (peer_id, user_id)).fetchone()["c"]
        pinned = connection.execute(
            "SELECT 1 FROM pinned_chats WHERE user_id = ? AND peer_id = ? AND peer_type = 'user'",
            (user_id, peer_id)
        ).fetchone()
        muted = connection.execute(
            "SELECT muted_until FROM muted_chats WHERE user_id = ? AND peer_id = ? AND peer_type = 'user'",
            (user_id, peer_id)
        ).fetchone()

        item = dict(user)
        item["alias"] = alias["alias"] if alias else None
        item["last_message"] = dict(last) if last else None
        item["unread"] = unread
        item["is_online"] = peer_id in connections and len(connections.get(peer_id, set())) > 0
        item["is_pinned"] = bool(pinned)
        item["is_muted"] = bool(muted)
        item["is_self"] = peer_id == user_id
        result.append(item)

    result.sort(key=lambda x: (0 if x.get("is_pinned") else 1, -(x["last_message"]["id"] if x.get("last_message") else 0)))
    connection.close()
    return result


# =========================================================
# GROUP ADMINS + SLOW MODE
# =========================================================

@app.post("/api/groups/{group_id}/admins")
def add_group_admin(group_id: int, data: AdminRequest, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    g = connection.execute("SELECT owner_id FROM groups WHERE id = ?", (group_id,)).fetchone()
    if not g or g["owner_id"] != user_id:
        connection.close()
        raise HTTPException(403, "Только владелец может назначать админов")
    connection.execute("""
        INSERT INTO group_admins (group_id, user_id, can_delete_messages, can_ban_users, can_invite, can_pin, can_change_info)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(group_id, user_id) DO UPDATE SET
            can_delete_messages = excluded.can_delete_messages,
            can_ban_users = excluded.can_ban_users,
            can_invite = excluded.can_invite,
            can_pin = excluded.can_pin,
            can_change_info = excluded.can_change_info
    """, (group_id, data.user_id, int(data.can_delete_messages), int(data.can_ban_users),
          int(data.can_invite), int(data.can_pin), int(data.can_change_info)))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.delete("/api/groups/{group_id}/admins/{admin_id}")
def remove_group_admin(group_id: int, admin_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    g = connection.execute("SELECT owner_id FROM groups WHERE id = ?", (group_id,)).fetchone()
    if not g or g["owner_id"] != user_id:
        connection.close()
        raise HTTPException(403, "Только владелец")
    connection.execute("DELETE FROM group_admins WHERE group_id = ? AND user_id = ?", (group_id, admin_id))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.get("/api/groups/{group_id}/admins")
def list_group_admins(group_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    member = connection.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id)
    ).fetchone()
    if not member:
        connection.close()
        raise HTTPException(403, "Нет доступа")
    rows = connection.execute("""
        SELECT ga.*, u.username, u.display_name, u.avatar_url
        FROM group_admins ga
        JOIN users u ON u.id = ga.user_id
        WHERE ga.group_id = ?
    """, (group_id,)).fetchall()
    connection.close()
    return [dict(r) for r in rows]


@app.put("/api/groups/{group_id}/slow-mode")
def set_slow_mode(group_id: int, data: SlowModeRequest, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    perms = get_group_admin_perms(connection, group_id, user_id)
    if not perms or (not perms.get("can_change_info") and not perms.get("is_owner")):
        connection.close()
        raise HTTPException(403, "Нет прав")
    seconds = max(0, min(86400, int(data.seconds)))
    connection.execute("UPDATE groups SET slow_mode_seconds = ? WHERE id = ?", (seconds, group_id))
    connection.commit()
    connection.close()
    return {"ok": True, "seconds": seconds}


@app.post("/api/groups/{group_id}/messages")
async def send_group_message(group_id: int, data: GroupMessageRequest, request: Request):
    sender_id = get_auth_user(request)
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")

    connection = db()
    member = connection.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, sender_id)
    ).fetchone()
    if not member:
        connection.close()
        raise HTTPException(403, "Нет доступа")

    g = connection.execute("SELECT slow_mode_seconds FROM groups WHERE id = ?", (group_id,)).fetchone()
    slow = g["slow_mode_seconds"] if g else 0
    if slow > 0:
        last = connection.execute(
            "SELECT last_message_at FROM group_slow_mode_log WHERE group_id = ? AND user_id = ?",
            (group_id, sender_id)
        ).fetchone()
        if last:
            try:
                last_dt = datetime.fromisoformat(last["last_message_at"].replace("Z", ""))
                if (datetime.utcnow() - last_dt).total_seconds() < slow:
                    connection.close()
                    raise HTTPException(429, f"Медленный режим: подождите {slow} сек.")
            except Exception:
                pass

    created = now()
    cursor = connection.execute("""
        INSERT INTO group_messages (group_id, sender_id, text, created_at)
        VALUES (?, ?, ?, ?)
    """, (group_id, sender_id, text, created))
    mid = cursor.lastrowid

    if slow > 0:
        connection.execute("""
            INSERT INTO group_slow_mode_log (group_id, user_id, last_message_at)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET last_message_at = excluded.last_message_at
        """, (group_id, sender_id, created))

    members = connection.execute(
        "SELECT user_id FROM group_members WHERE group_id = ?", (group_id,)
    ).fetchall()
    sender_info = user_public(connection, sender_id)
    group = connection.execute("SELECT name FROM groups WHERE id = ?", (group_id,)).fetchone()
    connection.commit()
    connection.close()

    message = {
        "id": mid,
        "group_id": group_id,
        "group_name": group["name"] if group else None,
        "sender_id": sender_id,
        "text": text,
        "created_at": created,
        "deleted": 0,
        "sender_username": sender_info.get("username"),
        "sender_name": sender_info.get("display_name") or sender_info.get("username"),
        "chat_kind": "group",
    }
    payload = {"type": "group_message", "message": message}
    for mrow in members:
        await send_ws(mrow["user_id"], payload)
    return {"ok": True, "message": message}


# =========================================================
# STICKERS
# =========================================================

@app.get("/api/stickers")
def list_stickers(request: Request):
    user_id = get_auth_user(request)
    connection = db()
    rows = connection.execute(
        "SELECT id, image_url, created_at, is_animated, lottie_json FROM stickers WHERE user_id = ? ORDER BY id DESC",
        (user_id,)
    ).fetchall()
    connection.close()
    return [dict(r) for r in rows]


@app.post("/api/stickers")
async def upload_sticker(request: Request, file: UploadFile = File(...), is_animated: bool = Form(False)):
    user_id = get_auth_user(request)
    import uuid as _uuid
    ext = (file.filename or "sticker.png").rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "gif", "webp", "json", "tgs"):
        ext = "png"
    name = f"sticker_{user_id}_{_uuid.uuid4().hex[:10]}.{ext}"
    path = os.path.join(UPLOAD_DIR, name)
    data = await file.read()
    with open(path, "wb") as out:
        out.write(data)
    url = "/uploads/" + name
    lottie = None
    if is_animated or ext in ("json", "tgs"):
        try:
            lottie = data.decode("utf-8") if ext == "json" else None
            is_animated = True
        except Exception:
            pass
    connection = db()
    cur = connection.execute(
        "INSERT INTO stickers (user_id, image_url, created_at, is_animated, lottie_json) VALUES (?,?,?,?,?)",
        (user_id, url, now(), int(is_animated), lottie)
    )
    sid = cur.lastrowid
    connection.commit()
    connection.close()
    return {"ok": True, "id": sid, "image_url": url, "is_animated": is_animated}


@app.delete("/api/stickers/{sticker_id}")
def delete_sticker(sticker_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute("DELETE FROM stickers WHERE id = ? AND user_id = ?", (sticker_id, user_id))
    connection.commit()
    connection.close()
    return {"ok": True}


# =========================================================
# MEDIA SEND
# =========================================================

@app.post("/api/messages/media")
async def send_message_media(request: Request, receiver_id: int, file: UploadFile = File(...),
                             text: str = Form("")):
    sender_id = get_auth_user(request)
    connection = db()
    blocked = connection.execute("""
        SELECT 1 FROM blocks
        WHERE (user_id = ? AND blocked_id = ?) OR (user_id = ? AND blocked_id = ?)
    """, (sender_id, receiver_id, receiver_id, sender_id)).fetchone()
    peer = connection.execute("SELECT is_bot, username FROM users WHERE id = ?", (receiver_id,)).fetchone()
    connection.close()
    if blocked:
        raise HTTPException(403, "Пользователь заблокирован")
    if peer and (peer["is_bot"] or peer["username"] == "lumi") and sender_id != receiver_id:
        raise HTTPException(403, "Боту нельзя писать")

    ctype = (file.content_type or "application/octet-stream").split(";")[0].strip().lower()
    name = file.filename or "file.bin"
    ext = Path(name).suffix.lower() or ".bin"

    if "video_note" in (name or "").lower():
        media_type = "video_note"
        ext = ".webm"
    elif ctype.startswith("image/") or ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        media_type = "image"
        ext = ".jpg"
    elif ctype.startswith("video/") or ext in {".mp4", ".mov", ".mkv"}:
        media_type = "video"
        ext = ext if ext in {".mp4", ".webm", ".mov"} else ".mp4"
    elif ctype.startswith("audio/") or ext in {".webm", ".ogg", ".mp3", ".m4a", ".wav", ".opus"}:
        media_type = "voice"
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
    cursor = connection.execute("""
        INSERT INTO messages (sender_id, receiver_id, text, created_at, media_url, media_type)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (sender_id, receiver_id, (text or "").strip(), created, url, media_type))
    mid = cursor.lastrowid
    clear_deleted_for_me(connection, sender_id, receiver_id)
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
        "reactions": [],
    }
    payload = {"type": "message", "message": message}
    await send_ws(sender_id, payload)
    if sender_id != receiver_id:
        await send_ws(receiver_id, payload)
    return {"ok": True, "message": message}


# =========================================================
# CALLS + PRESENCE + WS
# =========================================================

@app.post("/api/calls/signal")
async def call_signal(data: CallSignalRequest, request: Request):
    user_id = get_auth_user(request)
    payload = {
        "type": "call_signal",
        "from_id": user_id,
        "signal_type": data.signal_type,
        "payload": data.payload,
    }
    await send_ws(data.target_id, payload)
    return {"ok": True}


@app.post("/api/presence")
async def set_presence(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    token = data.get("token") or request.query_params.get("token")
    user_id = None
    if token and not request.cookies.get(SESSION_COOKIE):
        connection = db()
        session = connection.execute("""
            SELECT s.id AS session_id, s.user_id, s.expires_at
            FROM sessions s WHERE s.token_hash = ?
        """, (hash_token(token),)).fetchone()
        if not session:
            connection.close()
            raise HTTPException(401, "Не авторизован")
        try:
            expires = datetime.fromisoformat(session["expires_at"])
        except Exception:
            expires = datetime.utcnow()
        if expires < datetime.utcnow():
            connection.close()
            raise HTTPException(401, "Сессия истекла")
        user_id = session["user_id"]
        connection.close()
    else:
        user_id = get_auth_user(request, update_last_seen=False)

    online = bool(data.get("online", True))
    live_ws = len(connections.get(user_id, set()))
    if online:
        await broadcast_presence(user_id, True)
    else:
        if live_ws <= 1:
            await broadcast_presence(user_id, False)
    connection = db()
    connection.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now(), user_id))
    connection.commit()
    connection.close()
    return {"ok": True, "online": online if online else (live_ws > 1)}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=1008)
        return
    connection = db()
    session = connection.execute("""
        SELECT id, user_id, expires_at FROM sessions WHERE token_hash = ?
    """, (hash_token(token),)).fetchone()
    if not session:
        connection.close()
        await websocket.close(code=1008)
        return
    try:
        expires = datetime.fromisoformat(session["expires_at"])
    except Exception:
        expires = datetime.utcnow()
    if expires < datetime.utcnow():
        connection.close()
        await websocket.close(code=1008)
        return
    user_id = session["user_id"]
    connection.close()

    await websocket.accept()
    connections[user_id].add(websocket)
    try:
        await broadcast_presence(user_id, True)
    except Exception:
        pass
    try:
        connection = db()
        connection.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now(), user_id))
        connection.commit()
        connection.close()
    except Exception:
        pass

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            if msg_type == "ping":
                connection = db()
                connection.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now(), user_id))
                connection.commit()
                connection.close()
                await websocket.send_json({"type": "pong"})
            elif msg_type == "typing":
                target = data.get("target_id")
                if target:
                    await send_ws(int(target), {"type": "typing", "user_id": user_id})
            elif msg_type == "typing_stop":
                target = data.get("target_id")
                if target:
                    await send_ws(int(target), {"type": "typing_stop", "user_id": user_id})
            elif msg_type == "presence":
                await broadcast_presence(user_id, bool(data.get("online", True)))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        connections[user_id].discard(websocket)
        if not connections[user_id]:
            connections.pop(user_id, None)
            try:
                await broadcast_presence(user_id, False)
            except Exception:
                pass
            try:
                connection = db()
                connection.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now(), user_id))
                connection.commit()
                connection.close()
            except Exception:
                pass


async def broadcast_presence(user_id: int, online: bool):
    connection = db()
    peers = connection.execute("""
        SELECT DISTINCT CASE WHEN sender_id = ? THEN receiver_id ELSE sender_id END AS peer_id
        FROM messages
        WHERE sender_id = ? OR receiver_id = ?
        LIMIT 200
    """, (user_id, user_id, user_id)).fetchall()
    connection.close()
    payload = {
        "type": "presence",
        "user_id": user_id,
        "online": online,
        "last_seen": now(),
    }
    sent = set()
    for row in peers:
        pid = row["peer_id"]
        if pid and pid != user_id:
            sent.add(pid)
            await send_ws(pid, payload)
    for pid in list(connections.keys()):
        if pid != user_id and pid not in sent:
            await send_ws(pid, payload)



# =========================================================
# SETTINGS / PRIVACY
# =========================================================

@app.get("/api/settings")
def get_settings(request: Request):
    user_id = get_auth_user(request)
    connection = db()
    row = connection.execute("SELECT * FROM settings WHERE user_id = ?", (user_id,)).fetchone()
    connection.close()
    if not row:
        return {"language": "ru", "theme": "light", "notifications": True, "show_online": True, "show_last_seen": True}
    return {
        "language": row["language"],
        "theme": row["theme"] or "light",
        "notifications": bool(row["notifications"]),
        "show_online": bool(row["show_online"]),
        "show_last_seen": bool(row["show_last_seen"]),
        "auto_answer": bool(row["auto_answer"]) if "auto_answer" in row.keys() else False,
        "mute_on_join": bool(row["mute_on_join"]) if "mute_on_join" in row.keys() else False,
        "camera_on_join": bool(row["camera_on_join"]) if "camera_on_join" in row.keys() else False,
    }


@app.put("/api/settings")
def update_settings(data: SettingsRequest, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute("""
        INSERT INTO settings (user_id, language, theme, notifications, show_online, show_last_seen, auto_answer, mute_on_join, camera_on_join)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            language = excluded.language,
            theme = excluded.theme,
            notifications = excluded.notifications,
            show_online = excluded.show_online,
            show_last_seen = excluded.show_last_seen,
            auto_answer = excluded.auto_answer,
            mute_on_join = excluded.mute_on_join,
            camera_on_join = excluded.camera_on_join
    """, (user_id, data.language, data.theme, int(data.notifications), int(data.show_online),
          int(data.show_last_seen), int(data.auto_answer), int(data.mute_on_join), int(data.camera_on_join)))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.get("/api/privacy")
def get_privacy(request: Request):
    user_id = get_auth_user(request)
    connection = db()
    row = connection.execute("SELECT * FROM privacy_settings WHERE user_id = ?", (user_id,)).fetchone()
    connection.close()
    if not row:
        return {"phone_visibility": "all", "avatar_visibility": "all", "last_seen_visibility": "all"}
    return dict(row)


@app.put("/api/privacy")
def update_privacy(data: dict, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute("""
        INSERT INTO privacy_settings (user_id, phone_visibility, avatar_visibility, last_seen_visibility)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            phone_visibility = excluded.phone_visibility,
            avatar_visibility = excluded.avatar_visibility,
            last_seen_visibility = excluded.last_seen_visibility
    """, (user_id, data.get("phone_visibility", "all"), data.get("avatar_visibility", "all"), data.get("last_seen_visibility", "all")))
    connection.commit()
    connection.close()
    return {"ok": True}


# =========================================================
# GROUPS
# =========================================================

@app.get("/api/groups")
def list_groups(request: Request):
    user_id = get_auth_user(request)
    connection = db()
    rows = connection.execute("""
        SELECT g.*, 
            (SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.id) AS members_count
        FROM groups g
        JOIN group_members m ON m.group_id = g.id
        WHERE m.user_id = ?
        ORDER BY g.name
    """, (user_id,)).fetchall()
    connection.close()
    return [dict(r) for r in rows]


@app.post("/api/groups")
def create_group(data: GroupRequest, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    cur = connection.execute("""
        INSERT INTO groups (name, description, owner_id, created_at)
        VALUES (?, ?, ?, ?)
    """, (data.name.strip(), data.description.strip(), user_id, now()))
    gid = cur.lastrowid
    connection.execute("INSERT INTO group_members (group_id, user_id, joined_at) VALUES (?, ?, ?)", (gid, user_id, now()))
    connection.commit()
    connection.close()
    return {"ok": True, "id": gid}


@app.get("/api/groups/{group_id}/messages")
def get_group_messages(group_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    member = connection.execute("SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id)).fetchone()
    if not member:
        connection.close()
        raise HTTPException(403, "Нет доступа")
    rows = connection.execute("""
        SELECT gm.*, u.username, u.display_name, u.avatar_url
        FROM group_messages gm
        JOIN users u ON u.id = gm.sender_id
        WHERE gm.group_id = ? AND gm.deleted = 0
        ORDER BY gm.id ASC
        LIMIT 500
    """, (group_id,)).fetchall()
    connection.close()
    return [dict(r) for r in rows]


@app.post("/api/groups/{group_id}/leave")
def leave_group(group_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute("DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.put("/api/groups/{group_id}")
def rename_group(group_id: int, data: RenameEntityRequest, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    perms = get_group_admin_perms(connection, group_id, user_id)
    if not perms:
        connection.close()
        raise HTTPException(403, "Нет прав")
    connection.execute("UPDATE groups SET name = ? WHERE id = ?", (data.name.strip(), group_id))
    connection.commit()
    connection.close()
    return {"ok": True}


# =========================================================
# CHANNELS
# =========================================================

@app.get("/api/channels")
def list_channels(request: Request):
    user_id = get_auth_user(request)
    connection = db()
    rows = connection.execute("""
        SELECT c.*,
            CASE WHEN s.user_id IS NOT NULL THEN 1 ELSE 0 END AS joined,
            (SELECT COUNT(*) FROM channel_subscribers WHERE channel_id = c.id) AS subscribers
        FROM channels c
        LEFT JOIN channel_subscribers s ON s.channel_id = c.id AND s.user_id = ?
        ORDER BY c.name
        LIMIT 100
    """, (user_id,)).fetchall()
    connection.close()
    return [dict(r) for r in rows]


@app.post("/api/channels")
def create_channel(data: ChannelRequest, request: Request):
    user_id = get_auth_user(request)
    username = data.username.strip().lower()
    if not valid_username(username):
        raise HTTPException(400, "Некорректный username канала")
    connection = db()
    exists = connection.execute("SELECT id FROM channels WHERE username = ?", (username,)).fetchone()
    if exists:
        connection.close()
        raise HTTPException(400, "Username канала занят")
    cur = connection.execute("""
        INSERT INTO channels (name, username, description, owner_id, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (data.name.strip(), username, data.description.strip(), user_id, now()))
    cid = cur.lastrowid
    connection.execute("INSERT INTO channel_subscribers (channel_id, user_id, created_at) VALUES (?, ?, ?)", (cid, user_id, now()))
    connection.commit()
    connection.close()
    return {"ok": True, "id": cid}


@app.get("/api/channels/{channel_id}/messages")
def get_channel_messages(channel_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    rows = connection.execute("""
        SELECT cm.*, u.username, u.display_name
        FROM channel_messages cm
        JOIN users u ON u.id = cm.sender_id
        WHERE cm.channel_id = ? AND cm.deleted = 0
        ORDER BY cm.id ASC
        LIMIT 500
    """, (channel_id,)).fetchall()
    connection.close()
    return [dict(r) for r in rows]


@app.post("/api/channels/{channel_id}/join")
def join_channel(channel_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute("INSERT OR IGNORE INTO channel_subscribers (channel_id, user_id, created_at) VALUES (?, ?, ?)", (channel_id, user_id, now()))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.post("/api/channels/{channel_id}/leave")
def leave_channel(channel_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute("DELETE FROM channel_subscribers WHERE channel_id = ? AND user_id = ?", (channel_id, user_id))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.get("/api/channels/{channel_id}/info")
def channel_info(channel_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    ch = connection.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if not ch:
        connection.close()
        raise HTTPException(404, "Канал не найден")
    subs = connection.execute("SELECT COUNT(*) AS c FROM channel_subscribers WHERE channel_id = ?", (channel_id,)).fetchone()["c"]
    joined = connection.execute("SELECT 1 FROM channel_subscribers WHERE channel_id = ? AND user_id = ?", (channel_id, user_id)).fetchone()
    owner = connection.execute("SELECT username, display_name FROM users WHERE id = ?", (ch["owner_id"],)).fetchone()
    connection.close()
    return {
        **dict(ch),
        "subscribers": subs,
        "joined": bool(joined),
        "is_owner": ch["owner_id"] == user_id,
        "owner_name": (owner["display_name"] or owner["username"]) if owner else ""
    }


# =========================================================
# POSTS / FEED
# =========================================================

@app.get("/api/feed")
def get_feed(request: Request):
    user_id = get_auth_user(request)
    connection = db()
    rows = connection.execute("""
        SELECT p.*, u.username, u.display_name, u.avatar_url, u.is_verified,
            (SELECT COUNT(*) FROM post_likes WHERE post_id = p.id) AS likes,
            (SELECT COUNT(*) FROM comments WHERE post_id = p.id) AS comments_count,
            EXISTS(SELECT 1 FROM post_likes WHERE post_id = p.id AND user_id = ?) AS liked
        FROM posts p
        JOIN users u ON u.id = p.author_id
        ORDER BY p.id DESC
        LIMIT 50
    """, (user_id,)).fetchall()
    connection.close()
    return [dict(r) for r in rows]


@app.post("/api/posts")
def create_post(data: dict, request: Request):
    user_id = get_auth_user(request)
    text = (data.get("text") or "").strip()
    connection = db()
    cur = connection.execute("""
        INSERT INTO posts (author_id, text, created_at) VALUES (?, ?, ?)
    """, (user_id, text, now()))
    pid = cur.lastrowid
    connection.commit()
    connection.close()
    return {"ok": True, "id": pid}


@app.post("/api/posts/{post_id}/like")
def like_post(post_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    exists = connection.execute("SELECT 1 FROM post_likes WHERE post_id = ? AND user_id = ?", (post_id, user_id)).fetchone()
    if exists:
        connection.execute("DELETE FROM post_likes WHERE post_id = ? AND user_id = ?", (post_id, user_id))
        liked = False
    else:
        connection.execute("INSERT INTO post_likes (post_id, user_id) VALUES (?, ?)", (post_id, user_id))
        liked = True
    connection.commit()
    connection.close()
    return {"ok": True, "liked": liked}


@app.get("/api/posts/{post_id}/comments")
def get_comments(post_id: int, request: Request):
    get_auth_user(request)
    connection = db()
    rows = connection.execute("""
        SELECT c.*, u.username, u.display_name, u.avatar_url
        FROM comments c
        JOIN users u ON u.id = c.user_id
        WHERE c.post_id = ?
        ORDER BY c.id ASC
    """, (post_id,)).fetchall()
    connection.close()
    return [dict(r) for r in rows]


@app.post("/api/posts/{post_id}/comments")
def add_comment(post_id: int, data: dict, request: Request):
    user_id = get_auth_user(request)
    text = (data.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Пустой комментарий")
    connection = db()
    cur = connection.execute("""
        INSERT INTO comments (post_id, user_id, text, created_at, parent_id)
        VALUES (?, ?, ?, ?, ?)
    """, (post_id, user_id, text, now(), data.get("parent_id")))
    cid = cur.lastrowid
    connection.commit()
    connection.close()
    return {"ok": True, "id": cid}


# =========================================================
# SEARCH / FAVORITES / BLOCKS
# =========================================================

@app.get("/api/search")
def global_search(request: Request, q: str = ""):
    user_id = get_auth_user(request)
    q = q.strip()
    if not q:
        return {"users": [], "channels": [], "groups": [], "communities": []}
    like = "%" + q + "%"
    connection = db()
    users = connection.execute("""
        SELECT id, username, display_name, avatar_url, last_seen, is_bot, is_verified
        FROM users WHERE (username LIKE ? OR display_name LIKE ?) AND id != ?
        ORDER BY username LIMIT 20
    """, (like, like, user_id)).fetchall()
    channels = connection.execute("""
        SELECT c.id, c.name, c.username, c.description, c.owner_id, c.avatar_url,
            CASE WHEN s.user_id IS NOT NULL THEN 1 ELSE 0 END AS joined
        FROM channels c
        LEFT JOIN channel_subscribers s ON s.channel_id = c.id AND s.user_id = ?
        WHERE c.name LIKE ? OR c.username LIKE ?
        ORDER BY c.name LIMIT 20
    """, (user_id, like, like)).fetchall()
    groups = connection.execute("""
        SELECT g.id, g.name, g.description, g.owner_id
        FROM groups g
        JOIN group_members gm ON gm.group_id = g.id
        WHERE gm.user_id = ? AND (g.name LIKE ? OR g.description LIKE ?)
        ORDER BY g.name LIMIT 20
    """, (user_id, like, like)).fetchall()
    connection.close()
    return {
        "users": [dict(u) for u in users],
        "channels": [dict(c) for c in channels],
        "groups": [dict(g) for g in groups],
        "communities": []
    }


@app.get("/api/favorites")
def list_favorites(request: Request):
    user_id = get_auth_user(request)
    connection = db()
    rows = connection.execute("""
        SELECT m.*, u.username, u.display_name
        FROM favorites f
        JOIN messages m ON m.id = f.message_id
        JOIN users u ON u.id = m.sender_id
        WHERE f.user_id = ?
        ORDER BY f.id DESC
        LIMIT 100
    """, (user_id,)).fetchall()
    connection.close()
    return [dict(r) for r in rows]


@app.post("/api/favorites/{message_id}")
def add_favorite(message_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute("INSERT OR IGNORE INTO favorites (user_id, message_id) VALUES (?, ?)", (user_id, message_id))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.delete("/api/favorites/{message_id}")
def remove_favorite(message_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute("DELETE FROM favorites WHERE user_id = ? AND message_id = ?", (user_id, message_id))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.post("/api/users/{user_id}/block")
def block_user(user_id: int, request: Request):
    me = get_auth_user(request)
    connection = db()
    connection.execute("INSERT OR IGNORE INTO blocks (user_id, blocked_id, created_at) VALUES (?, ?, ?)", (me, user_id, now()))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.delete("/api/users/{user_id}/block")
def unblock_user(user_id: int, request: Request):
    me = get_auth_user(request)
    connection = db()
    connection.execute("DELETE FROM blocks WHERE user_id = ? AND blocked_id = ?", (me, user_id))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.put("/api/contacts/{contact_id}/alias")
def set_alias(contact_id: int, data: AliasRequest, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute("""
        INSERT INTO contact_aliases (user_id, contact_id, alias)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, contact_id) DO UPDATE SET alias = excluded.alias
    """, (user_id, contact_id, data.alias.strip()))
    connection.commit()
    connection.close()
    return {"ok": True, "alias": data.alias.strip()}


@app.delete("/api/chats/{peer_id}")
def delete_chat(peer_id: int, request: Request, for_both: bool = False):
    user_id = get_auth_user(request)
    connection = db()
    if for_both:
        connection.execute("""
            DELETE FROM messages
            WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)
        """, (user_id, peer_id, peer_id, user_id))
        connection.execute("""
            DELETE FROM chat_settings
            WHERE (user_id = ? AND peer_id = ?) OR (user_id = ? AND peer_id = ?)
        """, (user_id, peer_id, peer_id, user_id))
    else:
        connection.execute("""
            INSERT INTO chat_settings (user_id, peer_id, deleted_for_me)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, peer_id) DO UPDATE SET deleted_for_me = 1
        """, (user_id, peer_id))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.get("/api/chats/{peer_id}/settings")
def get_chat_settings(peer_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    row = connection.execute("""
        SELECT wallpaper_url, wallpaper_blur, deleted_for_me
        FROM chat_settings WHERE user_id = ? AND peer_id = ?
    """, (user_id, peer_id)).fetchone()
    alias = connection.execute("""
        SELECT alias FROM contact_aliases WHERE user_id = ? AND contact_id = ?
    """, (user_id, peer_id)).fetchone()
    blocked = connection.execute(
        "SELECT 1 FROM blocks WHERE user_id = ? AND blocked_id = ?", (user_id, peer_id)
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
    connection.execute("""
        INSERT INTO chat_settings (user_id, peer_id, wallpaper_url, wallpaper_blur)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, peer_id) DO UPDATE SET
            wallpaper_url = excluded.wallpaper_url,
            wallpaper_blur = excluded.wallpaper_blur
    """, (user_id, peer_id, data.wallpaper_url or None, int(data.wallpaper_blur)))
    connection.commit()
    connection.close()
    return {"ok": True}


@app.delete("/api/chats/{peer_id}/wallpaper")
def clear_wallpaper(peer_id: int, request: Request):
    user_id = get_auth_user(request)
    connection = db()
    connection.execute("""
        INSERT INTO chat_settings (user_id, peer_id, wallpaper_url, wallpaper_blur)
        VALUES (?, ?, NULL, 0)
        ON CONFLICT(user_id, peer_id) DO UPDATE SET wallpaper_url = NULL, wallpaper_blur = 0
    """, (user_id, peer_id))
    connection.commit()
    connection.close()
    return {"ok": True}


# =========================================================
# STATIC
# =========================================================

app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
