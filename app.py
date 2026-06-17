from __future__ import annotations

import logging
from urllib.parse import urlencode
from datetime import datetime, timedelta
from copy import copy
from flask import Flask, abort, redirect, render_template, request, url_for
from lastfm import get_album_tags
from markupsafe import escape
from plex_queue import plex_queue
from plexapi.exceptions import BadRequest, NotFound

from files import sync_album_genres_to_track_files, music_library_root
from plex import (
    remove_album_genre,
    remove_from_collections,
    remove_tracks_genre,
    set_album_genres,
    set_tracks_genres,
    sync_album_genre_to_collection,
    sync_collection_to_album_genre,
    titlecase_tracks,
)
from genres_yaml import (
    GenreEntry,
    GenresYaml,
    add_genre_entry,
    delete_genre_at,
    genres_yaml_path,
    get_genres_yaml,
    parse_genre_list_field,
    reload_genres_yaml,
    update_genre_at,
)
from rym import RymHierarchy, get_rym_hierarchy
from env import env
from util import form_bool, query_bool, timer
from plex_server import get_music_section
from search import (
    AlbumCache,
    plex_album_sort,
    resolve_album_sort,
)

log = logging.getLogger(__name__)

GENRE_SOURCE_RYM = "rym"
GENRE_SOURCE_CUSTOM = "custom"


def resolve_genre_source(value: str | None) -> str:
    return GENRE_SOURCE_RYM if value == GENRE_SOURCE_RYM else GENRE_SOURCE_CUSTOM


def request_genre_src() -> str | None:
    if not request:
        return None
    return request.args.get("genre_src") or request.form.get("genre_src")


def get_genre_hierarchy(source: str) -> RymHierarchy | GenresYaml | None:
    if source == GENRE_SOURCE_CUSTOM:
        return get_genres_yaml()
    return get_rym_hierarchy()


def genre_entry_from_form() -> GenreEntry:
    name = (request.form.get("name") or "").strip()
    examples = parse_genre_list_field(request.form.get("examples") or "")
    related = parse_genre_list_field(request.form.get("related") or "")
    return GenreEntry(name=name, examples=tuple(examples), related=tuple(related))


def editor_redirect(**params):
    return redirect(url_for("genres_editor", **params))


def is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def genres_editor_oob_response(
    *,
    flash: str | None = None,
    flash_kind: str = "success",
    genres: list | None = None,
):
    genres_yaml = get_genres_yaml()
    if genres is None:
        genres = genres_yaml.genres if genres_yaml else []
    hints = genres_yaml.genre_hints_json if genres_yaml else "[]"
    return render_template(
        "partial_genres_editor_oob.html",
        genres=genres,
        flash=flash,
        flash_kind=flash_kind,
        genre_hints_json=hints,
    )


