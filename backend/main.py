from datetime import datetime, timedelta
import os
import uuid
import shutil
import logging

from fastapi import FastAPI, HTTPException, Depends, Header, File, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer


from supabase import create_client
from jose import jwt
from passlib.context import CryptContext

# =========================
# CONFIG
# =========================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
UPLOAD_DIR = "./uploads"
STATIC_DIR = "./static"
FRONTEND_HTML = "index.html"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

SECRET_KEY = os.getenv("SECRET_KEY", "!QuoteMe_ZW@2026")
ALGORITHM = "HS256"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# =========================
# APP
# =========================
app = FastAPI(title="QuoteMe Supabase API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def handle_head_requests(request, call_next):
    if request.method == "HEAD":
        response = await call_next(request)
        return Response(
            content=None,
            status_code=response.status_code,
            headers=response.headers,
            media_type=response.media_type,
        )
    return await call_next(request)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


@app.get("/")
def home():
    return FileResponse("static/index.html")

@app.get("/admin")
def admin_page():
    return FileResponse("static/admin.html")

@app.get("/dashboard")
def dashboard_page():
    return FileResponse("static/dashboard.html")

@app.get("/story/{story_id}")
def story_page(story_id: int):
    return FileResponse("static/story.html")

# =========================
# AUTH HELPERS
# =========================
def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("username")
    except:
        return None


def require_admin(authorization: str = Header(...)):
    token = authorization.replace("Bearer ", "")
    user = verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user

def get_current_user(token: str = Depends(oauth2_scheme)):
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    username = payload.get("username")

    if not username:
        raise HTTPException(status_code=401, detail="Invalid token")

    return {"username": username}

# ==============================
# 💬 SENTIMENT
# ==============================
POS = {"love","great","amazing","wonderful","inspiring","beautiful","excellent","fantastic","awesome","good","brilliant","perfect","happy","joy","thank","best","outstanding"}
NEG = {"hate","terrible","awful","bad","horrible","worst","ugly","boring","disappointing","sad","angry","useless","poor","disgusting","failed","wrong"}

def sentiment(text):
    words = set(text.lower().split())
    if len(words & POS) > len(words & NEG):
        return "positive"
    elif len(words & NEG) > len(words & POS):
        return "negative"
    return "neutral"

# =========================
# ADMIN LOGIN
# =========================
@app.post("/admin/login")
def admin_login(data: dict):
    username_input = data.get("username")
    password = data.get("password")

    if not username_input or not password:
        raise HTTPException(status_code=400, detail="Missing credentials")

    username = username_input.strip().lower()

    res = supabase.table("admins")\
        .select("*")\
        .ilike("username", username)\
        .execute()

    if not res.data:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user = res.data[0]

    if not pwd_context.verify(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = jwt.encode({
        "username": username,
        "exp": datetime.utcnow() + timedelta(hours=12)
    }, SECRET_KEY, algorithm=ALGORITHM)

    return {"token": token}

# =========================
# SETTINGS
# =========================



@app.get("/settings")
def settings_alias():
    res = supabase.table("admin_settings").select("*").limit(1).execute()
    return res.data[0] if res.data else {}

@app.get("/admin/settings")
def get_admin_settings(username: str = Depends(require_admin)):
    admin = supabase.table("admins").select("*").eq("username", username).execute().data

    if not admin:
        raise HTTPException(status_code=404, detail="Admin not found")

    admin_id = admin[0]["id"]

    res = supabase.table("admin_settings")\
        .select("*")\
        .eq("admin_id", admin_id)\
        .execute()

    return res.data[0] if res.data else {}    


@app.put("/admin/settings")
def update_settings(data: dict, username: str = Depends(require_admin)):
    # get admin id first
    admin = supabase.table("admins").select("*").eq("username", username).execute().data

    if not admin:
        raise HTTPException(status_code=404, detail="Admin not found")

    admin_id = admin[0]["id"]

    res = supabase.table("admin_settings")\
        .update(data)\
        .eq("admin_id", admin_id)\
        .execute()

    return {"success": True, "data": res.data}


# =========================
# STATS DASHBOARD
# =========================
@app.get("/admin/stats")
def stats(username: str = Depends(require_admin)):
    return {
        "quotes": len(supabase.table("quotes").select("*").execute().data),
        "stories": len(supabase.table("stories").select("*").execute().data),
        "blogs": len(supabase.table("blogs").select("*").execute().data),
        "comments": len(supabase.table("comments").select("*").execute().data),
        "forumpost": len(supabase.table("forumpost").select("*").execute().data),
    }

# =========================
# UPLOAD IMAGE
# =========================
@app.post("/upload-image")
def upload(
    file: UploadFile = File(...),
    username: str = Depends(require_admin)
):
    # =========================
    # VALIDATE FILE NAME
    # =========================
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    ext = os.path.splitext(file.filename)[1].lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
        )

    # =========================
    # SAFE FILE NAME GENERATION
    # =========================
    filename = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join(UPLOAD_DIR, filename)

    # =========================
    # SAVE FILE SAFELY
    # =========================
    try:
        with open(path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

    # =========================
    # RETURN PUBLIC URL
    # =========================
    return {
        "success": True,
        "url": f"/uploads/{filename}"
    }


# =========================
# QUOTES
# =========================
@app.get("/quotes")
def get_quotes():
    try:
        res = supabase.table("quotes").select("*").execute()
        return res.data
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/quotes")
def create_quote(data: dict, username: str = Depends(require_admin)):
    res = supabase.table("quotes").insert(data).execute()
    return res.data


@app.put("/quotes/{quote_id}")
def update_quote(quote_id: int, data: dict, username: str = Depends(require_admin)):
    res = supabase.table("quotes").update(data).eq("id", quote_id).execute()
    return res.data


@app.delete("/quotes/{quote_id}")
def delete_quote(quote_id: int, username: str = Depends(require_admin)):
    supabase.table("quotes").delete().eq("id", quote_id).execute()
    return {"message": "Deleted"}


# =========================
# STORIES
# =========================
@app.get("/stories")
def get_stories():
    try:
        return supabase.table("stories").select("*").execute().data
    except Exception as e:
        logger.error(f"get_stories: {e}")
        raise HTTPException(500, str(e))


@app.get("/stories/{story_id}")
def get_story(story_id: int):
    try:
        res = supabase.table("stories").select("*").eq("id", story_id).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Story not found")
        return res.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"get_story {story_id}: {e}")
        raise HTTPException(500, str(e))


@app.post("/stories")
def create_story(data: dict, username: str = Depends(require_admin)):
    return supabase.table("stories").insert(data).execute().data


# =========================
# BLOGS
# =========================
@app.get("/blogs")
def get_blogs():
    return supabase.table("blogs").select("*").execute().data


@app.post("/blogs")
def create_blog(data: dict, username: str = Depends(require_admin)):
    return supabase.table("blogs").insert(data).execute().data


# =========================
# LIKES
# =========================
@app.post("/like/{item_type}/{item_id}")
def like(item_type: str, item_id: int):
    if item_type not in {"quote", "story", "blog"}:
        raise HTTPException(status_code=400, detail="Invalid item type")
    table = item_type + "s"
    try:
        rows = supabase.table(table).select("id, likes").eq("id", item_id).execute().data
        if not rows:
            raise HTTPException(status_code=404, detail="Not found")
        current = rows[0].get("likes") or 0
        new_val = current + 1
        supabase.table(table).update({"likes": new_val}).eq("id", item_id).execute()
        logger.info(f"Like: {table} id={item_id} likes={new_val}")
        return {"likes": new_val}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"like {item_type}/{item_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not update likes")


