#!/usr/bin/env python3
"""ClipStack - a Windows-style (Win+V) clipboard history for Linux (X11).

One small background process records everything you copy (text and images).
Pressing Super+V pops up a card list next to the mouse; clicking a card pastes
it into the window you were using. Pinned items survive "Clear all", restarts
and the history limit.
"""

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import warnings

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkPixbuf, GdkX11, Gio, GLib, Gtk, Pango  # noqa: E402

APP_ID = "io.github.clipstack"
VERSION = "1.0.0"

DATA_DIR = os.path.join(GLib.get_user_data_dir(), "clipstack")
IMG_DIR = os.path.join(DATA_DIR, "images")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "clipstack")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    # How many unpinned items to keep. Pinned items never count.
    "max_items": 25,
    # Windows wipes unpinned history on reboot; set false to copy that.
    "keep_history_after_restart": True,
    # "auto" follows the Cinnamon/GTK theme, or force "dark" / "light".
    "theme": "auto",
    "width": 380,
    "height": 500,
    # Show pinned items above the rest instead of in copy order.
    "pinned_on_top": False,
    # Terminals paste text with Ctrl+Shift+V; images still get Ctrl+V so
    # terminal apps (Claude Code, etc.) read the image through xclip.
    "smart_terminal_paste": True,
    # Skip items marked secret by password managers (KeePassXC, Bitwarden...).
    "ignore_password_managers": True,
    # Skip text bigger than this (in KB) to keep things snappy.
    "max_text_kb": 2048,
    "extra_terminals": [],
}

TERMINALS = {
    "gnome-terminal", "gnome-terminal-server", "x-terminal-emulator", "xfce4-terminal",
    "mate-terminal", "tilix", "terminator", "konsole", "kitty", "alacritty", "wezterm",
    "org.wezfurlong.wezterm", "xterm", "uxterm", "urxvt", "rxvt", "st", "st-256color",
    "foot", "ghostty", "com.mitchellh.ghostty", "guake", "tilda", "yakuake", "qterminal",
    "lxterminal", "sakura", "terminology", "tabby", "hyper", "blackbox",
    "com.raggesilver.blackbox", "ptyxis", "org.gnome.ptyxis", "org.gnome.console", "kgx",
    "cool-retro-term", "contour", "rio", "warp", "wave",
}
TEXT_TARGETS = {"UTF8_STRING", "text/plain;charset=utf-8", "text/plain", "STRING", "TEXT"}
SECRET_TARGETS = {"x-kde-passwordManagerHint"}
FILE_TARGETS = {"x-special/gnome-copied-files", "x-special/mate-copied-files"}

MODIFIERS = (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
             | Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.MOD4_MASK
             | Gdk.ModifierType.SUPER_MASK)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    except (OSError, ValueError) as e:
        print(f"clipstack: ignoring broken config ({e})", file=sys.stderr)
    return cfg


def time_ago(ts):
    s = time.time() - ts
    if s < 60:
        return "Just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    if s < 172800:
        return "Yesterday"
    return time.strftime("%b %d", time.localtime(ts))


class Keyboard:
    """Sends fake key presses through the X11 XTest extension (no xdotool needed)."""

    def __init__(self):
        x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
        xtst = ctypes.cdll.LoadLibrary("libXtst.so.6")
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x11.XStringToKeysym.restype = ctypes.c_ulong
        x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        x11.XFlush.argtypes = [ctypes.c_void_p]
        xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                           ctypes.c_ulong]
        self.x11, self.xtst = x11, xtst
        self.dpy = x11.XOpenDisplay(None)

    def combo(self, *keys):
        codes = [self.x11.XKeysymToKeycode(self.dpy, self.x11.XStringToKeysym(k.encode()))
                 for k in keys]
        for c in codes:
            self.xtst.XTestFakeKeyEvent(self.dpy, c, True, 0)
        for c in reversed(codes):
            self.xtst.XTestFakeKeyEvent(self.dpy, c, False, 0)
        self.x11.XFlush(self.dpy)


