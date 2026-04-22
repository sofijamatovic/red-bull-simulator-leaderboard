"""
Red Bull Simulator Leaderboard
================================
A modern, fullscreen leaderboard app built with CustomTkinter.
Reads/writes lap times to a local CSV file and auto-sorts on every update.

HOW THE SORTING LOGIC WORKS:
  1. All times are stored internally as total seconds (float) for reliable comparison.
  2. When a new entry is added OR the refresh timer fires, `_reload_data()` is called.
  3. `_reload_data()` reads the CSV, converts every "m:ss.fff" string to seconds,
     then calls DataFrame.sort_values(ascending=True) — lowest time = Position 1.
  4. Gap and Delta columns are derived from the sorted order on the fly:
       - Gap to Leader = each driver's time minus the fastest time.
       - Delta         = each driver's time minus the driver immediately above them.
  5. The Treeview is fully cleared and repopulated after every sort — O(n), instant.
"""

# ── Standard library ──────────────────────────────────────────────────────────
import os
import sys
import csv
import time
import threading
from datetime import datetime
from pathlib import Path

# ── Third-party (install via requirements.txt) ────────────────────────────────
try:
    import customtkinter as ctk
except ImportError:
    raise SystemExit("customtkinter not found. Run:  pip install -r requirements.txt")

try:
    import pandas as pd
except ImportError:
    raise SystemExit("pandas not found. Run:  pip install -r requirements.txt")

try:
    from PIL import Image
except ImportError:
    raise SystemExit("Pillow not found. Run:  pip install -r requirements.txt")

# ── Paths ─────────────────────────────────────────────────────────────────────
# BASE_DIR: folder of the script when running as .py;
#           folder of the .exe when running as a PyInstaller bundle.
#           sys.executable points to the .exe itself in both frozen and normal mode,
#           but when frozen we want its directory, not sys._MEIPASS (which is temp).
if getattr(sys, "frozen", False):
    # Running as a bundled .exe — sit next to the executable
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    # Running as a plain .py script — sit next to main.py
    BASE_DIR = Path(__file__).resolve().parent

# CSV always lives next to the .exe / .py — never inside the temp _MEIPASS folder
DATA_FILE = BASE_DIR / "leaderboard_data.csv"


def resource_path(relative_path: str) -> Path:
    """
    Return the absolute path to a bundled resource.

    When running as a plain .py script:
        Files are expected next to main.py — BASE_DIR is used.

    When running as a PyInstaller .exe (--onefile):
        PyInstaller extracts bundled files to a temp folder stored in
        sys._MEIPASS at runtime.  Without this helper, the .exe would look
        for images next to the .exe and fail to find them.
    """
    base = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    return base / relative_path


# ── Logo paths — resolved through resource_path so they work in the .exe ──────
LOGO_REDBULL = resource_path("Logo-red-bull-vector-transparent-PNG.png")
LOGO_FSRA    = resource_path("LOGO FSRA_BELA.png")


def _load_ctk_image(path: Path, target_width: int | None = None,
                    target_height: int | None = None) -> "ctk.CTkImage | None":
    """
    Load a PNG as a CTkImage scaled to target_width OR target_height,
    maintaining aspect ratio.  Uses Image.LANCZOS for crisp resampling.
    Returns None and prints a warning if the file is missing or unreadable.
    """
    if not path.exists():
        print(f"[WARNING] Logo not found — skipping: {path}")
        return None
    try:
        pil_img = Image.open(path).convert("RGBA")
        orig_w, orig_h = pil_img.size

        if target_width and not target_height:
            ratio        = target_width / orig_w
            target_height = int(orig_h * ratio)
        elif target_height and not target_width:
            ratio        = target_height / orig_h
            target_width  = int(orig_w * ratio)
        elif not target_width and not target_height:
            target_width, target_height = orig_w, orig_h

        pil_img = pil_img.resize((target_width, target_height), Image.LANCZOS)
        return ctk.CTkImage(light_image=pil_img, dark_image=pil_img,
                            size=(target_width, target_height))
    except Exception as e:
        print(f"[WARNING] Could not load logo '{path.name}': {e}")
        return None

