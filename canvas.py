"""
canvas.py — Canvas LMS integration for Marley.

Handles:
  - Local config storage (Canvas URL + credentials per user)
  - Session cookie caching + auto-reauth via Playwright SSO
  - Fetching assignments, grades, and assignment details
  - Works with any Canvas LMS instance (Microsoft SSO + 2FA)
"""

import os
import re
import time
import json
import threading
import queue
from pathlib import Path
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

DAYS_AHEAD = int(os.getenv("CANVAS_DAYS_AHEAD", 14))
DISPLAY_TZ = ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))

# ── Local config (stored per-user, not in repo) ────────
CONFIG_FILE = Path(__file__).parent / ".canvas_config.json"


def _load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_config(config: dict):
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def save_canvas_setup(canvas_url: str, email: str, password: str) -> dict:
    """Save Canvas credentials to local config (not committed to git)."""
    # Normalize URL
    canvas_url = canvas_url.rstrip("/")
    if not canvas_url.startswith("http"):
        canvas_url = "https://" + canvas_url

    config = _load_config()
    config["canvas_url"] = canvas_url
    config["email"] = email
    config["password"] = password
    _save_config(config)
    clear_cookies()  # force fresh login with new creds
    return {"status": "saved", "canvas_url": canvas_url, "message": f"Canvas configured for {canvas_url}. Credentials stored locally."}


def get_canvas_url() -> str | None:
    return _load_config().get("canvas_url")


def _canvas_url() -> str:
    """Return configured Canvas URL or a safe fallback for checks."""
    return get_canvas_url() or ""


def is_configured() -> bool:
    config = _load_config()
    return bool(config.get("canvas_url") and config.get("email") and config.get("password"))

# ── Cookie cache ────────────────────────────────────────
_cookie_lock = threading.Lock()
_cached_cookies: dict | None = None
_cookie_expiry: float = 0  # unix timestamp
COOKIE_TTL = 3600 * 4  # 4 hours — Canvas sessions last longer, but be safe

COOKIE_FILE = Path(__file__).parent / ".canvas_cookies.json"


def _save_cookies(cookies: dict):
    global _cached_cookies, _cookie_expiry
    with _cookie_lock:
        _cached_cookies = cookies
        _cookie_expiry = time.time() + COOKIE_TTL
    try:
        COOKIE_FILE.write_text(json.dumps({
            "cookies": cookies,
            "expiry": _cookie_expiry,
        }))
    except Exception:
        pass


def _load_cookies() -> dict | None:
    global _cached_cookies, _cookie_expiry
    with _cookie_lock:
        if _cached_cookies and time.time() < _cookie_expiry:
            return _cached_cookies

    # Try disk cache
    try:
        if COOKIE_FILE.exists():
            data = json.loads(COOKIE_FILE.read_text())
            if time.time() < data.get("expiry", 0):
                with _cookie_lock:
                    _cached_cookies = data["cookies"]
                    _cookie_expiry = data["expiry"]
                return _cached_cookies
    except Exception:
        pass
    return None


def clear_cookies():
    """Force re-login on next Canvas call."""
    global _cached_cookies, _cookie_expiry
    with _cookie_lock:
        _cached_cookies = None
        _cookie_expiry = 0
    try:
        COOKIE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def has_valid_session() -> bool:
    return _load_cookies() is not None


# ── Playwright SSO login ────────────────────────────────
# Runs in a dedicated thread (Playwright requirement).

_auth_lock = threading.Lock()
_auth_state = {"status": "idle"}
_init_q = queue.Queue(maxsize=1)
_code_q = queue.Queue(maxsize=1)
_final_q = queue.Queue(maxsize=1)


def get_auth_status() -> dict:
    with _auth_lock:
        return dict(_auth_state)


def _auth_set(**kwargs) -> dict:
    with _auth_lock:
        _auth_state.clear()
        _auth_state.update(kwargs)
    return dict(kwargs)


