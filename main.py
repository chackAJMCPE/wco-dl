"""
wco-dl — download anime & cartoons from wco.tv

Player flow:
    getvidlink.php
        ├─ response.server + /getvid?evid=<SD>&json -> MP4 URL
        ├─ response.server + /getvid?evid=<HD>      -> media redirect
        └─ response.server + /getvid?evid=<FHD>     -> media redirect
"""

import concurrent.futures
import json
import pathlib
import re
import shutil
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

warnings.filterwarnings("ignore", category=ResourceWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36"
)
AD_DOMAINS = {"ad.a-ads.com", "doubleclick.net", "googlesyndication.com", "adservice.google"}
MOVIE_SLUG_WORDS = {"movie", "film", "the-movie"}
DEFAULT_SERIES_LIST = ".wco-dl/series_list.txt"

# ---------------------------------------------------------------------------
# Terminal colours
# ---------------------------------------------------------------------------

class C:
    _on = getattr(__import__("sys").stdout, "isatty", lambda: False)()
    GREEN  = "\033[92m" if _on else ""
    YELLOW = "\033[93m" if _on else ""
    RED    = "\033[91m" if _on else ""
    DIM    = "\033[2m"  if _on else ""
    BOLD   = "\033[1m"  if _on else ""
    RESET  = "\033[0m"  if _on else ""

# ---------------------------------------------------------------------------
# Per-episode debug logger (silent until a failure flushes it)
# ---------------------------------------------------------------------------

class EpisodeLogger:
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
# Configuration
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
            self.settings_path.write_text(self.settings.model_dump_json(indent=2), "utf-8")

# ---------------------------------------------------------------------------
# Library  (Jellyfin-compatible folder layout + URL → path tracking)
# ---------------------------------------------------------------------------

class Library:
    """
    Folder layout:
        <root>/
            <Show Name>/
                Season 01/
                    Show Name - S01E01 - Episode 1 [Japanese].mp4
                    Show Name - S01E01A - Episode 1A [Japanese].mp4
                    Show Name - S01E24.5 - Episode 24.5 [Japanese].mp4
                Season 00/        ← OVAs and Movies (Jellyfin specials convention)
                    Show Name - S00E01 - OVA 01 [Japanese].mp4

    Tracking: <root>/downloaded.json  { episode_url: relative_path }
    """

    _SAFE_RE = re.compile(r'[\\/:*?"<>|]')

    def __init__(self, config: Config):
        self.root = pathlib.Path(config.settings.download_folder)
        self.root.mkdir(parents=True, exist_ok=True)
        self._path = self.root / "downloaded.json"
        self._lock = threading.Lock()
        try:
            self._data: dict[str, str] = (
                json.loads(self._path.read_text("utf-8")) if self._path.exists() else {}
            )
        except (json.JSONDecodeError, OSError):
            self._data = {}

    def _save(self):
        self._path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), "utf-8")

    @classmethod
    def _safe(cls, s: str) -> str:
        return cls._SAFE_RE.sub("", s).strip()

    @staticmethod
    def _fmt_ep(ep_no: str) -> str:
        """1 → 01, 1A → 01A, 24.5 → 24.5, 100 → 100"""
        m = re.match(r"^(\d+)(\.\d+)?([A-Za-z]?)$", ep_no)
        if not m:
            return ep_no
        num, decimal, letter = m.group(1), m.group(2) or "", m.group(3)
        return f"{int(num):02d}{decimal}{letter.upper()}"

    def build_path(self, show: str, season: str, episode: str, language: str) -> pathlib.Path:
        show_s = self._safe(show)
        ep_m   = re.search(r"([\d]+(?:\.[\d]+)?[A-Za-z]?)$", episode)
        ep_fmt = self._fmt_ep(ep_m.group(1) if ep_m else "0")

        if season in ("OVA", "Movies"):
            kind = "OVA" if season == "OVA" else "Movie"
            return (self.root / show_s / "Season 00" /
                    f"{show_s} - S00E{ep_fmt} - {kind} {ep_fmt} [{language}].mp4")

        sea_m = re.search(r"\d+", season)
        snum  = sea_m.group().zfill(2) if sea_m else "01"
        ep_title = self._safe(episode)
        return (self.root / show_s / f"Season {snum}" /
                f"{show_s} - S{snum}E{ep_fmt} - {ep_title} [{language}].mp4")

    def is_downloaded(self, url: str) -> str | None:
        """Returns relative path if file exists on disk, else None."""
        with self._lock:
            rel = self._data.get(url)
        if not rel:
            return None
        dest = self.root / rel
        return rel if dest.is_file() and dest.stat().st_size > 0 else None

    def mark_downloaded(self, url: str, dest: pathlib.Path):
        rel = str(dest.relative_to(self.root))
        with self._lock:
            self._data[url] = rel
            self._save()

    def cleanup_parts(self):
        for f in self.root.rglob("*.part"):
            try:
                f.unlink()
            except OSError:
                pass

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

