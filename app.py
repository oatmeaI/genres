"""Small Flask UI to list Plex music albums and edit album genres.

Configuration (environment variables):
  PLEX_URL                 Base URL of the server, e.g. http://127.0.0.1:32400
  PLEX_TOKEN               X-Plex-Token for an account that can edit the library
  PLEX_SECTION             Optional: exact name of the music library (uses the first music library if unset)
  RYM_HIERARCHY_PATH       Optional: path to RateYourMusic Hierarchy.txt (default: ./data/RateYourMusic_Hierarchy.txt)
  GENRES_YAML_PATH         Optional: path to custom genres.yaml (default: ./data/genres.yaml)
  MUSIC_LIBRARY_DISK_PATH  Optional: local filesystem root of the same music library Plex uses. When set, saving
                           genres also writes a single ``genre`` tag to each track file (semicolon-separated) via
                           mutagen. Paths are resolved by matching Plex ``MediaPart.file`` against this library's
                           Plex folder roots, then re-rooting under ``MUSIC_LIBRARY_DISK_PATH``.

The UI can switch between the RYM tree and genres.yaml (``?genre_src=custom``). RYM expands ancestor genres;
genres.yaml expands each entry's ``related`` list. Names not found in the active list are still saved as typed.
"""

from __future__ import annotations

import logging
from copy import copy
from flask import Flask, abort, redirect, render_template, request, url_for
from lastfm import get_album_tags
from markupsafe import escape
from plexapi.exceptions import BadRequest, NotFound

from files import sync_album_genres_to_track_files, music_library_root
from plex import set_album_genres, set_tracks_genres, titlecase_tracks
from genres_yaml import (
    GenreEntry,
    GenresYaml,
    add_genre_entry,
    get_genres_yaml,
    parse_genre_list_field,
    reload_genres_yaml,
)
from rym import RymHierarchy, get_rym_hierarchy
from env import env
from util import form_bool, timer
from plex_server import get_music_section
from search import (
    fetch_browse_page_slice,
    AlbumCache,
    album_has_rym_genre_gap,
    passes_multi_track_filter,
    plex_album_sort,
    resolve_album_sort,
    sort_albums,
)

log = logging.getLogger(__name__)

GENRE_SOURCE_RYM = "rym"
GENRE_SOURCE_CUSTOM = "custom"


def resolve_genre_source(value: str | None) -> str:
    return GENRE_SOURCE_CUSTOM if value == GENRE_SOURCE_CUSTOM else GENRE_SOURCE_RYM


def get_genre_hierarchy(source: str) -> RymHierarchy | GenresYaml | None:
    if source == GENRE_SOURCE_CUSTOM:
        return get_genres_yaml()
    return get_rym_hierarchy()