def start_canvas_login(email: str = None, password: str = None) -> dict:
    """Kick off SSO login. Blocks until 2FA detection (~60s max)."""
    config = _load_config()
    email = email or config.get("email", "")
    password = password or config.get("password", "")
    if not email or not password:
        return _auth_set(status="error", message="Canvas not configured. Tell me your school's Canvas URL, email, and password to set it up.")

    _flush_queues()
    _auth_set(status="pending")
    threading.Thread(target=_login_thread, args=(email, password), daemon=True).start()
    try:
        return _init_q.get(timeout=60)
    except queue.Empty:
        return _auth_set(status="error", message="Login timed out.")


def submit_2fa_code(code: str) -> dict:
    """Send 2FA code to the login thread."""
    try:
        _code_q.put_nowait(code)
    except queue.Full:
        return _auth_set(status="error", message="No active login session.")
    try:
        return _final_q.get(timeout=30)
    except queue.Empty:
        return _auth_set(status="error", message="Timed out after code submission.")


def _login_thread(email: str, password: str):
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        _auth_set(status="error", message="Playwright not installed. Run: pip install playwright && playwright install chromium")
        _safe_put(_init_q, get_auth_status())
        return

    pw = browser = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="en-US", timezone_id="America/New_York",
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        # Navigate to Canvas (redirects to Microsoft SSO)
        canvas_url = get_canvas_url() or "https://canvas.instructure.com"
        page.goto(canvas_url, wait_until="load", timeout=30_000)

        if _on_canvas(page):
            _init_q.put(_finish(page))
            return

        # Microsoft email step
        try:
            page.wait_for_selector('input[name="loginfmt"], input[type="email"]', timeout=15_000)
        except PlaywrightTimeout:
            _init_q.put(_auth_set(status="error", message=f"Could not reach Microsoft login. Page: {page.url}"))
            return

        page.fill('input[name="loginfmt"]', email)
        page.click('#idSIButton9')

        # Password step
        try:
            page.wait_for_selector('input[name="passwd"]', timeout=15_000)
        except PlaywrightTimeout:
            _init_q.put(_auth_set(status="error", message="Email step failed."))
            return

        if _has_error(page):
            _init_q.put(_auth_set(status="error", message=_error_text(page) or "Email not recognised."))
            return

        page.fill('input[name="passwd"]', password)
        page.click('#idSIButton9')
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeout:
            page.wait_for_load_state("load", timeout=10_000)

        if _on_canvas(page):
            _init_q.put(_finish(page))
            return

        # Wait for 2FA page to load
        try:
            page.wait_for_selector('div[data-value], input[name="otc"], input[name="code"], #idRichContext_DisplaySign', timeout=20_000)
        except PlaywrightTimeout:
            pass

        # Only treat as a hard error if the method picker is NOT present
        # (the "trouble verifying" message often appears alongside the picker as a soft warning)
        if _has_error(page) and not _is_method_picker(page):
            _init_q.put(_auth_set(status="error", message=_error_text(page) or "Incorrect password."))
            return

        is_picker = _is_method_picker(page)
        print(f"[canvas-auth] Method picker check: {is_picker}", flush=True)
        if is_picker:
            picked = _pick_otp_method(page)
            if picked:
                twofa = "code"
                print("[canvas-auth] Switched to OTP code entry", flush=True)
            else:
                twofa = _detect_2fa(page)
                print(f"[canvas-auth] OTP pick failed, fallback 2FA type: {twofa}", flush=True)
        else:
            twofa = _detect_2fa(page)
            print(f"[canvas-auth] 2FA type: {twofa}", flush=True)

        if twofa == "push":
            number = _get_display_number(page)
            _init_q.put(_auth_set(status="needs_push", number=number,
                                   message=f"Approve the sign-in on your phone. Number: {number}" if number else "Approve the sign-in on your phone."))
            _wait_for_push(page)
            return

        if twofa == "code":
            prompt = _get_2fa_prompt(page)
            _init_q.put(_auth_set(status="needs_code", message=prompt))
            try:
                code = _code_q.get(timeout=120)
            except queue.Empty:
                _final_q.put(_auth_set(status="error", message="Timed out waiting for code."))
                return

            filled = False
            for sel in ['#idTxtBx_SAOTCC_OTC', 'input[name="otc"]', 'input[name="code"]', 'input[autocomplete="one-time-code"]']:
                try:
                    page.wait_for_selector(sel, timeout=5_000)
                    page.fill(sel, code)
                    filled = True
                    break
                except PlaywrightTimeout:
                    continue

            if not filled:
                _final_q.put(_auth_set(status="error", message="Could not find the code input."))
                return

            page.locator('#idSubmit_SAOTCC_Continue, #idSIButton9, input[type="submit"]').first.click()
            page.wait_for_load_state("load", timeout=20_000)

            if _has_error(page):
                _final_q.put(_auth_set(status="error", message=_error_text(page) or "Invalid code."))
                return

            if _has_stay_signed_in(page):
                page.locator('#idBtn_Back, button:has-text("No"), input[value="No"]').first.click()
                page.wait_for_load_state("load", timeout=10_000)

            if not _on_canvas(page):
                try:
                    page.wait_for_url(f"*{_canvas_url()}*", timeout=15_000)
                except PlaywrightTimeout:
                    page.goto(_canvas_url(), wait_until="load", timeout=20_000)

            _final_q.put(_finish(page))
            return

        if _has_stay_signed_in(page):
            page.locator('#idBtn_Back, input[value="No"]').first.click()
            page.wait_for_load_state("load", timeout=10_000)
            if _on_canvas(page):
                _init_q.put(_finish(page))
                return

        _init_q.put(_auth_set(status="error", message=f"Unexpected page after login: {page.url}"))

    except Exception as e:
        result = _auth_set(status="error", message=str(e))
        _safe_put(_init_q, result)
        _safe_put(_final_q, result)
    finally:
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


