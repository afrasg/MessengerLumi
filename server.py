from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import hashlib
import secrets
import shutil
import re
import json
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
import asyncio

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = BASE_DIR / "messenger.db"
STATIC_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

SESSION_COOKIE = "mm_session"
SESSION_DAYS = 30
connections = defaultdict(set)
LUMI_AVATAR = "https://is1-ssl.mzstatic.com/image/thumb/PurpleSource221/v4/5c/e5/f3/5ce5f3be-c924-0649-5dba-309206c42ba6/Placeholder.mill/1200x630wa.jpg"

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def db():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def now():
    return datetime.utcnow().isoformat()

def hash_password(pwd):
    return hashlib.sha256(pwd.encode()).hexdigest()

def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()

def new_token():
    return secrets.token_urlsafe(48)

def valid_username(username):
    return bool(re.fullmatch(r"[a-zA-Z0-9_]{3,30}", username))

def set_auth_cookie(resp, token):
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS*24*60*60, httponly=True, samesite="lax", path="/")

def get_auth_user(req, update_last_seen=True):
    token = req.cookies.get(SESSION_COOKIE)
    if not token:
        auth = req.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        raise HTTPException(401, "Не авторизован")
    conn = db()
    session = conn.execute(
        "SELECT s.id, s.user_id, s.expires_at FROM sessions s WHERE s.token_hash = ?",
        (hash_token(token),)
    ).fetchone()
    if not session:
        conn.close()
        raise HTTPException(401, "Сессия недействительна")
    try:
        expires = datetime.fromisoformat(session["expires_at"])
    except:
        expires = datetime.utcnow()
    if expires < datetime.utcnow():
        conn.execute("DELETE FROM sessions WHERE id = ?", (session["id"],))
        conn.commit()
        conn.close()
        raise HTTPException(401, "Сессия истекла")
    conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (now(), session["id"]))
    if update_last_seen:
        conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now(), session["user_id"]))
    conn.commit()
    conn.close()
    return session["user_id"]

async def send_ws(user_id, payload):
    for sock in list(connections.get(user_id, set())):
        try:
            await sock.send_json(payload)
        except:
            pass

def init_db():
    conn = db()
    conn.executescript("""
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
            invite_status TEXT
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
            avatar_url TEXT
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
        CREATE TABLE IF NOT EXISTS privacy_settings (
            user_id INTEGER PRIMARY KEY,
            phone_visibility TEXT DEFAULT 'all',
            avatar_visibility TEXT DEFAULT 'all',
            last_seen_visibility TEXT DEFAULT 'all'
        );
    """)
    # Добавляем новые колонки если их нет
    try:
        conn.execute("ALTER TABLE settings ADD COLUMN auto_answer INTEGER DEFAULT 0")
    except: pass
    try:
        conn.execute("ALTER TABLE settings ADD COLUMN mute_on_join INTEGER DEFAULT 0")
    except: pass
    try:
        conn.execute("ALTER TABLE settings ADD COLUMN camera_on_join INTEGER DEFAULT 0")
    except: pass

    # Создаём бота Lumi
    import hashlib as _hl
    _ts = datetime.utcnow().isoformat()
    _ph = _hl.sha256(b'__lumi_bot_internal__').hexdigest()
    bot = conn.execute("SELECT id FROM users WHERE username = 'lumi'").fetchone()
    if not bot:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at, last_seen, display_name, bio, is_bot, is_verified, avatar_url) VALUES ('lumi', ?, ?, ?, 'Lumi', 'Официальный бот', 1, 1, ?)",
            (_ph, _ts, _ts, LUMI_AVATAR)
        )
    else:
        conn.execute("UPDATE users SET is_bot=1, is_verified=1, display_name='Lumi', avatar_url=? WHERE username='lumi'", (LUMI_AVATAR,))
    conn.commit()
    conn.close()

init_db()

# ============================================================
# MODELS
# ============================================================
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
    auto_answer: bool = False
    mute_on_join: bool = False
    camera_on_join: bool = False

class PrivacySettingsRequest(BaseModel):
    phone_visibility: str = "all"
    avatar_visibility: str = "all"
    last_seen_visibility: str = "all"

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
    action: str

class CallSignalRequest(BaseModel):
    target_id: int
    signal_type: str
    payload: dict = {}

# ============================================================
# AUTH
# ============================================================
@app.post("/api/register")
def register(data: RegisterRequest, req: Request, res: Response):
    username = data.username.strip().lower()
    if not valid_username(username):
        raise HTTPException(400, "Username: 3-30 символов, буквы, цифры, _")
    if len(data.password) < 6:
        raise HTTPException(400, "Пароль минимум 6 символов")
    conn = db()
    if conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
        conn.close()
        raise HTTPException(400, "Username занят")
    created = now()
    cursor = conn.execute(
        "INSERT INTO users (username, password_hash, created_at, last_seen, display_name) VALUES (?, ?, ?, ?, ?)",
        (username, hash_password(data.password), created, created, username)
    )
    user_id = cursor.lastrowid
    conn.execute("INSERT OR IGNORE INTO settings (user_id) VALUES (?)", (user_id,))
    conn.execute("INSERT OR IGNORE INTO privacy_settings (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
    token = new_token()
    conn = db()
    conn.execute(
        "INSERT INTO sessions (user_id, token_hash, browser_hash, created_at, last_seen, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, hash_token(token), "browser", created, created, (datetime.utcnow()+timedelta(days=SESSION_DAYS)).isoformat())
    )
    conn.commit()
    conn.close()
    set_auth_cookie(res, token)
    return {"ok": True, "token": token}

@app.post("/api/login")
def login(data: LoginRequest, req: Request, res: Response):
    username = data.username.strip().lower()
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user or user["password_hash"] != hash_password(data.password):
        conn.close()
        raise HTTPException(401, "Неверный логин или пароль")
    conn.execute("INSERT OR IGNORE INTO settings (user_id) VALUES (?)", (user["id"],))
    conn.execute("INSERT OR IGNORE INTO privacy_settings (user_id) VALUES (?)", (user["id"],))
    conn.commit()
    conn.close()
    token = new_token()
    conn = db()
    conn.execute(
        "INSERT INTO sessions (user_id, token_hash, browser_hash, created_at, last_seen, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"], hash_token(token), "browser", now(), now(), (datetime.utcnow()+timedelta(days=SESSION_DAYS)).isoformat())
    )
    conn.commit()
    conn.close()
    set_auth_cookie(res, token)
    return {"ok": True, "token": token}

@app.post("/api/logout")
def logout(req: Request, res: Response):
    token = req.cookies.get(SESSION_COOKIE)
    if token:
        conn = db()
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))
        conn.commit()
        conn.close()
    res.delete_cookie(SESSION_COOKIE)
    return {"ok": True}

