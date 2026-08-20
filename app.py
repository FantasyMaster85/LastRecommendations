from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template, request

LASTFM_URL = "https://www.last.fm/home/artists"
LASTFM_BASE_URL = "https://www.last.fm"
MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"

PORT = int(os.getenv("PORT", "9654"))
REFRESH_HOURS = float(os.getenv("REFRESH_HOURS", "25"))
REFRESH_SECONDS = REFRESH_HOURS * 60 * 60

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
STATE_FILE = DATA_DIR / "state.json"
DEBUG_HTML_FILE = DATA_DIR / "lastfm-debug.html"

DEFAULT_LASTFM_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# MusicBrainz requires <= 1 request/sec per application/IP.
DEFAULT_MB_UA = "LastfmLidarrRecommendations/1.0 (self-hosted personal application)"
MB_MIN_INTERVAL = 1.10

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$"
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("lastrecommendations")

app = Flask(__name__)

state_lock = threading.RLock()
refresh_lock = threading.Lock()
stop_event = threading.Event()

mb_rate_lock = threading.Lock()
mb_last_request = 0.0


@dataclass
class Artist:
    name: str
    url: str
    image: str | None = None
    first_seen: str | None = None
    mbid: str | None = None
    mbid_method: str | None = None
    mbid_error: str | None = None


def utc_iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def blank_state() -> dict:
    return {
        "artists": [],
        "seen": {},
        "history": {},
        "mbid_cache": {},
        "last_success": None,
        "last_attempt": None,
        "last_error": None,
        "source_url": LASTFM_URL,
        "musicbrainz": {
            "last_ok": None,
            "last_error": None,
            "last_status": None,
        },
    }


def migrate_history(loaded: dict) -> None:
    """
    Add the persistent all-time history structure to state files created by
    older versions. To avoid importing stale data produced by older scraper
    behavior, migration seeds history only from the currently stored, already
    filtered recommendation set. Future refreshes then preserve every artist.
    """
    history = loaded.setdefault("history", {})
    seen = loaded.get("seen", {}) or {}
    mbid_cache = loaded.get("mbid_cache", {}) or {}

    for current in (loaded.get("artists", []) or []):
        url = current.get("url")
        if not url:
            continue

        seen_entry = seen.get(url, {}) or {}
        cached = mbid_cache.get(url, {}) or {}
        first_seen = (
            current.get("first_seen")
            or seen_entry.get("first_seen")
            or loaded.get("last_success")
        )

        history.setdefault(
            url,
            {
                "name": current.get("name") or seen_entry.get("name") or url,
                "url": url,
                "image": current.get("image"),
                "first_seen": first_seen,
                "last_seen": loaded.get("last_success") or first_seen,
                "times_seen": 1,
                "mbid": current.get("mbid") or cached.get("mbid"),
                "mbid_method": current.get("mbid_method") or cached.get("method"),
                "mbid_error": current.get("mbid_error") or cached.get("error"),
            },
        )


def load_state() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        return blank_state()

    try:
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        defaults = blank_state()
        for key, value in defaults.items():
            loaded.setdefault(key, value)
        loaded.setdefault("musicbrainz", defaults["musicbrainz"])
        migrate_history(loaded)
        return loaded
    except Exception:
        log.exception("Could not load state file; starting with empty state.")
        value = blank_state()
        value["last_error"] = "Existing state.json could not be parsed."
        return value


state = load_state()


def save_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")

    with state_lock:
        tmp.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(STATE_FILE)


def is_artist_url(url: str) -> bool:
    parsed = urlparse(urljoin(LASTFM_BASE_URL, url))

    if parsed.netloc not in {"www.last.fm", "last.fm"}:
        return False

    path = parsed.path.rstrip("/")
    if not path.startswith("/music/"):
        return False

    rest = path[len("/music/"):]
    if not rest or "/" in rest or rest.startswith("+"):
        return False

    return True


def artist_name_from_url(url: str) -> str:
    parsed = urlparse(urljoin(LASTFM_BASE_URL, url))
    slug = parsed.path.rstrip("/").split("/")[-1]
    return unquote(slug.replace("+", " ")).strip()


