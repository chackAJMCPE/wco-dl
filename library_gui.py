#!/usr/bin/env python3
"""
GUI library viewer for wco-dl.

Features:
- Browse series -> seasons -> episodes
- Play episodes with the system default player
- Delete episodes, seasons, or entire series
- Delete both database records and associated video files
- Refresh the library without restarting
"""

from __future__ import annotations

import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk


DEFAULT_DB_PATH = "./downloads/library.db"


# ----------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------

class LibraryDB:
    def __init__(self, db_path: pathlib.Path):
        self.db_path = db_path
        self.download_folder = db_path.parent

        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._check_schema()

    def _check_schema(self) -> None:
        required = {"series", "seasons", "episodes"}
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        existing = {row[0] for row in rows}
        missing = required - existing
        if missing:
            raise RuntimeError(
                "Invalid wco-dl database. Missing table(s): "
                + ", ".join(sorted(missing))
            )

    def get_series(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, name FROM series ORDER BY name COLLATE NOCASE"
        ).fetchall()

    def get_seasons(self, series_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT id, name
            FROM seasons
            WHERE series_id = ?
            ORDER BY
                CASE
                    WHEN name LIKE 'Season %' THEN 0
                    WHEN name = 'OVA' THEN 1
                    WHEN name = 'Movies' THEN 2
                    ELSE 3
                END,
                name COLLATE NOCASE
            """,
            (series_id,),
        ).fetchall()

    @staticmethod
    def _episode_sort_key(row: sqlite3.Row) -> tuple:
        name = re.sub(r"\s*\[[^\]]+\]$", "", row["episode_name"])
        m = re.search(r"(\d+(?:\.\d+)?)", name)
        return (0, float(m.group(1)), name.casefold()) if m else (1, 0.0, name.casefold())

    def get_episodes(self, season_id: int) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            """
            SELECT id, episode_name, filename, language
            FROM episodes
            WHERE season_id = ?
            """,
            (season_id,),
        ).fetchall()
        rows.sort(key=self._episode_sort_key)
        return rows

    def get_series_info(self, series_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT id, name FROM series WHERE id = ?",
            (series_id,),
        ).fetchone()

    def get_season_info(self, season_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT id, series_id, name FROM seasons WHERE id = ?",
            (season_id,),
        ).fetchone()

    def get_episode_info(self, episode_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT
                e.id,
                e.episode_name,
                e.filename,
                e.language,
                s.id AS season_id,
                s.name AS season_name,
                ser.id AS series_id,
                ser.name AS series_name
            FROM episodes e
            JOIN seasons s ON e.season_id = s.id
            JOIN series ser ON s.series_id = ser.id
            WHERE e.id = ?
            """,
            (episode_id,),
        ).fetchone()

    def delete_episode(self, episode_id: int) -> tuple[bool, str]:
        row = self.get_episode_info(episode_id)
        if not row:
            return False, "Episode no longer exists."

        filename = row["filename"]
        video_path = self.download_folder / filename

        with self.conn:
            self.conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))

        file_error = ""
        if video_path.exists():
            try:
                video_path.unlink()
            except OSError as exc:
                file_error = f" Database entry deleted, but file could not be removed: {exc}"

        return True, file_error

    def delete_season(self, season_id: int) -> tuple[bool, str]:
        season = self.get_season_info(season_id)
        if not season:
            return False, "Season no longer exists."

        episodes = self.get_episodes(season_id)
        failures: list[str] = []

        with self.conn:
            self.conn.execute("DELETE FROM seasons WHERE id = ?", (season_id,))

        for ep in episodes:
            path = self.download_folder / ep["filename"]
            if path.exists():
                try:
                    path.unlink()
                except OSError as exc:
                    failures.append(f"{path.name}: {exc}")

        if failures:
            return True, (
                "Season deleted from the database, but some files could not be removed:\n"
                + "\n".join(failures)
            )
        return True, ""

    def delete_series(self, series_id: int) -> tuple[bool, str]:
        series = self.get_series_info(series_id)
        if not series:
            return False, "Series no longer exists."

        seasons = self.get_seasons(series_id)
        episode_files: list[pathlib.Path] = []

        for season in seasons:
            for ep in self.get_episodes(season["id"]):
                episode_files.append(self.download_folder / ep["filename"])

        with self.conn:
            self.conn.execute("DELETE FROM series WHERE id = ?", (series_id,))

        failures: list[str] = []
        for path in episode_files:
            if path.exists():
                try:
                    path.unlink()
                except OSError as exc:
                    failures.append(f"{path.name}: {exc}")

        if failures:
            return True, (
                "Series deleted from the database, but some files could not be removed:\n"
                + "\n".join(failures)
            )
        return True, ""

    def close(self) -> None:
        self.conn.close()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def locate_db() -> pathlib.Path | None:
    default = pathlib.Path(DEFAULT_DB_PATH)

    if default.is_file():
        return default

    chosen = input(
        f"library.db not found at '{default}'. "
        "Enter the database path (or press Enter to quit): "
    ).strip()

    if not chosen:
        return None

    path = pathlib.Path(chosen).expanduser()
    return path if path.is_file() else None


