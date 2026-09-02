from plexapi.library import MusicSection
from plexapi import CONFIG
CONFIG.autoreload = False  # requires manual reload() calls
import plexapi
from datetime import datetime
from util import disable_requests_logging, enable_requests_logging, timer
from concurrent.futures import ThreadPoolExecutor, as_completed
from env import env
import rich
from plex import sync_collection_to_album_genre

ALBUM_SORT_RECENTLY_PLAYED = "played"
ALBUM_SORT_RECENTLY_ADDED = "added"
ALBUM_SORT_MOST_PLAYED = "plays"
DEFAULT_ALBUM_SORT = ALBUM_SORT_RECENTLY_PLAYED

_PLEX_ALBUM_SORT = {
    ALBUM_SORT_RECENTLY_PLAYED: "lastViewedAt:desc",
    ALBUM_SORT_RECENTLY_ADDED: "addedAt:desc",
    ALBUM_SORT_MOST_PLAYED: "viewCount:desc",
}


def resolve_album_sort(value: str | None) -> str:
    if value in _PLEX_ALBUM_SORT:
        return value
    return DEFAULT_ALBUM_SORT


def plex_album_sort(value: str | None) -> str:
    return _PLEX_ALBUM_SORT[resolve_album_sort(value)]


def album_sort_key(album, sort_mode: str):
    mode = resolve_album_sort(sort_mode)
    if mode == ALBUM_SORT_RECENTLY_ADDED:
        return album.addedAt or datetime.min
    if mode == ALBUM_SORT_MOST_PLAYED:
        return album.viewCount or 0
    # album.reload()  # NOTE: keep recently played up to date, this is going to slow things down. Let's see how badly.
    return album.lastViewedAt or datetime.min


def sort_albums(albums: list, sort_mode: str) -> list:
    return sorted(albums, key=lambda a: album_sort_key(a, sort_mode), reverse=True)


def album_has_rym_genre_gap(album, rym_cf: set[str], genre_gap: bool) -> bool:
    if not genre_gap:
        return True
    genres = album.genres or []
    if not genres:
        return False
    return any(g.tag.casefold() not in rym_cf for g in genres)


def passes_multi_track_filter(album, exclude_single_tracks: bool) -> bool:
    if not exclude_single_tracks:
        return True
    return album.leafCount > 1


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


def filter_no_genres(album, no_genre):
    if not no_genre:
        return True
    return len(album.genres) < 1


def filter_unplayed(album, played_albums):
    if not played_albums:
        return True
    return album.viewCount > 0


def filter_by_query(album, q):
    if len(q) < 1:
        return True
    return (
        q.casefold() in album.title.casefold()
        or q.casefold() in album.parentTitle.casefold()
        or q.casefold() in [g.tag.casefold() for g in album.genres]
    )