class Network:
    def __init__(self):
        self.session: cloudscraper.CloudScraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._primed: set[str] = set()
        self._procs: list[subprocess.Popen] = []
        self._proc_lock = threading.Lock()

    def raw_get(self, url: str, headers: dict | None = None,
                allow_redirects: bool = False, timeout: int = 30) -> requests.Response:
        r = self.session.get(url, headers=headers or {}, allow_redirects=allow_redirects, timeout=timeout)
        logger.debug(f"  → GET {r.status_code} {url[:120]}")
        if r.ok:
            return r
        raise requests.HTTPError(f"HTTP {r.status_code} for {url[:100]}")

    def get(self, url: str, headers: dict | None = None) -> str:
        return self.raw_get(url, headers).text

    def post(self, url: str, headers: dict | None = None, data: dict | None = None) -> str:
        r = self.session.post(url, headers=headers or {}, data=data or {}, timeout=30)
        if r.ok:
            return r.text
        raise requests.HTTPError(f"HTTP {r.status_code} for {url[:100]}")

    def get_rendered_page(self, url: str, timeout_ms: int = 30_000, referer: str = "") -> str:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                extra_http_headers={"Referer": referer} if referer else {},
            )
            page = ctx.new_page()
            for wait in ("load", "domcontentloaded"):
                try:
                    page.goto(url, timeout=timeout_ms, wait_until=wait)
                    break
                except Exception as e:
                    logger.debug(f"  Playwright ({wait}): {e}")
            html = page.content()
            ctx.close()
            browser.close()
        return html

    @staticmethod
    def _is_delivery_node(host: str) -> bool:
        return bool(re.match(r"^(?:d|nd|m)\d+\.", host, re.IGNORECASE))

    def prime_server(self, server_url: str):
        host = urlparse(server_url).hostname or ""
        if self._is_delivery_node(host) or server_url in self._primed:
            return
        try:
            self.session.get(server_url, headers={"Referer": "https://embed.wcostream.com/"},
                             timeout=10, allow_redirects=True)
            self._primed.add(server_url)
            logger.debug(f"  CDN primed: {server_url}")
        except Exception as e:
            logger.debug(f"  CDN priming failed (non-fatal): {e}")

    @staticmethod
    def _looks_like_html(r: requests.Response) -> bool:
        ct = r.headers.get("Content-Type", "").lower()
        if "text/html" in ct or "application/xhtml" in ct:
            return True
        preview = r.content[:64].lstrip().lower()
        return preview.startswith(b"<!doctype html") or preview.startswith(b"<html")

    def download_file(self, label: str, url: str, dest: pathlib.Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        resume_from = dest.stat().st_size if dest.exists() else 0
        phpsessid = self.session.cookies.get("PHPSESSID")

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
        if phpsessid:
            headers["Cookie"] = f"PHPSESSID={phpsessid}"

        logger.debug(f"  Downloading: {url[:140]}")
        r = self.session.get(url, headers=headers, stream=True, allow_redirects=True, timeout=45)
        logger.debug(f"  Final URL:   {r.url[:140]}")
        logger.debug(f"  Status: {r.status_code}  Content-Type: {r.headers.get('Content-Type', 'N/A')}")

        # Server ignored Range header — restart from scratch
        if resume_from and r.status_code == 200:
            logger.debug("  Range ignored; restarting from byte 0")
            r.close()
            try:
                dest.unlink()
            except FileNotFoundError:
                pass
            resume_from = 0
            headers.pop("Range", None)
            r = self.session.get(url, headers=headers, stream=True, allow_redirects=True, timeout=45)

        if self._looks_like_html(r):
            body = r.content[:800].decode("utf-8", errors="replace")
            logger.debug(f"  Response body: {body}")
            raise RuntimeError(
                f"CDN returned HTML instead of video\n"
                f"  URL: {url}\n  Final: {r.url}\n"
                f"  Status: {r.status_code}\n  Body: {body[:300]}"
            )

        if r.status_code not in (200, 206):
            raise RuntimeError(f"Unexpected HTTP {r.status_code}\n  URL: {url}\n  Final: {r.url}")

        content_length = r.headers.get("Content-Length")
        total = None
        if content_length:
            try:
                n = int(content_length)
                total = (n + resume_from) if r.status_code == 206 else n
            except ValueError:
                pass

        if total is not None and resume_from == total:
            r.close()
            return

        mode = "ab" if (resume_from and r.status_code == 206) else "wb"
        initial = resume_from if mode == "ab" else 0

        with tqdm(total=total, unit="B", unit_scale=True, unit_divisor=1024,
                  desc=label, initial=initial) as bar:
            with open(dest, mode) as fh:
                for chunk in r.iter_content(64 * 1024):
                    if chunk:
                        fh.write(chunk)
                        bar.update(len(chunk))
        r.close()

    def download_hls(self, label: str, url: str, dest: pathlib.Path) -> None:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found on PATH")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0:
            return

        referer = (
            "https://vhs.watchanimesub.net/"
            if ("watchanimesub" in url or "cizgifilmlerizle" in url)
            else "https://embed.wcostream.com/"
        )
        try:
            resolved = self.session.head(url, allow_redirects=True,
                                         headers={"Referer": referer, "User-Agent": USER_AGENT},
                                         timeout=15).url
        except Exception:
            resolved = url
        logger.debug(f"  HLS: {url[:80]} → {resolved[:100]}")

        proc = subprocess.Popen(
            ["ffmpeg", "-y", "-user_agent", USER_AGENT,
             "-headers", f"Referer: {referer}\r\n",
             "-i", resolved, "-c", "copy", "-bsf:a", "aac_adtstoasc",
             "-progress", "pipe:1", str(dest)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        with self._proc_lock:
            self._procs.append(proc)

        duration: list[int] = []
        stderr_lines: list[str] = []

        def _drain():
            for line in (proc.stderr or []):
                stderr_lines.append(line)
                m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)", line)
                if m:
                    duration.append(int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]))

        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        t.join(timeout=5)

        total = duration[0] if duration else 0
        bar = (
            tqdm(total=total, desc=label, unit="s",
                 bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}s [{elapsed}<{remaining}]")
            if total else None
        )
        if not bar:
            print(label)

        for line in (proc.stdout or []):
            if line.strip().startswith("out_time_us=") and bar:
                try:
                    bar.n = min(int(line.split("=", 1)[1]) // 1_000_000, bar.total)
                    bar.refresh()
                except (ValueError, IndexError):
                    pass

        proc.wait()
        t.join()
        with self._proc_lock:
            if proc in self._procs:
                self._procs.remove(proc)

        if bar:
            bar.n = bar.total
            bar.refresh()
            bar.close()

        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed (code {proc.returncode})\n" + "".join(stderr_lines[-30:]))

    def kill_active_processes(self):
        with self._proc_lock:
            for p in list(self._procs):
                try:
                    p.kill()
                except OSError:
                    pass
            self._procs.clear()

# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def title_from_slug(slug: str) -> str:
    text = re.sub(r"(?<=\d)-(?=\d)", ".", slug).replace("-", " ").strip()
    return re.sub(r"\s+", " ", text).title()

def parse_series_name(url: str) -> str:
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    if "anime" in parts:
        idx = parts.index("anime")
        if idx + 1 >= len(parts):
            raise ValueError(f"Could not determine series name from URL: {url}")
        slug = parts[idx + 1]
    else:
        slug = parts[-1] if parts else ""
    slug = re.sub(r"(?i)(?:-)?(?:season|episode|ova|movie|film).*$", "", slug).strip("-")
    if not slug:
        raise ValueError(f"Could not determine series name from URL: {url}")
    return title_from_slug(slug)

def parse_episode_meta(url: str) -> tuple[str, str, str]:
    slug = urlparse(url).path.strip("/")
    text = re.sub(r"(?<=\d)-(?=\d)", ".", slug).replace("-", " ").strip()
    text = re.sub(r"\s+english\s+(subbed|dubbed)\s*$", "", text, flags=re.IGNORECASE).strip()

    ep_m   = re.search(r"\bepisode\s*(\d+(?:\.\d+)?[A-Za-z]?)\b", text, re.IGNORECASE)
    ep_no  = ep_m.group(1).upper() if ep_m else "0"
    sea_m  = re.search(r"\bseason\s*(\d+)\b", text, re.IGNORECASE)
    sea_no = sea_m.group(1) if sea_m else None
    is_ova   = bool(re.search(r"\bova\b", text, re.IGNORECASE))
    is_movie = bool(set(slug.split("-")) & MOVIE_SLUG_WORDS)

    name_end = re.search(r"\b(season|episode|ova|movie|film|part\s*\d+(?:\.\d+)?)\b", text, re.IGNORECASE)
    show = title_from_slug(text[:name_end.start()].strip() if name_end else text.strip())

    if is_movie:
        return show, "Movies", f"Episode {ep_no}"
    if is_ova:
        return show, "OVA", f"OVA {ep_no}"
    season = f"Season {sea_no}" if sea_no else "Season 1"
    return show, season, f"Episode {ep_no}"

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

    # --- episode list ---

    def get_episodes(self, url: str) -> list[tuple[str, str]]:
        html = self.net.get_rendered_page(url)
        soup = bs4.BeautifulSoup(html, "html.parser")
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        links = (soup.find_all("a", class_="dark-episode-item")
                 or soup.find_all("a", class_="sonra"))
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

    # --- embed URL extraction ---

    def get_embed_url(self, episode_url: str) -> str:
        html = self._fetch_episode_page(episode_url)
        src  = self._extract_iframe_src(html, episode_url)
        if src:
            return src
        logger.debug("  HTTP fetch missed iframe, falling back to Playwright")
        html = self.net.get_rendered_page(episode_url)
        src  = self._extract_iframe_src(html, episode_url)
        if src:
            return src
        raise RuntimeError("No embed iframe found on page")

    def _fetch_episode_page(self, url: str) -> str:
        try:
            r = self.net.session.get(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
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
            logger.debug(f"  iframe #{i}  id={fr.get('id','(none)')}  src={fr.get('src','')[:120]}")

        if "Become a Premium User Now!" in html or "This Video Is for Premium Users" in html:
            raise RuntimeError("Premium episode, skipping")

        iframe = (
            soup.find("iframe", {"id": "frameSaturn1"})
            or soup.find("iframe", {"id": "frameNewcizgifilmuploads0"})
            or next((fr for fr in all_iframes
                     if fr.get("src") and not any(ad in fr["src"] for ad in AD_DOMAINS)), None)
        )
        if iframe is None:
            return None
        src = iframe["src"]
        logger.debug(f"  Selected embed: {src[:140]}")
        return src

    # --- video source extraction ---

    def get_sources(self, embed_url: str) -> list[dict]:
        if "watchanimesub.net" in embed_url or "saturn" in embed_url.lower():
            return self._extract_saturn(embed_url)
        return self._extract_legacy(embed_url)

    def _extract_legacy(self, embed_url: str) -> list[dict]:
        parsed      = urlparse(embed_url)
        query       = parse_qs(parsed.query, keep_blank_values=True)
        file_param  = query.get("file",  [""])[0]
        embed_param = query.get("embed", [""])[0]
        pid_param   = query.get("pid",   [""])[0]
        h_param     = query.get("h",     [""])[0]
        t_param     = query.get("t",     [""])[0]

        if not file_param:
            raise ValueError(f"No 'file' param in embed URL: {embed_url[:80]}")

        base = file_param.rsplit(".", 1)[0] + ".mp4"
        video_path = base if embed_param in ("ndisk", "anime") else f"{embed_param}/{base}"
        v_encoded  = quote(video_path, safe="/")

        params = [f"v={v_encoded}", f"embed={embed_param}", "hd=1", "fullhd=1"]
        if pid_param: params.append(f"pid={quote(pid_param, safe='')}")
        if h_param:   params.append(f"h={quote(h_param, safe='')}")
        if t_param:   params.append(f"t={quote(t_param, safe='')}")

        getvidlink = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/inc/embed/getvidlink.php")
        video_js_url = (urljoin(f"{parsed.scheme}://{parsed.netloc}", "/inc/embed/video-js.php")
                        + "?" + parsed.query.replace("%20", "+"))

        # Load index.php (sets PHPSESSID) then video-js.php (authorises cdn.wcostream.com)
        for step_url, referer, dest_label in [
            (embed_url, "https://www.wco.tv/", "index.php"),
            (video_js_url, embed_url, "video-js.php"),
        ]:
            try:
                self.net.session.get(step_url, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Referer": referer,
                    "Sec-Fetch-Dest": "iframe",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin" if "video-js" in step_url else "cross-site",
                }, allow_redirects=True, timeout=15)
                logger.debug(f"  {dest_label} loaded — {len(dict(self.net.session.cookies))} cookie(s)")
            except Exception as e:
                logger.debug(f"  {dest_label} preload failed (non-fatal): {e}")

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

        logger.debug(f"  getvidlink: {getvidlink}?{'&'.join(params)[:120]}")
        resp = self.net.raw_get(f"{getvidlink}?{'&'.join(params)}", headers=xhr_headers)
        data = resp.json()
        enc       = str(data.get("enc")  or "")
        hd_token  = str(data.get("hd")   or "")
        fhd_token = str(data.get("fhd")  or "")
        server    = str(data.get("server") or "")
        cdn       = str(data.get("cdn")    or "")
        sub_token = str(data.get("sub")   or "")

        logger.debug(
            f"  getvidlink OK  server={server!r}  "
            f"enc={enc[:10]}... hd={bool(hd_token)} fhd={bool(fhd_token)} sub={bool(sub_token)}"
        )

        if not (enc or hd_token or fhd_token):
            raise RuntimeError(f"getvidlink returned no tokens: {data}")

        self.net.prime_server(server)
        if cdn and cdn != server:
            self.net.prime_server(cdn)

        resolver_headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://embed.wcostream.com/",
            "Origin": "https://embed.wcostream.com",
        }
        if phpsessid:
            resolver_headers["Cookie"] = f"PHPSESSID={phpsessid}"

        fallback_hosts = [h for h in (cdn,) if h and h != server]

        sources: list[dict] = []

        if enc:
            sd_url = self._resolve_json_token(server, enc, resolver_headers,
                                              fallback_servers=fallback_hosts)
            direct_sd = self._build_token_url(server, enc)
            fallbacks = []
            if sd_url and sd_url != direct_sd:
                fallbacks.append(direct_sd)
            if sd_url:
                sources.append({"label": "480p", "url": sd_url, "fallback_urls": fallbacks})
            else:
                sources.append({"label": "480p", "url": direct_sd, "fallback_urls": []})

        direct_enc = self._build_token_url(server, enc) if enc else None

        if hd_token:
            hd_fallbacks = [self._build_token_url(h, hd_token) for h in fallback_hosts]
            if direct_enc:
                hd_fallbacks.append(direct_enc)
            sources.append({"label": "720p", "url": self._build_token_url(server, hd_token),
                            "fallback_urls": hd_fallbacks})

        if fhd_token:
            fhd_fallbacks = [self._build_token_url(h, fhd_token) for h in fallback_hosts]
            if hd_token:
                fhd_fallbacks.append(self._build_token_url(server, hd_token))
            if direct_enc:
                fhd_fallbacks.append(direct_enc)
            sources.append({"label": "1080p", "url": self._build_token_url(server, fhd_token),
                            "fallback_urls": fhd_fallbacks})

        deduped = list({s["url"]: s for s in sources}.values())
        if not deduped:
            raise RuntimeError(f"No video sources: server={server} cdn={cdn} keys={sorted(data.keys())}")

        logger.debug("  Sources: " + ", ".join(f"{s['label']}={s['url'][:80]}" for s in deduped))
        return deduped

    def _resolve_json_token(self, server: str, token: str, headers: dict,
                            fallback_servers: list[str] | None = None) -> str | None:
        candidates = [server] + (fallback_servers or [])
        for base in candidates:
            response = None
            try:
                url = f"{base}/getvid?evid={token}&json"
                response = self.net.session.get(url, headers=headers, allow_redirects=True, timeout=20)

                # IMPORTANT: do NOT use _looks_like_html() here.
                # The &json endpoint may return text/html with a JSON body.
                try:
                    payload = response.json()
                except Exception:
                    raw = response.text.strip()
                    logger.debug(f"  SD resolver JSON decode failed; raw: {raw[:200]!r}")
                    payload = raw

                logger.debug(f"  SD resolver payload: {payload!r}")
                candidate = None
                if isinstance(payload, str):
                    candidate = payload.strip().strip("\"'")
                elif isinstance(payload, dict):
                    for key in ("url", "src", "file", "media", "redirect", "location"):
                        value = payload.get(key)
                        if isinstance(value, str):
                            candidate = value.strip()
                            break

                if not candidate:
                    logger.debug("  SD resolver returned no usable string")
                    continue

                candidate = candidate.replace("\\/", "/").replace("&json", "").replace("?json", "")

                if not candidate.startswith(("http://", "https://")):
                    logger.debug(f"  SD resolver returned non-absolute URL: {candidate!r}")
                    continue

                host = urlparse(candidate).hostname or ""
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

        return None

    @staticmethod
    def _is_wco_capacity_host(host: str) -> bool:
        host = (host or "").strip().lower().rstrip(".")
        if not host:
            return True
        parts = host.split(".")
        if any(not part for part in parts):
            return True
        return host in {"wcostream.com", "www.wcostream.com"} or len(parts) < 3

    @staticmethod
    def _build_token_url(server: str, token: str) -> str:
        return f"{server}/getvid?evid={token}"

    def _extract_saturn(self, embed_url: str) -> list[dict]:
        captured_m3u8: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=USER_AGENT)
            page = ctx.new_page()
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
            ctx.close()
            browser.close()

        hls_url = next(
            (e["url"] for e in player_sources if ".m3u8" in e.get("url", "") and "blob:" not in e["url"]),
            next((u for u in captured_m3u8 if u.startswith("http")), ""),
        )
        if not hls_url:
            raise RuntimeError("Saturn embed: no HLS URL found")

        base = re.sub(r"/0/\d+/index\.m3u8$", "", hls_url).rstrip("/index.m3u8")
        return [
            {"label": "480p",  "url": f"{base}/0/854/index.m3u8",  "fallback_urls": []},
            {"label": "720p",  "url": f"{base}/0/1280/index.m3u8", "fallback_urls": []},
            {"label": "1080p", "url": f"{base}/0/1920/index.m3u8", "fallback_urls": []},
        ]

    def select_resolution(self, sources: list[dict], preference: str) -> dict:
        if not sources:
            raise RuntimeError("No video sources available")

        def _by(label: str) -> dict | None:
            return next((s for s in sources if s["label"] == label), None)

        def _best() -> dict:
            def _res(s): 
                m = re.search(r"(\d+)", s.get("label", "0"))
                return int(m.group(1)) if m else 0
            return max(sources, key=_res)

        # All preferences cascade: FHD → HD → SD → whatever exists
        return _by("1080p") or _by("720p") or _by("480p") or _best()

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
    def __init__(self):
        self._entries: list[str] = []

    def add(self, episode_label: str, url: str, detail: str, debug_log: str = ""):
        entry = f"Episode : {episode_label}\nURL     : {url}\nError   : {detail}\n"
        if debug_log:
            entry += f"\nDebug log:\n{debug_log}\n"
        self._entries.append(entry)

    def has_errors(self) -> bool:
        return bool(self._entries)

    def write(self, path: str = "errors.txt"):
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"wco-dl error log\n{'─' * 60}\n\n")
            for i, entry in enumerate(self._entries, 1):
                f.write(f"[{i}]\n{entry}\n")

    def print_summary(self):
        if self._entries:
            print(f"\n{C.RED}Errors occurred during download. Check errors.txt{C.RESET}")

