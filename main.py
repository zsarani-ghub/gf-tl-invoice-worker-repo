from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import os
import json

app = FastAPI()

# ============================================================
# Version marker
# ============================================================

APP_VERSION = "worker-debug-2026-04-10-v1"

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
# Core worker logic
# ============================================================

def find_load_in_logistically(load_number: str) -> dict:
    """
    Logs into Logistically and attempts to open a specific load page.
    """

    if not LOGISTICALLY_BASE_URL:
        raise ValueError("LOGISTICALLY_BASE_URL is not set")

    if not LOGISTICALLY_USERNAME:
        raise ValueError("LOGISTICALLY_USERNAME is not set")

    if not LOGISTICALLY_PASSWORD:
        raise ValueError("LOGISTICALLY_PASSWORD is not set")

    # Based on the login page HTML you provided, the login form is on "/"
    login_url = f"{LOGISTICALLY_BASE_URL}/"

    # Based on your SOP and earlier examples, this is the order route pattern
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
            # Step 1: Open login page
            print("=== Opening Logistically login page ===")
            page.goto(login_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2000)

            print("=== Current URL after login page load ===")
            print(page.url)

            print("=== Login page content preview ===")
            print(page.content()[:1200])

            # Step 2: Fill login form using exact selectors
            print("=== Filling email field ===")
            page.locator("#email").fill(LOGISTICALLY_USERNAME)

            print("=== Filling password field ===")
            page.locator("#password").fill(LOGISTICALLY_PASSWORD)

            print("=== Clicking sign-in button ===")
            page.locator("#sign-in").click()

            # Step 3: Wait after login
            print("=== Waiting after login click ===")
            page.wait_for_timeout(5000)
            page.wait_for_load_state("networkidle", timeout=60000)

            print("=== Current URL after login attempt ===")
            print(page.url)

            print("=== Post-login page content preview ===")
            print(page.content()[:1200])

            # Step 4: Open target order page directly
            print("=== Opening Logistically load page ===")
            page.goto(order_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)

            current_url = page.url
            print("=== Current URL after opening order page ===")
            print(current_url)

            body_text = page.locator("body").inner_text(timeout=15000)

            print("=== Body text preview ===")
            print(body_text[:1500])

            load_found = (load_number in current_url) or (load_number in body_text)

            result = {
                "success": True,
                "version": APP_VERSION,
                "load_found": load_found,
                "load_number_or_po": load_number,
                "current_url": current_url,
                "message": f"Load {load_number} found" if load_found else f"Load {load_number} not found"
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