def play_file(filepath: pathlib.Path) -> None:
    if not filepath.exists():
        messagebox.showerror(
            "File not found",
            f"The video file does not exist:\n\n{filepath}",
        )
        return

    try:
        if sys.platform == "win32":
            os.startfile(str(filepath))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(filepath)])
        else:
            try:
                subprocess.Popen(["xdg-open", str(filepath)])
            except FileNotFoundError:
                for player in ("mpv", "vlc"):
                    if _command_exists(player):
                        subprocess.Popen([player, str(filepath)])
                        break
                else:
                    raise RuntimeError(
                        "No supported player found. Install xdg-open, mpv, or VLC."
                    )
    except Exception as exc:
        messagebox.showerror("Playback error", str(exc))


def _command_exists(command: str) -> bool:
    import shutil
    return shutil.which(command) is not None


def clean_episode_name(name: str) -> str:
    return re.sub(r"\s*\[[^\]]+\]$", "", name)


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

class LibraryApp(tk.Tk):
    def __init__(self, db: LibraryDB):
        super().__init__()

        self.db = db
        self.title("wco-dl Library")
        self.geometry("1000x650")
        self.minsize(750, 450)

        self.selected_type: str | None = None
        self.selected_id: int | None = None
        self._closing = False

        self._setup_style()
        self._build_ui()
        self.refresh()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass

        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10))
        style.configure("Treeview", rowheight=28)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=(12, 12, 12, 8))
        top.pack(fill="x")

        ttk.Label(top, text="wco-dl Library", style="Title.TLabel").pack(anchor="w")
        self.status_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.status_var, style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))

        toolbar = ttk.Frame(self, padding=(12, 0, 12, 8))
        toolbar.pack(fill="x")

        ttk.Button(toolbar, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(toolbar, text="Play", command=self.play_selected).pack(side="left", padx=(6, 0))

        self.delete_button = ttk.Button(
            toolbar,
            text="Delete",
            command=self.delete_selected,
            state="disabled",
        )
        self.delete_button.pack(side="left", padx=(6, 0))

        ttk.Label(
            toolbar,
            text="Double-click an episode to play",
        ).pack(side="right")

        container = ttk.Frame(self, padding=(12, 0, 12, 12))
        container.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(
            container,
            columns=("kind", "language", "file"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="Library")
        self.tree.heading("kind", text="Type")
        self.tree.heading("language", text="Language")
        self.tree.heading("file", text="File")

        self.tree.column("#0", width=470, anchor="w")
        self.tree.column("kind", width=100, anchor="center")
        self.tree.column("language", width=110, anchor="center")
        self.tree.column("file", width=250, anchor="w")

        yscroll = ttk.Scrollbar(container, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(container, orient="horizontal", command=self.tree.xview)

        self.tree.configure(
            yscrollcommand=yscroll.set,
            xscrollcommand=xscroll.set,
        )

        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")

        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", lambda _event: self.play_selected())
        self.tree.bind("<Delete>", lambda _event: self.delete_selected())

        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Play", command=self.play_selected)
        menu.add_separator()
        menu.add_command(label="Delete", command=self.delete_selected)
        self.context_menu = menu
        self.tree.bind("<Button-3>", self._show_context_menu)

    def _show_context_menu(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.tk_popup(event.x_root, event.y_root)

    def refresh(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        series_rows = self.db.get_series()
        total_series = len(series_rows)
        total_episodes = 0

        for series in series_rows:
            series_iid = f"series:{series['id']}"
            series_item = self.tree.insert(
                "",
                "end",
                iid=series_iid,
                text=series["name"],
                values=("Series", "", ""),
                open=False,
            )

            for season in self.db.get_seasons(series["id"]):
                season_iid = f"season:{season['id']}"
                season_item = self.tree.insert(
                    series_item,
                    "end",
                    iid=season_iid,
                    text=season["name"],
                    values=("Season", "", ""),
                    open=False,
                )

                episodes = self.db.get_episodes(season["id"])
                total_episodes += len(episodes)

                for ep in episodes:
                    ep_iid = f"episode:{ep['id']}"
                    self.tree.insert(
                        season_item,
                        "end",
                        iid=ep_iid,
                        text=clean_episode_name(ep["episode_name"]),
                        values=("Episode", ep["language"], ep["filename"]),
                    )

        self.status_var.set(
            f"{total_series} series • {total_episodes} episodes • "
            f"Database: {self.db.db_path}"
        )
        self._clear_selection()

    def _clear_selection(self) -> None:
        self.selected_type = None
        self.selected_id = None
        self.delete_button.configure(state="disabled")

    def on_select(self, _event: tk.Event | None = None) -> None:
        selection = self.tree.selection()
        if not selection:
            self._clear_selection()
            return

        iid = selection[0]
        try:
            kind, raw_id = iid.split(":", 1)
            self.selected_type = kind
            self.selected_id = int(raw_id)
            self.delete_button.configure(state="normal")
        except (ValueError, AttributeError):
            self._clear_selection()

    def play_selected(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Play", "Select an episode first.")
            return

        iid = selection[0]
        try:
            kind, raw_id = iid.split(":", 1)
            if kind != "episode":
                messagebox.showinfo("Play", "Select an episode to play.")
                return
            episode_id = int(raw_id)
        except (ValueError, AttributeError):
            messagebox.showerror("Play", "Invalid selection.")
            return

        episode = self.db.get_episode_info(episode_id)
        if not episode:
            self.refresh()
            messagebox.showerror("Play", "That episode no longer exists.")
            return

        video_path = self.db.download_folder / episode["filename"]
        play_file(video_path)

    def delete_selected(self) -> None:
        if not self.selected_type or self.selected_id is None:
            return

        kind = self.selected_type
        item_id = self.selected_id

        if kind == "episode":
            episode = self.db.get_episode_info(item_id)
            if not episode:
                self.refresh()
                return

            title = clean_episode_name(episode["episode_name"])
            question = (
                f"Delete this episode?\n\n"
                f"{episode['series_name']} / {episode['season_name']} / {title}\n\n"
                f"This will remove the database entry and the video file."
            )

            if not messagebox.askyesno("Delete episode", question, icon="warning"):
                return

            ok, warning = self.db.delete_episode(item_id)
            if ok:
                self.refresh()
                if warning:
                    messagebox.showwarning("Episode deleted", warning)
            else:
                messagebox.showerror("Delete failed", warning)
            return

        if kind == "season":
            season = self.db.get_season_info(item_id)
            if not season:
                self.refresh()
                return

            episodes = self.db.get_episodes(item_id)
            question = (
                f"Delete season '{season['name']}'?\n\n"
                f"This will permanently delete {len(episodes)} episode(s), "
                f"their database records, and their video files."
            )

            if not messagebox.askyesno("Delete season", question, icon="warning"):
                return

            ok, warning = self.db.delete_season(item_id)
            if ok:
                self.refresh()
                if warning:
                    messagebox.showwarning("Season deleted", warning)
            else:
                messagebox.showerror("Delete failed", warning)
            return

        if kind == "series":
            series = self.db.get_series_info(item_id)
            if not series:
                self.refresh()
                return

            seasons = self.db.get_seasons(item_id)
            episode_count = sum(len(self.db.get_episodes(s["id"])) for s in seasons)

            question = (
                f"Delete series '{series['name']}'?\n\n"
                f"This will permanently delete {len(seasons)} season(s), "
                f"{episode_count} episode(s), all database records, "
                f"and all associated video files."
            )

            if not messagebox.askyesno("Delete series", question, icon="warning"):
                return

            ok, warning = self.db.delete_series(item_id)
            if ok:
                self.refresh()
                if warning:
                    messagebox.showwarning("Series deleted", warning)
            else:
                messagebox.showerror("Delete failed", warning)

    def on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self.db.close()
        finally:
            self.destroy()


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def main() -> None:
    db_path = locate_db()
    if db_path is None:
        messagebox.showerror(
            "Database not found",
            "Could not locate downloads/library.db.",
        )
        return

    try:
        db = LibraryDB(db_path)
    except Exception as exc:
        messagebox.showerror("Database error", str(exc))
        return

    app = LibraryApp(db)
    app.mainloop()


if __name__ == "__main__":
    main()
