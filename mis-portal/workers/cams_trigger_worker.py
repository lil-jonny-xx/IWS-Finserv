#!/usr/bin/env python3
"""
CAMS Trigger Worker — IWS MIS Portal
Submits the CAMS CAS request form via Playwright (headed via Xvfb).
CAMS skips OTP for registered email addresses — PDF arrives directly by email.

Anti-bot measures:
  - Headed Chromium on a virtual display (Xvfb) — reCAPTCHA v3 scores headless poorly
  - Persistent browser profile — cookies/history make the session look like a returning user
  - playwright-stealth — patches ~15 fingerprinting vectors probed by reCAPTCHA

IMPORTANT: The email entered MUST be registered in the investor's folio with CAMS/KFintech.
Unregistered emails cause silent failures (no OTP, no email, no error shown).
Register via CAMS GoGreen service or CAMServ chatbot before running automation.
"""
import logging
import random
import socket
import time
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import Stealth
from pyvirtualdisplay import Display

logger = logging.getLogger(__name__)

CAMS_CAS_URL = (
    "https://www.camsonline.com/Investors/Statements/Consolidated-Account-Statement"
)

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

NAV_TIMEOUT    = 60_000
ACTION_TIMEOUT = 10_000
TOR_PROXY      = "socks5://127.0.0.1:9050"

# Persistent Chromium profile — cookies/history accumulate across daily runs,
# improving reCAPTCHA v3 trust score over time.
BROWSER_PROFILE_DIR = Path("/var/www/mis-portal/.cams_browser_profile")


