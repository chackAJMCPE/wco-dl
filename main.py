"""
wco-dl — download anime & cartoons from wco.tv

The legacy WCO player flow mirrors the current embed JavaScript:

    getvidlink.php
        ├─ response.server + /getvid?evid=<SD_TOKEN>&json -> redirected MP4 URL
        ├─ response.server + /getvid?evid=<HD_TOKEN>      -> media redirect
        └─ response.server + /getvid?evid=<FHD_TOKEN>     -> media redirect

The player also exposes response.cdn. It is retained as a fallback resolver,
not substituted for response.server, because the current player explicitly
builds its media URLs from response.server.
"""

import json
import pathlib
import re
import shutil
import sqlite3
import subprocess
import concurrent.futures
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

warnings.filterwarnings("ignore", category=ResourceWarning)


# ---------------------------------------------------------------------------
# Terminal colours
# ---------------------------------------------------------------------------

class C:
    """ANSI colour codes — gracefully disabled on terminals that don't support them."""
    import sys as _sys
    _on = _sys.stdout.isatty() if hasattr(_sys.stdout, "isatty") else False

    GREEN = "\033[92m" if _on else ""
    YELLOW = "\033[93m" if _on else ""
    RED = "\033[91m" if _on else ""
    CYAN = "\033[96m" if _on else ""
    DIM = "\033[2m" if _on else ""
    BOLD = "\033[1m" if _on else ""
    RESET = "\033[0m" if _on else ""


class EpisodeLogger:
    """Buffers debug lines for the current episode."""

    def __init__(self):
        self._buf: list[str] = []

    def debug(self, msg: str):
        self._buf.append(msg)

    def flush(self) -> str:
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
    "Chrome/152.0.0.0 Safari/537.36"
)

AD_DOMAINS = {
    "ad.a-ads.com",
    "doubleclick.net",
    "googlesyndication.com",
    "adservice.google",
}

DEFAULT_SERIES_LIST = ".wco-dl/series_list.txt"
MOVIE_SLUG_WORDS = {"movie", "film", "the-movie"}


# ---------------------------------------------------------------------------
# Configuration & Settings
# ---------------------------------------------------------------------------

class Settings(pydantic.BaseModel):
    resolution: str = "best"
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
# Progress
# ---------------------------------------------------------------------------

class Progress:
    def __init__(self, config: Config):
        self._path = config.config_dir / "progress.json"
        self._data = (
            json.loads(self._path.read_text("utf-8"))
            if self._path.exists()
            else {"pending": []}
        )
        self._data.setdefault("pending", [])

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
# Library database
# ---------------------------------------------------------------------------

