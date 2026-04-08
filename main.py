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
    Returns whether the load appears to exist.

    This version includes extra debug logging to help identify:
    - missing env vars
    - page load issues
    - selector issues
    - login problems
    """
    if not LOGISTICALLY_BASE_URL:
        raise ValueError("LOGISTICALLY_BASE_URL is not set")

    if not LOGISTICALLY_USERNAME:
        raise ValueError("LOGISTICALLY_USERNAME is not set")

    if not LOGISTICALLY_PASSWORD:
        raise ValueError("LOGISTICALLY_PASSWORD is not set")

    login_url = f"{LOGISTICALLY_BASE_URL}/login"
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

            # Give dynamic JS a moment to finish rendering
            print("=== Waiting for login page JS render ===")
            page.wait_for_timeout(3000)

            print("=== Current URL after login page load ===")
            print(page.url)

            # Log a small preview of page HTML to help debug selector issues
            print("=== Login page content preview ===")
            print(page.content()[:1500])

            # ------------------------------------------------
            # Step 2: Locate username field
            # ------------------------------------------------
            all_inputs = page.locator("input")
            input_count = all_inputs.count()
            print(f"=== Input count found on page: {input_count} ===")

            if input_count == 0:
                raise ValueError("No input fields found on Logistically login page")

            # Debug approach:
            # use first input as username field
            username_input = page.locator("input").first

            print("=== Filling username field ===")
            username_input.fill(LOGISTICALLY_USERNAME)

            # ------------------------------------------------
            # Step 3: Locate password field
            # ------------------------------------------------
            password_input = page.locator('input[type="password"]')
            password_count = password_input.count()
            print(f"=== Password field count found: {password_count} ===")

            if password_count == 0:
                raise ValueError("No password field found on Logistically login page")

            print("=== Filling password field ===")
            password_input.first.fill(LOGISTICALLY_PASSWORD)

            # ------------------------------------------------
            # Step 4: Click login button
            # ------------------------------------------------
            button_count = page.locator("button").count()
            print(f"=== Button count found on page: {button_count} ===")

            if button_count == 0:
                raise ValueError("No button found on Logistically login page")

            print("=== Clicking first button for login ===")
            page.locator("button").first.click()

            # Wait after login click
            print("=== Waiting after login click ===")
            page.wait_for_timeout(5000)
            page.wait_for_load_state("networkidle", timeout=60000)

            print("=== Current URL after login attempt ===")
            print(page.url)

            print("=== Post-login page content preview ===")
            print(page.content()[:1500])

            # ------------------------------------------------
            # Step 5: Open target order page directly
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

            # Basic check for whether the load appears on the page or URL
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