def canonical_lastfm_artist_url(url: str) -> str:
    parsed = urlparse(urljoin(LASTFM_BASE_URL, url))
    return f"https://www.last.fm{parsed.path.rstrip('/')}"


def find_recommendation_image(anchor) -> str | None:
    """
    Find an image only from the same recommendation card as the title anchor.
    """
    card = (
        anchor.find_parent("li")
        or anchor.find_parent(class_="recs-feed-item")
        or anchor.find_parent(class_="recs-feed-cover")
    )

    img = None
    if card:
        img = (
            card.select_one(".recs-feed-cover-image img")
            or card.select_one("img")
        )

    if not img:
        # Conservative fallback around the title itself.
        parent = anchor.parent
        for _ in range(4):
            if not parent:
                break
            img = parent.find("img")
            if img:
                break
            parent = parent.parent

    if not img:
        return None

    for attr in ("src", "data-src", "data-original"):
        value = img.get(attr)
        if value and not value.startswith("data:"):
            return urljoin(LASTFM_BASE_URL, value)

    srcset = img.get("srcset")
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        if first and not first.startswith("data:"):
            return urljoin(LASTFM_BASE_URL, first)

    return None


def extract_artists(document: str) -> list[Artist]:
    """
    Extract ONLY the actual recommendation title from each Last.fm card.

    Last.fm recommendation cards also contain links under text such as:
        Similar to Broken Bells, Sir Sly and Miike Snow

    Those are intentionally excluded.

    Current Last.fm markup identifies the actual recommended artist title with:
        data-analytics-action="ProfileRecArtistTitle"

    We also retain a narrow structural fallback:
        h3.recs-feed-title > a
    """
    soup = BeautifulSoup(document, "html.parser")
    main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup

    anchors = main.select(
        'a[data-analytics-action="ProfileRecArtistTitle"][href]'
    )

    if not anchors:
        anchors = main.select(
            "h3.recs-feed-title > a[href]"
        )

    found: dict[str, Artist] = {}

    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        absolute = urljoin(LASTFM_BASE_URL, href)

        if not is_artist_url(absolute):
            continue

        canonical = canonical_lastfm_artist_url(absolute)
        name = " ".join(anchor.stripped_strings).strip()

        if not name:
            name = artist_name_from_url(canonical)

        if not name or len(name) > 200:
            continue

        if canonical not in found:
            found[canonical] = Artist(
                name=name,
                url=canonical,
                image=find_recommendation_image(anchor),
            )

    return list(found.values())


def cookie_dict(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}

    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue

        name, value = part.split("=", 1)
        cookies[name.strip()] = value.strip()

    return cookies


def fetch_recommendations() -> list[Artist]:
    cookie_header = os.getenv("LASTFM_COOKIE", "").strip()

    if not cookie_header:
        raise RuntimeError(
            "LASTFM_COOKIE is not configured in docker-compose.yml."
        )

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": os.getenv("LASTFM_USER_AGENT", DEFAULT_LASTFM_UA),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://www.last.fm/",
        }
    )
    session.cookies.update(cookie_dict(cookie_header))

    response = session.get(
        LASTFM_URL,
        timeout=45,
        allow_redirects=True,
    )
    response.raise_for_status()

    DEBUG_HTML_FILE.write_text(response.text, encoding="utf-8")

    final_path = urlparse(response.url).path.rstrip("/")
    if final_path != "/home/artists":
        raise RuntimeError(
            f"Last.fm redirected the authenticated request to {response.url!r}. "
            "The session cookie is probably expired or invalid."
        )

    artists = extract_artists(response.text)

    if not artists:
        raise RuntimeError(
            "The Last.fm page loaded, but no recommendation-title links were found. "
            "Expected data-analytics-action='ProfileRecArtistTitle' or "
            "h3.recs-feed-title > a. Inspect data/lastfm-debug.html."
        )

    return artists


def valid_mbid(value: object) -> bool:
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    return " ".join(value.casefold().split())


def mb_score(candidate: dict) -> int:
    try:
        return int(candidate.get("score", 0))
    except (TypeError, ValueError):
        return 0


def set_mb_status(ok: bool, status: int | None = None, error: str | None = None) -> None:
    with state_lock:
        info = state.setdefault("musicbrainz", {})
        info["last_status"] = status

        if ok:
            info["last_ok"] = utc_iso_now()
            info["last_error"] = None
        else:
            info["last_error"] = error


