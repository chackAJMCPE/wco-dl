#!/usr/bin/env python3
"""
Simple library viewer for wco-dl.
Displays series, seasons, episodes with language, and allows playing episodes.
"""

import os
import pathlib
import sqlite3
import subprocess
import sys
import webbrowser
from typing import Optional

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

DEFAULT_DB_PATH = "./downloads/library.db"


def get_db_path() -> pathlib.Path:
    """Return the path to library.db, or exit if not found."""
    db_path = pathlib.Path(DEFAULT_DB_PATH)
    if not db_path.is_file():
        # Try to find it in the current directory or user input
        user_input = input(f"library.db not found at '{db_path}'. Enter path to database file: ").strip()
        if user_input:
            db_path = pathlib.Path(user_input)
        if not db_path.is_file():
            print(f"Error: Database file '{db_path}' not found.")
            sys.exit(1)
    return db_path


def get_series_list(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Return list of (series_id, series_name)."""
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM series ORDER BY name")
    return cur.fetchall()


def get_seasons_for_series(conn: sqlite3.Connection, series_id: int) -> list[tuple[int, str]]:
    """Return list of (season_id, season_name)."""
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM seasons WHERE series_id = ? ORDER BY name", (series_id,))
    return cur.fetchall()


def get_episodes_for_season(conn: sqlite3.Connection, season_id: int) -> list[tuple[int, str, str, str]]:
    """Return list of (episode_id, episode_name, filename, language)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, episode_name, filename, language FROM episodes WHERE season_id = ? ORDER BY episode_name",
        (season_id,)
    )
    return cur.fetchall()


def play_file(filepath: str) -> None:
    """Play video file using system default player."""
    if not os.path.exists(filepath):
        print(f"Error: File '{filepath}' not found.")
        return
    print(f"Playing: {filepath}")
    try:
        if sys.platform == "win32":
            os.startfile(filepath)
        elif sys.platform == "darwin":
            subprocess.run(["open", filepath])
        else:
            # Try xdg-open, fallback to mpv/vlc
            try:
                subprocess.run(["xdg-open", filepath], check=True)
            except FileNotFoundError:
                subprocess.run(["mpv", filepath])
    except Exception as e:
        print(f"Error opening file: {e}")


def main() -> None:
    db_path = get_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    while True:
        # List series
        series = get_series_list(conn)
        if not series:
            print("No series found in database.")
            break

        print("\n--- Library ---")
        for idx, (sid, name) in enumerate(series, 1):
            print(f"{idx}. {name}")

        print("\nOptions:")
        print("  Enter number to browse series")
        print("  q / quit  to exit")
        choice = input("\nChoice: ").strip().lower()
        if choice in ("q", "quit"):
            break

        try:
            idx = int(choice)
            if idx < 1 or idx > len(series):
                print("Invalid selection.")
                continue
            series_id, series_name = series[idx - 1]
        except ValueError:
            print("Invalid input.")
            continue

        # Now browse seasons
        while True:
            seasons = get_seasons_for_series(conn, series_id)
            print(f"\n--- {series_name} --- Seasons:")
            if not seasons:
                print("No seasons found.")
                break
            for j, (sid, name) in enumerate(seasons, 1):
                print(f"{j}. {name}")
            print("  b / back  to go back to series list")
            subchoice = input("Choose season (or 'b'): ").strip().lower()
            if subchoice == "b":
                break
            try:
                s_idx = int(subchoice)
                if s_idx < 1 or s_idx > len(seasons):
                    print("Invalid selection.")
                    continue
                season_id, season_name = seasons[s_idx - 1]
            except ValueError:
                print("Invalid input.")
                continue

            # Browse episodes
            while True:
                episodes = get_episodes_for_season(conn, season_id)
                print(f"\n--- {series_name} - {season_name} --- Episodes:")
                if not episodes:
                    print("No episodes in this season.")
                    break
                for k, (ep_id, ep_name, filename, lang) in enumerate(episodes, 1):
                    print(f"{k}. {ep_name} ({lang})")
                print("  b / back  to go back to seasons")
                ep_choice = input("Choose episode number to play (or 'b'): ").strip().lower()
                if ep_choice == "b":
                    break
                try:
                    ep_idx = int(ep_choice)
                    if ep_idx < 1 or ep_idx > len(episodes):
                        print("Invalid selection.")
                        continue
                    ep_id, ep_name, filename, lang = episodes[ep_idx - 1]
                    # Construct full path to video
                    download_folder = pathlib.Path(db_path).parent
                    video_path = download_folder / filename
                    play_file(str(video_path))
                except ValueError:
                    print("Invalid input.")
                    continue

    conn.close()


if __name__ == "__main__":
    main()