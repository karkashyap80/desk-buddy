#!/usr/bin/env python3
"""Desk Buddy: browser / CPU / RAM usage in the top bar + water and walk reminders.

One program, one file. It runs in the background after login and:
  * shows "Chr 12%  Fx 3%  CPU 20%  RAM 45%" in the top bar (tray indicator),
    or in a small floating bar where the desktop has no tray;
  * pops up a "drink water" card and a "walk 2 minutes" card, each on its own
    schedule (default: water every 30 min, walk every hour, 11:00 to 20:00).

Commands (run with: python3 desk_buddy.py <command>):
  install [--yes] [--floating]   install packages, copy the app, start at login
  uninstall [--purge]            remove the app (--purge also deletes settings)
  run [--floating]               start the app (this is what login runs)
  stop                           stop the running app
  settings                       open the reminder settings window
  test [water|walk]              show a reminder card now
  usage                          print one usage reading in the terminal
  status                         is it installed / running, what is next

Linux only (GTK 3). Needs no pip packages; see README.md.
"""
import fcntl
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time

APP = "desk-buddy"
NAME = "Desk Buddy"
HOME = os.path.expanduser("~")
DATA_HOME = os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local", "share"))
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config"))
INSTALL_DIR = os.path.join(DATA_HOME, APP)
INSTALLED_SCRIPT = os.path.join(INSTALL_DIR, "desk_buddy.py")
AUTOSTART_FILE = os.path.join(CONFIG_HOME, "autostart", f"{APP}.desktop")
MENU_FILE = os.path.join(DATA_HOME, "applications", f"{APP}.desktop")
CONFIG_DIR = os.path.join(CONFIG_HOME, APP)
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")
# Per-user folder only: a shared /tmp would let another user plant the lock file.
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), APP)
LOCK_FILE = os.path.join(RUNTIME_DIR, f"{APP}.lock")
PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable

REFRESH_SECONDS = 3
SLOT_SECONDS = 15 * 60  # reminders line up on :00, :15, :30, :45
CARD_W, CARD_H = 500, 120
MARGIN = 14
SLIDE_SECONDS = 4.0

KINDS = ("water", "walk")
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
DEFAULTS = {
    "water": {"on": True, "start_hour": 11, "end_hour": 20, "every_minutes": 30, "days": ALL_DAYS},
    "walk": {"on": True, "start_hour": 11, "end_hour": 20, "every_minutes": 60, "days": ALL_DAYS},
}
LABELS = {"water": "💧 Water", "walk": "🚶 Walk"}
TEXT = {  # (title, body) for each combination of due reminders
    ("water",): ("Water break 💧", "Drink a glass of water."),
    ("walk",): ("Walk break 🚶", "Stand up and walk\nfor 2 minutes."),
    ("water", "walk"): ("Time to move 💧🚶", "Drink a glass of water\nand walk for 2 minutes."),
}
INTERVALS = [15, 30, 45, 60, 90, 120]  # multiples of SLOT_SECONDS
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
BROWSERS = {  # key: (short label, menu name, executable names)
    "chrome": ("Chr", "Chrome", ("chrome", "chromium", "chromium-browser")),
    "firefox": ("Fx", "Firefox", ("firefox", "firefox-bin", "firefox-esr")),
}

# Filled in by load_gtk(), so the non-GUI commands work before packages exist.
Gtk = Gdk = GLib = AppIndicator = None


# ---------------------------------------------------------------- settings

def hour_label(h):
    return time.strftime("%I:%M %p", time.struct_time((2000, 1, 1, h, 0, 0, 0, 1, -1)))


