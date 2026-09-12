"""
wco-dl — download anime & cartoons from wco.tv
"""

import io
import json
import logging
import pathlib
import re
import shutil
import sqlite3
import subprocess
import threading
import traceback
import warnings
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlunparse

import bs4
import cloudscraper
import pydantic
import requests
import typer
from playwright.sync_api import sync_playwright
from tqdm import tqdm

# Suppress harmless Playwright asyncio cleanup warnings on Windows
warnings.filterwarnings("ignore", category=ResourceWarning)

# ---------------------------------------------------------------------------
# Terminal colours
# ---------------------------------------------------------------------------

class C:
    """ANSI colour codes — gracefully disabled on terminals that don't support them."""
    import sys as _sys
    _on = _sys.stdout.isatty() if hasattr(_sys.stdout, 'isatty') else False

    GREEN  = "\033[92m"  if _on else ""
    YELLOW = "\033[93m"  if _on else ""
    RED    = "\033[91m"  if _on else ""
    CYAN   = "\033[96m"  if _on else ""
    DIM    = "\033[2m"   if _on else ""
    BOLD   = "\033[1m"   if _on else ""
    RESET  = "\033[0m"   if _on else ""

class EpisodeLogger:
    """
    Buffers debug lines for the current episode in memory.
    On success the buffer is discarded. On failure it's included in errors.txt.
    """
    def __init__(self):
        self._buf: list[str] = []

    def debug(self, msg: str):
        self._buf.append(msg)

    def flush(self) -> str:
        """Return all buffered lines as a single string and clear the buffer."""
        out = "\n".join(self._buf)
        self._buf.clear()
        return out

    def clear(self):
        self._buf.clear()

logger = EpisodeLogger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

AD_DOMAINS = {"ad.a-ads.com", "doubleclick.net", "googlesyndication.com", "adservice.google"}

DEFAULT_SERIES_LIST = ".wco-dl/series_list.txt"

# Show name aliases — maps any variant to a canonical name.
# Japanese title variants, English title variants, OVAs and movies all
# collapse to the same canonical name so the library stays tidy.
# Key: lowercase slug words  →  Value: canonical title
SHOW_ALIASES: dict[str, str] = {
    # Re:Zero
    "re zero kara hajimeru isekai seikatsu":         "Re:Zero",
    "re zero starting life in another world":         "Re:Zero",
    "rezero kara hajimeru isekai seikatsu":           "Re:Zero",
    "rezero starting life in another world":          "Re:Zero",
    # Slime
    "tensei shitara slime datta ken":                 "That Time I Got Reincarnated as a Slime",
    "that time i got reincarnated as a slime":        "That Time I Got Reincarnated as a Slime",
}

# URL slug fragments that mark an entry as a movie (not an OVA/season)
MOVIE_SLUG_WORDS = {"movie", "film", "the-movie"}


# ---------------------------------------------------------------------------
# Configuration & Settings
# ---------------------------------------------------------------------------

class Settings(pydantic.BaseModel):
    resolution: str = "best"           # "sd" | "hd" | "fhd" | "best"
    download_folder: str = "./downloads"


class Config:
    def __init__(self):
        self.config_dir = pathlib.Path(".wco-dl")
        self.config_dir.mkdir(exist_ok=True)
        self.settings_path = self.config_dir / "settings.json"
        if self.settings_path.exists():
            self.settings = Settings(**json.loads(self.settings_path.read_text("utf-8")))
        else:
            self.settings = Settings()
            self._save()

    def _save(self):
        self.settings_path.write_text(self.settings.model_dump_json(indent=2), "utf-8")


# ---------------------------------------------------------------------------
# Progress (resume tracking)
# ---------------------------------------------------------------------------

class Progress:
    def __init__(self, config: Config):
        self._path = config.config_dir / "progress.json"
        self._data = json.loads(self._path.read_text("utf-8")) if self._path.exists() else {"pending": []}

    def _save(self):
        self._path.write_text(json.dumps(self._data, indent=2), "utf-8")

    def add_pending(self, url: str):
        if url not in self._data["pending"]:
            self._data["pending"].append(url)
            self._save()

    def remove_pending(self, url: str):
        if url in self._data["pending"]:
            self._data["pending"].remove(url)
            self._save()

    def get_pending(self) -> list[str]:
        return self._data["pending"].copy()

    def clear_pending(self):
        self._data["pending"] = []
        self._save()


# ---------------------------------------------------------------------------
# Library database (SQLite)
# ---------------------------------------------------------------------------

