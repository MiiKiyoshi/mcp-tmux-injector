"""Configuration, deny-list, and shared paths."""
import fnmatch
import json
from pathlib import Path

_INSTRUCTIONS_FILE = Path(__file__).parent.parent / "INSTRUCTIONS.md"
INSTRUCTIONS = _INSTRUCTIONS_FILE.read_text() if _INSTRUCTIONS_FILE.exists() else ""

# Deny-list config: ~/.config/mcp-tmux-injector/config.json
_CONFIG_PATH = Path.home() / ".config" / "mcp-tmux-injector" / "config.json"
_deny_rules: dict[str, list[str]] = {}  # {"shell": [...], "python": [...], "tcl": [...], "send_text": [...]}
TMUX_SOCKET_PATH: str | None = None


def _load_config():
    global _deny_rules, TMUX_SOCKET_PATH
    if _CONFIG_PATH.exists():
        cfg = json.loads(_CONFIG_PATH.read_text())
        _deny_rules = cfg.get("deny", {})
        if "tmux" in cfg and "socket_path" in cfg["tmux"]:
            socket_path = Path(cfg["tmux"]["socket_path"]).expanduser()
            if not socket_path.is_absolute():
                raise ValueError("tmux.socket_path must be an absolute path")
            TMUX_SOCKET_PATH = str(socket_path)


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