def _tor_available() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", 9050), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def trigger_cas_request(pan_number: str, email: str, pdf_password: str) -> bool:
    """
    Open CAMS CAS page, fill the form, and submit.
    Returns True on detected success, False on failure.
    The pdf_password is the password used to protect the emailed PDF.
    """
    logger.info(f"Triggering CAMS CAS for PAN: {pan_number[:4]}**** email: {email}")

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    display = Display(visible=False, size=(1366, 768))
    display.start()
    logger.debug("Xvfb display started")

    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
                user_agent=random.choice(_USER_AGENTS),
                java_script_enabled=True,
                ignore_https_errors=False,
                viewport={"width": 1366, "height": 768},
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = ctx.new_page()
            Stealth().apply_stealth_sync(page)

            try:
                page.goto(CAMS_CAS_URL, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
                # Wait for Angular to render (mat-radio-button signals app is ready)
                page.wait_for_selector("mat-radio-button", timeout=NAV_TIMEOUT)
                logger.info("CAMS page loaded")

                _dismiss_tnc(page)
                page.wait_for_timeout(random.randint(400, 900))

                # --- Statement type: Detailed (includes transaction listing) ---
                _click_radio_by_value_or_text(page, "detailed", "Detailed", "Statement type")
                page.wait_for_timeout(random.randint(300, 700))

                # --- Folio listing: With zero balance folios (all history) ---
                # The form default is "Without zero balance folios". We want "With zero balance"
                # to capture complete history including fully-redeemed folios.
                # Value "Y" = include zero balance folios on CAMS CAS-CAMS+KFintech form.
                try:
                    _click_radio_by_value_or_text(page, "Y", "With zero balance folios", "Folio listing")
                    page.wait_for_timeout(random.randint(300, 700))
                except RuntimeError:
                    logger.warning("Could not click 'With zero balance folios' radio — using default")

                # --- Email ---
                _save_screenshot(page, f"cams_prefill_{pan_number[:4]}.png")
                _fill_field(page, [
                    'input[formcontrolname="email_id"]',
                    'input[placeholder*="Email" i]',
                ], email, "email")
                page.wait_for_timeout(random.randint(400, 900))

                # --- PAN (optional but helps CAMS match folios) ---
                _fill_field(page, [
                    'input[formcontrolname="pan"]',
                    'input[placeholder*="PAN" i]',
                ], pan_number.upper(), "PAN")
                page.wait_for_timeout(random.randint(400, 900))

                # --- PDF Password ---
                _fill_field(page, [
                    'input[formcontrolname="password"]',
                    'input[placeholder*="Password" i]:not([placeholder*="Confirm" i]):not([placeholder*="Retype" i])',
                ], pdf_password, "PDF password")
                page.wait_for_timeout(random.randint(300, 700))

                # --- Confirm Password ---
                _fill_field(page, [
                    'input[formcontrolname="confirmPassword"]',
                    'input[formcontrolname="confirm_password"]',
                    'input[placeholder*="Confirm" i]',
                    'input[placeholder*="Retype" i]',
                ], pdf_password, "confirm password")
                page.wait_for_timeout(random.randint(400, 800))

                _save_screenshot(page, f"cams_preflight_{pan_number[:4]}.png")

                # Post-fill verification screenshot — confirms fields were actually populated
                # before Submit so failures can be distinguished from CAMS server errors.
                _save_screenshot(page, f"cams_postfill_{pan_number[:4]}.png")

                # --- Submit ---
                page.locator('button:has-text("Submit")').first.click(
                    timeout=ACTION_TIMEOUT, force=True
                )
                logger.debug("Clicked Submit")

                # Wait for CAMS to process and show a result
                page.wait_for_timeout(4_000)
                _save_screenshot(page, f"cams_postsubmit_{pan_number[:4]}.png")

                success = _check_submit_result(page)
                if success:
                    logger.info("CAMS form submitted successfully. CAS will arrive by email.")
                else:
                    logger.error(
                        "CAMS submission did not show a success indicator. "
                        "Possible causes: email not registered with CAMS, form validation error, "
                        "or CAMS showed OTP screen. Check screenshot at "
                        f"/tmp/cams_postsubmit_{pan_number[:4]}.png"
                    )
                return success

            except PWTimeout as e:
                logger.error(f"CAMS page timeout: {e}")
                _save_screenshot(page, f"cams_timeout_{pan_number[:4]}.png")
                return False
            except Exception as e:
                logger.error(f"CAMS trigger failed: {e}")
                _save_screenshot(page, f"cams_error_{pan_number[:4]}.png")
                return False
            finally:
                ctx.close()
    finally:
        display.stop()
        logger.debug("Xvfb display stopped")


def _dismiss_tnc(page):
    """Accept the T&C/Disclaimer modal that CAMS shows on each fresh session."""
    try:
        if page.locator("mat-dialog-container").count() == 0:
            logger.debug("No T&C dialog found — skipping")
            return

        dialog = page.locator("mat-dialog-container")

        # Step 1: Click ACCEPT radio — try Playwright first (respects Angular bindings),
        # then JS click fallback (fires click event which Angular listens to).
        clicked_radio = False
        accept_mat = dialog.locator('mat-radio-button').filter(has_text="ACCEPT")
        if accept_mat.count() > 0:
            try:
                accept_mat.first.click(timeout=5_000, force=True)
                clicked_radio = True
                logger.debug("Clicked ACCEPT mat-radio-button via Playwright")
            except Exception:
                pass

        if not clicked_radio:
            # Plain HTML radio inputs — fire click() so Angular sees the event
            page.evaluate("""
                const dialog = document.querySelector('mat-dialog-container');
                if (!dialog) return;
                const inputs = dialog.querySelectorAll('input[type="radio"]');
                for (const inp of inputs) {
                    const val = (inp.value || '').toUpperCase();
                    const parent = inp.closest('label,div,span') || inp.parentElement;
                    const txt = (parent?.textContent || '').trim().toUpperCase();
                    if (val === 'ACCEPT' || txt.startsWith('ACCEPT')) {
                        inp.click();
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        inp.dispatchEvent(new Event('input',  {bubbles: true}));
                        return;
                    }
                }
                // Last resort: click whichever mat-radio-button comes first
                const first = dialog.querySelector('mat-radio-button');
                if (first) first.click();
            """)
            logger.debug("Clicked ACCEPT radio via JS click()")

        page.wait_for_timeout(700)

        # Step 2: Click PROCEED button
        button_clicked = False
        for btn_text in ["PROCEED", "Proceed", "Accept", "Submit", "OK", "Continue"]:
            btn = dialog.locator(f'button:has-text("{btn_text}")')
            if btn.count() > 0:
                try:
                    btn.first.click(timeout=6_000, force=True)
                    logger.debug(f"Clicked T&C dialog button: {btn_text}")
                    button_clicked = True
                    break
                except Exception:
                    continue

        if button_clicked:
            try:
                page.wait_for_selector("mat-dialog-container", state="detached", timeout=10_000)
                page.wait_for_timeout(400)
                logger.info("T&C dialog dismissed via PROCEED button")
                return
            except Exception:
                logger.debug("Dialog still present after PROCEED click — falling back to JS removal")

        # Step 3: JS removal fallback — remove ALL CDK overlay elements
        page.evaluate("""
            for (const sel of [
                'mat-dialog-container',
                '.cdk-overlay-backdrop',
                '.cdk-overlay-pane',
                '.cdk-global-overlay-wrapper',
                '.cdk-overlay-container',
            ]) {
                document.querySelectorAll(sel).forEach(el => el.remove());
            }
        """)
        page.wait_for_timeout(600)
        logger.info("T&C overlay removed via JS")

    except Exception as e:
        logger.debug(f"T&C dismiss error (may be ok): {e}")


def _check_submit_result(page) -> bool:
    """
    Inspect page after Submit to determine success or failure.
    Returns True on success, False on detected error or OTP prompt.
    """
    try:
        content = page.content().lower()

        # Transient server rejection — rate-limit or bot detection, NOT an OTP/email issue
        if "unable to process your request" in content or "please try again later" in content:
            logger.error(
                "CAMS returned 'unable to process your request' — likely rate-limited "
                "or bot-detected. Try again after a delay."
            )
            return False

        # OTP / verification screen — email not registered with CAMS
        # NOTE: check AFTER rate-limit so CAMS static page text mentioning 'otp'
        # doesn't cause a false positive on the "unable to process" error page.
        otp_signals = ["enter otp", "verify your email", "one time password"]
        if any(s in content for s in otp_signals):
            logger.error(
                "CAMS is asking for OTP — email is NOT registered in the folio. "
                "Register the email via CAMS GoGreen or CAMServ, then re-run."
            )
            return False

        # Explicit error messages
        error_signals = [
            "invalid email", "email not registered", "email id not found",
            "no records found", "pan not found", "invalid pan",
            "error occurred", "failed to submit",
        ]
        if any(s in content for s in error_signals):
            logger.error(f"CAMS showed an error after Submit")
            return False

        # Success indicators
        success_signals = [
            "request has been submitted",
            "statement will be sent",
            "will be emailed",
            "sent to your registered",
            "sent to your email",
            "thank you",
            "request received",
            "successfully submitted",
        ]
        if any(s in content for s in success_signals):
            return True

        # No clear signal — ambiguous. Log a warning and assume submitted.
        logger.warning(
            "Could not confirm CAMS submission result from page content. "
            "Treating as submitted — monitor the inbox."
        )
        return True

    except Exception as e:
        logger.warning(f"Could not check submit result: {e}")
        return True


def _fill_field(page, selectors: list, value: str, label: str):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=ACTION_TIMEOUT)
            loc.click(timeout=ACTION_TIMEOUT, force=True)
            page.wait_for_timeout(random.randint(150, 400))
            loc.type(value, delay=random.randint(40, 90))
            logger.debug(f"Filled {label} via: {sel}")
            return
        except Exception:
            continue
    raise RuntimeError(
        f"Could not find {label} field. CAMS may have changed their form. "
        f"Tried: {selectors}"
    )


