#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Installing mcp-tmux-injector..."

# Check requirements
if ! command -v tmux &> /dev/null; then
    echo "ERROR: tmux is not installed"
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 is not installed"
    exit 1
fi

# Install package
pip install -e .

echo "Installation complete!"
echo ""

# Claude Code integration
if command -v claude &> /dev/null; then
    read -p "Add to Claude Code? [Y/n] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        claude mcp add tmux-injector --scope user -- mcp-tmux-injector
        echo "Added to Claude Code"
    fi
fi

# Claude Desktop integration
CLAUDE_CONFIG="$HOME/.config/Claude/claude_desktop_config.json"
if [ -d "$HOME/.config/Claude" ]; then
    read -p "Add to Claude Desktop? [Y/n] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        if [ -f "$CLAUDE_CONFIG" ]; then
            # Check if already configured
            if grep -q "tmux-injector" "$CLAUDE_CONFIG"; then
                echo "Already configured in Claude Desktop"
            else
                echo "Add this to $CLAUDE_CONFIG:"
                echo ""
                echo '  "mcpServers": {'
                echo '    "tmux-injector": {'
                echo '      "command": "mcp-tmux-injector"'
                echo '    }'
                echo '  }'
            fi
        else
            echo "Creating $CLAUDE_CONFIG..."
            mkdir -p "$(dirname "$CLAUDE_CONFIG")"
            cat > "$CLAUDE_CONFIG" << 'EOF'
{
  "mcpServers": {
    "tmux-injector": {
      "command": "mcp-tmux-injector"
    }
  }
}
EOF
            echo "Created Claude Desktop config"
        fi
    fi
fi

echo ""
echo "Done! Restart Claude to use tmux-injector."