def musicbrainz_get(path: str, params=None, allow_404: bool = False) -> dict:
    """
    Rate-limited MusicBrainz API GET.

    If MusicBrainz rejects or cannot service a request, preserve the actual
    HTTP status/error text so the dashboard can show why resolution failed.
    """
    global mb_last_request

    headers = {
        "User-Agent": os.getenv("MUSICBRAINZ_USER_AGENT", DEFAULT_MB_UA),
        "Accept": "application/json",
    }

    with mb_rate_lock:
        elapsed = time.monotonic() - mb_last_request
        if elapsed < MB_MIN_INTERVAL:
            time.sleep(MB_MIN_INTERVAL - elapsed)

        last_error = None

        for attempt in range(3):
            try:
                response = requests.get(
                    f"{MUSICBRAINZ_API}{path}",
                    params=params,
                    headers=headers,
                    timeout=30,
                )
                mb_last_request = time.monotonic()

                if response.status_code == 404 and allow_404:
                    set_mb_status(True, 404, None)
                    return {}

                if response.status_code in {429, 503} and attempt < 2:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = max(float(retry_after), 2.0)
                    except (TypeError, ValueError):
                        delay = 2.5 * (attempt + 1)

                    log.warning(
                        "MusicBrainz returned HTTP %s; retrying in %.1f seconds.",
                        response.status_code,
                        delay,
                    )
                    time.sleep(delay)
                    continue

                if not response.ok:
                    body = " ".join(response.text.split())[:300]
                    message = (
                        f"MusicBrainz HTTP {response.status_code}: "
                        f"{body or response.reason}"
                    )
                    set_mb_status(False, response.status_code, message)
                    raise RuntimeError(message)

                payload = response.json()
                set_mb_status(True, response.status_code, None)
                return payload

            except requests.RequestException as exc:
                last_error = f"MusicBrainz network error: {exc}"
                set_mb_status(False, None, last_error)
            except ValueError as exc:
                last_error = f"MusicBrainz returned invalid JSON: {exc}"
                set_mb_status(False, None, last_error)
            except RuntimeError as exc:
                last_error = str(exc)

            mb_last_request = time.monotonic()

            if attempt < 2:
                time.sleep(2.5 * (attempt + 1))

        raise RuntimeError(last_error or "Unknown MusicBrainz request failure")


def lucene_escape(value: str) -> str:
    # Escape Lucene special characters inside a quoted value.
    value = value or ""
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    return value


def candidate_names(candidate: dict) -> set[str]:
    names = set()

    name = candidate.get("name")
    if isinstance(name, str):
        names.add(normalize_name(name))

    for alias in candidate.get("aliases", []) or []:
        if isinstance(alias, dict):
            alias_name = alias.get("name")
            if isinstance(alias_name, str):
                names.add(normalize_name(alias_name))

    return names


def search_musicbrainz_artist(name: str) -> list[dict]:
    """
    Search MusicBrainz using the documented artist search endpoint.

    We use an exact quoted artist field first. Search results include the MBID
    in each artist object's `id` field.
    """
    query = f'artist:"{lucene_escape(name)}"'

    payload = musicbrainz_get(
        "/artist/",
        params={
            "query": query,
            "limit": 25,
            "fmt": "json",
        },
    )

    return [
        candidate
        for candidate in (payload.get("artists", []) or [])
        if valid_mbid(candidate.get("id"))
    ]


def resolve_lastfm_url_relationship(lastfm_url: str) -> dict | None:
    """
    Ask MusicBrainz whether the exact Last.fm artist URL is linked to an artist.

    This is most useful for disambiguating same-name artists. MusicBrainz does
    not have Last.fm relationships for every artist, so absence is not failure.
    """
    payload = musicbrainz_get(
        "/url",
        params={
            "resource": lastfm_url,
            "inc": "artist-rels",
            "fmt": "json",
        },
        allow_404=True,
    )

    if not payload:
        return None

    matches: dict[str, dict] = {}

    for relation in payload.get("relations", []) or []:
        artist = relation.get("artist")
        if not isinstance(artist, dict):
            continue

        mbid = artist.get("id")
        if valid_mbid(mbid):
            matches[mbid.lower()] = {
                "mbid": mbid.lower(),
                "name": artist.get("name"),
            }

    if len(matches) == 1:
        return next(iter(matches.values()))

    return None


