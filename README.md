# ClipStack

A Windows-style **Win+V clipboard history** for Linux Mint (Cinnamon / X11).
Small (one Python file), fast, no Electron, no background lag.

## Features
- **Super+V** opens a card popup next to the mouse; **click** (or Enter) pastes into the app you were in
- Text **and images** (screenshots, copied pictures) with thumbnails
- **Pin / unpin** items, **delete** single items, **Clear all** (keeps pinned)
- Keeps the latest **25** unpinned items (pinned ones never expire)
- Text previews show up to 4 lines, then …
- Pasting an item leaves it where it is in the list
- Stays open while you type; closes on Esc or a click outside; light/dark theme follows your system
- Starts automatically on login; history survives restarts
- Terminal-aware: text goes in with Ctrl+Shift+V in terminals, Ctrl+V elsewhere
- Ignores password-manager copies; history folder is private (chmod 700)
- Installs **xclip**, so terminal agents (Claude Code etc.) can paste images

## Install
```bash
git clone <your-repo-url> clipstack && cd clipstack
./install.sh                         # asks for sudo once (for xclip)
./install.sh --shortcut '<Super><Shift>v'   # optional: different shortcut
```
Re-running `install.sh` updates an existing install. If CopyQ is also running,
quit it (`copyq exit`) so it doesn't fight over Super+V.

## Keys in the popup
| Key | Action |
|---|---|
| Up / Down, Tab, PgUp / PgDn | move |
| Enter or click | paste |
| Ctrl+P | pin / unpin |
| Delete | remove item |
| Esc or click outside | close |

## Settings
`clipstack --config` opens `~/.config/clipstack/config.json`
(restart with `clipstack --quit; clipstack --daemon`).
Options: `max_items`, `keep_history_after_restart`, `theme` (auto/dark/light),
`width`, `height`, `pinned_on_top`, `smart_terminal_paste`,
`ignore_password_managers`, `max_text_kb`, `extra_terminals`.

## Commands
`clipstack` (toggle popup) · `--daemon` · `--clear` · `--quit` · `--config` · `--version`

## Uninstall
`./uninstall.sh` (keeps history) or `./uninstall.sh --purge` (deletes it too).

## How it works
A GTK3 process watches the X11 CLIPBOARD and stores items in
`~/.local/share/clipstack/` (JSON + PNG files). The shortcut runs
`clipstack-toggle`, which tells the running process to show the popup over D-Bus.
Pasting puts the item back on the clipboard, refocuses your previous window and
sends Ctrl+V through the XTest extension (no xdotool needed).

## Limits
X11 only (Cinnamon default on Mint). Wayland isn't supported.
