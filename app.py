"""Small Flask UI to list Plex music albums and edit album genres.

Configuration (environment variables):
  PLEX_URL                 Base URL of the server, e.g. http://127.0.0.1:32400
  PLEX_TOKEN               X-Plex-Token for an account that can edit the library
  PLEX_SECTION             Optional: exact name of the music library (uses the first music library if unset)
  RYM_HIERARCHY_PATH       Optional: path to RateYourMusic Hierarchy.txt (default: ./data/RateYourMusic_Hierarchy.txt)
  MUSIC_LIBRARY_DISK_PATH  Optional: local filesystem root of the same music library Plex uses. When set, saving
                           genres also writes a single ``genre`` tag to each track file (semicolon-separated) via
                           mutagen. Paths are resolved by matching Plex ``MediaPart.file`` against this library's
                           Plex folder roots, then re-rooting under ``MUSIC_LIBRARY_DISK_PATH``.

When the hierarchy file is loaded, each comma-separated genre you enter is matched (case-insensitive) to the
tree. Recognized names are expanded to include ancestor genres on the album; top-level section labels
(Descriptors, Genres, Scenes & Movements) are omitted. Names not found in the tree are still saved as typed.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from flask import Flask, abort, render_template, request
from markupsafe import escape
from plexapi.exceptions import BadRequest, NotFound, Unauthorized
from plexapi.library import MusicSection
from plexapi.server import PlexServer

from rym_hierarchy import (
    build_datalist_inner_html,
    expand_genre_picks,
    load_nodes,
    rym_genre_names_casefold,
)

log = logging.getLogger(__name__)


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v


@lru_cache(maxsize=1)
def get_server() -> PlexServer:
    base = _env("PLEX_URL")
    token = _env("PLEX_TOKEN")
    if not base or not token:
        raise RuntimeError("Set PLEX_URL and PLEX_TOKEN in the environment.")
    return PlexServer(base.rstrip("/"), token)


def get_music_section() -> MusicSection:
    server = get_server()
    name = _env("PLEX_SECTION")
    sections = [s for s in server.library.sections() if isinstance(s, MusicSection)]
    if not sections:
        raise RuntimeError("No music library found on this Plex server.")
    if name:
        for s in sections:
            if s.title == name:
                return s
        titles = ", ".join(s.title for s in sections)
        raise RuntimeError(f'No music library named "{name}". Available: {titles}')
    return sections[0]


@dataclass
class RymHierarchy:
    nodes: list
    datalist_inner_html: str


_rym: RymHierarchy | None = None
_rym_load_error: str | None = None


def _hierarchy_path() -> Path:
    custom = _env("RYM_HIERARCHY_PATH")
    if custom:
        return Path(custom).expanduser().resolve()
    return Path(__file__).resolve().parent / "data" / "RateYourMusic_Hierarchy.txt"


def get_rym_hierarchy() -> RymHierarchy | None:
    """Load RYM tree once. Returns None if the file is missing (expansion and datalist disabled)."""
    global _rym, _rym_load_error
    if _rym is not None:
        return _rym
    if _rym_load_error is not None:
        return None
    path = _hierarchy_path()
    try:
        nodes = load_nodes(path)
        _rym = RymHierarchy(nodes=nodes, datalist_inner_html=build_datalist_inner_html(nodes))
        return _rym
    except OSError as e:
        _rym_load_error = str(e)
        return None


def album_has_rym_genre_gap(album, rym_cf: set[str]) -> bool:
    genres = album.genres or []
    if not genres:
        return True
    return any(g.tag.casefold() not in rym_cf for g in genres)


def passes_multi_track_filter(album, exclude_single_tracks: bool) -> bool:
    """When enabled, drop albums with only one track (``leafCount`` ≤ 1). Unknown counts are kept."""
    if not exclude_single_tracks:
        return True
    n = getattr(album, "leafCount", None)
    if n is None:
        return True
    return int(n) > 1


_gap_cache_lock = threading.Lock()
_GAP_MATCH_CACHE: dict[str, tuple[list, bool]] = {}


def gap_cache_key(section: MusicSection, exclude_single_tracks: bool) -> str:
    """Invalidate when the hierarchy file on disk changes or single-track filter toggles."""
    try:
        mt = int(_hierarchy_path().stat().st_mtime_ns)
    except OSError:
        mt = 0
    st = int(bool(exclude_single_tracks))
    return f"{section.key}:{mt}:st={st}"


def get_gap_cached(section: MusicSection, exclude_single_tracks: bool) -> tuple[list, bool] | None:
    key = gap_cache_key(section, exclude_single_tracks)
    with _gap_cache_lock:
        hit = _GAP_MATCH_CACHE.get(key)
    return hit


def invalidate_gap_match_cache() -> None:
    with _gap_cache_lock:
        _GAP_MATCH_CACHE.clear()


def fetch_gap_page_slice(
    section: MusicSection,
    album_sort: str,
    rym_cf: set[str],
    page: int,
    per: int,
    exclude_single_tracks: bool,
    *,
    chunk: int = 100,
    max_scan: int = 500_000,
) -> tuple[list, bool, bool]:
    """First-page-friendly scan: collect matches until ``page * per + 1`` or library / cap ends."""
    need = page * per + 1
    matches: list = []
    container_start = 0
    scanned = 0
    while len(matches) < need and scanned < max_scan:
        batch = section.search(
            libtype="album",
            sort=album_sort,
            maxresults=chunk,
            container_start=container_start,
        )
        if not batch:
            break
        for album in batch:
            scanned += 1
            if album_has_rym_genre_gap(album, rym_cf) and passes_multi_track_filter(
                album, exclude_single_tracks
            ):
                matches.append(album)
                if len(matches) >= need:
                    break
        container_start += len(batch)
        if len(matches) >= need:
            break
        if scanned >= max_scan:
            break
        if len(batch) < chunk:
            break
    start_idx = (page - 1) * per
    page_albums = matches[start_idx : start_idx + per]
    has_next = len(matches) > page * per
    hit_cap = scanned >= max_scan
    return page_albums, has_next, hit_cap


def fetch_browse_page_slice(
    section: MusicSection,
    album_sort: str,
    page: int,
    per: int,
    exclude_single_tracks: bool,
    *,
    chunk: int = 100,
    max_scan: int = 500_000,
) -> tuple[list, bool, bool]:
    """Paginated browse; when excluding single-track albums, scan until enough multi-track rows."""
    if not exclude_single_tracks:
        start = (page - 1) * per
        batch = section.search(
            libtype="album",
            sort=album_sort,
            maxresults=per,
            container_start=start,
        )
        return batch, len(batch) == per, False

    need = page * per + 1
    kept: list = []
    container_start = 0
    scanned = 0
    while len(kept) < need and scanned < max_scan:
        batch = section.search(
            libtype="album",
            sort=album_sort,
            maxresults=chunk,
            container_start=container_start,
        )
        if not batch:
            break
        for album in batch:
            scanned += 1
            if passes_multi_track_filter(album, True):
                kept.append(album)
                if len(kept) >= need:
                    break
        container_start += len(batch)
        if len(kept) >= need:
            break
        if scanned >= max_scan:
            break
        if len(batch) < chunk:
            break
    start_idx = (page - 1) * per
    page_albums = kept[start_idx : start_idx + per]
    has_next = len(kept) > page * per
    hit_cap = scanned >= max_scan
    return page_albums, has_next, hit_cap


def fetch_all_gap_matches(
    section: MusicSection,
    album_sort: str,
    rym_cf: set[str],
    exclude_single_tracks: bool,
    *,
    chunk: int = 100,
    max_scan: int = 500_000,
) -> tuple[list, bool]:
    """Scan the whole music library (newest first) and return all albums with no / non‑RYM genres.

    Returns (matches, hit_scan_cap) where hit_scan_cap is True if scanning stopped at ``max_scan``
    before the library ended.
    """
    matches: list = []
    container_start = 0
    scanned = 0
    while scanned < max_scan:
        batch = section.search(
            libtype="album",
            sort=album_sort,
            maxresults=chunk,
            container_start=container_start,
        )
        if not batch:
            break
        for album in batch:
            scanned += 1
            if album_has_rym_genre_gap(album, rym_cf) and passes_multi_track_filter(
                album, exclude_single_tracks
            ):
                matches.append(album)
        container_start += len(batch)
        if len(batch) < chunk:
            break
    hit_cap = scanned >= max_scan
    return matches, hit_cap


def _music_library_disk_root() -> Path | None:
    raw = _env("MUSIC_LIBRARY_DISK_PATH")
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def resolve_plex_audio_file_on_disk(
    plex_file: str,
    plex_library_roots: list[str],
    disk_root: Path,
) -> Path | None:
    """Map Plex ``MediaPart.file`` onto a path under ``MUSIC_LIBRARY_DISK_PATH``.

    Strips the longest matching Plex library folder (from the section's ``locations``), then joins the
    remainder under ``disk_root``. If no root matches but ``plex_file`` exists on this machine, returns that path.
    """
    if not plex_file:
        return None
    disk_root = disk_root.resolve()
    plex_path = Path(plex_file)
    roots = [Path(r).expanduser() for r in plex_library_roots if r]
    roots.sort(key=lambda p: len(p.parts), reverse=True)
    for root in roots:
        try:
            rel = plex_path.relative_to(root)
        except ValueError:
            continue
        return disk_root / rel
    
    if plex_path.is_file():
        return plex_path.resolve()
    return None


def _write_genre_tag_with_mutagen(path: Path, genre_semicolon_separated: str) -> None:
    from mutagen import File as mutagen_file
    from mutagen import MutagenError

    path = path.resolve()
    audio = mutagen_file(str(path), easy=True)
    if audio is None:
        audio = mutagen_file(str(path), easy=False)
    if audio is None:
        log.warning("mutagen: unsupported type, skipping %s", path)
        return
    try:
        audio["genre"] = genre_semicolon_separated
        audio.save()
    except (MutagenError, KeyError, TypeError, ValueError, OSError) as e:
        log.warning("mutagen: could not set genre on %s: %s", path, e)


def sync_album_genres_to_track_files(
    album,
    genre_tags: list[str],
    *,
    disk_root: Path,
    plex_library_roots: list[str],
) -> None:
    """Write the same genre list to each track file under ``disk_root`` (semicolon-separated)."""
    joined = ";".join(dict.fromkeys(genre_tags)) if genre_tags else ""
    # print(f"joined: {joined}")
    album.reload()
    seen: set[Path] = set()
    try:
        tracks = album.tracks()
    except (BadRequest, NotFound, OSError, RuntimeError) as e:
        log.warning("Could not list tracks for album %s: %s", getattr(album, "title", album), e)
        return

    for index, track in enumerate(tracks):
        print(f"processing file track: {track.title} ({index + 1} / {len(tracks)})")
        try:
            if track.isPartialObject() or not getattr(track, "media", None):
                track.reload()
        except (BadRequest, NotFound, OSError, RuntimeError) as e:
            log.warning("Track reload failed (%s): %s", getattr(track, "title", track), e)
            continue
        try:
            locs = track.locations
        except (AttributeError, BadRequest, NotFound, OSError, RuntimeError) as e:
            log.warning("No file locations for track %s: %s", getattr(track, "title", track), e)
            continue
        for plex_file in locs:
            local = resolve_plex_audio_file_on_disk(plex_file, plex_library_roots, disk_root)
            # print(f"local: {local}")
            if local is None or not local.is_file():
                log.warning(
                    "Skipping missing or unmapped file for track %s (plex file=%r)",
                    getattr(track, "title", track),
                    plex_file,
                )
                continue
            if local in seen:
                continue
            seen.add(local)
            _write_genre_tag_with_mutagen(local, joined)


def set_album_genres(album, genre_csv: str, rym: RymHierarchy | None) -> list[str]:
    """Apply genres to Plex. Returns list of input tokens that were not found in the RYM tree (may be empty)."""
    picks = [s.strip() for s in genre_csv.split(",") if s.strip()]

    if rym is not None:
        tags, unknown = expand_genre_picks(picks, rym.nodes)
        tags.reverse()
    else:
        tags = picks
        unknown = []

    album.reload()

    album.batchEdits()
    for g in list(album.genres or []):
        album.removeGenre(g.tag)
        
    album.saveEdits()
    album.reload()

    album.addStyle(tags)
    album.addGenre(tags)

    album.reload()

    for track in album.tracks():
        print(f"processing Plex track: {track.title} ({track.trackNumber} / {album.leafCount})")
        track.reload()
        track.batchEdits()
        for g in list(track.genres or []):
            track.removeGenre(g.tag)
        track.saveEdits()
        track.reload()
        track.addGenre(tags)

    disk = _music_library_disk_root()
    
    if disk is not None:
        try:
            section = album._server.library.sectionByID(album.librarySectionID)
            sync_album_genres_to_track_files(
                album,
                tags,
                disk_root=disk,
                plex_library_roots=list(section.locations),
            )
        except (BadRequest, NotFound, OSError, RuntimeError, AttributeError) as e:
            log.warning("Could not sync genres to audio files on disk: %s", e)

    print(f"Done: {album.title}")
    return unknown


def create_app() -> Flask:
    app = Flask(__name__)

    @app.context_processor
    def inject_rym_datalist():
        rym = get_rym_hierarchy()
        return {"rym_genre_options_html": rym.datalist_inner_html if rym else ""}

    @app.route("/")
    def index():
        try:
            section = get_music_section()
        except RuntimeError as e:
            return render_template("error.html", message=str(e)), 503

        q = (request.args.get("q") or "").strip()
        rym_gaps = request.args.get("rym_gaps") in ("1", "on", "true", "yes")
        exclude_single_tracks = request.args.get("no_singleton") in ("1", "on", "true", "yes")
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        per = 40
        start = (page - 1) * per
        album_sort = "addedAt:desc"
        hub_limit = 500

        rym = get_rym_hierarchy()
        rym_cf = rym_genre_names_casefold(rym.nodes) if rym else None
        gap_effective = bool(rym_gaps and rym_cf is not None)
        gap_unavailable = bool(rym_gaps and rym_cf is None)

        try:
            library_album_total = section.totalViewSize(libtype="album")
        except (BadRequest, NotFound, OSError, RuntimeError):
            library_album_total = None

        scan_hit_cap = False
        filter_match_total: int | None = None
        gap_count_pending = False
        try:
            if q:
                all_found = section.hubSearch(q, mediatype="album", limit=hub_limit)
                if gap_effective:
                    all_found = [a for a in all_found if album_has_rym_genre_gap(a, rym_cf)]
                if exclude_single_tracks:
                    all_found = [a for a in all_found if passes_multi_track_filter(a, True)]
                if gap_effective or exclude_single_tracks:
                    filter_match_total = len(all_found)
                albums = all_found[start : start + per]
                pager_has_next = start + per < len(all_found)
            elif gap_effective:
                cached = get_gap_cached(section, exclude_single_tracks)
                if cached is not None:
                    all_matches, scan_hit_cap = cached
                    filter_match_total = len(all_matches)
                    albums = all_matches[start : start + per]
                    pager_has_next = start + per < len(all_matches)
                else:
                    albums, pager_has_next, scan_hit_cap = fetch_gap_page_slice(
                        section,
                        album_sort,
                        rym_cf,
                        page,
                        per,
                        exclude_single_tracks,
                    )
                    filter_match_total = None
                    gap_count_pending = True
            else:
                albums, pager_has_next, scan_hit_cap = fetch_browse_page_slice(
                    section, album_sort, page, per, exclude_single_tracks
                )
        except (BadRequest, NotFound) as e:
            return (
                render_template(
                    "index.html",
                    section_title=section.title,
                    albums=[],
                    q=q,
                    page=page,
                    per_page=per,
                    rym_gaps=rym_gaps,
                    exclude_single_tracks=exclude_single_tracks,
                    gap_unavailable=gap_unavailable,
                    pager_has_next=False,
                    library_album_total=library_album_total,
                    scan_hit_cap=False,
                    gap_effective=gap_effective,
                    filter_match_total=None,
                    gap_count_pending=False,
                    error=str(e),
                ),
                400,
            )
        except Unauthorized:
            return render_template("error.html", message="Plex rejected the token (401)."), 401

        # STOP DELETING THIS LOOP. It's a hack to get the Plex library to refresh the metadata for the item.
        for album in albums:
            album.reload()

        return render_template(
            "index.html",
            section_title=section.title,
            albums=albums,
            q=q,
            page=page,
            per_page=per,
            rym_gaps=rym_gaps,
            exclude_single_tracks=exclude_single_tracks,
            gap_unavailable=gap_unavailable,
            gap_effective=gap_effective,
            gap_count_pending=gap_count_pending,
            pager_has_next=pager_has_next,
            library_album_total=library_album_total,
            scan_hit_cap=scan_hit_cap,
            filter_match_total=filter_match_total,
            error=None,
        )

    @app.post("/album/<int:rating_key>/genre")
    def update_genre(rating_key: int):
        genre = request.form.get("genre", "")
        try:
            section = get_music_section()
        except RuntimeError as e:
            return (
                f'<tr id="album-{rating_key}"><td colspan="3" class="row-error">{escape(str(e))}</td></tr>',
                503,
            )

        try:
            album = section.fetchItem(rating_key)
        except NotFound:
            abort(404)

        rym = get_rym_hierarchy()
        try:
            unknown = set_album_genres(album, genre, rym)
            album.reload()
        except Exception as e:  # noqa: BLE001 — surface Plex errors to the row
            print(f"error: {e}")
            try:
                album.reload()
            except Exception:
                print(f"error2: {e}")
                pass
            chip_tags = [s.strip() for s in genre.split(",") if s.strip()]
            return (
                render_template(
                    "partial_album_row.html",
                    album=album,
                    chip_tags=chip_tags,
                    error=str(e),
                    genre_unknown_note=None,
                ),
                400,
            )

        note = None
        if unknown:
            note = "Not in RYM hierarchy (saved as typed): " + ", ".join(unknown)

        invalidate_gap_match_cache()

        return render_template(
            "partial_album_row.html",
            album=album,
            error=None,
            genre_unknown_note=note,
        )

    @app.get("/partial/gap-match-count")
    def gap_match_count():
        """Full-library gap scan; fills server cache. Loaded async via HTMX so / does not block."""
        try:
            section = get_music_section()
        except RuntimeError as e:
            return escape(str(e)), 503

        rym = get_rym_hierarchy()
        rym_cf = rym_genre_names_casefold(rym.nodes) if rym else None
        if not rym_cf:
            return "—", 200

        exclude_single_tracks = request.args.get("no_singleton") in ("1", "on", "true", "yes")
        key = gap_cache_key(section, exclude_single_tracks)
        hit = get_gap_cached(section, exclude_single_tracks)
        if hit is not None:
            matches, hit_cap = hit
            return render_template(
                "partial_gap_count_inner.html",
                total=len(matches),
                scan_hit_cap=hit_cap,
            )

        try:
            matches, hit_cap = fetch_all_gap_matches(
                section, "addedAt:desc", rym_cf, exclude_single_tracks
            )
        except (BadRequest, NotFound) as e:
            return escape(str(e)), 400

        with _gap_cache_lock:
            if gap_cache_key(section, exclude_single_tracks) == key:
                _GAP_MATCH_CACHE[key] = (matches, hit_cap)
        return render_template(
            "partial_gap_count_inner.html",
            total=len(matches),
            scan_hit_cap=hit_cap,
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(_env("PORT", "5000")), debug=True)
