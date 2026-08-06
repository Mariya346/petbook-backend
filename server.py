import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Dict
from google import genai
import asyncio
import bcrypt
import certifi
import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Header
from fastapi.responses import StreamingResponse
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from jose import jwt, JWTError
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr
from starlette.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(
    mongo_url,
    tlsCAFile=certifi.where()
)
db = client[os.environ['DB_NAME']]

JWT_SECRET = os.environ['JWT_SECRET']
JWT_ALGO = "HS256"
JWT_EXPIRE_DAYS = 30
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
STRIPE_API_KEY = os.environ.get('STRIPE_API_KEY', '')
GOOGLE_CLIENT_IDS = [c.strip() for c in os.environ.get('GOOGLE_CLIENT_IDS', '').split(',') if c.strip()]

stripe.api_key = STRIPE_API_KEY
client_ai = genai.Client(api_key=GEMINI_API_KEY)

GEMINI_MODEL = "gemini-flash-latest"
app = FastAPI(title="PETBOOK API")
api_router = APIRouter(prefix="/api")

# ---------------- Models ----------------
class UserRegister(BaseModel):
    email: EmailStr
    password: str
    name: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class GoogleAuthIn(BaseModel):
    id_token: str

class UserOut(BaseModel):
    id: str
    email: str
    name: str
    avatar: Optional[str] = None
    bio: Optional[str] = None
    location: Optional[str] = None
    followers: int = 0
    following: int = 0
    is_premium: bool = False

class AuthResponse(BaseModel):
    token: str
    user: UserOut

class PetIn(BaseModel):
    name: str
    species: str
    breed: str
    age: str
    gender: Optional[str] = "Unknown"
    bio: Optional[str] = ""
    image: Optional[str] = None  # base64 data URI
    color: Optional[str] = "#F2A63D"
    for_adoption: bool = False
    for_mating: bool = False

class Pet(PetIn):
    id: str
    owner_id: str
    owner_name: str
    created_at: str

class PostIn(BaseModel):
    caption: str
    image: Optional[str] = None  # base64 data URI
    pet_id: Optional[str] = None
    tags: List[str] = []
    is_reel: bool = False

class Post(BaseModel):
    id: str
    author_id: str
    author_name: str
    author_avatar: Optional[str] = None
    pet_id: Optional[str] = None
    pet_name: Optional[str] = None
    caption: str
    image: Optional[str] = None
    tags: List[str] = []
    likes: int = 0
    liked_by: List[str] = []
    comments_count: int = 0
    is_reel: bool = False
    created_at: str

class CommentIn(BaseModel):
    text: str

class Comment(BaseModel):
    id: str
    post_id: str
    author_id: str
    author_name: str
    text: str
    created_at: str

class Listing(BaseModel):
    id: str
    title: str
    price: str
    price_cents: Optional[int] = None
    category: str
    description: str
    image: Optional[str] = None
    color: str = "#F2A63D"
    location: str = ""
    verified: bool = False
    owner_id: Optional[str] = None
    owner_name: Optional[str] = None
    created_at: str

class Service(BaseModel):
    id: str
    name: str
    category: str
    distance_km: float
    rating: float
    reviews: int
    open_status: str
    icon: str
    color: str
    lat: float = 19.076
    lng: float = 72.877

class HealthRecord(BaseModel):
    id: str
    pet_id: str
    title: str
    detail: str
    date: str
    status: str
    kind: str

class ChatMessageAI(BaseModel):
    role: str
    text: str

class AIChatIn(BaseModel):
    assistant: str
    history: List[ChatMessageAI] = []
    message: str

class MessageIn(BaseModel):
    text: str

class Message(BaseModel):
    id: str
    from_user: str
    from_name: str
    to_user: str
    text: str
    created_at: str

class CheckoutIn(BaseModel):
    kind: str  # "listing" or "premium"
    listing_id: Optional[str] = None
    amount_cents: Optional[int] = None
    origin_url: str

# ---------------- Helpers ----------------
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def hash_password(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()

def verify_password(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode(), h.encode())
    except Exception:
        return False

def make_token(user_id: str) -> str:
    payload = {"sub": user_id, "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)

async def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        user_id = payload.get("sub")
    except JWTError:
        raise HTTPException(401, "Invalid token")
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "password": 0})
    if not user:
        raise HTTPException(401, "User not found")
    return user