class AlbumCache:
    albums = {}
    loaded = False

    def __init__(self, section, album_sort) -> None:
        self.section = section
        self.album_sort = album_sort

    def preload_album(self, album):
        album._autoReload = False # Stops python-plexapi from making extra requests; might break stuff
        tracks = album.tracks()

        # Hacky and slow way to preload user ratings for tracks.
        # Gotta be a way to make this faster...
        for track in tracks:
            track._autoReload = False
            track.loaded_rating = track.userRating
            track.loaded_plays = track.viewCount

        album.loaded_tracks = tracks
        album.last_track = tracks[-1]

        for yield_value in sync_collection_to_album_genre(album, self.section):
            pass

        return album.ratingKey, album

    def size(self):
        return len(list(self.albums.values()))

    # TODO: duplicating this whole thing is stupid but whatever
    def load_generator(
        self,
        force: bool = False,
        chunk: int = 100,
        max_scan: int | None = None,
    ):
        timer.time("load")

        if bool(env("DEBUG", "")):
            max_scan = int(env("DEBUG_MAX_SCAN", "300"))

        max_scan = max_scan or int(env("MAX_SCAN", "50_000"))

        if self.loaded and not force:
            return

        container_start = 0
        scanned = 0

        while scanned < max_scan:
            batch = self.section.search(
                libtype="album",
                sort=self.album_sort,
                maxresults=chunk,
                container_start=container_start,
            )

            if not batch:
                break

            scanned += len(batch)
            loaded = self.section.fetchItems(
                [x.ratingKey for x in batch],
            )

            with ThreadPoolExecutor(max_workers=10) as executor:
                future_to_album = {
                    executor.submit(self.preload_album, album): album
                    for album in loaded
                }
                for future in as_completed(future_to_album):
                    rating_key, album = future.result()
                    self.albums[rating_key] = album

            yield f"."

            container_start += len(batch)

            if len(batch) < chunk:
                break

        self.loaded = True

        timer.time("load")

    def load(
        self,
        force: bool = False,
        chunk: int = 100,
        max_scan: int | None = None,
    ):
        timer.time("load")

        if bool(env("DEBUG", "")):
            max_scan = int(env("DEBUG_MAX_SCAN", "300"))

        max_scan = max_scan or int(env("MAX_SCAN", "50_000"))

        if self.loaded and not force:
            return

        container_start = 0
        scanned = 0

        while scanned < max_scan:
            batch = self.section.search(
                libtype="album",
                sort=self.album_sort,
                maxresults=chunk,
                container_start=container_start,
            )

            if not batch:
                break

            scanned += len(batch)
            loaded = self.section.fetchItems(
                [x.ratingKey for x in batch],
            )

            with ThreadPoolExecutor(max_workers=10) as executor:
                future_to_album = {
                    executor.submit(self.preload_album, album): album
                    for album in loaded
                }
                for future in as_completed(future_to_album):
                    rating_key, album = future.result()
                    self.albums[rating_key] = album

            print(f"loaded batch {container_start}")

            container_start += len(batch)

            if len(batch) < chunk:
                break

            

        self.loaded = True

        timer.time("load")

    def get(self, rating_key):
        if rating_key in self.albums:
            return self.albums[rating_key]
        else:
            album = self.section.searchAlbums(id=rating_key)[0]
            album.reload()
            self.albums[rating_key] = album
            return album

    def update(self, album):
        album.reload()
        self.albums[album.ratingKey] = album
        return album

    def partially_played_albums(
        self,
    ):
        def filter_unplayed(album):
            return any(
                track.loaded_plays < 1 for track in album.loaded_tracks
            ) and any(track.loaded_plays > 0 for track in album.loaded_tracks)

        all_album_list = list(
                filter(
                    lambda album: (filter_unplayed(album)),
                    list(self.albums.values()),
                )
            )
        return all_album_list

    def unfaved_albums(
        self,
    ):
        def filter_unfaved(album):
            return all(
                track.loaded_rating == None for track in album.loaded_tracks
            )

        all_album_list = list(
                filter(
                    lambda album: (filter_unfaved(album)),
                    list(self.albums.values()),
                )
            )
        return all_album_list

    def unplayed_albums(
        self,
    ):
        def filter_unplayed(album):
            return all(
                track.viewCount < 1 for track in album.loaded_tracks
            )

        all_album_list = list(
                filter(
                    lambda album: (filter_unplayed(album)),
                    list(self.albums.values()),
                )
            )
        return all_album_list

    def upgrade_albums(
        self,
        bitrate = 320
    ):
        def filter_upgrade(album):
            return all(
                (track.media[0].bitrate if track.media[0].bitrate is not None else 0) < bitrate for track in album.loaded_tracks
            )

        all_album_list = list(
                filter(
                    lambda album: (filter_upgrade(album)),
                    list(self.albums.values()),
                )
            )
        return all_album_list

    # TODO: will not detect albums where the missing tracks are at the end of the album
    def incomplete_albums(
        self,
    ):
        def filter_incomplete(album):
            if album.last_track.trackNumber is None or album.loaded_tracks is None:
                # TODO: note this somewhere, probably
                return False  

            #NOTE: `and` condition here filters for only albums that have been matched; should be an option
            return (len(album.loaded_tracks) < album.last_track.trackNumber) and len(album.guids) > 0

        all_album_list = list(
                filter(
                    lambda album: (filter_incomplete(album)),
                    list(self.albums.values()),
                )
            )
        return all_album_list

    def fetch_upgrade_page(
        self,
        page: int,
        per: int,
        album_sort: str = DEFAULT_ALBUM_SORT,
        bitrate: int = 256,
    ):
        def filter_upgradeable(album):
            return any(
                track.media[0].bitrate < bitrate for track in album.loaded_tracks
            )

        start = (page - 1) * per
        all_album_list = sort_albums(
            list(
                filter(
                    lambda album: (filter_upgradeable(album)),
                    list(self.albums.values()),
                )
            ),
            album_sort,
        )
        album_list = all_album_list[start : start + per]
        has_next = start + per < len(all_album_list)

        return album_list, has_next, len(all_album_list)

    # TODO: will not detect albums where the missing tracks are at the end of the album
    def fetch_incomplete_page(
        self, page: int, per: int, album_sort: str = DEFAULT_ALBUM_SORT
    ):
        def filter_incomplete(album):
            if album.last_track.trackNumber is None or album.loaded_tracks is None:
                return False  # TODO: note this somewhere, probably
            #NOTE: `and` condition here filters for only albums that have been matched;
            # should be an option
            return (len(album.loaded_tracks) < album.last_track.trackNumber) and len(album.guids) > 0

        start = (page - 1) * per
        all_album_list = sort_albums(
            list(
                filter(
                    lambda album: (filter_incomplete(album)),
                    list(self.albums.values()),
                )
            ),
            album_sort,
        )
        album_list = all_album_list[start : start + per]
        has_next = start + per < len(all_album_list)

        return album_list, has_next, len(all_album_list)

    def fetch_page(
        self,
        rym_cf: set[str],
        page: int,
        per: int,
        exclude_single_tracks: bool,
        sort_mode: str = DEFAULT_ALBUM_SORT,
        genre_gap: bool = False,
        no_genre: bool = False,
        played_albums: bool = False,
        q: str = "",
    ) -> tuple[list, bool, int]:
        # enable_requests_logging()
        start = (page - 1) * per
        all_album_list = sort_albums(
            list(
                filter(
                    lambda x: (
                        album_has_rym_genre_gap(x, rym_cf, genre_gap)
                        and passes_multi_track_filter(x, exclude_single_tracks)
                        and filter_by_query(x, q)
                        and filter_unplayed(x, played_albums)
                        and filter_no_genres(x, no_genre)
                    ),
                    list(self.albums.values()),
                )
            ),
            sort_mode,
        )
        album_list = all_album_list[start : start + per]
        has_next = start + per < len(all_album_list)

        return album_list, has_next, len(all_album_list)

    def find_by_genre(self, genre: str):
        albums = []
        genre = genre.casefold()
        for album in self.albums.values():
            genres = [g.tag.casefold() for g in album.genres]
            if genre in genres:
                albums.append(album)
        return albums

    def fetch_all_gap_matches(
        self,
        rym_cf: set[str],
        exclude_single_tracks: bool,
    ) -> list:
        matches: list = []
        for _, key in enumerate(self.albums):
            album = self.albums[key]
            has_gap = album_has_rym_genre_gap(album, rym_cf, genre_gap=True)
            multitrack = passes_multi_track_filter(album, exclude_single_tracks)
            if has_gap and multitrack:
                matches.append(album)
        return matches
