#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# DevOps AI Agent — One-time setup for Linux / macOS
#
# Usage:
#     bash setup.sh
#
# Requires: Python 3.12+, Git, make
# Installs: virtual-env, project dependencies, pre-commit hooks
# ──────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
DIM='\033[2m'
RESET='\033[0m'

TOTAL_STEPS=7
CURRENT_STEP=0

# ── Helper functions ──────────────────────────────────────────────────

progress_bar() {
    local pct=$1
    local filled=$(( pct / 5 ))
    local empty=$(( 20 - filled ))
    local bar=""
    for ((i=0; i<filled; i++)); do bar+="█"; done
    for ((i=0; i<empty; i++));  do bar+="░"; done
    echo "$bar"
}

step() {
    CURRENT_STEP=$((CURRENT_STEP + 1))
    local pct=$(( CURRENT_STEP * 100 / TOTAL_STEPS ))
    local bar
    bar=$(progress_bar "$pct")
    echo ""
    echo -e "  ${MAGENTA}[${bar}] ${pct}% — Step ${CURRENT_STEP}/${TOTAL_STEPS}${RESET}"
    echo -e "  ${CYAN}>>> $1${RESET}"
}

ok()   { echo -e "      ${GREEN}[OK]${RESET} $1"; }
skip() { echo -e "      ${YELLOW}[SKIP]${RESET} $1"; }
err()  { echo -e "      ${RED}[ERROR]${RESET} $1"; }

run_verbose() {
    # Run a command with real-time output, filtered to show meaningful lines.
    # Usage: run_verbose "label" command arg1 arg2 ...
    local label="$1"; shift
    echo -e "      ${DIM}[$label] Running: $*${RESET}"
    # Stream output, highlight key pip/install lines for readability
    "$@" 2>&1 | while IFS= read -r line; do
        # Show pip progress + key status lines
        if echo "$line" | grep -qiE 'install|collect|download|build|using|success|requirement|prepar|already|upgrad|uninstall|running|creating|copying|writing|adding'; then
            echo "        $line"
        fi
    done
    # Capture the real exit code of the command (not grep)
    local exit_code=${PIPESTATUS[0]}
    if [ "$exit_code" -ne 0 ]; then
        err "Command failed with exit code $exit_code: $*"
        exit "$exit_code"
    fi
}

# ── Detect OS ─────────────────────────────────────────────────────────
detect_os() {
    case "$(uname -s)" in
        Linux*)  echo "linux"  ;;
        Darwin*) echo "macos"  ;;
        MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
        *)       echo "unknown" ;;
    esac
}

OS_TYPE=$(detect_os)
if [ "$OS_TYPE" = "windows" ]; then
    err "This script is for Linux/macOS. On Windows run:"
    echo -e "      ${YELLOW}powershell -ExecutionPolicy Bypass -File setup.ps1${RESET}"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo -e "  ${CYAN}DevOps AI Agent — Setup (${OS_TYPE})${RESET}"
echo -e "  ${DIM}─────────────────────────────────────────${RESET}"

# ── 1. Check Python ──────────────────────────────────────────────────
step "Checking Python..."
PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON_CMD="$cmd"
        break
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    err "Python not found. Install Python 3.12+ and re-run."
    echo -e "      ${YELLOW}Ubuntu/Debian: sudo apt install python3 python3-venv${RESET}"
    echo -e "      ${YELLOW}macOS:         brew install python@3.12${RESET}"
    exit 1
fi

PY_VERSION=$($PYTHON_CMD --version 2>&1)
PY_MINOR=$($PYTHON_CMD -c "import sys; print(sys.version_info.minor)")
if [ "$PY_MINOR" -lt 12 ]; then
    err "$PY_VERSION detected — Python 3.12+ is required."
    exit 1
fi
ok "$PY_VERSION"

