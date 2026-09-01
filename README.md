# MARLEY

Personal AI assistant with voice, web search, Canvas LMS homework tracking, and system management. Think JARVIS, but yours.

---

## Mac Setup (Quick Start)

### 1. Install Prerequisites

Open **Terminal** and run:

```bash
# Install Homebrew (if you don't have it)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python
brew install python@3.13

# Install git (if needed)
brew install git
```

### 2. Clone & Install

```bash
git clone https://github.com/homebrewdlc/marley.git ~/marley
cd ~/marley
git checkout maggie
./marley setup
```

The setup command creates the virtual environment, installs all dependencies, and sets up Playwright for Canvas integration.

### 3. Add Your API Key

```bash
nano ~/marley/.env
```

You need at minimum the **Anthropic API key**. Get one at [console.anthropic.com](https://console.anthropic.com) — sign up, go to API Keys, create a new key.

Your `.env` should look like:
```
ANTHROPIC_API_KEY=sk-ant-your-key-here
PORT=7777
```

For voice output (optional), add an ElevenLabs key:
```
ELEVENLABS_API_KEY=sk_your-key-here
ELEVENLABS_VOICE_ID=JBFqnCBsd6RMkjVDRZzb
```

### 4. Run It

```bash
cd ~/marley
./marley
```

Open **http://localhost:7777** in **Chrome** or **Edge** (Safari doesn't support voice input).

### 5. Done!

Talk to Marley. Ask her anything. Use voice by clicking the mic button. Say "Marley" to wake her up hands-free.

---

## Commands

```
./marley              Start the server
./marley stop         Stop the server
./marley restart      Restart
./marley status       Check if running
./marley setup        First-time install (venv, deps, playwright)
./marley logs         Tail server output
```

## Canvas LMS (Homework Tracking)

Marley can pull your assignments, grades, and deadlines from Canvas. Setup happens through the chat:

1. Ask Marley about your homework
2. She'll ask for your school's Canvas URL (e.g., `canvas.howard.edu`)
3. Enter your email and password when prompted
4. Approve the 2FA notification on your phone
5. Done — she caches the session so you don't have to log in every time

Switch to the **Calendar** tab to see your assignments on a visual calendar with color-coded urgency.

## Features

- **Chat** — Talk to Marley via text or voice. She has web search and can answer anything.
- **Voice Input** — Click the mic or say "Marley" to activate (Chrome/Edge only)
- **Voice Output** — Toggle VOX for spoken responses (needs ElevenLabs key)
- **Calendar** — Visual assignment calendar with urgency colors and a Strategy planner
- **Canvas LMS** — Homework, grades, and assignment details from any Canvas instance
- **Web Search** — Full internet search built in

## Troubleshooting

**"command not found: python3"**
Run `brew install python@3.13` and try again.

**Playwright fails to install**
Run `~/marley/venv/bin/playwright install chromium` manually. If it still fails, Canvas features won't work but everything else will.

**Voice input doesn't work**
Use Chrome or Edge. Safari doesn't support the Web Speech API.

**"Connection refused" on localhost:7777**
Make sure the server is running: `./marley status`. Check logs: `./marley logs`.

**Canvas 2FA keeps timing out**
Make sure your phone's authenticator app is ready. Marley auto-selects the verification code method — enter the code when prompted.

## Requirements

- Python 3.11+
- Anthropic API key
- Chrome or Edge (for voice)
- macOS 12+ (Monterey or newer)

## Files

```
marley              Launcher script
server.py           FastAPI backend + Claude tools + calendar API
canvas.py           Canvas LMS auth + assignment fetching
static/index.html   Frontend UI
.env.example        Configuration template
requirements.txt    Python dependencies
```

## License

MIT