class History:
    """The list of clipboard items, newest first, saved to disk."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.items = []
        self.on_change = None
        self._save_id = 0
        os.makedirs(IMG_DIR, mode=0o700, exist_ok=True)
        os.chmod(DATA_DIR, 0o700)  # clipboards hold passwords and private stuff
        self._load()

    def _load(self):
        try:
            with open(HISTORY_FILE) as f:
                self.items = json.load(f).get("items", [])
        except (OSError, ValueError):
            self.items = []
        if not self.cfg["keep_history_after_restart"]:
            self.items = [i for i in self.items if i.get("pinned")]
        self.items = [i for i in self.items
                      if i["kind"] == "text" or os.path.exists(self.image_path(i))]
        self._remove_orphan_images()

    def _remove_orphan_images(self):
        used = {i["image"] for i in self.items if i["kind"] == "image"}
        for name in os.listdir(IMG_DIR):
            if name not in used:
                try:
                    os.remove(os.path.join(IMG_DIR, name))
                except OSError:
                    pass

    @staticmethod
    def image_path(item):
        return os.path.join(IMG_DIR, item["image"])

    def find(self, item_id):
        return next((i for i in self.items if i["id"] == item_id), None)

    def add(self, item):
        existing = next((i for i in self.items if i["hash"] == item["hash"]), None)
        if existing:
            self.items.remove(existing)
            existing["time"] = item["time"]
            item = existing
        self.items.insert(0, item)
        self._trim()
        self.changed()
        return item

    def bump(self, item):
        self.items.remove(item)
        item["time"] = time.time()
        self.items.insert(0, item)
        self.changed()

    def _trim(self):
        unpinned = [i for i in self.items if not i.get("pinned")]
        for old in unpinned[self.cfg["max_items"]:]:
            self._drop(old)

    def _drop(self, item):
        self.items.remove(item)
        if item["kind"] == "image" and not any(
                i["kind"] == "image" and i["image"] == item["image"] for i in self.items):
            try:
                os.remove(self.image_path(item))
            except OSError:
                pass

    def delete(self, item):
        self._drop(item)
        self.changed()

    def toggle_pin(self, item):
        item["pinned"] = not item.get("pinned")
        self._trim()
        self.changed()

    def clear_unpinned(self):
        for item in [i for i in self.items if not i.get("pinned")]:
            self._drop(item)
        self.changed()

    def ordered(self):
        if self.cfg["pinned_on_top"]:
            return ([i for i in self.items if i.get("pinned")]
                    + [i for i in self.items if not i.get("pinned")])
        return list(self.items)

    def changed(self):
        if self.on_change:
            self.on_change()
        if not self._save_id:
            self._save_id = GLib.timeout_add(400, self._save)

    def _save(self):
        self._save_id = 0
        tmp = HISTORY_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "items": self.items}, f)
        os.replace(tmp, HISTORY_FILE)
        return False

    def flush(self):
        if self._save_id:
            GLib.source_remove(self._save_id)
            self._save()


class Watcher:
    """Listens for clipboard changes and turns them into history items."""

    def __init__(self, cfg, history):
        self.cfg = cfg
        self.history = history
        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.clipboard.connect("owner-change", self._on_owner_change)
        self.suppress_until = 0.0
        # Hash of the item currently owned by the app it was copied from. Pasting
        # that item again leaves the clipboard alone, so rich formatting survives.
        self.live_hash = None
        self._debounce_id = 0

    def _on_owner_change(self, *_):
        if time.monotonic() < self.suppress_until:
            return
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(120, self._read)

    def _read(self):
        self._debounce_id = 0
        self.clipboard.request_targets(self._on_targets)
        return False

    def _on_targets(self, _clipboard, atoms, *_):
        names = {a.name() for a in (atoms or [])}
        if not names:
            self.live_hash = None
            return
        if self.cfg["ignore_password_managers"] and names & SECRET_TARGETS:
            return
        if names & FILE_TARGETS:
            return  # copied files in the file manager; Windows doesn't list these either
        if names & TEXT_TARGETS:
            self.clipboard.request_text(self._on_text)
        elif any(n.startswith("image/") for n in names):
            self.clipboard.request_image(self._on_image)

    def _on_text(self, _clipboard, text, *_):
        if not text or not text.strip() or len(text) > self.cfg["max_text_kb"] * 1024:
            return
        digest = hashlib.sha1(b"t" + text.encode("utf-8", "replace")).hexdigest()
        self.live_hash = digest
        self.history.add({"id": uuid.uuid4().hex, "kind": "text", "text": text,
                          "hash": digest, "pinned": False, "time": time.time()})

    def _on_image(self, _clipboard, pixbuf, *_):
        if pixbuf is None:
            return
        threading.Thread(target=self._store_image, args=(pixbuf,), daemon=True).start()

    def _store_image(self, pixbuf):
        data = pixbuf.read_pixel_bytes().get_data()
        digest = hashlib.sha1(b"i%d,%d," % (pixbuf.get_width(), pixbuf.get_height())
                              + data).hexdigest()
        name = digest + ".png"
        path = os.path.join(IMG_DIR, name)
        if not os.path.exists(path):
            pixbuf.savev(path + ".tmp", "png", [], [])
            os.replace(path + ".tmp", path)
        item = {"id": uuid.uuid4().hex, "kind": "image", "image": name, "hash": digest,
                "w": pixbuf.get_width(), "h": pixbuf.get_height(),
                "pinned": False, "time": time.time()}
        GLib.idle_add(self._add_image, item)

    def _add_image(self, item):
        self.live_hash = item["hash"]
        self.history.add(item)
        return False

    def put(self, item):
        """Puts a history item back on the clipboard."""
        if item["hash"] == self.live_hash:
            return
        self.suppress_until = time.monotonic() + 0.5
        self.live_hash = None
        if item["kind"] == "text":
            self.clipboard.set_text(item["text"], -1)
        else:
            self.clipboard.set_image(GdkPixbuf.Pixbuf.new_from_file(History.image_path(item)))


CSS = """
window.clipstack { background-color: transparent; }
.cs-panel, .cs-panel * {
  font-family: "Noto Sans", "Noto Sans Arabic", "Noto Naskh Arabic", "Ubuntu", sans-serif;
  font-size: 10pt;
}
.cs-panel {
  background-color: @cs_bg; color: @cs_fg;
  border-radius: 10px; border: 1px solid @cs_border;
  padding: 12px 10px 8px 10px;
}
.cs-title { font-weight: bold; font-size: 11pt; padding-left: 4px; }
.cs-clear {
  background-image: none; background-color: transparent; border: none; box-shadow: none;
  color: @cs_accent; padding: 2px 8px; border-radius: 4px; min-height: 0;
}
.cs-clear:hover { background-color: @cs_hover; }
.cs-clear label { color: @cs_accent; }
.cs-search {
  margin: 10px 2px 8px 2px; border-radius: 6px; min-height: 30px;
  background-image: none; background-color: @cs_card; color: @cs_fg;
  border: 1px solid @cs_border; box-shadow: none;
}
.cs-search:focus { border-bottom: 2px solid @cs_accent; }
scrolledwindow, viewport, list.cs-list { background-color: transparent; }
list.cs-list row { background-color: transparent; padding: 3px 2px; outline: none;
                   box-shadow: none; border: none; }
