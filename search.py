from plexapi.library import MusicSection
from datetime import datetime
from util import disable_requests_logging, enable_requests_logging, timer
from concurrent.futures import ThreadPoolExecutor, as_completed
from env import env

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
    return q in album.title or q in album.parentTitle


class AlbumCache:
    albums = {}
    loaded = False

    def __init__(self, section, album_sort) -> None:
        self.section = section
        self.album_sort = album_sort

    def preload_album(self, album):
        album.reload()  # hacky way to preload genres for the album
        return album.ratingKey, album

    def size(self):
        return len(list(self.albums.values()))

    def load(
        self,
        force: bool = False,
        chunk: int = 100,
        max_scan: int | None = None,
    ):
        timer.time("load")

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
            album = self.section.get(rating_key)
            album.reload()
            self.albums[rating_key] = album
            return album

    def update(self, album):
        album.reload()
        self.albums[album.ratingKey] = album

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
        return list(
            filter(
                lambda album: (
                    any(
                        album_genre.tag.casefold() == genre
                        for album_genre in (album.genres or [])
                    )
                ),
                list(self.albums.values()),
            )
        )

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
