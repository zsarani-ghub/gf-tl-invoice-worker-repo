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
# Versioning (VERY important for debugging deployments)
# ============================================================

APP_VERSION = "worker-debug-2026-04-10-v6"

# ============================================================
# Environment variables (injected via Cloud Run)
# ============================================================

LOGISTICALLY_BASE_URL = os.getenv("LOGISTICALLY_BASE_URL", "").rstrip("/")
LOGISTICALLY_USERNAME = os.getenv("LOGISTICALLY_USERNAME", "")
LOGISTICALLY_PASSWORD = os.getenv("LOGISTICALLY_PASSWORD", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

# ============================================================
# Request schema (incoming API payload)
# ============================================================

class LoadLookupRequest(BaseModel):
    ticket_id: int
    load_number_or_po: str
    invoice_number: str
    invoice_total: str


# ============================================================
# Helper: Detect if current page is login page
# ============================================================

def is_login_page_text(body_text: str) -> bool:
    """
    Determines if the current page is the login screen
    based on presence of multiple login-related keywords.
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

    # Count how many login indicators are present
    signal_count = sum(1 for s in login_signals if s in body_text_lower)

    # If enough signals are present, assume login page
    return signal_count >= 3


# ============================================================
# Helper: Classify page after navigation
# ============================================================

def detect_order_page(page, load_number: str) -> dict:
    """
    Classifies the current page into one of:
    - login_page
    - not_found_or_no_access
    - order_page
    - unknown

    This is critical to avoid false positives.
    """

    current_url = page.url
    body_text = page.locator("body").inner_text(timeout=15000)

    body_text_lower = body_text.lower()
    load_lower = load_number.lower()

    # ------------------------------------------------
    # 1. Detect login page
    # ------------------------------------------------
    if is_login_page_text(body_text):
        return {
            "page_type": "login_page",
            "load_found": False,
            "reason": "Session appears to be on login page, not order page",
            "current_url": current_url,
            "body_preview": body_text[:1000]
        }

    # ------------------------------------------------
    # 2. Detect TMS access denied / 403 page
    # ------------------------------------------------
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

    # ------------------------------------------------
    # 3. Detect valid order page using strong signals
    # IMPORTANT: URL alone is NOT trusted
    # ------------------------------------------------
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

    # Count how many order-related signals exist
    strong_signal_count = sum(1 for s in strong_order_signals if s in body_text_lower)

    # Only mark as found if:
    # - load number is present
    # - AND enough order page indicators exist
    if load_lower in body_text_lower and strong_signal_count >= 4:
        return {
            "page_type": "order_page",
            "load_found": True,
            "reason": "Detected real order page using order-page content signals",
            "current_url": current_url,
            "body_preview": body_text[:1000]
        }

    # ------------------------------------------------
    # 4. Unknown state (safe fallback)
    # ------------------------------------------------
    return {
        "page_type": "unknown",
        "load_found": False,
        "reason": "Could not confirm order page",
        "current_url": current_url,
        "body_preview": body_text[:1000]
    }


# ============================================================
# Core: Perform login and verify success
# ============================================================

def perform_login(page):
    """
    Logs into Logistically and verifies that login succeeded.

    If login fails, raises an exception immediately.
    """

    login_url = f"{LOGISTICALLY_BASE_URL}/"

    print("=== Opening login page ===")
    page.goto(login_url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2000)

    # Fill credentials
    print("=== Filling credentials ===")
    page.locator("#email").fill(LOGISTICALLY_USERNAME)
    page.locator("#password").fill(LOGISTICALLY_PASSWORD)

    print("=== Clicking sign-in ===")
    page.locator("#sign-in").click()

    # Wait for login form to disappear (key signal)
    try:
        page.wait_for_selector("#email", state="hidden", timeout=15000)
    except PlaywrightTimeoutError:
        print("⚠️ Login form did not disappear in time")

    # Allow page to settle
    page.wait_for_timeout(4000)
    page.wait_for_load_state("networkidle", timeout=60000)

    current_url = page.url
    body_text = page.locator("body").inner_text(timeout=15000)

    print("=== Post-login URL ===", current_url)

    # If still on login page → login failed
    if is_login_page_text(body_text):
        raise ValueError("Login failed — still on login page")

    print("✅ Login successful")

    return True


# ============================================================
# Core: Main business logic (lookup load)
# ============================================================

def find_load_in_logistically(load_number: str) -> dict:
    """
    Full workflow:
    1. Login
    2. Navigate to load URL
    3. Detect page type
    4. Return structured result
    """

    # Basic config validation
    if not LOGISTICALLY_BASE_URL:
        raise ValueError("LOGISTICALLY_BASE_URL is not set")

    if not LOGISTICALLY_USERNAME:
        raise ValueError("LOGISTICALLY_USERNAME is not set")

    if not LOGISTICALLY_PASSWORD:
        raise ValueError("LOGISTICALLY_PASSWORD is not set")

    order_url = f"{LOGISTICALLY_BASE_URL}/tms/#/3pl/orders/{load_number}"

    print(f"=== VERSION {APP_VERSION} ===")
    print("=== Config ===")
    print(json.dumps({
        "base_url": LOGISTICALLY_BASE_URL,
        "username_present": bool(LOGISTICALLY_USERNAME),
        "headless": HEADLESS,
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
            # Step 1: Login
            perform_login(page)

            # Step 2: Open order page
            print("=== Opening order page ===")
            page.goto(order_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)

            print("=== Current URL ===", page.url)

            # Step 3: Detect result
            page_result = detect_order_page(page, load_number)

            # Step 4: Build structured response
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
                    else f"Load {load_number} not found in TMS or session invalid"
                )
            }

            print("=== Result ===")
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
    Quick sanity check endpoint.
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
    Entry point for external systems (e.g. Freshdesk worker).

    Validates input and triggers lookup workflow.
    """

    try:
        print(f"=== Incoming request === {payload.dict()}")

        if not payload.load_number_or_po:
            raise HTTPException(status_code=400, detail="Missing load_number_or_po")

        return find_load_in_logistically(payload.load_number_or_po)

    except Exception as e:
        print("=== ERROR ===", str(e))

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