def choose_search_candidate(name: str, candidates: list[dict]) -> tuple[dict | None, str | None]:
    """
    Pick a MusicBrainz result without requiring Last.fm to have a relationship.

    A unique exact canonical-name match is accepted immediately. If canonical
    names do not give a unique answer, a unique exact alias match is accepted.
    Multiple exact matches remain ambiguous rather than guessing.
    """
    wanted = normalize_name(name)

    canonical = [
        c for c in candidates
        if normalize_name(c.get("name", "")) == wanted
    ]

    if canonical:
        best_score = max(mb_score(c) for c in canonical)
        best = [c for c in canonical if mb_score(c) == best_score]

        if len(best) == 1:
            return best[0], None

        return None, (
            f"MusicBrainz has {len(best)} equally ranked exact-name artists "
            f"named {name!r}"
        )

    alias_matches = [
        c for c in candidates
        if wanted in candidate_names(c)
    ]

    if alias_matches:
        best_score = max(mb_score(c) for c in alias_matches)
        best = [c for c in alias_matches if mb_score(c) == best_score]

        if len(best) == 1:
            return best[0], None

        return None, (
            f"MusicBrainz has {len(best)} equally ranked exact alias matches "
            f"for {name!r}"
        )

    return None, f"No exact MusicBrainz artist-name match found for {name!r}"


def cache_resolution(
    url: str,
    name: str,
    mbid: str | None,
    matched_name: str | None,
    method: str | None,
    error: str | None,
) -> None:
    with state_lock:
        cache = state.setdefault("mbid_cache", {})
        cache[url] = {
            "name": name,
            "mbid": mbid,
            "matched_name": matched_name,
            "method": method,
            "last_attempt": utc_iso_now(),
            "resolved_at": utc_iso_now() if mbid else None,
            "error": error,
        }


def resolve_one_artist(artist: Artist) -> None:
    """
    Resolve one recommended Last.fm artist to a MusicBrainz Artist MBID.

    Strategy:
      1. Search MusicBrainz by the recommendation's artist name.
      2. If there is a unique exact name/alias result, use its `id`.
      3. If the name is ambiguous, ask MusicBrainz about the exact Last.fm URL.
      4. Never guess if ambiguity remains.
    """
    try:
        candidates = search_musicbrainz_artist(artist.name)
        chosen, search_error = choose_search_candidate(artist.name, candidates)

        if chosen:
            artist.mbid = chosen["id"].lower()
            artist.mbid_method = "musicbrainz-name"
            artist.mbid_error = None

            cache_resolution(
                artist.url,
                artist.name,
                artist.mbid,
                chosen.get("name"),
                artist.mbid_method,
                None,
            )
            return

        # Same-name ambiguity is where the exact Last.fm URL relationship is
        # most valuable.
        relationship = resolve_lastfm_url_relationship(artist.url)

        if relationship and valid_mbid(relationship.get("mbid")):
            artist.mbid = relationship["mbid"].lower()
            artist.mbid_method = "lastfm-url"
            artist.mbid_error = None

            cache_resolution(
                artist.url,
                artist.name,
                artist.mbid,
                relationship.get("name"),
                artist.mbid_method,
                None,
            )
            return

        artist.mbid = None
        artist.mbid_method = None
        artist.mbid_error = search_error or "MusicBrainz match remained ambiguous"

        cache_resolution(
            artist.url,
            artist.name,
            None,
            None,
            None,
            artist.mbid_error,
        )

    except Exception as exc:
        artist.mbid = None
        artist.mbid_method = None
        artist.mbid_error = str(exc)

        cache_resolution(
            artist.url,
            artist.name,
            None,
            None,
            None,
            artist.mbid_error,
        )

        log.warning(
            "MusicBrainz lookup failed for %s: %s",
            artist.name,
            exc,
        )


