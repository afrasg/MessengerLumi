import sqlite3
import hashlib
import secrets
from datetime import datetime, timedelta
from collections import defaultdict

import jwt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


app = FastAPI(title="MyMessenger")

SECRET_KEY = secrets.token_hex(32)
ALGORITHM = "HS256"

DB = "messenger.db"

# user_id -> активные WebSocket-подключения
connections = defaultdict(list)


# ---------------------------------------------------------
# DATABASE
# ---------------------------------------------------------

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
        created_at TEXT NOT NULL,
        last_seen TEXT
    );

    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL,
        receiver_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        is_read INTEGER DEFAULT 0
    );
    """)

    connection.commit()
    connection.close()


init_db()


# ---------------------------------------------------------
# PASSWORDS
# ---------------------------------------------------------

def hash_password(password: str):
    return hashlib.sha256(password.encode()).hexdigest()


# ---------------------------------------------------------
# JWT
# ---------------------------------------------------------

def create_token(user_id: int):
    payload = {
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(days=30)
    }

    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_user_from_token(token: str):
    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )

        return int(payload["user_id"])

    except Exception:
        return None


# ---------------------------------------------------------
# MODELS
# ---------------------------------------------------------

class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class MessageRequest(BaseModel):
    receiver_id: int
    text: str


# ---------------------------------------------------------
# AUTH
# ---------------------------------------------------------

@app.post("/api/register")
def register(data: RegisterRequest):

    username = data.username.strip()

    if len(username) < 3:
        raise HTTPException(400, "Username должен содержать минимум 3 символа")

    if len(data.password) < 6:
        raise HTTPException(400, "Пароль должен содержать минимум 6 символов")

    connection = db()

    existing = connection.execute(
        "SELECT id FROM users WHERE username = ?",
        (username,)
    ).fetchone()

    if existing:
        connection.close()
        raise HTTPException(400, "Такой пользователь уже существует")

    now = datetime.utcnow().isoformat()

    cursor = connection.execute(
        """
        INSERT INTO users
        (username, password_hash, created_at, last_seen)
        VALUES (?, ?, ?, ?)
        """,
        (
            username,
            hash_password(data.password),
            now,
            now
        )
    )

    user_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "token": create_token(user_id),
        "user": {
            "id": user_id,
            "username": username
        }
    }


@app.post("/api/login")
def login(data: LoginRequest):

    connection = db()

    user = connection.execute(
        """
        SELECT *
        FROM users
        WHERE username = ?
        """,
        (data.username.strip(),)
    ).fetchone()

    if not user:
        connection.close()
        raise HTTPException(401, "Неверный логин или пароль")

    if user["password_hash"] != hash_password(data.password):
        connection.close()
        raise HTTPException(401, "Неверный логин или пароль")

    now = datetime.utcnow().isoformat()

    connection.execute(
        "UPDATE users SET last_seen = ? WHERE id = ?",
        (now, user["id"])
    )

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "token": create_token(user["id"]),
        "user": {
            "id": user["id"],
            "username": user["username"]
        }
    }


# ---------------------------------------------------------
# CURRENT USER
# ---------------------------------------------------------

@app.get("/api/me")
def me(request: Request):

    auth = request.headers.get("Authorization", "")

    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Не авторизован")

    user_id = get_user_from_token(auth[7:])

    if not user_id:
        raise HTTPException(401, "Недействительный токен")

    connection = db()

    user = connection.execute(
        "SELECT id, username, created_at, last_seen FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    connection.close()

    if not user:
        raise HTTPException(404, "Пользователь не найден")

    return dict(user)


# ---------------------------------------------------------
# USER SEARCH
# ---------------------------------------------------------

@app.get("/api/users")
def search_users(q: str = ""):

    q = q.strip()

    connection = db()

    users = connection.execute(
        """
        SELECT id, username, last_seen
        FROM users
        WHERE username LIKE ?
        ORDER BY username
        LIMIT 50
        """,
        (f"%{q}%",)
    ).fetchall()

    connection.close()

    return [dict(user) for user in users]


# ---------------------------------------------------------
# MESSAGE HISTORY
# ---------------------------------------------------------

@app.get("/api/messages/{other_user_id}")
def get_messages(other_user_id: int, request: Request):

    auth = request.headers.get("Authorization", "")

    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Не авторизован")

    user_id = get_user_from_token(auth[7:])

    if not user_id:
        raise HTTPException(401, "Недействительный токен")

    connection = db()

    messages = connection.execute(
        """
        SELECT
            id,
            sender_id,
            receiver_id,
            text,
            created_at,
            is_read
        FROM messages
        WHERE
            (sender_id = ? AND receiver_id = ?)
            OR
            (sender_id = ? AND receiver_id = ?)
        ORDER BY id ASC
        """,
        (
            user_id,
            other_user_id,
            other_user_id,
            user_id
        )
    ).fetchall()

    # Сообщения от собеседника считаем прочитанными
    connection.execute(
        """
        UPDATE messages
        SET is_read = 1
        WHERE sender_id = ?
        AND receiver_id = ?
        """,
        (
            other_user_id,
            user_id
        )
    )

    connection.commit()
    connection.close()

    return [dict(message) for message in messages]


# ---------------------------------------------------------
# SEND MESSAGE
# ---------------------------------------------------------

@app.post("/api/messages")
async def send_message(
    data: MessageRequest,
    request: Request
):

    auth = request.headers.get("Authorization", "")

    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Не авторизован")

    sender_id = get_user_from_token(auth[7:])

    if not sender_id:
        raise HTTPException(401, "Недействительный токен")

    text = data.text.strip()

    if not text:
        raise HTTPException(400, "Пустое сообщение")

    if len(text) > 5000:
        raise HTTPException(400, "Сообщение слишком длинное")

    connection = db()

    receiver = connection.execute(
        "SELECT id FROM users WHERE id = ?",
        (data.receiver_id,)
    ).fetchone()

    if not receiver:
        connection.close()
        raise HTTPException(404, "Пользователь не найден")

    now = datetime.utcnow().isoformat()

    cursor = connection.execute(
        """
        INSERT INTO messages
        (sender_id, receiver_id, text, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            sender_id,
            data.receiver_id,
            text,
            now
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
        "created_at": now,
        "is_read": 0
    }

    # Мгновенно отправляем сообщение всем
    # активным соединениям получателя.
    for socket in list(connections.get(data.receiver_id, [])):
        try:
            await socket.send_json({
                "type": "message",
                "message": message
            })
        except Exception:
            pass

    # Также отправляем копию отправителю,
    # если у него открыто несколько вкладок.
    for socket in list(connections.get(sender_id, [])):
        try:
            await socket.send_json({
                "type": "message",
                "message": message
            })
        except Exception:
            pass

    return {
        "ok": True,
        "message": message
    }


# ---------------------------------------------------------
# WEBSOCKET
# ---------------------------------------------------------

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    user_id: int
):

    await websocket.accept()

    connections[user_id].append(websocket)

    # Сообщаем остальному серверу, что пользователь онлайн.
    try:

        while True:

            data = await websocket.receive_json()

            if data.get("type") == "ping":
                await websocket.send_json({
                    "type": "pong"
                })

    except WebSocketDisconnect:

        if websocket in connections[user_id]:
            connections[user_id].remove(websocket)

        if not connections[user_id]:
            connections.pop(user_id, None)


# ---------------------------------------------------------
# FRONTEND
# ---------------------------------------------------------

app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)


@app.get("/")
def index():
    return FileResponse("static/index.html")