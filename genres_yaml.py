"""Load custom genres from genres.yaml and expand picks with related genres."""

from __future__ import annotations

import html
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

from env import env


@dataclass(frozen=True)
class GenreEntry:
    name: str
    examples: tuple[str, ...] = ()
    related: tuple[str, ...] = ()


@dataclass
class GenresYaml:
    genres: list[GenreEntry]
    datalist_inner_html: str

    def expand_picks(self, picks: list[str]) -> tuple[list[str], list[str]]:
        """Return Plex genre strings; each recognized pick adds its ``related`` genres first."""
        by_name = {g.name.casefold(): g for g in self.genres}
        seen: set[str] = set()
        ordered: list[str] = []
        unknown: list[str] = []

        def add_label(label: str) -> None:
            key = label.casefold()
            if key in seen:
                return
            seen.add(key)
            ordered.append(label)

        for raw in picks:
            term = raw.strip()
            if not term:
                continue
            entry = by_name.get(term.casefold())
            if entry is None:
                unknown.append(term)
                add_label(term)
                continue
            for related in entry.related:
                add_label(related)
            add_label(entry.name)
        return ordered, unknown

    def known_names_casefold(self) -> set[str]:
        return {g.name.casefold() for g in self.genres}


def genres_yaml_path() -> Path:
    custom = env("GENRES_YAML_PATH")
    if custom:
        return Path(custom).expanduser().resolve()
    return Path(__file__).resolve().parent / "data" / "genres.yaml"


def _parse_entries(data: object) -> list[GenreEntry]:
    raw = data.get("genres") if isinstance(data, dict) else data
    if not raw:
        return []
    entries: list[GenreEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        examples = tuple(
            str(x).strip() for x in (item.get("examples") or []) if str(x).strip()
        )
        related = tuple(
            str(x).strip() for x in (item.get("related") or []) if str(x).strip()
        )
        entries.append(GenreEntry(name=name, examples=examples, related=related))
    return entries


def build_datalist_inner_html(genres: list[GenreEntry]) -> str:
    names = sorted({g.name for g in genres}, key=str.casefold)
    return "\n".join(f'<option value="{html.escape(n)}">' for n in names)


def _load_genres_yaml() -> GenresYaml | None:
    path = genres_yaml_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        print(e)
        return None
    except yaml.YAMLError as e:
        print(e)
        return None
    genres = _parse_entries(data)
    return GenresYaml(
        genres=genres,
        datalist_inner_html=build_datalist_inner_html(genres),
    )


@cache
def get_genres_yaml() -> GenresYaml | None:
    """Load genres.yaml once. Returns None if the file is missing or invalid."""
    return _load_genres_yaml()


def reload_genres_yaml() -> GenresYaml | None:
    """Drop the cached genres.yaml and read it again from disk."""
    get_genres_yaml.cache_clear()
    return get_genres_yaml()
