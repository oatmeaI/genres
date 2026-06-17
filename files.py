import logging

from pathlib import Path
from env import env
from plexapi.exceptions import BadRequest, NotFound
from mutagen._util import MutagenError
from mutagen._file import File as mutagen_file

log = logging.getLogger(__name__)


def resolve_plex_audio_file_on_disk(
    plex_file: str,
    plex_library_roots: list[str],
) -> Path | None:
    """Map Plex ``MediaPart.file`` onto a path under ``MUSIC_LIBRARY_DISK_PATH``.

    Strips the longest matching Plex library folder (from the section's ``locations``), then joins the
    remainder under ``disk_root``. If no root matches but ``plex_file`` exists on this machine, returns that path.
    """
    disk_root = music_library_root()

    if not plex_file or not disk_root:
        return None

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


def music_library_root() -> Path | None:
    raw = env("MUSIC_LIBRARY_DISK_PATH")
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def sync_album_genres_to_track_files(
    album,
) -> None:
    tags = [g.tag for g in album.genres]
    """Write the same genre list to each track file under ``disk_root`` (semicolon-separated)."""
    joined = ";".join(dict.fromkeys(tags)) if tags else ""
    album.reload()
    section = album._server.library.sectionByID(album.librarySectionID)
    plex_library_roots = list(section.locations)
    seen: set[Path] = set()
    try:
        tracks = album.tracks()
    except (BadRequest, NotFound, OSError, RuntimeError) as e:
        log.warning(
            "Could not list tracks for album %s: %s", getattr(album, "title", album), e
        )
        return

    for index, track in enumerate(tracks):
        print(f"processing file track: {track.title} ({index + 1} / {len(tracks)})")
        try:
            if track.isPartialObject() or not getattr(track, "media", None):
                track.reload()
        except (BadRequest, NotFound, OSError, RuntimeError) as e:
            log.warning(
                "Track reload failed (%s): %s", getattr(track, "title", track), e
            )
            continue
        try:
            locs = track.locations
        except (AttributeError, BadRequest, NotFound, OSError, RuntimeError) as e:
            log.warning(
                "No file locations for track %s: %s", getattr(track, "title", track), e
            )
            continue
        for plex_file in locs:
            local = resolve_plex_audio_file_on_disk(plex_file, plex_library_roots)
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


def _write_genre_tag_with_mutagen(path: Path, genre_semicolon_separated: str) -> None:
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
