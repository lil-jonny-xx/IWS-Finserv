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
import os
import random
import shutil
import tempfile
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

# Persistent Chromium profile — cookies/history accumulate across daily runs,
# improving reCAPTCHA v3 trust score over time.
BROWSER_PROFILE_DIR = Path("/var/www/mis-portal/.cams_browser_profile")

# Auto-retry on transient CAMS rate-limit / bot-detection (e.g. the IWS entity).
# Exponential backoff: attempt N waits CAMS_RETRY_BASE_DELAY * 2**(N-1) seconds.
CAMS_RETRY_MAX_ATTEMPTS = int(os.getenv("CAMS_RETRY_MAX_ATTEMPTS", "3"))
CAMS_RETRY_BASE_DELAY   = int(os.getenv("CAMS_RETRY_BASE_DELAY", "60"))  # seconds


class CamsRateLimited(Exception):
    """Raised when CAMS rejects the submission with a transient rate-limit /
    bot-detection page (retryable), as opposed to a hard failure such as an
    unregistered email (not retryable)."""


def trigger_cas_request(pan_number: str, email: str, pdf_password: str,
                        fresh_profile: bool = False) -> bool:
    """
    Open CAMS CAS page, fill the form, and submit.
    Returns True on detected success, False on failure.
    The pdf_password is the password used to protect the emailed PDF.

    fresh_profile=True launches from a throwaway browser profile instead of the
    persistent one. Used on retries: a whole-form reset means the persistent
    profile's reCAPTCHA v3 trust has degraded over the run, so re-submitting from
    the same profile would just hit the same reset. A clean profile gives the
    retry an unpoisoned session to be scored against.
    """
    logger.info("Triggering CAMS CAS request")

    if fresh_profile:
        profile_dir = Path(tempfile.mkdtemp(prefix="cams_profile_"))
        logger.info("Using a fresh throwaway browser profile: %s", profile_dir)
    else:
        profile_dir = BROWSER_PROFILE_DIR
        profile_dir.mkdir(parents=True, exist_ok=True)
        for stale in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            p = profile_dir / stale
            if p.exists() or p.is_symlink():
                p.unlink()
                logger.warning("Removed stale browser lock: %s", stale)

    display = Display(visible=False, size=(1366, 768))
    display.start()
    logger.debug("Xvfb display started")

    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-crash-reporter",
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
                try:
                    _click_radio_by_value_or_text(page, "Y", "With zero balance folios", "Folio listing")
                    page.wait_for_timeout(random.randint(300, 700))
                    logger.info("Folio listing: With zero balance folios selected")
                except RuntimeError:
                    logger.warning("Could not click 'With zero balance folios' radio — using default")

                # --- Period: Specific Period with full history from date ---
                # Default is "Current Financial Year" which gives only FY-to-date transactions.
                # We need the full history to compute CAGR / XIRR from first investment date.
                try:
                    _click_radio_by_value_or_text(page, "specific", "Specific Period", "Period")
                    page.wait_for_timeout(random.randint(400, 800))
                    from_date_str = "01-Apr-1992"
                    to_date_str   = date.today().strftime("%d-%b-%Y")
                    _fill_date_field(page, [
                        '#fromDate_new',
                        'input[formcontrolname="from_date"]',
                        'input[placeholder="fromdate"]',
                        'input[formcontrolname="fromDate"]',
                        'input[formcontrolname="startDate"]',
                        'input[formcontrolname="start_date"]',
                        'input[aria-label*="from" i]',
                        'input[placeholder*="From" i]',
                    ], from_date_str, "FROM DATE")
                    page.wait_for_timeout(random.randint(200, 500))
                    _fill_date_field(page, [
                        '#to-date-input',
                        'input[formcontrolname="to_date"]',
                        'input[placeholder="todate"]',
                        'input[formcontrolname="toDate"]',
                        'input[formcontrolname="endDate"]',
                        'input[formcontrolname="end_date"]',
                        'input[aria-label*="to" i]',
                        'input[placeholder*="To" i]',
                    ], to_date_str, "TO DATE")
                    page.wait_for_timeout(random.randint(300, 600))
                    logger.info(f"Period set: {from_date_str} → {to_date_str}")
                except RuntimeError as e:
                    logger.warning(f"Could not set Specific Period — using CAMS default period: {e}")

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

                # Dismiss any disclaimer that appeared during form filling
                _dismiss_tnc(page)

                # Post-fill verification screenshot — confirms fields were actually populated
                # before Submit so failures can be distinguished from CAMS server errors.
                _save_screenshot(page, f"cams_postfill_{pan_number[:4]}.png")

                # Guard: dismissing the T&C/Disclaimer can trigger an auto-submit,
                # putting the success page up before we even attempt to click Submit.
                if _is_success_page(page):
                    logger.info("CAMS form submitted successfully. CAS will arrive by email.")
                    return True

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
                _save_screenshot(page, f"cams_timeout_{pan_number[:4]}.png")
                # Submit may have gone through before the timeout fired —
                # check whether the success page is already showing.
                if _is_success_page(page):
                    logger.info("Submit timed out but success page detected — treating as success.")
                    return True
                logger.error(f"CAMS page timeout: {e}")
                return False
            except CamsRateLimited:
                # Retryable — let it propagate to trigger_cas_request_with_retry.
                _save_screenshot(page, f"cams_ratelimit_{pan_number[:4]}.png")
                raise
            except Exception as e:
                logger.error(f"CAMS trigger failed: {e}")
                _save_screenshot(page, f"cams_error_{pan_number[:4]}.png")
                return False
            finally:
                ctx.close()
    finally:
        display.stop()
        logger.debug("Xvfb display stopped")
        if fresh_profile:
            shutil.rmtree(profile_dir, ignore_errors=True)


