"""PETBOOK iteration 3 backend tests: Google sign-in endpoint + regressions."""
import requests

BASE_URL = "https://petverse-social-2.preview.emergentagent.com"
API = f"{BASE_URL}/api"
ORIGIN = BASE_URL
DEMO_EMAIL = "demo@petbook.app"
DEMO_PASSWORD = "demo1234"

session = requests.Session()
session.headers.update({"Content-Type": "application/json"})
state = {}


# --- Google auth: invalid session token should NOT crash and return 4xx ---
def test_google_auth_invalid_session_token():
    r = session.post(f"{API}/auth/google",
                     json={"session_token": "totally-bogus-token-abc123"}, timeout=30)
    # Expect 4xx (401 preferred, but 400/401 range acceptable per spec). Must NOT be 5xx.
    assert 400 <= r.status_code < 500, f"got {r.status_code}: {r.text}"
    # And ideally 401 per the code path
    assert r.status_code in (400, 401), r.text
    body = r.json()
    assert "detail" in body


# --- Google auth: missing session_token in body should return 422 (Pydantic) ---
def test_google_auth_missing_session_token():
    r = session.post(f"{API}/auth/google", json={}, timeout=30)
    assert r.status_code == 422, r.text
    body = r.json()
    assert "detail" in body


# --- Google auth: empty string session_token should also fail cleanly (defensive) ---
def test_google_auth_empty_session_token():
    r = session.post(f"{API}/auth/google", json={"session_token": ""}, timeout=30)
    # Empty passes Pydantic (str type) but Emergent verify will reject -> 401
    assert 400 <= r.status_code < 500, f"got {r.status_code}: {r.text}"


# --- Regression: demo email/password login still works ---
def test_login_demo_regression():
    r = session.post(f"{API}/auth/login",
                     json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["user"]["email"] == DEMO_EMAIL
    assert d.get("token")
    state["token"] = d["token"]


# --- Regression: GET /api/pets returns 9 seeded pets ---
def test_pets_seed_regression():
    r = session.get(f"{API}/pets", timeout=30)
    assert r.status_code == 200, r.text
    pets = r.json()
    assert isinstance(pets, list), pets
    assert len(pets) == 9, f"expected 9 seeded pets, got {len(pets)}"
    # sanity: each pet has expected fields
    for p in pets:
        assert p.get("id") and p.get("name") and p.get("species")


# --- Regression: Stripe /api/checkout/session for premium still works ---
def test_stripe_checkout_premium_regression():
    assert state.get("token"), "login must run first"
    headers = {"Authorization": f"Bearer {state['token']}"}
    r = session.post(f"{API}/checkout/session",
                     json={"kind": "premium", "origin_url": ORIGIN},
                     headers=headers, timeout=45)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("url"), d
    assert d.get("session_id"), d
    assert "stripe.com" in d["url"] or "checkout" in d["url"]
