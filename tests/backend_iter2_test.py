"""PETBOOK iteration 2 backend tests: follow, chats/DMs, reels, comments, checkout, services."""
import uuid
import pytest
import requests

BASE_URL = "https://petverse-social-2.preview.emergentagent.com"
API = f"{BASE_URL}/api"
ORIGIN = BASE_URL
DEMO_EMAIL = "demo@petbook.app"
DEMO_PASSWORD = "demo1234"

session = requests.Session()
session.headers.update({"Content-Type": "application/json"})
state = {}


def _auth():
    return {"Authorization": f"Bearer {state['token']}"}


# --- setup: login demo ---
def test_login_demo():
    r = session.post(f"{API}/auth/login",
                     json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    state["token"] = d["token"]
    state["me_id"] = d["user"]["id"]
    assert d["user"]["email"] == DEMO_EMAIL


# --- users list excludes self ---
def test_list_users_excludes_self():
    r = session.get(f"{API}/users", headers=_auth(), timeout=30)
    assert r.status_code == 200, r.text
    users = r.json()
    assert isinstance(users, list) and len(users) >= 1
    ids = [u["id"] for u in users]
    assert state["me_id"] not in ids
    # seed contains James Chen
    james = next((u for u in users if u["name"] == "James Chen"), None)
    assert james is not None, "James Chen seed user missing"
    state["other_id"] = james["id"]


# --- follow toggle + status ---
def test_follow_toggle_and_status():
    other = state["other_id"]
    # first ensure known state (query status)
    r0 = session.get(f"{API}/users/{other}/follow-status", headers=_auth(), timeout=30)
    assert r0.status_code == 200
    initial = r0.json().get("following", False)

    r1 = session.post(f"{API}/users/{other}/follow", headers=_auth(), timeout=30)
    assert r1.status_code == 200, r1.text
    after1 = r1.json()["following"]
    assert after1 != initial

    r2 = session.get(f"{API}/users/{other}/follow-status", headers=_auth(), timeout=30)
    assert r2.status_code == 200
    assert r2.json()["following"] == after1

    # toggle back to original
    r3 = session.post(f"{API}/users/{other}/follow", headers=_auth(), timeout=30)
    assert r3.status_code == 200
    assert r3.json()["following"] == initial


# --- chats list contains seeded convo with James ---
def test_chats_list_seeded():
    r = session.get(f"{API}/chats", headers=_auth(), timeout=30)
    assert r.status_code == 200, r.text
    chats = r.json()
    assert isinstance(chats, list) and len(chats) >= 1
    james_row = next((c for c in chats if c["other_user_id"] == state["other_id"]), None)
    assert james_row is not None, "Seeded conversation with James missing"
    assert james_row.get("last_message")


def test_chat_get_messages_sorted():
    r = session.get(f"{API}/chats/{state['other_id']}", headers=_auth(), timeout=30)
    assert r.status_code == 200, r.text
    msgs = r.json()
    assert isinstance(msgs, list) and len(msgs) >= 2
    times = [m["created_at"] for m in msgs]
    assert times == sorted(times), "messages not sorted ascending"


def test_chat_send_message():
    txt = f"TEST_MSG_{uuid.uuid4().hex[:6]}"
    r = session.post(f"{API}/chats/{state['other_id']}", json={"text": txt},
                     headers=_auth(), timeout=30)
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["text"] == txt and m["to_user"] == state["other_id"]
    assert m["from_user"] == state["me_id"]
    # verify persisted
    r2 = session.get(f"{API}/chats/{state['other_id']}", headers=_auth(), timeout=30)
    assert any(x["id"] == m["id"] for x in r2.json())


# --- posts / reels split ---
def test_posts_feed_non_reel():
    r = session.get(f"{API}/posts", params={"is_reel": "false"}, timeout=30)
    assert r.status_code == 200, r.text
    posts = r.json()
    assert isinstance(posts, list) and len(posts) >= 1
    assert all(p["is_reel"] is False for p in posts)
    state["post_id"] = posts[0]["id"]
    state["initial_comments_count"] = posts[0].get("comments_count", 0)


def test_reels_feed():
    r = session.get(f"{API}/posts", params={"is_reel": "true"}, timeout=30)
    assert r.status_code == 200, r.text
    reels = r.json()
    assert isinstance(reels, list) and len(reels) >= 5
    assert all(p["is_reel"] is True for p in reels)


def test_create_reel_post():
    r = session.post(f"{API}/posts", json={
        "caption": "TEST_REEL_caption", "tags": ["reel"], "is_reel": True,
    }, headers=_auth(), timeout=30)
    assert r.status_code in (200, 201), r.text
    p = r.json()
    assert p["is_reel"] is True and p["caption"] == "TEST_REEL_caption"


# --- comments ---
def test_add_and_list_comment_and_increment():
    pid = state["post_id"]
    txt = f"TEST_COMMENT_{uuid.uuid4().hex[:6]}"
    r = session.post(f"{API}/posts/{pid}/comments", json={"text": txt},
                     headers=_auth(), timeout=30)
    assert r.status_code in (200, 201), r.text
    c = r.json()
    assert c["text"] == txt and c["post_id"] == pid

    r2 = session.get(f"{API}/posts/{pid}/comments", timeout=30)
    assert r2.status_code == 200
    comments = r2.json()
    assert any(x["id"] == c["id"] for x in comments)

    # verify comments_count incremented on post
    r3 = session.get(f"{API}/posts", params={"is_reel": "false"}, timeout=30)
    p = next((x for x in r3.json() if x["id"] == pid), None)
    assert p is not None
    assert p["comments_count"] >= state["initial_comments_count"] + 1


# --- checkout: premium ---
def test_checkout_premium():
    r = session.post(f"{API}/checkout/session", json={
        "kind": "premium", "origin_url": ORIGIN,
    }, headers=_auth(), timeout=45)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("session_id") and d.get("url"), d
    assert "stripe.com" in d["url"] or "checkout" in d["url"]
    state["premium_session_id"] = d["session_id"]


# --- checkout: listing ---
def test_checkout_listing():
    lr = session.get(f"{API}/listings", params={"category": "marketplace"}, timeout=30)
    assert lr.status_code == 200
    listings = lr.json()
    assert len(listings) >= 1
    lid = listings[0]["id"]
    r = session.post(f"{API}/checkout/session", json={
        "kind": "listing", "listing_id": lid, "origin_url": ORIGIN,
    }, headers=_auth(), timeout=45)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("session_id") and d.get("url")
    state["listing_session_id"] = d["session_id"]


# --- checkout status ---
def test_checkout_status():
    sid = state.get("premium_session_id")
    assert sid, "premium checkout must run first"
    r = session.get(f"{API}/checkout/status/{sid}", headers=_auth(), timeout=45)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "status" in d and "payment_status" in d
    assert d.get("kind") == "premium"
    assert d.get("name")


# --- services w/ lat lng ---
def test_services_has_lat_lng():
    r = session.get(f"{API}/services", timeout=30)
    assert r.status_code == 200, r.text
    services = r.json()
    assert isinstance(services, list) and len(services) >= 1
    for s in services:
        assert isinstance(s.get("lat"), (int, float))
        assert isinstance(s.get("lng"), (int, float))