def resolve_mbids(artists: list[Artist]) -> None:
    with state_lock:
        cache = state.setdefault("mbid_cache", {})

    for artist in artists:
        cached = cache.get(artist.url, {})

        # Successful mappings are stable and reusable.
        if valid_mbid(cached.get("mbid")):
            artist.mbid = cached["mbid"].lower()
            artist.mbid_method = cached.get("method") or "cache"
            artist.mbid_error = None
            continue

        # Failed/ambiguous mappings are retried on each 25-hour refresh.
        resolve_one_artist(artist)


def refresh() -> dict:
    if not refresh_lock.acquire(blocking=False):
        return {
            "ok": False,
            "message": "A refresh is already running.",
        }

    try:
        attempted = utc_iso_now()

        with state_lock:
            state["last_attempt"] = attempted
            state["last_error"] = None

        save_state()

        artists = fetch_recommendations()

        log.info(
            "Last.fm scrape found %d actual recommendation title(s).",
            len(artists),
        )

        resolve_mbids(artists)

        now = utc_iso_now()

        with state_lock:
            seen = state.setdefault("seen", {})
            history = state.setdefault("history", {})
            previous_urls = {
                a.get("url")
                for a in state.get("artists", [])
            }

            current = []

            for artist in artists:
                if artist.url not in seen:
                    seen[artist.url] = {
                        "name": artist.name,
                        "first_seen": now,
                    }
                else:
                    seen[artist.url]["name"] = artist.name

                artist.first_seen = seen[artist.url]["first_seen"]
                artist_data = asdict(artist)
                current.append(artist_data)

                history_entry = history.get(artist.url)
                if history_entry is None:
                    history_entry = {
                        "name": artist.name,
                        "url": artist.url,
                        "image": artist.image,
                        "first_seen": artist.first_seen,
                        "last_seen": now,
                        "times_seen": 1,
                        "mbid": artist.mbid,
                        "mbid_method": artist.mbid_method,
                        "mbid_error": artist.mbid_error,
                    }
                    history[artist.url] = history_entry
                else:
                    history_entry["name"] = artist.name
                    history_entry["url"] = artist.url
                    history_entry["image"] = artist.image or history_entry.get("image")
                    history_entry["first_seen"] = (
                        history_entry.get("first_seen") or artist.first_seen
                    )
                    history_entry["last_seen"] = now
                    history_entry["times_seen"] = int(
                        history_entry.get("times_seen", 0) or 0
                    ) + 1
                    history_entry["mbid"] = artist.mbid or history_entry.get("mbid")
                    history_entry["mbid_method"] = (
                        artist.mbid_method or history_entry.get("mbid_method")
                    )
                    history_entry["mbid_error"] = artist.mbid_error

            state["artists"] = current
            state["last_success"] = now
            state["last_error"] = None

            current_urls = {a["url"] for a in current}
            added = current_urls - previous_urls
            removed = previous_urls - current_urls
            resolved_count = sum(
                1 for a in current if valid_mbid(a.get("mbid"))
            )

        save_state()

        log.info(
            "Refresh complete: %d actual Last.fm recommendations, "
            "%d MBIDs resolved (%d new, %d removed).",
            len(artists),
            resolved_count,
            len(added),
            len(removed),
        )

        return {
            "ok": True,
            "artists": len(artists),
            "resolved": resolved_count,
            "unresolved": len(artists) - resolved_count,
            "history_total": len(state.get("history", {})),
            "added": len(added),
            "removed": len(removed),
        }

    except Exception as exc:
        log.exception("Refresh failed.")

        with state_lock:
            state["last_error"] = str(exc)

        save_state()

        return {
            "ok": False,
            "message": str(exc),
        }

    finally:
        refresh_lock.release()


def current_mbids() -> list[str]:
    with state_lock:
        values = [
            artist.get("mbid")
            for artist in state.get("artists", [])
            if valid_mbid(artist.get("mbid"))
        ]

    return list(dict.fromkeys(mbid.lower() for mbid in values))


def lidarr_payload() -> list[dict[str, str]]:
    return [
        {"MusicBrainzId": mbid}
        for mbid in current_mbids()
    ]


