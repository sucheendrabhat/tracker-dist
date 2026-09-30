#!/usr/bin/env python3
"""
GATE CSE Tracker
=================

A study tracker built specifically around the 3-volume GATE Overflow CSE
PDF set (Maths & Aptitude / Algorithms & Core CS / Systems & Networks --
whatever your 3 files cover). All 3 volumes are indexed once and then
kept in memory together, so switching between them or jumping across
volumes from the dashboard is instant.

For each question you can:
  - see the title, answer key, source link, and PDF page
  - mark it L1 (easy) / L2 (forgot something) / L3 (didn't understand)
  - keep free-text notes
  - step through questions with Prev/Next -- either through a whole
    volume, or through whatever filtered list you came from (e.g. just
    your L3s in one subject)

Dashboard:
  - per-subject progress bars (across one volume or all of them)
  - filter by subject and by level
  - export your L2/L3 questions to a printable PDF, complete with a
    cropped image of the actual question as it appears in the source
    PDF (not just a link)

Requirements
------------
    pip install pymupdf reportlab

Run
---
    python app.py
"""

import os
import queue
import sys
import threading
import webbrowser
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import fitz  # noqa: F401  (imported here so a missing pymupdf fails fast, with a clear message)
except ImportError:
    sys.exit(
        "This tool needs the 'pymupdf' package, which isn't installed.\n\n"
        "Install it with:\n\n    pip install pymupdf reportlab\n\n"
        "then run this script again."
    )

from indexer import build_index, content_fingerprint
from database import Database
import sync

# ---------------------------------------------------------------------------
# Look & feel
# ---------------------------------------------------------------------------

COLOR_BG = "#f5f6f8"
COLOR_PANEL = "#ffffff"
COLOR_BORDER = "#dfe3e8"
COLOR_TEXT = "#1f2430"
COLOR_MUTED = "#6b7280"
COLOR_ACCENT = "#2563eb"
COLOR_ACCENT_DARK = "#1d4ed8"

LEVELS = [
    ("L1", "L1 \u00b7 Easy", "#16a34a", "#eafbea"),
    ("L2", "L2 \u00b7 Forgot something", "#ea580c", "#fff2e8"),
    ("L3", "L3 \u00b7 Didn't understand", "#dc2626", "#fdecec"),
]
LEVEL_COLOR = {code: color for code, _label, color, _bg in LEVELS}
LEVEL_BG = {code: bg for code, _label, _color, bg in LEVELS}
LEVEL_LABEL = {code: label for code, label, _color, _bg in LEVELS}

FONT_FAMILY = "Segoe UI"

# How long to wait after your last change (a level mark, a notes edit)
# before pushing an update to the website -- batches up a burst of
# changes into one sync instead of one per click/keystroke.
SYNC_DEBOUNCE_MS = 20_000


def _fmt_time(iso_str):
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.astimezone().strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return iso_str


def _volume_short_label(index, n):
    """A friendly 'Volume N' label with a subject summary, e.g.
    'Volume 2 \u2014 Algorithms, Compiler Design +4 more'."""
    chapters = sorted({(e["chapter_num"], e["chapter_name"]) for e in index.values()})
    names = [name for _num, name in chapters]
    summary = ", ".join(names[:2])
    if len(names) > 2:
        summary += f" +{len(names) - 2} more"
    return f"Volume {n} \u2014 {summary}" if summary else f"Volume {n}"


