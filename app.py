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

from flask import Flask, abort, render_template, request
from markupsafe import escape
from plexapi.exceptions import BadRequest, NotFound

from files import sync_album_genres_to_track_files, music_library_root
from plex import set_album_genres, set_tracks_genres
from rym import get_rym_hierarchy
from env import env
from util import timer
from plex_server import get_music_section
from search import (
    fetch_gap_page_slice,
    fetch_browse_page_slice,
    fetch_all_gap_matches,
    album_has_rym_genre_gap,
    passes_multi_track_filter,
)
from rym_hierarchy import (
    expand_genre_picks,
    rym_genre_names_casefold,
)

log = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)

    rym = get_rym_hierarchy()

    @app.context_processor
    def inject_rym_datalist():
        return {"rym_genre_options_html": rym.datalist_inner_html if rym else ""}

    @app.route("/")
    def index():
        timer.time("route")
        if rym is None:
            log.warning("Couldn't load RYM hiercharchy")
            abort(500)

        try:
            section = get_music_section()
        except RuntimeError as e:
            return render_template("error.html", message=str(e)), 503

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

        per = 10
        start = (page - 1) * per
        album_sort = "lastViewedAt:desc"  # TODO: query param
        q = (request.args.get("q") or "").strip()
        hub_limit = 500

        rym_cf = rym_genre_names_casefold(rym.nodes)
        gap_effective = bool(rym_gaps and rym_cf is not None)
        gap_unavailable = bool(rym_gaps and rym_cf is None)

        timer.time("lib size")
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
                    all_found = [
                        a for a in all_found if album_has_rym_genre_gap(a, rym_cf)
                    ]
                if exclude_single_tracks:
                    all_found = [
                        a for a in all_found if passes_multi_track_filter(a, True)
                    ]
                if gap_effective or exclude_single_tracks:
                    filter_match_total = len(all_found)
                albums = all_found[start : start + per]
                pager_has_next = start + per < len(all_found)
            elif gap_effective:
                timer.time("fetch albums")
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

        timer.time("route")
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

        if rym is None:
            abort(500)

        picks = [s.strip() for s in genre.split(",") if s.strip()]
        tags, unknown = expand_genre_picks(picks, rym.nodes)
        tags.reverse()

        try:
            set_album_genres(album, tags)
            set_tracks_genres(album, tags)
            disk = music_library_root()
            if disk is not None:
                sync_album_genres_to_track_files(album, tags)

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
                ),
                400,
            )

        note = None
        if unknown:
            note = "Not in RYM hierarchy (saved as typed): " + ", ".join(unknown)

        return render_template(
            "partial_album_row.html",
            album=album,
            error=None,
            genre_unknown_note=note,
        )

    @app.get("/partial/gap-match-count")
    def gap_match_count():
        return "—", 200
        """Full-library gap scan; fills server cache. Loaded async via HTMX so / does not block."""
        try:
            section = get_music_section()
        except RuntimeError as e:
            return escape(str(e)), 503

        rym = get_rym_hierarchy()
        rym_cf = rym_genre_names_casefold(rym.nodes) if rym else None
        if not rym_cf:
            return "—", 200

        exclude_single_tracks = request.args.get("no_singleton") in (
            "1",
            "on",
            "true",
            "yes",
        )

        try:
            matches, hit_cap = fetch_all_gap_matches(
                section, "addedAt:desc", rym_cf, exclude_single_tracks
            )
        except (BadRequest, NotFound) as e:
            return escape(str(e)), 400

        return render_template(
            "partial_gap_count_inner.html",
            total=len(matches),
            scan_hit_cap=hit_cap,
        )

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(env("PORT", "5000")), debug=True)