# ---------------------------------------------------------------------------
# Download orchestration
# ---------------------------------------------------------------------------

def download_episode(
    url: str,
    network: Network,
    scraper: Scraper,
    config: Config,
    library: Library,
    series_name: str | None = None,
) -> tuple[bool, str, str]:
    try:
        embed_url = scraper.get_embed_url(url)
    except RuntimeError as e:
        debug_log = logger.flush()
        if "Premium episode" in str(e):
            return False, "⏭  Skipped  — premium only", ""
        detail = f"Could not load page: {e}\n{traceback.format_exc()}"
        return False, "✗  Failed   — could not load page", detail + (f"\n\nDebug log:\n{debug_log}" if debug_log else "")

    parsed_show, season, episode = parse_episode_meta(url)
    show     = series_name if series_name is not None else parsed_show
    language = detect_language(url, episode)
    logger.debug(f"── {show} / {season} / {episode} ({language}) ──")

    if library.is_downloaded(url):
        logger.clear()
        return True, f"⏭  {C.DIM}Skipped{C.RESET}  — already downloaded", ""

    try:
        sources = scraper.get_sources(embed_url)
    except Exception as e:
        debug_log = logger.flush()
        detail = f"Source extraction failed: {e}\n{traceback.format_exc()}"
        if debug_log:
            detail += f"\n\nDebug log:\n{debug_log}"
        return False, "✗  Failed   — source extraction error", detail

    source = scraper.select_resolution(sources, config.settings.resolution)
    media  = source["url"]
    res    = source["label"]
    dest   = library.build_path(show, season, episode, language)
    temp   = dest.with_suffix(".part")
    label  = f"{show} — {season} {episode} [{language}] [{res}]"
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        candidates = list(dict.fromkeys([media, *source.get("fallback_urls", [])]))
        last_error: Exception | None = None

        for attempt, candidate in enumerate(candidates, 1):
            try:
                if temp.exists(): temp.unlink()
                if dest.exists(): dest.unlink()
                logger.debug(f"  Media attempt {attempt}/{len(candidates)}: {candidate[:140]}")
                if ".m3u8" in candidate.lower():
                    network.download_hls(label, candidate, dest)
                else:
                    network.download_file(label, candidate, temp)
                    temp.rename(dest)
                last_error = None
                media = candidate
                break
            except Exception as candidate_error:
                last_error = candidate_error
                logger.debug(f"  Media attempt {attempt} failed: {candidate_error}")

        if last_error is not None:
            raise last_error

        library.mark_downloaded(url, dest)
        logger.clear()
        return (
            True,
            f"✓  {C.GREEN}Done{C.RESET}  {C.DIM}[{res}]{C.RESET}  — "
            f"{season} {episode} [{language}] → {C.DIM}{dest.name}{C.RESET}",
            "",
        )

    except Exception as e:
        debug_log = logger.flush()
        for p in (temp, dest):
            if p.exists():
                try: p.unlink()
                except OSError: pass
        detail = f"Download error: {e}\nMedia URL: {media}\n{traceback.format_exc()}"
        if debug_log:
            detail += f"\n\nDebug log:\n{debug_log}"
        return False, "✗  Failed   — download error", detail


