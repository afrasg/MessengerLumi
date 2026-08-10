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

UPLOAD_DIR.mkdir(exist_ok=True)

DB = str(BASE_DIR / "messenger.db")

SECRET_KEY = os.environ.get(
    "SECRET_KEY",
    "CHANGE_THIS_SECRET_KEY_IN_RENDER"
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
# WEBSOCKETS
# =========================================================

connections = defaultdict(set)


# =========================================================
# DATABASE
# =========================================================

def db():

    connection = sqlite3.connect(
        DB,
        timeout=30
    )

    connection.row_factory = sqlite3.Row

    return connection


def column_exists(
    connection,
    table,
    column
):

    rows = connection.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()

    return any(
        row["name"] == column
        for row in rows
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

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            edited INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0
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
            UNIQUE(post_id,user_id)
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reply_to_id INTEGER
        );
        """
    )

    if not column_exists(
        connection,
        "users",
        "display_name"
    ):

        connection.execute(
            """
            ALTER TABLE users
            ADD COLUMN display_name TEXT
            """
        )

    if not column_exists(
        connection,
        "users",
        "bio"
    ):

        connection.execute(
            """
            ALTER TABLE users
            ADD COLUMN bio TEXT DEFAULT ''
            """
        )

    if not column_exists(
        connection,
        "users",
        "avatar_url"
    ):

        connection.execute(
            """
            ALTER TABLE users
            ADD COLUMN avatar_url TEXT
            """
        )

    if not column_exists(
        connection,
        "messages",
        "edited"
    ):

        connection.execute(
            """
            ALTER TABLE messages
            ADD COLUMN edited INTEGER DEFAULT 0
            """
        )

    if not column_exists(
        connection,
        "messages",
        "deleted"
    ):

        connection.execute(
            """
            ALTER TABLE messages
            ADD COLUMN deleted INTEGER DEFAULT 0
            """
        )

    if not column_exists(
        connection,
        "comments",
        "reply_to_id"
    ):

        connection.execute(
            """
            ALTER TABLE comments
            ADD COLUMN reply_to_id INTEGER
            """
        )

    connection.execute(
        """
        UPDATE users
        SET display_name=username
        WHERE display_name IS NULL
        """
    )

    connection.commit()
    connection.close()


init_db()


# =========================================================
# PASSWORD
# =========================================================

def hash_password(password):

    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


# =========================================================
# JWT
# =========================================================

def create_token(user_id):

    payload = {
        "user_id": user_id,
        "exp": datetime.utcnow()
        + timedelta(days=30)
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

        return int(
            payload["user_id"]
        )

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
    reply_to_id: int | None = None


# =========================================================
# REGISTER
# =========================================================

@app.post("/api/register")
def register(data: RegisterRequest):

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

    if not username.replace(
        "_",
        ""
    ).isalnum():

        raise HTTPException(
            400,
            "Username может содержать буквы, цифры и _"
        )

    if len(data.password) < 6:

        raise HTTPException(
            400,
            "Пароль должен содержать минимум 6 символов"
        )

    connection = db()

    existing = connection.execute(
        """
        SELECT id
        FROM users
        WHERE username=?
        """,
        (username,)
    ).fetchone()

    if existing:

        connection.close()

        raise HTTPException(
            400,
            "Такой username уже занят"
        )

    now = datetime.utcnow().isoformat()

    cursor = connection.execute(
        """
        INSERT INTO users
        (
            username,
            password_hash,
            created_at,
            last_seen,
            display_name,
            bio
        )
        VALUES(?,?,?,?,?,?)
        """,
        (
            username,
            hash_password(data.password),
            now,
            now,
            username,
            ""
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
            "username": username,
            "display_name": username
        }
    }


# =========================================================
# LOGIN
# =========================================================

@app.post("/api/login")
def login(data: LoginRequest):

    connection = db()

    user = connection.execute(
        """
        SELECT *
        FROM users
        WHERE username=?
        """,
        (
            data.username.strip().lower(),
        )
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

    now = datetime.utcnow().isoformat()

    connection.execute(
        """
        UPDATE users
        SET last_seen=?
        WHERE id=?
        """,
        (
            now,
            user["id"]
        )
    )

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "token": create_token(
            user["id"]
        )
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
            created_at,
            last_seen
        FROM users
        WHERE id=?
        """,
        (user_id,)
    ).fetchone()

    connection.close()

    if not user:

        raise HTTPException(
            404,
            "Пользователь не найден"
        )

    return dict(user)


