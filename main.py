from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import os
import json

# ============================================================
# FastAPI app initialization
# ============================================================

app = FastAPI()

# ============================================================
# Version marker
# Change this whenever you want to prove a fresh deploy happened
# ============================================================

APP_VERSION = "worker-debug-2026-04-10-v8"

# ============================================================
# Environment variables injected through Cloud Run
# ============================================================

LOGISTICALLY_BASE_URL = os.getenv("LOGISTICALLY_BASE_URL", "").rstrip("/")
LOGISTICALLY_USERNAME = os.getenv("LOGISTICALLY_USERNAME", "")
LOGISTICALLY_PASSWORD = os.getenv("LOGISTICALLY_PASSWORD", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

# ============================================================
# Incoming request schema
# ============================================================

class LoadLookupRequest(BaseModel):
    ticket_id: int
    load_number_or_po: str
    invoice_number: str
    invoice_total: str


# ============================================================
# Helper: Identify whether current page is the login page
# ============================================================

def is_login_page_text(body_text: str) -> bool:
    """
    Detect whether the current page still looks like the TMS login page.
    """
    body_text_lower = body_text.lower()

    login_signals = [
        "sign in",
        "e-mail",
        "password",
        "forgot password",
        "stay signed in",
        "powered by logistically tms"
    ]

    signal_count = sum(1 for s in login_signals if s in body_text_lower)
    return signal_count >= 3


# ============================================================
# Helper: Detect final page classification after opening order URL
# ============================================================

def detect_order_page(page, load_number: str) -> dict:
    """
    Classify the current page into one of:
    - login_page
    - not_found_or_no_access
    - order_page
    - unknown

    IMPORTANT:
    URL alone is not enough because the attempted order URL
    itself always contains the requested load number.
    """
    current_url = page.url
    body_text = page.locator("body").inner_text(timeout=15000)

    body_text_lower = body_text.lower()
    load_lower = load_number.lower()

    # 1. Login page
    if is_login_page_text(body_text):
        return {
            "page_type": "login_page",
            "load_found": False,
            "reason": "Session appears to be on login page, not order page",
            "current_url": current_url,
            "body_preview": body_text[:1000]
        }

    # 2. Explicit 403 / no-access page
    if (
        "you don't have access to this page or resource" in body_text_lower
        or "(403)" in body_text_lower
    ):
        return {
            "page_type": "not_found_or_no_access",
            "load_found": False,
            "reason": "TMS returned access/not-found style page",
            "current_url": current_url,
            "body_preview": body_text[:1000]
        }

    # 3. Strong real order page signals
    strong_order_signals = [
        f"edit order: order {load_lower}",
        "order #",
        "customer:",
        "ship date:",
        "order status:",
        "carrier:",
        "bids",
        "ref numbers",
        "attachments",
        "cost",
        "invoice"
    ]

    strong_signal_count = sum(1 for s in strong_order_signals if s in body_text_lower)

    if load_lower in body_text_lower and strong_signal_count >= 4:
        return {
            "page_type": "order_page",
            "load_found": True,
            "reason": "Detected real order page using order-page content signals",
            "current_url": current_url,
            "body_preview": body_text[:1000]
        }

    # 4. Unknown fallback
    return {
        "page_type": "unknown",
        "load_found": False,
        "reason": "Could not confirm order page",
        "current_url": current_url,
        "body_preview": body_text[:1000]
    }


# ============================================================
# Helper: Perform login and confirm we reached TMS home
# ============================================================

def perform_login(page):
    """
    Perform login and confirm that the app reaches the post-login
    TMS shell / home page.

    Expected successful landing page pattern:
    .../tms/#/3pl/
    """
    login_url = f"{LOGISTICALLY_BASE_URL}/"
    expected_post_login_fragment = "/tms/#/3pl"

    print("=== Opening login page ===")
    page.goto(login_url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2000)

    print("=== Filling email field ===")
    page.locator("#email").fill(LOGISTICALLY_USERNAME)

    print("=== Filling password field ===")
    page.locator("#password").fill(LOGISTICALLY_PASSWORD)

    print("=== Clicking sign-in button ===")
    page.locator("#sign-in").click()

    # Give the app time to authenticate and redirect
    page.wait_for_timeout(5000)

    # Try to wait until URL indicates post-login shell/home
    try:
        page.wait_for_url(f"**{expected_post_login_fragment}**", timeout=30000)
    except PlaywrightTimeoutError:
        print("=== Timed out waiting for post-login TMS URL ===")

    # Let the page settle after redirect
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        print("=== Network did not fully settle after login ===")

    current_url = page.url
    body_text = page.locator("body").inner_text(timeout=15000)

    print("=== URL after login attempt ===")
    print(current_url)

    print("=== Body preview after login attempt ===")
    print(body_text[:1200])

    # Save screenshot for debugging
    screenshot_path = "/tmp/post_login_state.png"
    page.screenshot(path=screenshot_path, full_page=True)
    print(f"=== Saved screenshot to {screenshot_path} ===")

    # Real success test: we should be in the TMS shell/home
    if expected_post_login_fragment not in current_url:
        raise ValueError(
            f"Login failed — did not reach TMS home. URL={current_url} | "
            f"Body preview={body_text[:500]}"
        )

    # If somehow still on login page, also fail
    if is_login_page_text(body_text):
        raise ValueError(
            f"Login failed — still on login page. URL={current_url} | "
            f"Body preview={body_text[:500]}"
        )

    print("=== Login successful - reached TMS home ===")
    return True


# ============================================================
# Core business logic:
# Login -> open target order page -> classify result
# ============================================================

def find_load_in_logistically(load_number: str) -> dict:
    """
    Full workflow:
    1. Validate config
    2. Login and confirm TMS home page reached
    3. Navigate directly to target order URL
    4. Classify final page
    """
    if not LOGISTICALLY_BASE_URL:
        raise ValueError("LOGISTICALLY_BASE_URL is not set")

    if not LOGISTICALLY_USERNAME:
        raise ValueError("LOGISTICALLY_USERNAME is not set")

    if not LOGISTICALLY_PASSWORD:
        raise ValueError("LOGISTICALLY_PASSWORD is not set")

    login_url = f"{LOGISTICALLY_BASE_URL}/"
    order_url = f"{LOGISTICALLY_BASE_URL}/tms/#/3pl/orders/{load_number}"

    print(f"=== VERSION {APP_VERSION} ===")
    print("=== Worker config check ===")
    print(json.dumps({
        "base_url": LOGISTICALLY_BASE_URL,
        "username_present": bool(LOGISTICALLY_USERNAME),
        "headless": HEADLESS,
        "login_url": login_url,
        "order_url": order_url
    }))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        context = browser.new_context()
        page = context.new_page()

        try:
            # Step 1: Login and confirm we reached TMS home
            perform_login(page)

            # Step 2: Open the target order URL directly
            print("=== Opening target order page ===")
            page.goto(order_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)

            print("=== Current URL after opening target order page ===")
            print(page.url)

            # Step 3: Detect whether the resulting page is real order / error / login
            page_result = detect_order_page(page, load_number)

            result = {
                "success": True,
                "version": APP_VERSION,
                "load_found": page_result["load_found"],
                "page_type": page_result["page_type"],
                "reason": page_result["reason"],
                "load_number_or_po": load_number,
                "current_url": page_result["current_url"],
                "body_preview": page_result["body_preview"],
                "message": (
                    f"Load {load_number} found in TMS"
                    if page_result["load_found"]
                    else f"Load {load_number} not found in TMS or session was not on a valid order page"
                )
            }

            print("=== Worker result ===")
            print(json.dumps(result))

            return result

        finally:
            browser.close()


# ============================================================
# Health endpoint
# ============================================================

@app.get("/")
def health():
    """
    Health endpoint used to confirm:
    - latest code is deployed
    - env vars are present
    """
    return {
        "status": "ok",
        "version": APP_VERSION,
        "username_present": bool(LOGISTICALLY_USERNAME),
        "base_url_present": bool(LOGISTICALLY_BASE_URL),
        "headless": HEADLESS
    }


# ============================================================
# Main API endpoint
# ============================================================

@app.post("/lookup-load")
def lookup_load(payload: LoadLookupRequest):
    """
    Main worker endpoint for TMS load lookup.
    """
    try:
        print("=== Incoming request ===")
        print(payload.dict())

        if not payload.load_number_or_po:
            raise HTTPException(status_code=400, detail="Missing load_number_or_po")

        return find_load_in_logistically(payload.load_number_or_po)

    except Exception as e:
        print("=== WORKER ERROR ===")
        print(str(e))

        raise HTTPException(
            status_code=500,
            detail={
                "version": APP_VERSION,
                "error": str(e),
                "username_present": bool(LOGISTICALLY_USERNAME),
                "base_url_present": bool(LOGISTICALLY_BASE_URL),
                "headless": HEADLESS
            }
        )