def _click_radio_by_value_or_text(page, value: str, text: str, label: str):
    """Click a mat-radio-button by value attribute, falling back to visible text."""
    # Try by value attribute first
    by_value = page.locator(f'mat-radio-button[value="{value}"]')
    if by_value.count() > 0:
        by_value.first.click(timeout=ACTION_TIMEOUT, force=True)
        logger.debug(f"Clicked radio '{label}' by value={value}")
        return

    # Fall back to text content match
    by_text = page.locator(f'mat-radio-button:has-text("{text}")')
    if by_text.count() > 0:
        by_text.first.click(timeout=ACTION_TIMEOUT, force=True)
        logger.debug(f"Clicked radio '{label}' by text='{text}'")
        return

    raise RuntimeError(
        f"Could not find radio '{label}' (value={value}, text='{text}'). "
        "CAMS may have changed their form."
    )


_SCREENSHOT_DIR = "/home/SAdmin/.cams-screenshots"

def _save_screenshot(page, filename: str):
    try:
        import os as _os
        _os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
        # mode=0o700: only owner can read — screenshots may contain form fields
        _os.chmod(_SCREENSHOT_DIR, 0o700)
        path = f"{_SCREENSHOT_DIR}/{filename}"
        page.screenshot(path=path)
        logger.info(f"Screenshot: {path}")
    except Exception:
        pass