def create_app() -> Flask:
    app = Flask(__name__)
    section = get_music_section()
    album_cache = AlbumCache(section, "lastViewedAt:desc")
    album_cache.load()

    @app.context_processor
    def inject_genre_datalist():
        src = resolve_genre_source(request.args.get("genre_src") if request else None)
        hierarchy = get_genre_hierarchy(src)
        return {
            "genre_hints_json": hierarchy.genre_hints_json if hierarchy else "[]",
            "genre_src": src,
        }

    @app.post("/genres")
    def create_genre():
        name = (request.form.get("name") or "").strip()
        examples = parse_genre_list_field(request.form.get("examples") or "")
        related = parse_genre_list_field(request.form.get("related") or "")

        try:
            add_genre_entry(GenreEntry(name=name, examples=tuple(examples), related=tuple(related)))
        except ValueError as e:
            return redirect(
                url_for(
                    "index",
                    genre_src=GENRE_SOURCE_CUSTOM,
                    genre_create_error=str(e),
                )
            )
        except OSError as e:
            log.exception("Failed to write genres.yaml")
            return redirect(
                url_for(
                    "index",
                    genre_src=GENRE_SOURCE_CUSTOM,
                    genre_create_error=f"Could not write genres.yaml: {e}",
                )
            )

        return redirect(
            url_for(
                "index",
                genre_src=GENRE_SOURCE_CUSTOM,
                genre_added=name,
            )
        )

    @app.route("/")
    def index():
        timer.time("route")

        try:
            section = get_music_section()
        except RuntimeError as e:
            return render_template("error.html", message=str(e)), 503

        genre_src = resolve_genre_source(request.args.get("genre_src"))
        genres_reloaded = False
        if request.args.get("reload_genres") in ("1", "on", "true", "yes"):
            reload_genres_yaml()
            genres_reloaded = True
        hierarchy = get_genre_hierarchy(genre_src)
        if hierarchy is None:
            label = (
                "genres.yaml" if genre_src == GENRE_SOURCE_CUSTOM else "RYM hierarchy"
            )
            return render_template(
                "error.html",
                message=f"Could not load {label}.",
            ), 503

        rym_gaps = request.args.get("rym_gaps") in ("1", "on", "true", "yes")
        exclude_single_tracks = request.args.get("no_singleton") in (
            "1",
            "on",
            "true",
            "yes",
        )
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1

        force_reload = request.args.get("reload") in (
            "1",
            "on",
            "true",
            "yes",
        )

        if force_reload:
            album_cache.load(force=True)

        per = 10
        start = (page - 1) * per
        album_sort = resolve_album_sort(request.args.get("sort"))
        plex_sort = plex_album_sort(album_sort)
        if plex_sort != album_cache.album_sort:
            album_cache.album_sort = plex_sort
            album_cache.load(force=True)
        q = (request.args.get("q") or "").strip()
        hub_limit = 500

        known_cf = hierarchy.known_names_casefold()
        gap_effective = bool(rym_gaps and known_cf is not None)
        gap_unavailable = bool(rym_gaps and known_cf is None)

        timer.time("lib size")
        try:
            library_album_total = section.totalViewSize(libtype="album")
        except (BadRequest, NotFound, OSError, RuntimeError):
            library_album_total = None

        scan_hit_cap = False
        filter_match_total: int | None = None
        gap_count_pending = False
        timer.time("lib size")

        try:
            if q:
                all_found = section.hubSearch(q, mediatype="album", limit=hub_limit)
                if gap_effective:
                    all_found = [
                        a for a in all_found if album_has_rym_genre_gap(a, known_cf)
                    ]
                if exclude_single_tracks:
                    all_found = [
                        a for a in all_found if passes_multi_track_filter(a, True)
                    ]
                if gap_effective or exclude_single_tracks:
                    filter_match_total = len(all_found)
                all_found = sort_albums(all_found, album_sort)
                albums = all_found[start : start + per]
                pager_has_next = start + per < len(all_found)
            elif gap_effective:
                timer.time("fetch albums")
                albums, pager_has_next, scan_hit_cap = album_cache.fetch_gap_page_slice(
                    known_cf,
                    page,
                    per,
                    exclude_single_tracks,
                    album_sort,
                )
                timer.time("fetch albums")
                filter_match_total = None
                gap_count_pending = True
            else:
                albums, pager_has_next, scan_hit_cap = fetch_browse_page_slice(
                    section, plex_sort, page, per, exclude_single_tracks
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
                    genre_src=genre_src,
                    album_sort=album_sort,
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

        timer.time("route")
        for album in albums:
            album.reload()
            album.genres
        get_album_tags(albums[0])
        return render_template(
            "index.html",
            section_title=section.title,
            albums=albums,
            q=q,
            page=page,
            per_page=per,
            rym_gaps=rym_gaps,
            genre_src=genre_src,
            album_sort=album_sort,
            exclude_single_tracks=exclude_single_tracks,
            gap_unavailable=gap_unavailable,
            gap_effective=gap_effective,
            gap_count_pending=gap_count_pending,
            pager_has_next=pager_has_next,
            library_album_total=library_album_total,
            scan_hit_cap=scan_hit_cap,
            filter_match_total=filter_match_total,
            genres_reloaded=genres_reloaded,
            genre_added=request.args.get("genre_added"),
            genre_create_error=request.args.get("genre_create_error"),
            error=None,
        )

    @app.get("/album/<int:rating_key>/lastfm")
    def lastfm(rating_key: int):
        album = album_cache.get(rating_key)
        if album is None:
            abort(500)
            return

        lastfm_tags = get_album_tags(album)
        _album = copy(album)
        _album.genres = lastfm_tags

        return render_template(
            "partial_album_row.html",
            album=_album,
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
            album = album_cache.get(rating_key)
        except NotFound:
            abort(404)
            return

        genre_src = resolve_genre_source(request.form.get("genre_src"))
        hierarchy = get_genre_hierarchy(genre_src)

        if hierarchy is None or album is None:
            abort(500)
            return

        picks = [s.strip() for s in genre.split(",") if s.strip()]
        tags, unknown = hierarchy.expand_picks(picks)
        tags.reverse()

        try:
            timer.time("album genre")
            set_album_genres(album, tags)
            timer.time("album genre")

            timer.time("track genre")
            set_tracks_genres(album, tags, section)
            timer.time("track genre")

            timer.time("reload album")
            album_cache.update(album)
            timer.time("reload album")

            if form_bool(request, "titlecase_tracks"):
                titlecase_tracks(album)

            if form_bool(request, "titlecase_albums"):
                titlecase_tracks(album)

            if bool(env("EDIT_TAGS", "")):
                timer.time("file update")
                disk = music_library_root()
                if disk is not None:
                    sync_album_genres_to_track_files(album, tags)
                timer.time("file update")

        except Exception as e:
            print(f"error: {e}")
            chip_tags = [s.strip() for s in genre.split(",") if s.strip()]
            return (
                render_template(
                    "partial_album_row.html",
                    album=album,
                    chip_tags=chip_tags,
                    error=str(e),
                    genre_unknown_note=None,
                    genre_src=genre_src,
                ),
                400,
            )

        note = None
        if unknown:
            label = (
                "genres.yaml" if genre_src == GENRE_SOURCE_CUSTOM else "RYM hierarchy"
            )
            note = f"Not in {label} (saved as typed): " + ", ".join(unknown)

        return render_template(
            "partial_album_row.html",
            album=album,
            error=None,
            genre_unknown_note=note,
            genre_src=genre_src,
        )

    @app.get("/partial/gap-match-count")
    def gap_match_count():
        genre_src = resolve_genre_source(request.args.get("genre_src"))
        hierarchy = get_genre_hierarchy(genre_src)
        known_cf = hierarchy.known_names_casefold() if hierarchy else None
        if not known_cf:
            return "—", 200

        exclude_single_tracks = request.args.get("no_singleton") in (
            "1",
            "on",
            "true",
            "yes",
        )

        try:
            matches = album_cache.fetch_all_gap_matches(known_cf, exclude_single_tracks)
        except (BadRequest, NotFound) as e:
            return escape(str(e)), 400

        return render_template(
            "partial_gap_count_inner.html",
            total=len(matches),
            scan_hit_cap=False,
        )

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(env("PORT", "5000")), debug=True)