class LibraryDB:
    def __init__(self, config: Config):
        self.download_folder = pathlib.Path(config.settings.download_folder)
        self.download_folder.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.download_folder / "library.db"),
            check_same_thread=False,
        )
        self._lock = threading.Lock()
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        self.conn.executescript(
            """
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
            """
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('file_counter', '0')"
        )
        self.conn.commit()

    def get_next_filename(self) -> str:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE meta SET value = CAST(value AS INTEGER) + 1 "
                "WHERE key = 'file_counter' RETURNING CAST(value AS INTEGER)"
            )
            new_val = cur.fetchone()[0]
            self.conn.commit()
        return f"{new_val - 1:08x}.mp4"

    def add_episode(
        self,
        series: str,
        season: str,
        episode: str,
        filename: str,
        language: str,
    ):
        with self._lock:
            self.conn.execute("INSERT OR IGNORE INTO series (name) VALUES (?)", (series,))
            series_id = self.conn.execute(
                "SELECT id FROM series WHERE name = ?", (series,)
            ).fetchone()[0]
            self.conn.execute(
                "INSERT OR IGNORE INTO seasons (series_id, name) VALUES (?, ?)",
                (series_id, season),
            )
            season_id = self.conn.execute(
                "SELECT id FROM seasons WHERE series_id = ? AND name = ?",
                (series_id, season),
            ).fetchone()[0]
            self.conn.execute(
                "INSERT OR REPLACE INTO episodes "
                "(season_id, episode_name, filename, language) VALUES (?, ?, ?, ?)",
                (season_id, f"{episode} [{language}]", filename, language),
            )
            self.conn.commit()

    def get_episode_filename(
        self,
        series: str,
        season: str,
        episode: str,
        language: str,
    ) -> str | None:
        with self._lock:
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
        self.session: cloudscraper.CloudScraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._primed_servers: set[str] = set()
        self._active_procs: list[subprocess.Popen] = []
        self._proc_lock = threading.Lock()

    def raw_get(
        self,
        url: str,
        headers: dict | None = None,
        allow_redirects: bool = False,
        timeout: int | float = 30,
    ) -> requests.Response:
        r = self.session.get(
            url,
            headers=headers or {},
            allow_redirects=allow_redirects,
            timeout=timeout,
        )
        logger.debug(f"  → GET {r.status_code} {url[:120]}")
        if r.ok:
            return r
        raise requests.HTTPError(
            f"HTTP {r.status_code} for {url[:100]} (final URL: {r.url[:120]})"
        )

    def get(self, url: str, headers: dict | None = None) -> str:
        return self.raw_get(url, headers, allow_redirects=False).text

    def post(self, url: str, headers: dict | None = None, data: dict | None = None) -> str:
        r = self.session.post(
            url,
            headers=headers or {},
            data=data or {},
            timeout=30,
        )
        if r.ok:
            return r.text
        raise requests.HTTPError(f"HTTP {r.status_code} for {url[:100]}")

    def get_rendered_page(
        self,
        url: str,
        timeout_ms: int = 30_000,
        referer: str = "",
    ) -> str:
        """Render a page with a headless browser and return the HTML."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT,
                extra_http_headers={"Referer": referer} if referer else {},
            )
            page = context.new_page()
            loaded = False
            for wait_until in ("load", "domcontentloaded"):
                try:
                    page.goto(url, timeout=timeout_ms, wait_until=wait_until)
                    loaded = True
                    break
                except Exception as e:
                    logger.debug(f"  Playwright ({wait_until}): {e}")
            if not loaded:
                try:
                    page.goto(url, timeout=timeout_ms)
                except Exception:
                    pass
            html = page.content()
            context.close()
            browser.close()
            return html

    @staticmethod
    def _host(url: str) -> str:
        return urlparse(url).hostname or ""

    @staticmethod
    def _is_delivery_node(host: str) -> bool:
        return bool(re.match(r"^(?:d|nd|m)\d+\.", host, re.IGNORECASE))

    def prime_server(self, server_url: str):
        """Prime a resolver/CDN without caching individual delivery nodes."""
        parsed = urlparse(server_url)
        host = parsed.hostname or ""
        if self._is_delivery_node(host):
            # Delivery nodes are token-specific. Do not treat one node as a
            # reusable resolver endpoint.
            return
        if server_url in self._primed_servers:
            return
        try:
            self.session.get(
                server_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": "https://embed.wcostream.com/",
                },
                timeout=10,
                allow_redirects=True,
            )
            self._primed_servers.add(server_url)
            logger.debug(f"  CDN primed: {server_url}")
        except Exception as e:
            logger.debug(f"  CDN priming failed (non-fatal): {e}")

    @staticmethod
    def _looks_like_html(response: requests.Response) -> bool:
        ct = response.headers.get("Content-Type", "").lower()
        if "text/html" in ct or "application/xhtml" in ct:
            return True
        preview = response.content[:64].lstrip().lower()
        return preview.startswith(b"<!doctype html") or preview.startswith(b"<html")

    def download_file(self, label: str, url: str, filename: str, folder: str) -> str:
        """Download an MP4 with safe resume handling and delivery-node redirects."""
        dest = pathlib.Path(folder) / filename
        pathlib.Path(folder).mkdir(parents=True, exist_ok=True)

        resume_from = dest.stat().st_size if dest.exists() else 0
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Referer": "https://embed.wcostream.com/",
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-site",
        }
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"

        logger.debug(f"  Downloading: {url[:140]}")
        r = self.session.get(
            url,
            headers=headers,
            stream=True,
            allow_redirects=True,
            timeout=45,
        )
        logger.debug(f"  Final URL:   {r.url[:140]}")
        logger.debug(
            f"  Status: {r.status_code}  Content-Type: "
            f"{r.headers.get('Content-Type', 'N/A')}"
        )

        # A ranged request returning 200 means the server ignored Range. Do not
        # append a complete file to a partial file.
        if resume_from and r.status_code == 200:
            logger.debug("  Range ignored by server; restarting download from byte 0")
            r.close()
            try:
                dest.unlink()
            except FileNotFoundError:
                pass
            resume_from = 0
            headers.pop("Range", None)
            r = self.session.get(
                url,
                headers=headers,
                stream=True,
                allow_redirects=True,
                timeout=45,
            )
            logger.debug(f"  Restart status: {r.status_code}  Final: {r.url[:140]}")

        if self._looks_like_html(r):
            body = r.content[:800].decode("utf-8", errors="replace")
            logger.debug(f"  Response body: {body}")
            raise RuntimeError(
                "CDN returned HTML instead of video\n"
                f"  Requested URL: {url}\n"
                f"  Final URL: {r.url}\n"
                f"  Status: {r.status_code}\n"
                f"  Body preview: {body[:300]}"
            )

        if r.status_code not in (200, 206):
            raise RuntimeError(
                f"Unexpected media HTTP status {r.status_code}\n"
                f"  Requested URL: {url}\n"
                f"  Final URL: {r.url}"
            )

        content_length = r.headers.get("Content-Length")
        total = None
        if content_length:
            try:
                length = int(content_length)
                total = length + resume_from if r.status_code == 206 else length
            except ValueError:
                pass

        if total is not None and resume_from == total:
            r.close()
            return filename

        mode = "ab" if resume_from and r.status_code == 206 else "wb"
        initial = resume_from if mode == "ab" else 0

        with tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=label,
            initial=initial,
        ) as bar:
            with open(dest, mode) as fh:
                for chunk in r.iter_content(1024 * 64):
                    if chunk:
                        fh.write(chunk)
                        bar.update(len(chunk))
        r.close()
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
            resolved = self.session.head(
                url,
                allow_redirects=True,
                headers={"Referer": referer, "User-Agent": USER_AGENT},
                timeout=15,
            ).url
        except Exception:
            resolved = url
        logger.debug(f"  HLS: {url[:80]} → {resolved[:100]}")

        proc = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-user_agent", USER_AGENT,
                "-headers", f"Referer: {referer}\r\n",
                "-i", resolved,
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                "-progress", "pipe:1",
                str(dest),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self._proc_lock:
            self._active_procs.append(proc)

        duration: list[int] = []
        stderr_lines: list[str] = []

        def _drain():
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_lines.append(line)
                m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)", line)
                if m:
                    duration.append(
                        int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
                    )

        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        t.join(timeout=5)

        total = duration[0] if duration else 0
        bar = (
            tqdm(
                total=total,
                desc=label,
                unit="s",
                bar_format=(
                    "{desc}: {percentage:3.0f}%|{bar}| "
                    "{n_fmt}/{total_fmt}s [{elapsed}<{remaining}]"
                ),
            )
            if total
            else None
        )
        if not bar:
            print(label)

        assert proc.stdout is not None
        for line in proc.stdout:
            if line.strip().startswith("out_time_us=") and bar:
                try:
                    bar.n = min(int(line.split("=", 1)[1]) // 1_000_000, bar.total)
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
                + "".join(stderr_lines[-30:])
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

def title_from_slug(slug: str) -> str:
    text = re.sub(r"(?<=\d)-(?=\d)", ".", slug).replace("-", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text.title()


def parse_series_name(url: str) -> str:
    path = urlparse(url).path.strip("/")
    parts = [part for part in path.split("/") if part]

    if "anime" in parts:
        idx = parts.index("anime")
        if idx + 1 >= len(parts):
            raise ValueError(f"Could not determine series name from URL: {url}")
        slug = parts[idx + 1]
    else:
        slug = parts[-1] if parts else ""

    slug = re.sub(
        r"(?i)(?:-)?(?:season|episode|ova|movie|film).*$",
        "",
        slug,
    ).strip("-")
    if not slug:
        raise ValueError(f"Could not determine series name from URL: {url}")
    return title_from_slug(slug)


def parse_episode_meta(url: str) -> tuple[str, str, str]:
    slug = urlparse(url).path.strip("/")
    text = re.sub(r"(?<=\d)-(?=\d)", ".", slug).replace("-", " ").strip()
    text = re.sub(
        r"\s+english\s+(subbed|dubbed)\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    ep_m = re.search(
        r"\bepisode\s*(\d+(?:\.\d+)?[A-Za-z]?)\b",
        text,
        re.IGNORECASE,
    )
    ep_no = ep_m.group(1).upper() if ep_m else "0"

    sea_m = re.search(r"\bseason\s*(\d+)\b", text, re.IGNORECASE)
    sea_no = sea_m.group(1) if sea_m else None
    is_ova = bool(re.search(r"\bova\b", text, re.IGNORECASE))

    slug_words = set(slug.split("-"))
    is_movie = bool(slug_words & MOVIE_SLUG_WORDS)

    name_end = re.search(
        r"\b(season|episode|ova|movie|film|part\s*\d+(?:\.\d+)?)\b",
        text,
        re.IGNORECASE,
    )
    show_raw = text[: name_end.start()].strip() if name_end else text.strip()
    show = title_from_slug(show_raw)

    if is_movie:
        season = "Movies"
        episode = f"Episode {ep_no}"
    elif is_ova:
        season = "OVA"
        episode = f"OVA {ep_no}"
    elif sea_no:
        season = f"Season {sea_no}"
        episode = f"Episode {ep_no}"
    else:
        season = "Season 1"
        episode = f"Episode {ep_no}"

    return show, season, episode


def detect_language(url: str, episode_label: str) -> str:
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

    def get_episodes(self, url: str) -> list[tuple[str, str]]:
        """Return [(episode_url, label), ...] oldest first."""
        html = self.net.get_rendered_page(url)
        soup = bs4.BeautifulSoup(html, "html.parser")
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        links = soup.find_all("a", class_="dark-episode-item") or soup.find_all(
            "a", class_="sonra"
        )
        episodes = []
        for a in links:
            href = a.get("href")
            if not href:
                continue
            if href.startswith("/"):
                href = base + href
            label = re.sub(r"(HD|SD)$", "", a.get_text(strip=True)).strip()
            episodes.append((href, label))
        episodes.reverse()
        return episodes

    def get_embed_url(self, episode_url: str) -> str:
        # Try fast HTTP fetch first — works for most episodes since wco.tv
        # renders the iframe src server-side. Only fall back to Playwright
        # if the iframe isn't found (JS-rendered pages or CF challenge).
        html = self._fetch_episode_page(episode_url)
        src = self._extract_iframe_src(html, episode_url)
        if src:
            return src

        # Fallback: full Playwright render
        logger.debug("  HTTP fetch missed iframe, falling back to Playwright")
        html = self.net.get_rendered_page(episode_url)
        src = self._extract_iframe_src(html, episode_url)
        if src:
            return src
        raise RuntimeError("No embed iframe found on page")

    def _fetch_episode_page(self, url: str) -> str:
        try:
            r = self.net.session.get(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.wco.tv/",
            }, allow_redirects=True, timeout=15)
            return r.text
        except Exception as e:
            logger.debug(f"  HTTP episode fetch failed: {e}")
            return ""

    def _extract_iframe_src(self, html: str, episode_url: str) -> str | None:
        if not html:
            return None
        soup = bs4.BeautifulSoup(html, "html.parser")
        all_iframes = soup.find_all("iframe")

        for i, fr in enumerate(all_iframes):
            logger.debug(f"  iframe #{i}  id={fr.get('id', '(none)')}  src={fr.get('src', '')[:120]}")

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
            return None

        src = iframe["src"]
        logger.debug(f"  Selected embed: {src[:140]}")
        return src

    def get_sources(self, embed_url: str) -> list[dict]:
        if "watchanimesub.net" in embed_url or "saturn" in embed_url.lower():
            return self._extract_saturn(embed_url)
        return self._extract_legacy(embed_url)

    # ---------------- legacy player ----------------

    @staticmethod
    def _canonical_cdn(value: str | None) -> str | None:
        if not value:
            return None
        value = str(value).strip().strip('"\'')
        if not value:
            return None
        if not value.startswith(("http://", "https://")):
            value = "https://" + value.lstrip("/")
        parsed = urlparse(value)
        if not parsed.hostname:
            return None
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _is_delivery_node_url(value: str | None) -> bool:
        if not value:
            return False
        host = urlparse(value).hostname or ""
        return Network._is_delivery_node(host)

    def _extract_legacy(self, embed_url: str) -> list[dict]:
        """Extract legacy MP4 sources using the current WCO player protocol."""
        parsed = urlparse(embed_url)
        query = parse_qs(parsed.query, keep_blank_values=True)

        file_param = query.get("file", [""])[0]
        embed_param = query.get("embed", [""])[0]
        pid_param = query.get("pid", [""])[0]
        h_param = query.get("h", [""])[0]
        t_param = query.get("t", [""])[0]

        if not file_param:
            raise ValueError(f"No 'file' param in embed URL: {embed_url[:100]}")

        base = file_param.rsplit(".", 1)[0] + ".mp4"

        if embed_param in ("ndisk", "anime"):
            video_path = base
        else:
            video_path = f"{embed_param}/{base}"

        # Matches the current player:
        # /inc/embed/getvidlink.php?v=<path>&embed=<embed>&hd=1
        params = [
            f"v={quote(video_path, safe='/')}",
            f"embed={quote(embed_param, safe='')}",
            "hd=1",
        ]
        if pid_param:
            params.append(f"pid={quote(pid_param, safe='')}")
        if h_param:
            params.append(f"h={quote(h_param, safe='')}")
        if t_param:
            params.append(f"t={quote(t_param, safe='')}")

        embed_origin = f"{parsed.scheme}://{parsed.netloc}"
        getvidlink = urljoin(embed_origin, "/inc/embed/getvidlink.php")
        video_js_url = (
            urljoin(embed_origin, "/inc/embed/video-js.php")
            + "?"
            + parsed.query.replace("%20", "+")
        )

        common_page_headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

        try:
            self.net.session.get(
                embed_url,
                headers={
                    **common_page_headers,
                    "Referer": "https://www.wco.tv/",
                    "Sec-Fetch-Dest": "iframe",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "cross-site",
                },
                allow_redirects=True,
                timeout=15,
            )
            logger.debug(
                f"  index.php loaded — {len(dict(self.net.session.cookies))} cookie(s) set"
            )
        except Exception as e:
            logger.debug(f"  index.php preload failed (non-fatal): {e}")

        try:
            self.net.session.get(
                video_js_url,
                headers={
                    **common_page_headers,
                    "Referer": embed_url,
                    "Sec-Fetch-Dest": "iframe",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-User": "?1",
                },
                allow_redirects=True,
                timeout=15,
            )
            logger.debug(
                f"  video-js.php loaded — {len(dict(self.net.session.cookies))} cookie(s) set"
            )
        except Exception as e:
            logger.debug(f"  video-js.php preload failed (non-fatal): {e}")

        xhr_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Referer": video_js_url,
            "Origin": embed_origin,
        }

        request_url = f"{getvidlink}?{'&'.join(params)}"
        logger.debug(f"  getvidlink: {request_url[:240]}")
        resp = self.net.raw_get(
            request_url,
            headers=xhr_headers,
            allow_redirects=True,
            timeout=20,
        )

        try:
            data = resp.json()
        except Exception as e:
            raise RuntimeError(
                f"getvidlink returned non-JSON response: {resp.text[:500]}"
            ) from e

        logger.debug(
            "  getvidlink JSON:\n"
            + json.dumps(data, indent=2, ensure_ascii=False)
        )

        enc = str(data.get("enc") or "")
        hd_token = str(data.get("hd") or "")
        fhd_token = str(data.get("fhd") or "")
        sub_token = str(data.get("sub") or "")

        server = self._normalise_origin(data.get("server"))
        cdn = self._normalise_origin(data.get("cdn"))

        # This is the critical detail from the supplied player source:
        #   const videoUrl = server + '/getvid?evid=' + vsd + '&json';
        #   sources use server + '/getvid?evid=' + vhd/vfhd.
        if not server:
            raise RuntimeError(
                "getvidlink returned no usable 'server' field\n"
                f"  response: {json.dumps(data, ensure_ascii=False)}"
            )

        logger.debug(
            f"  Player endpoints: server={server!r} cdn={cdn!r} "
            f"enc={enc[:10]}... hd={bool(hd_token)} fhd={bool(fhd_token)} sub={bool(sub_token)}"
        )

        if not (enc or hd_token or fhd_token):
            raise RuntimeError(
                "getvidlink returned no video tokens\n"
                f"  embed: {embed_url[:100]}\n"
                f"  response: {json.dumps(data, ensure_ascii=False)}"
            )

        self.net.prime_server(server)
        if cdn and cdn != server:
            self.net.prime_server(cdn)

        resolver_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://embed.wcostream.com/",
            "Origin": "https://embed.wcostream.com",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }

        sources: list[dict] = []

        # SD: current player explicitly resolves through &json and expects
        # the response body to be the redirected MP4 URL.
        if enc:
            sd_url = self._resolve_json_token(
                server,
                enc,
                resolver_headers,
                fallback_servers=[cdn] if cdn else [],
            )
            if sd_url:
                sources.append({"label": "480p", "url": sd_url, "fallback_urls": []})

        # HD/FHD: current player feeds server/getvid directly to Video.js and
        # lets the browser follow the delivery-node redirect.
        fallback_hosts = [h for h in (cdn,) if h and h != server]
        if hd_token:
            sources.append({
                "label": "720p",
                "url": self._build_token_url(server, hd_token),
                "fallback_urls": [self._build_token_url(h, hd_token) for h in fallback_hosts],
            })
        if fhd_token:
            sources.append({
                "label": "1080p",
                "url": self._build_token_url(server, fhd_token),
                "fallback_urls": [self._build_token_url(h, fhd_token) for h in fallback_hosts],
            })

        deduped: list[dict] = []
        seen: set[str] = set()
        for source in sources:
            if source["url"] not in seen:
                seen.add(source["url"])
                deduped.append(source)

        if not deduped:
            raise RuntimeError(
                "No video sources could be constructed\n"
                f"  server: {server}\n"
                f"  cdn: {cdn}\n"
                f"  response keys: {sorted(data.keys())}"
            )

        logger.debug(
            "  Sources: "
            + ", ".join(f"{s['label']}={s['url'][:120]}" for s in deduped)
        )
        return deduped

    def _resolve_json_token(
        self,
        server: str,
        token: str,
        headers: dict,
        fallback_servers: list[str] | None = None,
    ) -> str | None:
        """Resolve an SD WCO token via the player's `&json` endpoint."""
        candidates: list[str] = []
        for value in [server, *(fallback_servers or [])]:
            value = self._normalise_origin(value)
            if value and value not in candidates:
                candidates.append(value)

        response = None
        for base in candidates:
            url = self._build_token_url(base, token) + "&json"
            logger.debug(f"  SD resolver: {url[:180]}")
            try:
                response = self.net.session.get(
                    url,
                    headers=headers,
                    allow_redirects=True,
                    timeout=20,
                )
                logger.debug(
                    f"  SD resolver response: {response.status_code} "
                    f"{url[:100]} -> {response.url[:140]}"
                )

                if response.status_code != 200:
                    logger.debug(
                        f"  SD resolver HTTP {response.status_code}; trying next host"
                    )
                    continue

                if self.net._looks_like_html(response):
                    preview = response.text[:300].replace("\n", " ")
                    logger.debug(f"  SD resolver returned HTML: {preview!r}")
                    continue

                try:
                    payload = response.json()
                except Exception:
                    payload = response.text.strip().strip('"')

                candidate = None
                if isinstance(payload, str):
                    candidate = payload
                elif isinstance(payload, dict):
                    for key in ("url", "src", "file", "media", "redirect", "location"):
                        value = payload.get(key)
                        if isinstance(value, str):
                            candidate = value
                            break

                if not candidate or not candidate.startswith(("http://", "https://")):
                    logger.debug(f"  SD resolver returned no absolute URL: {payload!r}")
                    continue

                candidate = candidate.replace("&json", "").replace("?json", "")
                host = (urlparse(candidate).hostname or "").lower()
                logger.debug(f"  SD resolved URL: {candidate[:160]}")

                if self._is_wco_capacity_host(host):
                    logger.debug(f"  SD resolver returned capacity host: {host}")
                    continue

                return candidate

            except Exception as e:
                logger.debug(f"  SD resolver failed on {base}: {e}")
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                    response = None

        return None

    @staticmethod
    def _build_token_url(server: str, token: str) -> str:
        return f"{server.rstrip('/')}/getvid?evid={quote(token, safe='')}"

    @staticmethod
    def _normalise_origin(value: str | None) -> str | None:
        if not value:
            return None
        value = str(value).strip().rstrip("/")
        if not value:
            return None
        if not re.match(r"^https?://", value, flags=re.IGNORECASE):
            value = "https://" + value
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            return None
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _is_wco_capacity_host(host: str) -> bool:
        """Match the site's own subdomain-capacity check."""
        host = (host or "").strip().lower().rstrip(".")
        if not host:
            return True
        parts = host.split(".")
        if any(not part for part in parts):
            return True
        return host in {"wcostream.com", "www.wcostream.com"} or len(parts) < 3

    # ---------------- Saturn/HLS player ----------------

    def _extract_saturn(self, embed_url: str) -> list[dict]:
        captured_m3u8: list[str] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            page.on(
                "request",
                lambda r: captured_m3u8.append(r.url) if ".m3u8" in r.url else None,
            )

            try:
                page.goto(embed_url, timeout=30_000, wait_until="domcontentloaded")
            except Exception as e:
                logger.debug(f"  Saturn page load: {e}")

            try:
                page.click(
                    "video, .vjs-big-play-button, .play-button, body",
                    timeout=3_000,
                )
            except Exception:
                pass
            page.wait_for_timeout(5_000)

            player_sources: list[dict] = page.evaluate(
                """() => {
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
                }"""
            ) or []

            context.close()
            browser.close()

        hls_url = next(
            (
                e["url"]
                for e in player_sources
                if ".m3u8" in e.get("url", "") and "blob:" not in e["url"]
            ),
            next((u for u in captured_m3u8 if u.startswith("http")), ""),
        )
        if not hls_url:
            raise RuntimeError("Saturn embed: no HLS URL found")

        # Preserve the original path structure but replace the quality segment.
        m = re.match(r"^(.*?/0/)(?:\d+)(/index\.m3u8)$", hls_url)
        if m:
            prefix, suffix = m.groups()
        else:
            prefix = hls_url.rsplit("/", 2)[0] + "/"
            suffix = "/index.m3u8"

        return [
            {"label": "480p", "url": f"{prefix}854{suffix}"},
            {"label": "720p", "url": f"{prefix}1280{suffix}"},
            {"label": "1080p", "url": f"{prefix}1920{suffix}"},
        ]

    def select_resolution(self, sources: list[dict], preference: str) -> dict:
        mapping = {"sd": "480p", "hd": "720p", "fhd": "1080p"}
        target = mapping.get(preference.lower())
        if target:
            match = next((s for s in sources if s["label"] == target), None)
            if match:
                return match

        def _res(source: dict) -> int:
            m = re.search(r"(\d+)", source.get("label", "0"))
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
            if a.get("href")
        ]