def _wait_for_push(page):
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    for _ in range(150):  # up to 5 min
        time.sleep(2)
        try:
            if _on_canvas(page):
                _finish(page)
                return

            if _has_stay_signed_in(page):
                page.locator('#idBtn_Back, button:has-text("No"), input[value="No"]').first.click()
                continue

            url = page.url
            if _canvas_url() not in url and _is_microsoft(url):
                gone = page.locator('#idRichContext_DisplaySign, #displaySign, .displaySign').count() == 0
                if gone:
                    time.sleep(4)
                    if _has_stay_signed_in(page):
                        page.locator('#idBtn_Back, button:has-text("No"), input[value="No"]').first.click()
                        time.sleep(2)
                    try:
                        page.goto(_canvas_url(), wait_until="load", timeout=20_000)
                    except PlaywrightTimeout:
                        pass
                    _finish(page)
                    return
        except Exception as e:
            err = str(e).lower()
            if any(k in err for k in ["closed", "target", "destroyed"]):
                _auth_set(status="error", message="Browser session lost.")
                return

    _auth_set(status="error", message="Push notification timed out.")


def _finish(page) -> dict:
    cookies = _extract_cookies(page)
    if cookies:
        _save_cookies(cookies)
        return _auth_set(status="success", message=f"Logged into Canvas. Session cached for {COOKIE_TTL // 3600} hours.")
    return _auth_set(status="error", message="Logged in but could not extract session cookies.")


def _extract_cookies(page) -> dict | None:
    try:
        if _canvas_url() not in page.url:
            page.goto(_canvas_url(), wait_until="load", timeout=15_000)
        cookies = page.context.cookies()
        domain = _canvas_url().split("//")[1]
        cookie_dict = {c["name"]: c["value"] for c in cookies if domain in c.get("domain", "")}
        return cookie_dict if cookie_dict else None
    except Exception:
        return None


# ── 2FA helpers ─────────────────────────────────────────

def _detect_2fa(page) -> str | None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    number_sel = '#idRichContext_DisplaySign, #displaySign, .displaySign, [data-bind*="DisplaySign"], #idDiv_SAOTCC_DisplaySign'
    code_sel = 'input[name="otc"], input[name="code"], input[autocomplete="one-time-code"]'
    try:
        page.wait_for_selector(f'{number_sel}, {code_sel}', timeout=20_000)
    except PlaywrightTimeout:
        pass

    for sel in number_sel.split(', '):
        try:
            if page.locator(sel.strip()).count() > 0:
                return "push"
        except Exception:
            pass

    for sel in code_sel.split(', '):
        try:
            el = page.locator(sel.strip()).first
            if el.count() > 0 and el.is_visible():
                return "code"
        except Exception:
            pass

    html = page.content().lower()
    if any(k in html for k in ["enter the number shown", "approve sign in", "push notification", "number matching"]):
        return "push"
    if any(k in html for k in ["verification code", "enter the code", "one-time"]):
        return "code"
    return None


