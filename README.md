# MARLEY

Personal AI assistant with voice, web search, Canvas LMS homework tracking, and system management. Think JARVIS, but yours.

## Quick Start

```bash
git clone https://github.com/d-boy/marley.git
cd marley
./marley setup
# Edit .env with your Anthropic API key
./marley
```

Open **http://localhost:7777** in Chrome or Edge.

## Commands

```
./marley              Start the server
./marley stop         Stop the server
./marley restart      Restart
./marley status       Check if running
./marley setup        First-time install (venv, deps, playwright)
./marley logs         Tail server output
```

## Requirements

- Python 3.11+
- Anthropic API key ([console.anthropic.com](https://console.anthropic.com))
- Chrome or Edge (for voice input via Web Speech API)

### Optional

- **ElevenLabs API key** for high-quality voice output
- **Playwright + Chromium** for Canvas LMS integration (installed automatically by `./marley setup`)

## Configuration

Copy `.env.example` to `.env` and add your keys:

```
ANTHROPIC_API_KEY=sk-ant-...
PORT=7777
ELEVENLABS_API_KEY=sk_...        # optional, for voice
ELEVENLABS_VOICE_ID=JBFqnCBsd6RMkjVDRZzb  # optional
```

## Features

### Chat
Talk to Marley via text or voice. She has access to web search, can run shell commands, and integrates with any local services you configure.

### Calendar
Visual calendar showing upcoming Canvas LMS assignments with urgency color-coding. Hit **STRATEGY** and Marley generates a weekly game plan. Hit **SPEAK** and she reads your deadlines aloud.

### Canvas LMS
Homework, grades, and assignment details from any Canvas instance using Microsoft SSO. Setup happens through the chat on first use — Marley asks for your school URL, email, and password (stored locally only, never sent anywhere).

### Voice
- **Wake word**: Say "Marley" to activate (enable via WAKE WORD button)
- **Continuous conversation**: After activation, Marley listens continuously until 45s of silence
- **Voice output**: Toggle VOX for spoken responses (requires ElevenLabs key)

## Files

```
marley              Launcher script
server.py           FastAPI backend + Claude tools + calendar API
canvas.py           Canvas LMS auth (Playwright SSO) + assignment fetching
static/index.html   Frontend UI
.env.example        Configuration template
requirements.txt    Python dependencies
```

## How It Works

Marley runs a FastAPI server with a WebSocket connection to the browser. User messages go to Claude (Haiku) with custom tool definitions. Claude decides which tools to call (web search, Canvas, shell, etc.), Marley executes them, feeds results back, and Claude responds naturally. The calendar tab uses direct REST endpoints for speed.

## License

MIT