def create_app() -> Flask:
    app = Flask(__name__)
    section = get_music_section()
    album_cache = AlbumCache(section, "lastViewedAt:desc")
    album_cache.load()

    @app.context_processor
    def inject_genre_datalist():
        if request and (
            request.path.startswith("/genres")
            or request.path.startswith("/partial/genres-editor")
        ):
            hierarchy = get_genres_yaml()
            src = GENRE_SOURCE_CUSTOM
        else:
            src = resolve_genre_source(request_genre_src())
            hierarchy = get_genre_hierarchy(src)
        return {
            "genre_hints_json": hierarchy.genre_hints_json if hierarchy else "[]",
            "genre_src": src,
            "hierarchy_known_cf": (
                hierarchy.known_names_casefold() if hierarchy else frozenset()
            ),
        }

    @app.get("/genres/editor")
    def genres_editor():
        if query_bool(request, "reload_genres"):
            reload_genres_yaml()
        genres_yaml = get_genres_yaml()
        if genres_yaml is None:
            return render_template(
                "error.html",
                message="Could not load genres.yaml.",
            ), 503
        return render_template(
            "genres_editor.html",
            genres=genres_yaml.genres,
            file_path=str(genres_yaml_path()),
            genre_added=request.args.get("genre_added"),
            genre_updated=request.args.get("genre_updated"),
            genre_deleted_yaml=request.args.get("genre_deleted"),
            genres_reloaded=query_bool(request, "reload_genres"),
            error=request.args.get("error"),
        )

    @app.get("/partial/genres-editor")
    def genres_editor_partial():
        if query_bool(request, "reload_genres"):
            reload_genres_yaml()
        genres_yaml = get_genres_yaml()
        if genres_yaml is None:
            return "Could not load genres.yaml.", 503
        flash = (
            "Reloaded genres.yaml from disk."
            if query_bool(request, "reload_genres")
            else None
        )
        return genres_editor_oob_response(flash=flash, genres=genres_yaml.genres)

    @app.post("/genres")
    def create_genre():
        entry = genre_entry_from_form()
        try:
            add_genre_entry(entry)
        except ValueError as e:
            if is_htmx():
                return genres_editor_oob_response(flash=str(e), flash_kind="error")
            return editor_redirect(error=str(e))
        except OSError as e:
            log.exception("Failed to write genres.yaml")
            msg = f"Could not write genres.yaml: {e}"
            if is_htmx():
                return genres_editor_oob_response(flash=msg, flash_kind="error")
            return editor_redirect(error=msg)
        if is_htmx():
            return genres_editor_oob_response(flash=f"Added {entry.name}.")
        return editor_redirect(genre_added=entry.name)

    @app.post("/genres/<int:index>")
    def update_genre_entry(index: int):
        entry = genre_entry_from_form()
        try:
            update_genre_at(index, entry)
        except ValueError as e:
            if is_htmx():
                return genres_editor_oob_response(flash=str(e), flash_kind="error")
            return editor_redirect(error=str(e))
        except OSError as e:
            log.exception("Failed to write genres.yaml")
            msg = f"Could not write genres.yaml: {e}"
            if is_htmx():
                return genres_editor_oob_response(flash=msg, flash_kind="error")
            return editor_redirect(error=msg)
        if is_htmx():
            return genres_editor_oob_response(flash=f"Updated {entry.name}.")
        return editor_redirect(genre_updated=entry.name)

    @app.post("/genres/<int:index>/delete")
    def delete_genre_entry(index: int):
        try:
            name = delete_genre_at(index)
        except ValueError as e:
            if is_htmx():
                return genres_editor_oob_response(flash=str(e), flash_kind="error")
            return editor_redirect(error=str(e))
        except OSError as e:
            log.exception("Failed to write genres.yaml")
            msg = f"Could not write genres.yaml: {e}"
            if is_htmx():
                return genres_editor_oob_response(flash=msg, flash_kind="error")
            return editor_redirect(error=msg)
        if is_htmx():
            return genres_editor_oob_response(flash=f"Deleted {name} from the file.")
        return editor_redirect(genre_deleted=name)

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

        rym_gaps = query_bool(request, "rym_gaps")
        no_genre = query_bool(request, "no_genre")
        played_albums = query_bool(request, "played_albums")
        exclude_single_tracks = query_bool(request, "no_singleton")
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1

        force_reload = query_bool(request, "reload")
        if force_reload:
            album_cache.load(force=True)

        per = 25
        album_sort = resolve_album_sort(request.args.get("sort"))
        plex_sort = plex_album_sort(album_sort)
        if plex_sort != album_cache.album_sort:
            album_cache.album_sort = plex_sort
            album_cache.load(force=True)
        q = (request.args.get("q") or "").strip()

        known_cf = hierarchy.known_names_casefold()
        gap_effective = bool(rym_gaps and known_cf is not None)
        gap_unavailable = bool(rym_gaps and known_cf is None)

        timer.time("lib size")
        library_album_total = album_cache.size()

        scan_hit_cap = False
        filter_match_total: int | None = None
        gap_count_pending = False
        timer.time("lib size")

        try:
            timer.time("fetch albums")
            albums, pager_has_next, filter_match_total = album_cache.fetch_page(
                known_cf,
                page,
                per,
                exclude_single_tracks,
                album_sort,
                rym_gaps,
                no_genre,
                played_albums,
                q,
            )
            timer.time("fetch albums")
        except (BadRequest, NotFound) as e:
            return (
                render_template(
                    "index.html",
                    section_title=section.title,
                    albums=[],
                    q=q,
                    no_genre=no_genre,
                    played_albums=played_albums,
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

        for album in albums:
            album.reload()
            album.genres

        return render_template(
            "index.html",
            section_title=section.title,
            albums=albums,
            q=q,
            no_genre=no_genre,
            played_albums=played_albums,
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
            genre_deleted=request.args.get("genre_deleted"),
            error=None,
        )

    @app.get("/sync-collection-genre")
    def sync_collection_genre():
        albums = album_cache.albums.values()
        i = 0
        for album in albums:
            i += 1
            print(f"[{i}/{len(albums)}] - {album.title}")
            sync_collection_to_album_genre(album, section)

    @app.get("/sync-genre-collection")
    def sync_genre_collection():
        albums = album_cache.albums.values()
        i = 0
        for album in albums:
            i += 1
            print(f"[{i}/{len(albums)}] - {album.title}")
            sync_album_genre_to_collection(album, section)

    @app.post("/batch-delete-genre")
    def batch_delete_genre():
        name = (request.form.get("name") or "").strip()
        albums = album_cache.find_by_genre(name)
        i = 0
        for album in albums:
            i += 1
            print(f"[{i}/{len(albums)}] - {album.title}")
            remove_album_genre(album, name, section)
            print("album tag updated")
            remove_tracks_genre(album, name, section)
            print("track tags updated")
            album_cache.update(album)
        return redirect(
            url_for(
                "index",
                genre_src=GENRE_SOURCE_CUSTOM,
                genre_deleted=name,
            )
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

        def update():
            print(tags)
            set_album_genres(album, tags)
            set_tracks_genres(album, tags, section)
            sync_collection_to_album_genre(album, section)
            updated_album = album_cache.update(album)
            remove_from_collections(album, section)
            sync_album_genre_to_collection(updated_album, section)

        try:
            plex_queue.queue_request(
                update,
                f"Update {album.title}",
            )

            if form_bool(request, "titlecase_tracks"):
                titlecase_tracks(album)

            if form_bool(request, "titlecase_albums"):
                titlecase_tracks(album)

            # NOTE: feels weird to hardcode this "unsure" case
            if bool(env("EDIT_TAGS", "")) and genre.casefold() != "unsure":
                timer.time("file update")
                disk = music_library_root()
                if disk is not None:
                    sync_album_genres_to_track_files(album)
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

    @app.get("/history")
    def history():
        oldest = request.args.get("oldest") or None
        oldest_input = request.args.get("oldest_input")

        if oldest_input:
            oldest = datetime.strptime(oldest_input, "%Y-%m-%d").timestamp()

        newest = (
            request.args.get("newest")
            or (datetime.now() - timedelta(days=0)).timestamp()
        )

        newest_input = request.args.get("newest_input")

        if newest_input:
            newest = datetime.strptime(newest_input, "%Y-%m-%d").timestamp()

        max = int(request.args.get("max") or 25) or None

        filter = query_bool(request, "filter")

        args = {
            "librarySectionID": section.key,
            "viewedAt<": int(newest),
            "sort": "viewedAt:desc",
        }

        if oldest:
            args["viewedAt>"] = int(oldest)

        key = f"/status/sessions/history/all?{urlencode(args)}"

        history = section.fetchItems(key, maxresults=max)

        newest_shown = (
            int(history[0].viewedAt.timestamp()) if len(history) > 0 else int(newest)
        )
        oldest_shown = (
            int(history[-1].viewedAt.timestamp())
            if len(history) > 0
            else 0  # FIXME int(oldest)
        )

        prev_play = None
        filtered_plays = []

        if filter:
            prev_play = None
            for play in history:
                if (
                    prev_play
                    and play.title == prev_play.title
                    and prev_play.parentTitle == play.parentTitle
                ):
                    if prev_play not in filtered_plays:
                        filtered_plays.append(prev_play)
                    filtered_plays.append(play)
                prev_play = play
        else:
            filtered_plays = history

        for play in filtered_plays:
            try:
                play.url = play.album().thumbUrl
            except Exception as e:
                play.url = None

        return render_template(
            "history_editor.html",
            history=filtered_plays,  # TODO: this always shows a previous button even when not available?
            newest_shown=newest_shown + 1,
            oldest_shown=oldest_shown - 1,  # Not clean, but ensures we avoid repeats
            max=max or 0,
            should_filter=filter,
            oldest_input=datetime.fromtimestamp(oldest or 0).strftime("%Y-%m-%d"),
            newest_input=datetime.fromtimestamp(int(newest)).strftime("%Y-%m-%d"),
        )

    @app.delete("/delete-history")
    def delete_history():
        id = request.args.get("key")
        # day = request.args.get("day")
        history_item = section.fetchItem(id)
        history_item.delete()
        return "", 200

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