class LibraryDB:
    def __init__(self, config: Config):
        self.download_folder = pathlib.Path(config.settings.download_folder)
        self.download_folder.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.download_folder / "library.db"))
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS series (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seasons (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                name      TEXT NOT NULL,
                FOREIGN KEY(series_id) REFERENCES series(id) ON DELETE CASCADE,
                UNIQUE(series_id, name)
            );
            CREATE TABLE IF NOT EXISTS episodes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id    INTEGER NOT NULL,
                episode_name TEXT NOT NULL,
                filename     TEXT NOT NULL,
                language     TEXT NOT NULL,
                FOREIGN KEY(season_id) REFERENCES seasons(id) ON DELETE CASCADE,
                UNIQUE(season_id, episode_name)
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        self.conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('file_counter', '0')")
        self.conn.commit()

    def get_next_filename(self) -> str:
        cur = self.conn.execute(
            "UPDATE meta SET value = CAST(value AS INTEGER) + 1 "
            "WHERE key = 'file_counter' RETURNING CAST(value AS INTEGER)"
        )
        new_val = cur.fetchone()[0]
        self.conn.commit()
        return f"{new_val - 1:08x}.mp4"

    def add_episode(self, series: str, season: str, episode: str, filename: str, language: str):
        self.conn.execute("INSERT OR IGNORE INTO series (name) VALUES (?)", (series,))
        series_id = self.conn.execute("SELECT id FROM series WHERE name = ?", (series,)).fetchone()[0]
        self.conn.execute("INSERT OR IGNORE INTO seasons (series_id, name) VALUES (?, ?)", (series_id, season))
        season_id = self.conn.execute(
            "SELECT id FROM seasons WHERE series_id = ? AND name = ?", (series_id, season)
        ).fetchone()[0]
        self.conn.execute(
            "INSERT OR REPLACE INTO episodes (season_id, episode_name, filename, language) VALUES (?, ?, ?, ?)",
            (season_id, f"{episode} [{language}]", filename, language),
        )
        self.conn.commit()

    def get_episode_filename(self, series: str, season: str, episode: str, language: str) -> str | None:
        row = self.conn.execute(
            """SELECT e.filename FROM episodes e
               JOIN seasons s   ON e.season_id  = s.id
               JOIN series ser  ON s.series_id  = ser.id
               WHERE ser.name = ? AND s.name = ? AND e.episode_name = ?""",
            (series, season, f"{episode} [{language}]"),
        ).fetchone()
        return row[0] if row else None

    def cleanup_orphaned(self):
        for f in self.download_folder.glob("*.part"):
            f.unlink()
            logger.debug(f"  Cleaned up orphaned file: {f.name}")

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

class Network:
    def __init__(self):
        self.session: cloudscraper.CloudScraper = cloudscraper.create_scraper()
        self._primed_servers: set[str] = set()
        self._active_procs: list[subprocess.Popen] = []
        self._proc_lock = threading.Lock()

    def raw_get(self, url: str, headers: dict = None, allow_redirects: bool = False) -> requests.Response:
        r = self.session.get(url, headers=headers or {}, allow_redirects=allow_redirects)
        logger.debug(f"  → GET {r.status_code} {url[:100]}")
        if r.ok:
            return r
        raise requests.HTTPError(f"HTTP {r.status_code} for {url[:80]}")

    def get(self, url: str, headers: dict = None) -> str:
        return self.raw_get(url, headers).text

    def post(self, url: str, headers: dict = None, data: dict = None) -> str:
        r = self.session.post(url, headers=headers or {}, data=data or {})
        if r.ok:
            return r.text
        raise requests.HTTPError(f"HTTP {r.status_code} for {url[:80]}")

    def get_rendered_page(self, url: str, timeout_ms: int = 30_000, referer: str = "") -> str:
        """Render a page with a headless browser and return the HTML."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT,
                extra_http_headers={"Referer": referer} if referer else {},
            )
            page = context.new_page()
            for wait_until in ("load", "domcontentloaded"):
                try:
                    page.goto(url, timeout=timeout_ms, wait_until=wait_until)
                    break
                except Exception as e:
                    logger.debug(f"  Playwright ({wait_until}): {e}")
            html = page.content()
            context.close()
            browser.close()
            return html

    def prime_server(self, server_url: str):
        """Hit a server once so cloudscraper can obtain CF clearance cookies."""
        if server_url in self._primed_servers:
            return
        try:
            self.session.get(server_url, timeout=10, allow_redirects=True)
            self._primed_servers.add(server_url)
            logger.debug(f"  CF primed: {server_url}")
        except Exception as e:
            logger.debug(f"  CF priming failed (non-fatal): {e}")

    def download_file(self, label: str, url: str, filename: str, folder: str) -> str:
        dest = pathlib.Path(folder) / filename
        pathlib.Path(folder).mkdir(parents=True, exist_ok=True)

        resume_from = dest.stat().st_size if dest.exists() else 0
        phpsessid = self.session.cookies.get("PHPSESSID")
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity;q=1, *;q=0",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Origin": "https://embed.wcostream.com",
            "Referer": "https://embed.wcostream.com/",
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-site",
            "Range": f"bytes={resume_from}-" if resume_from else "bytes=0-",
        }
        if phpsessid:
            headers["Cookie"] = f"PHPSESSID={phpsessid}"
            logger.debug(f"  Sending PHPSESSID to CDN: {phpsessid[:8]}...")

        logger.debug(f"  Downloading: {url[:100]}")
        r = self.session.get(url, headers=headers, stream=True, allow_redirects=True)
        logger.debug(f"  Final URL:   {r.url[:100]}")
        logger.debug(f"  Status: {r.status_code}  Content-Type: {r.headers.get('Content-Type', 'N/A')}")

        ct = r.headers.get("Content-Type", "")
        if "text/html" in ct:
            body = r.content[:500].decode("utf-8", errors="replace")
            logger.debug(f"  Response body: {body}")
            raise RuntimeError(
                f"CDN returned HTML instead of video\n"
                f"  URL: {url}\n"
                f"  Status: {r.status_code}\n"
                f"  Body preview: {body[:200]}"
            )

        content_length = r.headers.get("Content-Length")
        total = int(content_length) if content_length else None
        if total is not None and resume_from == total:
            return filename

        with tqdm(total=total, unit="B", unit_scale=True, unit_divisor=1024,
                  desc=label, initial=resume_from) as bar:
            with open(dest, "ab") as fh:
                for chunk in r.iter_content(1024):
                    fh.write(chunk)
                    bar.update(len(chunk))
        return filename

    def download_hls(self, label: str, url: str, filename: str, folder: str) -> str:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg is required for HLS streams but was not found on PATH")

        dest = pathlib.Path(folder) / filename
        pathlib.Path(folder).mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0:
            return filename

        referer = (
            "https://vhs.watchanimesub.net/"
            if ("watchanimesub" in url or "cizgifilmlerizle" in url)
            else "https://embed.wcostream.com/"
        )
        try:
            resolved = self.session.head(url, allow_redirects=True,
                                         headers={"Referer": referer}).url
        except Exception:
            resolved = url
        logger.debug(f"  HLS: {url[:60]} → {resolved[:60]}")

        proc = subprocess.Popen(
            ["ffmpeg", "-y",
             "-user_agent", USER_AGENT,
             "-headers", f"Referer: {referer}\r\n",
             "-i", resolved,
             "-c", "copy", "-bsf:a", "aac_adtstoasc",
             "-progress", "pipe:1",
             str(dest)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        with self._proc_lock:
            self._active_procs.append(proc)

        duration: list[int] = []
        stderr_lines: list[str] = []

        def _drain():
            for line in proc.stderr:
                stderr_lines.append(line)
                m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)", line)
                if m:
                    duration.append(int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]))

        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        t.join(timeout=5)

        total = duration[0] if duration else 0
        bar = tqdm(total=total, desc=label, unit="s",
                   bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}s [{elapsed}<{remaining}]"
                   ) if total else None
        if not bar:
            print(label)

        for line in proc.stdout:
            if line.strip().startswith("out_time_us=") and bar:
                try:
                    bar.n = min(int(line.split("=")[1]) // 1_000_000, bar.total)
                    bar.refresh()
                except (ValueError, IndexError):
                    pass

        proc.wait()
        t.join()
        with self._proc_lock:
            if proc in self._active_procs:
                self._active_procs.remove(proc)

        if bar:
            bar.n = bar.total
            bar.refresh()
            bar.close()

        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed (exit code {proc.returncode})\n"
                + "".join(stderr_lines[-20:])
            )
        return filename

    def kill_active_processes(self):
        with self._proc_lock:
            for p in list(self._active_procs):
                try:
                    p.kill()
                except OSError:
                    pass
            self._active_procs.clear()


# ---------------------------------------------------------------------------
# Metadata normalization
# ---------------------------------------------------------------------------

def normalize_show_name(raw: str) -> str:
    """
    Map a raw parsed show name to a canonical name.
    Checks the SHOW_ALIASES table first, then returns the raw name.
    """
    key = raw.lower().strip()
    # Try full match
    if key in SHOW_ALIASES:
        return SHOW_ALIASES[key]
    # Try stripping trailing OVA/Movie/Special suffixes before matching
    stripped = re.sub(r"\s+(ova|movie|film|special|ova \d+|movie \d+).*$", "", key, flags=re.IGNORECASE).strip()
    if stripped in SHOW_ALIASES:
        return SHOW_ALIASES[stripped]
    return raw


def parse_episode_meta(url: str) -> tuple[str, str, str]:
    """
    Parse show name, season, and episode from a wco.tv URL slug.
    Returns (show, season, episode) as clean strings.

    Language suffixes ("English Subbed", "English Dubbed") are stripped
    from show names. OVAs stay inside their show as a special season.
    Movies get their own series entry.
    """
    slug  = urlparse(url).path.strip("/")
    text  = re.sub(r"(?<=\d)-(?=\d)", ".", slug).replace("-", " ").strip()

    # Strip trailing language suffix (case-insensitive)
    text  = re.sub(r"\s+english\s+(subbed|dubbed)\s*$", "", text, flags=re.IGNORECASE).strip()

    # Extract episode number (handles "Episode 1", "Episode 1A", "Episode 1.5" etc.)
    ep_m  = re.search(r"\bepisode\s*(\d+(?:\.\d+)?[A-Za-z]?)\b", text, re.IGNORECASE)
    ep_no = ep_m.group(1).upper() if ep_m else "0"

    # Extract season number
    sea_m = re.search(r"\bseason\s*(\d+)\b", text, re.IGNORECASE)
    sea_no = sea_m.group(1) if sea_m else None

    # Check for OVA
    is_ova = bool(re.search(r"\bova\b", text, re.IGNORECASE))

    # Check for movie
    slug_words = set(slug.split("-"))
    is_movie = bool(slug_words & MOVIE_SLUG_WORDS)

    # Extract show name: everything before season/episode/ova/movie marker
    name_end = re.search(
        r"\b(season|episode|ova|movie|film|part\s*\d+(?:\.\d+)?)\b", text, re.IGNORECASE
    )
    if name_end:
        show_raw = text[:name_end.start()].strip()
    else:
        show_raw = text.strip()

    show_raw  = re.sub(r"\s+", " ", show_raw).title()
    show      = normalize_show_name(show_raw)

    if is_movie:
        # Movies get their own series so they don't clutter episode lists
        season  = "Movies"
        episode = f"Episode {ep_no}"
    elif is_ova:
        # OVAs stay inside the show as a dedicated season
        season  = "OVA"
        episode = f"OVA {ep_no}"
    elif sea_no:
        season  = f"Season {sea_no}"
        episode = f"Episode {ep_no}"
    else:
        season  = "Season 1"
        episode = f"Episode {ep_no}"

    return show, season, episode


def detect_language(url: str, episode_label: str) -> str:
    """
    dubbed  → English dub
    subbed  → Japanese with English subtitles
    neither → English (default)
    """
    combined = (url + " " + episode_label).lower()
    if "dubbed" in combined:
        return "English"
    if "subbed" in combined:
        return "Japanese"
    return "English"


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class Scraper:
    def __init__(self, network: Network):
        self.net = network

    # --- episode list ---

    def get_episodes(self, url: str) -> list[tuple[str, str]]:
        """Return [(episode_url, label), ...] oldest first."""
        html = self.net.get_rendered_page(url)
        soup = bs4.BeautifulSoup(html, "html.parser")
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        links = soup.find_all("a", class_="dark-episode-item") or soup.find_all("a", class_="sonra")
        episodes = []
        for a in links:
            href = a["href"]
            if href.startswith("/"):
                href = base + href
            label = re.sub(r"(HD|SD)$", "", a.get_text(strip=True)).strip()
            episodes.append((href, label))
        episodes.reverse()
        return episodes

    # --- embed URL extraction ---

    def get_embed_url(self, episode_url: str) -> str:
        """Render the episode page and return the embed iframe src."""
        html = self.net.get_rendered_page(episode_url)
        soup = bs4.BeautifulSoup(html, "html.parser")
        all_iframes = soup.find_all("iframe")

        for i, fr in enumerate(all_iframes):
            logger.debug(f"  iframe #{i}  id={fr.get('id','(none)')}  src={fr.get('src','')[:100]}")

        if "Become a Premium User Now!" in html or "This Video Is for Premium Users" in html:
            raise RuntimeError("Premium episode, skipping")

        iframe = (
            soup.find("iframe", {"id": "frameSaturn1"})
            or soup.find("iframe", {"id": "frameNewcizgifilmuploads0"})
            or next(
                (fr for fr in all_iframes
                 if fr.get("src") and not any(ad in fr["src"] for ad in AD_DOMAINS)),
                None,
            )
        )
        if iframe is None:
            raise RuntimeError("No embed iframe found on page")

        src = iframe["src"]
        logger.debug(f"  Selected embed: {src[:100]}")
        return src

    # --- video source resolution ---

    def get_sources(self, embed_url: str) -> list[dict]:
        """Return a list of {"label": "720p", "url": "..."} dicts."""
        if "watchanimesub.net" in embed_url or "saturn" in embed_url.lower():
            return self._extract_saturn(embed_url)
        return self._extract_legacy(embed_url)

    def _extract_legacy(self, embed_url: str) -> list[dict]:
        parsed      = urlparse(embed_url)
        query       = parse_qs(parsed.query, keep_blank_values=True)
        file_param  = query.get("file",  [""])[0]
        embed_param = query.get("embed", [""])[0]
        hd_param    = query.get("hd",    [""])[0]   # only send if present in URL
        pid_param   = query.get("pid",   [""])[0]
        h_param     = query.get("h",     [""])[0]
        t_param     = query.get("t",     [""])[0]

        if not file_param:
            raise ValueError(f"No 'file' param in embed URL: {embed_url[:80]}")

        # .flv → .mp4
        base = file_param.rsplit(".", 1)[0] + ".mp4"

        # Build v= param — confirmed by HAR for each embed type:
        #   ndisk  → v=<raw file path, NO prefix>
        #   anime  → v=<raw file path, NO prefix>
        #   cizgi  → v=cizgi/<full file path>
        #   neptun → v=neptun/<full file path>
        #   others → v=<embed>/<full file path>
        if embed_param in ("ndisk", "anime"):
            video_path = base
        else:
            video_path = f"{embed_param}/{base}"
        v_encoded = quote(video_path, safe="/")

        qs = f"v={v_encoded}&embed={embed_param}"
        if hd_param:    qs += f"&hd={hd_param}"
        if pid_param:   qs += f"&pid={pid_param}"
        if h_param:     qs += f"&h={h_param}"
        if t_param:     qs += f"&t={t_param}"

        getvidlink = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/inc/embed/getvidlink.php")

        # Referer must be video-js.php (the actual player page), not index.php
        video_js_url = (
            urljoin(f"{parsed.scheme}://{parsed.netloc}", "/inc/embed/video-js.php")
            + "?" + parsed.query.replace("%20", "+")
        )

        # Load index.php as the browser would (cross-site iframe) to set PHPSESSID
        try:
            self.net.session.get(embed_url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Referer": "https://www.wco.tv/",
                "Sec-Fetch-Dest": "iframe",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site",
            }, allow_redirects=True, timeout=15)
            logger.debug(f"  Embed page loaded — {len(dict(self.net.session.cookies))} cookie(s) set")
        except Exception as e:
            logger.debug(f"  Embed page preload failed (non-fatal): {e}")

        phpsessid = self.net.session.cookies.get("PHPSESSID")
        xhr_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Encoding": "gzip, deflate, br",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Referer": video_js_url,
        }
        if phpsessid:
            xhr_headers["Cookie"] = f"PHPSESSID={phpsessid}"
            logger.debug(f"  Sending PHPSESSID to getvidlink: {phpsessid[:8]}...")

        logger.debug(f"  getvidlink: {getvidlink}?{qs[:120]}")
        resp = self.net.raw_get(f"{getvidlink}?{qs}", headers=xhr_headers)
        data = resp.json()
        enc       = data.get("enc", "")
        server    = data.get("server", "")
        hd_token  = data.get("hd",    "")
        fhd_token = data.get("fhd",   "")

        logger.debug(
            f"  getvidlink OK  server={server!r}  "
            f"enc={enc[:8]}...  hd={bool(hd_token)}  fhd={bool(fhd_token)}"
        )

        if not server or not enc:
            raise RuntimeError(
                f"getvidlink returned no server/enc token\n"
                f"  embed: {embed_url[:80]}\n"
                f"  response: {data}"
            )

        self.net.prime_server(server)

        # Token resolution uses bare embed origin as Referer (confirmed by HAR)
        phpsessid = self.net.session.cookies.get("PHPSESSID")
        token_headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://embed.wcostream.com/",
            "Origin": "https://embed.wcostream.com",
        }
        if phpsessid:
            token_headers["Cookie"] = f"PHPSESSID={phpsessid}"

        sources = []
        for token, label in [(enc, "480p"), (hd_token, "720p"), (fhd_token, "1080p")]:
            url = self._resolve_token(token, server, token_headers)
            if url:
                sources.append({"label": label, "url": url})

        if not sources:
            raise RuntimeError(
                f"No video sources could be resolved\n"
                f"  server: {server}\n"
                f"  enc token: {enc[:20]}..."
            )

        labels = [s["label"] for s in sources]
        logger.debug(f"  Resolved sources: {labels}")
        return sources

    def _resolve_token(self, token: str, server: str, headers: dict) -> str | None:
        if not token:
            return None

        json_url = f"{server}/getvid?evid={token}&json"
        try:
            r = self.net.raw_get(json_url, headers=headers, allow_redirects=True)
            candidate: str | None = None
            try:
                body = r.json()
                if isinstance(body, str) and body.startswith("http"):
                    candidate = body
                elif isinstance(body, dict) and "url" in body:
                    candidate = body["url"]
            except Exception:
                text = r.text.strip().strip('"')
                if text.startswith("http"):
                    candidate = text

            logger.debug(f"  Token → {str(candidate)[:80]}")
            if not candidate:
                return None

            # Strip stray &json / ?json suffix
            for suffix in ("&json", "?json"):
                if candidate.endswith(suffix):
                    candidate = candidate[:-len(suffix)]

            # Fix empty subdomain (neptun bug: https://.wcostream.com/...)
            cp = urlparse(candidate)
            sp = urlparse(server)
            if not cp.hostname or cp.hostname.startswith("."):
                candidate = urlunparse(cp._replace(netloc=sp.netloc))
                logger.debug(f"  Token (subdomain fixed) → {candidate[:80]}")
                cp = urlparse(candidate)

            # Prime the CDN subdomain
            self.net.prime_server(f"{cp.scheme}://{cp.netloc}")

            return candidate

        except Exception as e:
            logger.debug(f"  Token resolution error: {e}")
            return None

    def _extract_saturn(self, embed_url: str) -> list[dict]:
        """Playwright-based extraction for Saturn/HLS embeds."""
        captured_m3u8: list[str] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            page.on("request", lambda r: captured_m3u8.append(r.url) if ".m3u8" in r.url else None)

            try:
                page.goto(embed_url, timeout=30_000, wait_until="domcontentloaded")
            except Exception as e:
                logger.debug(f"  Saturn page load: {e}")

            try:
                page.click("video, .vjs-big-play-button, .play-button, body", timeout=3_000)
            except Exception:
                pass
            page.wait_for_timeout(5_000)

            player_sources: list[dict] = page.evaluate("""() => {
                const out = [];
                if (typeof videojs !== 'undefined') {
                    for (const [, p] of Object.entries(videojs.getPlayers())) {
                        if (!p) continue;
                        const ql = p.qualityLevels?.();
                        if (ql) for (let i = 0; i < ql.length; i++) {
                            const l = ql[i];
                            if (l) out.push({ label: (l.height || 'auto') + 'p', url: l.uri || '' });
                        }
                        const src = p.currentSrc?.();
                        if (src) out.push({ label: 'current', url: src });
                    }
                }
                document.querySelectorAll('video').forEach(v => {
                    if (v.src) out.push({ label: 'video', url: v.src });
                    v.querySelectorAll('source').forEach(s => {
                        if (s.src) out.push({ label: s.getAttribute('label') || 'source', url: s.src });
                    });
                });
                const m = document.documentElement.innerHTML
                    .match(/getRedirectedUrl\\(\\s*["']([^"']+\\.m3u8)["']/);
                if (m) out.push({ label: 'hls_script', url: m[1] });
                return out;
            }""") or []

            context.close()
            browser.close()

        hls_url = next(
            (e["url"] for e in player_sources
             if ".m3u8" in e.get("url", "") and "blob:" not in e["url"]),
            next((u for u in captured_m3u8 if u.startswith("http")), ""),
        )
        if not hls_url:
            raise RuntimeError("Saturn embed: no HLS URL found")

        base = re.sub(r"/0/\d+/index\.m3u8$", "", hls_url).rstrip("/index.m3u8")
        return [
            {"label": "480p",  "url": f"{base}/0/854/index.m3u8"},
            {"label": "720p",  "url": f"{base}/0/1280/index.m3u8"},
            {"label": "1080p", "url": f"{base}/0/1920/index.m3u8"},
        ]

    def select_resolution(self, sources: list[dict], preference: str) -> dict:
        mapping = {"sd": "480p", "hd": "720p", "fhd": "1080p"}
        target  = mapping.get(preference.lower())
        if target:
            match = next((s for s in sources if s["label"] == target), None)
            if match:
                return match
        def _res(s: dict) -> int:
            m = re.search(r"(\d+)", s.get("label", "0"))
            return int(m.group(1)) if m else 0
        return max(sources, key=_res)

    def search(self, query: str) -> list[str]:
        response = self.net.post(
            "https://www.wco.tv/search",
            headers={"User-Agent": USER_AGENT},
            data={"catara": query, "konuara": "series"},
        )
        soup = bs4.BeautifulSoup(response, "html.parser")
        return [
            a["href"]
            for div in soup.find_all("div", attrs={"class": "left", "id": "blog"})
            for a in div.find_all("a")
        ]


# ---------------------------------------------------------------------------
# Error log
# ---------------------------------------------------------------------------

class ErrorLog:
    """Collects error details during a run and writes them to errors.txt."""

    def __init__(self):
        self._entries: list[str] = []

    def add(self, episode_label: str, url: str, detail: str, debug_log: str = ""):
        entry = (
            f"Episode : {episode_label}\n"
            f"URL     : {url}\n"
            f"Error   : {detail}\n"
        )
        if debug_log:
            entry += f"\nDebug log:\n{debug_log}\n"
        self._entries.append(entry)

    def has_errors(self) -> bool:
        return bool(self._entries)

    def write(self, path: str = "errors.txt"):
        p = pathlib.Path(path)
        with p.open("w", encoding="utf-8") as f:
            f.write(f"wco-dl error log\n{'─' * 60}\n\n")
            for i, entry in enumerate(self._entries, 1):
                f.write(f"[{i}]\n{entry}\n")

    def print_summary(self):
        """Print a red warning to the terminal if any errors occurred."""
        if self._entries:
            RED   = "\033[91m"
            RESET = "\033[0m"
            print(
                f"\n{RED}Errors occurred during download of some episodes. "
                f"Check the specifics in errors.txt{RESET}"
            )


# ---------------------------------------------------------------------------
# Download orchestration
# ---------------------------------------------------------------------------

def download_episode(
    url: str,
    network: Network,
    scraper: Scraper,
    config: Config,
    db: LibraryDB,
    progress: Progress,
) -> tuple[bool, str, str]:
    """
    Returns (success, short_message, error_detail).
    short_message is always shown in the terminal.
    error_detail is non-empty only on failure and goes to errors.txt.
    """
    # Step 1: get embed URL (catches premium episodes)
    try:
        embed_url = scraper.get_embed_url(url)
    except RuntimeError as e:
        debug_log = logger.flush()
        if "Premium episode" in str(e):
            return False, "⏭  Skipped  — premium only", ""
        detail = f"Could not load page: {e}\n{traceback.format_exc()}"
        return False, "✗  Failed   — could not load page", detail

    # Step 2: parse episode metadata
    show, season, episode = parse_episode_meta(url)
    language = detect_language(url, episode)
    logger.debug(f"── {show} / {season} / {episode} ({language}) ──")

    # Step 3: already downloaded?
    if existing := db.get_episode_filename(show, season, episode, language):
        progress.remove_pending(url)
        logger.clear()
        return True, f"⏭  {C.DIM}Skipped{C.RESET}  — already downloaded ({existing})", ""

    # Step 4: extract video sources
    try:
        sources = scraper.get_sources(embed_url)
    except Exception as e:
        debug_log = logger.flush()
        detail = f"Source extraction failed: {e}\n{traceback.format_exc()}"
        return False, "✗  Failed   — source extraction error", detail

    source   = scraper.select_resolution(sources, config.settings.resolution)
    media    = source["url"]
    is_hls   = ".m3u8" in media
    hex_name = db.get_next_filename()
    folder   = config.settings.download_folder
    temp     = pathlib.Path(folder) / f"{hex_name}.part"
    final    = pathlib.Path(folder) / hex_name
    label    = f"{show} — {season} {episode} [{language}]"

    progress.add_pending(url)

    try:
        if is_hls:
            network.download_hls(label, media, hex_name, folder)
        else:
            network.download_file(label, media, f"{hex_name}.part", folder)
            temp.rename(final)

        db.add_episode(show, season, episode, hex_name, language)
        progress.remove_pending(url)
        logger.clear()
        return True, f"✓  {C.GREEN}Done{C.RESET}     — {season} {episode} [{language}] → {C.DIM}{hex_name}{C.RESET}", ""

    except Exception as e:
        debug_log = logger.flush()
        for p in (temp, final):
            if p.exists():
                p.unlink()
        detail = f"Download error: {e}\nMedia URL: {media}\n{traceback.format_exc()}"
        return False, "✗  Failed   — download error", detail


def download_series(
    url: str,
    network: Network,
    scraper: Scraper,
    config: Config,
    db: LibraryDB,
    progress: Progress,
    error_log: ErrorLog,
) -> None:
    print(f"  Fetching episode list...")
    episodes = scraper.get_episodes(url)
    if not episodes:
        print(f"  No episodes found.")
        return
    print(f"  Found {len(episodes)} episode(s). Starting download...\n")
    ok = skip = fail = 0
    for i, (ep_url, label) in enumerate(episodes, 1):
        print(f"  [{i:>3}/{len(episodes)}] {label}")
        success, msg, detail = download_episode(ep_url, network, scraper, config, db, progress)
        print(f"         {msg}")
        if success:
            if "Skipped" in msg: skip += 1
            else: ok += 1
        else:
            if "premium" in msg or "Skipped" in msg:
                skip += 1
            else:
                fail += 1
                error_log.add(label, ep_url, detail, logger.flush())
    print(f"\n  Done — {ok} downloaded, {skip} skipped, {fail} failed.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

app = typer.Typer(
    help="wco-dl — download anime & cartoons from wco.tv",
    add_completion=False,
)


def _init() -> tuple[Config, Network, Scraper, LibraryDB, Progress]:
    config  = Config()
    network = Network()
    scraper = Scraper(network)
    db      = LibraryDB(config)
    prog    = Progress(config)
    return config, network, scraper, db, prog


@app.command()
def main(
    target:     str  = typer.Argument(None),
    search:     bool = typer.Option(False, "--search",  "-s",  help="Search for a show"),
    episode:    bool = typer.Option(False, "--episode", "-de", help="Download a single episode URL"),
    series:     bool = typer.Option(False, "--series",  "-ds", help="Download a full series URL"),
    all_series: bool = typer.Option(False, "--all",     "-da", help="Download all series from list file"),
    list_file:  str  = typer.Option(DEFAULT_SERIES_LIST, "--list-file", help="Series list file (one URL per line)"),
):
    """
    wco-dl — download anime & cartoons from wco.tv

    \b
    Examples:
      python main.py -s "slime"
      python main.py -de "https://www.wco.tv/some-episode-english-subbed"
      python main.py -ds "https://www.wco.tv/anime/some-anime/?season=all"
      python main.py -da
    """
    modes = [search, episode, series, all_series]
    if sum(modes) != 1:
        typer.echo("Specify exactly one mode: -s / -de / -ds / -da")
        raise typer.Exit(1)

    config, network, scraper, db, progress = _init()
    db.cleanup_orphaned()
    error_log = ErrorLog()

    if stale := progress.get_pending():
        print(f"Clearing {len(stale)} stale pending entry(s) from previous session.")
        progress.clear_pending()

    try:
        # ── Search ──────────────────────────────────────────────────────────
        if search:
            if not target:
                typer.echo("--search requires a query"); raise typer.Exit(1)
            print(f"Searching for '{target}'...")
            results = scraper.search(target)
            if not results:
                print("No results found.")
            else:
                print(f"Found {len(results)} result(s):\n")
                for path in results:
                    name = path.replace("/anime/", "").replace("-", " ").title()
                    print(f"  {name}")
                    print(f"  https://www.wco.tv{path}\n")

        # ── Single episode ───────────────────────────────────────────────────
        elif episode:
            if not target:
                typer.echo("--episode requires a URL"); raise typer.Exit(1)
            show, season, ep = parse_episode_meta(target)
            print(f"Downloading: {show} — {season} {ep}")
            success, msg, detail = download_episode(target, network, scraper, config, db, progress)
            print(f"  {msg}")
            if not success and detail:
                error_log.add(f"{show} — {season} {ep}", target, detail, logger.flush())

        # ── Full series ──────────────────────────────────────────────────────
        elif series:
            if not target:
                typer.echo("--series requires a URL"); raise typer.Exit(1)
            print(f"Series: {target}")
            download_series(target, network, scraper, config, db, progress, error_log)

        # ── All series from file ─────────────────────────────────────────────
        elif all_series:
            path = pathlib.Path(list_file)
            if not path.is_file():
                typer.echo(f"List file not found: {path}"); raise typer.Exit(1)
            urls = [line.strip() for line in path.read_text("utf-8").splitlines()
                    if line.strip() and not line.startswith("#")]
            if not urls:
                print("Series list is empty."); return
            print(f"Found {len(urls)} series in list.\n")
            if not typer.confirm("Download all? (This may take a very long time)"):
                print("Aborted."); return
            for i, u in enumerate(urls, 1):
                print(f"\n[{i}/{len(urls)}] {u}")
                try:
                    download_series(u, network, scraper, config, db, progress, error_log)
                except Exception as e:
                    print(f"  ✗ Series failed: {e}")
                    error_log.add(u, u, f"Series-level failure: {e}\n{traceback.format_exc()}")

    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")

    finally:
        db.close()
        if error_log.has_errors():
            error_log.write("errors.txt")
            error_log.print_summary()


if __name__ == "__main__":
    app()