# ---------------------------------------------------------------------------
# Error log
# ---------------------------------------------------------------------------

class ErrorLog:
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
        if self._entries:
            print(
                f"\n{C.RED}Errors occurred during download of some episodes. "
                f"Check the specifics in errors.txt{C.RESET}"
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
    series_name: str | None = None,
) -> tuple[bool, str, str]:
    try:
        embed_url = scraper.get_embed_url(url)
    except RuntimeError as e:
        debug_log = logger.flush()
        if "Premium episode" in str(e):
            return False, "⏭  Skipped  — premium only", ""
        detail = f"Could not load page: {e}\n{traceback.format_exc()}"
        return False, "✗  Failed   — could not load page", detail + (
            f"\n\nDebug log:\n{debug_log}" if debug_log else ""
        )

    parsed_show, season, episode = parse_episode_meta(url)
    show = series_name if series_name is not None else parsed_show
    language = detect_language(url, episode)
    logger.debug(f"── {show} / {season} / {episode} ({language}) ──")

    existing = db.get_episode_filename(show, season, episode, language)
    if existing:
        progress.remove_pending(url)
        logger.clear()
        return (
            True,
            f"⏭  {C.DIM}Skipped{C.RESET}  — already downloaded ({existing})",
            "",
        )

    try:
        sources = scraper.get_sources(embed_url)
    except Exception as e:
        debug_log = logger.flush()
        detail = f"Source extraction failed: {e}\n{traceback.format_exc()}"
        if debug_log:
            detail += f"\n\nDebug log:\n{debug_log}"
        return False, "✗  Failed   — source extraction error", detail

    source = scraper.select_resolution(sources, config.settings.resolution)
    media = source["url"]
    res = source["label"]
    is_hls = ".m3u8" in media.lower()

    hex_name = db.get_next_filename()
    folder = config.settings.download_folder
    temp = pathlib.Path(folder) / f"{hex_name}.part"
    final = pathlib.Path(folder) / hex_name
    label = f"{show} — {season} {episode} [{language}] [{res}]"

    progress.add_pending(url)

    try:
        candidates = [media, *source.get("fallback_urls", [])]
        # Preserve order while removing duplicates. The first URL is always the
        # exact URL the player would normally use. Fallbacks are only attempted
        # after a real media request fails (e.g. a stale d02 delivery node).
        candidates = list(dict.fromkeys(candidates))

        last_error: Exception | None = None
        for attempt, candidate in enumerate(candidates, 1):
            try:
                if temp.exists():
                    temp.unlink()
                if final.exists():
                    final.unlink()

                logger.debug(
                    f"  Media attempt {attempt}/{len(candidates)}: {candidate[:140]}"
                )
                if ".m3u8" in candidate.lower():
                    network.download_hls(label, candidate, hex_name, folder)
                else:
                    network.download_file(label, candidate, f"{hex_name}.part", folder)
                    temp.rename(final)
                last_error = None
                media = candidate
                break
            except Exception as candidate_error:
                last_error = candidate_error
                logger.debug(
                    f"  Media attempt {attempt} failed: {candidate_error}"
                )

        if last_error is not None:
            raise last_error

        db.add_episode(show, season, episode, hex_name, language)
        progress.remove_pending(url)
        logger.clear()
        return (
            True,
            f"✓  {C.GREEN}Done{C.RESET}  {C.DIM}[{res}]{C.RESET}  — "
            f"{season} {episode} [{language}] → {C.DIM}{hex_name}{C.RESET}",
            "",
        )

    except Exception as e:
        debug_log = logger.flush()
        for p in (temp, final):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        detail = (
            f"Download error: {e}\n"
            f"Media URL: {media}\n"
            f"{traceback.format_exc()}"
        )
        if debug_log:
            detail += f"\n\nDebug log:\n{debug_log}"
        return False, "✗  Failed   — download error", detail


