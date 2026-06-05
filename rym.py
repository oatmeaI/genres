from functools import cache
from pathlib import Path
from env import env
from dataclasses import dataclass
from rym_hierarchy import (
    build_datalist_inner_html,
    build_genre_hints_json,
    expand_genre_picks,
    load_nodes,
    rym_genre_names_casefold,
)


@dataclass
class RymHierarchy:
    nodes: list
    datalist_inner_html: str
    genre_hints_json: str

    def expand_picks(self, picks: list[str]) -> tuple[list[str], list[str]]:
        return expand_genre_picks(picks, self.nodes)

    def known_names_casefold(self) -> set[str]:
        return rym_genre_names_casefold(self.nodes)


def hierarchy_path() -> Path:
    custom = env("RYM_HIERARCHY_PATH")
    if custom:
        return Path(custom).expanduser().resolve()
    return Path(__file__).resolve().parent / "data" / "RateYourMusic_Hierarchy.txt"


_rym: RymHierarchy | None = None
_rym_load_error: str | None = None


@cache
def get_rym_hierarchy() -> RymHierarchy | None:
    """Load RYM tree once. Returns None if the file is missing (expansion and datalist disabled)."""
    global _rym, _rym_load_error
    if _rym is not None:
        return _rym
    if _rym_load_error is not None:
        return None
    path = hierarchy_path()
    try:
        nodes = load_nodes(path)
        _rym = RymHierarchy(
            nodes=nodes,
            datalist_inner_html=build_datalist_inner_html(nodes),
            genre_hints_json=build_genre_hints_json(nodes),
        )
        return _rym
    except OSError as e:
        _rym_load_error = str(e)
        return None
