"""
MARLEY — Your personal AI assistant with full system access.
FastAPI backend powered by Claude with web search + local tools.
"""
import os
import json
import uuid
import re
import sqlite3
import subprocess
import asyncio
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
import anthropic
import httpx

from canvas import (
    get_assignments, get_grades, get_assignment_detail,
    start_canvas_login, submit_2fa_code, get_auth_status,
    has_valid_session, clear_cookies, is_configured,
    save_canvas_setup, get_canvas_url,
)

load_dotenv()

app = FastAPI(title="MARLEY")
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# In-memory conversation sessions
sessions: dict[str, list] = {}

# ── Brain repo (for conversation logs) ───────────────────
BRAIN_REPO = Path.home() / "claude_brain"
CONVOS_DIR = BRAIN_REPO / "claude_brain" / "Conversations"
CONVOS_DIR.mkdir(parents=True, exist_ok=True)

END_PHRASES = re.compile(
    r"we'?re good,?\s*marley|that'?s all,?\s*marley|good night,?\s*marley|end session",
    re.IGNORECASE,
)


def save_and_push_conversation(messages: list) -> str:
    """Save conversation to markdown and push to GitHub."""
    now = datetime.now()
    filename = now.strftime("%Y-%m-%d_%H-%M") + ".md"
    filepath = CONVOS_DIR / filename

    # Build markdown
    lines = [
        f"# Marley Conversation — {now.strftime('%B %d, %Y %I:%M %p')}",
        "",
        "---",
        "",
    ]

    for msg in messages:
        role = msg["role"].upper()
        content = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
        # Skip tool results
        if isinstance(msg.get("content"), list):
            continue
        label = "DEVIN" if role == "USER" else "MARLEY"
        lines.append(f"**{label}:** {content}")
        lines.append("")

    filepath.write_text("\n".join(lines))

    # Git commit and push
    try:
        subprocess.run(
            ["git", "add", str(filepath)],
            cwd=str(BRAIN_REPO), capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "commit", "-m", f"Marley conversation — {now.strftime('%Y-%m-%d %H:%M')}"],
            cwd=str(BRAIN_REPO), capture_output=True, timeout=10,
        )
        result = subprocess.run(
            ["git", "push"],
            cwd=str(BRAIN_REPO), capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return f"Conversation saved and pushed to GitHub ({filename})"
        else:
            return f"Conversation saved locally ({filename}) but push failed: {result.stderr.strip()[:200]}"
    except Exception as e:
        return f"Conversation saved locally ({filename}) but git error: {e}"


# ── Database paths ───────────────────────────────────────
TRADING_DB = Path.home() / "trading_bot" / "trading_bot.db"
DISPATCH_DB = Path.home() / "dispatch_system" / "backend" / "dispatch.db"
YOUTUBE_DB = Path.home() / "youtube-auto" / "db" / "queue.db"

# ── Agent definitions ────────────────────────────────────
AGENTS = {
    "trading_bot": {
        "name": "Trading Bot",
        "dir": str(Path.home() / "trading_bot"),
        "start": "marley",
        "stop": "marley stop",
        "check": "pgrep -f 'trading_bot/main.py'",
        "port": None,
        "description": "Alpaca stock trading bot — EMA/RSI crossover strategy",
    },
    "trading_dashboard": {
        "name": "Trading Dashboard",
        "dir": str(Path.home() / "trading_bot"),
        "start": "marley",
        "stop": "marley stop",
        "check": "ss -tlnp | grep ':5050'",
        "port": 5050,
        "description": "Web dashboard for the trading bot — positions, trades, P&L",
    },
    "dispatch_system": {
        "name": "Dispatch System",
        "dir": str(Path.home() / "dispatch_system" / "backend"),
        "start": "cd ~/dispatch_system/backend && source venv/bin/activate && uvicorn app.main:app --host 0.0.0.0 --port 8000 &",
        "stop": "pkill -f 'dispatch_system.*uvicorn'",
        "check": "ss -tlnp | grep ':8000'",
        "port": 8000,
        "description": "Death investigation dispatch platform — SMS intake, worker notifications",
    },
    "youtube_auto": {
        "name": "YouTube Auto",
        "dir": str(Path.home() / "youtube-auto"),
        "start": "systemctl --user start youtube-auto",
        "stop": "systemctl --user stop youtube-auto",
        "check": "systemctl --user is-active youtube-auto",
        "port": None,
        "description": "Podcast clip bot — downloads, transcribes, clips, uploads YouTube Shorts",
    },
    "agent_hq": {
        "name": "Agent HQ",
        "dir": str(Path.home() / "agent-hq"),
        "start": "cd ~/agent-hq && source venv/bin/activate && python app.py &",
        "stop": "pkill -f 'agent-hq.*app.py'",
        "check": "ss -tlnp | grep ':6060'",
        "port": 6060,
        "description": "Command center dashboard — monitors and controls all agents",
    },
}

# ── Tool definitions for Claude ──────────────────────────
TOOLS = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 5,
    },
    {
        "name": "check_portfolio",
        "description": "Check the trading bot's portfolio — current equity, cash, positions, P&L, and recent trades. Use this when the user asks about the trader, portfolio, stocks, positions, or money.",
        "input_schema": {
            "type": "object",
            "properties": {
                "detail": {
                    "type": "string",
                    "enum": ["summary", "positions", "recent_trades", "top_winners", "full"],
                    "description": "Level of detail: 'summary' for quick equity/cash snapshot, 'positions' for open positions, 'recent_trades' for last 10 trades, 'top_winners' for best/worst performers, 'full' for everything",
                }
            },
            "required": ["detail"],
        },
    },
    {
        "name": "check_agents",
        "description": "Check the status of all agents/services running on this machine (trading bot, dispatch system, youtube auto, agent hq). Use when the user asks about agents, services, or what's running.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "manage_agent",
        "description": "Start or stop an agent/service. Use when the user asks to start, stop, or restart a service.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": list(AGENTS.keys()),
                    "description": "Which agent to manage",
                },
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "restart"],
                    "description": "Action to perform",
                },
            },
            "required": ["agent", "action"],
        },
    },
    {
        "name": "check_dispatches",
        "description": "Check the dispatch system for open/recent dispatches, job counts, and worker info. Use when user asks about dispatches, jobs, or workers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "enum": ["open", "today", "stats", "workers"],
                    "description": "'open' for current open dispatches, 'today' for today's count, 'stats' for overall stats, 'workers' for active worker info",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_youtube",
        "description": "Check the YouTube Auto podcast clipper bot status — uploads, failures, recent clips. Use when user asks about YouTube, videos, clips, or uploads.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "enum": ["stats", "recent", "today", "channels"],
                    "description": "'stats' for overall counts, 'recent' for last 10 uploads, 'today' for today's activity, 'channels' for breakdown by channel",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_canvas",
        "description": "Check Canvas LMS for homework assignments, grades, or details about a specific assignment. Use when the user asks about homework, assignments, classes, grades, what's due, or anything school-related.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "enum": ["assignments", "grades", "detail"],
                    "description": "'assignments' for upcoming homework, 'grades' for current grades, 'detail' for info about a specific assignment",
                },
                "search": {
                    "type": "string",
                    "description": "For 'detail' query: assignment name or course to search for (e.g. 'CSCI homework 3' or 'research paper')",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "canvas_setup",
        "description": "Configure Canvas LMS for the first time. Use when the user provides their school's Canvas URL, email, and password. Stores credentials locally on their machine (never sent anywhere). Use this BEFORE canvas_login if Canvas is not yet configured.",
        "input_schema": {
            "type": "object",
            "properties": {
                "canvas_url": {
                    "type": "string",
                    "description": "The school's Canvas URL (e.g. 'https://school.instructure.com' or 'canvas.school.edu')",
                },
                "email": {
                    "type": "string",
                    "description": "The user's school email address",
                },
                "password": {
                    "type": "string",
                    "description": "The user's school password",
                },
            },
            "required": ["canvas_url", "email", "password"],
        },
    },
    {
        "name": "canvas_login",
        "description": "Log into Canvas LMS via SSO. Triggers a 2FA push notification to the user's phone. Use when Canvas session has expired or the user asks to log into Canvas.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command on the local machine. Use for system checks, file operations, or launching tools like Claude Code. Be careful with destructive commands.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run",
                },
                "background": {
                    "type": "boolean",
                    "description": "Run in background (for long-running processes like Claude Code). Default false.",
                },
            },
            "required": ["command"],
        },
    },
]