class GateTrackerApp:
    def __init__(self, root):
        self.root = root
        root.title("GATE CSE Tracker")
        root.geometry("1040x720")
        root.minsize(820, 600)
        root.configure(bg=COLOR_BG)

        self.db = Database()

        # source_id -> {'path','filename','index','sorted_ids','label'}
        self.volumes = {}
        self.active_source_id = None

        self.current_qid = None
        self.current_url = None
        self.current_level = None
        self._notes_save_after_id = None

        self._nav_list = []   # [(source_id, question_id), ...]
        self._nav_pos = -1

        self._volume_queue = []
        self._volume_total = 0
        self._volume_done = 0
        self._volume_errors = []
        self._startup_sync_done = False

        self._sync_after_id = None
        self._sync_in_progress = False
        self.sync_status_var = tk.StringVar(value=self._initial_sync_status_text())

        self._msg_queue = queue.Queue()
        self._setup_style()
        self._build_menu()
        self._build_ui()
        self._poll_after_id = self.root.after(50, self._poll_queue)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind_all("<Alt-Right>", lambda e: self._go_next())
        self.root.bind_all("<Alt-Left>", lambda e: self._go_prev())

        self._autoload_known_volumes()

    # ------------------------------------------------------------------
    # Website sync
    # ------------------------------------------------------------------

    def _initial_sync_status_text(self):
        repo = sync.get_repo_path()
        if not repo:
            return "Sync: not set up"
        last = sync.load_config().get("last_synced")
        return f"Sync: last synced {_fmt_time(last)}" if last else "Sync: repo set, not synced yet"

    def set_sync_repo(self):
        path = filedialog.askdirectory(title="Choose your gate-track website repo folder")
        if not path:
            return
        if not os.path.isdir(os.path.join(path, ".git")):
            if not messagebox.askyesno(
                "Not a git repo",
                f"{path}\n\ndoesn't look like a git repository (no .git folder here). "
                "Use it anyway?",
            ):
                return
        sync.set_repo_path(path)
        self.sync_status_var.set("Sync: repo set \u2014 syncing\u2026")
        self._trigger_sync(delay_ms=200)

    def sync_now_clicked(self):
        self._trigger_sync(delay_ms=0, manual=True)

    def _trigger_sync(self, delay_ms=0, manual=False):
        """Cancel any pending sync timer and schedule a new one -- used
        for the startup sync and manual 'Sync Now'."""
        if self._sync_after_id:
            self.root.after_cancel(self._sync_after_id)
        self._sync_after_id = self.root.after(delay_ms, lambda: self._start_sync_thread(manual=manual))

    def _schedule_sync(self):
        """Debounced trigger -- called after a level mark or a notes save.
        Restarts the wait each time, so a burst of changes becomes one
        sync a little while after you stop, not one per change."""
        if not sync.get_repo_path():
            return  # nothing configured yet -- don't nag about it
        if self._sync_after_id:
            self.root.after_cancel(self._sync_after_id)
        self._sync_after_id = self.root.after(SYNC_DEBOUNCE_MS, self._start_sync_thread)

    def _start_sync_thread(self, manual=False):
        self._sync_after_id = None
        if self._sync_in_progress:
            # already syncing -- try again shortly instead of overlapping
            self._sync_after_id = self.root.after(5000, self._start_sync_thread)
            return
        repo_path = sync.get_repo_path()
        if not repo_path:
            if manual:
                messagebox.showinfo(
                    "No website repo set",
                    "Choose your gate-track repo folder first, from "
                    "Website \u2192 Set Website Repo Folder...",
                )
            return

        self._sync_in_progress = True
        self.sync_status_var.set("Sync: syncing\u2026")
        db_path = self.db.db_path

        def worker():
            try:
                status = sync.sync_now(db_path, repo_path)
                self._msg_queue.put(("sync_done", status))
            except sync.SyncError as exc:
                self._msg_queue.put(("sync_error", str(exc)))
            except Exception as exc:  # noqa: BLE001
                self._msg_queue.put(("sync_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Look & feel
    # ------------------------------------------------------------------

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(".", font=(FONT_FAMILY, 10), background=COLOR_BG, foreground=COLOR_TEXT)
        style.configure("TFrame", background=COLOR_BG)
        style.configure("Panel.TFrame", background=COLOR_PANEL)
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT)
        style.configure("Panel.TLabel", background=COLOR_PANEL, foreground=COLOR_TEXT)
        style.configure("Muted.TLabel", background=COLOR_BG, foreground=COLOR_MUTED)
        style.configure("Panel.Muted.TLabel", background=COLOR_PANEL, foreground=COLOR_MUTED)
        style.configure("Header.TLabel", background=COLOR_BG, foreground=COLOR_TEXT,
                         font=(FONT_FAMILY, 16, "bold"))
        style.configure("SubHeader.TLabel", background=COLOR_BG, foreground=COLOR_MUTED,
                         font=(FONT_FAMILY, 9))

        style.configure("TButton", font=(FONT_FAMILY, 10), padding=6)
        style.configure("Accent.TButton", font=(FONT_FAMILY, 10, "bold"),
                         padding=7, foreground="white", background=COLOR_ACCENT)
        style.map("Accent.TButton", background=[("active", COLOR_ACCENT_DARK)])

        style.configure("TNotebook", background=COLOR_BG, borderwidth=0)
        style.configure("TNotebook.Tab", font=(FONT_FAMILY, 10), padding=(16, 8))
        style.map("TNotebook.Tab", background=[("selected", COLOR_PANEL)],
                  foreground=[("selected", COLOR_ACCENT)])

        style.configure("TLabelframe", background=COLOR_PANEL, bordercolor=COLOR_BORDER)
        style.configure("TLabelframe.Label", background=COLOR_PANEL, foreground=COLOR_MUTED,
                         font=(FONT_FAMILY, 9, "bold"))

        style.configure("Treeview", rowheight=26, font=(FONT_FAMILY, 10),
                         background=COLOR_PANEL, fieldbackground=COLOR_PANEL)
        style.configure("Treeview.Heading", font=(FONT_FAMILY, 10, "bold"))

        style.configure("TCombobox", padding=4)
        style.configure("TCheckbutton", background=COLOR_BG)
        style.configure("Panel.TCheckbutton", background=COLOR_PANEL)
        style.configure("TRadiobutton", background=COLOR_BG, font=(FONT_FAMILY, 10))

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Load Volume(s)...", command=self.load_volumes_dialog)
        self.volumes_menu = tk.Menu(file_menu, tearoff=0)
        file_menu.add_cascade(label="Loaded Volumes", menu=self.volumes_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Backup Data...", command=self.backup_data)
        file_menu.add_command(label="Restore from Backup...", command=self.restore_data)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        website_menu = tk.Menu(menubar, tearoff=0)
        website_menu.add_command(label="Set Website Repo Folder...", command=self.set_sync_repo)
        website_menu.add_command(label="Sync Now", command=self.sync_now_clicked)
        menubar.add_cascade(label="Website", menu=website_menu)

        self.root.config(menu=menubar)
        self._refresh_volumes_menu()

    def _refresh_volumes_menu(self):
        self.volumes_menu.delete(0, "end")
        if not self.volumes:
            self.volumes_menu.add_command(label="(none loaded yet)", state="disabled")
            return
        for sid, info in sorted(self.volumes.items()):
            self.volumes_menu.add_command(
                label=f"{info['label']}  ({len(info['index'])} questions)", state="disabled"
            )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        header = tk.Frame(self.root, bg=COLOR_BG)
        header.pack(fill="x", padx=18, pady=(16, 8))
        ttk.Label(header, text="GATE CSE Tracker", style="Header.TLabel").pack(side="left")
        self.volumes_summary_var = tk.StringVar(value="No volumes loaded yet")
        ttk.Label(header, textvariable=self.volumes_summary_var, style="SubHeader.TLabel").pack(
            side="left", padx=(14, 0), pady=(6, 0)
        )
        ttk.Button(
            header, text="Load Volume(s)...", command=self.load_volumes_dialog, style="Accent.TButton"
        ).pack(side="right")
        ttk.Label(header, textvariable=self.sync_status_var, style="SubHeader.TLabel").pack(
            side="right", padx=(0, 12), pady=(6, 0)
        )

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        self.search_frame_tab = ttk.Frame(self.notebook, style="TFrame")
        self.dashboard_tab = ttk.Frame(self.notebook, style="TFrame")
        self.analytics_tab = ttk.Frame(self.notebook, style="TFrame")
        self.activity_tab = ttk.Frame(self.notebook, style="TFrame")
        self.notebook.add(self.search_frame_tab, text="  Search & Track  ")
        self.notebook.add(self.dashboard_tab, text="  Dashboard  ")
        self.notebook.add(self.analytics_tab, text="  Analytics  ")
        self.notebook.add(self.activity_tab, text="  Activity  ")

        self._build_search_tab(self.search_frame_tab)
        self._build_dashboard_tab(self.dashboard_tab)
        self._build_analytics_tab(self.analytics_tab)
        self._build_activity_tab(self.activity_tab)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.status_var = tk.StringVar(value="Loading your volumes...")
        status = tk.Label(
            self.root, textvariable=self.status_var, bg="#eef0f3", fg=COLOR_MUTED,
            anchor="w", padx=10, pady=4, font=(FONT_FAMILY, 9),
        )
        status.pack(fill="x", side="bottom")

        self.root.bind_all("<Button-1>", self._maybe_hide_suggestions, add="+")

    # -- Search tab ------------------------------------------------------

    def _build_search_tab(self, parent):
        top_row = ttk.Frame(parent, padding=(0, 12, 0, 6))
        top_row.pack(fill="x")

        ttk.Label(top_row, text="Volume:").pack(side="left")
        self.volume_var = tk.StringVar(value="")
        self.volume_combo = ttk.Combobox(top_row, textvariable=self.volume_var, state="readonly", width=42)
        self.volume_combo.pack(side="left", padx=(6, 18))
        self.volume_combo.bind("<<ComboboxSelected>>", self._on_volume_picked)

        ttk.Label(top_row, text="Question ID:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(top_row, textvariable=self.search_var, font=(FONT_FAMILY, 12))
        self.search_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.search_entry.bind("<Return>", lambda e: self.do_search())
        self.search_entry.bind("<KeyRelease>", self.on_type)
        self.search_entry.bind("<Escape>", lambda e: self._hide_suggestions())
        ttk.Button(top_row, text="Search", command=self.do_search, style="Accent.TButton").pack(side="left")

        nav_row = ttk.Frame(parent, padding=(0, 0, 0, 6))
        nav_row.pack(fill="x")
        self.prev_btn = ttk.Button(nav_row, text="\u25c0 Prev", command=self._go_prev, state="disabled")
        self.prev_btn.pack(side="left")
        self.next_btn = ttk.Button(nav_row, text="Next \u25b6", command=self._go_next, state="disabled")
        self.next_btn.pack(side="left", padx=(6, 10))
        self.nav_pos_var = tk.StringVar(value="")
        ttk.Label(nav_row, textvariable=self.nav_pos_var, style="Muted.TLabel").pack(side="left")
        ttk.Label(nav_row, text="(Alt+\u2190 / Alt+\u2192 also work)", style="Muted.TLabel").pack(
            side="right"
        )

        self.suggest_list = tk.Listbox(parent, height=6, activestyle="dotbox", font=(FONT_FAMILY, 10))
        self.suggest_list.bind("<<ListboxSelect>>", self.on_suggest_pick)

        result = tk.Frame(parent, bg=COLOR_PANEL, highlightbackground=COLOR_BORDER,
                           highlightthickness=1, padx=16, pady=14)
        result.pack(fill="both", expand=True)
        self.result_frame = result

        header_row = tk.Frame(result, bg=COLOR_PANEL)
        header_row.pack(anchor="w", fill="x")
        self.id_label = tk.Label(header_row, text="", font=(FONT_FAMILY, 15, "bold"),
                                  bg=COLOR_PANEL, fg=COLOR_TEXT)
        self.id_label.pack(side="left")
        self.chapter_label = tk.Label(header_row, text="", bg=COLOR_PANEL, fg=COLOR_MUTED,
                                       font=(FONT_FAMILY, 10))
        self.chapter_label.pack(side="left", padx=(12, 0))

        self.title_label = tk.Label(result, text="", wraplength=880, justify="left",
                                     bg=COLOR_PANEL, fg=COLOR_TEXT, font=(FONT_FAMILY, 11),
                                     anchor="w")
        self.title_label.pack(anchor="w", fill="x", pady=(8, 12))

        ans_frame = tk.Frame(result, bg=COLOR_PANEL)
        ans_frame.pack(anchor="w", fill="x")
        tk.Label(ans_frame, text="Answer key:", font=(FONT_FAMILY, 11, "bold"),
                 bg=COLOR_PANEL, fg=COLOR_TEXT).pack(side="left")
        self.answer_label = tk.Label(ans_frame, text="", font=(FONT_FAMILY, 15, "bold"),
                                      bg=COLOR_PANEL, fg="#16a34a")
        self.answer_label.pack(side="left", padx=8)

        link_frame = tk.Frame(result, bg=COLOR_PANEL)
        link_frame.pack(anchor="w", fill="x", pady=(8, 0))
        tk.Label(link_frame, text="Link:", bg=COLOR_PANEL, fg=COLOR_TEXT).pack(side="left")
        self.link_label = tk.Label(link_frame, text="", fg=COLOR_ACCENT, bg=COLOR_PANEL, cursor="hand2")
        self.link_label.pack(side="left", padx=8)
        self.link_label.bind("<Button-1>", self.open_link)
        self.copy_btn = ttk.Button(link_frame, text="Copy Link", command=self.copy_link, state="disabled")
        self.copy_btn.pack(side="left", padx=8)

        self.page_label = tk.Label(result, text="", bg=COLOR_PANEL, fg=COLOR_MUTED, font=(FONT_FAMILY, 9))
        self.page_label.pack(anchor="w", pady=(6, 0))

        track_frame = tk.LabelFrame(result, text="  How did it go?  ", padx=12, pady=10,
                                     bg=COLOR_PANEL, fg=COLOR_MUTED, font=(FONT_FAMILY, 9, "bold"),
                                     bd=1, relief="solid", labelanchor="nw")
        track_frame.configure(highlightbackground=COLOR_BORDER)
        track_frame.pack(fill="x", pady=(16, 0))

        btn_row = tk.Frame(track_frame, bg=COLOR_PANEL)
        btn_row.pack(anchor="w")
        self.level_buttons = {}
        for code, label, color, _bg in LEVELS:
            b = tk.Button(
                btn_row, text=label, relief="raised", bd=0, padx=14, pady=7,
                font=(FONT_FAMILY, 10), cursor="hand2",
                command=lambda c=code: self.set_level(c),
            )
            b.pack(side="left", padx=(0, 8))
            self.level_buttons[code] = b

        self.clear_level_btn = ttk.Button(btn_row, text="Clear", command=lambda: self.set_level(None))
        self.clear_level_btn.pack(side="left", padx=(8, 0))

        self.level_status_label = tk.Label(track_frame, text="", bg=COLOR_PANEL, fg=COLOR_MUTED,
                                            font=(FONT_FAMILY, 9))
        self.level_status_label.pack(anchor="w", pady=(8, 0))

        notes_frame = tk.LabelFrame(result, text="  Notes  ", padx=12, pady=10,
                                     bg=COLOR_PANEL, fg=COLOR_MUTED, font=(FONT_FAMILY, 9, "bold"),
                                     bd=1, relief="solid", labelanchor="nw")
        notes_frame.configure(highlightbackground=COLOR_BORDER)
        notes_frame.pack(fill="both", expand=True, pady=(12, 0))

        self.notes_text = tk.Text(notes_frame, height=6, wrap="word", font=(FONT_FAMILY, 10),
                                   relief="flat", bg="#fbfbfc", highlightbackground=COLOR_BORDER,
                                   highlightthickness=1, padx=8, pady=6)
        self.notes_text.pack(fill="both", expand=True)
        self.notes_text.bind("<KeyRelease>", self._on_notes_typed)
        self.notes_text.bind("<FocusOut>", lambda e: self._flush_pending_notes_save())

        notes_btn_row = tk.Frame(notes_frame, bg=COLOR_PANEL)
        notes_btn_row.pack(fill="x", pady=(8, 0))
        ttk.Button(notes_btn_row, text="Save now", command=self.save_notes).pack(side="left")
        self.notes_saved_label = tk.Label(notes_btn_row, text="", bg=COLOR_PANEL, fg=COLOR_MUTED,
                                           font=(FONT_FAMILY, 9))
        self.notes_saved_label.pack(side="left", padx=10)

        self._set_tracking_controls_enabled(False)
        self._refresh_level_buttons()

    # -- Dashboard tab -----------------------------------------------------

    def _build_dashboard_tab(self, parent):
        controls = ttk.Frame(parent, padding=(0, 12, 0, 6))
        controls.pack(fill="x")

        ttk.Label(controls, text="Show:").pack(side="left")
        self.scope_var = tk.StringVar(value="all")
        ttk.Radiobutton(
            controls, text="This volume", variable=self.scope_var, value="current",
            command=self._on_scope_change,
        ).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(
            controls, text="All volumes", variable=self.scope_var, value="all",
            command=self._on_scope_change,
        ).pack(side="left", padx=(4, 20))

        ttk.Label(controls, text="Subject:").pack(side="left")
        self.chapter_var = tk.StringVar(value="All subjects")
        self.chapter_combo = ttk.Combobox(
            controls, textvariable=self.chapter_var, state="readonly", width=34
        )
        self.chapter_combo["values"] = ["All subjects"]
        self.chapter_combo.pack(side="left", padx=(6, 0))
        self.chapter_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_dashboard())

        level_row = ttk.Frame(parent, padding=(0, 6, 0, 6))
        level_row.pack(fill="x")
        ttk.Label(level_row, text="Filter:").pack(side="left")
        self.level_filter_vars = {}
        for code in ("L1", "L2", "L3", "NONE"):
            var = tk.BooleanVar(value=True)
            label = "Not attempted" if code == "NONE" else code
            cb = ttk.Checkbutton(level_row, text=label, variable=var, command=self.refresh_dashboard)
            cb.pack(side="left", padx=4)
            self.level_filter_vars[code] = var
        ttk.Button(level_row, text="Refresh", command=self.refresh_dashboard).pack(side="left", padx=(16, 0))
        ttk.Button(
            level_row, text="\U0001F4C4 Export Weak Spots (PDF)...", command=self.export_weak_spots,
            style="Accent.TButton",
        ).pack(side="left", padx=(8, 0))

        bars_outer = tk.LabelFrame(parent, text="  Progress by subject  ", padx=10, pady=8,
                                    bg=COLOR_PANEL, fg=COLOR_MUTED, font=(FONT_FAMILY, 9, "bold"),
                                    bd=1, relief="solid", labelanchor="nw")
        bars_outer.configure(highlightbackground=COLOR_BORDER)
        bars_outer.pack(fill="x", pady=(0, 10))

        legend = tk.Frame(bars_outer, bg=COLOR_PANEL)
        legend.pack(anchor="w", pady=(0, 6))
        for label, color in (
            ("L1 Easy", LEVEL_COLOR["L1"]), ("L2 Forgot", LEVEL_COLOR["L2"]),
            ("L3 Didn't understand", LEVEL_COLOR["L3"]), ("Not attempted", "#e0e0e0"),
        ):
            sw = tk.Canvas(legend, width=12, height=12, highlightthickness=0, bg=COLOR_PANEL)
            sw.create_rectangle(0, 0, 12, 12, fill=color, outline="")
            sw.pack(side="left", padx=(0, 3))
            tk.Label(legend, text=label, font=(FONT_FAMILY, 8), bg=COLOR_PANEL, fg=COLOR_MUTED).pack(
                side="left", padx=(0, 14)
            )

        bars_frame = tk.Frame(bars_outer, bg=COLOR_PANEL)
        bars_frame.pack(fill="x")
        self.bars_canvas = tk.Canvas(bars_frame, height=190, bg=COLOR_PANEL, highlightthickness=0)
        bars_vsb = ttk.Scrollbar(bars_frame, orient="vertical", command=self.bars_canvas.yview)
        self.bars_canvas.configure(yscrollcommand=bars_vsb.set)
        self.bars_canvas.pack(side="left", fill="x", expand=True)
        bars_vsb.pack(side="right", fill="y")

        self.stats_var = tk.StringVar(value="Load your volumes to see your progress.")
        ttk.Label(parent, textvariable=self.stats_var, padding=(0, 0, 0, 8)).pack(anchor="w")

        tree_frame = tk.Frame(parent, bg=COLOR_PANEL, highlightbackground=COLOR_BORDER, highlightthickness=1)
        tree_frame.pack(fill="both", expand=True)

        columns = ("id", "level", "page", "source", "title")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("id", text="Question ID", command=lambda: self._sort_tree("id", False))
        self.tree.heading("level", text="Level", command=lambda: self._sort_tree("level", False))
        self.tree.heading("page", text="Page", command=lambda: self._sort_tree("page", False))
        self.tree.heading("source", text="Volume", command=lambda: self._sort_tree("source", False))
        self.tree.heading("title", text="Title", command=lambda: self._sort_tree("title", False))
        self.tree.column("id", width=110, anchor="w")
        self.tree.column("level", width=90, anchor="center")
        self.tree.column("page", width=60, anchor="center")
        self.tree.column("source", width=110, anchor="w")
        self.tree.column("title", width=440, anchor="w")
        self.tree["displaycolumns"] = ("id", "level", "page", "source", "title")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("L1", background=LEVEL_BG["L1"])
        self.tree.tag_configure("L2", background=LEVEL_BG["L2"])
        self.tree.tag_configure("L3", background=LEVEL_BG["L3"])
        self.tree.tag_configure("NONE", background="#ffffff")

        self.tree.bind("<Double-1>", self.on_tree_double_click)

    # -- Analytics tab -----------------------------------------------------

    def _build_analytics_tab(self, parent):
        controls = ttk.Frame(parent, padding=(0, 12, 0, 6))
        controls.pack(fill="x")
        ttk.Label(controls, text="Show:").pack(side="left")
        self.analytics_scope_var = tk.StringVar(value="all")
        ttk.Radiobutton(controls, text="This volume", variable=self.analytics_scope_var, value="current",
                         command=self.refresh_analytics).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(controls, text="All volumes", variable=self.analytics_scope_var, value="all",
                         command=self.refresh_analytics).pack(side="left", padx=(4, 20))
        ttk.Button(controls, text="Refresh", command=self.refresh_analytics).pack(side="left")

        cards_frame = tk.Frame(parent, bg=COLOR_BG)
        cards_frame.pack(fill="x", pady=(4, 14))
        self.analytics_cards = {}
        card_defs = [
            ("total", "Total questions", COLOR_TEXT),
            ("attempted", "Attempted", COLOR_ACCENT),
            ("pending", "Pending", COLOR_MUTED),
            ("L1", "L1 \u00b7 Easy", LEVEL_COLOR["L1"]),
            ("L2", "L2 \u00b7 Forgot something", LEVEL_COLOR["L2"]),
            ("L3", "L3 \u00b7 Didn't understand", LEVEL_COLOR["L3"]),
        ]
        for key, label, color in card_defs:
            card = tk.Frame(cards_frame, bg=COLOR_PANEL, highlightbackground=COLOR_BORDER,
                             highlightthickness=1, padx=14, pady=10)
            card.pack(side="left", fill="both", expand=True, padx=(0, 8))
            num_label = tk.Label(card, text="0", font=(FONT_FAMILY, 20, "bold"), bg=COLOR_PANEL, fg=color)
            num_label.pack(anchor="w")
            tk.Label(card, text=label, font=(FONT_FAMILY, 8), bg=COLOR_PANEL, fg=COLOR_MUTED).pack(anchor="w")
            self.analytics_cards[key] = num_label

        tk.Label(parent, text="By subject \u2014 sorted weakest first, click a column to re-sort, "
                               "double-click a row to open it in the Dashboard",
                 font=(FONT_FAMILY, 9), bg=COLOR_BG, fg=COLOR_MUTED).pack(anchor="w", pady=(0, 4))

        tree_frame = tk.Frame(parent, bg=COLOR_PANEL, highlightbackground=COLOR_BORDER, highlightthickness=1)
        tree_frame.pack(fill="both", expand=True)

        columns = ("subject", "total", "pending", "l1", "l2", "l3", "pct")
        headers = {"subject": "Subject", "total": "Total", "pending": "Pending",
                   "l1": "L1", "l2": "L2", "l3": "L3", "pct": "% Done"}
        widths = {"subject": 340, "total": 70, "pending": 80, "l1": 60, "l2": 60, "l3": 60, "pct": 80}

        self.analytics_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        for c in columns:
            self.analytics_tree.heading(c, text=headers[c], command=lambda c=c: self._sort_analytics(c, False))
            self.analytics_tree.column(c, width=widths[c], anchor=("w" if c == "subject" else "center"))

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.analytics_tree.yview)
        self.analytics_tree.configure(yscrollcommand=vsb.set)
        self.analytics_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.analytics_tree.tag_configure("urgent", background=LEVEL_BG["L3"])
        self.analytics_tree.bind("<Double-1>", self._on_analytics_double_click)

    def refresh_analytics(self):
        scope = self.analytics_scope_var.get()
        if scope == "current":
            stats_by_chapter = self.db.get_stats_by_chapter(self.active_source_id) if self.active_source_id else {}
        else:
            stats_by_chapter = self.db.get_unified_stats_by_chapter()

        total = sum(s["total"] for s in stats_by_chapter.values())
        l1 = sum(s["L1"] for s in stats_by_chapter.values())
        l2 = sum(s["L2"] for s in stats_by_chapter.values())
        l3 = sum(s["L3"] for s in stats_by_chapter.values())
        attempted = l1 + l2 + l3
        pending = total - attempted

        self.analytics_cards["total"].config(text=str(total))
        pct = f"{attempted / total * 100:.0f}%" if total else "0%"
        self.analytics_cards["attempted"].config(text=f"{attempted} ({pct})")
        self.analytics_cards["pending"].config(text=str(pending))
        self.analytics_cards["L1"].config(text=str(l1))
        self.analytics_cards["L2"].config(text=str(l2))
        self.analytics_cards["L3"].config(text=str(l3))

        for item in self.analytics_tree.get_children():
            self.analytics_tree.delete(item)

        def pct_done(stats):
            t = stats["total"] or 1
            return (stats["L1"] + stats["L2"] + stats["L3"]) / t

        for subject, stats in sorted(stats_by_chapter.items(), key=lambda kv: pct_done(kv[1])):
            t = stats["total"] or 1
            done_pct = pct_done(stats) * 100
            tag = "urgent" if stats["L3"] >= 5 else ""
            self.analytics_tree.insert(
                "", "end", iid=subject,
                values=(subject, stats["total"], stats["NONE"], stats["L1"], stats["L2"], stats["L3"],
                        f"{done_pct:.0f}%"),
                tags=(tag,) if tag else (),
            )

    def _sort_analytics(self, col, reverse):
        items = [(self.analytics_tree.set(k, col), k) for k in self.analytics_tree.get_children("")]
        if col in ("total", "pending", "l1", "l2", "l3"):
            items.sort(key=lambda t: int(t[0]), reverse=reverse)
        elif col == "pct":
            items.sort(key=lambda t: float(t[0].rstrip("%")), reverse=reverse)
        else:
            items.sort(key=lambda t: t[0], reverse=reverse)
        for index, (_val, k) in enumerate(items):
            self.analytics_tree.move(k, "", index)
        self.analytics_tree.heading(col, command=lambda: self._sort_analytics(col, not reverse))

    def _on_analytics_double_click(self, event):
        sel = self.analytics_tree.selection()
        if not sel:
            return
        subject = sel[0]
        self.notebook.select(self.dashboard_tab)
        self.scope_var.set(self.analytics_scope_var.get())
        self._refresh_chapter_combo()
        self.chapter_var.set(subject)
        self.refresh_dashboard()

    # -- Activity tab (what was marked, on what day) ------------------------

    def _build_activity_tab(self, parent):
        controls = ttk.Frame(parent, padding=(0, 12, 0, 6))
        controls.pack(fill="x")
        ttk.Label(controls, text="Show:").pack(side="left")
        self.activity_scope_var = tk.StringVar(value="all")
        ttk.Radiobutton(controls, text="This volume", variable=self.activity_scope_var, value="current",
                         command=self.refresh_activity).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(controls, text="All volumes", variable=self.activity_scope_var, value="all",
                         command=self.refresh_activity).pack(side="left", padx=(4, 20))
        ttk.Button(controls, text="Refresh", command=self.refresh_activity).pack(side="left")

        cards_frame = tk.Frame(parent, bg=COLOR_BG)
        cards_frame.pack(fill="x", pady=(4, 12))
        self.activity_cards = {}
        card_defs = [
            ("streak", "Current streak", COLOR_ACCENT),
            ("longest", "Longest streak", COLOR_TEXT),
            ("active_days", "Active days", COLOR_TEXT),
            ("total_marked", "Questions marked", COLOR_TEXT),
        ]
        for key, label, color in card_defs:
            card = tk.Frame(cards_frame, bg=COLOR_PANEL, highlightbackground=COLOR_BORDER,
                             highlightthickness=1, padx=14, pady=10)
            card.pack(side="left", fill="both", expand=True, padx=(0, 8))
            num_label = tk.Label(card, text="0", font=(FONT_FAMILY, 20, "bold"), bg=COLOR_PANEL, fg=color)
            num_label.pack(anchor="w")
            tk.Label(card, text=label, font=(FONT_FAMILY, 8), bg=COLOR_PANEL, fg=COLOR_MUTED).pack(anchor="w")
            self.activity_cards[key] = num_label

        heatmap_outer = tk.LabelFrame(parent, text="  Last 18 weeks  ", padx=10, pady=8,
                                       bg=COLOR_PANEL, fg=COLOR_MUTED, font=(FONT_FAMILY, 9, "bold"),
                                       bd=1, relief="solid", labelanchor="nw")
        heatmap_outer.configure(highlightbackground=COLOR_BORDER)
        heatmap_outer.pack(fill="x", pady=(0, 10))
        self.heatmap_canvas = tk.Canvas(heatmap_outer, height=140, bg=COLOR_PANEL, highlightthickness=0)
        self.heatmap_canvas.pack(fill="x")
        self.heatmap_hint_var = tk.StringVar(value="Hover a square to see that day's count.")
        tk.Label(heatmap_outer, textvariable=self.heatmap_hint_var, font=(FONT_FAMILY, 8),
                 bg=COLOR_PANEL, fg=COLOR_MUTED).pack(anchor="w", pady=(4, 0))

        tk.Label(parent, text="By day", font=(FONT_FAMILY, 9), bg=COLOR_BG, fg=COLOR_MUTED).pack(
            anchor="w", pady=(0, 4)
        )
        tree_frame = tk.Frame(parent, bg=COLOR_PANEL, highlightbackground=COLOR_BORDER, highlightthickness=1)
        tree_frame.pack(fill="both", expand=True)

        columns = ("date", "total", "l1", "l2", "l3")
        headers = {"date": "Date", "total": "Total", "l1": "L1", "l2": "L2", "l3": "L3"}
        widths = {"date": 150, "total": 80, "l1": 70, "l2": 70, "l3": 70}
        self.activity_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        for c in columns:
            self.activity_tree.heading(c, text=headers[c])
            self.activity_tree.column(c, width=widths[c], anchor=("w" if c == "date" else "center"))
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.activity_tree.yview)
        self.activity_tree.configure(yscrollcommand=vsb.set)
        self.activity_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def refresh_activity(self):
        scope = self.activity_scope_var.get()
        if scope == "current" and self.active_source_id is None:
            daily = []
        else:
            daily = self.db.get_daily_activity(self.active_source_id if scope == "current" else None)

        from datetime import date, timedelta

        active_dates = {d["date"] for d in daily}

        today = date.today()
        cursor = today if today.isoformat() in active_dates else today - timedelta(days=1)
        streak = 0
        while cursor.isoformat() in active_dates:
            streak += 1
            cursor -= timedelta(days=1)

        longest = 0
        run = 0
        prev_date = None
        for ds in sorted(active_dates):
            d = date.fromisoformat(ds)
            run = run + 1 if (prev_date is not None and (d - prev_date).days == 1) else 1
            longest = max(longest, run)
            prev_date = d

        total_marked = sum(d["total"] for d in daily)

        self.activity_cards["streak"].config(text=f"{streak} day{'s' if streak != 1 else ''}")
        self.activity_cards["longest"].config(text=f"{longest} day{'s' if longest != 1 else ''}")
        self.activity_cards["active_days"].config(text=str(len(active_dates)))
        self.activity_cards["total_marked"].config(text=str(total_marked))

        for item in self.activity_tree.get_children():
            self.activity_tree.delete(item)
        for d in daily:
            self.activity_tree.insert(
                "", "end", values=(d["date"], d["total"], d["L1"], d["L2"], d["L3"])
            )

        self._draw_heatmap({d["date"]: d["total"] for d in daily})

    @staticmethod
    def _heat_color(count):
        if count <= 0:
            return "#ebedf0"
        if count <= 1:
            return "#c6dbef"
        if count <= 3:
            return "#6baed6"
        if count <= 6:
            return "#2171b5"
        return "#08306b"

    def _draw_heatmap(self, counts_by_date):
        from datetime import date, timedelta

        canvas = self.heatmap_canvas
        canvas.delete("all")

        weeks = 18
        today = date.today()
        start = today - timedelta(days=weeks * 7 - 1)
        start -= timedelta(days=start.weekday())  # snap back to that week's Monday

        cell, gap = 14, 3
        x0, y0 = 34, 18
        day_labels = ["Mon", "", "Wed", "", "Fri", "", ""]
        for row, lbl in enumerate(day_labels):
            if lbl:
                canvas.create_text(x0 - 6, y0 + row * (cell + gap) + cell / 2, anchor="e",
                                    text=lbl, font=(FONT_FAMILY, 7), fill=COLOR_MUTED)

        last_month = None
        d = start
        col = 0
        while d <= today:
            if d.month != last_month:
                canvas.create_text(x0 + col * (cell + gap), y0 - 8, anchor="w",
                                    text=d.strftime("%b"), font=(FONT_FAMILY, 7), fill=COLOR_MUTED)
                last_month = d.month
            for row in range(7):
                cur = d + timedelta(days=row)
                if cur > today:
                    break
                cnt = counts_by_date.get(cur.isoformat(), 0)
                x = x0 + col * (cell + gap)
                y = y0 + row * (cell + gap)
                tag = f"day_{cur.isoformat()}"
                canvas.create_rectangle(x, y, x + cell, y + cell, fill=self._heat_color(cnt),
                                         outline="#ffffff", width=1, tags=(tag,))
                canvas.tag_bind(tag, "<Enter>", lambda e, dt=cur, c=cnt: self.heatmap_hint_var.set(
                    f"{dt.strftime('%a, %d %b %Y')}: {c} question(s) marked"
                ))
            d += timedelta(days=7)
            col += 1

        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_tab_changed(self, event):
        tab = self.notebook.select()
        if tab == str(self.dashboard_tab):
            self.refresh_dashboard()
        elif tab == str(self.analytics_tab):
            self.refresh_analytics()
        elif tab == str(self.activity_tab):
            self.refresh_activity()

    # ------------------------------------------------------------------
    # Loading volumes
    # ------------------------------------------------------------------

    def load_volumes_dialog(self):
        paths = filedialog.askopenfilenames(
            title="Choose your GATE Overflow volume(s)",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if not paths:
            return
        self._queue_volumes(list(paths))

    def _autoload_known_volumes(self):
        sources = self.db.list_sources()
        paths = [s["filepath"] for s in sources if s["filepath"] and os.path.isfile(s["filepath"])]
        if paths:
            self._queue_volumes(paths)
        else:
            self.status_var.set(
                "Welcome! Use \u201cLoad Volume(s)...\u201d above to open your GATE Overflow PDFs."
            )

    def _queue_volumes(self, paths):
        # skip anything already loaded in this session
        new_paths = [p for p in paths if os.path.abspath(p) not in
                     {os.path.abspath(v["path"]) for v in self.volumes.values()}]
        if not new_paths:
            return
        self._volume_queue.extend(new_paths)
        self._volume_total += len(new_paths)
        if self._volume_done == 0 and len(self._volume_queue) == len(new_paths):
            self._volume_errors = []
        self._load_next_volume()

    def _load_next_volume(self):
        if not self._volume_queue:
            if self._volume_total:
                total_q = sum(len(v["index"]) for v in self.volumes.values())
                self.status_var.set(
                    f"Loaded {len(self.volumes)} volume(s), {total_q} questions total."
                )
                if self._volume_errors:
                    messagebox.showwarning(
                        "Some volumes couldn't be loaded",
                        "\n".join(self._volume_errors),
                    )
                if not self._startup_sync_done:
                    self._startup_sync_done = True
                    self._trigger_sync(delay_ms=3000)
            self._volume_total = 0
            self._volume_done = 0
            self._volume_errors = []
            return

        path = self._volume_queue.pop(0)
        self._volume_done += 1
        n = self._volume_done
        total = max(self._volume_total, n)
        self.status_var.set(f"Indexing volume {n}/{total}: {os.path.basename(path)}...")

        def worker():
            try:
                idx = build_index(
                    path, progress_cb=lambda d, t: self._msg_queue.put(("vol_progress", path, d, t, n, total))
                )
                fp = content_fingerprint(path)
            except Exception as exc:  # noqa: BLE001
                self._msg_queue.put(("vol_error", path, exc))
                return
            self._msg_queue.put(("vol_done", path, idx, fp))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                msg = self._msg_queue.get_nowait()
                kind = msg[0]
                if kind == "vol_progress":
                    _, path, d, t, n, total = msg
                    self.status_var.set(f"Indexing volume {n}/{total}: {os.path.basename(path)} (page {d}/{t})")
                elif kind == "vol_done":
                    _, path, idx, fp = msg
                    self._volume_loaded(path, idx, fp)
                    self._load_next_volume()
                elif kind == "vol_error":
                    _, path, exc = msg
                    self._volume_errors.append(f"{os.path.basename(path)}: {exc}")
                    self._load_next_volume()
                elif kind == "sync_done":
                    _, status_text = msg
                    self._sync_in_progress = False
                    self.sync_status_var.set(f"Sync: {status_text}")
                elif kind == "sync_error":
                    _, err_text = msg
                    self._sync_in_progress = False
                    self.sync_status_var.set("Sync: failed \u2014 see status bar")
                    self.status_var.set(f"Website sync failed: {err_text}")
        except queue.Empty:
            pass
        finally:
            if self.root.winfo_exists():
                self._poll_after_id = self.root.after(50, self._poll_queue)

    def _volume_loaded(self, path, idx, fingerprint):
        source_id = self.db.get_or_create_source(
            os.path.basename(path), os.path.abspath(path), fingerprint
        )
        self.db.sync_questions(source_id, idx)

        n = len(self.volumes) + 1 if source_id not in self.volumes else \
            list(sorted(self.volumes.keys()) + [source_id]).index(source_id) + 1
        label = _volume_short_label(idx, n)

        self.volumes[source_id] = {
            "path": os.path.abspath(path),
            "filename": os.path.basename(path),
            "index": idx,
            "sorted_ids": sorted(idx.keys(), key=self._id_sort_key),
            "label": label,
        }

        self._refresh_volumes_menu()
        self._refresh_volume_combo()
        self._refresh_chapter_combo()

        if self.active_source_id is None:
            self.active_source_id = source_id
            self.volume_var.set(self.volumes[source_id]["label"])
            self.search_btn_enable(True)

        self.volumes_summary_var.set(
            " \u00b7 ".join(f"{v['label'].split(chr(0x2014))[0].strip()} \u2713" for v in self.volumes.values())
        )

        self.refresh_dashboard()
        self.refresh_analytics()
        self.refresh_activity()

    def search_btn_enable(self, enabled):
        self.search_entry.config(state="normal" if enabled else "disabled")

    def _refresh_volume_combo(self):
        labels = [self.volumes[sid]["label"] for sid in sorted(self.volumes.keys())]
        self.volume_combo["values"] = labels

    def _on_volume_picked(self, event):
        self._flush_pending_notes_save()
        label = self.volume_var.get()
        for sid, info in self.volumes.items():
            if info["label"] == label:
                self.active_source_id = sid
                break
        self._clear_result()
        self.status_var.set(f"Switched to {self.volumes[self.active_source_id]['filename']}.")

    @staticmethod
    def _id_sort_key(qid):
        return tuple(int(p) for p in qid.split("."))

    # ------------------------------------------------------------------
    # Search tab behaviour
    # ------------------------------------------------------------------

    def on_type(self, event):
        if event.keysym in ("Return", "Escape", "Up", "Down"):
            return
        query = self.search_var.get().strip()
        if not query or self.active_source_id is None:
            self._hide_suggestions()
            return
        sorted_ids = self.volumes[self.active_source_id]["sorted_ids"]
        matches = [i for i in sorted_ids if i.startswith(query)][:15]
        if not matches or matches == [query]:
            self._hide_suggestions()
            return
        self.suggest_list.delete(0, "end")
        for m in matches:
            self.suggest_list.insert("end", m)
        self.suggest_list.pack(fill="x", pady=(0, 6), before=self.result_frame)

    def _hide_suggestions(self):
        self.suggest_list.pack_forget()

    def _maybe_hide_suggestions(self, event):
        if event.widget not in (self.suggest_list, self.search_entry):
            self._hide_suggestions()

    def on_suggest_pick(self, event):
        sel = self.suggest_list.curselection()
        if not sel:
            return
        qid = self.suggest_list.get(sel[0])
        self.search_var.set(qid)
        self._hide_suggestions()
        self.do_search()

    def do_search(self):
        self._hide_suggestions()
        if self.active_source_id is None:
            messagebox.showinfo("No volume loaded", "Load a volume first.")
            return
        vol = self.volumes[self.active_source_id]
        qid = self.search_var.get().strip()
        entry = vol["index"].get(qid)
        if not entry:
            self._clear_result()
            self._set_tracking_controls_enabled(False)
            self.status_var.set(f"No question found with ID '{qid}' in {vol['filename']}.")
            return

        # searching directly sets the nav context to "this whole volume"
        self._nav_list = [(self.active_source_id, q) for q in vol["sorted_ids"]]
        self._nav_pos = vol["sorted_ids"].index(qid)
        self._show_question(self.active_source_id, qid, entry)

    def _show_question(self, source_id, qid, entry):
        self._flush_pending_notes_save()

        self.active_source_id = source_id
        self.volume_var.set(self.volumes[source_id]["label"])
        self.search_var.set(qid)

        self.current_qid = qid
        self.id_label.config(text=qid)
        self.chapter_label.config(text=entry.get("chapter_name") or "")
        self.title_label.config(text=entry.get("title") or "(title not found)")
        answer = entry.get("answer")
        self.answer_label.config(text=answer if answer else "Not found in the Answer Keys")
        url = entry.get("url")
        self.current_url = url
        self.link_label.config(text=url or "(no link found for this question)")
        self.copy_btn.config(state="normal" if url else "disabled")
        page = entry.get("page")
        self.page_label.config(text=f"Found on PDF page {page}" if page else "")

        progress = self.db.get_progress(source_id, qid)
        self.current_level = progress["level"]
        self._set_tracking_controls_enabled(True)
        self._refresh_level_buttons()
        if progress["updated_at"]:
            self.level_status_label.config(text=f"Last updated {_fmt_time(progress['updated_at'])}")
        else:
            self.level_status_label.config(text="Not attempted yet")

        self.notes_text.delete("1.0", "end")
        if progress["notes"]:
            self.notes_text.insert("1.0", progress["notes"])
        self.notes_saved_label.config(text="")

        self.status_var.set(f"Showing {qid} from {self.volumes[source_id]['filename']}.")
        self._update_nav_controls()

    def _clear_result(self):
        self.current_qid = None
        self.id_label.config(text="")
        self.chapter_label.config(text="")
        self.title_label.config(text="")
        self.answer_label.config(text="")
        self.link_label.config(text="")
        self.page_label.config(text="")
        self.copy_btn.config(state="disabled")
        self.current_url = None
        self.current_level = None
        self._refresh_level_buttons()
        self.level_status_label.config(text="")
        self.notes_text.config(state="normal")
        self.notes_text.delete("1.0", "end")
        self.notes_saved_label.config(text="")

    def _set_tracking_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for b in self.level_buttons.values():
            b.config(state=state)
        self.clear_level_btn.config(state=state)
        self.notes_text.config(state=state)

    # -- level / notes actions --

    def set_level(self, level):
        if not self.current_qid or self.active_source_id is None:
            return
        self.db.set_level(self.active_source_id, self.current_qid, level)
        self.current_level = level
        self._refresh_level_buttons()
        self.level_status_label.config(text="Saved just now")
        if level:
            self.status_var.set(f"Marked {self.current_qid} as {level}.")
        else:
            self.status_var.set(f"Cleared level for {self.current_qid}.")
        self.refresh_dashboard()
        self.refresh_analytics()
        self.refresh_activity()
        self._schedule_sync()

    def _refresh_level_buttons(self):
        for code, button in self.level_buttons.items():
            if code == self.current_level:
                button.config(bg=LEVEL_COLOR[code], fg="white", relief="sunken",
                               font=(FONT_FAMILY, 10, "bold"))
            else:
                button.config(bg="#eef0f3", fg=COLOR_TEXT, relief="raised",
                               font=(FONT_FAMILY, 10, "normal"))

    def _on_notes_typed(self, event=None):
        if event is not None and event.keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R",
                                                     "Alt_L", "Alt_R", "Tab"):
            return
        if self._notes_save_after_id:
            self.root.after_cancel(self._notes_save_after_id)
        self.notes_saved_label.config(text="Typing...")
        self._notes_save_after_id = self.root.after(1000, self._debounced_save_notes)

    def _debounced_save_notes(self):
        self._notes_save_after_id = None
        self.save_notes(silent=True)

    def _flush_pending_notes_save(self):
        """Save immediately rather than waiting for the debounce timer --
        used when focus leaves the notes box or we're about to switch to
        a different question, so nothing typed gets lost."""
        if self._notes_save_after_id:
            self.root.after_cancel(self._notes_save_after_id)
            self._notes_save_after_id = None
        self.save_notes(silent=True)

    def save_notes(self, silent=False):
        if not self.current_qid or self.active_source_id is None:
            return
        notes = self.notes_text.get("1.0", "end-1c")
        self.db.set_notes(self.active_source_id, self.current_qid, notes)
        self.notes_saved_label.config(text="Saved automatically." if silent else "Saved.")
        if not silent:
            self.status_var.set(f"Notes saved for {self.current_qid}.")
        self._schedule_sync()

    # -- link actions --

    def open_link(self, event):
        if self.current_url:
            webbrowser.open(self.current_url)

    def copy_link(self):
        if self.current_url:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.current_url)
            self.status_var.set("Link copied to clipboard.")

    # -- prev / next navigation --

    def _update_nav_controls(self):
        n = len(self._nav_list)
        if n == 0 or self._nav_pos < 0:
            self.prev_btn.config(state="disabled")
            self.next_btn.config(state="disabled")
            self.nav_pos_var.set("")
            return
        self.prev_btn.config(state=("normal" if self._nav_pos > 0 else "disabled"))
        self.next_btn.config(state=("normal" if self._nav_pos < n - 1 else "disabled"))
        self.nav_pos_var.set(f"{self._nav_pos + 1} of {n}")

    def _go_prev(self):
        if self._nav_pos > 0:
            self._nav_pos -= 1
            self._jump_to_nav_pos()

    def _go_next(self):
        if self._nav_pos < len(self._nav_list) - 1:
            self._nav_pos += 1
            self._jump_to_nav_pos()

    def _jump_to_nav_pos(self):
        source_id, qid = self._nav_list[self._nav_pos]
        if source_id not in self.volumes:
            messagebox.showwarning(
                "Volume not loaded",
                "That question belongs to a volume that isn't currently loaded.",
            )
            return
        self.notebook.select(self.search_frame_tab)
        entry = self.volumes[source_id]["index"].get(qid)
        if entry:
            self._show_question(source_id, qid, entry)

    # ------------------------------------------------------------------
    # Dashboard tab behaviour
    # ------------------------------------------------------------------

    def _on_scope_change(self):
        self.chapter_var.set("All subjects")
        self._refresh_chapter_combo()
        self.refresh_dashboard()

    def _refresh_chapter_combo(self):
        if self.scope_var.get() == "all":
            chapters = self.db.get_unified_chapters()
        elif self.active_source_id is not None:
            chapters = [(n, c) for _num, n, c in self.db.get_chapters(self.active_source_id)]
        else:
            chapters = []
        values = ["All subjects"] + [name for name, _cnt in chapters]
        self.chapter_combo["values"] = values
        if self.chapter_var.get() not in values:
            self.chapter_var.set("All subjects")

    def refresh_dashboard(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        scope = self.scope_var.get()
        if scope == "current" and self.active_source_id is None:
            self.stats_var.set("Load your volumes to see your progress.")
            self.bars_canvas.delete("all")
            return

        chapter = self.chapter_var.get()
        selected_levels = [c for c, v in self.level_filter_vars.items() if v.get()]

        if scope == "current":
            rows = self.db.query_questions(self.active_source_id, chapter, selected_levels)
            stats = self.db.get_stats(self.active_source_id, chapter)
            stats_by_chapter = self.db.get_stats_by_chapter(self.active_source_id)
        else:
            rows = self.db.query_unified_questions(chapter, selected_levels)
            stats = self.db.get_unified_stats(chapter)
            stats_by_chapter = self.db.get_unified_stats_by_chapter()

        for r in rows:
            level = r["level"] or "NONE"
            display_level = "" if level == "NONE" else level
            sid = r.get("source_id", self.active_source_id)
            source_label = self.volumes[sid]["label"].split("\u2014")[0].strip() if sid in self.volumes else \
                r.get("source_filename", "")
            iid = f"{sid}::{r['question_id']}"
            self.tree.insert(
                "", "end", iid=iid,
                values=(r["question_id"], display_level, r["page"] or "", source_label, r["title"] or ""),
                tags=(level,),
            )

        self.stats_var.set(
            f"{stats['total']} questions   \u00b7   L1: {stats['L1']}   \u00b7   "
            f"L2: {stats['L2']}   \u00b7   L3: {stats['L3']}   \u00b7   "
            f"Not attempted: {stats['NONE']}"
        )
        self._draw_progress_bars(stats_by_chapter)

    def _draw_progress_bars(self, stats_by_chapter):
        canvas = self.bars_canvas
        canvas.delete("all")
        if not stats_by_chapter:
            canvas.create_text(10, 12, anchor="nw", text="No data yet.", fill=COLOR_MUTED)
            canvas.configure(scrollregion=(0, 0, 0, 30))
            return

        row_h = 28
        label_w = 250
        bar_x0 = label_w + 8
        bar_w = 280
        count_x = bar_x0 + bar_w + 10
        selected = self.chapter_var.get()

        subjects = sorted(stats_by_chapter.keys())
        y = 6
        for i, subject in enumerate(subjects):
            stats = stats_by_chapter[subject]
            total = stats["total"] or 1
            tag = f"row_{i}"

            display_name = subject if len(subject) <= 34 else subject[:31] + "..."
            canvas.create_text(8, y + row_h / 2, anchor="w", text=display_name,
                                font=(FONT_FAMILY, 9), fill=COLOR_TEXT, tags=(tag,))
            canvas.create_rectangle(bar_x0, y + 5, bar_x0 + bar_w, y + row_h - 5,
                                     fill="#e5e7eb", outline="", tags=(tag,))
            x = bar_x0
            for level in ("L1", "L2", "L3"):
                frac = stats.get(level, 0) / total
                w = bar_w * frac
                if w > 0.5:
                    canvas.create_rectangle(x, y + 5, x + w, y + row_h - 5,
                                             fill=LEVEL_COLOR[level], outline="", tags=(tag,))
                    x += w
            canvas.create_text(
                count_x, y + row_h / 2, anchor="w",
                text=f"L1 {stats.get('L1',0)}  L2 {stats.get('L2',0)}  "
                     f"L3 {stats.get('L3',0)}  \u2014 {stats.get('NONE',0)} left",
                font=(FONT_FAMILY, 8), fill=COLOR_MUTED, tags=(tag,),
            )
            canvas.create_rectangle(0, y, count_x + 220, y + row_h, fill="", outline="", tags=(tag,))
            if subject == selected:
                canvas.create_rectangle(2, y + 1, count_x + 215, y + row_h - 1,
                                         outline=COLOR_ACCENT, width=1, tags=(tag,))
            canvas.tag_bind(tag, "<Button-1>", lambda e, s=subject: self._select_subject_from_bar(s))
            y += row_h

        canvas.configure(scrollregion=(0, 0, count_x + 230, y + 6))

    def _select_subject_from_bar(self, subject):
        self.chapter_var.set(subject)
        self.refresh_dashboard()

    def _sort_tree(self, col, reverse):
        items = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        if col == "page":
            items.sort(key=lambda t: (t[0] == "", int(t[0]) if t[0] != "" else 0), reverse=reverse)
        elif col == "id":
            items.sort(key=lambda t: self._id_sort_key(t[0]), reverse=reverse)
        else:
            items.sort(key=lambda t: t[0], reverse=reverse)
        for index, (_val, k) in enumerate(items):
            self.tree.move(k, "", index)
        self.tree.heading(col, command=lambda: self._sort_tree(col, not reverse))

    def on_tree_double_click(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        # nav context becomes "everything currently in this table, in its
        # current order" -- lets you Prev/Next through exactly this list
        self._nav_list = []
        for iid in self.tree.get_children(""):
            sid_str, qid = iid.split("::", 1)
            self._nav_list.append((int(sid_str), qid))
        clicked_sid_str, clicked_qid = sel[0].split("::", 1)
        clicked = (int(clicked_sid_str), clicked_qid)
        self._nav_pos = self._nav_list.index(clicked) if clicked in self._nav_list else 0

        self._jump_to_nav_pos()

    # -- export --

    def export_weak_spots(self):
        scope = self.scope_var.get()
        if scope == "current" and self.active_source_id is None:
            messagebox.showinfo("No volume loaded", "Load a volume first.")
            return
        self._open_export_dialog(scope, self.chapter_var.get())

    def _open_export_dialog(self, scope, chapter):
        dlg = tk.Toplevel(self.root)
        dlg.title("Export Weak Spots")
        dlg.configure(bg=COLOR_PANEL)
        dlg.transient(self.root)
        dlg.resizable(False, False)

        scope_desc = "All loaded volumes" if scope == "all" else self.volumes[self.active_source_id]["filename"]
        if chapter and chapter != "All subjects":
            scope_desc += f" \u00b7 {chapter}"

        tk.Label(dlg, text="Export Weak Spots", font=(FONT_FAMILY, 13, "bold"),
                 bg=COLOR_PANEL, fg=COLOR_TEXT).pack(anchor="w", padx=18, pady=(18, 2))
        tk.Label(dlg, text=f"From: {scope_desc}", font=(FONT_FAMILY, 9),
                 bg=COLOR_PANEL, fg=COLOR_MUTED).pack(anchor="w", padx=18, pady=(0, 14))

        tk.Label(dlg, text="Include:", font=(FONT_FAMILY, 10, "bold"),
                 bg=COLOR_PANEL, fg=COLOR_TEXT).pack(anchor="w", padx=18)

        level_vars = {}
        level_frame = tk.Frame(dlg, bg=COLOR_PANEL)
        level_frame.pack(anchor="w", padx=18, pady=(4, 4))
        defaults = {"L1": False, "L2": True, "L3": True, "NONE": False}
        option_labels = {
            "L1": "L1 \u00b7 Easy", "L2": "L2 \u00b7 Forgot something",
            "L3": "L3 \u00b7 Didn't understand", "NONE": "Not attempted yet (a to-do list)",
        }
        count_var = tk.StringVar(value="")

        def update_count():
            levels = [c for c, v in level_vars.items() if v.get()]
            if not levels:
                count_var.set("Select at least one option.")
                return
            stats = self.db.get_stats(self.active_source_id, chapter) if scope == "current" \
                else self.db.get_unified_stats(chapter)
            total = sum(stats.get(c, 0) for c in levels)
            count_var.set(f"{total} question(s) will be included.")

        for code in ("L3", "L2", "L1", "NONE"):
            var = tk.BooleanVar(value=defaults[code])
            var.trace_add("write", lambda *a: update_count())
            cb = tk.Checkbutton(level_frame, text=option_labels[code], variable=var, bg=COLOR_PANEL,
                                 anchor="w", font=(FONT_FAMILY, 10), activebackground=COLOR_PANEL)
            cb.pack(anchor="w", pady=1)
            level_vars[code] = var

        quick_row = tk.Frame(dlg, bg=COLOR_PANEL)
        quick_row.pack(anchor="w", padx=18, pady=(6, 10))

        def _set(l1, l2, l3, none):
            level_vars["L1"].set(l1); level_vars["L2"].set(l2)
            level_vars["L3"].set(l3); level_vars["NONE"].set(none)

        ttk.Button(quick_row, text="Weak only (L2+L3)", command=lambda: _set(False, True, True, False)).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(quick_row, text="Everything", command=lambda: _set(True, True, True, True)).pack(
            side="left", padx=4
        )
        ttk.Button(quick_row, text="Pending only", command=lambda: _set(False, False, False, True)).pack(
            side="left", padx=4
        )

        include_snap_var = tk.BooleanVar(value=True)
        tk.Checkbutton(dlg, text="Include a snapshot image of each question", variable=include_snap_var,
                        bg=COLOR_PANEL, font=(FONT_FAMILY, 10), activebackground=COLOR_PANEL).pack(
            anchor="w", padx=18, pady=(0, 4)
        )

        tk.Label(dlg, textvariable=count_var, bg=COLOR_PANEL, fg=COLOR_MUTED, font=(FONT_FAMILY, 9)).pack(
            anchor="w", padx=18, pady=(0, 14)
        )
        update_count()

        btn_row = tk.Frame(dlg, bg=COLOR_PANEL)
        btn_row.pack(fill="x", padx=18, pady=(0, 18))

        def do_export():
            levels = [c for c, v in level_vars.items() if v.get()]
            if not levels:
                messagebox.showwarning("Pick at least one option", "Select at least one option to export.",
                                        parent=dlg)
                return
            dlg.destroy()
            self._run_export(scope, chapter, levels, include_snap_var.get())

        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="Export...", command=do_export, style="Accent.TButton").pack(side="right")

        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{max(x,0)}+{max(y,0)}")
        dlg.grab_set()

    def _run_export(self, scope, chapter, levels, include_snapshots):
        if scope == "current":
            rows = self.db.query_questions(self.active_source_id, chapter, levels)
            subheading = self.volumes[self.active_source_id]["filename"]
        else:
            rows = self.db.query_unified_questions(chapter, levels)
            subheading = "All loaded volumes"
        if chapter and chapter != "All subjects":
            subheading += f" \u00b7 {chapter}"

        level_set = set(levels)
        if level_set == {"L2", "L3"}:
            heading, name_bit = "GATE Weak Spots Review", "weak_spots"
        elif level_set == {"NONE"}:
            heading, name_bit = "GATE To-Do List", "todo"
        elif level_set == {"L1", "L2", "L3", "NONE"}:
            heading, name_bit = "GATE Full Review", "full_review"
        else:
            heading, name_bit = "GATE Review", "review"

        if not rows:
            messagebox.showinfo("Nothing to export", "No questions match this filter yet.")
            return

        suggested = f"{name_bit}_" + (
            chapter.lower().replace(" ", "_").replace(":", "") if chapter != "All subjects" else "all"
        ) + ".pdf"
        path = filedialog.asksaveasfilename(
            title="Export to...",
            defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf")],
            initialfile=suggested,
        )
        if not path:
            return

        self.status_var.set(f"Exporting {len(rows)} questions" + (" (rendering snapshots)..." if include_snapshots else "..."))
        self.root.update_idletasks()
        try:
            from export import export_weak_spots_pdf

            export_weak_spots_pdf(
                path, rows,
                heading=heading,
                subheading=subheading,
                show_source=(scope == "all"),
                include_snapshots=include_snapshots,
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Export failed", str(exc))
            self.status_var.set("Export failed.")
            return

        self.status_var.set(f"Exported {len(rows)} questions to {path}.")
        messagebox.showinfo("Export complete", f"Saved {len(rows)} questions to:\n{path}")

    # ------------------------------------------------------------------
    # Backup / restore
    # ------------------------------------------------------------------

    def backup_data(self):
        path = filedialog.asksaveasfilename(
            title="Backup tracker data to...",
            defaultextension=".db",
            filetypes=[("SQLite database", "*.db")],
            initialfile="gate_tracker_backup.db",
        )
        if not path:
            return
        try:
            self.db.backup_to(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Backup failed", str(exc))
            return
        messagebox.showinfo("Backup complete", f"Your tracking data was saved to:\n{path}")

    def restore_data(self):
        self._flush_pending_notes_save()
        path = filedialog.askopenfilename(
            title="Restore tracker data from...",
            filetypes=[("SQLite database", "*.db"), ("All files", "*.*")],
        )
        if not path:
            return
        if not messagebox.askyesno(
            "Restore data",
            "This will replace all current tracking data (levels and notes) "
            "with the contents of this backup. This can't be undone.\n\nContinue?",
        ):
            return
        try:
            self.db.restore_from(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Restore failed", str(exc))
            return

        self.volumes = {}
        self.active_source_id = None
        self._nav_list = []
        self._nav_pos = -1
        self._clear_result()
        self._set_tracking_controls_enabled(False)
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.bars_canvas.delete("all")
        self.volume_combo["values"] = []
        self.volume_var.set("")
        self.chapter_combo["values"] = ["All subjects"]
        self.chapter_var.set("All subjects")
        self.stats_var.set("Load your volumes to see your progress.")
        self.volumes_summary_var.set("No volumes loaded yet")
        for item in self.analytics_tree.get_children():
            self.analytics_tree.delete(item)
        for key in self.analytics_cards:
            self.analytics_cards[key].config(text="0")
        for item in self.activity_tree.get_children():
            self.activity_tree.delete(item)
        for key in self.activity_cards:
            self.activity_cards[key].config(text="0")
        self.heatmap_canvas.delete("all")
        self._refresh_volumes_menu()
        self.status_var.set("Data restored. Reloading your volumes...")
        self._autoload_known_volumes()
        messagebox.showinfo(
            "Restore complete",
            "Your data was restored, and your volumes are reloading now.",
        )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _on_close(self):
        try:
            self._flush_pending_notes_save()
        except Exception:
            pass
        try:
            self.root.after_cancel(self._poll_after_id)
        except Exception:
            pass
        try:
            if self._sync_after_id:
                self.root.after_cancel(self._sync_after_id)
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    GateTrackerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