def load_config():
    cfg = {kind: dict(rule) for kind, rule in DEFAULTS.items()}
    try:
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
        for kind in KINDS:
            if isinstance(saved.get(kind), dict):
                cfg[kind].update({k: v for k, v in saved[kind].items() if k in DEFAULTS[kind]})
    except (OSError, ValueError, AttributeError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def is_due(rule, now):
    """True on the 15-minute tick that starts one of this reminder's slots."""
    if not rule["on"] or now.tm_wday not in rule["days"]:
        return False
    minute = now.tm_hour * 60 + now.tm_min // 15 * 15
    since = minute - rule["start_hour"] * 60
    return 0 <= since and minute <= rule["end_hour"] * 60 and since % rule["every_minutes"] == 0


def due_kinds(cfg, now):
    return tuple(kind for kind in KINDS if is_due(cfg[kind], now))


def next_due(rule, after):
    """Epoch seconds of this reminder's next slot after `after`, or None."""
    t = int(after // SLOT_SECONDS) * SLOT_SECONDS + SLOT_SECONDS
    for _ in range(8 * 24 * 4):  # look up to 8 days ahead
        if is_due(rule, time.localtime(t)):
            return t
        t += SLOT_SECONDS
    return None


def when(t):
    if t is None:
        return "off"
    if time.localtime(t).tm_yday == time.localtime().tm_yday:
        return time.strftime("%I:%M %p", time.localtime(t))
    return time.strftime("%a %I:%M %p", time.localtime(t))


def summary(rule):
    if not rule["on"]:
        return "Off."
    if not rule["days"]:
        return "No days selected: off."
    count = (rule["end_hour"] - rule["start_hour"]) * 60 // rule["every_minutes"] + 1
    days = "every day" if len(rule["days"]) == 7 else ", ".join(DAY_NAMES[d] for d in sorted(rule["days"]))
    return (f"{hour_label(rule['start_hour'])} to {hour_label(rule['end_hour'])}, "
            f"every {rule['every_minutes']} min: {count} a day, {days}.")


# ------------------------------------------------------------------- usage

class Usage:
    """Machine CPU / RAM and per-browser CPU / RAM, read straight from /proc.

    CPU % is a share of the whole machine (100 % = every core busy), the same
    way System Monitor's total works. Browser RAM is resident minus shared
    pages summed over the browser's processes, so it is an estimate.
    """

    def __init__(self):
        self.tck = os.sysconf("SC_CLK_TCK")
        self.page = os.sysconf("SC_PAGE_SIZE")
        self.cores = os.cpu_count() or 1
        self.kinds = {}       # pid -> browser key or None (cached per pid)
        self.last_ticks = {}  # pid -> utime + stime at the previous sample
        self.last_time = None
        self.last_cpu = None  # (busy, total) jiffies from /proc/stat

    def classify(self, pid):
        try:  # only our own processes are readable, which is all we need
            exe = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
        except OSError:
            return None
        for key, (_, _, names) in BROWSERS.items():
            if exe in names:
                return key
        return None

    def machine_cpu(self):
        with open("/proc/stat") as f:
            values = [int(v) for v in f.readline().split()[1:9]]
        idle = values[3] + values[4]
        total = sum(values)
        prev, self.last_cpu = self.last_cpu, (total - idle, total)
        if prev is None or total <= prev[1]:
            return None
        return (total - idle - prev[0]) / (total - prev[1]) * 100

    @staticmethod
    def memory():
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, value = line.split(":", 1)
                info[key] = int(value.split()[0]) * 1024
        total = info["MemTotal"]
        return total - info.get("MemAvailable", info.get("MemFree", 0)), total

    def sample(self):
        now = time.monotonic()
        elapsed = now - self.last_time if self.last_time else 0
        self.last_time = now
        browsers = {key: {"running": False, "ticks": 0, "ram": 0} for key in BROWSERS}
        seen = set()
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            seen.add(name)
            if name not in self.kinds:
                self.kinds[name] = self.classify(name)
            key = self.kinds[name]
            if key is None:
                continue
            try:
                with open(f"/proc/{name}/stat") as f:
                    fields = f.read().rsplit(")", 1)[1].split()  # comm may hold spaces
                with open(f"/proc/{name}/statm") as f:
                    resident, shared = (int(v) for v in f.read().split()[1:3])
            except (OSError, IndexError, ValueError):
                continue
            ticks = int(fields[11]) + int(fields[12])  # utime + stime
            b = browsers[key]
            b["running"] = True
            b["ram"] += max(resident - shared, 0) * self.page
            prev = self.last_ticks.get(name)
            if prev is not None and ticks >= prev:
                b["ticks"] += ticks - prev
            self.last_ticks[name] = ticks
        for pid in list(self.kinds):  # forget exited processes (PIDs get reused)
            if pid not in seen:
                self.kinds.pop(pid, None)
                self.last_ticks.pop(pid, None)

        for b in browsers.values():
            b["cpu"] = b["ticks"] / self.tck / (elapsed * self.cores) * 100 if b["running"] and elapsed else None
        ram_used, ram_total = self.memory()
        return {"cpu": self.machine_cpu(), "ram_used": ram_used, "ram_total": ram_total, "browsers": browsers}


def pct(value):
    return "…" if value is None else f"{round(value)}%"


def gb(n):
    return f"{n / 1024 ** 3:.1f} GB"


def bar_text(s):
    parts = []
    for key, (short, _, _) in BROWSERS.items():
        b = s["browsers"][key]
        parts.append(f"{short} {pct(b['cpu']) if b['running'] else '–'}")
    parts.append(f"CPU {pct(s['cpu'])}")
    parts.append(f"RAM {round(s['ram_used'] / s['ram_total'] * 100)}%")
    return "  ".join(parts)


def detail_lines(s):
    lines = []
    for key, (_, name, _) in BROWSERS.items():
        b = s["browsers"][key]
        lines.append(f"{name}: CPU {pct(b['cpu'])} · RAM ~{gb(b['ram'])}" if b["running"] else f"{name}: not running")
    lines.append(f"CPU: {pct(s['cpu'])} of {os.cpu_count()} cores")
    lines.append(f"RAM: {gb(s['ram_used'])} of {gb(s['ram_total'])} "
                 f"({round(s['ram_used'] / s['ram_total'] * 100)}%)")
    return lines


# --------------------------------------------------------------------- GUI

def load_gtk():
    global Gtk, Gdk, GLib, AppIndicator
    # Wayland does not let apps place their own windows; XWayland does.
    os.environ["GDK_BACKEND"] = "x11"
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk as _Gdk, GLib as _GLib, Gtk as _Gtk
    except (ImportError, ValueError) as e:
        sys.exit(f"{NAME}: GTK 3 for Python is missing ({e}).\n"
                 f"Run: python3 {os.path.basename(__file__)} install")
    Gtk, Gdk, GLib = _Gtk, _Gdk, _GLib
    for name in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(name, "0.1")
            AppIndicator = getattr(__import__("gi.repository", fromlist=[name]), name)
            break
        except (ImportError, ValueError, AttributeError):
            continue
    css = Gtk.CssProvider()
    css.load_from_data(b"""
        #card, #floatbar { background-color: transparent; }
        #card > box { background-color: #e8f6ff; border-radius: 16px; border: 2px solid #5fb4e6; }
        #card button { background-image: none; background-color: #1f8fd6; color: white;
                       font-weight: bold; border-radius: 10px; padding: 8px 6px; border: none; }
        #card button:hover { background-color: #136fab; }
        #card button.gear { background-color: transparent; color: #1f8fd6; font-size: 18px; padding: 4px 8px; }
        #card button.gear:hover { background-color: #cfe9fa; }
        #floatbar > * { background-color: rgba(20, 30, 40, 0.85); border-radius: 8px; }
        #floatbar label { color: #f2f6f8; font-family: monospace; padding: 4px 10px; }
    """)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def work_area():
    display = Gdk.Display.get_default()
    return (display.get_primary_monitor() or display.get_monitor(0)).get_workarea()


class RuleEditor:
    """The on switch, times and days for one reminder, laid out in a grid."""

    def __init__(self, grid, rule):
        self.on = Gtk.Switch(halign=Gtk.Align.START)
        self.on.set_active(rule["on"])
        self.start = Gtk.ComboBoxText()
        self.end = Gtk.ComboBoxText()
        for h in range(24):
            self.start.append(str(h), hour_label(h))
            self.end.append(str(h), hour_label(h))
        self.start.set_active_id(str(rule["start_hour"]))
        self.end.set_active_id(str(rule["end_hour"]))
        self.every = Gtk.ComboBoxText()
        for m in INTERVALS:
            self.every.append(str(m), f"{m} minutes" if m < 60 else f"{m / 60:g} hour{'s' if m > 60 else ''}")
        if not self.every.set_active_id(str(rule["every_minutes"])):
            self.every.set_active_id("60")
        days = Gtk.Box(spacing=6)
        self.day_checks = []
        for i, name in enumerate(DAY_NAMES):
            check = Gtk.CheckButton(label=name)
            check.set_active(i in rule["days"])
            days.pack_start(check, False, False, 0)
            self.day_checks.append(check)
        for row, (label, widget) in enumerate([
            ("On", self.on), ("First reminder", self.start), ("Last reminder", self.end),
            ("Every", self.every), ("Days", days),
        ]):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
        self.summary = Gtk.Label(label=summary(rule), xalign=0)
        self.summary.set_line_wrap(True)
        self.summary.set_max_width_chars(48)
        grid.attach(self.summary, 0, 5, 2, 1)

    def value(self):
        return {
            "on": self.on.get_active(),
            "start_hour": int(self.start.get_active_id()),
            "end_hour": int(self.end.get_active_id()),
            "every_minutes": int(self.every.get_active_id()),
            "days": [i for i, c in enumerate(self.day_checks) if c.get_active()],
        }


class Settings:
    def __init__(self, on_close):
        self.win = Gtk.Window(title=f"{NAME}: reminder settings")
        self.win.set_keep_above(True)
        self.win.set_position(Gtk.WindowPosition.CENTER)
        self.win.set_resizable(False)
        self.win.connect("destroy", lambda *_: on_close())
        cfg = load_config()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_border_width(16)
        self.win.add(outer)
        self.editors = {}
        for kind in KINDS:
            frame = Gtk.Frame(label=LABELS[kind])
            grid = Gtk.Grid(row_spacing=8, column_spacing=12)
            grid.set_border_width(10)
            frame.add(grid)
            outer.pack_start(frame, False, False, 0)
            self.editors[kind] = RuleEditor(grid, cfg[kind])
        self.status = Gtk.Label(xalign=0)
        outer.pack_start(self.status, False, False, 0)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.win.destroy())
        save = Gtk.Button(label="Save")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", self.on_save)
        buttons.pack_start(close, False, False, 0)
        buttons.pack_start(save, False, False, 0)
        outer.pack_start(buttons, False, False, 0)
        self.win.show_all()

    def present(self):
        self.win.present()

    def on_save(self, *_):
        cfg = {kind: editor.value() for kind, editor in self.editors.items()}
        bad = [LABELS[k] for k in KINDS if cfg[k]["end_hour"] < cfg[k]["start_hour"]]
        if bad:
            self.status.set_markup(
                f"<span foreground='#b3261e'>{' and '.join(bad)}: last reminder must be after the first one.</span>")
            return
        try:
            save_config(cfg)
        except OSError as e:
            self.status.set_markup(f"<span foreground='#b3261e'>Could not save: {GLib.markup_escape_text(str(e))}</span>")
            return
        for kind, editor in self.editors.items():
            editor.summary.set_text(summary(cfg[kind]))
        self.status.set_markup("<b>Saved.</b> New times apply from the next reminder.")


class Card:
    """The reminder card: rises up the right edge, settles top-right, waits for Done."""

    def __init__(self, kinds, on_done, on_gear):
        self.kinds = kinds
        self.on_done = on_done
        self.win = Gtk.Window(type=Gtk.WindowType.POPUP)  # no frame, stays on top
        self.win.set_name("card")
        self.win.set_default_size(CARD_W, CARD_H)
        self.win.set_keep_above(True)
        self.win.set_app_paintable(True)
        visual = self.win.get_screen().get_rgba_visual()
        if visual is not None:
            self.win.set_visual(visual)
        area = work_area()
        self.x = area.x + area.width - CARD_W - MARGIN
        self.start_y = area.y + area.height  # just below the bottom edge
        self.end_y = area.y + MARGIN
        self.t0 = time.monotonic()
        self.phase = 0.0

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_border_width(14)
        self.win.add(box)
        self.art = Gtk.DrawingArea()
        self.art.set_size_request(70, 90)
        self.art.connect("draw", self.draw)
        box.pack_start(self.art, False, False, 0)

        heading, message = TEXT[kinds]
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for markup in (f"<span size='large' weight='bold' foreground='#0b3d5c'>{heading}</span>",
                       f"<span foreground='#24506b'>{message}</span>",
                       f"<span size='small' foreground='#6b8796'>{time.strftime('%I:%M %p')}</span>"):
            label = Gtk.Label(xalign=0)
            label.set_markup(markup)
            text.pack_start(label, False, False, 0)
        box.pack_start(text, True, True, 0)

        done = Gtk.Button(label="  Done ✓  ")
        done.set_valign(Gtk.Align.CENTER)
        done.connect("clicked", lambda *_: self.close())
        box.pack_end(done, False, False, 0)
        gear = Gtk.Button(label="⚙")
        gear.set_tooltip_text("Set reminder times")
        gear.set_valign(Gtk.Align.CENTER)
        gear.get_style_context().add_class("gear")
        gear.connect("clicked", lambda *_: on_gear())
        box.pack_end(gear, False, False, 0)

        self.win.move(self.x, self.start_y)
        self.win.show_all()
        self.timer = GLib.timeout_add(16, self.tick)

    def close(self):
        GLib.source_remove(self.timer)
        self.win.destroy()
        self.on_done()

    def tick(self):
        t = min((time.monotonic() - self.t0) / SLIDE_SECONDS, 1.0)
        self.win.move(self.x, int(self.start_y + (self.end_y - self.start_y) * (1 - (1 - t) ** 3)))
        # Brisk steps while sliding in, gentle marching in place after.
        self.phase += 0.22 if t < 1.0 else 0.12
        self.art.queue_draw()
        return True

    @staticmethod
    def drop(cr, dx, dy, scale):
        cr.set_source_rgb(0.37, 0.71, 0.90)
        cr.move_to(dx, dy - 9 * scale)
        cr.curve_to(dx + 7 * scale, dy, dx + 7 * scale, dy + 8 * scale, dx, dy + 8 * scale)
        cr.curve_to(dx - 7 * scale, dy + 8 * scale, dx - 7 * scale, dy, dx, dy - 9 * scale)
        cr.fill()

    def draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        if "walk" not in self.kinds:  # water only: a big, gently bobbing drop
            self.drop(cr, w / 2, h / 2 + math.sin(self.phase) * 3, 3.2)
            return False
        s = math.sin(self.phase)
        bob = abs(math.cos(self.phase)) * 2
        cx, hip_y = w / 2, h * 0.58 - bob
        shoulder_y, leg = h * 0.30 - bob, h * 0.34
        cr.set_line_cap(1)  # round
        cr.set_line_width(5)
        cr.set_source_rgb(0.12, 0.45, 0.70)
        cr.arc(cx, h * 0.15 - bob, 8, 0, 2 * math.pi)  # head
        cr.fill()
        cr.move_to(cx, shoulder_y)  # body
        cr.line_to(cx, hip_y)
        cr.stroke()
        for sign in (1, -1):  # legs (knee + foot) and arms swing opposite
            a = sign * s * 0.55
            kx, ky = cx + math.sin(a) * leg * 0.5, hip_y + math.cos(a) * leg * 0.5
            b = a - abs(s) * 0.3 * sign if sign * s < 0 else a
            fx, fy = kx + math.sin(b) * leg * 0.5, ky + math.cos(b) * leg * 0.5
            cr.move_to(cx, hip_y)
            cr.line_to(kx, ky)
            cr.line_to(fx, fy)
            cr.line_to(fx + 5, fy)
            cr.stroke()
            arm = -a * 0.9
            cr.move_to(cx, shoulder_y + 3)
            cr.line_to(cx + math.sin(arm) * leg * 0.55, shoulder_y + 3 + math.cos(arm) * leg * 0.55)
            cr.stroke()
        if "water" in self.kinds:
            self.drop(cr, w - 10, h * 0.12, 1.0)
        return False


class FloatBar:
    """Fallback for desktops without a tray: a small bar in the bottom-right corner."""

    def __init__(self, menu):
        self.menu = menu
        self.win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.win.set_name("floatbar")
        self.win.set_keep_above(True)
        events = Gtk.EventBox()
        self.label = Gtk.Label(label=f"{NAME} …")
        events.add(self.label)
        events.connect("button-press-event", lambda _w, e: self.menu.popup_at_pointer(e))
        self.win.add(events)
        self.win.show_all()

    def set_text(self, text):
        self.label.set_text(text)
        area = work_area()
        width, height = self.win.get_preferred_size()[1].width, self.win.get_preferred_size()[1].height
        self.win.move(area.x + area.width - width - MARGIN, area.y + area.height - height - MARGIN)


class App:
    def __init__(self, floating):
        self.usage = Usage()
        self.card = None
        self.settings = None
        self.paused_until = 0
        self.last_slot = int(time.time() // SLOT_SECONDS)  # no card for a slot already under way at login

        self.menu = Gtk.Menu()
        self.info_items = []
        for _ in range(len(BROWSERS) + 2):
            item = Gtk.MenuItem(label="…")
            item.set_sensitive(False)
            self.menu.append(item)
            self.info_items.append(item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.next_item = Gtk.MenuItem(label="Next: …")
        self.next_item.set_sensitive(False)
        self.menu.append(self.next_item)
        self.add_item("Show a reminder now", lambda: self.show_card(KINDS))
        self.add_item("Reminder settings…", self.open_settings)
        self.pause_item = self.add_item("Pause reminders for 1 hour", self.toggle_pause)
        if shutil.which("gnome-system-monitor"):
            self.add_item("Open System Monitor", lambda: subprocess.Popen(["gnome-system-monitor"]))
        self.menu.append(Gtk.SeparatorMenuItem())
        self.add_item(f"Quit {NAME}", Gtk.main_quit)
        self.menu.show_all()

        if AppIndicator is not None and not floating:
            self.indicator = AppIndicator.Indicator.new(
                APP, "utilities-system-monitor", AppIndicator.IndicatorCategory.SYSTEM_SERVICES)
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_menu(self.menu)
            self.bar = None
        else:
            self.indicator = None
            self.bar = FloatBar(self.menu)

        self.refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.refresh)
        GLib.timeout_add_seconds(20, self.check_reminders)

    def add_item(self, label, action):
        item = Gtk.MenuItem(label=label)
        item.connect("activate", lambda *_: action())
        self.menu.append(item)
        return item

    def refresh(self):
        try:
            s = self.usage.sample()
        except OSError:
            return True
        text = bar_text(s)
        if self.indicator is not None:
            self.indicator.set_label(text, "Chr 100%  Fx 100%  CPU 100%  RAM 100%")
        else:
            self.bar.set_text(text)
        for item, line in zip(self.info_items, detail_lines(s)):
            item.set_label(line)
        now = time.time()
        if now < self.paused_until:
            self.next_item.set_label(f"Reminders paused until {when(self.paused_until)}")
            self.pause_item.set_label("Resume reminders")
        else:
            cfg = load_config()
            self.next_item.set_label("Next: " + ", ".join(
                f"{kind} {when(next_due(cfg[kind], now))}" for kind in KINDS))
            self.pause_item.set_label("Pause reminders for 1 hour")
        return True

    def check_reminders(self):
        slot = int(time.time() // SLOT_SECONDS)
        if slot != self.last_slot:
            self.last_slot = slot
            if time.time() >= self.paused_until:
                kinds = due_kinds(load_config(), time.localtime(slot * SLOT_SECONDS))
                if kinds:
                    self.show_card(kinds)
        return True

    def show_card(self, kinds):
        if self.card is None:  # one card at a time; the next comes at its own slot
            self.card = Card(kinds, on_done=self.card_closed, on_gear=self.open_settings)

    def card_closed(self):
        self.card = None

    def open_settings(self):
        if self.settings is None:
            self.settings = Settings(on_close=lambda: setattr(self, "settings", None))
        self.settings.present()

    def toggle_pause(self):
        self.paused_until = 0 if time.time() < self.paused_until else time.time() + 3600
        self.refresh()


# ---------------------------------------------------------------- commands

def take_lock():
    """Hold the single-instance lock for this process's lifetime; False if taken."""
    global _lock
    os.makedirs(RUNTIME_DIR, mode=0o700, exist_ok=True)
    _lock = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _lock.seek(0)
    _lock.truncate()
    _lock.write(str(os.getpid()))
    _lock.flush()
    return True


def running_pid():
    try:
        with open(LOCK_FILE) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return None  # nobody holds it
            except OSError:
                return int(f.read().strip() or 0) or None
    except OSError:
        return None


def cmd_run(args):
    if not take_lock():
        if "--open" in args:  # app-menu launch while running: show the settings
            return cmd_settings(args)
        print(f"{NAME} is already running.")
        return
    load_gtk()
    for sig in (signal.SIGTERM, signal.SIGINT):  # `stop`, logout and Ctrl+C quit cleanly
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: Gtk.main_quit() or False)
    App(floating="--floating" in args)
    Gtk.main()


def cmd_settings(_args):
    load_gtk()
    Settings(on_close=Gtk.main_quit)
    Gtk.main()


def cmd_test(args):
    load_gtk()
    kinds = tuple(k for k in KINDS if k in args) or KINDS
    Card(kinds, on_done=Gtk.main_quit, on_gear=lambda: Settings(on_close=lambda: None))
    Gtk.main()


def cmd_usage(_args):
    usage = Usage()
    usage.sample()
    time.sleep(1.5)
    s = usage.sample()
    print(bar_text(s))
    for line in detail_lines(s):
        print("  " + line)


def cmd_stop(_args):
    pid = running_pid()
    if pid is None:
        print(f"{NAME} is not running.")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if running_pid() is None:
            print(f"Stopped {NAME} (pid {pid}).")
            return
        time.sleep(0.1)
    print(f"Asked {NAME} (pid {pid}) to stop; it has not exited yet.")


def cmd_status(_args):
    pid = running_pid()
    print(f"Installed:  {'yes, ' + INSTALLED_SCRIPT if os.path.exists(INSTALLED_SCRIPT) else 'no'}")
    print(f"Autostart:  {'yes' if os.path.exists(AUTOSTART_FILE) else 'no'}")
    print(f"Running:    {f'yes (pid {pid})' if pid else 'no'}")
    print(f"Settings:   {CONFIG_FILE if os.path.exists(CONFIG_FILE) else 'defaults (nothing saved yet)'}")
    cfg = load_config()
    for kind in KINDS:
        print(f"  {kind:5}  {summary(cfg[kind])}  Next: {when(next_due(cfg[kind], time.time()))}")


# Package names per package manager: GTK 3 + Python bindings + cairo + tray indicator.
PACKAGES = {
    "apt-get": (["python3-gi", "python3-gi-cairo", "gir1.2-gtk-3.0", "gir1.2-ayatanaappindicator3-0.1"],
                ["gnome-shell-extension-appindicator"]),
    "dnf": (["python3-gobject", "python3-cairo", "gtk3", "libayatana-appindicator-gtk3"],
            ["gnome-shell-extension-appindicator"]),
    "pacman": (["python-gobject", "python-cairo", "gtk3", "libayatana-appindicator"],
               ["gnome-shell-extension-appindicator"]),
    "zypper": (["python3-gobject", "python3-gobject-cairo", "python3-gobject-Gdk", "typelib-1_0-Gtk-3_0",
                "typelib-1_0-AyatanaAppIndicator3-0_1"], ["gnome-shell-extension-appindicator"]),
}
INSTALL_CMD = {"apt-get": ["apt-get", "install", "-y"], "dnf": ["dnf", "install", "-y"],
               "pacman": ["pacman", "-S", "--needed", "--noconfirm"], "zypper": ["zypper", "install", "-y"]}
CHECK_CMD = {"apt-get": ["dpkg-query", "-W", "-f=${Status}"], "dnf": ["rpm", "-q"],
             "pacman": ["pacman", "-Q"], "zypper": ["rpm", "-q"]}
TRAY_EXTENSIONS = ("ubuntu-appindicators@ubuntu.com", "appindicatorsupport@rgcjonas.gmail.com")


def is_installed(manager, package):
    result = subprocess.run(CHECK_CMD[manager] + [package], capture_output=True, text=True)
    if manager == "apt-get":
        return "install ok installed" in result.stdout
    return result.returncode == 0


def gtk_check(python):
    code = ("import gi; gi.require_version('Gtk', '3.0'); gi.require_foreign('cairo'); "
            "from gi.repository import Gtk; ok = 0\n"
            "for n in ('AyatanaAppIndicator3', 'AppIndicator3'):\n"
            "    try:\n        gi.require_version(n, '0.1'); ok = 1; break\n"
            "    except ValueError: pass\n"
            "print('tray' if ok else 'notray')")
    result = subprocess.run([python, "-c", code], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def write_desktop(path, name, comment, exec_args):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("[Desktop Entry]\nType=Application\n"
                f"Name={name}\nComment={comment}\n"
                f"Exec={PYTHON} {INSTALLED_SCRIPT} {exec_args}\n"
                "Icon=utilities-system-monitor\nTerminal=false\nCategories=Utility;\n"
                "X-GNOME-Autostart-enabled=true\n")


def cmd_install(args):
    if not sys.platform.startswith("linux"):
        sys.exit(f"{NAME} works on Linux only (it uses GTK 3 and /proc).")
    gnome = "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    print(f"== {NAME} install ==\n")

    # 1. System packages
    manager = next((m for m in PACKAGES if shutil.which(m)), None)
    if manager is None:
        print("1. Packages: unknown package manager. Install these yourself: GTK 3, PyGObject (python3-gi),\n"
              "   its cairo support, and the Ayatana AppIndicator library. Then run install again.")
    else:
        base, gnome_extra = PACKAGES[manager]
        wanted = base + (gnome_extra if gnome else [])
        missing = [p for p in wanted if not is_installed(manager, p)]
        if not missing:
            print("1. Packages: all present.")
        else:
            command = ["sudo"] + INSTALL_CMD[manager] + missing
            print(f"1. Packages missing: {' '.join(missing)}\n   Will run: {' '.join(command)}")
            if "--yes" in args or input("   Install them now? [Y/n] ").strip().lower() in ("", "y", "yes"):
                if manager == "apt-get":
                    subprocess.run(["sudo", "apt-get", "update"], check=False)
                if subprocess.run(command).returncode != 0:
                    print("   Package install failed; see the messages above. Continuing with the rest.")
            else:
                print("   Skipped. The app needs them; run install again later.")

    # 2. Check GTK actually loads with the python the app will use
    check = gtk_check(PYTHON)
    if check is None:
        sys.exit(f"\n2. GTK check: {PYTHON} cannot load GTK 3 + cairo. Fix the packages above and run install again.")
    floating = "--floating" in args or check == "notray"
    if not floating:
        mode = "usage shows in the top bar"
    elif check == "notray":
        mode = "no tray library, so usage shows in a floating bar"
    else:
        mode = "usage shows in a floating bar, as asked"
    print(f"2. GTK check: OK, {mode}.")

    # 3. GNOME needs its tray extension switched on to show indicators
    if gnome and not floating and shutil.which("gnome-extensions"):
        listed = subprocess.run(["gnome-extensions", "list"], capture_output=True, text=True).stdout.split()
        enabled = subprocess.run(["gnome-extensions", "list", "--enabled"], capture_output=True, text=True).stdout.split()
        ext = next((e for e in TRAY_EXTENSIONS if e in listed), None)
        if ext is None:
            print("3. GNOME tray extension: just installed or not found. Log out and back in once so GNOME\n"
                  "   sees it; until then the top-bar text may not show (or use: install --floating).")
        elif ext in enabled:
            print(f"3. GNOME tray extension: on ({ext}).")
        else:
            subprocess.run(["gnome-extensions", "enable", ext])
            print(f"3. GNOME tray extension: switched on ({ext}).")
    else:
        print("3. Tray extension: not needed on this desktop.")

    # 4. Copy the app and set it to start at login
    os.makedirs(INSTALL_DIR, exist_ok=True)
    if os.path.abspath(__file__) != INSTALLED_SCRIPT:
        shutil.copy2(os.path.abspath(__file__), INSTALLED_SCRIPT)
    run_args = "run --floating" if floating else "run"
    write_desktop(AUTOSTART_FILE, NAME, "Browser, CPU and RAM usage + water and walk reminders", run_args)
    write_desktop(MENU_FILE, NAME, "Start Desk Buddy, or open its reminder settings", run_args + " --open")
    print(f"4. Installed to {INSTALLED_SCRIPT}\n   Starts at login ({AUTOSTART_FILE})\n"
          f"   App menu entry: \"{NAME}\" (opens the settings when it is already running)")

    # 5. Start (or restart, after an upgrade) now
    if "--no-start" not in args:
        if running_pid():
            cmd_stop([])
        subprocess.Popen([PYTHON, INSTALLED_SCRIPT] + run_args.split(), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"5. Started. Try a card now:  python3 {INSTALLED_SCRIPT} test")
    print(f"\nDone. Settings: open \"{NAME}\" from the app menu, or click the top-bar text > Reminder settings.")


def cmd_uninstall(args):
    if running_pid():
        cmd_stop([])
    for path in (AUTOSTART_FILE, MENU_FILE):
        if os.path.exists(path):
            os.remove(path)
            print(f"Removed {path}")
    if os.path.isdir(INSTALL_DIR):
        shutil.rmtree(INSTALL_DIR)
        print(f"Removed {INSTALL_DIR}")
    if "--purge" in args and os.path.isdir(CONFIG_DIR):
        shutil.rmtree(CONFIG_DIR)
        print(f"Removed settings {CONFIG_DIR}")
    elif os.path.isdir(CONFIG_DIR):
        print(f"Kept your settings in {CONFIG_DIR} (use --purge to delete them).")
    print("System packages were left installed; they are shared with other apps.")


COMMANDS = {"install": cmd_install, "uninstall": cmd_uninstall, "run": cmd_run, "stop": cmd_stop,
            "settings": cmd_settings, "test": cmd_test, "usage": cmd_usage, "status": cmd_status}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(0 if len(sys.argv) < 2 else 2)
    COMMANDS[sys.argv[1]](sys.argv[2:])