async def user_out_async(u: dict) -> UserOut:
    followers = await db.follows.count_documents({"followee_id": u["id"]})
    following = await db.follows.count_documents({"follower_id": u["id"]})
    sub = await db.subscriptions.find_one({"user_id": u["id"], "status": "active"}, {"_id": 0})
    return UserOut(
        id=u["id"], email=u["email"], name=u["name"],
        avatar=u.get("avatar"), bio=u.get("bio"), location=u.get("location"),
        followers=followers, following=following, is_premium=bool(sub),
    )

# ---------------- Auth ----------------
@api_router.post("/auth/register", response_model=AuthResponse)
async def register(body: UserRegister):
    existing = await db.users.find_one({"email": body.email.lower()})
    if existing:
        raise HTTPException(400, "Email already registered")
    user = {
        "id": str(uuid.uuid4()),
        "email": body.email.lower(),
        "password": hash_password(body.password),
        "name": body.name,
        "avatar": None,
        "bio": "New to PETBOOK \U0001F43E",
        "location": "",
        "created_at": now_iso(),
    }
    await db.users.insert_one(user)
    return AuthResponse(token=make_token(user["id"]), user=await user_out_async(user))

@api_router.post("/auth/login", response_model=AuthResponse)
async def login(body: UserLogin):
    user = await db.users.find_one({"email": body.email.lower()})
    if not user or not verify_password(body.password, user["password"]):
        raise HTTPException(401, "Invalid credentials")
    return AuthResponse(token=make_token(user["id"]), user=await user_out_async(user))

@api_router.get("/auth/me", response_model=UserOut)
async def me(user=Depends(get_current_user)):
    return await user_out_async(user)

@api_router.post("/auth/google", response_model=AuthResponse)
async def auth_google(body: GoogleAuthIn):
    """Verify a real Google ID token (issued by our own OAuth client) and return our app JWT."""
    try:
        idinfo = google_id_token.verify_oauth2_token(body.id_token, google_requests.Request())
    except ValueError:
        raise HTTPException(401, "Invalid Google token")

    if GOOGLE_CLIENT_IDS and idinfo.get("aud") not in GOOGLE_CLIENT_IDS:
        raise HTTPException(401, "Token was not issued for this app")
    if idinfo.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(401, "Invalid token issuer")

    email = (idinfo.get("email") or "").lower()
    if not email:
        raise HTTPException(400, "Google account has no email")
    if not idinfo.get("email_verified"):
        raise HTTPException(400, "Google email not verified")
    name = idinfo.get("name") or email.split("@")[0]
    picture = idinfo.get("picture")

    existing = await db.users.find_one({"email": email})
    if existing:
        update = {"name": name, "avatar": picture or existing.get("avatar")}
        await db.users.update_one({"id": existing["id"]}, {"$set": update})
        user = {**existing, **update}
    else:
        user = {
            "id": str(uuid.uuid4()),
            "email": email,
            # Random unusable password (Google users log in via /auth/google only)
            "password": hash_password(str(uuid.uuid4())),
            "name": name,
            "avatar": picture,
            "bio": "New to PETBOOK \U0001F43E",
            "location": "",
            "provider": "google",
            "created_at": now_iso(),
        }
        await db.users.insert_one(dict(user))

    return AuthResponse(token=make_token(user["id"]), user=await user_out_async(user))

# ---------------- Users / Follow ----------------
@api_router.get("/users", response_model=List[UserOut])
async def list_users(search: Optional[str] = None, user=Depends(get_current_user)):
    q = {"id": {"$ne": user["id"]}}
    if search:
        q["name"] = {"$regex": search, "$options": "i"}
    docs = await db.users.find(q, {"_id": 0, "password": 0}).limit(100).to_list(100)
    return [await user_out_async(u) for u in docs]

