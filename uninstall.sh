#!/usr/bin/env bash
# Removes ClipStack. Your history is kept unless you pass --purge.
set -uo pipefail
BIN="$HOME/.local/bin"
"$BIN/clipstack" --quit 2>/dev/null
rm -rf "$HOME/.local/lib/clipstack"
rm -f "$BIN/clipstack" "$BIN/clipstack-toggle" \
      "$HOME/.local/share/applications/clipstack.desktop" "$HOME/.config/autostart/clipstack.desktop"

KB=org.cinnamon.desktop.keybindings
if gsettings list-keys $KB >/dev/null 2>&1; then
  list="$(gsettings get $KB custom-list)"
  for name in $(echo "$list" | tr -d "[]',"); do
    p="/org/cinnamon/desktop/keybindings/custom-keybindings/$name/"
    if [[ "$(gsettings get $KB.custom-keybinding:$p command 2>/dev/null)" == *clipstack* ]]; then
      gsettings reset-recursively $KB.custom-keybinding:$p
      list="$(echo "$list" | sed -E "s/, '$name'|'$name', |'$name'//")"
      gsettings set $KB custom-list "$list"
    fi
  done
fi

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "$HOME/.local/share/clipstack" "$HOME/.config/clipstack"
  echo "ClipStack and its history removed."
else
  echo "ClipStack removed. History kept in ~/.local/share/clipstack (use --purge to delete it)."
fi
