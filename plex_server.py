from plexapi.library import MusicSection
from plexapi.server import PlexServer
from functools import lru_cache
from env import env


# This basically memoizes
@lru_cache(maxsize=1)
def get_server() -> PlexServer:
    base = env("PLEX_URL")
    token = env("PLEX_TOKEN")
    if not base or not token:
        raise RuntimeError("Set PLEX_URL and PLEX_TOKEN in the environment.")
    return PlexServer(base.rstrip("/"), token)


def get_music_section() -> MusicSection:
    server = get_server()
    name = env("PLEX_SECTION")
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