@api_router.post("/users/{user_id}/follow")
async def toggle_follow(user_id: str, user=Depends(get_current_user)):
    if user_id == user["id"]:
        raise HTTPException(400, "Cannot follow yourself")
    existing = await db.follows.find_one({"follower_id": user["id"], "followee_id": user_id})
    if existing:
        await db.follows.delete_one({"follower_id": user["id"], "followee_id": user_id})
        return {"following": False}
    await db.follows.insert_one({
        "id": str(uuid.uuid4()), "follower_id": user["id"], "followee_id": user_id, "created_at": now_iso(),
    })
    return {"following": True}

@api_router.get("/users/{user_id}/follow-status")
async def follow_status(user_id: str, user=Depends(get_current_user)):
    ex = await db.follows.find_one({"follower_id": user["id"], "followee_id": user_id})
    return {"following": bool(ex)}

# ---------------- Pets ----------------
@api_router.get("/pets", response_model=List[Pet])
async def list_pets(owner_id: Optional[str] = None, species: Optional[str] = None,
                    for_adoption: Optional[bool] = None, for_mating: Optional[bool] = None):
    q = {}
    if owner_id: q["owner_id"] = owner_id
    if species: q["species"] = species
    if for_adoption is not None: q["for_adoption"] = for_adoption
    if for_mating is not None: q["for_mating"] = for_mating
    items = await db.pets.find(q, {"_id": 0}).to_list(500)
    return items

@api_router.post("/pets", response_model=Pet)
async def create_pet(body: PetIn, user=Depends(get_current_user)):
    pet = {
        "id": str(uuid.uuid4()),
        "owner_id": user["id"],
        "owner_name": user["name"],
        "created_at": now_iso(),
        **body.model_dump(),
    }
    await db.pets.insert_one(pet)
    pet.pop("_id", None)
    return pet

@api_router.get("/pets/{pet_id}", response_model=Pet)
async def get_pet(pet_id: str):
    pet = await db.pets.find_one({"id": pet_id}, {"_id": 0})
    if not pet: raise HTTPException(404, "Pet not found")
    return pet

# ---------------- Posts / Reels ----------------
@api_router.get("/posts", response_model=List[Post])
async def list_posts(is_reel: Optional[bool] = None):
    q = {}
    if is_reel is not None: q["is_reel"] = is_reel
    items = await db.posts.find(q, {"_id": 0}).sort("created_at", -1).to_list(300)
    return items

@api_router.post("/posts", response_model=Post)
async def create_post(body: PostIn, user=Depends(get_current_user)):
    pet_name = None
    if body.pet_id:
        pet = await db.pets.find_one({"id": body.pet_id}, {"_id": 0})
        if pet: pet_name = pet.get("name")
    post = {
        "id": str(uuid.uuid4()),
        "author_id": user["id"],
        "author_name": user["name"],
        "author_avatar": user.get("avatar"),
        "pet_id": body.pet_id,
        "pet_name": pet_name,
        "caption": body.caption,
        "image": body.image,
        "tags": body.tags,
        "is_reel": body.is_reel,
        "likes": 0,
        "liked_by": [],
        "comments_count": 0,
        "created_at": now_iso(),
    }
    await db.posts.insert_one(post)
    post.pop("_id", None)
    return post

@api_router.post("/posts/{post_id}/like")
async def toggle_like(post_id: str, user=Depends(get_current_user)):
    post = await db.posts.find_one({"id": post_id}, {"_id": 0})
    if not post: raise HTTPException(404, "Post not found")
    liked_by = post.get("liked_by", [])
    if user["id"] in liked_by:
        liked_by.remove(user["id"])
    else:
        liked_by.append(user["id"])
    await db.posts.update_one({"id": post_id}, {"$set": {"liked_by": liked_by, "likes": len(liked_by)}})
    return {"likes": len(liked_by), "liked": user["id"] in liked_by}

@api_router.get("/posts/{post_id}/comments", response_model=List[Comment])
async def list_comments(post_id: str):
    items = await db.comments.find({"post_id": post_id}, {"_id": 0}).sort("created_at", 1).to_list(500)
    return items

@api_router.post("/posts/{post_id}/comments", response_model=Comment)
async def add_comment(post_id: str, body: CommentIn, user=Depends(get_current_user)):
    c = {
        "id": str(uuid.uuid4()),
        "post_id": post_id,
        "author_id": user["id"],
        "author_name": user["name"],
        "text": body.text,
        "created_at": now_iso(),
    }
    await db.comments.insert_one(c)
    await db.posts.update_one({"id": post_id}, {"$inc": {"comments_count": 1}})
    c.pop("_id", None)
    return c

