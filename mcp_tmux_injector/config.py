"""Configuration, deny-list, and shared paths."""
import fnmatch
import json
import sys
from pathlib import Path

_INSTRUCTIONS_FILE = Path(__file__).parent.parent / "instructions.txt"
INSTRUCTIONS = _INSTRUCTIONS_FILE.read_text() if _INSTRUCTIONS_FILE.exists() else ""

# Deny-list config: ~/.config/mcp-tmux-injector/config.json
_CONFIG_PATH = Path.home() / ".config" / "mcp-tmux-injector" / "config.json"
_deny_rules: dict[str, list[str]] = {}  # {"shell": [...], "python": [...], "tcl": [...], "send_text": [...]}

# Directory for fingerprint snapshot files (used by Monitor-mode poll_pane)
FINGERPRINT_DIR = Path.home() / ".cache" / "mcp-tmux-injector" / "fingerprints"

# Path to the mcp-tmux-injector entry point in the active venv.
# The server runs through this venv's python, so the binary sits beside it.
# Used directly in watch commands instead of `uv run --directory ...` to avoid
# uv resolution overhead and shorten the command string the model sees.
SERVER_BIN = str(Path(sys.executable).parent / "mcp-tmux-injector")


def _load_config():
    global _deny_rules
    if _CONFIG_PATH.exists():
        cfg = json.loads(_CONFIG_PATH.read_text())
        _deny_rules = cfg.get("deny", {})


_load_config()


class DenyError(Exception):
    pass


def check_deny(code: str, category: str) -> None:
    """Check code against deny patterns. Raises DenyError if matched."""
    patterns = _deny_rules.get(category, [])
    for pattern in patterns:
        for line in code.split('\n'):
            if fnmatch.fnmatch(line.strip(), pattern):
                raise DenyError(f"Blocked by deny rule: '{pattern}' matched '{line.strip()}'")
