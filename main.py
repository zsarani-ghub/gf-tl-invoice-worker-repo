from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)
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

APP_VERSION = "worker-debug-2026-04-10-v9"

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
# Helper: Identify whether current page still looks like login
# ============================================================


def is_login_page_text(body_text: str) -> bool:
    """
    Detect whether the current page still looks like the TMS login page.

    We use multiple text signals rather than trusting one keyword.
    """
    body_text_lower = body_text.lower()

    login_signals = [
        "sign in",
        "e-mail",
        "password",
        "forgot password",
        "stay signed in",
        "powered by logistically tms",
    ]

    signal_count = sum(1 for signal in login_signals if signal in body_text_lower)
    return signal_count >= 3


# ============================================================
# Helper: Classify final page after attempting to open order URL
# ============================================================


def detect_order_page(page, load_number: str) -> dict:
    """
    Classify the resulting page into one of:
    - login_page
    - not_found_or_no_access
    - order_page
    - unknown

    IMPORTANT:
    URL alone is not trusted because the requested order URL itself
    always includes the load number even when the page is invalid.
    """
    current_url = page.url
    body_text = page.locator("body").inner_text(timeout=15000)

    body_text_lower = body_text.lower()
    load_lower = load_number.lower()

    # --------------------------------------------------------
    # 1. Detect login page
    # --------------------------------------------------------
    if is_login_page_text(body_text):
        return {
            "page_type": "login_page",
            "load_found": False,
            "reason": "Session appears to be on login page, not order page",
            "current_url": current_url,
            "body_preview": body_text[:1000],
        }

    # --------------------------------------------------------
    # 2. Detect explicit TMS 403 / no-access page
    # --------------------------------------------------------
    if (
        "you don't have access to this page or resource" in body_text_lower
        or "(403)" in body_text_lower
    ):
        return {
            "page_type": "not_found_or_no_access",
            "load_found": False,
            "reason": "TMS returned access/not-found style page",
            "current_url": current_url,
            "body_preview": body_text[:1000],
        }

    # --------------------------------------------------------
    # 3. Detect real order page using strong content signals
    # --------------------------------------------------------
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
        "invoice",
    ]

    strong_signal_count = sum(
        1 for signal in strong_order_signals if signal in body_text_lower
    )

    # A real order page should contain the load number in body text
    # plus enough strong order-page indicators.
    if load_lower in body_text_lower and strong_signal_count >= 4:
        return {
            "page_type": "order_page",
            "load_found": True,
            "reason": "Detected real order page using order-page content signals",
            "current_url": current_url,
            "body_preview": body_text[:1000],
        }

    # --------------------------------------------------------
    # 4. Fallback unknown state
    # --------------------------------------------------------
    return {
        "page_type": "unknown",
        "load_found": False,
        "reason": "Could not confirm order page",
        "current_url": current_url,
        "body_preview": body_text[:1000],
    }


# ============================================================
# Helper: Perform login using the real HTML form
# ============================================================


