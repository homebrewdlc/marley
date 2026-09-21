#!/usr/bin/env bash
# ── MARLEY — Mac Setup ──────────────────────────────────
# One-command setup for macOS. Run:
#   chmod +x setup-mac.sh && ./setup-mac.sh

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/venv"

# Colors
CYAN='\033[0;36m'
GREEN='\033[0;32m'
RED='\033[0;31m'
DIM='\033[2m'
YELLOW='\033[0;33m'
NC='\033[0m'

echo -e "${CYAN}"
echo "  ╔══════════════════════════════════════╗"
echo "  ║          M A R L E Y                 ║"
echo "  ║      Mac Setup                       ║"
echo "  ╚══════════════════════════════════════╝"
echo -e "${NC}"

# ── Step 1: Homebrew ──────────────────────────────────────
echo -e "${CYAN}[1/6]${NC} Checking Homebrew..."
if ! command -v brew &>/dev/null; then
  echo -e "${YELLOW}  Homebrew not found. Installing...${NC}"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Add brew to PATH for Apple Silicon Macs
  if [ -f /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
else
  echo -e "${GREEN}  Homebrew found.${NC}"
fi

# ── Step 2: Python ────────────────────────────────────────
echo -e "${CYAN}[2/6]${NC} Checking Python..."
if command -v python3 &>/dev/null; then
  PY=python3
  PY_VERSION=$($PY --version 2>&1 | awk '{print $2}')
  MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
  MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
  if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 11 ]; then
    echo -e "${GREEN}  Python $PY_VERSION found.${NC}"
  else
    echo -e "${YELLOW}  Python $PY_VERSION is too old (need 3.11+). Installing via Homebrew...${NC}"
    brew install python@3.13
    PY=$(brew --prefix)/bin/python3.13
    # Fallback: search common Homebrew paths
    if [ ! -x "$PY" ]; then
      PY=$(find "$(brew --prefix)/opt/python@3.13" -name "python3*" -type f 2>/dev/null | head -1)
    fi
    if [ ! -x "$PY" ]; then
      PY=$(which python3.13 2>/dev/null || true)
    fi
    if [ ! -x "$PY" ]; then
      echo -e "${RED}  Could not find Python 3.13 after install. Try: brew link python@3.13${NC}"
      exit 1
    fi
    echo -e "${GREEN}  Using $PY${NC}"
  fi
else
  echo -e "${YELLOW}  Python not found. Installing via Homebrew...${NC}"
  brew install python@3.13
  PY=$(brew --prefix)/bin/python3.13
  if [ ! -x "$PY" ]; then
    PY=$(which python3.13 2>/dev/null || true)
  fi
  if [ ! -x "$PY" ]; then
    echo -e "${RED}  Could not find Python 3.13 after install. Try: brew link python@3.13${NC}"
    exit 1
  fi
fi

# ── Step 3: Virtual environment ───────────────────────────
echo -e "${CYAN}[3/6]${NC} Creating virtual environment..."
if [ -d "$VENV" ]; then
  echo -e "${DIM}  Existing venv found, recreating...${NC}"
  rm -rf "$VENV"
fi
$PY -m venv "$VENV"

# ── Step 4: Dependencies ─────────────────────────────────
echo -e "${CYAN}[4/6]${NC} Installing dependencies..."
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$DIR/requirements.txt" -q
echo -e "${GREEN}  All packages installed (including edge-tts for voice).${NC}"

# ── Step 5: Playwright (for Canvas LMS) ──────────────────
echo -e "${CYAN}[5/6]${NC} Installing Playwright + Chromium (for Canvas LMS)..."
"$VENV/bin/playwright" install chromium 2>/dev/null && \
  echo -e "${GREEN}  Chromium installed.${NC}" || \
  echo -e "${DIM}  Playwright chromium install failed (Canvas login won't work, but everything else will).${NC}"

# ── Step 6: SSL certs + .env ─────────────────────────────
echo -e "${CYAN}[6/6]${NC} Setting up SSL and config..."

# Generate self-signed SSL certs (needed for voice input over LAN)
CERT_DIR="$DIR/cert"
if [ ! -f "$CERT_DIR/cert.pem" ]; then
  mkdir -p "$CERT_DIR"
  openssl req -x509 -newkey rsa:2048 \
    -keyout "$CERT_DIR/key.pem" \
    -out "$CERT_DIR/cert.pem" \
    -days 365 -nodes \
    -subj "/CN=marley" 2>/dev/null
  echo -e "${GREEN}  SSL certs generated.${NC}"
else
  echo -e "${GREEN}  SSL certs already exist.${NC}"
fi

# Create .env from template if it doesn't exist
if [ ! -f "$DIR/.env" ]; then
  cp "$DIR/.env.example" "$DIR/.env"
  echo -e "${YELLOW}  .env created from template. Edit it with your Anthropic API key:${NC}"
  echo -e "  ${DIM}$DIR/.env${NC}"
else
  echo -e "${GREEN}  .env already exists.${NC}"
fi

# Make the launcher executable
chmod +x "$DIR/marley"

echo ""
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo -e "${GREEN}  Setup complete!${NC}"
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo ""
echo -e "  ${CYAN}1.${NC} Edit your API key:  ${DIM}nano $DIR/.env${NC}"
echo -e "  ${CYAN}2.${NC} Start Marley:       ${DIM}./marley${NC}"
echo -e "  ${CYAN}3.${NC} Open in browser:    ${DIM}https://localhost:7777${NC}"
echo ""
echo -e "  ${DIM}Voice output uses edge-tts (free, no API key needed).${NC}"
echo -e "  ${DIM}Voice input requires HTTPS — accept the self-signed cert in your browser.${NC}"
echo ""
