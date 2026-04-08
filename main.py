from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import os
import json

app = FastAPI()

LOGISTICALLY_BASE_URL = os.getenv("LOGISTICALLY_BASE_URL", "").rstrip("/")
LOGISTICALLY_USERNAME = os.getenv("LOGISTICALLY_USERNAME", "")
LOGISTICALLY_PASSWORD = os.getenv("LOGISTICALLY_PASSWORD", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"


class LoadLookupRequest(BaseModel):
    ticket_id: int
    load_number_or_po: str
    invoice_number: str
    invoice_total: str


def find_load_in_logistically(load_number: str) -> dict:
    if not LOGISTICALLY_BASE_URL:
        raise ValueError("LOGISTICALLY_BASE_URL is not set")
    if not LOGISTICALLY_USERNAME:
        raise ValueError("LOGISTICALLY_USERNAME is not set")
    if not LOGISTICALLY_PASSWORD:
        raise ValueError("LOGISTICALLY_PASSWORD is not set")

    login_url = f"{LOGISTICALLY_BASE_URL}/login"
    order_url = f"{LOGISTICALLY_BASE_URL}/tms/#/3pl/orders/{load_number}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()

        try:
            print("=== Opening Logistically login page ===")
            print(login_url)

            page.goto(login_url, wait_until="domcontentloaded", timeout=30000)

            page.locator('input[type="email"], input[name="email"], input[name="username"], input[type="text"]').first.fill(LOGISTICALLY_USERNAME)
            page.locator('input[type="password"]').first.fill(LOGISTICALLY_PASSWORD)
            page.locator('button[type="submit"], button:has-text("Login"), button:has-text("Sign in")').first.click()

            page.wait_for_load_state("networkidle", timeout=30000)

            print("=== Opening Logistically load page ===")
            print(order_url)

            page.goto(order_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=30000)

            current_url = page.url
            body_text = page.locator("body").inner_text(timeout=10000)

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


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/lookup-load")
def lookup_load(payload: LoadLookupRequest):
    if not payload.load_number_or_po:
        raise HTTPException(status_code=400, detail="Missing load_number_or_po")

    return find_load_in_logistically(payload.load_number_or_po)
