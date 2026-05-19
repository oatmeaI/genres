from plexapi.library import MusicSection
from datetime import datetime
from util import disable_requests_logging, enable_requests_logging, timer
from concurrent.futures import ThreadPoolExecutor, as_completed


def album_has_rym_genre_gap(album, rym_cf: set[str]) -> bool:
    genres = album.genres or []
    if not genres:
        return True
    return any(g.tag.casefold() not in rym_cf for g in genres)


def passes_multi_track_filter(album, exclude_single_tracks: bool) -> bool:
    if not exclude_single_tracks:
        return True
    return album.leafCount > 1


def fetch_gap_page_slice(
    section: MusicSection,
    album_sort: str,
    rym_cf: set[str],
    page: int,
    per: int,
    exclude_single_tracks: bool,
    *,
    chunk: int = 50,
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
            album.reload()
            has_gap = album_has_rym_genre_gap(album, rym_cf)
            multitrack = passes_multi_track_filter(album, exclude_single_tracks)
            if has_gap and multitrack:
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


class AlbumCache:
    albums = {}
    loaded = False

    def __init__(self, section, album_sort) -> None:
        self.section = section
        self.album_sort = album_sort

    def preload_album(self, album):
        album.genres  # hacky way to preload genres for the album
        return album.ratingKey, album

    def load(
        self,
        chunk: int = 100,
        max_scan: int = 50_000,  # 1000,  # TODO: make this a setting this is usually 50k; set to a small number for debugging
    ):
        timer.time("load")

        if self.loaded:
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

    def fetch_gap_page_slice(
        self,
        rym_cf: set[str],
        page: int,
        per: int,
        exclude_single_tracks: bool,
    ) -> tuple[list, bool, bool]:

        # enable_requests_logging()
        start = (page - 1) * per
        all_album_list = sorted(
            list(
                filter(
                    lambda x: (
                        album_has_rym_genre_gap(x, rym_cf)
                        and passes_multi_track_filter(x, exclude_single_tracks)
                    ),
                    list(self.albums.values()),
                )
            ),
            key=lambda album: (
                album.lastViewedAt if album.lastViewedAt is not None else datetime.min
            ),
            reverse=True,
        )
        album_list = all_album_list[start : start + per]
        has_next = start + per < len(all_album_list)

        hit_cap = False  # TODO deprecate
        return album_list, has_next, hit_cap

    def fetch_all_gap_matches(
        self,
        rym_cf: set[str],
        exclude_single_tracks: bool,
    ) -> list:
        matches: list = []
        for _, key in enumerate(self.albums):
            album = self.albums[key]
            has_gap = album_has_rym_genre_gap(album, rym_cf)
            multitrack = passes_multi_track_filter(album, exclude_single_tracks)
            if has_gap and multitrack:
                matches.append(album)
        return matches