def download_series(
    url: str,
    network: Network,
    scraper: Scraper,
    config: Config,
    library: Library,
    error_log: ErrorLog,
    workers: int = 6,
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
    lock = threading.Lock()
    counters = {"ok": 0, "skip": 0, "fail": 0}

    def _do(item: tuple[int, tuple[str, str]]):
        i, (ep_url, label) = item
        with lock:
            print(f"  [{i:>3}/{total}] {label}")
        success, msg, detail = download_episode(ep_url, network, scraper, config, library, series_name=series_name)
        with lock:
            print(f"         {msg}")
            if success:
                counters["skip" if "Skipped" in msg else "ok"] += 1
            else:
                if "premium" in msg.lower() or "Skipped" in msg:
                    counters["skip"] += 1
                else:
                    counters["fail"] += 1
                    error_log.add(label, ep_url, detail, logger.flush())

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_do, enumerate(episodes, 1)))

    print(f"\n  Done — {counters['ok']} downloaded, {counters['skip']} skipped, {counters['fail']} failed.")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

app = typer.Typer(help="wco-dl — download anime & cartoons from wco.tv", add_completion=False)

def _init() -> tuple[Config, Network, Scraper, Library]:
    config  = Config()
    network = Network()
    scraper = Scraper(network)
    library = Library(config)
    return config, network, scraper, library