# =========================
# COMMENTS
# =========================
@app.get("/comments/{item_type}/{item_id}")
def get_comments(item_type: str, item_id: int):
    return supabase.table("comments")\
        .select("*")\
        .eq("item_type", item_type)\
        .eq("item_id", item_id)\
        .execute().data


# ── SENTIMENT & TOXICITY HELPERS ──
_POS = {"love","great","amazing","wonderful","inspiring","beautiful","excellent",
        "fantastic","awesome","good","brilliant","perfect","happy","joy","thank","best"}
_NEG = {"hate","terrible","awful","bad","horrible","worst","ugly","boring",
        "disappointing","sad","angry","useless","poor","disgusting","failed","wrong"}
_FLAGGED = {"murder","kill","attack","abuse","rape","bomb","terrorist"}
_TOXIC   = {"hate","stupid","idiot","ugly","horrible","disgusting","awful",
            "terrible","worst","dumb","moron","loser","trash"}

def _sentiment(text: str) -> str:
    words = set(text.lower().split())
    pos, neg = len(words & _POS), len(words & _NEG)
    if pos > neg: return "positive"
    if neg > pos: return "negative"
    return "neutral"

def _toxicity(text: str) -> float:
    words = set(text.lower().split())
    f = len(words & _FLAGGED)
    t = len(words & _TOXIC)
    if f > 0: return min(0.70 + f * 0.10, 1.0)
    if t > 0: return min(0.30 + t * 0.10, 0.69)
    return 0.0