# ---------------- Listings ----------------
@api_router.get("/listings", response_model=List[Listing])
async def list_listings(category: Optional[str] = None):
    q = {}
    if category: q["category"] = category
    items = await db.listings.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)
    return items

@api_router.get("/listings/{listing_id}", response_model=Listing)
async def get_listing(listing_id: str):
    l = await db.listings.find_one({"id": listing_id}, {"_id": 0})
    if not l: raise HTTPException(404, "Listing not found")
    return l

# ---------------- Services ----------------
@api_router.get("/services", response_model=List[Service])
async def list_services(category: Optional[str] = None):
    q = {}
    if category and category != "all": q["category"] = category
    items = await db.services.find(q, {"_id": 0}).to_list(200)
    return items

# ---------------- Health Records ----------------
@api_router.get("/health/{pet_id}", response_model=List[HealthRecord])
async def list_health(pet_id: str):
    items = await db.health.find({"pet_id": pet_id}, {"_id": 0}).sort("date", 1).to_list(500)
    return items

@api_router.post("/health", response_model=HealthRecord)
async def add_health(record: HealthRecord, user=Depends(get_current_user)):
    await db.health.insert_one(record.model_dump())
    return record

# ---------------- Messaging (DMs) ----------------
def chat_key(a: str, b: str) -> str:
    return "-".join(sorted([a, b]))

@api_router.get("/chats")
async def list_chats(user=Depends(get_current_user)):
    """Return latest message per conversation for me."""
    # Aggregate: for each chat_key involving me, pick the latest
    pipeline = [
        {"$match": {"$or": [{"from_user": user["id"]}, {"to_user": user["id"]}]}},
        {"$sort": {"created_at": -1}},
        {"$group": {"_id": "$chat_key", "last": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$last"}},
        {"$sort": {"created_at": -1}},
    ]
    docs = await db.messages.aggregate(pipeline).to_list(200)
    # Enrich with other user info
    result = []
    for d in docs:
        d.pop("_id", None)
        other_id = d["to_user"] if d["from_user"] == user["id"] else d["from_user"]
        other = await db.users.find_one({"id": other_id}, {"_id": 0, "password": 0})
        result.append({
            "chat_key": d["chat_key"],
            "other_user_id": other_id,
            "other_user_name": other["name"] if other else "Unknown",
            "other_user_avatar": (other or {}).get("avatar"),
            "last_message": d["text"],
            "last_from_me": d["from_user"] == user["id"],
            "last_time": d["created_at"],
        })
    return result

@api_router.get("/chats/{other_user_id}", response_model=List[Message])
async def get_chat(other_user_id: str, user=Depends(get_current_user)):
    key = chat_key(user["id"], other_user_id)
    docs = await db.messages.find({"chat_key": key}, {"_id": 0}).sort("created_at", 1).to_list(1000)
    return docs

@api_router.post("/chats/{other_user_id}", response_model=Message)
async def send_message(other_user_id: str, body: MessageIn, user=Depends(get_current_user)):
    other = await db.users.find_one({"id": other_user_id}, {"_id": 0})
    if not other:
        raise HTTPException(404, "Recipient not found")
    m = {
        "id": str(uuid.uuid4()),
        "chat_key": chat_key(user["id"], other_user_id),
        "from_user": user["id"],
        "from_name": user["name"],
        "to_user": other_user_id,
        "text": body.text,
        "created_at": now_iso(),
    }
    await db.messages.insert_one(m)
    m.pop("_id", None)
    m.pop("chat_key", None)
    return m

# ---------------- Stripe Checkout ----------------
STATIC_PACKAGES = {
    "premium_monthly": {"amount": 9.99, "currency": "usd", "name": "PETBOOK Premium (Monthly)"},
    "premium_yearly": {"amount": 79.00, "currency": "usd", "name": "PETBOOK Premium (Yearly)"},
}

@api_router.post("/checkout/session")
async def create_checkout(body: CheckoutIn, user=Depends(get_current_user)):
    host = body.origin_url.rstrip("/")
    success_url = f"{host}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{host}/checkout/cancel"

    if body.kind == "listing":
        if not body.listing_id:
            raise HTTPException(400, "listing_id required")
        listing = await db.listings.find_one({"id": body.listing_id}, {"_id": 0})
        if not listing:
            raise HTTPException(404, "Listing not found")
        # Determine price in USD
        price_cents = listing.get("price_cents") or body.amount_cents or 1999
        amount = round(price_cents / 100, 2)
        name = listing["title"]
        metadata = {"kind": "listing", "listing_id": body.listing_id, "user_id": user["id"], "product": name}
    elif body.kind == "premium":
        pack = STATIC_PACKAGES["premium_monthly"]
        amount = pack["amount"]
        name = pack["name"]
        metadata = {"kind": "premium", "user_id": user["id"], "product": name}
    else:
        raise HTTPException(400, "Invalid kind")

    session = await asyncio.to_thread(
        stripe.checkout.Session.create,
        payment_method_types=["card"],
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": name},
                "unit_amount": int(round(amount * 100)),
            },
            "quantity": 1,
        }],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=metadata,
    )
    # Record the pending transaction
    await db.transactions.insert_one({
        "id": str(uuid.uuid4()),
        "session_id": session.id,
        "user_id": user["id"],
        "kind": body.kind,
        "listing_id": body.listing_id,
        "amount": amount,
        "currency": "usd",
        "name": name,
        "status": "pending",
        "created_at": now_iso(),
    })
    return {"url": session.url, "session_id": session.id}