def download_series(
    url: str,
    network: Network,
    scraper: Scraper,
    config: Config,
    db: LibraryDB,
    progress: Progress,
    error_log: ErrorLog,
    workers: int = 3,
) -> None:
    series_name = parse_series_name(url)
    print(f"  Series name: {series_name}")
    print("  Fetching episode list...")

    episodes = scraper.get_episodes(url)
    if not episodes:
        print("  No episodes found.")
        return

    total = len(episodes)
    print(f"  Found {total} episode(s). Starting download ({workers} parallel)...\n")
    ok = skip = fail = 0
    # Lock to serialize counter updates and print statements
    print_lock = threading.Lock()
    counters = {"ok": 0, "skip": 0, "fail": 0}

    def _do(item: tuple[int, tuple[str, str]]) -> None:
        i, (ep_url, label) = item
        with print_lock:
            print(f"  [{i:>3}/{total}] {label}")
        success, msg, detail = download_episode(
            ep_url, network, scraper, config, db, progress,
            series_name=series_name,
        )
        with print_lock:
            print(f"         {msg}")
            if success:
                if "Skipped" in msg:
                    counters["skip"] += 1
                else:
                    counters["ok"] += 1
            else:
                if "premium" in msg.lower() or "Skipped" in msg:
                    counters["skip"] += 1
                else:
                    counters["fail"] += 1
                    error_log.add(label, ep_url, detail)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_do, enumerate(episodes, 1)))

    print(f"\n  Done — {counters['ok']} downloaded, {counters['skip']} skipped, {counters['fail']} failed.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

app = typer.Typer(
    help="wco-dl — download anime & cartoons from wco.tv",
    add_completion=False,
)


def _init() -> tuple[Config, Network, Scraper, LibraryDB, Progress]:
    config = Config()
    network = Network()
    scraper = Scraper(network)
    db = LibraryDB(config)
    prog = Progress(config)
    return config, network, scraper, db, prog


@app.command()
def main(
    target: str = typer.Argument(None),
    search: bool = typer.Option(False, "--search", "-s", help="Search for a show"),
    episode: bool = typer.Option(
        False, "--episode", "-de", help="Download a single episode URL"
    ),
    series: bool = typer.Option(
        False, "--series", "-ds", help="Download a full series URL"
    ),
    all_series: bool = typer.Option(
        False, "--all", "-da", help="Download all series from list file"
    ),
    list_file: str = typer.Option(
        DEFAULT_SERIES_LIST,
        "--list-file",
        help="Series list file (one URL per line)",
    ),
    workers: int = typer.Option(
        6, "--workers", "-w", help="Number of parallel episode downloads (default: 6)",
    ),
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
        if search:
            if not target:
                typer.echo("--search requires a query")
                raise typer.Exit(1)
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

        elif episode:
            if not target:
                typer.echo("--episode requires a URL")
                raise typer.Exit(1)
            show, season, ep = parse_episode_meta(target)
            print(f"Downloading: {show} — {season} {ep}")
            success, msg, detail = download_episode(
                target,
                network,
                scraper,
                config,
                db,
                progress,
                series_name=None,
            )
            print(f"  {msg}")
            if not success and detail:
                error_log.add(f"{show} — {season} {ep}", target, detail)

        elif series:
            if not target:
                typer.echo("--series requires a URL")
                raise typer.Exit(1)
            series_name = parse_series_name(target)
            print(f"Series: {series_name}")
            download_series(
                target, network, scraper, config, db, progress, error_log,
                workers=workers,
            )

        elif all_series:
            path = pathlib.Path(list_file)
            if not path.is_file():
                typer.echo(f"List file not found: {path}")
                raise typer.Exit(1)

            urls = [
                line.strip()
                for line in path.read_text("utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            ]
            if not urls:
                print("Series list is empty.")
                return

            print(f"Found {len(urls)} series in list.\n")
            if not typer.confirm("Download all? (This may take a very long time)"):
                print("Aborted.")
                return

            for i, u in enumerate(urls, 1):
                print(f"\n[{i}/{len(urls)}] {u}")
                try:
                    download_series(
                        u, network, scraper, config, db, progress, error_log,
                        workers=workers,
                    )
                except Exception as e:
                    print(f"  ✗ Series failed: {e}")
                    error_log.add(
                        u,
                        u,
                        f"Series-level failure: {e}\n{traceback.format_exc()}",
                    )

    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
    finally:
        try:
            network.kill_active_processes()
        finally:
            db.close()
            if error_log.has_errors():
                error_log.write("errors.txt")
                error_log.print_summary()


if __name__ == "__main__":
    app()
