#!/usr/bin/env bash
# Installs ClipStack for the current user. Safe to re-run (also used to update).
#   ./install.sh                 install / update
#   ./install.sh --shortcut KEY  use another shortcut, e.g. '<Super><Shift>v'
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHORTCUT='<Super>v'
[[ "${1:-}" == "--shortcut" && -n "${2:-}" ]] && SHORTCUT="$2"

LIB="$HOME/.local/lib/clipstack"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
AUTOSTART="$HOME/.config/autostart"
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

# 1. System packages: GTK bindings (preinstalled on Mint) and xclip, which
#    terminal apps like Claude Code use to read pasted images.
missing=()
python3 -c 'import gi; gi.require_version("Gtk","3.0")' 2>/dev/null || missing+=(python3-gi gir1.2-gtk-3.0)
command -v xclip >/dev/null || missing+=(xclip)
command -v xprop >/dev/null || missing+=(x11-utils)
command -v gdbus >/dev/null || missing+=(libglib2.0-bin)
if ((${#missing[@]})); then
  say "Installing system packages: ${missing[*]} (needs your password)"
  if ! sudo apt-get install -y "${missing[@]}"; then
    warn "Could not install ${missing[*]}. Run: sudo apt install ${missing[*]}"
  fi
fi

# 2. Program files
say "Installing ClipStack to $LIB"
mkdir -p "$LIB" "$BIN" "$APPS" "$AUTOSTART"
install -m 755 "$SRC/clipstack.py" "$LIB/clipstack.py"
ln -sf "$LIB/clipstack.py" "$BIN/clipstack"
install -m 755 "$SRC/clipstack-toggle" "$BIN/clipstack-toggle"

cat > "$APPS/clipstack.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Clipboard History
Comment=Windows-style clipboard history (Super+V)
Exec=$BIN/clipstack-toggle
Icon=edit-paste
Terminal=false
Categories=Utility;
Keywords=clipboard;copy;paste;history;
DESKTOP

# 3. Start on login
cat > "$AUTOSTART/clipstack.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Clipboard History
Comment=Records clipboard history in the background
Exec=$BIN/clipstack --daemon
Icon=edit-paste
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=2
DESKTOP

# 4. Keyboard shortcut (Cinnamon). Adds to your custom shortcuts without
#    touching existing ones; re-running updates the same entry.
if gsettings list-keys org.cinnamon.desktop.keybindings >/dev/null 2>&1; then
  KB=org.cinnamon.desktop.keybindings
  list="$(gsettings get $KB custom-list)"
  slot=""
  for name in $(echo "$list" | tr -d "[]',"); do
    p="/org/cinnamon/desktop/keybindings/custom-keybindings/$name/"
    if [[ "$(gsettings get $KB.custom-keybinding:$p command 2>/dev/null)" == *clipstack* ]]; then
      slot="$name"
    fi
  done
  if [[ -z "$slot" ]]; then
    i=0; while [[ "$list" == *"'custom$i'"* ]]; do i=$((i + 1)); done
    slot="custom$i"
    if [[ "$list" == "@as []" || "$list" == "[]" ]]; then
      gsettings set $KB custom-list "['$slot']"
    else
      gsettings set $KB custom-list "${list%]}, '$slot']"
    fi
  fi
  p="/org/cinnamon/desktop/keybindings/custom-keybindings/$slot/"
  gsettings set $KB.custom-keybinding:$p name 'Clipboard History'
  gsettings set $KB.custom-keybinding:$p command "$BIN/clipstack-toggle"
  gsettings set $KB.custom-keybinding:$p binding "['$SHORTCUT']"
  say "Shortcut $SHORTCUT -> Clipboard History"
else
  warn "Not on Cinnamon: add a keyboard shortcut for '$BIN/clipstack-toggle' in your desktop settings."
fi

if pgrep -x copyq >/dev/null; then
  warn "CopyQ is running and may also grab Super+V. Quit it (copyq exit) or remove its shortcut."
fi

# 5. (Re)start the background process
"$BIN/clipstack" --quit 2>/dev/null || true
sleep 0.5
nohup "$BIN/clipstack" --daemon >/dev/null 2>&1 &
disown || true
say "Done. Press ${SHORTCUT} to open your clipboard history."