@api_router.get("/checkout/status/{session_id}")
async def checkout_status(session_id: str, user=Depends(get_current_user)):
    status = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
    txn = await db.transactions.find_one({"session_id": session_id}, {"_id": 0})
    # If paid and not yet finalized, activate downstream state (idempotent)
    if txn and status.payment_status in ("paid", "complete") and txn["status"] != "paid":
        await db.transactions.update_one(
            {"session_id": session_id},
            {"$set": {"status": "paid", "paid_at": now_iso()}}
        )
        if txn["kind"] == "premium":
            await db.subscriptions.update_one(
                {"user_id": txn["user_id"], "kind": "premium"},
                {"$set": {
                    "user_id": txn["user_id"],
                    "kind": "premium",
                    "status": "active",
                    "activated_at": now_iso(),
                    "session_id": session_id,
                }},
                upsert=True,
            )
    return {
        "status": status.status,
        "payment_status": status.payment_status,
        "amount_total": status.amount_total,
        "currency": status.currency,
        "kind": txn["kind"] if txn else None,
        "name": txn["name"] if txn else None,
    }

# ---------------- AI Chat ----------------
ASSISTANT_SYSTEM = {
    "doctor": "You are 'Dr. Paws', a caring, friendly AI veterinary assistant. Give practical, concise pet health guidance. ALWAYS remind users to see a licensed vet for emergencies or definitive diagnosis. Keep responses under 200 words, warm, and use simple language.",
    "trainer": "You are 'Coach Paws', an expert positive-reinforcement pet trainer. Offer step-by-step, science-based training tips for dogs, cats and other pets. Keep responses friendly and under 200 words.",
    "nutrition": "You are 'Chef Paws', a pet nutrition expert. Recommend healthy, breed and age appropriate feeding plans. Flag foods that are toxic to pets. Keep responses concise and warm, under 200 words.",
    "blog": "You are 'Editor Paws', a pet-content copywriter. Help users draft engaging, magazine-quality blog posts about their pets. Suggest catchy titles, warm intros, and vivid descriptions. Keep responses under 300 words."
}

@api_router.post("/ai/chat")
async def ai_chat(body: AIChatIn):

    system = ASSISTANT_SYSTEM.get(body.assistant, ASSISTANT_SYSTEM["doctor"])

    prompt = system + "\n\n"

    for m in body.history[-8:]:
        if m.role == "user":
            prompt += f"User: {m.text}\n"
        else:
            prompt += f"Assistant: {m.text}\n"

    prompt += f"User: {body.message}"

    async def event_generator():

        try:
            print("========================================")
            print("AI endpoint hit!")
            print("Model:", GEMINI_MODEL)
            print("Message:", body.message)
            print("========================================")

            response = client_ai.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt
            )

            yield f"data: {response.text}\n\n"
            yield "data: [DONE]\n\n"

        except Exception as e:
            print("ERROR:", str(e))
            yield f"data: Error: {str(e)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )
