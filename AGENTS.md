Work directly on main; do not create task branches; after delivery, local and origin expose only main.
Tests: `pip install -e ".[dev]"`, then `python -m pytest -q`. The tool tests start a private tmux server on their own socket.