def trigger_cas_request_with_retry(
    pan_number: str,
    email: str,
    pdf_password: str,
    max_attempts: int = CAMS_RETRY_MAX_ATTEMPTS,
    base_delay: int = CAMS_RETRY_BASE_DELAY,
) -> bool:
    """trigger_cas_request with bounded exponential backoff on CAMS rate-limiting.

    Retries ONLY transient rate-limit / bot-detection rejections (CamsRateLimited);
    hard failures (e.g. unregistered email) return False immediately without retry.
    Returns True on success, False if all attempts are exhausted or a hard failure
    occurs. Keeps the bool contract expected by callers.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            # First attempt uses the persistent profile (accumulated reCAPTCHA
            # trust); retries use a throwaway profile to escape the degraded
            # session that triggered the rate-limit / form reset.
            return trigger_cas_request(
                pan_number, email, pdf_password,
                fresh_profile=(attempt > 1),
            )
        except CamsRateLimited:
            if attempt >= max_attempts:
                logger.error(
                    f"CAMS rate-limited after {max_attempts} attempt(s) — giving up."
                )
                return False
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                f"CAMS rate-limited (attempt {attempt}/{max_attempts}) — "
                f"backing off {delay}s before retry."
            )
            time.sleep(delay)
    return False


def _dismiss_tnc(page):
    """
    Accept any T&C / Disclaimer modal CAMS shows — handles both Angular Material
    (mat-dialog-container) and plain HTML modals (e.g. bootstrap-style Disclaimer
    that appears mid-session for some entities).
    """
    try:
        # Detect which kind of dialog is present
        has_mat    = page.locator("mat-dialog-container").count() > 0
        # Plain modal: only match real dialog containers. The page footer always
        # contains a "Disclaimer" quick-link, so matching any div whose text
        # includes "Disclaimer" fires on every page — and the fallback clicks
        # below then reset the form and submit it empty.
        has_plain  = page.evaluate("""
            (() => {
                const sels = '[role="dialog"], .modal.show, .modal.in, .modal-dialog, .swal2-container';
                for (const el of document.querySelectorAll(sels)) {
                    if (el.closest('mat-dialog-container')) continue;
                    const style = getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    if (el.offsetParent === null && style.position !== 'fixed') continue;
                    const txt = (el.innerText || '').trim();
                    if (txt.includes('Disclaimer') && el.querySelector('input[type="radio"], button')) return true;
                }
                return false;
            })()
        """)

        if not has_mat and not has_plain:
            logger.debug("No T&C/Disclaimer dialog found — skipping")
            return

        logger.info(f"Disclaimer modal detected (mat={has_mat}, plain={has_plain})")

        # Step 1: Click ACCEPT — works for both mat-radio-button and plain inputs.
        # Search ONLY inside the dialog: the CAS form's own radios (statement
        # type, folio listing) live outside it, and clicking one re-renders the
        # Angular form and wipes every filled field.
        page.evaluate("""
            (() => {
                const dialogSel = 'mat-dialog-container, [role="dialog"], .modal.show, .modal.in, .modal-dialog, .swal2-container';
                const dialogs = [...document.querySelectorAll(dialogSel)];
                if (!dialogs.length) return;
                for (const dlg of dialogs) {
                    // Try Angular Material radio first
                    const matBtns = [...dlg.querySelectorAll('mat-radio-button')];
                    for (const b of matBtns) {
                        if ((b.textContent || '').toUpperCase().includes('ACCEPT')) {
                            b.click(); return;
                        }
                    }
                    // Plain HTML radio inputs
                    const inputs = [...dlg.querySelectorAll('input[type="radio"]')];
                    for (const inp of inputs) {
                        const val = (inp.value || '').toUpperCase();
                        const lbl = inp.closest('label') || inp.parentElement;
                        const txt = (lbl?.textContent || '').trim().toUpperCase();
                        if (val === 'ACCEPT' || txt.startsWith('ACCEPT')) {
                            inp.checked = true;
                            inp.click();
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                            inp.dispatchEvent(new Event('input',  {bubbles: true}));
                            return;
                        }
                    }
                }
                // Last resort: first radio inside a dialog only — never a form radio
                const first = document.querySelector(dialogSel + ' mat-radio-button, ' +
                    dialogSel.split(', ').map(s => s + ' input[type="radio"]').join(', '));
                if (first) first.click();
            })()
        """)
        page.wait_for_timeout(700)

        # Step 2: Click PROCEED / Accept button — scoped to the dialog so we never
        # hit the CAS form's own Submit button.
        _dialog_sel = ('mat-dialog-container, [role="dialog"], .modal.show, '
                       '.modal.in, .modal-dialog, .swal2-container')
        button_clicked = False
        for btn_text in ["PROCEED", "Proceed", "Accept", "Submit", "OK", "Continue"]:
            btn = page.locator(_dialog_sel).locator(f'button:has-text("{btn_text}")')
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
                # Wait for either mat-dialog or plain modal to disappear
                page.wait_for_function("""
                    () => {
                        const mat = document.querySelector('mat-dialog-container');
                        if (mat) return false;
                        const sels = '[role="dialog"], .modal.show, .modal.in, .modal-dialog, .swal2-container';
                        const plain = [...document.querySelectorAll(sels)].some(el => {
                            const style = getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden') return false;
                            if (el.offsetParent === null && style.position !== 'fixed') return false;
                            return (el.innerText || '').includes('Disclaimer') &&
                                   el.querySelector('input[type="radio"]');
                        });
                        return !plain;
                    }
                """, timeout=10_000)
                page.wait_for_timeout(400)
                logger.info("T&C/Disclaimer dialog dismissed via PROCEED button")
                return
            except Exception:
                logger.debug("Dialog still present after PROCEED click — falling back to JS removal")

        # Step 3: JS removal fallback — nuke all overlay / modal elements
        page.evaluate("""
            for (const sel of [
                'mat-dialog-container',
                '.cdk-overlay-backdrop',
                '.cdk-overlay-pane',
                '.cdk-global-overlay-wrapper',
                '.cdk-overlay-container',
                '.modal-backdrop',
                '.modal.show',
                '.modal.in',
            ]) {
                document.querySelectorAll(sel).forEach(el => el.remove());
            }
            // Also fix body scroll-lock that modals apply
            document.body.classList.remove('modal-open', 'cdk-global-scrollblock');
            document.body.style.overflow = '';
        """)
        page.wait_for_timeout(600)
        logger.info("T&C/Disclaimer overlay removed via JS fallback")

    except Exception as e:
        logger.debug(f"T&C dismiss error (may be ok): {e}")


def _is_success_page(page) -> bool:
    """
    Returns True only on definitive CAMS success signals.
    Unlike _check_submit_result, does NOT fall through to True on ambiguous content —
    used for pre-Submit and post-timeout guards where we cannot risk a false positive.
    """
    try:
        content = page.content().lower()
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
        return any(s in content for s in success_signals)
    except Exception:
        return False


def _check_submit_result(page) -> bool:
    """
    Inspect page after Submit to determine success or failure.
    Returns True on success, False on detected error or OTP prompt.
    """
    try:
        content = page.content().lower()

        # Transient server rejection — rate-limit or bot detection, NOT an OTP/email issue.
        # Raise (not return False) so the retry wrapper can distinguish it from a hard
        # failure and back off + retry.
        if "unable to process your request" in content or "please try again later" in content:
            logger.error(
                "CAMS returned 'unable to process your request' — likely rate-limited "
                "or bot-detected. Will back off and retry."
            )
            raise CamsRateLimited("CAMS transient rejection (rate-limit / bot-detection)")

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

        # Form-reset detection: CAMS navigates back to a blank form on certain
        # submission failures. Two distinct causes, handled differently:
        #
        #  • Whole-form reset — the email field is cleared too ("please enter the
        #    email" / "email is required"). The form we filled correctly was wiped:
        #    the hallmark of a transient reCAPTCHA/session reset, which accumulates
        #    over a long run (entities triggered late score worse on reCAPTCHA v3).
        #    Retryable — raise CamsRateLimited so the wrapper backs off and
        #    re-submits on a fresh browser profile.
        #
        #  • Password-only rejection — password signals WITHOUT any email-required
        #    signal: the other fields kept their values and only the password was
        #    flagged, i.e. a genuinely bad password. _random_pdf_password generates
        #    a CAMS-compliant password (≥2 digits, alnum only) so this should never
        #    happen; if it does, retrying won't help — hard failure.
        email_reset_signals = [
            "please enter the email",
            "email is required",
        ]
        password_reset_signals = [
            "password is required",
            "confirm password is required",
            "password should contain atleast",
            "password may contain only",
        ]
        matched = [s for s in (email_reset_signals + password_reset_signals)
                   if s in content]
        if matched:
            whole_form_reset = any(s in content for s in email_reset_signals)
            if whole_form_reset:
                logger.error(
                    f"CAMS form was reset after Submit — validation errors {matched}. "
                    "Whole form cleared: transient reCAPTCHA/session reset. "
                    "Will back off and retry on a fresh browser profile."
                )
                raise CamsRateLimited(
                    "CAMS form reset (transient reCAPTCHA/session reset)"
                )
            logger.error(
                f"CAMS form was reset after Submit — validation errors {matched}. "
                "Likely cause: invalid PDF password. Hard failure — will not wait for PDF."
            )
            return False

        # No clear signal — ambiguous. Log a warning and assume submitted.
        logger.warning(
            "Could not confirm CAMS submission result from page content. "
            "Treating as submitted — monitor the inbox."
        )
        return True

    except CamsRateLimited:
        # Must escape the catch-all below — the retry wrapper needs this to
        # back off and re-submit on a fresh browser profile.
        raise
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


def _type_into_date_input(loc, value: str, page):
    """Fill a read-only Angular Material date picker input.

    Angular Material sets readonly on date inputs to block browser autocomplete UI.
    We remove the attribute via JS, then use page.keyboard (bypasses Playwright's
    editability guard) and fire Tab/blur so the DateAdapter parses the new string.
    """
    try:
        page.evaluate("el => el.removeAttribute('readonly')", loc.element_handle())
    except Exception:
        pass
    loc.click(force=True)  # focus the element
    page.wait_for_timeout(random.randint(100, 250))
    page.keyboard.press("Control+a")
    page.wait_for_timeout(30)
    page.keyboard.type(value, delay=random.randint(50, 100))
    # Tab fires blur → Angular DateAdapter parses the typed string and updates the control
    page.keyboard.press("Tab")
    page.wait_for_timeout(200)


_SHORT_TIMEOUT = 3_000  # ms — used for date-field probing


def _fill_date_field(page, selectors: list, value: str, label: str):
    """Fill a date input, clearing existing value first (date pickers retain stale text)."""
    # Primary: Playwright label-based selection (matches Angular Material aria-labelledby)
    for label_variant in [label, label.upper(), label.title()]:
        try:
            loc = page.get_by_label(label_variant, exact=True).first
            if loc.count() == 0:
                continue
            loc.wait_for(state="visible", timeout=_SHORT_TIMEOUT)
            _type_into_date_input(loc, value, page)
            logger.debug(f"Filled date '{label}' via get_by_label('{label_variant}')")
            return
        except Exception:
            continue

    # Secondary: CSS selector list (short timeout — we're probing)
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.wait_for(state="visible", timeout=_SHORT_TIMEOUT)
            _type_into_date_input(loc, value, page)
            logger.debug(f"Filled date '{label}' via: {sel}")
            return
        except Exception:
            continue

    # Dump all inputs to help diagnose selector mismatches
    try:
        all_inputs = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('input')).map(el => ({
                type:             el.type,
                value:            el.value.substring(0, 30),
                readOnly:         el.readOnly,
                formcontrolname:  el.getAttribute('formcontrolname'),
                ngReflect:        el.getAttribute('ng-reflect-name'),
                ariaLabel:        el.getAttribute('aria-label'),
                ariaLabelledBy:   el.getAttribute('aria-labelledby'),
                labelledByText:   (() => { const id = el.getAttribute('aria-labelledby'); return id ? (document.getElementById(id)||{}).textContent||'' : ''; })(),
                placeholder:      el.placeholder,
                id:               el.id,
                classList:        el.className.substring(0, 80),
                visible:          el.offsetParent !== null,
            }));
        }""")
        logger.warning(f"DOM input dump for '{label}': {all_inputs}")
    except Exception as dump_err:
        logger.warning(f"Could not dump inputs: {dump_err}")

    raise RuntimeError(
        f"Could not find date field '{label}'. CAMS may have changed their form. "
        f"Tried get_by_label variants + selectors: {selectors}"
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
        _os.chmod(_SCREENSHOT_DIR, 0o700)
        path = f"{_SCREENSHOT_DIR}/{filename}"
        try:
            page.screenshot(path=path, timeout=15_000)
        except Exception as e1:
            logger.warning(f"Screenshot attempt 1 failed ({filename}): {e1}")
            # Fallback: capture just the viewport without full-page rendering
            try:
                page.screenshot(path=path, full_page=False, timeout=15_000)
            except Exception as e2:
                logger.warning(f"Screenshot attempt 2 failed ({filename}): {e2}")
                # Last resort: CDP direct capture
                try:
                    cdp = page.context.new_cdp_session(page)
                    data = cdp.send("Page.captureScreenshot", {"format": "png", "fromSurface": False})
                    import base64 as _b64
                    with open(path, "wb") as f:
                        f.write(_b64.b64decode(data["data"]))
                    cdp.detach()
                except Exception as e3:
                    logger.error(f"All screenshot methods failed ({filename}): e1={e1} e2={e2} e3={e3}")
                    return
        _os.chmod(path, 0o600)
        logger.info(f"Screenshot saved: {filename}")
    except Exception as _e:
        logger.warning(f"Screenshot save failed ({filename}): {_e}")
