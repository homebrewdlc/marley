"""
canvas.py — Canvas LMS integration for Marley.

Handles:
  - Local config storage (Canvas URL + credentials)
  - Session cookie caching via visible-browser login
  - Fetching assignments, grades, and assignment details
  - Works with any Canvas LMS instance
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


def save_canvas_setup(canvas_url: str) -> dict:
    """Save Canvas URL to local config."""
    # Normalize URL
    canvas_url = canvas_url.rstrip("/")
    if not canvas_url.startswith("http"):
        canvas_url = "https://" + canvas_url

    config = _load_config()
    config["canvas_url"] = canvas_url
    _save_config(config)
    clear_cookies()  # force fresh login with new URL
    return {"status": "saved", "canvas_url": canvas_url, "message": f"Canvas configured for {canvas_url}. Say 'log into Canvas' to open the login browser."}


def get_canvas_url() -> str | None:
    return _load_config().get("canvas_url")


def _canvas_url() -> str:
    """Return configured Canvas URL or a safe fallback for checks."""
    return get_canvas_url() or ""


def is_configured() -> bool:
    config = _load_config()
    return bool(config.get("canvas_url"))

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


# ── Visible-browser login ─────────────────────────────────
# Opens a real Chromium window so the user can log in manually.
# Marley watches for the URL to land on Canvas, grabs cookies, done.

_auth_lock = threading.Lock()
_auth_state = {"status": "idle"}
_login_result_q = queue.Queue(maxsize=1)


def get_auth_status() -> dict:
    with _auth_lock:
        return dict(_auth_state)


def _auth_set(**kwargs) -> dict:
    with _auth_lock:
        _auth_state.clear()
        _auth_state.update(kwargs)
    return dict(kwargs)


def start_canvas_login() -> dict:
    """Open a visible browser for the user to log into Canvas manually."""
    canvas_url = get_canvas_url()
    if not canvas_url:
        return _auth_set(status="error", message="Canvas is not configured. Tell me your school's Canvas URL first.")

    # Flush any previous result
    while not _login_result_q.empty():
        try:
            _login_result_q.get_nowait()
        except queue.Empty:
            break

    _auth_set(status="pending", message="Opening browser — log into Canvas and I'll take it from there.")
    threading.Thread(target=_visible_login_thread, args=(canvas_url,), daemon=True).start()

    # Wait up to 5 minutes for the user to complete login
    try:
        return _login_result_q.get(timeout=300)
    except queue.Empty:
        return _auth_set(status="error", message="Login timed out after 5 minutes. Try again when you're ready.")


def _visible_login_thread(canvas_url: str):
    """Launch visible Chromium, wait for user to log in, capture cookies."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result = _auth_set(status="error", message="Playwright not installed. Run: pip install playwright && playwright install chromium")
        _safe_put(_login_result_q, result)
        return

    pw = browser = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = context.new_page()
        page.goto(canvas_url, wait_until="load", timeout=30_000)

        # Already logged in?
        if canvas_url in page.url:
            cookies = _extract_cookies(page, canvas_url)
            if cookies:
                _save_cookies(cookies)
                _safe_put(_login_result_q, _auth_set(
                    status="success",
                    message=f"Already logged in. Session cached for {COOKIE_TTL // 3600} hours.",
                ))
                return

        # Poll until the URL lands on Canvas (user is logging in manually)
        print("[canvas-auth] Browser open — waiting for user to complete login...", flush=True)
        for _ in range(600):  # poll for up to 5 min (every 0.5s)
            time.sleep(0.5)
            try:
                current_url = page.url
            except Exception:
                # Browser was closed by user
                _safe_put(_login_result_q, _auth_set(
                    status="error", message="Browser was closed before login completed.",
                ))
                return

            if canvas_url in current_url:
                cookies = _extract_cookies(page, canvas_url)
                if cookies:
                    _save_cookies(cookies)
                    print("[canvas-auth] Login successful, cookies captured.", flush=True)
                    _safe_put(_login_result_q, _auth_set(
                        status="success",
                        message=f"Logged into Canvas. Session cached for {COOKIE_TTL // 3600} hours.",
                    ))
                    return

        _safe_put(_login_result_q, _auth_set(
            status="error", message="Login timed out. Try again when you're ready.",
        ))

    except Exception as e:
        _safe_put(_login_result_q, _auth_set(status="error", message=str(e)))
    finally:
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


def _extract_cookies(page, canvas_url: str) -> dict | None:
    """Pull Canvas session cookies from the browser context."""
    try:
        if canvas_url not in page.url:
            page.goto(canvas_url, wait_until="load", timeout=15_000)
        cookies = page.context.cookies()
        domain = canvas_url.split("//")[1].split("/")[0]
        cookie_dict = {c["name"]: c["value"] for c in cookies if domain in c.get("domain", "")}
        return cookie_dict if cookie_dict else None
    except Exception:
        return None


def _safe_put(q: queue.Queue, item):
    try:
        q.put_nowait(item)
    except queue.Full:
        pass


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