@app.get("/api/me")
def me(req: Request):
    user_id = get_auth_user(req)
    conn = db()
    user = conn.execute("SELECT id, username, display_name, bio, avatar_url, created_at, last_seen, is_bot, is_verified FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(user)

@app.delete("/api/account")
def delete_account(data: DeleteAccountRequest, req: Request, res: Response):
    user_id = get_auth_user(req)
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user or user["password_hash"] != hash_password(data.password):
        conn.close()
        raise HTTPException(403, "Неверный пароль")
    conn.execute("DELETE FROM messages WHERE sender_id = ? OR receiver_id = ?", (user_id, user_id))
    conn.execute("DELETE FROM favorites WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM posts WHERE author_id = ?", (user_id,))
    conn.execute("DELETE FROM comments WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM post_likes WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM group_members WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM channel_subscribers WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM settings WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM privacy_settings WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM groups WHERE owner_id = ?", (user_id,))
    conn.execute("DELETE FROM channels WHERE owner_id = ?", (user_id,))
    conn.execute("DELETE FROM communities WHERE owner_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    res.delete_cookie(SESSION_COOKIE)
    return {"ok": True}

# ============================================================
# PROFILE
# ============================================================
@app.put("/api/profile")
def update_profile(data: ProfileRequest, req: Request):
    user_id = get_auth_user(req)
    username = data.username.strip().lower()
    display_name = data.display_name.strip()
    bio = data.bio.strip()
    if not valid_username(username):
        raise HTTPException(400, "Некорректный username")
    if not display_name:
        display_name = username
    conn = db()
    if conn.execute("SELECT id FROM users WHERE username = ? AND id != ?", (username, user_id)).fetchone():
        conn.close()
        raise HTTPException(400, "Username занят")
    conn.execute("UPDATE users SET username = ?, display_name = ?, bio = ? WHERE id = ?", (username, display_name, bio, user_id))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/avatar")
async def upload_avatar(req: Request, file: UploadFile = File(...)):
    user_id = get_auth_user(req)
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if file.content_type not in allowed:
        raise HTTPException(400, "Только JPG, PNG, WEBP")
    filename = f"avatar_{user_id}_{secrets.token_hex(8)}{allowed[file.content_type]}"
    path = UPLOAD_DIR / filename
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    url = "/uploads/" + filename
    conn = db()
    conn.execute("UPDATE users SET avatar_url = ? WHERE id = ?", (url, user_id))
    conn.commit()
    conn.close()
    return {"ok": True, "avatar_url": url}

# ============================================================
# USERS
# ============================================================
@app.get("/api/users/{user_id}")
def get_user_profile(user_id: int, req: Request):
    current_user_id = get_auth_user(req)
    conn = db()
    user = conn.execute("SELECT id, username, display_name, bio, avatar_url, created_at, last_seen, is_bot, is_verified FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    result = dict(user)
    if user["is_bot"] or user["username"] == "lumi":
        result["created_at"] = None
        result["last_seen"] = None
        return result
    if user_id == current_user_id:
        return result
    conn = db()
    privacy = conn.execute("SELECT last_seen_visibility FROM privacy_settings WHERE user_id = ?", (user_id,)).fetchone()
    has_dialog = conn.execute("SELECT 1 FROM messages WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?) LIMIT 1", (current_user_id, user_id, user_id, current_user_id)).fetchone()
    conn.close()
    visibility = privacy["last_seen_visibility"] if privacy else "all"
    if visibility == "none" or (visibility == "contacts" and not has_dialog):
        result["last_seen"] = None
    result["created_at"] = None
    return result

# ============================================================
# MESSAGES
# ============================================================
@app.get("/api/messages/{other_user_id}")
def get_messages(other_user_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    messages = conn.execute(
        "SELECT id, sender_id, receiver_id, CASE WHEN deleted=1 THEN '' ELSE text END AS text, created_at, edited_at, deleted, is_read, media_url, media_type, invite_id, invite_status FROM messages WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?) ORDER BY id ASC",
        (user_id, other_user_id, other_user_id, user_id)
    ).fetchall()
    conn.execute("UPDATE messages SET is_read = 1 WHERE sender_id = ? AND receiver_id = ? AND is_read = 0", (other_user_id, user_id))
    conn.commit()
    conn.close()
    return [dict(m) for m in messages]

@app.post("/api/messages")
async def send_message(data: MessageRequest, req: Request):
    sender_id = get_auth_user(req)
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if len(text) > 5000:
        raise HTTPException(400, "Слишком длинное")
    if sender_id == data.receiver_id:
        raise HTTPException(400, "Нельзя себе")
    conn = db()
    receiver = conn.execute("SELECT id, is_bot, username FROM users WHERE id = ?", (data.receiver_id,)).fetchone()
    if not receiver:
        conn.close()
        raise HTTPException(404, "Пользователь не найден")
    if receiver["is_bot"] or receiver["username"] == "lumi":
        conn.close()
        raise HTTPException(403, "Боту нельзя писать")
    blocked = conn.execute("SELECT 1 FROM blocks WHERE (user_id = ? AND blocked_id = ?) OR (user_id = ? AND blocked_id = ?)", (sender_id, data.receiver_id, data.receiver_id, sender_id)).fetchone()
    if blocked:
        conn.close()
        raise HTTPException(403, "Заблокирован")
    created = now()
    cursor = conn.execute("INSERT INTO messages (sender_id, receiver_id, text, created_at) VALUES (?, ?, ?, ?)", (sender_id, data.receiver_id, text, created))
    mid = cursor.lastrowid
    conn.commit()
    conn.close()
    sender = conn.execute("SELECT username, display_name FROM users WHERE id = ?", (sender_id,)).fetchone()
    conn.close()
    msg = {"id": mid, "sender_id": sender_id, "receiver_id": data.receiver_id, "text": text, "created_at": created, "edited_at": None, "deleted": 0, "is_read": 0, "sender_username": sender["username"], "sender_name": sender["display_name"] or sender["username"], "chat_kind": "private"}
    payload = {"type": "message", "message": msg}
    await send_ws(sender_id, payload)
    await send_ws(data.receiver_id, payload)
    return {"ok": True, "message": msg}

@app.put("/api/messages/{message_id}")
async def edit_message(message_id: int, data: EditMessageRequest, req: Request):
    user_id = get_auth_user(req)
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    conn = db()
    msg = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not msg:
        conn.close()
        raise HTTPException(404, "Не найдено")
    if msg["sender_id"] != user_id:
        conn.close()
        raise HTTPException(403, "Не ваше")
    if msg["deleted"]:
        conn.close()
        raise HTTPException(400, "Удалено")
    edited = now()
    conn.execute("UPDATE messages SET text = ?, edited_at = ? WHERE id = ?", (text, edited, message_id))
    conn.commit()
    conn.close()
    updated = {"id": message_id, "sender_id": msg["sender_id"], "receiver_id": msg["receiver_id"], "text": text, "created_at": msg["created_at"], "edited_at": edited, "deleted": 0}
    payload = {"type": "message_updated", "message": updated}
    await send_ws(msg["sender_id"], payload)
    await send_ws(msg["receiver_id"], payload)
    return {"ok": True, "message": updated}

@app.delete("/api/messages/{message_id}")
async def delete_message(message_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    msg = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not msg:
        conn.close()
        raise HTTPException(404, "Не найдено")
    if msg["sender_id"] != user_id:
        conn.close()
        raise HTTPException(403, "Не ваше")
    conn.execute("UPDATE messages SET deleted = 1, text = '' WHERE id = ?", (message_id,))
    conn.commit()
    conn.close()
    payload = {"type": "message_deleted", "message": {"id": message_id, "sender_id": msg["sender_id"], "receiver_id": msg["receiver_id"], "deleted": 1, "text": ""}}
    await send_ws(msg["sender_id"], payload)
    await send_ws(msg["receiver_id"], payload)
    return {"ok": True}

@app.post("/api/messages/{message_id}/favorite")
def favorite_message(message_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    msg = conn.execute("SELECT id FROM messages WHERE id = ? AND (sender_id = ? OR receiver_id = ?)", (message_id, user_id, user_id)).fetchone()
    if not msg:
        conn.close()
        raise HTTPException(404, "Не найдено")
    existing = conn.execute("SELECT id FROM favorites WHERE user_id = ? AND message_id = ?", (user_id, message_id)).fetchone()
    if existing:
        conn.execute("DELETE FROM favorites WHERE user_id = ? AND message_id = ?", (user_id, message_id))
        fav = False
    else:
        conn.execute("INSERT INTO favorites (user_id, message_id) VALUES (?, ?)", (user_id, message_id))
        fav = True
    conn.commit()
    conn.close()
    return {"favorite": fav}

@app.get("/api/favorites")
def get_favorites(req: Request):
    user_id = get_auth_user(req)
    conn = db()
    rows = conn.execute("SELECT m.id, m.sender_id, m.receiver_id, m.text, m.created_at, m.edited_at, m.deleted FROM favorites f JOIN messages m ON m.id = f.message_id WHERE f.user_id = ? ORDER BY f.id DESC", (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ============================================================
# FEED / POSTS
# ============================================================
@app.get("/api/feed")
def feed(req: Request):
    get_auth_user(req)
    conn = db()
    posts = conn.execute(
        "SELECT p.id, p.author_id, p.text, p.media_url, p.media_type, p.created_at, u.username, u.display_name, u.avatar_url, (SELECT COUNT(*) FROM post_likes WHERE post_id = p.id) AS likes, (SELECT COUNT(*) FROM comments WHERE post_id = p.id) AS comments_count FROM posts p JOIN users u ON u.id = p.author_id ORDER BY p.id DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return [dict(p) for p in posts]

@app.post("/api/posts")
def create_post(data: PostRequest, req: Request):
    user_id = get_auth_user(req)
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "Напишите текст")
    if len(text) > 5000:
        raise HTTPException(400, "Слишком длинный")
    conn = db()
    cursor = conn.execute("INSERT INTO posts (author_id, text, created_at) VALUES (?, ?, ?)", (user_id, text, now()))
    pid = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"ok": True, "id": pid}

@app.post("/api/posts/media")
async def create_media_post(req: Request, text: str = "", file: UploadFile = File(...)):
    user_id = get_auth_user(req)
    allowed = {"image/jpeg": (".jpg", "image"), "image/png": (".png", "image"), "image/webp": (".webp", "image"), "video/mp4": (".mp4", "video"), "video/webm": (".webm", "video")}
    if file.content_type not in allowed:
        raise HTTPException(400, "Формат не поддерживается")
    ext, media_type = allowed[file.content_type]
    filename = f"post_{user_id}_{secrets.token_hex(10)}{ext}"
    path = UPLOAD_DIR / filename
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    url = "/uploads/" + filename
    conn = db()
    cursor = conn.execute("INSERT INTO posts (author_id, text, media_url, media_type, created_at) VALUES (?, ?, ?, ?, ?)", (user_id, text.strip(), url, media_type, now()))
    pid = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"ok": True, "id": pid, "media_url": url}

@app.post("/api/posts/{post_id}/like")
def like_post(post_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    existing = conn.execute("SELECT id FROM post_likes WHERE post_id = ? AND user_id = ?", (post_id, user_id)).fetchone()
    if existing:
        conn.execute("DELETE FROM post_likes WHERE post_id = ? AND user_id = ?", (post_id, user_id))
        liked = False
    else:
        conn.execute("INSERT INTO post_likes (post_id, user_id) VALUES (?, ?)", (post_id, user_id))
        liked = True
    count = conn.execute("SELECT COUNT(*) FROM post_likes WHERE post_id = ?", (post_id,)).fetchone()[0]
    conn.commit()
    conn.close()
    return {"liked": liked, "likes": count}

@app.get("/api/posts/{post_id}/comments")
def get_comments(post_id: int, req: Request):
    get_auth_user(req)
    conn = db()
    comments = conn.execute("SELECT c.id, c.post_id, c.user_id, c.parent_id, c.text, c.created_at, u.username, u.display_name, u.avatar_url, puser.username AS reply_to_username FROM comments c JOIN users u ON u.id = c.user_id LEFT JOIN comments parent ON parent.id = c.parent_id LEFT JOIN users puser ON puser.id = parent.user_id WHERE c.post_id = ? ORDER BY c.id ASC", (post_id,)).fetchall()
    conn.close()
    return [dict(c) for c in comments]

@app.post("/api/posts/{post_id}/comments")
def create_comment(post_id: int, data: CommentRequest, req: Request):
    user_id = get_auth_user(req)
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "Пустой комментарий")
    conn = db()
    post = conn.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        conn.close()
        raise HTTPException(404, "Пост не найден")
    if data.parent_id:
        parent = conn.execute("SELECT id FROM comments WHERE id = ? AND post_id = ?", (data.parent_id, post_id)).fetchone()
        if not parent:
            conn.close()
            raise HTTPException(400, "Комментарий не найден")
    cursor = conn.execute("INSERT INTO comments (post_id, user_id, parent_id, text, created_at) VALUES (?, ?, ?, ?, ?)", (post_id, user_id, data.parent_id, text, now()))
    cid = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"ok": True, "id": cid}

@app.delete("/api/comments/{comment_id}")
def delete_comment(comment_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    comment = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
    if not comment:
        conn.close()
        raise HTTPException(404, "Не найден")
    if comment["user_id"] != user_id:
        conn.close()
        raise HTTPException(403, "Не ваш")
    conn.execute("DELETE FROM comments WHERE id = ? OR parent_id = ?", (comment_id, comment_id))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/posts/{post_id}")
def delete_post(post_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        conn.close()
        raise HTTPException(404, "Не найден")
    if post["author_id"] != user_id:
        conn.close()
        raise HTTPException(403, "Не ваш")
    conn.execute("DELETE FROM comments WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM post_likes WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ============================================================
# SETTINGS
# ============================================================
@app.get("/api/settings")
def get_settings(req: Request):
    user_id = get_auth_user(req)
    conn = db()
    conn.execute("INSERT OR IGNORE INTO settings (user_id) VALUES (?)", (user_id,))
    conn.commit()
    settings = conn.execute("SELECT language, theme, notifications, show_online, show_last_seen, auto_answer, mute_on_join, camera_on_join FROM settings WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(settings)

@app.put("/api/settings")
def update_settings(data: SettingsRequest, req: Request):
    user_id = get_auth_user(req)
    if data.language not in {"ru", "en", "be", "kk"}:
        raise HTTPException(400, "Язык не поддерживается")
    if data.theme not in {"dark", "light", "blue"}:
        raise HTTPException(400, "Тема не поддерживается")
    conn = db()
    conn.execute("""INSERT OR REPLACE INTO settings 
        (user_id, language, theme, notifications, show_online, show_last_seen, auto_answer, mute_on_join, camera_on_join) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", 
        (user_id, data.language, data.theme, int(data.notifications), int(data.show_online), int(data.show_last_seen), 
         int(data.auto_answer), int(data.mute_on_join), int(data.camera_on_join)))
    conn.commit()
    conn.close()
    return {"ok": True}

# ============================================================
# PRIVACY
# ============================================================
@app.get("/api/privacy")
def get_privacy(req: Request):
    user_id = get_auth_user(req)
    conn = db()
    conn.execute("INSERT OR IGNORE INTO privacy_settings (user_id) VALUES (?)", (user_id,))
    conn.commit()
    settings = conn.execute("SELECT phone_visibility, avatar_visibility, last_seen_visibility FROM privacy_settings WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(settings)

@app.put("/api/privacy")
def update_privacy(data: PrivacySettingsRequest, req: Request):
    user_id = get_auth_user(req)
    valid = {"all", "contacts", "none"}
    if data.phone_visibility not in valid or data.avatar_visibility not in valid or data.last_seen_visibility not in valid:
        raise HTTPException(400, "Некорректное значение")
    conn = db()
    conn.execute("INSERT OR REPLACE INTO privacy_settings (user_id, phone_visibility, avatar_visibility, last_seen_visibility) VALUES (?, ?, ?, ?)", 
        (user_id, data.phone_visibility, data.avatar_visibility, data.last_seen_visibility))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/chats/clear")
def clear_chats(req: Request):
    user_id = get_auth_user(req)
    conn = db()
    conn.execute("DELETE FROM messages WHERE sender_id = ? OR receiver_id = ?", (user_id, user_id))
    conn.execute("DELETE FROM chat_settings WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM favorites WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ============================================================
# GROUPS
# ============================================================
@app.post("/api/groups")
def create_group(data: GroupRequest, req: Request):
    user_id = get_auth_user(req)
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Название группы обязательно")
    conn = db()
    cursor = conn.execute("INSERT INTO groups (name, description, owner_id, created_at) VALUES (?, ?, ?, ?)", 
        (name, data.description.strip(), user_id, now()))
    gid = cursor.lastrowid
    conn.execute("INSERT INTO group_members (group_id, user_id, joined_at) VALUES (?, ?, ?)", (gid, user_id, now()))
    conn.commit()
    conn.close()
    return {"ok": True, "id": gid}

@app.get("/api/groups")
def get_groups(req: Request):
    user_id = get_auth_user(req)
    conn = db()
    groups = conn.execute("SELECT g.id, g.name, g.description, g.owner_id, g.created_at FROM groups g JOIN group_members gm ON gm.group_id = g.id WHERE gm.user_id = ? ORDER BY g.id DESC", (user_id,)).fetchall()
    conn.close()
    return [dict(g) for g in groups]

@app.get("/api/groups/{group_id}/messages")
def get_group_messages(group_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    member = conn.execute("SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id)).fetchone()
    if not member:
        conn.close()
        raise HTTPException(403, "Вы не состоите в группе")
    msgs = conn.execute("SELECT m.id, m.group_id, m.sender_id, CASE WHEN m.deleted=1 THEN '' ELSE m.text END AS text, m.created_at, m.deleted, u.username, u.display_name AS sender_name FROM group_messages m JOIN users u ON u.id = m.sender_id WHERE m.group_id = ? ORDER BY m.id ASC", (group_id,)).fetchall()
    conn.close()
    return [dict(m) for m in msgs]

@app.post("/api/groups/{group_id}/messages")
async def send_group_message(group_id: int, data: GroupMessageRequest, req: Request):
    user_id = get_auth_user(req)
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if len(text) > 5000:
        raise HTTPException(400, "Слишком длинное")
    conn = db()
    member = conn.execute("SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id)).fetchone()
    if not member:
        conn.close()
        raise HTTPException(403, "Вы не состоите в группе")
    created = now()
    cursor = conn.execute("INSERT INTO group_messages (group_id, sender_id, text, created_at) VALUES (?, ?, ?, ?)", (group_id, user_id, text, created))
    mid = cursor.lastrowid
    members = conn.execute("SELECT user_id FROM group_members WHERE group_id = ?", (group_id,)).fetchall()
    group = conn.execute("SELECT name FROM groups WHERE id = ?", (group_id,)).fetchone()
    sender = conn.execute("SELECT username, display_name FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.commit()
    conn.close()
    msg = {"id": mid, "group_id": group_id, "group_name": group["name"] if group else "Группа", "sender_id": user_id, "text": text, "created_at": created, "deleted": 0, "sender_username": sender["username"], "sender_name": sender["display_name"] or sender["username"], "chat_kind": "group"}
    payload = {"type": "group_message", "message": msg}
    for m in members:
        await send_ws(m["user_id"], payload)
    return {"ok": True, "message": msg}

@app.post("/api/groups/{group_id}/leave")
def leave_group(group_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    conn.execute("DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.put("/api/groups/{group_id}")
def rename_group(group_id: int, data: RenameEntityRequest, req: Request):
    user_id = get_auth_user(req)
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Название обязательно")
    conn = db()
    g = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    if not g or g["owner_id"] != user_id:
        conn.close()
        raise HTTPException(403, "Только создатель")
    conn.execute("UPDATE groups SET name = ? WHERE id = ?", (name, group_id))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/groups/{group_id}/avatar")
async def group_avatar(group_id: int, req: Request, file: UploadFile = File(...)):
    user_id = get_auth_user(req)
    conn = db()
    g = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    if not g or g["owner_id"] != user_id:
        conn.close()
        raise HTTPException(403, "Только создатель")
    conn.close()
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if file.content_type not in allowed:
        raise HTTPException(400, "Только изображения")
    filename = f"gr_{group_id}_{secrets.token_hex(8)}{allowed[file.content_type]}"
    path = UPLOAD_DIR / filename
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    url = "/uploads/" + filename
    conn = db()
    conn.execute("UPDATE groups SET avatar_url = ? WHERE id = ?", (url, group_id))
    conn.commit()
    conn.close()
    return {"ok": True, "avatar_url": url}

@app.post("/api/groups/{group_id}/invite-bot")
async def invite_group_bot(group_id: int, data: InviteRequest, req: Request):
    user_id = get_auth_user(req)
    username = data.username.strip().lower()
    conn = db()
    group = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    member = conn.execute("SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id)).fetchone()
    if not group or not member:
        conn.close()
        raise HTTPException(403, "Нет доступа")
    target = conn.execute("SELECT id, username FROM users WHERE username = ?", (username,)).fetchone()
    inviter = conn.execute("SELECT username, display_name FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        conn.close()
        raise HTTPException(404, "Пользователь не найден")
    cursor = conn.execute("INSERT INTO invites (type, target_id, from_user_id, to_user_id, status, created_at) VALUES ('group', ?, ?, ?, 'pending', ?)", 
        (group_id, user_id, target["id"], now()))
    invite_id = cursor.lastrowid
    conn.commit()
    conn.close()
    text = f'Приглашение в группу «{group["name"]}» от @{inviter["username"]}'
    msg = bot_send_message(target["id"], text, invite_id=invite_id, invite_status="pending")
    if msg:
        conn = db()
        conn.execute("UPDATE invites SET message_id = ? WHERE id = ?", (msg["id"], invite_id))
        conn.commit()
        conn.close()
        await send_ws(target["id"], {"type": "message", "message": msg})
    return {"ok": True}

# ============================================================
# CHANNELS
# ============================================================
@app.post("/api/channels")
def create_channel(data: ChannelRequest, req: Request):
    user_id = get_auth_user(req)
    name = data.name.strip()
    username = data.username.strip().lower()
    if not name:
        raise HTTPException(400, "Название канала обязательно")
    if not valid_username(username):
        raise HTTPException(400, "Некорректный username канала")
    conn = db()
    try:
        cursor = conn.execute("INSERT INTO channels (name, username, description, owner_id, created_at) VALUES (?, ?, ?, ?, ?)", 
            (name, username, data.description.strip(), user_id, now()))
        cid = cursor.lastrowid
        conn.execute("INSERT INTO channel_subscribers (channel_id, user_id, created_at) VALUES (?, ?, ?)", (cid, user_id, now()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        raise HTTPException(400, "Такой username канала уже существует")
    conn.close()
    return {"ok": True, "id": cid}

@app.get("/api/channels")
def get_channels(req: Request):
    user_id = get_auth_user(req)
    conn = db()
    channels = conn.execute("SELECT c.id, c.name, c.username, c.description, c.owner_id, c.created_at FROM channels c JOIN channel_subscribers s ON s.channel_id = c.id WHERE s.user_id = ? ORDER BY c.id DESC", (user_id,)).fetchall()
    conn.close()
    result = []
    for ch in channels:
        item = dict(ch)
        item["is_owner"] = item["owner_id"] == user_id
        result.append(item)
    return result

@app.get("/api/channels/{channel_id}/messages")
def get_channel_messages(channel_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    sub = conn.execute("SELECT 1 FROM channel_subscribers WHERE channel_id = ? AND user_id = ?", (channel_id, user_id)).fetchone()
    if not sub:
        conn.close()
        raise HTTPException(403, "Вы не подписаны на канал")
    msgs = conn.execute("SELECT m.id, m.channel_id, m.sender_id, CASE WHEN m.deleted=1 THEN '' ELSE m.text END AS text, m.created_at, m.deleted, u.username, u.display_name AS sender_name FROM channel_messages m JOIN users u ON u.id = m.sender_id WHERE m.channel_id = ? ORDER BY m.id ASC", (channel_id,)).fetchall()
    conn.close()
    return [dict(m) for m in msgs]

@app.post("/api/channels/{channel_id}/messages")
async def send_channel_message(channel_id: int, data: ChannelMessageRequest, req: Request):
    user_id = get_auth_user(req)
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if len(text) > 5000:
        raise HTTPException(400, "Слишком длинное")
    conn = db()
    channel = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if not channel:
        conn.close()
        raise HTTPException(404, "Канал не найден")
    if channel["owner_id"] != user_id:
        conn.close()
        raise HTTPException(403, "В канал может писать только создатель")
    created = now()
    cursor = conn.execute("INSERT INTO channel_messages (channel_id, sender_id, text, created_at) VALUES (?, ?, ?, ?)", (channel_id, user_id, text, created))
    mid = cursor.lastrowid
    subs = conn.execute("SELECT user_id FROM channel_subscribers WHERE channel_id = ?", (channel_id,)).fetchall()
    ch = conn.execute("SELECT name FROM channels WHERE id = ?", (channel_id,)).fetchone()
    sender = conn.execute("SELECT username, display_name FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.commit()
    conn.close()
    msg = {"id": mid, "channel_id": channel_id, "channel_name": ch["name"] if ch else "Канал", "sender_id": user_id, "text": text, "created_at": created, "deleted": 0, "sender_username": sender["username"], "sender_name": sender["display_name"] or sender["username"], "chat_kind": "channel"}
    payload = {"type": "channel_message", "message": msg}
    for s in subs:
        await send_ws(s["user_id"], payload)
    return {"ok": True, "message": msg}

@app.post("/api/channels/{channel_id}/join")
def join_channel(channel_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    if not conn.execute("SELECT id FROM channels WHERE id = ?", (channel_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "Канал не найден")
    existing = conn.execute("SELECT 1 FROM channel_subscribers WHERE channel_id = ? AND user_id = ?", (channel_id, user_id)).fetchone()
    if existing:
        conn.close()
        return {"ok": True, "joined": True}
    conn.execute("INSERT INTO channel_subscribers (channel_id, user_id, created_at) VALUES (?, ?, ?)", (channel_id, user_id, now()))
    conn.commit()
    conn.close()
    return {"ok": True, "joined": True}

@app.post("/api/channels/{channel_id}/leave")
def leave_channel(channel_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    conn.execute("DELETE FROM channel_subscribers WHERE channel_id = ? AND user_id = ?", (channel_id, user_id))
    conn.execute("DELETE FROM channel_mutes WHERE channel_id = ? AND user_id = ?", (channel_id, user_id))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.put("/api/channels/{channel_id}")
def rename_channel(channel_id: int, data: RenameEntityRequest, req: Request):
    user_id = get_auth_user(req)
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Название обязательно")
    conn = db()
    ch = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if not ch or ch["owner_id"] != user_id:
        conn.close()
        raise HTTPException(403, "Только создатель")
    conn.execute("UPDATE channels SET name = ? WHERE id = ?", (name, channel_id))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/channels/{channel_id}/avatar")
async def channel_avatar(channel_id: int, req: Request, file: UploadFile = File(...)):
    user_id = get_auth_user(req)
    conn = db()
    ch = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if not ch or ch["owner_id"] != user_id:
        conn.close()
        raise HTTPException(403, "Только создатель")
    conn.close()
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if file.content_type not in allowed:
        raise HTTPException(400, "Только изображения")
    filename = f"ch_{channel_id}_{secrets.token_hex(8)}{allowed[file.content_type]}"
    path = UPLOAD_DIR / filename
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    url = "/uploads/" + filename
    conn = db()
    conn.execute("UPDATE channels SET avatar_url = ? WHERE id = ?", (url, channel_id))
    conn.commit()
    conn.close()
    return {"ok": True, "avatar_url": url}

@app.post("/api/channels/{channel_id}/mute")
def mute_channel(channel_id: int, req: Request, muted: bool = True):
    user_id = get_auth_user(req)
    conn = db()
    conn.execute("INSERT INTO channel_mutes (channel_id, user_id, muted) VALUES (?, ?, ?) ON CONFLICT(channel_id, user_id) DO UPDATE SET muted = excluded.muted", 
        (channel_id, user_id, int(muted)))
    conn.commit()
    conn.close()
    return {"ok": True, "muted": muted}

# ============================================================
# COMMUNITIES
# ============================================================
@app.post("/api/communities")
def create_community(data: CommunityRequest, req: Request):
    user_id = get_auth_user(req)
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Название сообщества обязательно")
    conn = db()
    cursor = conn.execute("INSERT INTO communities (name, description, owner_id, created_at) VALUES (?, ?, ?, ?)", 
        (name, data.description.strip(), user_id, now()))
    cid = cursor.lastrowid
    conn.execute("INSERT INTO community_members (community_id, user_id, joined_at) VALUES (?, ?, ?)", (cid, user_id, now()))
    conn.execute("INSERT INTO community_chats (community_id, name, description, created_at) VALUES (?, ?, ?, ?)", 
        (cid, "Общий", "Основной чат сообщества", now()))
    conn.commit()
    conn.close()
    return {"ok": True, "id": cid}

@app.get("/api/communities")
def get_communities(req: Request):
    user_id = get_auth_user(req)
    conn = db()
    rows = conn.execute("SELECT c.id, c.name, c.description, c.owner_id, c.created_at FROM communities c JOIN community_members m ON m.community_id = c.id WHERE m.user_id = ? ORDER BY c.id DESC", (user_id,)).fetchall()
    conn.close()
    result = []
    for row in rows:
        item = dict(row)
        item["is_owner"] = item["owner_id"] == user_id
        result.append(item)
    return result

@app.get("/api/communities/{community_id}/chats")
def get_community_chats(community_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    member = conn.execute("SELECT 1 FROM community_members WHERE community_id = ? AND user_id = ?", (community_id, user_id)).fetchone()
    if not member:
        conn.close()
        raise HTTPException(403, "Вы не состоите в сообществе")
    chats = conn.execute("SELECT id, community_id, name, description, created_at FROM community_chats WHERE community_id = ? ORDER BY id ASC", (community_id,)).fetchall()
    conn.close()
    return [dict(c) for c in chats]

@app.post("/api/communities/{community_id}/chats")
def create_community_chat(community_id: int, data: CommunityChatRequest, req: Request):
    user_id = get_auth_user(req)
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Название чата обязательно")
    conn = db()
    member = conn.execute("SELECT 1 FROM community_members WHERE community_id = ? AND user_id = ?", (community_id, user_id)).fetchone()
    if not member:
        conn.close()
        raise HTTPException(403, "Вы не состоите в сообществе")
    cursor = conn.execute("INSERT INTO community_chats (community_id, name, description, created_at) VALUES (?, ?, ?, ?)", 
        (community_id, name, data.description.strip(), now()))
    cid = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"ok": True, "id": cid}

@app.get("/api/community-chats/{chat_id}/messages")
def get_community_chat_messages(chat_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    chat = conn.execute("SELECT cc.id, cc.community_id FROM community_chats cc WHERE cc.id = ?", (chat_id,)).fetchone()
    if not chat:
        conn.close()
        raise HTTPException(404, "Чат не найден")
    member = conn.execute("SELECT 1 FROM community_members WHERE community_id = ? AND user_id = ?", (chat["community_id"], user_id)).fetchone()
    if not member:
        conn.close()
        raise HTTPException(403, "Нет доступа")
    msgs = conn.execute("SELECT m.id, m.chat_id, m.sender_id, CASE WHEN m.deleted=1 THEN '' ELSE m.text END AS text, m.created_at, m.deleted, u.username, u.display_name AS sender_name FROM community_chat_messages m JOIN users u ON u.id = m.sender_id WHERE m.chat_id = ? ORDER BY m.id ASC", (chat_id,)).fetchall()
    conn.close()
    return [dict(m) for m in msgs]

@app.post("/api/community-chats/{chat_id}/messages")
async def send_community_chat_message(chat_id: int, data: GroupMessageRequest, req: Request):
    user_id = get_auth_user(req)
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if len(text) > 5000:
        raise HTTPException(400, "Слишком длинное")
    conn = db()
    chat = conn.execute("SELECT cc.id, cc.community_id FROM community_chats cc WHERE cc.id = ?", (chat_id,)).fetchone()
    if not chat:
        conn.close()
        raise HTTPException(404, "Чат не найден")
    member = conn.execute("SELECT 1 FROM community_members WHERE community_id = ? AND user_id = ?", (chat["community_id"], user_id)).fetchone()
    if not member:
        conn.close()
        raise HTTPException(403, "Нет доступа")
    created = now()
    cursor = conn.execute("INSERT INTO community_chat_messages (chat_id, sender_id, text, created_at) VALUES (?, ?, ?, ?)", (chat_id, user_id, text, created))
    mid = cursor.lastrowid
    members = conn.execute("SELECT user_id FROM community_members WHERE community_id = ?", (chat["community_id"],)).fetchall()
    community = conn.execute("SELECT name FROM communities WHERE id = ?", (chat["community_id"],)).fetchone()
    chat_row = conn.execute("SELECT name FROM community_chats WHERE id = ?", (chat_id,)).fetchone()
    sender = conn.execute("SELECT username, display_name FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.commit()
    conn.close()
    msg = {"id": mid, "chat_id": chat_id, "community_id": chat["community_id"], "community_name": community["name"] if community else "Сообщество", "chat_name": chat_row["name"] if chat_row else "Чат", "sender_id": user_id, "text": text, "created_at": created, "deleted": 0, "sender_username": sender["username"], "sender_name": sender["display_name"] or sender["username"], "chat_kind": "community"}
    payload = {"type": "community_message", "message": msg}
    for m in members:
        await send_ws(m["user_id"], payload)
    return {"ok": True, "message": msg}

@app.post("/api/communities/{community_id}/invite-bot")
async def invite_community_bot(community_id: int, data: InviteRequest, req: Request):
    user_id = get_auth_user(req)
    username = data.username.strip().lower()
    conn = db()
    community = conn.execute("SELECT * FROM communities WHERE id = ?", (community_id,)).fetchone()
    member = conn.execute("SELECT 1 FROM community_members WHERE community_id = ? AND user_id = ?", (community_id, user_id)).fetchone()
    if not community or not member:
        conn.close()
        raise HTTPException(403, "Нет доступа")
    target = conn.execute("SELECT id, username FROM users WHERE username = ?", (username,)).fetchone()
    inviter = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        conn.close()
        raise HTTPException(404, "Пользователь не найден")
    cursor = conn.execute("INSERT INTO invites (type, target_id, from_user_id, to_user_id, status, created_at) VALUES ('community', ?, ?, ?, 'pending', ?)", 
        (community_id, user_id, target["id"], now()))
    invite_id = cursor.lastrowid
    conn.commit()
    conn.close()
    text = f'Приглашение в сообщество «{community["name"]}» от @{inviter["username"]}'
    msg = bot_send_message(target["id"], text, invite_id=invite_id, invite_status="pending")
    if msg:
        conn = db()
        conn.execute("UPDATE invites SET message_id = ? WHERE id = ?", (msg["id"], invite_id))
        conn.commit()
        conn.close()
        await send_ws(target["id"], {"type": "message", "message": msg})
    return {"ok": True}

# ============================================================
# INVITES
# ============================================================
@app.post("/api/invites/{invite_id}/respond")
async def respond_invite(invite_id: int, data: InviteActionRequest, req: Request):
    user_id = get_auth_user(req)
    action = data.action.strip().lower()
    if action not in ("accept", "decline"):
        raise HTTPException(400, "action: accept|decline")
    conn = db()
    inv = conn.execute("SELECT * FROM invites WHERE id = ? AND to_user_id = ?", (invite_id, user_id)).fetchone()
    if not inv:
        conn.close()
        raise HTTPException(404, "Приглашение не найдено")
    if inv["status"] != "pending":
        conn.close()
        raise HTTPException(400, "Уже отвечено")
    status = "accepted" if action == "accept" else "declined"
    conn.execute("UPDATE invites SET status = ? WHERE id = ?", (status, invite_id))
    if action == "accept":
        if inv["type"] == "group":
            conn.execute("INSERT OR IGNORE INTO group_members (group_id, user_id, joined_at) VALUES (?, ?, ?)", 
                (inv["target_id"], user_id, now()))
        elif inv["type"] == "community":
            conn.execute("INSERT OR IGNORE INTO community_members (community_id, user_id, joined_at) VALUES (?, ?, ?)", 
                (inv["target_id"], user_id, now()))
    if inv["message_id"]:
        conn.execute("UPDATE messages SET invite_status = ? WHERE id = ?", (status, inv["message_id"]))
    conn.commit()
    conn.close()
    reply = bot_send_message(user_id, "Ваш выбор был учтён")
    if reply:
        await send_ws(user_id, {"type": "message", "message": reply})
    return {"ok": True, "status": status}

# ============================================================
# BOT HELPERS
# ============================================================
def bot_send_message(to_user_id, text, invite_id=None, invite_status=None):
    lumi_id = get_lumi_id()
    if not lumi_id:
        return None
    conn = db()
    created = now()
    cursor = conn.execute("INSERT INTO messages (sender_id, receiver_id, text, created_at, invite_id, invite_status) VALUES (?, ?, ?, ?, ?, ?)", 
        (lumi_id, to_user_id, text, created, invite_id, invite_status))
    mid = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"id": mid, "sender_id": lumi_id, "receiver_id": to_user_id, "text": text, "created_at": created, "edited_at": None, "deleted": 0, "is_read": 0, "media_url": None, "media_type": None, "invite_id": invite_id, "invite_status": invite_status}

def get_lumi_id(conn=None):
    own = conn is None
    if own:
        conn = db()
    row = conn.execute("SELECT id FROM users WHERE username = 'lumi'").fetchone()
    if own:
        conn.close()
    return row["id"] if row else None

# ============================================================
# AUTH CODE
# ============================================================
@app.post("/api/auth/request-code")
async def request_code(data: RequestCodeRequest, req: Request):
    username = data.username.strip().lower()
    conn = db()
    user = conn.execute("SELECT id, username FROM users WHERE username = ?", (username,)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(404, "Пользователь не найден")
    if user["username"] == "lumi":
        conn.close()
        raise HTTPException(400, "Нельзя")
    import random
    code = f"{random.randint(100000, 999999)}"
    created = datetime.utcnow()
    expires = created + timedelta(minutes=10)
    conn.execute("INSERT INTO login_codes (user_id, code, created_at, expires_at, used) VALUES (?, ?, ?, ?, 0)", 
        (user["id"], code, created.isoformat(), expires.isoformat()))
    conn.commit()
    conn.close()
    msg = bot_send_message(user["id"], f"🔐 Ваш код для входа: {code}\nКод действует 10 минут.")
    if msg:
        await send_ws(user["id"], {"type": "message", "message": msg})
    return {"ok": True, "detail": "Код отправлен ботом Lumi в ЛС"}

@app.post("/api/auth/login-code")
def login_code(data: CodeLoginRequest, req: Request, res: Response):
    username = data.username.strip().lower()
    code = data.code.strip()
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(401, "Неверный username или код")
    row = conn.execute("SELECT * FROM login_codes WHERE user_id = ? AND code = ? AND used = 0 ORDER BY id DESC LIMIT 1", 
        (user["id"], code)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(401, "Неверный username или код")
    try:
        exp = datetime.fromisoformat(row["expires_at"])
    except:
        exp = datetime.utcnow()
    if exp < datetime.utcnow():
        conn.close()
        raise HTTPException(401, "Код истёк")
    conn.execute("UPDATE login_codes SET used = 1 WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    token = new_token()
    conn = db()
    conn.execute("INSERT INTO sessions (user_id, token_hash, browser_hash, created_at, last_seen, expires_at) VALUES (?, ?, ?, ?, ?, ?)", 
        (user["id"], hash_token(token), "browser", now(), now(), (datetime.utcnow()+timedelta(days=SESSION_DAYS)).isoformat()))
    conn.commit()
    conn.close()
    set_auth_cookie(res, token)
    return {"ok": True, "token": token}

# ============================================================
# BLOCKS / CHAT SETTINGS
# ============================================================
@app.post("/api/users/{other_id}/block")
def block_user(other_id: int, req: Request):
    user_id = get_auth_user(req)
    if other_id == user_id:
        raise HTTPException(400, "Нельзя заблокировать себя")
    conn = db()
    conn.execute("INSERT OR IGNORE INTO blocks (user_id, blocked_id, created_at) VALUES (?, ?, ?)", (user_id, other_id, now()))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/users/{other_id}/block")
def unblock_user(other_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    conn.execute("DELETE FROM blocks WHERE user_id = ? AND blocked_id = ?", (user_id, other_id))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.put("/api/contacts/{contact_id}/alias")
def set_alias(contact_id: int, data: AliasRequest, req: Request):
    user_id = get_auth_user(req)
    alias = data.alias.strip()
    conn = db()
    if not alias:
        conn.execute("DELETE FROM contact_aliases WHERE user_id = ? AND contact_id = ?", (user_id, contact_id))
    else:
        conn.execute("INSERT INTO contact_aliases (user_id, contact_id, alias) VALUES (?, ?, ?) ON CONFLICT(user_id, contact_id) DO UPDATE SET alias = excluded.alias", 
            (user_id, contact_id, alias))
    conn.commit()
    conn.close()
    return {"ok": True, "alias": alias}

@app.get("/api/chats/{peer_id}/settings")
def get_chat_settings(peer_id: int, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    row = conn.execute("SELECT wallpaper_url, wallpaper_blur, deleted_for_me FROM chat_settings WHERE user_id = ? AND peer_id = ?", 
        (user_id, peer_id)).fetchone()
    alias = conn.execute("SELECT alias FROM contact_aliases WHERE user_id = ? AND contact_id = ?", (user_id, peer_id)).fetchone()
    blocked = conn.execute("SELECT 1 FROM blocks WHERE user_id = ? AND blocked_id = ?", (user_id, peer_id)).fetchone()
    conn.close()
    return {"wallpaper_url": row["wallpaper_url"] if row else None, "wallpaper_blur": bool(row["wallpaper_blur"]) if row else False, 
            "deleted_for_me": bool(row["deleted_for_me"]) if row else False, "alias": alias["alias"] if alias else None, "blocked": bool(blocked)}

@app.put("/api/chats/{peer_id}/wallpaper")
def set_wallpaper(peer_id: int, data: WallpaperRequest, req: Request):
    user_id = get_auth_user(req)
    conn = db()
    conn.execute("INSERT INTO chat_settings (user_id, peer_id, wallpaper_url, wallpaper_blur) VALUES (?, ?, ?, ?) ON CONFLICT(user_id, peer_id) DO UPDATE SET wallpaper_url = excluded.wallpaper_url, wallpaper_blur = excluded.wallpaper_blur", 
        (user_id, peer_id, data.wallpaper_url or None, int(data.wallpaper_blur)))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/chats/{peer_id}")
def delete_chat(peer_id: int, req: Request, for_both: bool = False):
    user_id = get_auth_user(req)
    conn = db()
    if for_both:
        conn.execute("DELETE FROM messages WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)", 
            (user_id, peer_id, peer_id, user_id))
        conn.execute("DELETE FROM chat_settings WHERE (user_id = ? AND peer_id = ?) OR (user_id = ? AND peer_id = ?)", 
            (user_id, peer_id, peer_id, user_id))
    else:
        conn.execute("INSERT INTO chat_settings (user_id, peer_id, deleted_for_me) VALUES (?, ?, 1) ON CONFLICT(user_id, peer_id) DO UPDATE SET deleted_for_me = 1", 
            (user_id, peer_id))
    conn.commit()
    conn.close()
    return {"ok": True}

# ============================================================
# MEDIA
# ============================================================
@app.post("/api/messages/media")
async def send_media(req: Request, receiver_id: int, file: UploadFile = File(...), text: str = Form("")):
    sender_id = get_auth_user(req)
    conn = db()
    blocked = conn.execute("SELECT 1 FROM blocks WHERE (user_id = ? AND blocked_id = ?) OR (user_id = ? AND blocked_id = ?)", 
        (sender_id, receiver_id, receiver_id, sender_id)).fetchone()
    peer = conn.execute("SELECT is_bot, username FROM users WHERE id = ?", (receiver_id,)).fetchone()
    conn.close()
    if blocked:
        raise HTTPException(403, "Пользователь заблокирован")
    if peer and (peer["is_bot"] or peer["username"] == "lumi"):
        raise HTTPException(403, "Боту нельзя писать")
    ctype = (file.content_type or "application/octet-stream").split(";")[0].strip().lower()
    name = file.filename or "file.bin"
    ext = Path(name).suffix.lower() or ".bin"
    if ctype.startswith("image/") or ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        media_type = "image"
        ext = ".jpg"
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
    with open(path, "wb") as f:
        f.write(data_bytes)
    url = "/uploads/" + filename
    conn = db()
    created = now()
    cursor = conn.execute("INSERT INTO messages (sender_id, receiver_id, text, created_at, media_url, media_type) VALUES (?, ?, ?, ?, ?, ?)", 
        (sender_id, receiver_id, (text or "").strip(), created, url, media_type))
    mid = cursor.lastrowid
    conn.commit()
    conn.close()
    sender = conn.execute("SELECT username, display_name FROM users WHERE id = ?", (sender_id,)).fetchone()
    conn.close()
    msg = {"id": mid, "sender_id": sender_id, "receiver_id": receiver_id, "text": (text or "").strip(), "created_at": created, "edited_at": None, "deleted": 0, "is_read": 0, "media_url": url, "media_type": media_type, "sender_username": sender["username"], "sender_name": sender["display_name"] or sender["username"], "chat_kind": "private"}
    payload = {"type": "message", "message": msg}
    await send_ws(sender_id, payload)
    await send_ws(receiver_id, payload)
    return {"ok": True, "message": msg}

# ============================================================
# SEARCH
# ============================================================
@app.get("/api/search")
def search(req: Request, q: str = ""):
    user_id = get_auth_user(req)
    q = q.strip()
    if not q:
        return {"users": [], "channels": [], "groups": [], "communities": []}
    like = "%" + q + "%"
    conn = db()
    users = conn.execute("SELECT id, username, display_name, avatar_url, last_seen, is_bot, is_verified FROM users WHERE (username LIKE ? OR display_name LIKE ?) AND id != ? ORDER BY username LIMIT 20", 
        (like, like, user_id)).fetchall()
    channels = conn.execute("SELECT c.id, c.name, c.username, c.description, c.owner_id, CASE WHEN s.user_id IS NOT NULL THEN 1 ELSE 0 END AS joined FROM channels c LEFT JOIN channel_subscribers s ON s.channel_id = c.id AND s.user_id = ? WHERE c.name LIKE ? OR c.username LIKE ? OR c.description LIKE ? ORDER BY c.name LIMIT 20", 
        (user_id, like, like, like)).fetchall()
    groups = conn.execute("SELECT g.id, g.name, g.description, g.owner_id FROM groups g JOIN group_members gm ON gm.group_id = g.id WHERE gm.user_id = ? AND (g.name LIKE ? OR g.description LIKE ?) ORDER BY g.name LIMIT 20", 
        (user_id, like, like)).fetchall()
    communities = conn.execute("SELECT c.id, c.name, c.description, c.owner_id FROM communities c JOIN community_members m ON m.community_id = c.id WHERE m.user_id = ? AND (c.name LIKE ? OR c.description LIKE ?) ORDER BY c.name LIMIT 20", 
        (user_id, like, like)).fetchall()
    conn.close()
    return {
        "users": [dict(u) for u in users],
        "channels": [{**dict(c), "is_owner": c["owner_id"] == user_id, "joined": bool(c["joined"])} for c in channels],
        "groups": [dict(g) for g in groups],
        "communities": [dict(c) for c in communities]
    }

# ============================================================
# DIALOGS
# ============================================================
@app.get("/api/dialogs")
def get_dialogs(req: Request):
    user_id = get_auth_user(req)
    conn = db()
    rows = conn.execute("""SELECT CASE WHEN m.sender_id = ? THEN m.receiver_id ELSE m.sender_id END AS peer_id, MAX(m.id) AS last_id
        FROM messages m
        LEFT JOIN chat_settings cs ON cs.user_id = ? AND cs.peer_id = CASE WHEN m.sender_id = ? THEN m.receiver_id ELSE m.sender_id END
        WHERE (m.sender_id = ? OR m.receiver_id = ?) AND IFNULL(cs.deleted_for_me, 0) = 0
        GROUP BY peer_id ORDER BY last_id DESC LIMIT 50""", 
        (user_id, user_id, user_id, user_id, user_id)).fetchall()
    result = []
    for row in rows:
        peer_id = row["peer_id"]
        user = conn.execute("SELECT id, username, display_name, avatar_url, is_bot, is_verified, last_seen FROM users WHERE id = ?", (peer_id,)).fetchone()
        if not user:
            continue
        last = conn.execute("SELECT id, text, created_at, sender_id, media_type, deleted, is_read FROM messages WHERE id = ?", (row["last_id"],)).fetchone()
        alias = conn.execute("SELECT alias FROM contact_aliases WHERE user_id = ? AND contact_id = ?", (user_id, peer_id)).fetchone()
        unread = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE sender_id = ? AND receiver_id = ? AND is_read = 0 AND deleted = 0", 
            (peer_id, user_id)).fetchone()["c"]
        item = dict(user)
        item["alias"] = alias["alias"] if alias else None
        item["last_message"] = dict(last) if last else None
        item["unread"] = unread
        result.append(item)
    conn.close()
    return result

# ============================================================
# CALLS
# ============================================================
@app.post("/api/calls/signal")
async def call_signal(data: CallSignalRequest, req: Request):
    user_id = get_auth_user(req)
    payload = {"type": "call_signal", "from_id": user_id, "signal_type": data.signal_type, "payload": data.payload}
    await send_ws(data.target_id, payload)
    return {"ok": True}

# ============================================================
# WEBSOCKET
# ============================================================
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=1008)
        return
    conn = db()
    session = conn.execute("SELECT id, user_id, expires_at FROM sessions WHERE token_hash = ?", (hash_token(token),)).fetchone()
    conn.close()
    if not session:
        await websocket.close(code=1008)
        return
    try:
        expires = datetime.fromisoformat(session["expires_at"])
    except:
        expires = datetime.utcnow()
    if expires < datetime.utcnow():
        await websocket.close(code=1008)
        return
    user_id = session["user_id"]
    await websocket.accept()
    connections[user_id].add(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
            elif data.get("type") == "typing":
                target = data.get("target_id")
                if target:
                    await send_ws(target, {"type": "typing", "user_id": user_id})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        connections[user_id].discard(websocket)
        if not connections[user_id]:
            connections.pop(user_id, None)

# ============================================================
# STATIC
# ============================================================
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