def _is_method_picker(page) -> bool:
    """Check if the page is a 2FA method selection screen."""
    try:
        count = page.locator('div[data-value="PhoneAppOTP"]').count()
        total = page.locator('div[data-value]').count()
        print(f"[canvas-auth] _is_method_picker: PhoneAppOTP={count}, total data-value divs={total}", flush=True)
        # It's a picker if there are multiple method options
        return total >= 2
    except Exception as e:
        print(f"[canvas-auth] _is_method_picker error: {e}", flush=True)
        return False


def _pick_otp_method(page) -> bool:
    """Click the OTP code option on the method picker page."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    try:
        otp_option = page.locator('div[data-value="PhoneAppOTP"]').first
        if otp_option.count() > 0 and otp_option.is_visible():
            otp_option.click()
            # Wait for the code input to appear — it loads dynamically
            try:
                page.wait_for_selector('#idTxtBx_SAOTCC_OTC, input[name="otc"]', timeout=10_000)
                return True
            except PlaywrightTimeout:
                pass
    except Exception as e:
        print(f"[canvas-auth] OTP pick error: {e}", flush=True)
    return False


def _get_display_number(page) -> str | None:
    for sel in ['#idRichContext_DisplaySign', '#displaySign', '.displaySign']:
        try:
            el = page.locator(sel).first
            if el.count() > 0:
                tag = el.evaluate("e => e.tagName").upper()
                text = (el.input_value() if tag == "INPUT" else el.text_content() or "").strip()
                if text:
                    return text
        except Exception:
            continue
    return None


def _get_2fa_prompt(page) -> str:
    for sel in [".text-title", "#idDiv_SAOTCS_Title", "#idDiv_SAOTCC_Title", "h1"]:
        try:
            el = page.locator(sel).first
            if el.count() > 0:
                text = el.text_content().strip()
                if text:
                    return text
        except Exception:
            pass
    return "Enter your verification code."


def _has_error(page) -> bool:
    try:
        err = page.locator('#idTd_Tile_ErrorMessage, .alert-error, [aria-live="assertive"]').first
        return err.count() > 0 and bool((err.text_content() or "").strip())
    except Exception:
        return False


def _error_text(page) -> str:
    try:
        return page.locator('#idTd_Tile_ErrorMessage, .alert-error, [aria-live="assertive"]').first.text_content().strip()
    except Exception:
        return ""


def _on_canvas(page) -> bool:
    try:
        return _canvas_url() in page.url
    except Exception:
        return False


def _is_microsoft(url: str) -> bool:
    return any(d in url for d in ["microsoftonline.com", "microsoft.com", "live.com"])


def _has_stay_signed_in(page) -> bool:
    try:
        return page.locator('#idBtn_Back, input[value="No"]').count() > 0
    except Exception:
        return False


def _safe_put(q: queue.Queue, item):
    try:
        q.put_nowait(item)
    except queue.Full:
        pass


def _flush_queues():
    for q in (_init_q, _code_q, _final_q):
        while not q.empty():
            try:
                q.get_nowait()
            except queue.Empty:
                break


# ── Canvas API calls ────────────────────────────────────

def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, 23, 59, tzinfo=timezone.utc)
    if isinstance(value, str):
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(value.rstrip("Z"), fmt.rstrip("Z"))
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _api_get(path: str, params: dict = None, cookies: dict = None) -> list | dict:
    if not is_configured():
        raise ValueError("Canvas not configured. Tell me your school's Canvas URL, email, and password to set it up.")
    cookies = cookies or _load_cookies()
    if not cookies:
        raise ValueError("Not logged into Canvas. Say 'log into Canvas' to authenticate.")
    resp = requests.get(f"{_canvas_url()}{path}", params=params or {}, cookies=cookies, timeout=15)
    if resp.status_code == 401:
        clear_cookies()
        raise ValueError("Canvas session expired. Say 'log into Canvas' to re-authenticate.")
    resp.raise_for_status()
    return resp.json()


def get_assignments() -> list[dict]:
    """Fetch assignments due in the next DAYS_AHEAD days."""
    cookies = _load_cookies()
    if not cookies:
        raise ValueError("Not logged into Canvas. Say 'log into Canvas' to authenticate.")

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=DAYS_AHEAD)
    now_local = now.astimezone(DISPLAY_TZ)

    courses = _api_get("/api/v1/courses", {"enrollment_state": "active", "per_page": 50}, cookies)
    if not isinstance(courses, list):
        return []

    assignments = []
    for course in courses:
        if not isinstance(course, dict) or "id" not in course:
            continue
        course_id = course["id"]
        course_name = course.get("name") or course.get("course_code") or "Unknown"

        try:
            items = _api_get(f"/api/v1/courses/{course_id}/assignments",
                             {"bucket": "upcoming", "per_page": 50, "order_by": "due_at"}, cookies)
        except Exception:
            continue

        if not isinstance(items, list):
            continue

        for a in items:
            if not isinstance(a, dict):
                continue
            due_dt = _parse_dt(a.get("due_at"))
            if due_dt is None or not (now <= due_dt <= end):
                continue

            due_local = due_dt.astimezone(DISPLAY_TZ)
            assignments.append({
                "id": a.get("id"),
                "course": course_name,
                "title": a.get("name") or "Unnamed Assignment",
                "due": due_local.strftime("%A, %b %-d @ %-I:%M %p"),
                "days_left": (due_local.date() - now_local.date()).days,
                "points": a.get("points_possible", "?"),
                "description": _strip_html(str(a.get("description") or ""))[:500],
                "url": a.get("html_url") or "",
                "submission_types": a.get("submission_types", []),
            })

    assignments.sort(key=lambda x: x.get("days_left", 999))
    return assignments


def get_grades() -> list[dict]:
    """Fetch current grades for all active courses."""
    cookies = _load_cookies()
    if not cookies:
        raise ValueError("Not logged into Canvas. Say 'log into Canvas' to authenticate.")

    courses = _api_get("/api/v1/courses",
                       {"enrollment_state": "active", "include[]": "total_scores", "per_page": 50}, cookies)
    if not isinstance(courses, list):
        return []

    grades = []
    for course in courses:
        enrollment = next((e for e in course.get("enrollments", []) if e.get("type") == "student"), None)
        if not enrollment:
            continue
        grades.append({
            "course": course.get("name") or "Unknown",
            "score": enrollment.get("computed_current_score"),
            "grade": enrollment.get("computed_current_grade"),
        })

    grades.sort(key=lambda x: x["course"])
    return grades


def get_assignment_detail(search: str) -> dict | None:
    """Find a specific assignment by name (fuzzy match) and return full details."""
    assignments = get_assignments()
    if not assignments:
        return None

    search_lower = search.lower()
    # Try exact-ish match first, then substring
    for a in assignments:
        if search_lower in a["title"].lower() or search_lower in a["course"].lower():
            return a

    # Fuzzy: check if all words in search appear in title or course
    words = search_lower.split()
    for a in assignments:
        combined = f"{a['title']} {a['course']}".lower()
        if all(w in combined for w in words):
            return a

    return None


def get_assignment_content(assignment_id: int) -> dict:
    """Fetch full assignment content including description and file attachments.

    Returns the full description text and downloads any attached files,
    extracting text content from PDFs, docs, and text files.
    """
    cookies = _load_cookies()
    if not cookies:
        raise ValueError("Not logged into Canvas. Say 'log into Canvas' to authenticate.")

    # We need to find which course this assignment belongs to
    courses = _api_get("/api/v1/courses", {"enrollment_state": "active", "per_page": 50}, cookies)
    if not isinstance(courses, list):
        raise ValueError("Could not fetch courses.")

    assignment_data = None
    course_id = None

    for course in courses:
        if not isinstance(course, dict) or "id" not in course:
            continue
        try:
            a = _api_get(f"/api/v1/courses/{course['id']}/assignments/{assignment_id}", {}, cookies)
            if isinstance(a, dict) and a.get("id") == assignment_id:
                assignment_data = a
                course_id = course["id"]
                break
        except Exception:
            continue

    if not assignment_data:
        raise ValueError(f"Assignment {assignment_id} not found.")

    course_name = next(
        (c.get("name", "Unknown") for c in courses if c.get("id") == course_id),
        "Unknown",
    )

    # Full description (HTML → plain text)
    raw_desc = assignment_data.get("description") or ""
    description = _strip_html(raw_desc)

    result = {
        "id": assignment_id,
        "title": assignment_data.get("name", "Unnamed"),
        "course": course_name,
        "description": description,
        "points": assignment_data.get("points_possible", "?"),
        "due": assignment_data.get("due_at", ""),
        "submission_types": assignment_data.get("submission_types", []),
        "url": assignment_data.get("html_url", ""),
        "attachments": [],
    }

    # Fetch any file attachments on the assignment itself
    _fetch_attachments(assignment_data, result, cookies)

    return result


def _fetch_attachments(assignment_data: dict, result: dict, cookies: dict):
    """Download and extract text from assignment attachments."""
    import tempfile

    # Canvas can have attachments in the assignment description as links,
    # or via the rubric/external tools. Check for direct file references.
    # Also check for linked files in the description HTML.
    raw_desc = assignment_data.get("description") or ""

    # Find file download links in the description HTML
    file_urls = re.findall(
        r'href="([^"]*(?:/files/\d+|\.pdf|\.docx?|\.txt|\.rtf)[^"]*)"',
        raw_desc,
        re.IGNORECASE,
    )

    # Also check the Canvas files API for the course
    canvas_url = _canvas_url()

    for url in file_urls:
        # Make absolute
        if url.startswith("/"):
            url = canvas_url + url
        elif not url.startswith("http"):
            continue

        try:
            # Follow redirects to get the actual file
            resp = requests.get(url, cookies=cookies, timeout=30, allow_redirects=True)
            if resp.status_code != 200:
                result["attachments"].append({
                    "url": url,
                    "error": f"Download failed: HTTP {resp.status_code}",
                })
                continue

            content_type = resp.headers.get("content-type", "")
            filename = _extract_filename(resp, url)

            # Extract text based on file type
            text = ""
            if "pdf" in content_type or filename.lower().endswith(".pdf"):
                text = _extract_pdf_text(resp.content)
            elif filename.lower().endswith((".txt", ".rtf", ".csv", ".md")):
                text = resp.text[:10000]
            elif filename.lower().endswith((".doc", ".docx")):
                text = _extract_docx_text(resp.content)
            else:
                text = f"[File type not supported for text extraction: {content_type}]"

            result["attachments"].append({
                "filename": filename,
                "content": text[:8000],  # cap to avoid huge payloads
            })
        except Exception as e:
            result["attachments"].append({
                "url": url,
                "error": str(e),
            })


def _extract_filename(resp, url: str) -> str:
    """Get filename from Content-Disposition header or URL."""
    cd = resp.headers.get("content-disposition", "")
    match = re.search(r'filename="?([^";\n]+)"?', cd)
    if match:
        return match.group(1).strip()
    # Fallback: last segment of URL path
    from urllib.parse import urlparse, unquote
    path = urlparse(url).path
    return unquote(path.split("/")[-1]) or "unknown_file"


def _extract_pdf_text(content: bytes) -> str:
    """Extract text from PDF bytes."""
    try:
        import io
        # Try PyPDF2 / pypdf
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader

        reader = PdfReader(io.BytesIO(content))
        text = ""
        for page in reader.pages[:20]:  # cap at 20 pages
            text += page.extract_text() or ""
            text += "\n"
        return text.strip() or "[PDF contained no extractable text]"
    except ImportError:
        return "[PDF reader not installed — run: pip install pypdf]"
    except Exception as e:
        return f"[PDF extraction failed: {e}]"


def _extract_docx_text(content: bytes) -> str:
    """Extract text from DOCX bytes."""
    try:
        import io
        import zipfile
        import xml.etree.ElementTree as ET

        # DOCX is a zip containing XML
        z = zipfile.ZipFile(io.BytesIO(content))
        xml_content = z.read("word/document.xml")
        tree = ET.fromstring(xml_content)

        # Extract all text from w:t elements
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        texts = [t.text for t in tree.iter(f"{{{ns['w']}}}t") if t.text]
        return " ".join(texts).strip() or "[DOCX contained no extractable text]"
    except Exception as e:
        return f"[DOCX extraction failed: {e}]"