# ── Red Bull colour palette ───────────────────────────────────────────────────
RB_RED     = "#DB0030"   # Red Bull red — accent / highlights
RB_WHITE   = "#FFFFFF"
RB_LIGHT   = "#E8EDF2"   # off-white for secondary text

# Background layers — original dark navy, shifted a touch lighter/bluer
RB_BG      = "#002B45"   # main window background (lighter navy-blue)
RB_PANEL   = "#003A5C"   # card / panel background
RB_ROW_ALT = "#002030"   # alternating table row
RB_BORDER  = "#004F78"   # subtle border colour

# Podium & highlight colours
RB_GOLD       = "#FFD700"   # P1 highlight
RB_SILVER     = "#C0C0C0"   # P2 highlight
RB_BRONZE     = "#CD7F32"   # P3 highlight
RB_YELLOW     = "#FFCC00"   # newest-entry highlight (Red Bull Yellow)
RB_YELLOW_BG  = "#2A2000"   # settled row background (dark yellow tint)
RB_YELLOW_DIM = "#FFCC00"   # flash "bright" pulse — full Red Bull Yellow

FLASH_COUNT       = 6        # number of on/off flash cycles after submit
FLASH_INTERVAL_MS = 220      # milliseconds per flash step

# ── CSV helpers ───────────────────────────────────────────────────────────────
CSV_HEADERS = ["Name", "Time (m:ss.fff)", "Timestamp"]


def _ensure_csv() -> None:
    """Create the CSV with headers if it doesn't exist yet."""
    if not DATA_FILE.exists():
        with open(DATA_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)


def _time_to_seconds(time_str: str) -> float:
    """
    Convert a lap time string 'm:ss.fff' → total float seconds.
    Returns infinity on parse failure so bad rows sort to the bottom.
    """
    try:
        m_part, s_part = time_str.strip().split(":")
        s_val, ms_val  = s_part.split(".")
        return int(m_part) * 60 + int(s_val) + int(ms_val) / 1000
    except Exception:
        return float("inf")


