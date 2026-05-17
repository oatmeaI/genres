from plexapi.library import MusicSection


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
