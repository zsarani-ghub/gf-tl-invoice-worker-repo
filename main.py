from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import os
import json

app = FastAPI()

# ============================================================
# Version marker
# ============================================================

APP_VERSION = "worker-debug-2026-04-10-v5"

# ============================================================
# Environment variables
# ============================================================

LOGISTICALLY_BASE_URL = os.getenv("LOGISTICALLY_BASE_URL", "").rstrip("/")
LOGISTICALLY_USERNAME = os.getenv("LOGISTICALLY_USERNAME", "")
LOGISTICALLY_PASSWORD = os.getenv("LOGISTICALLY_PASSWORD", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

# ============================================================
# Request model
# ============================================================

class LoadLookupRequest(BaseModel):
    ticket_id: int
    load_number_or_po: str
    invoice_number: str
    invoice_total: str


# ============================================================
# Helpers
# ============================================================

def detect_order_page(page, load_number: str) -> dict:
    """
    Detect whether the current page is:
    - a valid order page for the requested load
    - a TMS error/access page
    - something unknown
    """
    current_url = page.url
    body_text = page.locator("body").inner_text(timeout=15000)

    body_text_lower = body_text.lower()
    current_url_lower = current_url.lower()
    load_lower = load_number.lower()

    # 1. Explicit TMS access / not-found style page
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

    # 2. Strong positive signal: actual order page
    strong_found_signals = [
        f"edit order: order {load_lower}" in body_text_lower,
        f"order {load_lower}" in body_text_lower,
        f"/orders/{load_lower}" in current_url_lower
    ]

    if any(strong_found_signals):
        return {
            "page_type": "order_page",
            "load_found": True,
            "reason": "Detected real order page",
            "current_url": current_url,
            "body_preview": body_text[:1000]
        }

    # 3. Weak fallback if load number is somewhere on page
    if load_lower in body_text_lower:
        return {
            "page_type": "possible_order_page",
            "load_found": True,
            "reason": "Load number appears in page body",
            "current_url": current_url,
            "body_preview": body_text[:1000]
        }

    # 4. Unknown page state
    return {
        "page_type": "unknown",
        "load_found": False,
        "reason": "Could not confirm order page",
        "current_url": current_url,
        "body_preview": body_text[:1000]
    }


# ============================================================
# Core worker logic
# ============================================================

def find_load_in_logistically(load_number: str) -> dict:
    """
    Logs into Logistically and attempts to open a specific load page.
    Correctly handles:
    - load found
    - 403 / no access / not found page
    - unknown page state
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
        "LOGISTICALLY_BASE_URL": LOGISTICALLY_BASE_URL,
        "LOGISTICALLY_USERNAME": LOGISTICALLY_USERNAME,
        "HEADLESS": HEADLESS,
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
            # Step 1: Open login page
            # ------------------------------------------------
            print("=== Opening Logistically login page ===")
            page.goto(login_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2000)

            print("=== Current URL after login page load ===")
            print(page.url)

            # ------------------------------------------------
            # Step 2: Fill login form
            # ------------------------------------------------
            print("=== Filling email field ===")
            page.locator("#email").fill(LOGISTICALLY_USERNAME)

            print("=== Filling password field ===")
            page.locator("#password").fill(LOGISTICALLY_PASSWORD)

            print("=== Clicking sign-in button ===")
            page.locator("#sign-in").click()

            # ------------------------------------------------
            # Step 3: Wait after login
            # ------------------------------------------------
            print("=== Waiting after login click ===")
            page.wait_for_timeout(5000)
            page.wait_for_load_state("networkidle", timeout=60000)

            print("=== Current URL after login attempt ===")
            print(page.url)

            # ------------------------------------------------
            # Step 4: Open target order page directly
            # ------------------------------------------------
            print("=== Opening Logistically load page ===")
            page.goto(order_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)

            current_url = page.url
            print("=== Current URL after opening order page ===")
            print(current_url)

            # ------------------------------------------------
            # Step 5: Detect page outcome
            # ------------------------------------------------
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
                    f"Load {load_number} found"
                    if page_result["load_found"]
                    else f"Load {load_number} not found or inaccessible"
                )
            }

            print("=== Worker result ===")
            print(json.dumps(result))

            return result

        finally:
            browser.close()


# ============================================================
# Routes
# ============================================================

@app.get("/")
def health():
    return {
        "status": "ok",
        "version": APP_VERSION,
        "username_present": bool(LOGISTICALLY_USERNAME),
        "base_url_present": bool(LOGISTICALLY_BASE_URL),
        "headless": HEADLESS
    }


@app.post("/lookup-load")
def lookup_load(payload: LoadLookupRequest):
    try:
        print(f"=== VERSION {APP_VERSION} ===")
        print(f"LOGISTICALLY_USERNAME raw value: [{LOGISTICALLY_USERNAME}]")
        print(f"LOGISTICALLY_BASE_URL raw value: [{LOGISTICALLY_BASE_URL}]")
        print(f"HEADLESS raw value: [{HEADLESS}]")

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