@app.post("/comments")
def add_comment(data: dict):
    """
    Safe comment insert.
    Tries to save with `toxicity` first.
    If the column does not exist in Supabase it retries without it.
    Add the column with:  ALTER TABLE comments ADD COLUMN IF NOT EXISTS toxicity float4 DEFAULT 0;
    """
    text      = (data.get("content")   or "").strip()
    username  = (data.get("username")  or "").strip()
    item_type = (data.get("item_type") or "").strip()
    item_id   = data.get("item_id")

    if not text:
        raise HTTPException(status_code=400, detail="Comment content is required")
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    if not item_type or item_id is None:
        raise HTTPException(status_code=400, detail="item_type and item_id are required")

    payload = {
        "username":  username,
        "content":   text,
        "item_type": item_type,
        "item_id":   int(item_id),
        "sentiment": _sentiment(text),
    }

    # Attempt 1: include toxicity score
    try:
        res = supabase.table("comments").insert({**payload, "toxicity": _toxicity(text)}).execute()
        if res.data:
            logger.info(f"Comment saved (with toxicity) id={res.data[0].get('id')}")
            return res.data
    except Exception as e:
        err = str(e).lower()
        if any(kw in err for kw in ["toxicity", "column", "schema", "undefined", "does not exist", "not found"]):
            logger.warning(f"toxicity column missing, retrying without it ({e})")
        else:
            logger.error(f"add_comment error: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to save comment: {e}")

    # Attempt 2: without toxicity
    try:
        res = supabase.table("comments").insert(payload).execute()
        if res.data:
            logger.info(f"Comment saved (no toxicity) id={res.data[0].get('id')}")
            return res.data
        raise ValueError("Supabase returned empty data")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"add_comment retry error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save comment: {e}")


@app.delete("/comments/{comment_id}")
def delete_comment(comment_id: int, username: str = Depends(require_admin)):
    """Delete a comment by ID. Requires admin Authorization header."""
    try:
        supabase.table("comments").delete().eq("id", comment_id).execute()
        logger.info(f"Admin '{username}' deleted comment {comment_id}")
        return {"message": "Comment deleted", "id": comment_id}
    except Exception as e:
        logger.error(f"delete_comment {comment_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete comment: {e}")


# =========================
# FORUM
# =========================
@app.get("/forum/posts")
def get_posts():
    return supabase.table("forumpost").select("*").execute().data