@app.get("/")
def index():
    with state_lock:
        snapshot = json.loads(json.dumps(state))

    artists = snapshot.get("artists", [])
    resolved = sum(
        1 for artist in artists
        if valid_mbid(artist.get("mbid"))
    )

    return render_template(
        "index.html",
        artists=artists,
        resolved=resolved,
        unresolved=len(artists) - resolved,
        last_success=snapshot.get("last_success"),
        last_attempt=snapshot.get("last_attempt"),
        last_error=snapshot.get("last_error"),
        musicbrainz=snapshot.get("musicbrainz", {}),
        history_total=len(snapshot.get("history", {})),
        refresh_hours=REFRESH_HOURS,
    )


@app.get("/history")
def all_history():
    with state_lock:
        snapshot = json.loads(json.dumps(state))

    history_items = list(snapshot.get("history", {}).values())
    history_items.sort(
        key=lambda artist: artist.get("first_seen") or "",
        reverse=True,
    )

    per_page_options = [10, 20, 50, 100]
    try:
        per_page = int(request.args.get("per_page", "20"))
    except (TypeError, ValueError):
        per_page = 20

    if per_page not in per_page_options:
        per_page = 20

    try:
        page = max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        page = 1

    total = len(history_items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    end = start + per_page

    return render_template(
        "history.html",
        artists=history_items[start:end],
        total=total,
        page=page,
        total_pages=total_pages,
        per_page=per_page,
        per_page_options=per_page_options,
        last_success=snapshot.get("last_success"),
        refresh_hours=REFRESH_HOURS,
    )


@app.get("/history.json")
def history_json():
    with state_lock:
        history_items = json.loads(
            json.dumps(list(state.get("history", {}).values()))
        )

    history_items.sort(
        key=lambda artist: artist.get("first_seen") or "",
        reverse=True,
    )
    return jsonify(history_items)


@app.get("/lidarr.json")
def lidarr_list():
    body = json.dumps(
        lidarr_payload(),
        indent=2,
        ensure_ascii=False,
    ) + "\n"

    return Response(
        body,
        mimetype="application/json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/mbids.json")
def bare_mbid_list():
    body = json.dumps(
        current_mbids(),
        indent=2,
        ensure_ascii=False,
    ) + "\n"

    return Response(
        body,
        mimetype="application/json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/details.json")
def details():
    with state_lock:
        artists = json.loads(json.dumps(state.get("artists", [])))

    return jsonify(artists)


@app.get("/healthz")
def health():
    with state_lock:
        artists = state.get("artists", [])
        resolved = sum(
            1 for artist in artists
            if valid_mbid(artist.get("mbid"))
        )

        ok = bool(state.get("last_success")) and not state.get("last_error")

        payload = {
            "ok": ok,
            "artists": len(artists),
            "resolved": resolved,
            "unresolved": len(artists) - resolved,
            "history_total": len(state.get("history", {})),
            "last_success": state.get("last_success"),
            "last_attempt": state.get("last_attempt"),
            "last_error": state.get("last_error"),
            "musicbrainz": state.get("musicbrainz", {}),
        }

    return jsonify(payload), (200 if ok else 503)


@app.get("/musicbrainz-test")
def musicbrainz_test():
    """
    Read-only diagnostic endpoint:
      /musicbrainz-test?artist=Electric%20Guest

    It returns the top MusicBrainz search candidates so a failed resolver can
    be diagnosed without shelling into the container.
    """
    artist_name = (request.args.get("artist") or "Electric Guest").strip()

    try:
        candidates = search_musicbrainz_artist(artist_name)
        result = [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "score": mb_score(c),
                "disambiguation": c.get("disambiguation"),
                "type": c.get("type"),
                "country": c.get("country"),
            }
            for c in candidates[:10]
        ]

        return jsonify(
            {
                "ok": True,
                "query": artist_name,
                "results": result,
            }
        )
    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "query": artist_name,
                "error": str(exc),
            }
        ), 502


@app.post("/refresh")
def manual_refresh():
    result = refresh()
    return jsonify(result), (200 if result.get("ok") else 500)


def scheduler_loop() -> None:
    refresh()

    while not stop_event.wait(REFRESH_SECONDS):
        refresh()


if __name__ == "__main__":
    worker = threading.Thread(
        target=scheduler_loop,
        name="refresh-scheduler",
        daemon=True,
    )
    worker.start()

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
    )