list.cs-list row:selected, list.cs-list row:hover { background-color: transparent; }
.cs-card { background-color: @cs_card; border-radius: 6px; padding: 9px 6px 8px 12px;
           border: 1px solid @cs_card_border; }
row:hover .cs-card { background-color: @cs_hover_card; }
row:selected .cs-card { border: 1px solid @cs_accent; background-color: @cs_hover_card; }
.cs-text { color: @cs_fg; }
.cs-meta { color: @cs_dim; font-size: 8pt; }
.cs-btn {
  opacity: 0; background-image: none; background-color: transparent; border: none;
  box-shadow: none; padding: 3px; min-width: 22px; min-height: 22px; border-radius: 4px;
  color: @cs_dim;
}
.cs-btn image { color: @cs_dim; }
row:hover .cs-btn, row:selected .cs-btn, .cs-btn.pinned { opacity: 1; }
.cs-btn:hover { background-color: @cs_hover; }
.cs-btn.pinned, .cs-btn.pinned image { color: @cs_accent; }
.cs-thumb { border-radius: 4px; }
.cs-empty { color: @cs_dim; padding: 40px 20px; }
.cs-empty-title { color: @cs_fg; font-weight: bold; font-size: 10.5pt; }
.cs-hint { color: @cs_dim; font-size: 7.5pt; padding-top: 6px; }
"""

PALETTES = {
    "dark": {"bg": "rgba(32,32,32,0.97)", "fg": "#f3f3f3", "dim": "#a0a0a0",
             "border": "rgba(255,255,255,0.09)", "card": "#2d2d2d",
             "card_border": "rgba(255,255,255,0.04)", "hover_card": "#383838",
             "hover": "rgba(255,255,255,0.08)", "accent": "#60cdff"},
    "light": {"bg": "rgba(243,243,243,0.98)", "fg": "#1b1b1b", "dim": "#616161",
              "border": "rgba(0,0,0,0.12)", "card": "#ffffff",
              "card_border": "rgba(0,0,0,0.06)", "hover_card": "#f9f9f9",
              "hover": "rgba(0,0,0,0.06)", "accent": "#005fb8"},
}


def system_prefers_dark():
    try:
        out = subprocess.run(["gsettings", "get", "org.x.apps.portal", "color-scheme"],
                             capture_output=True, text=True, timeout=1).stdout
        if "prefer-dark" in out:
            return True
        if "prefer-light" in out:
            return False
    except (OSError, subprocess.SubprocessError):
        pass
    theme = Gtk.Settings.get_default().get_property("gtk-theme-name") or ""
    return "dark" in theme.lower()


class Popup(Gtk.Window):
    def __init__(self, app):
        super().__init__(title="Clipboard")
        self.app = app
        self.cfg = app.cfg
        self.history = app.history
        self.dirty = True
        self.shown_at = 0.0
        self.thumbs = {}

        self.get_style_context().add_class("clipstack")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_default_size(self.cfg["width"], self.cfg["height"])
        self.set_size_request(self.cfg["width"], self.cfg["height"])
        visual = self.get_screen().get_rgba_visual()
        if visual and self.get_screen().is_composited():
            self.set_visual(visual)
        self._apply_css()

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        panel.get_style_context().add_class("cs-panel")
        self.add(panel)

        header = Gtk.Box(spacing=6)
        title = Gtk.Label(label="Clipboard", xalign=0)
        title.get_style_context().add_class("cs-title")
        header.pack_start(title, True, True, 0)
        clear = Gtk.Button(label="Clear all")
        clear.set_tooltip_text("Remove everything except pinned items")
        clear.get_style_context().add_class("cs-clear")
        clear.connect("clicked", lambda *_: self.history.clear_unpinned())
        header.pack_end(clear, False, False, 0)
        panel.pack_start(header, False, False, 0)

        self.search = Gtk.SearchEntry(placeholder_text="Search history")
        self.search.get_style_context().add_class("cs-search")
        self.search.connect("search-changed", self._on_search)
        panel.pack_start(self.search, False, False, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.get_style_context().add_class("cs-list")
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_activate_on_single_click(True)
        self.listbox.set_can_focus(False)
        self.listbox.set_filter_func(self._filter)
        self.listbox.connect("row-activated", lambda _lb, row: self.app.paste(row.item))
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.add(self.listbox)

        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        empty.get_style_context().add_class("cs-empty")
        t = Gtk.Label(label="Nothing here yet")
        t.get_style_context().add_class("cs-empty-title")
        d = Gtk.Label(label="Copy text or an image and it will show up here.",
                      wrap=True, justify=Gtk.Justification.CENTER)
        empty.pack_start(t, False, False, 0)
        empty.pack_start(d, False, False, 0)
        self.stack = Gtk.Stack()
        self.stack.add_named(self.scroller, "list")
        self.stack.add_named(empty, "empty")
        panel.pack_start(self.stack, True, True, 0)

        hint = Gtk.Label(label="Enter paste  ·  Ctrl+P pin  ·  Del remove  ·  Esc close",
                         ellipsize=Pango.EllipsizeMode.END)
        hint.get_style_context().add_class("cs-hint")
        panel.pack_end(hint, False, False, 0)

        self.connect("key-press-event", self._on_key)
        self.connect("focus-out-event", self._on_focus_out)
        self.connect("delete-event", lambda *_: self.hide() or True)
        panel.show_all()
        self.realize()

    def _apply_css(self):
        theme = self.cfg["theme"]
        if theme not in PALETTES:
            theme = "dark" if system_prefers_dark() else "light"
        colors = "".join(f"@define-color cs_{k} {v};\n" for k, v in PALETTES[theme].items())
        provider = Gtk.CssProvider()
        provider.load_from_data((colors + CSS).encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

    # ---- building the list -------------------------------------------------

    def rebuild(self):
        self.dirty = False
        keep = self.selected_item()
        for row in self.listbox.get_children():
            self.listbox.remove(row)
        live = {i["image"] for i in self.history.items if i["kind"] == "image"}
        self.thumbs = {k: v for k, v in self.thumbs.items() if k in live}
        for item in self.history.ordered():
            self.listbox.add(self._make_row(item))
        self.listbox.show_all()
        self.stack.set_visible_child_name("list" if self.history.items else "empty")
        rows = self._visible_rows()
        target = next((r for r in rows if keep and r.item["id"] == keep["id"]), None)
        self._select(target or (rows[0] if rows else None))

    def _make_row(self, item):
        row = Gtk.ListBoxRow()
        row.item = item
        card = Gtk.Box(spacing=4)
        card.get_style_context().add_class("cs-card")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)

        if item["kind"] == "text":
            preview = item["text"].lstrip("\n")[:600].replace("\t", "    ")
            label = Gtk.Label(label=preview, xalign=0, yalign=0, wrap=True,
                              wrap_mode=Pango.WrapMode.WORD_CHAR,
                              ellipsize=Pango.EllipsizeMode.END, lines=4,
                              max_width_chars=1, selectable=False)
            label.get_style_context().add_class("cs-text")
            body.pack_start(label, False, False, 0)
            lines = item["text"].count("\n") + 1
            meta = time_ago(item["time"]) + (f"  ·  {lines} lines" if lines > 1 else "")
        else:
            body.pack_start(self._thumb(item), False, False, 0)
            meta = f"Image  ·  {item.get('w', '?')} × {item.get('h', '?')}  ·  " \
                   f"{time_ago(item['time'])}"
        m = Gtk.Label(label=meta, xalign=0)
        m.get_style_context().add_class("cs-meta")
        body.pack_start(m, False, False, 0)
        card.pack_start(body, True, True, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        pin = self._icon_button("view-pin-symbolic",
                                "Unpin" if item.get("pinned") else "Pin (Ctrl+P)")
        if item.get("pinned"):
            pin.get_style_context().add_class("pinned")
        pin.connect("clicked", lambda *_: self.history.toggle_pin(item))
        delete = self._icon_button("edit-delete-symbolic", "Remove (Del)")
        delete.connect("clicked", lambda *_: self.history.delete(item))
        buttons.pack_start(pin, False, False, 0)
        buttons.pack_start(delete, False, False, 0)
        card.pack_end(buttons, False, False, 0)

        row.search_text = (item["text"].lower() if item["kind"] == "text" else "image picture")
        row.add(card)
        return row

    def _icon_button(self, icon, tip):
        b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
        b.set_tooltip_text(tip)
        b.set_can_focus(False)
        b.set_valign(Gtk.Align.START)
        b.get_style_context().add_class("cs-btn")
        return b

    def _thumb(self, item):
        name = item["image"]
        if name not in self.thumbs:
            max_w = self.cfg["width"] - 90
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(History.image_path(item),
                                                             max_w, 120, True)
            except GLib.Error:
                pb = None
            self.thumbs[name] = pb
        img = Gtk.Image.new_from_pixbuf(self.thumbs[name]) if self.thumbs[name] \
            else Gtk.Image.new_from_icon_name("image-missing", Gtk.IconSize.DIALOG)
        img.set_halign(Gtk.Align.START)
        img.get_style_context().add_class("cs-thumb")
        return img

    # ---- selection & search ------------------------------------------------

    def _filter(self, row):
        q = self.search.get_text().strip().lower()
        return not q or all(w in row.search_text for w in q.split())

    def _on_search(self, *_):
        self.listbox.invalidate_filter()
        rows = self._visible_rows()
        self._select(rows[0] if rows else None)

    def _visible_rows(self):
        return [r for r in self.listbox.get_children() if self._filter(r)]

    def selected_item(self):
        row = self.listbox.get_selected_row()
        return row.item if row else None

    def _select(self, row):
        if row is None:
            self.listbox.unselect_all()
            return
        self.listbox.select_row(row)
        GLib.idle_add(self._scroll_to, row)

    def _scroll_to(self, row):
        alloc = row.get_allocation()
        adj = self.scroller.get_vadjustment()
        if alloc.y < adj.get_value():
            adj.set_value(alloc.y)
        elif alloc.y + alloc.height > adj.get_value() + adj.get_page_size():
            adj.set_value(alloc.y + alloc.height - adj.get_page_size())
        return False

    def _move(self, step):
        rows = self._visible_rows()
        if not rows:
            return
        cur = self.listbox.get_selected_row()
        i = rows.index(cur) + step if cur in rows else 0
        self._select(rows[max(0, min(len(rows) - 1, i))])

    # ---- keyboard & focus --------------------------------------------------

    def _on_key(self, _w, ev):
        key = ev.keyval
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        item = self.selected_item()
        if key == Gdk.KEY_Escape:
            if self.search.get_text():
                self.search.set_text("")
            else:
                self.hide()
        elif key in (Gdk.KEY_Down, Gdk.KEY_Tab):
            self._move(1)
        elif key in (Gdk.KEY_Up, Gdk.KEY_ISO_Left_Tab):
            self._move(-1)
        elif key == Gdk.KEY_Page_Down:
            self._move(5)
        elif key == Gdk.KEY_Page_Up:
            self._move(-5)
        elif key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if item:
                self.app.paste(item)
        elif key == Gdk.KEY_Delete and not self.search.get_text() and item:
            self._move(1) if item is not self._visible_rows()[-1].item else self._move(-1)
            self.history.delete(item)
        elif ctrl and key in (Gdk.KEY_p, Gdk.KEY_P) and item:
            self.history.toggle_pin(item)
        else:
            return False
        return True

    def _on_focus_out(self, *_):
        # The window manager can bounce focus right after showing; ignore that.
        if time.monotonic() - self.shown_at > 0.4:
            self.hide()
        return False

    # ---- showing -----------------------------------------------------------

    def popup(self):
        if self.dirty:
            self.rebuild()
        self.search.set_text("")
        rows = self._visible_rows()
        self._select(rows[0] if rows else None)
        self.scroller.get_vadjustment().set_value(0)
        self._place_near_pointer()
        self.shown_at = time.monotonic()
        self.show()
        ts = GdkX11.x11_get_server_time(self.get_window())
        self.present_with_time(ts)
        self.get_window().focus(ts)
        self.search.grab_focus()

    def _place_near_pointer(self):
        display = Gdk.Display.get_default()
        _screen, px, py = display.get_default_seat().get_pointer().get_position()
        area = display.get_monitor_at_point(px, py).get_workarea()
        w, h = self.cfg["width"], self.cfg["height"]
        x = min(max(px - 24, area.x + 8), area.x + area.width - w - 8)
        y = py + 16 if py + 16 + h <= area.y + area.height else py - h - 16
        y = min(max(y, area.y + 8), area.y + area.height - h - 8)
        self.move(x, y)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.popup_win = None
        self.target_window = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.cfg = load_config()
        self.history = History(self.cfg)
        self.watcher = Watcher(self.cfg, self.history)
        self.keyboard = Keyboard()
        self.terminals = TERMINALS | {t.lower() for t in self.cfg["extra_terminals"]}
        self.popup_win = Popup(self)
        self.add_window(self.popup_win)
        self.history.on_change = self._on_history_change
        for name, cb in (("show", lambda *_: self.toggle()),
                         ("clear", lambda *_: self.history.clear_unpinned()),
                         ("quit", lambda *_: self.quit())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
        self.hold()  # keep running in the background with no window open

    def do_shutdown(self):
        self.history.flush()
        Gtk.Application.do_shutdown(self)

    def do_command_line(self, command_line):
        args = command_line.get_arguments()[1:]
        if "--version" in args:
            command_line.print_(f"ClipStack {VERSION}\n")
        elif "--quit" in args:
            self.quit()
        elif "--clear" in args:
            self.history.clear_unpinned()
        elif "--config" in args:
            subprocess.Popen(["xdg-open", CONFIG_FILE])
        elif "--daemon" not in args:
            self.toggle()
        return 0

    def _on_history_change(self):
        win = self.popup_win
        win.dirty = True
        if win.get_visible():
            win.rebuild()

    def toggle(self):
        if self.popup_win.get_visible():
            self.popup_win.hide()
            return
        with warnings.catch_warnings():  # deprecated, but still the simplest EWMH lookup
            warnings.simplefilter("ignore", DeprecationWarning)
            self.target_window = Gdk.Screen.get_default().get_active_window()
        self.popup_win.popup()

    def paste(self, item):
        self.watcher.put(item)
        if not self.cfg["pinned_on_top"] or not item.get("pinned"):
            self.history.bump(item)
        self.popup_win.hide()
        target = self.target_window
        if target is not None:
            try:
                target.focus(GdkX11.x11_get_server_time(self.popup_win.get_window()))
            except Exception:
                pass
        keys = ("Control_L", "v")
        if self.cfg["smart_terminal_paste"] and item["kind"] == "text" \
                and self._is_terminal(target):
            keys = ("Control_L", "Shift_L", "v")
        GLib.timeout_add(90, self._send_paste, keys, time.monotonic() + 2.0)

    def _send_paste(self, keys, deadline):
        # Wait until the user lets go of Super/Ctrl/... or they'd mix into Ctrl+V.
        keymap = Gdk.Keymap.get_for_display(Gdk.Display.get_default())
        if keymap.get_modifier_state() & MODIFIERS and time.monotonic() < deadline:
            return True
        self.keyboard.combo(*keys)
        if os.environ.get("CLIPSTACK_DEBUG"):
            active = Gdk.Screen.get_default().get_active_window()
            print("paste", keys, "target", self.target_window and self.target_window.get_xid(),
                  "active", active and active.get_xid(), file=sys.stderr)
        return False

    def _is_terminal(self, window):
        if window is None:
            return False
        try:
            out = subprocess.run(["xprop", "-id", str(window.get_xid()), "WM_CLASS"],
                                 capture_output=True, text=True, timeout=1).stdout
        except (OSError, subprocess.SubprocessError, AttributeError):
            return False
        classes = {c.strip().strip('"').lower() for c in out.partition("=")[2].split(",")}
        return bool(classes & self.terminals)


def main():
    GLib.set_prgname("clipstack")
    GLib.set_application_name("Clipboard History")
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