def perform_login(page):
    """
    Perform login in a controlled way using the actual HTML login form.

    This version:
    - opens login page
    - fills email + password
    - sets hidden return_uri explicitly
    - submits the real form once
    - captures cookies before/after
    - confirms whether we reached the post-login TMS shell/home
    """
    login_url = f"{LOGISTICALLY_BASE_URL}/"
    expected_post_login_fragment = "/tms/#/3pl/"

    print("=== Opening login page ===")
    page.goto(login_url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2000)

    # Fill credentials into the actual login inputs
    print("=== Filling email field ===")
    page.locator("#email").fill(LOGISTICALLY_USERNAME)

    print("=== Filling password field ===")
    page.locator("#password").fill(LOGISTICALLY_PASSWORD)

    # Explicitly set return_uri because the page script normally populates it
    # from window.location.href. We want to force a clean landing path.
    print("=== Setting return_uri explicitly ===")
    page.locator("#return_uri").evaluate(
        "(el, value) => el.value = value",
        f"{LOGISTICALLY_BASE_URL}/tms/#/3pl/",
    )

    # Capture cookies before submit for comparison
    context = page.context
    cookies_before = context.cookies()
    print("=== Cookies before login ===")
    print(cookies_before)

    # Submit the actual form once, in a controlled way
    print("=== Submitting login form ===")
    try:
        with page.expect_navigation(wait_until="networkidle", timeout=30000):
            page.locator("#login-form").evaluate("form => form.submit()")
    except PlaywrightTimeoutError:
        print("=== No full navigation detected during login submit ===")

    # Give the site time to establish session / redirect
    page.wait_for_timeout(4000)

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        print("=== Network did not fully settle after login ===")

    current_url = page.url
    body_text = page.locator("body").inner_text(timeout=15000)

    print("=== URL after login submit ===")
    print(current_url)

    print("=== Body preview after login submit ===")
    print(body_text[:1200])

    # Capture cookies after submit to see whether session changed
    cookies_after = context.cookies()
    print("=== Cookies after login ===")
    print(cookies_after)

    # Save a screenshot to assist future debugging
    screenshot_path = "/tmp/post_login_state.png"
    page.screenshot(path=screenshot_path, full_page=True)
    print(f"=== Saved screenshot to {screenshot_path} ===")

    # Determine whether we actually reached the post-login TMS shell/home
    if expected_post_login_fragment not in current_url:
        raise ValueError(
            "Login failed — did not reach TMS home. "
            f"URL={current_url} | "
            f"Body preview={body_text[:500]} | "
            f"Cookies after login={cookies_after}"
        )

    # Extra protection: even if URL changed, page must not still be login page
    if is_login_page_text(body_text):
        raise ValueError(
            "Login failed — still on login page. "
            f"URL={current_url} | "
            f"Body preview={body_text[:500]} | "
            f"Cookies after login={cookies_after}"
        )

    print("=== Login successful - reached TMS home ===")
    return True


# ============================================================
# Core business logic
# ============================================================


def find_load_in_logistically(load_number: str) -> dict:
    """
    Full workflow:
    1. Validate configuration
    2. Login and confirm TMS shell/home reached
    3. Open target order URL directly
    4. Detect whether final page is valid order / login / 403 / unknown
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
    print(
        json.dumps(
            {
                "base_url": LOGISTICALLY_BASE_URL,
                "username_present": bool(LOGISTICALLY_USERNAME),
                "headless": HEADLESS,
                "login_url": login_url,
                "order_url": order_url,
            }
        )
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        # Use a realistic browser fingerprint similar to a normal desktop browser
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1600, "height": 900},
        )

        page = context.new_page()

        try:
            # ------------------------------------------------
            # Step 1: Login and confirm TMS home reached
            # ------------------------------------------------
            perform_login(page)

            # ------------------------------------------------
            # Step 2: Open target order URL directly
            # ------------------------------------------------
            print("=== Opening target order page ===")
            page.goto(order_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)

            print("=== Current URL after opening target order page ===")
            print(page.url)

            # ------------------------------------------------
            # Step 3: Classify resulting page
            # ------------------------------------------------
            page_result = detect_order_page(page, load_number)

            # ------------------------------------------------
            # Step 4: Build final structured response
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
                ),
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
    Health endpoint to confirm:
    - latest code is deployed
    - environment variables are present
    """
    return {
        "status": "ok",
        "version": APP_VERSION,
        "username_present": bool(LOGISTICALLY_USERNAME),
        "base_url_present": bool(LOGISTICALLY_BASE_URL),
        "headless": HEADLESS,
    }


# ============================================================
# Main API endpoint
# ============================================================


@app.post("/lookup-load")
def lookup_load(payload: LoadLookupRequest):
    """
    Main worker endpoint for TMS load lookup.

    Expected input:
    - ticket_id
    - load_number_or_po
    - invoice_number
    - invoice_total

    Returns structured result indicating whether the target
    load page was truly found in TMS.
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
                "headless": HEADLESS,
            },
        )