@app.command()
def main(
    target:     str  = typer.Argument(None),
    search:     bool = typer.Option(False, "--search",  "-s",  help="Search for a show"),
    episode:    bool = typer.Option(False, "--episode", "-de", help="Download a single episode URL"),
    series:     bool = typer.Option(False, "--series",  "-ds", help="Download a full series URL"),
    all_series: bool = typer.Option(False, "--all",     "-da", help="Download all series from list file"),
    list_file:  str  = typer.Option(DEFAULT_SERIES_LIST, "--list-file"),
    workers:    int  = typer.Option(6, "--workers", "-w", help="Parallel episode downloads"),
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
    if sum([search, episode, series, all_series]) != 1:
        typer.echo("Specify exactly one mode: -s / -de / -ds / -da")
        raise typer.Exit(1)

    config, network, scraper, library = _init()
    library.cleanup_parts()
    error_log = ErrorLog()

    try:
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
                    print(f"  {path.replace('/anime/', '').replace('-', ' ').title()}")
                    print(f"  https://www.wco.tv{path}\n")

        elif episode:
            if not target:
                typer.echo("--episode requires a URL"); raise typer.Exit(1)
            show, season, ep = parse_episode_meta(target)
            print(f"Downloading: {show} — {season} {ep}")
            success, msg, detail = download_episode(target, network, scraper, config, library)
            print(f"  {msg}")
            if not success and detail:
                error_log.add(f"{show} — {season} {ep}", target, detail)

        elif series:
            if not target:
                typer.echo("--series requires a URL"); raise typer.Exit(1)
            print(f"Series: {parse_series_name(target)}")
            download_series(target, network, scraper, config, library, error_log, workers=workers)

        elif all_series:
            path = pathlib.Path(list_file)
            if not path.is_file():
                typer.echo(f"List file not found: {path}"); raise typer.Exit(1)
            urls = [l.strip() for l in path.read_text("utf-8").splitlines()
                    if l.strip() and not l.startswith("#")]
            if not urls:
                print("Series list is empty."); return
            print(f"Found {len(urls)} series in list.\n")
            if not typer.confirm("Download all? (This may take a very long time)"):
                print("Aborted."); return
            for i, u in enumerate(urls, 1):
                print(f"\n[{i}/{len(urls)}] {u}")
                try:
                    download_series(u, network, scraper, config, library, error_log, workers=workers)
                except Exception as e:
                    print(f"  ✗ Series failed: {e}")
                    error_log.add(u, u, f"Series-level failure: {e}\n{traceback.format_exc()}")

    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
    finally:
        try:
            network.kill_active_processes()
        finally:
            if error_log.has_errors():
                error_log.write("errors.txt")
                error_log.print_summary()

if __name__ == "__main__":
    app()
