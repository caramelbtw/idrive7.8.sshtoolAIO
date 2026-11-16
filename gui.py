"""MGU Shell — BMW iDrive SSH Tool

Clean two-tab GUI: Setup (flash/enable/disable) and Shell (SSH terminal).
Drives ssh_enable.py on a background thread; connects to enrolled units
via the system OpenSSH client.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

if getattr(sys, "frozen", False):
    HERE = os.path.dirname(os.path.abspath(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


class _NullWriter:
    def write(self, *_):
        pass

    def flush(self):
        pass


if sys.stdout is None:
    sys.stdout = _NullWriter()
if sys.stderr is None:
    sys.stderr = _NullWriter()

import ssh_enable
from i18n import Translator
from mgu7 import discovery
from mgu7.flash import ID7, ID8_BDC3, ID8_BCP
from mgu7 import sshcli

ARCHES = (ID7, ID8_BDC3, ID8_BCP)
VARIANT_PROMPT = "Select variant..."
VARIANT_CHOICES = (
    (ID7, "ID7 — BDC2 / BDC3  (030.xxx)"),
    (ID8_BDC3, "ID8 — BDC3  (036.xxx)"),
    (ID8_BCP, "ID8 — BCP  (036.xxx)"),
)
_VARIANT_BY_LABEL = {label: code for code, label in VARIANT_CHOICES}

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
BG       = "#111318"
SURFACE  = "#191d24"
SURFACE2 = "#1f242d"
BORDER   = "#2b3240"

TEXT     = "#e4e8f0"
TEXT2    = "#a0aabb"
TEXT3    = "#6b7688"

BLUE     = "#4a90e2"
BLUE_HI  = "#3a7bd4"
GREEN    = "#3dce8a"
GREEN_DK = "#143024"
GREEN_HI = "#1a4030"
AMBER    = "#e8b84a"
AMBER_DK = "#2a2210"
AMBER_HI = "#372d16"
RED      = "#e85454"
RED_DK   = "#2a1616"
RED_HI   = "#381e1d"

CONSOLE  = "#0c0e12"

FONT     = "Segoe UI"
FONT_B   = "Segoe UI Semibold"
MONO     = "Consolas"

QUICK_ACTIONS = [
    ("System",    "uname -a && cat /etc/os-release 2>/dev/null | head -5"),
    ("Disk",      "df -h"),
    ("Memory",    "free -h 2>/dev/null || cat /proc/meminfo | head -8"),
    ("Network",   "ip addr show 2>/dev/null | grep -E 'inet |link/' || ifconfig 2>/dev/null"),
    ("Files /tmp","ls -la /tmp/"),
    ("Processes", "ps aux 2>/dev/null | head -20 || ps"),
    ("SSHD Conf", "cat /etc/ssh/sshd_config 2>/dev/null | grep -vE '^#|^$' | head -20"),
    ("Auth Keys", "cat /usr/share/sshd/authorized_keys.root 2>/dev/null || echo 'not found'"),
]


class _QueueWriter:
    def __init__(self, q):
        self.q = q
        self.pending = ""

    def write(self, text):
        if text:
            self.pending += text
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                self.q.put(line + "\n")
        return len(text)

    def flush(self):
        if self.pending:
            self.q.put(self.pending)
            self.pending = ""


def scan_enrollments():
    enroll_dir = os.path.join(HERE, "data", "enrollments")
    results = []
    if not os.path.isdir(enroll_dir):
        return results
    for name in sorted(os.listdir(enroll_dir)):
        unit_dir = os.path.join(enroll_dir, name)
        if not os.path.isdir(unit_dir):
            continue
        key_path = os.path.join(unit_dir, "id_rsa.pkcs1.pem")
        if not os.path.isfile(key_path):
            continue
        meta_path = os.path.join(unit_dir, "metadata.json")
        vin = name.upper()
        mgu_ip = ""
        arch = ""
        fingerprint = ""
        created = ""
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                vin = (meta.get("vin") or name).upper()
                mgu_ip = meta.get("mgu_ip") or ""
                arch = meta.get("arch") or ""
                created = meta.get("created_utc") or ""
                fp = meta.get("fingerprints") or {}
                fingerprint = fp.get("sha256", "")
            except (json.JSONDecodeError, OSError):
                pass
        connect_path = os.path.join(unit_dir, "connect.txt")
        if not mgu_ip and os.path.isfile(connect_path):
            try:
                with open(connect_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip().startswith("Host (IP):"):
                            mgu_ip = line.split(":", 1)[1].strip()
                            break
            except OSError:
                pass
        results.append({
            "vin": vin, "ip": mgu_ip, "key": key_path, "dir": unit_dir,
            "arch": arch, "fingerprint": fingerprint, "created": created,
        })
    return results


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.worker = None
        self.i18n = Translator()
        self._labels = []
        self._history = []
        self._is_busy = False
        self._ssh_connected = False
        self._ssh_busy = False

        root.title("MGU Shell")
        root.configure(bg=BG)
        icon_path = os.path.join(HERE, "docs", "icon.ico")
        if os.path.isfile(icon_path):
            try:
                root.iconbitmap(icon_path)
            except tk.TclError:
                pass

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w = min(int(sw * 0.82), 1360)
        h = min(int(sh * 0.82), 860)
        x = (sw - w) // 2
        y = max((sh - h) // 2 - 16, 0)
        root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        root.minsize(860, 560)

        self.vin = tk.StringVar()
        self.gateway = tk.StringVar(value="auto")
        self.mgu_ip = tk.StringVar()
        self.arch = tk.StringVar(value="")
        self.variant_label = tk.StringVar(value=VARIANT_PROMPT)
        self.disable_debug = tk.BooleanVar(value=True)
        self.unique_key = tk.BooleanVar(value=False)

        self._apply_theme()
        self._build_ui()
        self.root.after(100, self._drain)

    def _apply_theme(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass

        s.configure(".", background=BG, foreground=TEXT, font=(FONT, 10))
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=TEXT, font=(FONT, 10))
        s.configure("Dim.TLabel", background=SURFACE, foreground=TEXT3, font=(FONT, 8))
        s.configure("SurfLabel.TLabel", background=SURFACE, foreground=TEXT, font=(FONT, 10))

        s.configure("App.TNotebook", background=BG, borderwidth=0,
                    tabmargins=[0, 0, 0, 0])
        s.configure("App.TNotebook.Tab", background=BG, foreground=TEXT3,
                    font=(FONT, 11), padding=(28, 10), borderwidth=0,
                    expand=[0, 0, 0, 0])
        s.map("App.TNotebook.Tab",
              background=[("selected", BG), ("active", BG)],
              foreground=[("selected", TEXT), ("active", TEXT2)],
              font=[("selected", (FONT, 11)), ("!selected", (FONT, 11))],
              padding=[("selected", (28, 10)), ("!selected", (28, 10))],
              expand=[("selected", [0, 0, 0, 0])])

        s.configure("TCombobox", fieldbackground=SURFACE2, background=SURFACE2,
                    foreground=TEXT, arrowcolor=TEXT2, bordercolor=BORDER,
                    lightcolor=SURFACE2, darkcolor=SURFACE2,
                    selectbackground=SURFACE2, selectforeground=TEXT,
                    padding=(8, 5))
        s.map("TCombobox",
              fieldbackground=[("readonly", SURFACE2)],
              foreground=[("readonly", TEXT)],
              arrowcolor=[("active", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", SURFACE)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", BLUE)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#fff")
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 10))

        s.configure("TMenubutton", background=SURFACE2, foreground=TEXT,
                    font=(FONT, 10), padding=(10, 6), relief="flat",
                    borderwidth=0, arrowcolor=TEXT2, focuscolor=SURFACE2)
        s.map("TMenubutton",
              background=[("active", BORDER), ("disabled", SURFACE)],
              foreground=[("disabled", TEXT3)])

        s.configure("Surf.TCheckbutton", background=SURFACE, foreground=TEXT,
                    font=(FONT, 9), focuscolor=SURFACE)
        try:
            s.configure("Surf.TCheckbutton", indicatorbackground=BG,
                        indicatorforeground="#fff", bordercolor=BORDER,
                        indicatorrelief="flat")
            s.map("Surf.TCheckbutton",
                  background=[("active", SURFACE)],
                  indicatorbackground=[("selected", BLUE), ("active", SURFACE2)])
        except tk.TclError:
            pass

        def btn(name, bg, hover, fg="#fff", font=(FONT, 10, "bold"), pad=(14, 9)):
            s.configure(name, background=bg, foreground=fg, font=font,
                        padding=pad, borderwidth=0, relief="flat", focuscolor=bg)
            s.map(name,
                  background=[("active", hover), ("disabled", SURFACE)],
                  foreground=[("disabled", TEXT3)])

        btn("Primary.TButton", BLUE, BLUE_HI, pad=(18, 12), font=(FONT, 12, "bold"))
        btn("Green.TButton",   GREEN_DK, GREEN_HI, GREEN, pad=(12, 8))
        btn("Amber.TButton",   AMBER_DK, AMBER_HI, AMBER, pad=(12, 8))
        btn("Red.TButton",     RED_DK,   RED_HI,   RED,   pad=(12, 8))
        btn("Ghost.TButton",   SURFACE2, BORDER, TEXT2, font=(FONT, 9), pad=(10, 6))
        btn("Connect.TButton", GREEN_DK, GREEN_HI, GREEN, pad=(14, 10), font=(FONT, 11, "bold"))
        btn("Send.TButton",    BLUE, BLUE_HI, "#fff", pad=(14, 7))
        btn("QA.TButton",      SURFACE2, BORDER, TEXT2, font=(FONT, 8), pad=(8, 4))

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self):
        self._build_menu()

        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=20, pady=(14, 0))

        tk.Label(top, text="MGU Shell", bg=BG, fg=TEXT,
                 font=(FONT_B, 16)).pack(side="left")
        tk.Label(top, text="iDrive 7/8", bg=BG, fg=TEXT3,
                 font=(FONT, 10)).pack(side="left", padx=(10, 0), pady=(4, 0))

        right_bar = tk.Frame(top, bg=BG)
        right_bar.pack(side="right")
        self.ssh_indicator = tk.Label(right_bar, text="• offline", bg=BG,
                                      fg=TEXT3, font=(FONT, 9))
        self.ssh_indicator.pack(side="right", padx=(12, 0))
        self.language = tk.StringVar(value="English")
        lang_box = ttk.Combobox(right_bar, textvariable=self.language,
            values=[d["name"] for d in self.i18n.catalogs.values()],
            state="readonly", width=10)
        lang_box.pack(side="right")
        lang_box.bind("<<ComboboxSelected>>", self._change_language)

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x", padx=20, pady=(12, 0))

        self.notebook = ttk.Notebook(self.root, style="App.TNotebook")
        self.notebook.pack(fill="both", expand=True, padx=20, pady=(4, 0))

        self.setup_frame = tk.Frame(self.notebook, bg=BG)
        self.ssh_frame = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(self.setup_frame, text="  Setup  ")
        self.notebook.add(self.ssh_frame, text="  Shell  ")

        self._build_setup()
        self._build_shell()

        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill="x", side="bottom", padx=20, pady=(0, 8))
        self.status_dot = tk.Canvas(bottom, width=8, height=8, bg=BG,
                                    highlightthickness=0)
        self._dot_id = self.status_dot.create_oval(0, 0, 8, 8, fill=GREEN, outline=GREEN)
        self.status_dot.pack(side="left", padx=(0, 6), pady=4)
        self.status_label = tk.Label(bottom, text="Ready", bg=BG, fg=TEXT3,
                                     font=(FONT, 9))
        self.status_label.pack(side="left", pady=4)

    # ==================================================================
    # SETUP TAB
    # ==================================================================

    def _build_setup(self):
        cols = tk.Frame(self.setup_frame, bg=BG)
        cols.pack(fill="both", expand=True, pady=(12, 12))
        cols.columnconfigure(0, weight=0, minsize=380)
        cols.columnconfigure(1, weight=1)
        cols.rowconfigure(0, weight=1)

        left = tk.Frame(cols, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        right = tk.Frame(cols, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")

        self._build_setup_controls(left)
        self._build_setup_log(right)

    def _build_setup_controls(self, parent):
        card = self._card(parent)
        card.pack(fill="x", pady=(0, 10))
        inner = tk.Frame(card, bg=SURFACE)
        inner.pack(fill="x", padx=16, pady=14)

        tk.Label(inner, text="Vehicle", bg=SURFACE, fg=TEXT,
                 font=(FONT_B, 11)).pack(anchor="w")

        tk.Label(inner, text="VIN", bg=SURFACE, fg=TEXT3,
                 font=(FONT, 9)).pack(anchor="w", pady=(10, 2))
        vin_row = tk.Frame(inner, bg=SURFACE)
        vin_row.pack(fill="x")
        self._entry(vin_row, self.vin, 18).pack(side="left", fill="x", expand=True, ipady=3)
        self.detect_btn = self._label(ttk.Button, vin_row, "Detect",
                                      style="Ghost.TButton", command=self.detect_vin)
        self.detect_btn.pack(side="right", padx=(6, 0))

        tk.Label(inner, text="Variant", bg=SURFACE, fg=TEXT3,
                 font=(FONT, 9)).pack(anchor="w", pady=(10, 2))
        self.variant_mb = ttk.OptionMenu(inner, self.variant_label,
            self.variant_label.get(),
            *[label for _, label in VARIANT_CHOICES],
            command=self._on_variant)
        self.variant_mb.configure(style="TMenubutton")
        self.variant_mb["menu"].configure(bg=SURFACE, fg=TEXT,
            activebackground=BLUE, activeforeground="#fff",
            borderwidth=0, activeborderwidth=0, relief="flat", font=(FONT, 10))
        self.variant_mb.pack(fill="x")

        ttk.Label(inner, text="Wrong variant = stuck in programming mode.",
                  style="Dim.TLabel").pack(anchor="w", pady=(6, 0))

        tk.Label(inner, text="Connection", bg=SURFACE, fg=TEXT3,
                 font=(FONT, 9)).pack(anchor="w", pady=(12, 2))
        conn = tk.Frame(inner, bg=SURFACE)
        conn.pack(fill="x")
        conn.columnconfigure(1, weight=1)
        conn.columnconfigure(3, weight=1)
        tk.Label(conn, text="GW", bg=SURFACE, fg=TEXT3,
                 font=(FONT, 8)).grid(row=0, column=0, padx=(0, 4))
        self._entry(conn, self.gateway, 10).grid(row=0, column=1, sticky="ew", ipady=2)
        tk.Label(conn, text="IP", bg=SURFACE, fg=TEXT3,
                 font=(FONT, 8)).grid(row=0, column=2, padx=(10, 4))
        self._entry(conn, self.mgu_ip, 10).grid(row=0, column=3, sticky="ew", ipady=2)

        card2 = self._card(parent)
        card2.pack(fill="x", pady=(0, 10))
        inner2 = tk.Frame(card2, bg=SURFACE)
        inner2.pack(fill="x", padx=16, pady=14)

        tk.Label(inner2, text="Actions", bg=SURFACE, fg=TEXT,
                 font=(FONT_B, 11)).pack(anchor="w")

        self.btn_flash = self._label(ttk.Button, inner2, "Flash + Enable",
                                     style="Primary.TButton", command=self.do_flash)
        self.btn_flash.pack(fill="x", pady=(10, 0))
        ttk.Label(inner2, text="Flashes debug image and enables root SSH.",
                  style="Dim.TLabel").pack(anchor="w", pady=(4, 8))

        row2 = tk.Frame(inner2, bg=SURFACE)
        row2.pack(fill="x")
        row2.columnconfigure((0, 1, 2), weight=1, uniform="btn")
        self.btn_enable = self._label(ttk.Button, row2, "Enable",
                                      style="Green.TButton", command=self.do_enable)
        self.btn_fix = self._label(ttk.Button, row2, "Fix Key",
                                   style="Amber.TButton", command=self.do_fix_key)
        self.btn_disable = self._label(ttk.Button, row2, "Disable",
                                       style="Red.TButton", command=self.do_disable)
        self.btn_enable.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_fix.grid(row=0, column=1, sticky="ew", padx=4)
        self.btn_disable.grid(row=0, column=2, sticky="ew", padx=(4, 0))

        self.action_buttons = [self.btn_flash, self.btn_enable, self.btn_disable, self.btn_fix]

        card3 = self._card(parent)
        card3.pack(fill="x")
        inner3 = tk.Frame(card3, bg=SURFACE)
        inner3.pack(fill="x", padx=16, pady=12)

        tk.Label(inner3, text="Options", bg=SURFACE, fg=TEXT,
                 font=(FONT_B, 11)).pack(anchor="w", pady=(0, 6))

        self._label(ttk.Checkbutton, inner3, "Disable debug mode after enabling",
                    variable=self.disable_debug,
                    style="Surf.TCheckbutton").pack(anchor="w")
        ttk.Label(inner3, text="Clears B7F8E5 DTC. SSH stays on.",
                  style="Dim.TLabel").pack(anchor="w", padx=(22, 0))

        self._label(ttk.Checkbutton, inner3, "Unique per-car key (advanced)",
                    variable=self.unique_key,
                    style="Surf.TCheckbutton").pack(anchor="w", pady=(6, 0))
        ttk.Label(inner3, text="One-time only. Lost key cannot be recovered.",
                  style="Dim.TLabel").pack(anchor="w", padx=(22, 0))

    def _build_setup_log(self, parent):
        hdr = tk.Frame(parent, bg=BG)
        hdr.pack(fill="x", pady=(0, 6))
        tk.Label(hdr, text="Log", bg=BG, fg=TEXT2,
                 font=(FONT_B, 10)).pack(side="left")
        ttk.Button(hdr, text="Open folder", style="Ghost.TButton",
                   command=self.open_logs_folder).pack(side="right")

        wrap = tk.Frame(parent, bg=BORDER)
        wrap.pack(fill="both", expand=True)
        self.out = scrolledtext.ScrolledText(
            wrap, wrap="word", font=(MONO, 9),
            bg=CONSOLE, fg=TEXT2, insertbackground=BLUE,
            relief="flat", borderwidth=0, padx=12, pady=10,
            selectbackground="#1d3350", selectforeground=TEXT)
        self.out.pack(fill="both", expand=True, padx=1, pady=1)
        self.out.configure(state="disabled")
        try:
            self.out.vbar.configure(bg=SURFACE, troughcolor=CONSOLE,
                                    activebackground=SURFACE2, relief="flat",
                                    borderwidth=0, width=8, highlightthickness=0,
                                    elementborderwidth=0)
        except (AttributeError, tk.TclError):
            pass

    # ==================================================================
    # SHELL TAB
    # ==================================================================

    def _build_shell(self):
        cols = tk.Frame(self.ssh_frame, bg=BG)
        cols.pack(fill="both", expand=True, pady=(12, 12))
        cols.columnconfigure(0, weight=0, minsize=280)
        cols.columnconfigure(1, weight=1)
        cols.rowconfigure(0, weight=1)

        sidebar = tk.Frame(cols, bg=BG, width=280)
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        sidebar.grid_propagate(False)

        terminal = tk.Frame(cols, bg=BG)
        terminal.grid(row=0, column=1, sticky="nsew")

        self._build_shell_sidebar(sidebar)
        self._build_shell_terminal(terminal)

        self._enrolled_units = []
        self._ssh_cmd_history = []
        self._ssh_cmd_index = -1
        self.root.after(500, self._refresh_units)

    def _build_shell_sidebar(self, parent):
        card = self._card(parent)
        card.pack(fill="x", pady=(0, 10))
        ci = tk.Frame(card, bg=SURFACE)
        ci.pack(fill="x", padx=14, pady=12)

        hdr = tk.Frame(ci, bg=SURFACE)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Connection", bg=SURFACE, fg=TEXT,
                 font=(FONT_B, 11)).pack(side="left")
        self.ssh_led = tk.Label(hdr, text="•", bg=SURFACE, fg=TEXT3,
                                font=(FONT, 12))
        self.ssh_led.pack(side="right")

        tk.Label(ci, text="Unit", bg=SURFACE, fg=TEXT3,
                 font=(FONT, 9)).pack(anchor="w", pady=(8, 2))
        self.ssh_unit_var = tk.StringVar()
        self.ssh_unit_box = ttk.Combobox(ci, textvariable=self.ssh_unit_var,
                                         state="readonly", width=24)
        self.ssh_unit_box.pack(fill="x")
        self.ssh_unit_box.bind("<<ComboboxSelected>>", self._on_unit_selected)

        tk.Label(ci, text="IP Address", bg=SURFACE, fg=TEXT3,
                 font=(FONT, 9)).pack(anchor="w", pady=(8, 2))
        self.ssh_ip_var = tk.StringVar()
        self._entry(ci, self.ssh_ip_var, 18).pack(fill="x", ipady=3)

        btn_row = tk.Frame(ci, bg=SURFACE)
        btn_row.pack(fill="x", pady=(10, 0))
        self.ssh_connect_btn = ttk.Button(btn_row, text="Connect",
            style="Connect.TButton", command=self._ssh_connect)
        self.ssh_connect_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.ssh_disconnect_btn = ttk.Button(btn_row, text="Disconnect",
            style="Red.TButton", command=self._ssh_disconnect)
        self.ssh_disconnect_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.ssh_disconnect_btn.configure(state="disabled")

        ref_row = tk.Frame(ci, bg=SURFACE)
        ref_row.pack(fill="x", pady=(6, 0))
        ttk.Button(ref_row, text="Refresh", style="Ghost.TButton",
                   command=self._refresh_units).pack(side="left")
        ttk.Button(ref_row, text="Keys", style="Ghost.TButton",
                   command=self._open_keys_folder).pack(side="right")

        card2 = self._card(parent)
        card2.pack(fill="x", pady=(0, 10))
        ii = tk.Frame(card2, bg=SURFACE)
        ii.pack(fill="x", padx=14, pady=10)
        tk.Label(ii, text="Unit Info", bg=SURFACE, fg=TEXT,
                 font=(FONT_B, 10)).pack(anchor="w")
        self.info_text = tk.Text(ii, height=5, width=24, font=(MONO, 8),
                                 bg=BG, fg=TEXT3, relief="flat", borderwidth=0,
                                 padx=8, pady=6, highlightthickness=1,
                                 highlightbackground=BORDER, highlightcolor=BORDER)
        self.info_text.pack(fill="x", pady=(6, 0))
        self.info_text.insert("1.0", "No unit selected.")
        self.info_text.configure(state="disabled")

        card3 = self._card(parent)
        card3.pack(fill="x")
        si = tk.Frame(card3, bg=SURFACE)
        si.pack(fill="x", padx=14, pady=10)
        tk.Label(si, text="File Transfer", bg=SURFACE, fg=TEXT,
                 font=(FONT_B, 10)).pack(anchor="w")

        ttk.Button(si, text="Upload", style="Ghost.TButton",
                   command=self._scp_upload).pack(fill="x", pady=(8, 0))
        ttk.Button(si, text="Download", style="Ghost.TButton",
                   command=self._scp_download).pack(fill="x", pady=(4, 0))

        tk.Label(si, text="Remote path", bg=SURFACE, fg=TEXT3,
                 font=(FONT, 8)).pack(anchor="w", pady=(6, 2))
        self.scp_remote_var = tk.StringVar(value="/tmp/")
        self._entry(si, self.scp_remote_var, 20).pack(fill="x", ipady=2)

    def _build_shell_terminal(self, parent):
        qa = tk.Frame(parent, bg=SURFACE, highlightthickness=1,
                      highlightbackground=BORDER, highlightcolor=BORDER)
        qa.pack(fill="x", pady=(0, 8))
        qa_inner = tk.Frame(qa, bg=SURFACE)
        qa_inner.pack(fill="x", padx=8, pady=5)
        for label, cmd in QUICK_ACTIONS:
            ttk.Button(qa_inner, text=label, style="QA.TButton",
                       command=lambda c=cmd: self._ssh_run_quick(c)
                       ).pack(side="left", padx=2)

        term_wrap = tk.Frame(parent, bg=BORDER)
        term_wrap.pack(fill="both", expand=True)
        self.ssh_output = scrolledtext.ScrolledText(
            term_wrap, wrap="word", font=(MONO, 10),
            bg=CONSOLE, fg=TEXT2, insertbackground=BLUE,
            relief="flat", borderwidth=0, padx=14, pady=12,
            selectbackground="#1d3350", selectforeground=TEXT)
        self.ssh_output.pack(fill="both", expand=True, padx=1, pady=1)
        self.ssh_output.configure(state="disabled")
        self.ssh_output.tag_configure("prompt", foreground=BLUE, font=(MONO, 10, "bold"))
        self.ssh_output.tag_configure("error", foreground=RED)
        self.ssh_output.tag_configure("success", foreground=GREEN)
        self.ssh_output.tag_configure("dim", foreground=TEXT3)
        self.ssh_output.tag_configure("info", foreground=BLUE)
        self.ssh_output.tag_configure("amber", foreground=AMBER)
        try:
            self.ssh_output.vbar.configure(bg=SURFACE, troughcolor=CONSOLE,
                                           activebackground=SURFACE2, relief="flat",
                                           borderwidth=0, width=8, highlightthickness=0,
                                           elementborderwidth=0)
        except (AttributeError, tk.TclError):
            pass

        cmd_bar = tk.Frame(parent, bg=SURFACE, highlightthickness=1,
                           highlightbackground=BORDER, highlightcolor=BLUE)
        cmd_bar.pack(fill="x", pady=(8, 0))
        cmd_inner = tk.Frame(cmd_bar, bg=SURFACE)
        cmd_inner.pack(fill="x", padx=10, pady=6)

        self.ssh_prompt_lbl = tk.Label(cmd_inner, text="root@mgu ~#", bg=SURFACE,
                                       fg=BLUE, font=(MONO, 10, "bold"))
        self.ssh_prompt_lbl.pack(side="left", padx=(0, 8))
        self.ssh_cmd_var = tk.StringVar()
        self.ssh_cmd_entry = tk.Entry(cmd_inner, textvariable=self.ssh_cmd_var,
                                      font=(MONO, 10), bg=SURFACE, fg=TEXT,
                                      insertbackground=BLUE, relief="flat",
                                      highlightthickness=0, borderwidth=0)
        self.ssh_cmd_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.ssh_cmd_entry.bind("<Return>", self._ssh_send_command)
        self.ssh_cmd_entry.bind("<Up>", self._ssh_history_up)
        self.ssh_cmd_entry.bind("<Down>", self._ssh_history_down)

        ttk.Button(cmd_inner, text="Run", style="Send.TButton",
                   command=self._ssh_send_command).pack(side="right", padx=(8, 0))

    # ------------------------------------------------------------------
    # Primitives
    # ------------------------------------------------------------------

    def _card(self, parent):
        return tk.Frame(parent, bg=SURFACE, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)

    def _entry(self, parent, var, width):
        return tk.Entry(parent, textvariable=var, width=width, font=(MONO, 10),
                        bg=BG, fg=TEXT, insertbackground=BLUE, relief="flat",
                        disabledbackground=BG, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BLUE)

    def _label(self, cls, parent, source, **kw):
        w = cls(parent, text=self.i18n.t(source), **kw)
        self._labels.append((w, source))
        return w

    # ------------------------------------------------------------------
    # SSH sidebar
    # ------------------------------------------------------------------

    def _open_keys_folder(self):
        folder = os.path.join(HERE, "data", "enrollments")
        os.makedirs(folder, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            messagebox.showinfo("Keys folder",
                "Enrollment keys are in:\n%s\n\n(%s)" % (folder, e))

    def _refresh_units(self):
        self._enrolled_units = scan_enrollments()
        labels = []
        for u in self._enrolled_units:
            ip = u["ip"] or "no IP"
            labels.append("%s  |  %s" % (u["vin"], ip))
        self.ssh_unit_box["values"] = labels
        if labels:
            self.ssh_unit_box.current(0)
            self._on_unit_selected()
        self._term_write("Found %d enrolled unit(s).\n" % len(self._enrolled_units), "dim")

    def _on_unit_selected(self, _ev=None):
        idx = self.ssh_unit_box.current()
        if 0 <= idx < len(self._enrolled_units):
            unit = self._enrolled_units[idx]
            self.ssh_ip_var.set(unit["ip"])
            self._update_info(unit)

    def _update_info(self, unit):
        self.info_text.configure(state="normal")
        self.info_text.delete("1.0", "end")
        lines = ["VIN:  %s" % unit["vin"],
                 "IP:   %s" % (unit["ip"] or "unknown")]
        if unit["arch"]:
            lines.append("Arch: %s" % unit["arch"])
        if unit["created"]:
            lines.append("Date: %s" % unit["created"][:10])
        if unit["fingerprint"]:
            fp = unit["fingerprint"]
            lines.append("Key:  %s" % (fp[:28] + "…" if len(fp) > 30 else fp))
        lines.append("Dir:  %s" % os.path.basename(unit["dir"]))
        self.info_text.insert("1.0", "\n".join(lines))
        self.info_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # SSH terminal
    # ------------------------------------------------------------------

    def _term_write(self, text, tag=None):
        self.ssh_output.configure(state="normal")
        if tag:
            self.ssh_output.insert("end", text, tag)
        else:
            self.ssh_output.insert("end", text)
        self.ssh_output.see("end")
        self.ssh_output.configure(state="disabled")

    def _term_clear(self):
        self.ssh_output.configure(state="normal")
        self.ssh_output.delete("1.0", "end")
        self.ssh_output.configure(state="disabled")

    def _set_ssh_status(self, connected):
        self._ssh_connected = connected
        if connected:
            self.ssh_led.configure(fg=GREEN)
            self.ssh_indicator.configure(text="• connected", fg=GREEN)
            self.ssh_connect_btn.configure(state="disabled")
            self.ssh_disconnect_btn.configure(state="normal")
        else:
            self.ssh_led.configure(fg=TEXT3)
            self.ssh_indicator.configure(text="• offline", fg=TEXT3)
            self.ssh_connect_btn.configure(state="normal")
            self.ssh_disconnect_btn.configure(state="disabled")

    def _get_ssh_unit(self):
        idx = self.ssh_unit_box.current()
        ip = self.ssh_ip_var.get().strip()
        if not ip:
            messagebox.showerror("No IP", "Enter or select a unit IP address.")
            return None
        if idx < 0 or idx >= len(self._enrolled_units):
            messagebox.showerror("No unit", "Select an enrolled unit first.")
            return None
        unit = self._enrolled_units[idx]
        if not os.path.isfile(unit["key"]):
            messagebox.showerror("Key missing",
                "SSH key not found:\n%s\n\nRun Flash + Enable first." % unit["key"])
            return None
        return ip, unit["key"], unit

    def _ssh_connect(self):
        info = self._get_ssh_unit()
        if not info:
            return
        ip, key_path, unit = info

        if not sshcli.available():
            messagebox.showerror("OpenSSH missing",
                "OpenSSH client not found on PATH.\n"
                "Install via Settings > Apps > Optional features > OpenSSH Client.")
            return

        self._term_clear()
        self._term_write("Connecting to root@%s (%s)...\n\n" % (ip, unit["vin"]), "info")
        self._set_ssh_status(True)
        self._ssh_busy = True

        def connect():
            rc, out, err = sshcli.run(ip, key_path,
                "echo '--- System ---' && uname -a && echo && "
                "echo '--- Uptime ---' && uptime 2>/dev/null && echo && "
                "echo '--- Disk ---' && df -h / /tmp 2>/dev/null && echo && "
                "echo '--- Network ---' && ip -4 addr show 2>/dev/null | grep inet || ifconfig 2>/dev/null | grep inet",
                timeout=30)
            self.root.after(0, lambda: self._ssh_connect_done(rc, out, err, ip, unit))

        threading.Thread(target=connect, daemon=True).start()

    def _ssh_connect_done(self, rc, out, err, ip, unit):
        self._ssh_busy = False
        if rc == 0:
            self._term_write("Connected.\n\n", "success")
            for line in out.splitlines():
                if line.startswith("---"):
                    self._term_write(line + "\n", "info")
                else:
                    self._term_write(line + "\n")
            self._term_write("\nReady.\n\n", "dim")
        else:
            self._set_ssh_status(False)
            self._term_write("Connection failed (exit %d)\n" % rc, "error")
            if err.strip():
                self._term_write(err.strip() + "\n", "error")
            if sshcli.is_local_key_permission_error(err):
                self._term_write(
                    "\nKey permission issue (Windows ACL).\n"
                    "Fix: icacls \"%s\" /inheritance:r /grant:r \"%%USERNAME%%:F\"\n"
                    % unit["key"], "amber")

    def _ssh_run_quick(self, cmd):
        if not self._ssh_connected:
            self._term_write("Not connected.\n", "error")
            return
        if self._ssh_busy:
            self._term_write("Busy.\n", "amber")
            return
        self.ssh_cmd_var.set(cmd)
        self._ssh_send_command()

    def _ssh_send_command(self, _ev=None):
        cmd = self.ssh_cmd_var.get().strip()
        if not cmd:
            return
        self._ssh_cmd_history.append(cmd)
        self._ssh_cmd_index = -1
        self.ssh_cmd_var.set("")

        if not self._ssh_connected:
            self._term_write("Not connected.\n", "error")
            return
        if self._ssh_busy:
            self._term_write("Busy.\n", "amber")
            return

        info = self._get_ssh_unit()
        if not info:
            return
        ip, key_path, unit = info

        self._term_write("root@mgu:~# %s\n" % cmd, "prompt")
        self._ssh_busy = True

        def run():
            rc, out, err = sshcli.run(ip, key_path, cmd, timeout=60)
            def show():
                self._ssh_busy = False
                if out.strip():
                    self._term_write(out if out.endswith("\n") else out + "\n")
                if err.strip():
                    self._term_write(err.strip() + "\n", "error")
                if rc != 0 and not err.strip():
                    self._term_write("[exit %d]\n" % rc, "dim")
            self.root.after(0, show)

        threading.Thread(target=run, daemon=True).start()

    def _ssh_history_up(self, _ev=None):
        if not self._ssh_cmd_history:
            return "break"
        if self._ssh_cmd_index == -1:
            self._ssh_cmd_index = len(self._ssh_cmd_history) - 1
        elif self._ssh_cmd_index > 0:
            self._ssh_cmd_index -= 1
        self.ssh_cmd_var.set(self._ssh_cmd_history[self._ssh_cmd_index])
        return "break"

    def _ssh_history_down(self, _ev=None):
        if not self._ssh_cmd_history or self._ssh_cmd_index == -1:
            return "break"
        if self._ssh_cmd_index < len(self._ssh_cmd_history) - 1:
            self._ssh_cmd_index += 1
            self.ssh_cmd_var.set(self._ssh_cmd_history[self._ssh_cmd_index])
        else:
            self._ssh_cmd_index = -1
            self.ssh_cmd_var.set("")
        return "break"

    def _ssh_disconnect(self):
        self._set_ssh_status(False)
        self._term_write("\nDisconnected.\n\n", "dim")

    # ------------------------------------------------------------------
    # SCP
    # ------------------------------------------------------------------

    def _scp_upload(self):
        if not self._ssh_connected:
            messagebox.showinfo("Not connected", "Connect to a unit first.")
            return
        info = self._get_ssh_unit()
        if not info:
            return
        ip, key_path, unit = info

        local = filedialog.askopenfilename(title="Select file to upload")
        if not local:
            return
        remote = self.scp_remote_var.get().strip() or "/tmp/"
        if remote.endswith("/"):
            remote += os.path.basename(local)

        self._term_write("Uploading %s -> %s ...\n" % (os.path.basename(local), remote), "amber")

        def upload():
            try:
                with open(local, "rb") as f:
                    data = f.read()
            except OSError as e:
                self.root.after(0, lambda: self._term_write("Read error: %s\n" % e, "error"))
                return
            rc, out, err = sshcli.run(ip, key_path,
                "cat > '%s' && echo 'UPLOAD_OK' || echo 'UPLOAD_FAIL'" % remote,
                timeout=60, stdin_data=data)
            def show():
                if "UPLOAD_OK" in out:
                    self._term_write("Uploaded %s (%d bytes)\n" % (remote, len(data)), "success")
                else:
                    self._term_write("Upload failed: %s\n" % (err.strip() or out.strip()), "error")
            self.root.after(0, show)

        threading.Thread(target=upload, daemon=True).start()

    def _scp_download(self):
        if not self._ssh_connected:
            messagebox.showinfo("Not connected", "Connect to a unit first.")
            return
        info = self._get_ssh_unit()
        if not info:
            return
        ip, key_path, unit = info

        remote = self.scp_remote_var.get().strip()
        if not remote or remote.endswith("/"):
            messagebox.showinfo("Remote path", "Enter the full remote file path.")
            return

        local = filedialog.asksaveasfilename(
            title="Save as", initialfile=os.path.basename(remote))
        if not local:
            return

        self._term_write("Downloading %s ...\n" % remote, "amber")

        def download():
            rc, out, err = sshcli.run(ip, key_path, "cat '%s'" % remote, timeout=60)
            def show():
                if rc == 0:
                    try:
                        with open(local, "wb") as f:
                            f.write(out.encode("utf-8"))
                        self._term_write("Downloaded %s (%d bytes)\n" % (
                            os.path.basename(local), len(out.encode("utf-8"))), "success")
                    except OSError as e:
                        self._term_write("Save failed: %s\n" % e, "error")
                else:
                    self._term_write("Download failed: %s\n" % (
                        err.strip() or "exit %d" % rc), "error")
            self.root.after(0, show)

        threading.Thread(target=download, daemon=True).start()

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _set_dot(self, color):
        try:
            self.status_dot.itemconfig(self._dot_id, fill=color, outline=color)
        except (AttributeError, tk.TclError):
            pass

    def _change_language(self, _ev=None):
        tag = next(t for t, d in self.i18n.catalogs.items()
                   if d["name"] == self.language.get())
        self.i18n.select(tag)
        for widget, source in self._labels:
            widget.configure(text=self.i18n.t(source))
        self.status_label.configure(
            text=self.i18n.t("Working..." if self._is_busy else "Ready"))
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        for text in self._history:
            self.out.insert("end", self.i18n.log(text))
        self.out.see("end")
        self.out.configure(state="disabled")

    def _append(self, text):
        self._history.append(text)
        self.out.configure(state="normal")
        self.out.insert("end", self.i18n.log(text))
        self.out.see("end")
        self.out.configure(state="disabled")

    def open_logs_folder(self):
        folder = os.path.join(HERE, "data", "logs")
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            pass
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            messagebox.showinfo("Logs folder",
                "Logs are in:\n%s\n\n(%s)" % (folder, e))

    def _drain(self):
        try:
            while True:
                self._append(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _busy(self, on):
        self._is_busy = on
        state = "disabled" if on else "normal"
        for b in self.action_buttons:
            b.configure(state=state)
        self.detect_btn.configure(state=state)
        self._set_dot(AMBER if on else GREEN)
        self.status_label.configure(
            text=self.i18n.t("Working..." if on else "Ready"))

    def _build_menu(self):
        menubar = tk.Menu(self.root, bg=SURFACE, fg=TEXT,
                          activebackground=BLUE, activeforeground="#fff",
                          borderwidth=0)
        recovery = tk.Menu(menubar, tearoff=0, bg=SURFACE, fg=TEXT,
                           activebackground=BLUE, activeforeground="#fff",
                           borderwidth=0)
        recovery.add_command(label="Enter programming mode...",
                             command=self.do_enter_prog)
        recovery.add_command(label="Exit programming mode (recovery)...",
                             command=self.do_exit_prog)
        menubar.add_cascade(label="Recovery", menu=recovery)
        self.root.config(menu=menubar)

    def _common_args(self):
        args = ["--yes"]
        if self.vin.get().strip():
            args += ["--vin", self.vin.get().strip()]
        if self.gateway.get().strip():
            args += ["--gateway", self.gateway.get().strip()]
        if self.mgu_ip.get().strip():
            args += ["--mgu-ip", self.mgu_ip.get().strip()]
        if self.arch.get() in ARCHES:
            args += ["--arch", self.arch.get()]
        return args

    def _on_variant(self, label):
        self.arch.set(_VARIANT_BY_LABEL.get(label, ""))

    def _require_variant(self):
        if self.arch.get() in ARCHES:
            return True
        messagebox.showerror("Select variant",
            "Choose your head-unit variant before flashing.\n\n"
            "Wrong variant = stuck in programming mode.")
        return False

    def _run(self, argv, label):
        if self.worker and self.worker.is_alive():
            return
        self._busy(True)
        self._append("\n$ ssh_enable %s\n" % " ".join(argv))

        def task():
            saved_out, saved_err = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = _QueueWriter(self.q)
            rc = 1
            try:
                rc = ssh_enable.main(argv)
            except SystemExit as e:
                self.q.put("\n%s\n" % e)
                rc = e.code if isinstance(e.code, int) else 1
            except Exception as e:
                self.q.put("\nERROR: %s\n" % e)
            finally:
                sys.stdout.flush()
                sys.stdout, sys.stderr = saved_out, saved_err
                self.q.put("\n[%s finished, exit %s]\n" % (label, rc))
                self.root.after(0, lambda: self._on_run_done(rc))

        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    def _on_run_done(self, rc):
        self._busy(False)
        if rc == 0:
            self._refresh_units()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _persist_args(self):
        args = ["--persist"]
        if not self.disable_debug.get():
            args.append("--keep-debug")
        args.append("--unique-key" if self.unique_key.get() else "--no-unique-key")
        return args

    def do_enable(self):
        self._run(["enable"] + self._persist_args() + self._common_args(), "enable")

    def do_flash(self):
        if not self._require_variant():
            return
        if not self.vin.get().strip():
            messagebox.showerror(self.i18n.t("VIN required"),
                self.i18n.t("Flashing writes the VIN to the unit, so it is required.\n"
                "Enter it or click Detect first."))
            return
        if not messagebox.askyesno(
            self.i18n.t("Flash the debug image?"),
            self.i18n.t("This puts the head unit into programming mode and reboots it.\n\n"
            "VIN %s will be written.\n\nContinue?") % self.vin.get().strip()):
            return
        self._run(["flash"] + self._persist_args() + self._common_args(), "flash")

    def do_fix_key(self):
        if not self._require_variant():
            return
        if not self.vin.get().strip():
            messagebox.showerror("VIN required",
                "Fix-key re-flashes the debug image (writing the VIN),\n"
                "so a VIN is required.")
            return
        if not messagebox.askyesno("Fix refusing SSH key?",
            "Re-flashes the debug image carrying our key.\n"
            "The unit enters programming mode and reboots;\n"
            "CAFD coding is cleared.\n\n"
            "VIN %s will be written.\n\nContinue?" % self.vin.get().strip()):
            return
        self._run(["fix-key"] + self._persist_args() + self._common_args(), "fix-key")

    def do_disable(self):
        if not messagebox.askyesno(self.i18n.t("Disable SSH?"),
            self.i18n.t("Revert SSH to stock and stop debug mode?")):
            return
        self._run(["disable"] + self._common_args(), "disable")

    def do_enter_prog(self):
        if not self._require_variant():
            return
        if not messagebox.askyesno("Enter programming mode?",
            "Runs the pre-flash sequence and stops when the unit is in\n"
            "programming mode. Writes nothing to flash.\n\n"
            "Use 'Exit programming mode' to return to normal.\n\nContinue?"):
            return
        self._run(["enter-prog"] + self._common_args(), "enter-prog")

    def do_exit_prog(self):
        if not messagebox.askyesno("Exit programming mode?",
            "Leaves programming mode and reboots to normal.\n\n"
            "Use if stuck on the blue programming screen.\n\nContinue?"):
            return
        self._run(["exit-prog"] + self._common_args(), "exit-prog")

    def detect_vin(self):
        if self.worker and self.worker.is_alive():
            return
        self._busy(True)
        self._append("\nScanning for ENET gateway (UDP 6811)...\n")

        def task():
            try:
                gws = discovery.discover(
                    log=lambda *a, **k: self.q.put(" ".join(str(x) for x in a) + "\n"))
            except Exception as e:
                gws = []
                self.q.put("Discovery failed: %s\n" % e)

            def apply():
                if gws:
                    g = gws[0]
                    if g.vin:
                        self.vin.set(g.vin)
                    self.gateway.set(g.ip)
                    self.q.put("Found gateway %s | VIN: %s\n" % (
                        g.ip, g.vin or "not reported"))
                    if len(gws) > 1:
                        self.q.put("(%d gateways; using first)\n" % len(gws))
                else:
                    self.q.put("No ENET gateway responded.\n")
                self._busy(False)

            self.root.after(0, apply)

        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()


def main():
    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        try:
            with open(os.path.join(HERE, "mgu-ssh-error.log"), "w",
                      encoding="utf-8") as f:
                f.write(tb)
        except Exception:
            pass
        try:
            messagebox.showerror("MGU Shell failed to start", tb)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