# ── Tool implementations ─────────────────────────────────

def query_db(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    """Run a SQL query and return results as list of dicts."""
    if not db_path.exists():
        return [{"error": f"Database not found: {db_path}"}]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        conn.close()


def tool_check_portfolio(detail: str) -> str:
    results = {}

    if detail in ("summary", "full"):
        snap = query_db(TRADING_DB, "SELECT equity, cash, recorded_at FROM equity_history ORDER BY id DESC LIMIT 1")
        start = query_db(TRADING_DB, "SELECT equity FROM equity_history ORDER BY id ASC LIMIT 1")
        results["current"] = snap[0] if snap else "No data"
        if snap and start and not isinstance(start[0], dict) or (start and "equity" in start[0]):
            try:
                pct = round((snap[0]["equity"] - start[0]["equity"]) / start[0]["equity"] * 100, 2)
                results["total_return_pct"] = pct
                results["starting_equity"] = start[0]["equity"]
            except (KeyError, TypeError, ZeroDivisionError):
                pass

    if detail in ("positions", "full"):
        results["positions"] = query_db(TRADING_DB, """
            SELECT symbol,
                SUM(CASE WHEN side='buy' THEN qty ELSE -qty END) AS shares,
                ROUND(AVG(CASE WHEN side='buy' THEN price END), 2) AS avg_entry
            FROM trades WHERE status='executed'
            GROUP BY symbol HAVING shares > 0
            ORDER BY shares * avg_entry DESC
        """)

    if detail in ("recent_trades", "full"):
        results["recent_trades"] = query_db(TRADING_DB, """
            SELECT symbol, side, qty, ROUND(price, 2) as price,
                   ROUND(qty * price, 2) AS notional, status, created_at
            FROM trades WHERE status='executed'
            ORDER BY created_at DESC LIMIT 10
        """)

    if detail in ("top_winners", "full"):
        results["realized_pnl"] = query_db(TRADING_DB, """
            SELECT symbol,
                ROUND(SUM(CASE WHEN side='sell' THEN qty*price ELSE -qty*price END), 2) AS realized_pnl
            FROM trades WHERE status='executed'
            GROUP BY symbol ORDER BY realized_pnl DESC
        """)

    activity = query_db(TRADING_DB, """
        SELECT event_type, message, symbol, created_at FROM activity
        ORDER BY id DESC LIMIT 5
    """)
    results["recent_activity"] = activity

    return json.dumps(results, default=str)


def tool_check_agents() -> str:
    statuses = {}
    for key, agent in AGENTS.items():
        try:
            result = subprocess.run(
                agent["check"], shell=True, capture_output=True, text=True, timeout=5
            )
            running = result.returncode == 0 and result.stdout.strip() != ""
            # Special case for systemctl
            if "systemctl" in agent["check"]:
                running = "active" in result.stdout and "inactive" not in result.stdout
            statuses[key] = {
                "name": agent["name"],
                "status": "RUNNING" if running else "STOPPED",
                "port": agent["port"],
                "description": agent["description"],
            }
        except Exception as e:
            statuses[key] = {
                "name": agent["name"],
                "status": f"ERROR: {e}",
                "port": agent["port"],
            }
    return json.dumps(statuses)


def tool_manage_agent(agent: str, action: str) -> str:
    if agent not in AGENTS:
        return json.dumps({"error": f"Unknown agent: {agent}"})

    info = AGENTS[agent]
    try:
        if action == "start":
            cmd = info["start"]
        elif action == "stop":
            cmd = info["stop"]
        elif action == "restart":
            cmd = info["stop"] + " ; sleep 2 ; " + info["start"]
        else:
            return json.dumps({"error": f"Unknown action: {action}"})

        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        return json.dumps({
            "agent": info["name"],
            "action": action,
            "success": result.returncode == 0,
            "output": (result.stdout + result.stderr).strip()[:500],
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_check_dispatches(query: str) -> str:
    if query == "open":
        rows = query_db(DISPATCH_DB, """
            SELECT id, case_number, deceased_name, investigator_name, address,
                   slots_needed, slots_filled, created_at
            FROM dispatches WHERE status='open' ORDER BY created_at DESC
        """)
        return json.dumps({"open_dispatches": rows, "count": len(rows)}, default=str)

    elif query == "today":
        rows = query_db(DISPATCH_DB, """
            SELECT status, COUNT(*) as count FROM dispatches
            WHERE date(created_at) = date('now') GROUP BY status
        """)
        total = query_db(DISPATCH_DB, "SELECT COUNT(*) as total FROM dispatches WHERE date(created_at) = date('now')")
        return json.dumps({"today": rows, "total": total[0]["total"] if total else 0}, default=str)

    elif query == "stats":
        total = query_db(DISPATCH_DB, "SELECT status, COUNT(*) as count FROM dispatches GROUP BY status")
        workers = query_db(DISPATCH_DB, "SELECT COUNT(*) as count FROM users WHERE role='worker' AND phone_verified=1")
        return json.dumps({"all_time": total, "verified_workers": workers[0]["count"] if workers else 0}, default=str)

    elif query == "workers":
        workers = query_db(DISPATCH_DB, """
            SELECT u.name, u.phone,
                SUM(CASE WHEN dc.status='accepted' THEN 1 ELSE 0 END) AS accepted,
                SUM(CASE WHEN dc.status='declined' THEN 1 ELSE 0 END) AS declined
            FROM users u LEFT JOIN dispatch_candidates dc ON dc.user_id = u.id
            WHERE u.role='worker' AND u.phone_verified=1
            GROUP BY u.id ORDER BY accepted DESC
        """)
        return json.dumps({"workers": workers}, default=str)

    return json.dumps({"error": "Unknown query type"})


def tool_check_youtube(query: str) -> str:
    if query == "stats":
        rows = query_db(YOUTUBE_DB, "SELECT status, COUNT(*) as count FROM posts GROUP BY status")
        uploaded = query_db(YOUTUBE_DB, "SELECT COUNT(*) as count FROM posts WHERE status='posted' AND youtube_id != 'local'")
        return json.dumps({
            "breakdown": rows,
            "uploaded_to_youtube": uploaded[0]["count"] if uploaded else 0,
        }, default=str)

    elif query == "recent":
        rows = query_db(YOUTUBE_DB, """
            SELECT title, subreddit as channel, youtube_id, posted_at
            FROM posts WHERE status='posted'
            ORDER BY posted_at DESC LIMIT 10
        """)
        return json.dumps({"recent_uploads": rows}, default=str)

    elif query == "today":
        rows = query_db(YOUTUBE_DB, """
            SELECT title, subreddit as channel, youtube_id, posted_at
            FROM posts WHERE date(posted_at) = date('now')
        """)
        return json.dumps({"today": rows, "count": len(rows)}, default=str)

    elif query == "channels":
        rows = query_db(YOUTUBE_DB, """
            SELECT subreddit as channel, COUNT(*) as clips,
                SUM(CASE WHEN status='posted' THEN 1 ELSE 0 END) as posted,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
            FROM posts GROUP BY subreddit ORDER BY clips DESC
        """)
        return json.dumps({"channels": rows}, default=str)

    return json.dumps({"error": "Unknown query type"})


def tool_run_command(command: str, background: bool = False) -> str:
    try:
        if background:
            process = subprocess.Popen(
                command, shell=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return json.dumps({
                "status": "launched_in_background",
                "pid": process.pid,
                "command": command,
            })
        else:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=30
            )
            return json.dumps({
                "exit_code": result.returncode,
                "stdout": result.stdout.strip()[:2000],
                "stderr": result.stderr.strip()[:500],
            })
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Command timed out after 30 seconds"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_check_canvas(query: str, search: str = "") -> str:
    try:
        if query == "assignments":
            assignments = get_assignments()
            if not assignments:
                return json.dumps({"assignments": [], "message": "No assignments due in the next 14 days."})
            return json.dumps({"assignments": assignments, "count": len(assignments)}, default=str)

        elif query == "grades":
            grades = get_grades()
            if not grades:
                return json.dumps({"grades": [], "message": "No grade data available."})
            return json.dumps({"grades": grades}, default=str)

        elif query == "detail":
            if not search:
                return json.dumps({"error": "Need a search term to find a specific assignment."})
            result = get_assignment_detail(search)
            if not result:
                return json.dumps({"error": f"No assignment found matching '{search}'.", "suggestion": "Try checking all assignments first."})
            return json.dumps({"assignment": result}, default=str)

        return json.dumps({"error": f"Unknown query: {query}"})
    except ValueError as e:
        return json.dumps({"error": str(e)})


def tool_canvas_setup(canvas_url: str, email: str, password: str) -> str:
    result = save_canvas_setup(canvas_url, email, password)
    return json.dumps(result)


def tool_canvas_login() -> str:
    if not is_configured():
        return json.dumps({"status": "not_configured", "message": "Canvas is not set up yet. I need your school's Canvas URL, email, and password first."})
    if has_valid_session():
        return json.dumps({"status": "already_authenticated", "message": "Canvas session is still valid."})
    result = start_canvas_login()
    return json.dumps(result)


def execute_tool(name: str, input_data: dict) -> str:
    """Route tool calls to implementations."""
    if name == "check_portfolio":
        return tool_check_portfolio(input_data.get("detail", "summary"))
    elif name == "check_agents":
        return tool_check_agents()
    elif name == "manage_agent":
        return tool_manage_agent(input_data["agent"], input_data["action"])
    elif name == "check_dispatches":
        return tool_check_dispatches(input_data["query"])
    elif name == "check_youtube":
        return tool_check_youtube(input_data["query"])
    elif name == "check_canvas":
        return tool_check_canvas(input_data.get("query", "assignments"), input_data.get("search", ""))
    elif name == "canvas_setup":
        return tool_canvas_setup(input_data["canvas_url"], input_data["email"], input_data["password"])
    elif name == "canvas_login":
        return tool_canvas_login()
    elif name == "run_command":
        return tool_run_command(input_data["command"], input_data.get("background", False))
    return json.dumps({"error": f"Unknown tool: {name}"})


# ── System prompt ────────────────────────────────────────
SYSTEM_PROMPT = """You are MARLEY, a highly advanced personal AI assistant — like JARVIS or FRIDAY from the Marvel universe. You are loyal, sharp, and deeply integrated into your user's digital life.

Your user's name is Devin. You run on his Linux workstation (Fedora, Hyprland WM).

## Your personality
- Male persona — like JARVIS. Calm, composed, British-dry wit, deeply competent
- Never use emojis in responses. Ever.
- Direct and efficient — give concise answers unless detail is requested
- Proactive — if you notice something important in the data, mention it
- Conversational — this is a natural dialogue, not a Q&A bot
- Address the user as "Devin" or "sir" naturally, like JARVIS addresses Tony Stark
- When asked casually ("how's the trader doing?"), give a brief natural summary, not a data dump

## Your capabilities
You have access to these systems on this machine:

**Trading Bot** — Alpaca stock trading bot (EMA/RSI crossover strategy, paper trading). You can check portfolio equity, positions, P&L, and recent trades. The bot started with ~$100K.

**Dispatch System** — Death investigation dispatch platform. Workers get notified of jobs via Telegram/push. You can check open dispatches, job counts, and worker stats.

**YouTube Auto** — Podcast clip bot that downloads episodes, finds viral moments with AI, crops to 9:16 with subtitles, and uploads as YouTube Shorts.

**Agent HQ** — Command center dashboard that monitors all the other agents.

**Canvas LMS** — You can check homework assignments, grades, and details about specific assignments from Canvas. If Canvas isn't set up yet, ask for the school's Canvas URL, email, and password — then use canvas_setup to save it locally. If the session has expired, use canvas_login (the user will need to approve the 2FA push on their phone). Credentials are stored locally on this machine only, never sent anywhere else.

**Shell Access** — You can run shell commands for system checks, file operations, or launching tools. You can launch Claude Code (`claude -p "prompt"`) to build things.

## How to respond
- For casual questions about systems, use the appropriate tool and summarize naturally
- For homework/school questions, use check_canvas — summarize naturally, highlight urgency
- If Canvas returns "not configured", ask for the school's Canvas URL, email, and password, then use canvas_setup
- If Canvas returns "not logged in" or "session expired", do NOT automatically call canvas_login. Instead, tell the user their Canvas session needs to be refreshed and ask them to say "log into Canvas" when they are ready (they will need to approve a 2FA push on their phone). Only call canvas_login when the user explicitly asks to log in.
- For general knowledge or current events, use web search
- For building/coding requests, you can launch Claude Code with `run_command`
- Keep responses conversational unless the user wants detail
- You can see all agents, start/stop them, and check their health
- If the trading bot or any service is down, let Devin know proactively when he asks about it"""


ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/calendar")
async def calendar_assignments():
    """Return Canvas assignments for the calendar UI (no Claude needed)."""
    try:
        assignments = get_assignments()
        return {"assignments": assignments, "count": len(assignments)}
    except ValueError as e:
        return {"error": str(e), "assignments": []}
    except Exception as e:
        return {"error": f"Failed to fetch assignments: {e}", "assignments": []}


@app.post("/api/calendar/strategy")
async def calendar_strategy(request: Request):
    """Ask Claude for a weekly strategy based on current assignments."""
    try:
        assignments = get_assignments()
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to fetch assignments: {e}"}

    if not assignments:
        return {"strategy": "No upcoming assignments found. Enjoy the free time, but stay sharp."}

    # Build assignment summary for Claude
    assignment_text = "\n".join(
        f"- {a['title']} ({a['course']}) — due {a['due']}, {a['days_left']} days left, {a['points']} pts"
        for a in assignments
    )

    strategy_prompt = f"""Here are Devin's upcoming assignments:

{assignment_text}

Give Devin a concise, actionable weekly strategy for tackling these assignments. Consider:
- Urgency (what's due soonest)
- Point value (high-value assignments deserve more time)
- Logical grouping (similar subjects back to back)
- Realistic daily workload

Be direct, no fluff. Address him as Devin. No emojis. Format with clear day-by-day or priority-based structure."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system="You are MARLEY, a sharp AI assistant like JARVIS. Give concise, actionable academic strategy advice. No emojis. British-dry wit is welcome.",
            messages=[{"role": "user", "content": strategy_prompt}],
        )
        strategy_text = response.content[0].text
        return {"strategy": strategy_text, "assignment_count": len(assignments)}
    except Exception as e:
        return {"error": f"Strategy generation failed: {e}"}


@app.post("/api/tts")
async def text_to_speech(request: Request):
    """Convert text to speech using ElevenLabs."""
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        return {"error": "No text provided"}

    # Strip markdown for cleaner speech
    clean = text
    for pattern, repl in [
        (r'```[\s\S]*?```', ' code block '),
        (r'`[^`]+`', ''),
        (r'\*\*(.+?)\*\*', r'\1'),
        (r'\*(.+?)\*', r'\1'),
        (r'^#{1,3} ', ''),
        (r'\[([^\]]+)\]\([^)]+\)', r'\1'),
        (r'^[\-\*] ', ''),
        (r'^\d+\. ', ''),
    ]:
        import re as _re
        clean = _re.sub(pattern, repl, clean, flags=_re.MULTILINE)

    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "text": clean[:1000],  # cap to save quota
                "model_id": "eleven_turbo_v2",
                "voice_settings": {
                    "stability": 0.7,
                    "similarity_boost": 0.8,
                },
            },
            timeout=30.0,
        )

    if resp.status_code != 200:
        return {"error": f"ElevenLabs error: {resp.status_code}"}

    return StreamingResponse(
        iter([resp.content]),
        media_type="audio/mpeg",
        headers={"Content-Length": str(len(resp.content))},
    )


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    session_id = str(uuid.uuid4())
    sessions[session_id] = []

    try:
        while True:
            data = json.loads(await ws.receive_text())
            user_msg = data.get("message", "").strip()
            if not user_msg:
                continue

            sessions[session_id].append({"role": "user", "content": user_msg})

            # ── End phrase detection ────────────────────────
            if END_PHRASES.search(user_msg):
                save_result = save_and_push_conversation(sessions[session_id])
                farewell = f"Very good, Devin. Session logged and pushed.\n\n*{save_result}*\n\nI'll be here if you need me, sir."
                sessions[session_id].append({"role": "assistant", "content": farewell})
                # Stream the farewell
                for chunk in [farewell]:
                    await ws.send_text(json.dumps({"type": "delta", "text": chunk}))
                await ws.send_text(json.dumps({"type": "session_saved"}))
                await ws.send_text(json.dumps({"type": "done"}))
                continue

            await ws.send_text(json.dumps({"type": "thinking"}))

            try:
                messages = list(sessions[session_id])
                full_response = ""

                # Agentic loop — keep going until Claude stops calling tools
                while True:
                    full_response = ""
                    tool_use_blocks = []
                    current_tool_id = None
                    current_tool_name = None
                    current_tool_input = ""

                    with client.messages.stream(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=8192,
                        system=SYSTEM_PROMPT,
                        tools=TOOLS,
                        messages=messages,
                    ) as stream:
                        stop_reason = None
                        for event in stream:
                            if event.type == "content_block_start":
                                block = event.content_block
                                if hasattr(block, "type"):
                                    if block.type == "server_tool_use":
                                        await ws.send_text(json.dumps({
                                            "type": "searching",
                                            "query": "web_search",
                                        }))
                                    elif block.type == "tool_use":
                                        current_tool_id = block.id
                                        current_tool_name = block.name
                                        current_tool_input = ""
                                        # Show what MARLEY is doing
                                        tool_label = {
                                            "check_portfolio": "CHECKING PORTFOLIO",
                                            "check_agents": "SCANNING AGENTS",
                                            "manage_agent": "MANAGING AGENT",
                                            "check_dispatches": "CHECKING DISPATCHES",
                                            "check_youtube": "CHECKING YOUTUBE",
                                            "check_canvas": "CHECKING CANVAS",
                                            "canvas_setup": "CONFIGURING CANVAS",
                                            "canvas_login": "LOGGING INTO CANVAS",
                                            "run_command": "RUNNING COMMAND",
                                        }.get(block.name, "WORKING")
                                        await ws.send_text(json.dumps({
                                            "type": "searching",
                                            "query": tool_label,
                                        }))

                            elif event.type == "content_block_delta":
                                if hasattr(event.delta, "text"):
                                    full_response += event.delta.text
                                    await ws.send_text(json.dumps({
                                        "type": "delta",
                                        "text": event.delta.text,
                                    }))
                                elif hasattr(event.delta, "partial_json"):
                                    current_tool_input += event.delta.partial_json

                            elif event.type == "content_block_stop":
                                if current_tool_id and current_tool_name:
                                    try:
                                        parsed_input = json.loads(current_tool_input) if current_tool_input else {}
                                    except json.JSONDecodeError:
                                        parsed_input = {}
                                    tool_use_blocks.append({
                                        "id": current_tool_id,
                                        "name": current_tool_name,
                                        "input": parsed_input,
                                    })
                                    current_tool_id = None
                                    current_tool_name = None
                                    current_tool_input = ""

                            elif event.type == "message_delta":
                                if hasattr(event.delta, "stop_reason"):
                                    stop_reason = event.delta.stop_reason

                    # If Claude wants to use tools, execute them and loop
                    if stop_reason == "tool_use" and tool_use_blocks:
                        # Build the assistant message with all content blocks
                        assistant_content = []
                        if full_response:
                            assistant_content.append({"type": "text", "text": full_response})
                        for tb in tool_use_blocks:
                            assistant_content.append({
                                "type": "tool_use",
                                "id": tb["id"],
                                "name": tb["name"],
                                "input": tb["input"],
                            })
                        messages.append({"role": "assistant", "content": assistant_content})

                        # Execute tools and add results
                        tool_results = []
                        for tb in tool_use_blocks:
                            if tb["name"] == "web_search":
                                continue  # server-side tool, handled by API
                            result = execute_tool(tb["name"], tb["input"])
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tb["id"],
                                "content": result,
                            })

                        if tool_results:
                            messages.append({"role": "user", "content": tool_results})

                        # Reset for next iteration
                        full_response = ""
                        tool_use_blocks = []
                        continue
                    else:
                        # No more tool calls — we're done
                        break

                # Store final response in session
                if full_response:
                    sessions[session_id].append({
                        "role": "assistant",
                        "content": full_response,
                    })

                await ws.send_text(json.dumps({"type": "done"}))

            except anthropic.APIError as e:
                await ws.send_text(json.dumps({
                    "type": "error",
                    "text": f"API Error: {str(e)}",
                }))
            except Exception as e:
                await ws.send_text(json.dumps({
                    "type": "error",
                    "text": f"Error: {str(e)}",
                }))

    except WebSocketDisconnect:
        if session_id in sessions:
            del sessions[session_id]


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7777))
    print(f"\n  🧠 MARLEY is online at http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port)