@api_router.post("/ai/chat-sync")
async def ai_chat_sync(body: AIChatIn):

    system = ASSISTANT_SYSTEM.get(body.assistant, ASSISTANT_SYSTEM["doctor"])

    prompt = system + "\n\n"

    for m in body.history[-8:]:
        if m.role == "user":
            prompt += f"User: {m.text}\n"
        else:
            prompt += f"Assistant: {m.text}\n"

    prompt += f"User: {body.message}"

    try:
        print("========================================")
        print("AI CHAT SYNC")
        print("Model:", GEMINI_MODEL)
        print("Message:", body.message)
        print("========================================")

        response = client_ai.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )

        return {"reply": response.text}

    except Exception as e:
        print("ERROR:", str(e))
        return {"reply": str(e)}
# ---------------- Seed ----------------
SEED_PETS = [
    ("Max", "dog", "Golden Retriever", "3 years", "Male", "Loves fetch and beach mornings.", "#F2A63D", True, False),
    ("Luna", "cat", "Persian Cat", "2 years", "Female", "Sunbeam expert. Purrs on demand.", "#5B8266", False, False),
    ("Buddy", "dog", "German Shepherd", "4 years", "Male", "Loyal, alert, and always happy.", "#E6604A", False, True),
    ("Rocky", "dog", "Labrador Mix", "5 years", "Male", "Rescue with the biggest heart.", "#28513B", True, False),
    ("Simba", "cat", "Shorthair", "1 year", "Male", "Kitten energy, lion vibes.", "#C97A63", False, False),
    ("Bella", "dog", "Golden Puppy", "6 months", "Female", "New in the pack. Very small paws.", "#D9862A", True, False),
    ("Whiskers", "cat", "Tabby", "4 years", "Female", "Chief mouser at HQ.", "#3E6B4F", False, False),
    ("Cooper", "dog", "Beach Mutt", "2 years", "Male", "Salt in the fur, wind in the ears.", "#5B8266", False, True),
    ("Snowy", "cat", "Ragdoll", "3 years", "Female", "Cloud-shaped. Cloud-hearted.", "#E08A3E", False, False),
]

SEED_LISTINGS = [
    ("Royal Canin Adult Dog Food 4kg", "$48", 4800, "marketplace", "Breed-specific nutrition. Balanced protein & fiber.", "#F2A63D", "Ships worldwide", True),
    ("Handwoven Rope Toy Bundle", "$18", 1800, "marketplace", "Set of 3 natural cotton chew toys.", "#E6604A", "Local pickup", False),
    ("Cozy Cat Cave (Wool)", "$65", 6500, "marketplace", "Handmade felted wool. Fits medium cats.", "#5B8266", "Ships worldwide", True),
    ("Puppy Starter Kit", "$92", 9200, "marketplace", "Collar, leash, bowls, brush, blanket.", "#D9862A", "Ships worldwide", True),
    ("Meet Rocky - 5yr Lab Mix", "Adopt", 0, "adoption", "Neutered, vaccinated. Loves gentle homes.", "#28513B", "Mumbai Shelter", True),
    ("Baby Kittens (litter of 3)", "Adopt", 0, "adoption", "10 weeks old. Playful & socialized.", "#C97A63", "Delhi NGO", True),
    ("Senior Beagle - Rusty", "Adopt", 0, "adoption", "8yr old gentleman looking for a quiet couch.", "#F2A63D", "Bangalore Rescue", True),
    ("Champion Golden Retriever Sire", "Contact", 0, "mating", "AKC-registered. Full health panel.", "#F2A63D", "Verified breeder", True),
    ("Persian Queen for Mating", "Contact", 0, "mating", "DNA-tested. Vaccinated. Bloodline verified.", "#5B8266", "Verified breeder", True),
    ("Lost: Small Terrier 'Coco'", "$200 reward", 0, "lost", "Last seen near Central Park, red collar.", "#E6604A", "New York", False),
    ("Found: Tabby cat near cafe", "Free", 0, "lost", "Very friendly, no collar. Held safely.", "#3E6B4F", "Mumbai", False),
]