@app.post("/forum/post")
def create_post(data: dict):
    return supabase.table("forumpost").insert(data).execute().data


# =========================
# CONTACT
# =========================
@app.post("/contact/send")
def contact(data: dict):
    """Public contact form — no auth required."""
    try:
        return supabase.table("contactmessage").insert(data).execute().data
    except Exception as e:
        logger.error(f"contact/send: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================
# CHATBOT
# =========================
@app.post("/chatbot")
def chatbot(data: dict):
    msg = (data.get("message") or "").lower().strip()

    if not msg:
        return {"reply": "Please type a message 😊"}

    # =========================
    # GREETINGS
    # =========================
    if any(w in msg for w in ["hi", "hello", "hey", "good morning", "good evening"]):
        return {
            "reply": "Hey there 👋 Welcome to QuoteMe ZW 💖 I can help you find quotes, stories, blogs, or anything on the platform!"
        }

    # =========================
    # QUOTES
    # =========================
    if "quote" in msg:
        quotes = supabase.table("quotes").select("*").limit(3).execute().data

        if quotes:
            sample = "\n\n".join([f"💬 {q['text']} — {q.get('author','Unknown')}" for q in quotes])
            return {
                "reply": f"Here are some inspiring quotes for you ✨\n\n{sample}"
            }

        return {"reply": "We post daily inspirational quotes ✨"}

    # =========================
    # STORIES
    # =========================
    if "story" in msg:
        stories = supabase.table("stories").select("*").limit(2).execute().data

        if stories:
            sample = "\n\n".join([f"📖 {s['title']}\n{s['content'][:120]}..." for s in stories])
            return {
                "reply": f"Here are some empowerment stories 💖\n\n{sample}"
            }

        return {"reply": "We share powerful women empowerment stories 💖"}

    # =========================
    # BLOGS
    # =========================
    if "blog" in msg:
        blogs = supabase.table("blogs").select("*").limit(2).execute().data

        if blogs:
            sample = "\n\n".join([f"📰 {b['title']}\n{b['content'][:120]}..." for b in blogs])
            return {
                "reply": f"Here are some motivational blogs 🚀\n\n{sample}"
            }

        return {"reply": "Check our blog section for motivation 🚀"}

    # =========================
    # CONTACT / SUPPORT
    # =========================
    if any(w in msg for w in ["contact", "email", "reach", "support"]):
        return {
            "reply": "You can reach us via the Contact form on the homepage 📩 or email us at support@quotemezw.com"
        }

    # =========================
    # INSTAGRAM
    # =========================
    if "instagram" in msg or "social" in msg:
        return {
            "reply": "Follow us on Instagram @quoteme_zw 📸 for daily inspiration and updates!"
        }

    # =========================
    # ADMIN HELP
    # =========================
    if "admin" in msg:
        return {
            "reply": "Admin panel is available at /admin.html 🔐 Only authorized users can log in."
        }

    # =========================
    # ABOUT
    # =========================
    if any(w in msg for w in ["what is this", "about", "who are you"]):
        return {
            "reply": "QuoteMe ZW is a motivational platform sharing quotes, stories, and blogs to inspire women and youth across Zimbabwe 💖"
        }

    # =========================
    # HELP
    # =========================
    if any(w in msg for w in ["help", "what can you do"]):
        return {
            "reply": (
                "I can help you with:\n"
                "✨ Quotes\n"
                "📖 Stories\n"
                "📰 Blogs\n"
                "📩 Contact info\n"
                "📸 Instagram\n\n"
                "Just ask me anything!"
            )
        }

    # =========================
    # FALLBACK SMART RESPONSE
    # =========================
    return {
        "reply": (
            "I'm not fully sure what you're asking yet 🤔\n\n"
            "Try asking about:\n"
            "- quotes\n"
            "- stories\n"
            "- blogs\n"
            "- contact info\n\n"
            "Or just say 'help' 😊"
        )
    }