def _seconds_to_display(total: float) -> str:
    """Convert float seconds back to a human-readable 'm:ss.fff' string."""
    if total == float("inf"):
        return "??:??.???"
    minutes = int(total // 60)
    secs    = total % 60
    return f"{minutes}:{secs:06.3f}"


def _load_dataframe() -> pd.DataFrame:
    """
    Read the CSV and return a DataFrame with a numeric 'Seconds' column.
    The DataFrame is NOT yet sorted here — sorting happens in the caller.
    """
    _ensure_csv()
    df = pd.read_csv(DATA_FILE, dtype=str)

    # Guard: if the expected time column is missing entirely, return empty
    if "Time (m:ss.fff)" not in df.columns:
        print("[WARNING] CSV missing 'Time (m:ss.fff)' column — returning empty DataFrame.")
        return pd.DataFrame(columns=["Name", "Time (m:ss.fff)", "Timestamp", "Seconds"])

    # Keep only rows that have both Name and Time
    df = df.dropna(subset=["Name", "Time (m:ss.fff)"])
    df = df[df["Name"].str.strip() != ""]
    df["Seconds"] = df["Time (m:ss.fff)"].apply(_time_to_seconds)

    # Drop rows where time couldn't be parsed (inf means bad format)
    df = df[df["Seconds"] != float("inf")]

    return df


def _append_to_csv(name: str, lap_time: str) -> None:
    """Append a single new lap-time record to the CSV."""
    _ensure_csv()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(DATA_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([name.strip(), lap_time.strip(), ts])


# ── Main application ──────────────────────────────────────────────────────────
class LeaderboardApp(ctk.CTk):
    REFRESH_INTERVAL_MS = 15_000   # auto-refresh every 15 s

    def __init__(self):
        super().__init__()

        # ── Window setup ──────────────────────────────────────────────────────
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title("Red Bull Simulator — Leaderboard")
        self.configure(fg_color=RB_BG)

        # Maximise on launch (works on all platforms without taskbar issues)
        self.after(0, lambda: self.state("zoomed"))

        # ── Highlight / flash state ───────────────────────────────────────────
        # _highlighted_entry: (name, lap_time_str) of the most-recently submitted
        #   row.  Set in _submit_time(), cleared when the next submission starts.
        # _highlighted_frame: the CTkFrame widget for that row, so the flash
        #   callback can toggle its background colour directly.
        # _flash_job:  after() job id so we can cancel a running flash sequence
        #   the moment a new submission arrives.
        self._highlighted_entry: tuple | None  = None
        self._highlighted_frame: object | None = None   # CTkFrame | None
        self._flash_job:         str   | None  = None

        # ── Build UI ──────────────────────────────────────────────────────────
        self._build_header()
        self._build_table()
        self._build_entry_panel()
        self._build_footer()

        # ── Initial data load ─────────────────────────────────────────────────
        self._reload_data()

        # ── Schedule background auto-refresh ──────────────────────────────────
        self._schedule_refresh()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=RB_RED, corner_radius=0, height=90)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        # ── Left: Red Bull logo ────────────────────────────────────────────────
        rb_logo_img = _load_ctk_image(LOGO_REDBULL, target_width=150)
        if rb_logo_img:
            ctk.CTkLabel(
                header,
                image=rb_logo_img,
                text="",
                fg_color="transparent",
            ).pack(side="left", padx=(16, 4), pady=8)
        else:
            ctk.CTkLabel(
                header,
                text="🏎",
                font=ctk.CTkFont(size=36),
                fg_color="transparent",
            ).pack(side="left", padx=(16, 4))

        # ── Centre: title text — Red Bull Red on white ─────────────────────────
        ctk.CTkLabel(
            header,
            text="RED BULL SIMULATOR  —  FASTEST LAPS",
            font=ctk.CTkFont(family="Impact", size=32, weight="bold"),
            text_color=RB_WHITE,
        ).pack(side="left", padx=(8, 30), pady=0)

        # ── Right: live clock — white on red ───────────────────────────────────
        self._clock_label = ctk.CTkLabel(
            header,
            text="",
            font=ctk.CTkFont(family="Consolas", size=20, weight="bold"),
            text_color=RB_WHITE,
        )
        self._clock_label.pack(side="right", padx=30)
        self._tick_clock()

    def _build_table(self):
        """
        The leaderboard table lives in a scrollable frame.
        We use a grid of CTkLabels so we can colour individual rows (P1/P2/P3).
        """
        outer = ctk.CTkFrame(self, fg_color=RB_BG, corner_radius=0)
        outer.pack(fill="both", expand=True, padx=20, pady=(10, 0))

        # Column header row
        col_cfg = [
            ("POS",          60,  "center"),
            ("DRIVER",       340, "w"),
            ("LAP TIME",     180, "center"),
            ("GAP",          160, "center"),
            ("DELTA",        160, "center"),
        ]
        self._col_cfg = col_cfg

        header_row = ctk.CTkFrame(outer, fg_color=RB_PANEL, corner_radius=6)
        header_row.pack(fill="x", padx=0, pady=(0, 4))

        for col_text, col_w, anchor in col_cfg:
            ctk.CTkLabel(
                header_row,
                text=col_text,
                font=ctk.CTkFont(family="Impact", size=18),
                text_color=RB_RED,
                width=col_w,
                anchor=anchor,
            ).pack(side="left", padx=10, pady=8)

        # Scrollable area that holds the data rows
        self._scroll = ctk.CTkScrollableFrame(
            outer,
            fg_color=RB_BG,
            scrollbar_button_color=RB_RED,
            scrollbar_button_hover_color="#a00022",
            corner_radius=0,
        )
        self._scroll.pack(fill="both", expand=True)
        self._row_frames = []

    def _build_entry_panel(self):
        """Input area: driver name + lap time + submit button + FSRA logo."""
        panel = ctk.CTkFrame(self, fg_color=RB_PANEL, corner_radius=10, height=90)
        panel.pack(fill="x", padx=20, pady=10)
        panel.pack_propagate(False)

        # ── Right side first so it anchors flush to the right edge ───────────
        fsra_img = _load_ctk_image(LOGO_FSRA, target_height=60)
        if fsra_img:
            ctk.CTkLabel(
                panel,
                image=fsra_img,
                text="",
                fg_color="transparent",
            ).pack(side="right", padx=(8, 20), pady=0)

        # ── Left side: label + inputs + button + status ───────────────────────
        ctk.CTkLabel(
            panel, text="ADD LAP TIME",
            font=ctk.CTkFont(family="Impact", size=16),
            text_color=RB_RED,
        ).pack(side="left", padx=(20, 8), pady=0)

        self._name_var = ctk.StringVar()
        self._time_var = ctk.StringVar()

        name_entry = ctk.CTkEntry(
            panel,
            textvariable=self._name_var,
            placeholder_text="Driver Name",
            width=220,
            font=ctk.CTkFont(family="Consolas", size=16),
            fg_color=RB_BORDER,
            text_color=RB_WHITE,
            border_color=RB_RED,
            border_width=2,
        )
        name_entry.pack(side="left", padx=8, pady=18)

        time_entry = ctk.CTkEntry(
            panel,
            textvariable=self._time_var,
            placeholder_text="m:ss.fff  (e.g. 1:23.456)",
            width=220,
            font=ctk.CTkFont(family="Consolas", size=16),
            fg_color=RB_BORDER,
            text_color=RB_WHITE,
            border_color=RB_RED,
            border_width=2,
        )
        time_entry.pack(side="left", padx=8, pady=18)
        time_entry.bind("<Return>", lambda e: self._submit_time())

        ctk.CTkButton(
            panel,
            text="SUBMIT",
            command=self._submit_time,
            fg_color=RB_RED,
            hover_color="#a00022",
            text_color=RB_WHITE,
            font=ctk.CTkFont(family="Impact", size=18),
            width=120,
            height=44,
            corner_radius=6,
        ).pack(side="left", padx=12)

        self._status_label = ctk.CTkLabel(
            panel,
            text="",
            font=ctk.CTkFont(family="Consolas", size=14),
            text_color=RB_LIGHT,
        )
        self._status_label.pack(side="left", padx=16)

    def _build_footer(self):
        footer = ctk.CTkFrame(self, fg_color=RB_BG, corner_radius=0, height=28)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        ctk.CTkLabel(
            footer,
            text=f"Data saved to: {DATA_FILE}   •   Auto-refreshes every {self.REFRESH_INTERVAL_MS//1000}s",
            font=ctk.CTkFont(family="Consolas", size=11),
            text_color=RB_BORDER,
        ).pack(side="left", padx=20)

    # ── Data / sorting logic ──────────────────────────────────────────────────

    def _reload_data(self, trigger_highlight: bool = False):
        """
        Core sorting routine — called after every submit and on the timer.
        Wrapped in try/except so any error prints to the terminal rather than
        silently failing and leaving the leaderboard blank.
        """
        try:
            df = _load_dataframe()

            if df.empty:
                self._clear_rows()
                return

            # ── SORT: ascending by lap time in seconds ────────────────────────
            df = df.sort_values(by="Seconds", ascending=True).reset_index(drop=True)
            top10 = df.head(10).copy()

            leader_time = top10.loc[0, "Seconds"]

            # Gap to leader (P1)
            top10["Gap"]   = top10["Seconds"] - leader_time
            # Delta to previous row (NaN for P1, computed via diff)
            top10["Delta"] = top10["Seconds"].diff()

            highlighted_pos = self._populate_rows(top10, leader_time)

            # Post-render: scroll + flash only on a brand-new submission
            if trigger_highlight and highlighted_pos is not None:
                self._scroll_to_row(highlighted_pos)
                self._start_flash(highlighted_pos)

        except Exception as e:
            print(f"[ERROR] _reload_data failed: {e}")

    def _populate_rows(self, df: pd.DataFrame, leader_time: float) -> int | None:
        """
        Clear old rows and render fresh ones from the sorted DataFrame.

        Returns the 0-based index (into self._row_frames) of the highlighted
        row, or None if no highlight is active.
        """
        self._clear_rows()
        self._highlighted_frame = None   # reset stale ref after clear
        highlighted_pos = None

        for pos_idx, (_, row) in enumerate(df.iterrows()):
            position = pos_idx + 1   # 1-indexed

            # ── Detect the highlighted (most-recently submitted) row ──────────
            # We match on both name AND time so duplicate names don't collide.
            is_new = (
                self._highlighted_entry is not None
                and row["Name"].strip().lower()         == self._highlighted_entry[0].strip().lower()
                and row["Time (m:ss.fff)"].strip()      == self._highlighted_entry[1].strip()
            )

            # ── Choose row colours ────────────────────────────────────────────
            if is_new:
                row_bg     = RB_YELLOW_BG
                pos_colour = RB_YELLOW
                highlighted_pos = pos_idx
            elif position == 1:
                row_bg     = "#1A2A00"   # dark gold tint
                pos_colour = RB_GOLD
            elif position == 2:
                row_bg     = "#1A1A1A"
                pos_colour = RB_SILVER
            elif position == 3:
                row_bg     = "#1A0D00"   # dark bronze tint
                pos_colour = RB_BRONZE
            else:
                row_bg     = RB_PANEL if position % 2 == 0 else RB_ROW_ALT
                pos_colour = RB_LIGHT

            # ── Build the row frame ───────────────────────────────────────────
            frame = ctk.CTkFrame(
                self._scroll,
                fg_color=row_bg,
                corner_radius=4,
                height=52,
                # Yellow border for the highlighted row
                border_width=2 if is_new else 0,
                border_color=RB_YELLOW if is_new else RB_BORDER,
            )
            frame.pack(fill="x", padx=0, pady=2)
            frame.pack_propagate(False)
            self._row_frames.append(frame)

            if is_new:
                self._highlighted_frame = frame   # keep ref for flash toggling

            # Position badge
            ctk.CTkLabel(
                frame,
                text=f"P{position}",
                font=ctk.CTkFont(family="Impact", size=22),
                text_color=pos_colour,
                width=60, anchor="center",
            ).pack(side="left", padx=10)

            # Driver name
            ctk.CTkLabel(
                frame,
                text=row["Name"].upper(),
                font=ctk.CTkFont(family="Impact", size=22),
                text_color=RB_YELLOW if is_new else RB_WHITE,
                width=340, anchor="w",
            ).pack(side="left", padx=10)

            # Lap time
            ctk.CTkLabel(
                frame,
                text=_seconds_to_display(row["Seconds"]),
                font=ctk.CTkFont(family="Consolas", size=20, weight="bold"),
                text_color=RB_YELLOW if is_new else (RB_GOLD if position == 1 else RB_LIGHT),
                width=180, anchor="center",
            ).pack(side="left", padx=10)

            # Gap to leader
            gap_text = "-" if position == 1 else f"+{row['Gap']:.3f}"
            ctk.CTkLabel(
                frame,
                text=gap_text,
                font=ctk.CTkFont(family="Consolas", size=18),
                text_color=RB_RED if (position > 1 and not is_new) else (RB_YELLOW if is_new else RB_LIGHT),
                width=160, anchor="center",
            ).pack(side="left", padx=10)

            # Delta to car ahead
            if position == 1 or pd.isna(row["Delta"]):
                delta_text = "-"
            else:
                delta_text = f"+{row['Delta']:.3f}"
            ctk.CTkLabel(
                frame,
                text=delta_text,
                font=ctk.CTkFont(family="Consolas", size=18),
                text_color=RB_YELLOW if is_new else RB_LIGHT,
                width=160, anchor="center",
            ).pack(side="left", padx=10)

        return highlighted_pos

    def _clear_rows(self):
        """Destroy all current data-row widgets."""
        for f in self._row_frames:
            f.destroy()
        self._row_frames.clear()

    # ── Highlight helpers ─────────────────────────────────────────────────────

    def _scroll_to_row(self, row_idx: int):
        """
        Scroll the CTkScrollableFrame so the highlighted row is visible.

        CTkScrollableFrame wraps a Canvas internally.  We measure where the
        target row frame sits relative to the total scrollable height and set
        the canvas yview fraction accordingly, nudging up by half a row so the
        target lands roughly centred on screen.
        """
        def _do_scroll():
            try:
                canvas = self._scroll._parent_canvas          # internal canvas
                total_rows = len(self._row_frames)
                if total_rows == 0:
                    return
                # Fraction of the way down the list this row sits
                frac = row_idx / total_rows
                # Back off by a small amount so the row isn't at the very bottom
                frac = max(0.0, frac - (1 / max(total_rows, 1)))
                canvas.yview_moveto(frac)
            except Exception:
                pass   # silently skip if the internal API ever changes

        # Give the layout one frame to settle before scrolling
        self.after(50, _do_scroll)

    def _start_flash(self, row_idx: int):
        """
        Flash the highlighted row's background between yellow and dim-yellow
        FLASH_COUNT times, then leave it glowing steady yellow.
        The flash is driven by recursive after() calls so it never blocks the UI.
        """
        # Cancel any previous flash that might still be running
        if self._flash_job is not None:
            try:
                self.after_cancel(self._flash_job)
            except Exception:
                pass
            self._flash_job = None

        def _step(remaining: int, bright: bool):
            frame = self._highlighted_frame
            if frame is None or not frame.winfo_exists():
                return   # row was destroyed before flash finished — stop safely

            # bright=True  → full yellow pulse (#FFCC00)
            # bright=False → dark yellow tint  (#2A2000)
            frame.configure(fg_color=RB_YELLOW_DIM if bright else RB_YELLOW_BG)

            if remaining > 0:
                self._flash_job = self.after(
                    FLASH_INTERVAL_MS,
                    lambda: _step(remaining - 1, not bright),
                )
            else:
                # Flash complete — settle on the bright highlight permanently
                frame.configure(fg_color=RB_YELLOW_BG)
                self._flash_job = None

        _step(FLASH_COUNT * 2, bright=False)   # *2 because each cycle = on + off

    # ── Submit handler ────────────────────────────────────────────────────────

    def _submit_time(self):
        name      = self._name_var.get().strip()
        lap_time  = self._time_var.get().strip()

        # Basic validation
        if not name:
            self._set_status("⚠  Please enter a driver name.", error=True)
            return
        if _time_to_seconds(lap_time) == float("inf"):
            self._set_status("⚠  Invalid time. Use format  m:ss.fff  e.g. 1:23.456", error=True)
            return

        # ── Cancel any running flash from the previous entry ──────────────────
        if self._flash_job is not None:
            try:
                self.after_cancel(self._flash_job)
            except Exception:
                pass
            self._flash_job = None

        # ── Tag this entry so _populate_rows can identify and highlight it ────
        self._highlighted_entry = (name, lap_time)

        # Persist to CSV — wrapped so a write error surfaces in the terminal
        try:
            _append_to_csv(name, lap_time)
        except Exception as e:
            self._set_status(f"⚠  Save failed: {e}", error=True)
            print(f"[ERROR] Could not write to CSV: {e}")
            return

        self._name_var.set("")
        self._time_var.set("")
        self._set_status(f"✔  {name}  —  {lap_time}  saved!", error=False)

        # Immediate UI update after submission — trigger_highlight=True fires
        # the scroll-to-row and flash animation after rendering
        self._reload_data(trigger_highlight=True)

    def _set_status(self, msg: str, error: bool = False):
        colour = RB_RED if error else "#00C853"
        self._status_label.configure(text=msg, text_color=colour)
        # Clear message after 4 s
        self.after(4000, lambda: self._status_label.configure(text=""))

    # ── Clock & refresh timer ─────────────────────────────────────────────────

    def _tick_clock(self):
        self._clock_label.configure(text=datetime.now().strftime("%H:%M:%S"))
        self.after(1000, self._tick_clock)

    def _schedule_refresh(self):
        """Auto-refresh in the background without blocking the UI."""
        def _bg():
            self._reload_data()
            self.after(self.REFRESH_INTERVAL_MS, self._schedule_refresh)
        self.after(self.REFRESH_INTERVAL_MS, _bg)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = LeaderboardApp()
    app.mainloop()