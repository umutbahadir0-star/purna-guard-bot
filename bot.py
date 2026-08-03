import os
import time
import json
import logging
import aiosqlite
from collections import defaultdict
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from telegram import Update, ChatMember
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)
from telegram.constants import ParseMode, MessageEntityType

# ================== AYARLAR ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "tornado123")
SECRET_KEY = os.getenv("SECRET_KEY", "tornado-gizli-key-123")
DB_PATH = "data/guard.db"
PORT = int(os.getenv("PORT", "8080"))

FLOOD_LIMIT = 6
FLOOD_SECONDS = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULTS = {
    "flood": True,
    "anti_link": False,
    "anti_forward": False,
    "anti_service": True,
    "strict_media": False,
}

LABELS = {
    "flood": "🌊 Flood / Spam koruma",
    "anti_link": "🔗 Link engelle",
    "anti_forward": "↪️ Forward engelle",
    "anti_service": "🧹 Servis mesaj sil",
    "strict_media": "🖼️ Tüm medya / sticker sil",
}

flood_tracker = defaultdict(lambda: defaultdict(list))

# ================== VERİTABANI ==================
async def init_db():
    Path("data").mkdir(exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER PRIMARY KEY,
                data TEXT,
                title TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bans (
                chat_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        await db.commit()

async def get_settings(chat_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT data FROM settings WHERE chat_id=?", (chat_id,)) as c:
            row = await c.fetchone()
            if row:
                d = json.loads(row[0])
                for k, v in DEFAULTS.items():
                    d.setdefault(k, v)
                return d
            return DEFAULTS.copy()

async def save_settings(chat_id: int, data: dict, title: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO settings (chat_id, data, title) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                data = excluded.data,
                title = COALESCE(NULLIF(excluded.title, ''), settings.title)
        """, (chat_id, json.dumps(data), title))
        await db.commit()

async def set_setting(chat_id: int, key: str, value: bool):
    s = await get_settings(chat_id)
    s[key] = value
    await save_settings(chat_id, s)
    return s

async def list_groups():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id, title, data FROM settings") as c:
            rows = await c.fetchall()
    result = []
    for cid, title, data in rows:
        s = json.loads(data) if data else DEFAULTS.copy()
        for k, v in DEFAULTS.items():
            s.setdefault(k, v)
        result.append({"chat_id": cid, "title": title or str(cid), "settings": s})
    return result

async def add_ban(chat_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO bans (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id))
        await db.commit()

async def remove_ban(chat_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bans WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        await db.commit()

async def is_banned(chat_id, user_id) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM bans WHERE chat_id=? AND user_id=?", (chat_id, user_id)) as c:
            return await c.fetchone() is not None

# ================== TELEGRAM YARDIMCI ==================
def is_flood(chat_id, user_id) -> bool:
    now = time.time()
    times = flood_tracker[chat_id][user_id]
    times[:] = [t for t in times if now - t < FLOOD_SECONDS]
    times.append(now)
    return len(times) > FLOOD_LIMIT

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, uid=None) -> bool:
    user = update.effective_user
    if not user:
        return False
    uid = uid or user.id
    if uid == OWNER_ID:
        return True
    if update.effective_chat.type not in ("group", "supergroup"):
        return False
    try:
        m = await context.bot.get_chat_member(update.effective_chat.id, uid)
        return m.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    except Exception:
        return False

def get_target(update, context):
    if update.message and update.message.reply_to_message:
        return update.message.reply_to_message.from_user.id
    if context.args:
        try:
            return int(context.args[0])
        except ValueError:
            pass
    return None

# ================== TELEGRAM KOMUTLAR ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌪️ <b>Tornado Guard Bot</b>\n\n"
        "Beni gruba ekle → admin yap.\n"
        "Ayarlar <b>web panelinden</b> yapılır.\n\n"
        "/id — ID öğren\n"
        "/admin — sınırlı yetki ver\n"
        "/unadmin — yetki al\n"
        "/kick — gruptan at\n"
        "/ban — kalıcı ban\n"
        "/unban — ban aç",
        parse_mode=ParseMode.HTML
    )

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    c = update.effective_chat
    text = f"👤 Kullanıcı ID: <code>{u.id}</code>\n💬 Grup ID: <code>{c.id}</code>"
    if c.title:
        text += f"\n📌 {c.title}"
    if c.type in ("group", "supergroup"):
        s = await get_settings(c.id)
        await save_settings(c.id, s, c.title or "")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.message.reply_text("❌ Yetkin yok")
    tid = get_target(update, context)
    if not tid:
        return await update.message.reply_text("Bir mesaja reply at veya /admin 123456789")
    try:
        await context.bot.promote_chat_member(
            update.effective_chat.id, tid,
            can_delete_messages=True,
            can_invite_users=True,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_pin_messages=False,
            can_manage_chat=False,
            can_manage_video_chats=False,
        )
        await update.message.reply_text(f"✅ <code>{tid}</code> sınırlı admin yapıldı", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def cmd_unadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.message.reply_text("❌ Yetkin yok")
    tid = get_target(update, context)
    if not tid:
        return await update.message.reply_text("Reply veya /unadmin 123456789")
    try:
        await context.bot.promote_chat_member(
            update.effective_chat.id, tid,
            can_delete_messages=False,
            can_invite_users=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_pin_messages=False,
            can_manage_chat=False,
            can_manage_video_chats=False,
        )
        await update.message.reply_text(f"✅ <code>{tid}</code> yetkisi alındı", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.message.reply_text("❌ Yetkin yok")
    tid = get_target(update, context)
    if not tid:
        return await update.message.reply_text("Reply veya /kick 123456789")
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, tid)
        await context.bot.unban_chat_member(update.effective_chat.id, tid)
        await update.message.reply_text(f"👢 <code>{tid}</code> atıldı", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.message.reply_text("❌ Yetkin yok")
    tid = get_target(update, context)
    if not tid:
        return await update.message.reply_text("Reply veya /ban 123456789")
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, tid)
        await add_ban(update.effective_chat.id, tid)
        await update.message.reply_text(f"🚫 <code>{tid}</code> banlandı", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.message.reply_text("❌ Yetkin yok")
    tid = get_target(update, context)
    if not tid:
        return await update.message.reply_text("/unban 123456789")
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, tid)
        await remove_ban(update.effective_chat.id, tid)
        await update.message.reply_text(f"✅ <code>{tid}</code> ban açıldı", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

# ================== KORUMA ==================
async def on_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if chat.type not in ("group", "supergroup"):
        return
    if user.is_bot:
        return

    s = await get_settings(chat.id)
    await save_settings(chat.id, s, chat.title or "")

    if await is_banned(chat.id, user.id):
        try:
            await msg.delete()
            await context.bot.ban_chat_member(chat.id, user.id)
        except Exception:
            pass
        return

    # Admin muafiyeti YOK — herkese uygulanır

    if s.get("flood") and is_flood(chat.id, user.id):
        try:
            await msg.delete()
        except Exception:
            pass
        return

    if s.get("anti_service") and (
        msg.new_chat_members or msg.left_chat_member or msg.new_chat_title
        or msg.new_chat_photo or msg.delete_chat_photo or msg.pinned_message
    ):
        try:
            await msg.delete()
        except Exception:
            pass
        return

    if s.get("anti_forward") and (msg.forward_origin or msg.forward_date):
        try:
            await msg.delete()
        except Exception:
            pass
        return

    if s.get("anti_link"):
        text = (msg.text or msg.caption or "").lower()
        has = "http://" in text or "https://" in text or "t.me/" in text
        for ents in (msg.entities or [], msg.caption_entities or []):
            for e in ents:
                if e.type in (MessageEntityType.URL, MessageEntityType.TEXT_LINK):
                    has = True
        if has:
            try:
                await msg.delete()
            except Exception:
                pass
            return

    if s.get("strict_media"):
        if msg.photo or msg.sticker or msg.animation or msg.video or (
            msg.document and (msg.document.mime_type or "").startswith("image/")
        ):
            try:
                await msg.delete()
            except Exception:
                pass

async def on_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    for m in msg.new_chat_members or []:
        if await is_banned(chat.id, m.id):
            try:
                await context.bot.ban_chat_member(chat.id, m.id)
            except Exception:
                pass
    s = await get_settings(chat.id)
    if s.get("anti_service"):
        try:
            await msg.delete()
        except Exception:
            pass

# ================== WEB PANEL ==================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("help", cmd_start))
    tg_app.add_handler(CommandHandler("id", cmd_id))
    tg_app.add_handler(CommandHandler("admin", cmd_admin))
    tg_app.add_handler(CommandHandler("unadmin", cmd_unadmin))
    tg_app.add_handler(CommandHandler("kick", cmd_kick))
    tg_app.add_handler(CommandHandler("ban", cmd_ban))
    tg_app.add_handler(CommandHandler("unban", cmd_unban))
    tg_app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_join))
    tg_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate.ALL, on_msg))

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(allowed_updates=["message"])
    app.state.tg_app = tg_app
    logger.info("Tornado Guard Bot + Web panel hazır")
    yield
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()

web = FastAPI(lifespan=lifespan)
web.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

@web.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return HTMLResponse("""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tornado Guard Giriş</title>
<style>
body{font-family:system-ui;background:#0f0f13;color:#eee;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
form{background:#1a1a24;padding:2rem;border-radius:16px;width:90%;max-width:340px}
input{width:100%;padding:14px;margin:10px 0;border:none;border-radius:10px;background:#2a2a3a;color:#fff;box-sizing:border-box;font-size:16px}
button{width:100%;padding:14px;background:#6c5ce7;color:#fff;border:none;border-radius:10px;font-weight:bold;font-size:16px;margin-top:8px}
h1{text-align:center;margin:0 0 1.2rem;font-size:1.6rem}
</style>
</head>
<body>
<form method="post" action="/login">
<h1>🌪️ Tornado Guard</h1>
<input type="password" name="password" placeholder="Şifre" required>
<button type="submit">Giriş Yap</button>
</form>
</body>
</html>
""")

@web.post("/login")
async def do_login(request: Request, password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=303)
    return HTMLResponse("<script>alert('Yanlış şifre');location='/login'</script>")

@web.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

@web.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not request.session.get("logged_in"):
        return RedirectResponse("/login", status_code=303)
    groups = await list_groups()
    html = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tornado Guard Panel</title>
<style>
body{font-family:system-ui;background:#0f0f13;color:#eee;margin:0;padding:16px}
h1{margin:0}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
.card{background:#1a1a24;border-radius:14px;padding:16px;margin-bottom:14px}
.row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #2a2a3a}
.row:last-child{border:none}
a.btn{background:#6c5ce7;color:#fff;border:none;padding:8px 14px;border-radius:8px;text-decoration:none;font-size:13px;font-weight:600}
a.off{background:#333}
small{color:#888}
h3{margin:0 0 4px}
.info{font-size:13px;line-height:1.55;color:#ccc}
.info b{color:#fff}
</style>
</head>
<body>
<div class="top">
<h1>🌪️ Tornado Guard</h1>
<a class="btn" href="/logout">Çıkış</a>
</div>

<div class="card">
<h3>📋 Komutlar & Bilgi</h3>
<div class="info">
<b>/id</b> — Kullanıcı + Grup ID gösterir<br>
<b>/admin</b> — Reply ile sınırlı yetki verir (silme + mesaj/görsel atma)<br>
<b>/unadmin</b> — Yetkiyi alır<br>
<b>/kick</b> — Gruptan atar<br>
<b>/ban</b> — Kalıcı banlar<br>
<b>/unban</b> — Banı açar<br><br>
<b>🌊 Flood</b> — Kısa sürede çok mesaj atanı siler (mute yok)<br>
<b>🔗 Link engelle</b> — Link içeren mesajları siler<br>
<b>↪️ Forward engelle</b> — İletilen (forward) mesajları siler<br>
<b>🧹 Servis mesaj</b> — Katıldı/ayrıldı vs. sistem mesajlarını siler<br>
<b>🖼️ Medya sil</b> — Foto, sticker, video, gif siler
</div>
</div>

<p style="color:#888;font-size:14px;margin:8px 0">Gruplar otomatik eklenir. Botu gruba ekleyip /id yaz.</p>
"""
    if not groups:
        html += '<div class="card">Henüz grup yok.<br>Botu gruba ekle ve bir kere <b>/id</b> yaz.</div>'
    for g in groups:
        html += f'<div class="card"><h3>{g["title"]}</h3><small>ID: {g["chat_id"]}</small>'
        for key, label in LABELS.items():
            on = g["settings"].get(key, False)
            cls = "" if on else " off"
            status = "AÇIK" if on else "KAPALI"
            html += f'''
            <div class="row">
                <span>{label}</span>
                <a class="btn{cls}" href="/toggle/{g["chat_id"]}/{key}">{status}</a>
            </div>'''
        html += f'''
        <div style="margin-top:14px">
            <a class="btn" href="/send/{g["chat_id"]}">📷 Mesaj / Foto Gönder</a>
        </div></div>'''
    html += "</body></html>"
    return HTMLResponse(html)

@web.get("/toggle/{chat_id}/{key}")
async def toggle(chat_id: int, key: str, request: Request):
    if not request.session.get("logged_in"):
        return RedirectResponse("/login", status_code=303)
    if key not in LABELS:
        return RedirectResponse("/", status_code=303)
    s = await get_settings(chat_id)
    await set_setting(chat_id, key, not s.get(key, False))
    return RedirectResponse("/", status_code=303)

@web.get("/send/{chat_id}", response_class=HTMLResponse)
async def send_page(chat_id: int, request: Request):
    if not request.session.get("logged_in"):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(f"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mesaj Gönder</title>
<style>
body{{font-family:system-ui;background:#0f0f13;color:#eee;padding:16px}}
textarea,input{{width:100%;padding:12px;margin:8px 0;border-radius:10px;border:none;background:#2a2a3a;color:#fff;box-sizing:border-box;font-size:16px}}
button{{background:#6c5ce7;color:#fff;border:none;padding:14px;border-radius:10px;width:100%;font-size:16px;font-weight:bold}}
a{{color:#6c5ce7;text-decoration:none}}
</style>
</head>
<body>
<a href="/">← Geri</a>
<h2>Gruba Gönder</h2>
<form method="post" action="/send/{chat_id}" enctype="multipart/form-data">
<textarea name="text" rows="4" placeholder="Mesaj yaz (isteğe bağlı)"></textarea>
<input type="file" name="photo" accept="image/*">
<button type="submit">Gönder</button>
</form>
</body>
</html>
""")

@web.post("/send/{chat_id}")
async def send_msg(
    chat_id: int,
    request: Request,
    text: str = Form(""),
    photo: UploadFile = File(None)
):
    if not request.session.get("logged_in"):
        return RedirectResponse("/login", status_code=303)
    tg = request.app.state.tg_app
    try:
        if photo and photo.filename:
            data = await photo.read()
            await tg.bot.send_photo(chat_id, photo=data, caption=text or None)
        elif text.strip():
            await tg.bot.send_message(chat_id, text)
        else:
            return HTMLResponse("<script>alert('Boş gönderilemez');history.back()</script>")
        return HTMLResponse("<script>alert('Gönderildi!');location='/'</script>")
    except Exception as e:
        return HTMLResponse(f"<script>alert('Hata: {str(e)}');history.back()</script>")

# ================== BAŞLAT ==================
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN eksik! Railway Variables'a ekle.")
    import uvicorn
    uvicorn.run(web, host="0.0.0.0", port=PORT)