# =========================================================
# PROFILE
# =========================================================

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

    if not username.replace(
        "_",
        ""
    ).isalnum():

        raise HTTPException(
            400,
            "Username может содержать буквы, цифры и _"
        )

    if not display_name:

        display_name = username

    connection = db()

    existing = connection.execute(
        """
        SELECT id
        FROM users
        WHERE username=?
        AND id!=?
        """,
        (
            username,
            user_id
        )
    ).fetchone()

    if existing:

        connection.close()

        raise HTTPException(
            400,
            "Этот username уже занят"
        )

    connection.execute(
        """
        UPDATE users
        SET
            username=?,
            display_name=?,
            bio=?
        WHERE id=?
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

    return {
        "ok": True
    }


# =========================================================
# AVATAR
# =========================================================

@app.post("/api/avatar")
async def upload_avatar(
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

    extension = allowed[
        file.content_type
    ]

    filename = (
        "avatar_"
        + str(user_id)
        + "_"
        + secrets.token_hex(8)
        + extension
    )

    path = UPLOAD_DIR / filename

    with open(path,"wb") as output:

        shutil.copyfileobj(
            file.file,
            output
        )

    url="/uploads/"+filename

    connection=db()

    connection.execute(
        """
        UPDATE users
        SET avatar_url=?
        WHERE id=?
        """,
        (
            url,
            user_id
        )
    )

    connection.commit()
    connection.close()

    return {
        "ok":True,
        "avatar_url":url
    }


# =========================================================
# USERS
# =========================================================

@app.get("/api/users")
def search_users(
    request: Request,
    q: str=""
):

    user_id=get_auth_user(request)

    connection=db()

    users=connection.execute(
        """
        SELECT
            id,
            username,
            display_name,
            avatar_url,
            bio,
            last_seen
        FROM users
        WHERE
            (
                username LIKE ?
                OR display_name LIKE ?
            )
            AND id!=?
        ORDER BY username
        LIMIT 50
        """,
        (
            "%"+q.strip()+"%",
            "%"+q.strip()+"%",
            user_id
        )
    ).fetchall()

    connection.close()

    return [
        dict(user)
        for user in users
    ]


# =========================================================
# MESSAGES
# =========================================================

@app.get("/api/messages/{other_user_id}")
def get_messages(
    other_user_id:int,
    request:Request
):

    user_id=get_auth_user(request)

    connection=db()

    messages=connection.execute(
        """
        SELECT
            id,
            sender_id,
            receiver_id,
            CASE
                WHEN deleted=1
                THEN '[Сообщение удалено]'
                ELSE text
            END AS text,
            created_at,
            is_read,
            edited,
            deleted
        FROM messages
        WHERE
            (
                sender_id=?
                AND receiver_id=?
            )
            OR
            (
                sender_id=?
                AND receiver_id=?
            )
        ORDER BY id ASC
        """,
        (
            user_id,
            other_user_id,
            other_user_id,
            user_id
        )
    ).fetchall()

    connection.execute(
        """
        UPDATE messages
        SET is_read=1
        WHERE
            sender_id=?
            AND receiver_id=?
        """,
        (
            other_user_id,
            user_id
        )
    )

    connection.commit()
    connection.close()

    return [
        dict(message)
        for message in messages
    ]


@app.post("/api/messages")
async def send_message(
    data:MessageRequest,
    request:Request
):

    sender_id=get_auth_user(request)

    text=data.text.strip()

    if not text:

        raise HTTPException(
            400,
            "Пустое сообщение"
        )

    if len(text)>5000:

        raise HTTPException(
            400,
            "Сообщение слишком длинное"
        )

    if sender_id==data.receiver_id:

        raise HTTPException(
            400,
            "Нельзя отправить сообщение самому себе"
        )

    connection=db()

    receiver=connection.execute(
        """
        SELECT id
        FROM users
        WHERE id=?
        """,
        (
            data.receiver_id,
        )
    ).fetchone()

    if not receiver:

        connection.close()

        raise HTTPException(
            404,
            "Пользователь не найден"
        )

    now=datetime.utcnow().isoformat()

    cursor=connection.execute(
        """
        INSERT INTO messages
        (
            sender_id,
            receiver_id,
            text,
            created_at
        )
        VALUES(?,?,?,?,?)
        """,
        (
            sender_id,
            data.receiver_id,
            text,
            now
        )
    )

    message_id=cursor.lastrowid

    connection.commit()
    connection.close()

    message={
        "id":message_id,
        "sender_id":sender_id,
        "receiver_id":data.receiver_id,
        "text":text,
        "created_at":now,
        "is_read":0,
        "edited":0,
        "deleted":0
    }

    await broadcast_user(
        data.receiver_id,
        {
            "type":"message",
            "message":message
        }
    )

    await broadcast_user(
        sender_id,
        {
            "type":"message",
            "message":message
        }
    )

    return {
        "ok":True,
        "message":message
    }


# =========================================================
# EDIT MESSAGE
# =========================================================

@app.put("/api/messages/{message_id}")
def edit_message(
    message_id:int,
    data:EditMessageRequest,
    request:Request
):

    user_id=get_auth_user(request)

    text=data.text.strip()

    if not text:

        raise HTTPException(
            400,
            "Пустое сообщение"
        )

    connection=db()

    message=connection.execute(
        """
        SELECT *
        FROM messages
        WHERE id=?
        """,
        (message_id,)
    ).fetchone()

    if not message:

        connection.close()

        raise HTTPException(
            404,
            "Сообщение не найдено"
        )

    if message["sender_id"]!=user_id:

        connection.close()

        raise HTTPException(
            403,
            "Можно изменять только свои сообщения"
        )

    connection.execute(
        """
        UPDATE messages
        SET
            text=?,
            edited=1
        WHERE id=?
        """,
        (
            text,
            message_id
        )
    )

    connection.commit()
    connection.close()

    return {
        "ok":True
    }


# =========================================================
# DELETE MESSAGE
# =========================================================

@app.delete("/api/messages/{message_id}")
def delete_message(
    message_id:int,
    request:Request
):

    user_id=get_auth_user(request)

    connection=db()

    message=connection.execute(
        """
        SELECT *
        FROM messages
        WHERE id=?
        """,
        (message_id,)
    ).fetchone()

    if not message:

        connection.close()

        raise HTTPException(
            404,
            "Сообщение не найдено"
        )

    if message["sender_id"]!=user_id:

        connection.close()

        raise HTTPException(
            403,
            "Можно удалять только свои сообщения"
        )

    connection.execute(
        """
        UPDATE messages
        SET
            deleted=1,
            text='[Сообщение удалено]'
        WHERE id=?
        """,
        (message_id,)
    )

    connection.commit()
    connection.close()

    return {
        "ok":True
    }


# =========================================================
# FEED
# =========================================================

@app.get("/api/feed")
def feed(request:Request):

    get_auth_user(request)

    connection=db()

    posts=connection.execute(
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
                WHERE post_id=p.id
            ) AS likes,
            (
                SELECT COUNT(*)
                FROM comments
                WHERE post_id=p.id
            ) AS comments
        FROM posts p
        JOIN users u
            ON u.id=p.author_id
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
    data:PostRequest,
    request:Request
):

    user_id=get_auth_user(request)

    text=data.text.strip()

    if not text:

        raise HTTPException(
            400,
            "Напиши текст поста"
        )

    if len(text)>5000:

        raise HTTPException(
            400,
            "Пост слишком длинный"
        )

    connection=db()

    now=datetime.utcnow().isoformat()

    cursor=connection.execute(
        """
        INSERT INTO posts
        (
            author_id,
            text,
            created_at
        )
        VALUES(?,?,?)
        """,
        (
            user_id,
            text,
            now
        )
    )

    post_id=cursor.lastrowid

    connection.commit()
    connection.close()

    return {
        "ok":True,
        "id":post_id
    }


# =========================================================
# MEDIA POST
# =========================================================

@app.post("/api/posts/media")
async def create_media_post(
    request:Request,
    text:str="",
    file:UploadFile=File(...)
):

    user_id=get_auth_user(request)

    allowed={
        "image/jpeg":(".jpg","image"),
        "image/png":(".png","image"),
        "image/webp":(".webp","image"),
        "video/mp4":(".mp4","video"),
        "video/webm":(".webm","video"),
        "video/quicktime":(".mov","video")
    }

    if file.content_type not in allowed:

        raise HTTPException(
            400,
            "Этот формат файла не поддерживается"
        )

    extension,media_type=allowed[
        file.content_type
    ]

    filename=(
        "post_"
        +str(user_id)
        +"_"
        +secrets.token_hex(10)
        +extension
    )

    path=UPLOAD_DIR/filename

    with open(path,"wb") as output:

        shutil.copyfileobj(
            file.file,
            output
        )

    url="/uploads/"+filename

    connection=db()

    now=datetime.utcnow().isoformat()

    cursor=connection.execute(
        """
        INSERT INTO posts
        (
            author_id,
            text,
            media_url,
            media_type,
            created_at
        )
        VALUES(?,?,?,?,?)
        """,
        (
            user_id,
            text.strip(),
            url,
            media_type,
            now
        )
    )

    post_id=cursor.lastrowid

    connection.commit()
    connection.close()

    return {
        "ok":True,
        "id":post_id,
        "media_url":url
    }


# =========================================================
# LIKE
# =========================================================

@app.post("/api/posts/{post_id}/like")
def like_post(
    post_id:int,
    request:Request
):

    user_id=get_auth_user(request)

    connection=db()

    existing=connection.execute(
        """
        SELECT id
        FROM post_likes
        WHERE
            post_id=?
            AND user_id=?
        """,
        (
            post_id,
            user_id
        )
    ).fetchone()

    if existing:

        connection.execute(
            """
            DELETE FROM post_likes
            WHERE
                post_id=?
                AND user_id=?
            """,
            (
                post_id,
                user_id
            )
        )

        liked=False

    else:

        connection.execute(
            """
            INSERT INTO post_likes
            (
                post_id,
                user_id
            )
            VALUES(?,?)
            """,
            (
                post_id,
                user_id
            )
        )

        liked=True

    count=connection.execute(
        """
        SELECT COUNT(*)
        FROM post_likes
        WHERE post_id=?
        """,
        (post_id,)
    ).fetchone()[0]

    connection.commit()
    connection.close()

    return {
        "liked":liked,
        "likes":count
    }


# =========================================================
# COMMENTS
# =========================================================

@app.get("/api/posts/{post_id}/comments")
def get_comments(
    post_id:int,
    request:Request
):

    get_auth_user(request)

    connection=db()

    comments=connection.execute(
        """
        SELECT
            c.id,
            c.post_id,
            c.user_id,
            c.text,
            c.created_at,
            c.reply_to_id,
            u.username,
            u.display_name,
            u.avatar_url,
            reply.username AS reply_to_username
        FROM comments c

        JOIN users u
            ON u.id=c.user_id

        LEFT JOIN comments parent
            ON parent.id=c.reply_to_id

        LEFT JOIN users reply
            ON reply.id=parent.user_id

        WHERE c.post_id=?

        ORDER BY c.id ASC
        """,
        (post_id,)
    ).fetchall()

    connection.close()

    return [
        dict(comment)
        for comment in comments
    ]


@app.post("/api/posts/{post_id}/comments")
def create_comment(
    post_id:int,
    data:CommentRequest,
    request:Request
):

    user_id=get_auth_user(request)

    text=data.text.strip()

    if not text:

        raise HTTPException(
            400,
            "Пустой комментарий"
        )

    connection=db()

    exists=connection.execute(
        """
        SELECT id
        FROM posts
        WHERE id=?
        """,
        (post_id,)
    ).fetchone()

    if not exists:

        connection.close()

        raise HTTPException(
            404,
            "Пост не найден"
        )

    if data.reply_to_id:

        parent=connection.execute(
            """
            SELECT id
            FROM comments
            WHERE
                id=?
                AND post_id=?
            """,
            (
                data.reply_to_id,
                post_id
            )
        ).fetchone()

        if not parent:

            connection.close()

            raise HTTPException(
                400,
                "Комментарий для ответа не найден"
            )

    now=datetime.utcnow().isoformat()

    cursor=connection.execute(
        """
        INSERT INTO comments
        (
            post_id,
            user_id,
            text,
            created_at,
            reply_to_id
        )
        VALUES(?,?,?,?,?)
        """,
        (
            post_id,
            user_id,
            text,
            now,
            data.reply_to_id
        )
    )

    comment_id=cursor.lastrowid

    connection.commit()
    connection.close()

    return {
        "ok":True,
        "id":comment_id
    }


# =========================================================
# DELETE COMMENT
# =========================================================

@app.delete("/api/comments/{comment_id}")
def delete_comment(
    comment_id:int,
    request:Request
):

    user_id=get_auth_user(request)

    connection=db()

    comment=connection.execute(
        """
        SELECT *
        FROM comments
        WHERE id=?
        """,
        (comment_id,)
    ).fetchone()

    if not comment:

        connection.close()

        raise HTTPException(
            404,
            "Комментарий не найден"
        )

    if comment["user_id"]!=user_id:

        connection.close()

        raise HTTPException(
            403,
            "Можно удалять только свои комментарии"
        )

    connection.execute(
        """
        DELETE FROM comments
        WHERE id=?
        """,
        (comment_id,)
    )

    connection.commit()
    connection.close()

    return {
        "ok":True
    }


# =========================================================
# DELETE POST
# =========================================================

@app.delete("/api/posts/{post_id}")
def delete_post(
    post_id:int,
    request:Request
):

    user_id=get_auth_user(request)

    connection=db()

    post=connection.execute(
        """
        SELECT *
        FROM posts
        WHERE id=?
        """,
        (post_id,)
    ).fetchone()

    if not post:

        connection.close()

        raise HTTPException(
            404,
            "Пост не найден"
        )

    if post["author_id"]!=user_id:

        connection.close()

        raise HTTPException(
            403,
            "Это не твой пост"
        )

    connection.execute(
        "DELETE FROM comments WHERE post_id=?",
        (post_id,)
    )

    connection.execute(
        "DELETE FROM post_likes WHERE post_id=?",
        (post_id,)
    )

    connection.execute(
        "DELETE FROM posts WHERE id=?",
        (post_id,)
    )

    connection.commit()
    connection.close()

    if post["media_url"]:

        filename=Path(
            post["media_url"]
        ).name

        path=UPLOAD_DIR/filename

        if path.exists():

            try:
                path.unlink()
            except Exception:
                pass

    return {
        "ok":True
    }


# =========================================================
# WEBSOCKET HELPERS
# =========================================================

async def broadcast_user(
    user_id,
    data
):

    dead=[]

    for websocket in list(
        connections.get(user_id,set())
    ):

        try:

            await websocket.send_json(data)

        except Exception:

            dead.append(websocket)

    for websocket in dead:

        connections[user_id].discard(
            websocket
        )

    if not connections[user_id]:

        connections.pop(
            user_id,
            None
        )


# =========================================================
# WEBSOCKET
# =========================================================

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(
    websocket:WebSocket,
    user_id:int
):

    await websocket.accept()

    connections[user_id].add(
        websocket
    )

    try:

        connection=db()

        connection.execute(
            """
            UPDATE users
            SET last_seen=?
            WHERE id=?
            """,
            (
                datetime.utcnow().isoformat(),
                user_id
            )
        )

        connection.commit()
        connection.close()

        while True:

            data=await websocket.receive_json()

            if data.get("type")=="ping":

                await websocket.send_json({
                    "type":"pong"
                })

    except WebSocketDisconnect:

        connections[user_id].discard(
            websocket
        )

        if not connections[user_id]:

            connections.pop(
                user_id,
                None
            )

    except Exception:

        connections[user_id].discard(
            websocket
        )

        if not connections[user_id]:

            connections.pop(
                user_id,
                None
            )


# =========================================================
# FILES
# =========================================================

app.mount(
    "/uploads",
    StaticFiles(
        directory=str(UPLOAD_DIR)
    ),
    name="uploads"
)


STATIC_DIR=BASE_DIR/"static"

STATIC_DIR.mkdir(
    exist_ok=True
)

app.mount(
    "/static",
    StaticFiles(
        directory=str(STATIC_DIR)
    ),
    name="static"
)


# =========================================================
# FRONTEND
# =========================================================

@app.get("/")
def index():

    return FileResponse(
        str(
            STATIC_DIR/"index.html"
        )
    )