SEED_SERVICES = [
    ("PawCare Veterinary Clinic", "vet", 0.5, 4.8, 250, "Open until 8 PM", "\u2695", "#5B8266", 19.076, 72.877),
    ("Furry Friends Grooming Spa", "groomer", 1.2, 4.9, 180, "Booking available", "\u2702", "#E6604A", 19.079, 72.881),
    ("Central Dog Park", "park", 2.0, 4.5, 95, "Open 24/7", "\U0001F332", "#28513B", 19.083, 72.870),
    ("The Waggy Terrace", "restaurant", 1.7, 4.7, 140, "Outdoor seating", "\U0001F37D", "#D9862A", 19.070, 72.882),
    ("Harborview Pet-Friendly Suites", "hotel", 3.4, 4.6, 88, "No weight limit", "\U0001F3E8", "#C97A63", 19.090, 72.865),
    ("Steady Paws Training Studio", "trainer", 2.9, 4.9, 61, "Group & private", "\U0001F393", "#F2A63D", 19.068, 72.890),
    ("BlueCross Animal Hospital", "vet", 3.1, 4.7, 320, "24/7 emergency", "\u2695", "#28513B", 19.100, 72.860),
    ("Fluffy Tails Grooming", "groomer", 2.4, 4.6, 88, "Walk-ins welcome", "\u2702", "#F2A63D", 19.065, 72.875),
]

SEED_REELS = [
    ("Max chases the wave — always ends the same way \U0001F30A", "#F2A63D"),
    ("Luna's morning stretch routine, 10/10 would watch again \U0001F408", "#5B8266"),
    ("Puppy vs. leaf — final boss battle \U0001F343", "#E6604A"),
    ("Cat tries stairs, engineering degree recommended \U0001F4D0", "#C97A63"),
    ("Buddy's zoomies at sunset \U0001F31E", "#28513B"),
]

