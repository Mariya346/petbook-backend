"""PETBOOK / PetVerse backend API tests against live preview URL."""
import os
import uuid
import pytest
import requests

BASE_URL = "https://petverse-social-2.preview.emergentagent.com"
API = f"{BASE_URL}/api"
DEMO_EMAIL = "demo@petbook.app"
DEMO_PASSWORD = "demo1234"

session = requests.Session()
session.headers.update({"Content-Type": "application/json"})

state = {}


# ---------- AUTH ----------
def test_register_returns_token_and_user():
    email = f"TEST_{uuid.uuid4().hex[:8]}@example.com"
    r = session.post(f"{API}/auth/register", json={
        "email": email, "password": "TestPass123!", "name": "TEST User"
    }, timeout=30)
    assert r.status_code in (200, 201), f"{r.status_code}: {r.text}"
    data = r.json()
    assert "token" in data and data["token"]
    assert "user" in data and data["user"].get("email", "").lower() == email.lower()
    state["reg_token"] = data["token"]


def test_login_demo_returns_token():
    r = session.post(f"{API}/auth/login",
                     json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=30)
    assert r.status_code == 200, f"{r.status_code}: {r.text}"
    data = r.json()
    assert data.get("token")
    assert data.get("user", {}).get("email") == DEMO_EMAIL
    state["token"] = data["token"]
    state["user_id"] = data["user"].get("id") or data["user"].get("_id")


def test_auth_me_with_bearer():
    assert state.get("token"), "login must run first"
    r = session.get(f"{API}/auth/me",
                    headers={"Authorization": f"Bearer {state['token']}"}, timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("email") == DEMO_EMAIL


# ---------- PETS ----------
def test_get_pets_returns_array_of_9():
    r = session.get(f"{API}/pets", timeout=30)
    assert r.status_code == 200, r.text
    pets = r.json()
    assert isinstance(pets, list)
    assert len(pets) >= 1
    state["pet_id"] = pets[0].get("id") or pets[0].get("_id")
    state["pets_count"] = len(pets)


def test_get_pet_by_id():
    assert state.get("pet_id")
    r = session.get(f"{API}/pets/{state['pet_id']}", timeout=30)
    assert r.status_code == 200, r.text
    pet = r.json()
    assert (pet.get("id") or pet.get("_id")) == state["pet_id"]


def test_create_pet_authenticated():
    assert state.get("token")
    payload = {
        "name": f"TEST_Pet_{uuid.uuid4().hex[:6]}",
        "species": "dog",
        "breed": "TestBreed",
        "age": "2",
        "bio": "created by pytest"
    }
    r = session.post(f"{API}/pets", json=payload,
                     headers={"Authorization": f"Bearer {state['token']}"}, timeout=30)
    assert r.status_code in (200, 201), f"{r.status_code}: {r.text}"
    pet = r.json()
    assert pet.get("name") == payload["name"]


# ---------- POSTS ----------
def test_get_posts_returns_seeded():
    r = session.get(f"{API}/posts", timeout=30)
    assert r.status_code == 200, r.text
    posts = r.json()
    assert isinstance(posts, list) and len(posts) >= 1
    state["post_id"] = posts[0].get("id") or posts[0].get("_id")


def test_post_like_toggle():
    assert state.get("token") and state.get("post_id")
    headers = {"Authorization": f"Bearer {state['token']}"}
    r1 = session.post(f"{API}/posts/{state['post_id']}/like", headers=headers, timeout=30)
    assert r1.status_code == 200, r1.text
    r2 = session.post(f"{API}/posts/{state['post_id']}/like", headers=headers, timeout=30)
    assert r2.status_code == 200, r2.text


# ---------- LISTINGS ----------
@pytest.mark.parametrize("cat", ["marketplace", "adoption", "mating", "lost"])
def test_listings_by_category(cat):
    r = session.get(f"{API}/listings", params={"category": cat}, timeout=30)
    assert r.status_code == 200, f"{cat}: {r.status_code} {r.text}"
    data = r.json()
    assert isinstance(data, list)


# ---------- SERVICES ----------
def test_services_returns_nearby():
    r = session.get(f"{API}/services", timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, list)


# ---------- AI ----------
def test_ai_chat_sync_doctor():
    assert state.get("token")
    r = session.post(f"{API}/ai/chat-sync",
                     json={"assistant": "doctor", "message": "My dog has a mild cough. Any tips?"},
                     headers={"Authorization": f"Bearer {state['token']}"}, timeout=90)
    assert r.status_code == 200, r.text
    data = r.json()
    reply = data.get("reply") or data.get("response") or data.get("message")
    assert isinstance(reply, str) and len(reply) > 0, f"empty reply: {data}"