# ── 2. Check Git ─────────────────────────────────────────────────────
step "Checking Git..."
if ! command -v git &>/dev/null; then
    err "Git not found. Install Git and re-run."
    echo -e "      ${YELLOW}Ubuntu/Debian: sudo apt install git${RESET}"
    echo -e "      ${YELLOW}macOS:         brew install git${RESET}"
    exit 1
fi
GIT_VERSION=$(git --version 2>&1)
ok "$GIT_VERSION"

# ── 3. Check make ────────────────────────────────────────────────────
step "Checking make..."
if command -v make &>/dev/null; then
    ok "make $(make --version 2>&1 | head -n1)"
else
    echo -e "      ${YELLOW}make not found — attempting install...${RESET}"
    if [ "$OS_TYPE" = "linux" ]; then
        if command -v apt-get &>/dev/null; then
            echo -e "      ${DIM}[apt] sudo apt-get install -y make${RESET}"
            sudo apt-get install -y make
        elif command -v dnf &>/dev/null; then
            echo -e "      ${DIM}[dnf] sudo dnf install -y make${RESET}"
            sudo dnf install -y make
        elif command -v yum &>/dev/null; then
            echo -e "      ${DIM}[yum] sudo yum install -y make${RESET}"
            sudo yum install -y make
        elif command -v pacman &>/dev/null; then
            echo -e "      ${DIM}[pacman] sudo pacman -Sy --noconfirm make${RESET}"
            sudo pacman -Sy --noconfirm make
        else
            err "Could not find a package manager. Install 'make' manually and re-run."
            exit 1
        fi
    elif [ "$OS_TYPE" = "macos" ]; then
        if command -v brew &>/dev/null; then
            echo -e "      ${DIM}[brew] brew install make${RESET}"
            brew install make
        else
            echo -e "      ${DIM}[xcode] Installing Xcode CLT (includes make)...${RESET}"
            xcode-select --install 2>/dev/null || true
        fi
    fi

    if command -v make &>/dev/null; then
        ok "make installed."
    else
        err "make installation failed. Install manually and re-run."
        exit 1
    fi
fi

# ── 4. Create virtual environment ────────────────────────────────────
step "Setting up virtual environment..."
VENV_PATH="$SCRIPT_DIR/.venv"
if [ -f "$VENV_PATH/bin/python" ]; then
    skip ".venv already exists."
else
    echo -e "      ${DIM}Creating .venv...${RESET}"
    $PYTHON_CMD -m venv "$VENV_PATH"
    ok "Created .venv"
fi

# Activate
# shellcheck disable=SC1091
source "$VENV_PATH/bin/activate"
ok "Activated .venv"

# ── 5. Upgrade pip ───────────────────────────────────────────────────
step "Upgrading pip..."
run_verbose "pip" python -m pip install --upgrade pip
ok "pip upgraded."

# ── 6. Install project dependencies ─────────────────────────────────
step "Installing project dependencies (dev + infra extras)..."
echo -e "      ${YELLOW}This may take a few minutes — streaming output below:${RESET}"
run_verbose "pip" pip install -e ".[dev,infra]" --verbose
ok "Dependencies installed (dev + infra extras)."

# ── 7. Install pre-commit hooks ─────────────────────────────────────
step "Installing git hooks..."
run_verbose "hooks" pre-commit install --hook-type pre-commit --hook-type commit-msg
ok "Pre-commit + commit-msg hooks installed."

# ── Done ─────────────────────────────────────────────────────────────
echo ""
echo -e "  ${MAGENTA}[████████████████████] 100% — All steps complete!${RESET}"
echo ""
echo -e "  ${GREEN}============================================${RESET}"
echo -e "  ${GREEN}  Setup complete! You're ready to go.       ${RESET}"
echo -e "  ${GREEN}============================================${RESET}"
echo ""
echo "  Activate venv:   source .venv/bin/activate"
echo "  Available tasks: make help"
echo "  Commit changes:  make commit"
echo "  Run tests:       make test"
echo ""