async def seed_data():
    if await db.pets.count_documents({}) == 0:
        demo_uid = str(uuid.uuid4())
        await db.users.insert_one({
            "id": demo_uid, "email": "demo@petbook.app",
            "password": hash_password("demo1234"), "name": "Sarah Mitchell",
            "avatar": None, "bio": "Golden retriever mom \U0001F436",
            "location": "Mumbai, India", "created_at": now_iso(),
        })
        # Second seed user for follow/DM demo
        other_uid = str(uuid.uuid4())
        await db.users.insert_one({
            "id": other_uid, "email": "james@petbook.app",
            "password": hash_password("demo1234"), "name": "James Chen",
            "avatar": None, "bio": "Cat dad, coffee snob \u2615",
            "location": "San Francisco, USA", "created_at": now_iso(),
        })
        third_uid = str(uuid.uuid4())
        await db.users.insert_one({
            "id": third_uid, "email": "emily@petbook.app",
            "password": hash_password("demo1234"), "name": "Dr. Emily Rodriguez",
            "avatar": None, "bio": "Veterinarian. Here to help paws.",
            "location": "Toronto, Canada", "created_at": now_iso(),
        })
        pets_docs = []
        for (name, species, breed, age, gender, bio, color, adopt, mate) in SEED_PETS:
            pets_docs.append({
                "id": str(uuid.uuid4()), "owner_id": demo_uid, "owner_name": "Sarah Mitchell",
                "name": name, "species": species, "breed": breed, "age": age,
                "gender": gender, "bio": bio, "image": None, "color": color,
                "for_adoption": adopt, "for_mating": mate,
                "created_at": now_iso(),
            })
        await db.pets.insert_many(pets_docs)

        posts_docs = []
        captions = [
            "Sunday morning at the beach — Max in his happy place. \U0001F30A",
            "Luna insists the couch is her throne. All hail the queen.",
            "First hike with Buddy today. Look at that focus! \U0001F3D4\uFE0F",
            "Rocky's adoption anniversary — 3 years of the best friendship.",
            "Simba discovered the paper bag. Chaos ensued.",
        ]
        for i, c in enumerate(captions):
            pet = pets_docs[i]
            posts_docs.append({
                "id": str(uuid.uuid4()),
                "author_id": demo_uid, "author_name": "Sarah Mitchell",
                "author_avatar": None,
                "pet_id": pet["id"], "pet_name": pet["name"],
                "caption": c, "image": None, "tags": ["pawlife", pet["species"]],
                "likes": 12 + i * 4, "liked_by": [], "comments_count": 0,
                "is_reel": False, "created_at": now_iso(),
            })
        # Reels
        for (cap, color) in SEED_REELS:
            posts_docs.append({
                "id": str(uuid.uuid4()),
                "author_id": demo_uid, "author_name": "Sarah Mitchell",
                "author_avatar": None,
                "pet_id": None, "pet_name": None,
                "caption": cap, "image": color, "tags": ["reel"],
                "likes": 42, "liked_by": [], "comments_count": 0,
                "is_reel": True, "created_at": now_iso(),
            })
        await db.posts.insert_many(posts_docs)

        # Seed a demo conversation
        await db.messages.insert_many([
            {"id": str(uuid.uuid4()), "chat_key": chat_key(demo_uid, other_uid),
             "from_user": other_uid, "from_name": "James Chen", "to_user": demo_uid,
             "text": "Hey! Loved your beach post with Max \U0001F436", "created_at": now_iso()},
            {"id": str(uuid.uuid4()), "chat_key": chat_key(demo_uid, other_uid),
             "from_user": demo_uid, "from_name": "Sarah Mitchell", "to_user": other_uid,
             "text": "Thanks James! Luna is looking royal as always.", "created_at": now_iso()},
        ])

    if await db.listings.count_documents({}) == 0:
        docs = []
        for (title, price, price_cents, cat, desc, color, loc, verified) in SEED_LISTINGS:
            docs.append({
                "id": str(uuid.uuid4()), "title": title, "price": price, "price_cents": price_cents,
                "category": cat, "description": desc, "image": None,
                "color": color, "location": loc, "verified": verified,
                "owner_id": None, "owner_name": "PETBOOK Partner",
                "created_at": now_iso(),
            })
        await db.listings.insert_many(docs)

    if await db.services.count_documents({}) == 0:
        docs = []
        for (n, c, d, r, rv, o, ic, col, lat, lng) in SEED_SERVICES:
            docs.append({
                "id": str(uuid.uuid4()), "name": n, "category": c,
                "distance_km": d, "rating": r, "reviews": rv,
                "open_status": o, "icon": ic, "color": col,
                "lat": lat, "lng": lng,
            })
        await db.services.insert_many(docs)

    if await db.health.count_documents({}) == 0:
        pet = await db.pets.find_one({"name": "Max"}, {"_id": 0})
        if pet:
            base = datetime.now(timezone.utc)
            samples = [
                ("DHPP - First Round", "Core puppy shot", -365 * 3 + 30, "done", "vaccine"),
                ("Rabies Vaccine", "Annual booster", -120, "done", "vaccine"),
                ("Annual Checkup", "General wellness at PawCare", -60, "done", "checkup"),
                ("Grooming Session", "Full groom + trim", -10, "done", "grooming"),
                ("Flea & Tick", "Monthly medication", 4, "upcoming", "medication"),
                ("Leptospirosis Booster", "Yearly booster", 30, "upcoming", "vaccine"),
                ("Annual Checkup 2026", "Book at PawCare Vet", 90, "upcoming", "checkup"),
            ]
            docs = []
            for (t, d, offset_days, status, kind) in samples:
                docs.append({
                    "id": str(uuid.uuid4()), "pet_id": pet["id"], "title": t,
                    "detail": d, "date": (base + timedelta(days=offset_days)).isoformat(),
                    "status": status, "kind": kind,
                })
            await db.health.insert_many(docs)

@app.on_event("startup")
async def on_start():
    await seed_data()

@api_router.get("/")
async def root():
    return {"message": "PETBOOK API is running \U0001F43E"}

@api_router.get("/health-check")
async def health():
    return {"ok": True, "ts": now_iso()}

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
