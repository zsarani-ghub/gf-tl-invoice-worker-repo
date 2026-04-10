from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
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

APP_VERSION = "worker-debug-2026-04-10-v7"

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

    We use multiple text signals rather than one single keyword
    to reduce false positives.
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

    # Require multiple signals before classifying as login page
    return signal_count >= 3


# ============================================================
# Helper: Detect whether current page is a valid order page
# or a known error/login page
# ============================================================

def detect_order_page(page, load_number: str) -> dict:
    """
    Classify the page into one of these buckets:
    - login_page
    - not_found_or_no_access
    - order_page
    - unknown

    IMPORTANT:
    URL alone is not enough because the attempted order URL
    itself contains the load number even when the page is not valid.
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
    # 2. Detect 403 / no access style page
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
    # 3. Detect real order page using strong content signals
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

    strong_signal_count = sum(1 for s in strong_order_signals if s in body_text_lower)

    # Only mark as found if:
    # - load number appears in page content
    # - enough real order-page indicators exist
    if load_lower in body_text_lower and strong_signal_count >= 4:
        return {
            "page_type": "order_page",
            "load_found": True,
            "reason": "Detected real order page using order-page content signals",
            "current_url": current_url,
            "body_preview": body_text[:1000]
        }

    # ------------------------------------------------
    # 4. Fallback unknown state
    # ------------------------------------------------
    return {
        "page_type": "unknown",
        "load_found": False,
        "reason": "Could not confirm order page",
        "current_url": current_url,
        "body_preview": body_text[:1000]
    }


# ============================================================
# Helper: Perform login
# IMPORTANT:
# This v7 version is intentionally tolerant.
# It does not fail early just because the page still briefly
# looks like login after clicking sign-in.
# ============================================================

def perform_login(page):
    """
    Perform login without enforcing an early hard failure.

    Some systems:
    - keep login DOM elements around briefly
    - establish session asynchronously
    - redirect slowly
    - behave differently under automation

    So this function:
    - opens login page
    - fills credentials
    - clicks sign in
    - waits a little
    - leaves final success/failure determination
      to the later order-page navigation step
    """
    login_url = f"{LOGISTICALLY_BASE_URL}/"

    print("=== Opening login page ===")
    page.goto(login_url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2000)

    # Fill credentials
    print("=== Filling email field ===")
    page.locator("#email").fill(LOGISTICALLY_USERNAME)

    print("=== Filling password field ===")
    page.locator("#password").fill(LOGISTICALLY_PASSWORD)

    print("=== Clicking sign-in button ===")
    page.locator("#sign-in").click()

    # Give the app time to establish session / redirect
    print("=== Waiting after login click ===")
    page.wait_for_timeout(5000)

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        print("=== Network did not fully settle after login click ===")

    print("=== URL after login attempt ===")
    print(page.url)

    try:
        body_preview = page.locator("body").inner_text(timeout=15000)[:1000]
        print("=== Body preview after login attempt ===")
        print(body_preview)
    except Exception:
        print("=== Could not read body preview after login attempt ===")

    # Do not fail here.
    # Final determination happens only after trying to open the target order page.
    return True


# ============================================================
# Core business logic:
# Login -> open target order page -> classify result
# ============================================================

def find_load_in_logistically(load_number: str) -> dict:
    """
    Full TMS lookup workflow:
    1. Validate configuration
    2. Login
    3. Open target order page
    4. Detect whether load is truly found
    5. Return structured result
    """
    # Validate required configuration first
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
            # ------------------------------------------------
            # Step 1: Perform login
            # ------------------------------------------------
            perform_login(page)

            # ------------------------------------------------
            # Step 2: Open target order page directly
            # ------------------------------------------------
            print("=== Opening target order page ===")
            page.goto(order_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)

            print("=== Current URL after opening target order page ===")
            print(page.url)

            # ------------------------------------------------
            # Step 3: Classify the resulting page
            # ------------------------------------------------
            page_result = detect_order_page(page, load_number)

            # ------------------------------------------------
            # Step 4: Build final structured result
            # ------------------------------------------------
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
# Useful for proving:
# - latest code is deployed
# - env vars are present
# ============================================================

@app.get("/")
def health():
    """
    Basic health endpoint for deployment and config verification.
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
    API endpoint called by upstream systems.

    Expected input:
    - ticket_id
    - load_number_or_po
    - invoice_number
    - invoice_total

    Returns:
    - structured result indicating whether the load page
      was truly found in TMS
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
