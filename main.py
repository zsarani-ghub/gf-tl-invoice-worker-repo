from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import os
import json

app = FastAPI()

# ============================================================
# Environment variables
# ============================================================

LOGISTICALLY_BASE_URL = os.getenv("LOGISTICALLY_BASE_URL", "").rstrip("/")
LOGISTICALLY_USERNAME = os.getenv("LOGISTICALLY_USERNAME", "")
LOGISTICALLY_PASSWORD = os.getenv("LOGISTICALLY_PASSWORD", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "false"

print("=== ENV DEBUG AT STARTUP ===")
print(f"BASE_URL=[{LOGISTICALLY_BASE_URL}]")
print(f"USERNAME=[{LOGISTICALLY_USERNAME}]")
print(f"PASSWORD_SET={[bool(LOGISTICALLY_PASSWORD)]}")
print(f"HEADLESS=[{HEADLESS}]")


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

print(f"LOGISTICALLY_USERNAME raw value: [{LOGISTICALLY_USERNAME}]")
def find_load_in_logistically(load_number: str) -> dict:
    """
    Logs into Logistically and attempts to open a specific load page.
    Returns whether the load appears to exist.
    """
    if not LOGISTICALLY_BASE_URL:
        raise ValueError("LOGISTICALLY_BASE_URL is not set")

    if not LOGISTICALLY_USERNAME:
        raise ValueError("LOGISTICALLY_USERNAME is not set")

    if not LOGISTICALLY_PASSWORD:
        raise ValueError("LOGISTICALLY_PASSWORD is not set")

    # Based on the provided login page HTML, login is served from "/"
    login_url = f"{LOGISTICALLY_BASE_URL}/"

    # Based on your SOP, this is the order route pattern after login
    order_url = f"{LOGISTICALLY_BASE_URL}/tms/#/3pl/orders/{load_number}"

    print("=== Worker config check ===")
    print(json.dumps({
        "LOGISTICALLY_BASE_URL": LOGISTICALLY_BASE_URL,
        "HEADLESS": HEADLESS,
        "login_url": login_url,
        "order_url": order_url
    }))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()

        try:
            # ------------------------------------------------
            # Step 1: Open login page
            # ------------------------------------------------
            print("=== Opening Logistically login page ===")
            page.goto(login_url, wait_until="networkidle", timeout=60000)

            print("=== Current URL after login page load ===")
            print(page.url)

            # Short wait for any JS behavior
            page.wait_for_timeout(2000)

            # ------------------------------------------------
            # Step 2: Fill login form using exact selectors
            # From provided page source:
            # - email input id="email"
            # - password input id="password"
            # - submit button id="sign-in"
            # ------------------------------------------------
            print("=== Filling email field ===")
            page.locator("#email").fill(LOGISTICALLY_USERNAME)

            print("=== Filling password field ===")
            page.locator("#password").fill(LOGISTICALLY_PASSWORD)

            print("=== Clicking sign-in button ===")
            page.locator("#sign-in").click()

            # Wait after login
            print("=== Waiting after login click ===")
            page.wait_for_timeout(5000)
            page.wait_for_load_state("networkidle", timeout=60000)

            print("=== Current URL after login attempt ===")
            print(page.url)

            print("=== Post-login page content preview ===")
            print(page.content()[:1500])

            # ------------------------------------------------
            # Step 3: Open target order page directly
            # ------------------------------------------------
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
    return {"status": "ok"}


@app.post("/lookup-load")
def lookup_load(payload: LoadLookupRequest):
    """
    Receives a load lookup request from the intake service
    and attempts to find that load in Logistically.
    """
    if not payload.load_number_or_po:
        raise HTTPException(status_code=400, detail="Missing load_number_or_po")

    try:
        return find_load_in_logistically(payload.load_number_or_po)
    except Exception as e:
        print("=== WORKER ERROR ===")
        print(str(e))
        raise HTTPException(status_code=500, detail=str(e))
