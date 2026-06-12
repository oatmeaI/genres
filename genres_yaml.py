"""Load custom genres from genres.yaml and expand picks with related genres."""

from __future__ import annotations

import html
import json
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
    genre_hints_json: str

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


def build_genre_hints_json(genres: list[GenreEntry]) -> str:
    hints = [
        {"name": g.name, "examples": list(g.examples)}
        for g in sorted(genres, key=lambda g: g.name.casefold())
    ]
    return json.dumps(hints, ensure_ascii=False)


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
        genre_hints_json=build_genre_hints_json(genres),
    )


@cache
def get_genres_yaml() -> GenresYaml | None:
    """Load genres.yaml once. Returns None if the file is missing or invalid."""
    return _load_genres_yaml()


def reload_genres_yaml() -> GenresYaml | None:
    """Drop the cached genres.yaml and read it again from disk."""
    get_genres_yaml.cache_clear()
    return get_genres_yaml()


def parse_genre_list_field(raw: str) -> list[str]:
    """Split a form field on commas or newlines."""
    items: list[str] = []
    seen: set[str] = set()
    for line in raw.replace(",", "\n").splitlines():
        value = line.strip()
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        items.append(value)
    return items


def _yaml_scalar(value: str) -> str:
    if value and all(c.isalnum() or c in " -'" for c in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def format_genres_yaml(genres: list[GenreEntry]) -> str:
    lines = ["---", "genres:"]
    for genre in genres:
        lines.append(f"- name: {_yaml_scalar(genre.name)}")
        lines.append("  examples: ")
        for example in genre.examples:
            lines.append(f"    - {_yaml_scalar(example)}")
        lines.append("  related: ")
        for related in genre.related:
            lines.append(f"    - {_yaml_scalar(related)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _validate_unique_name(
    genres: list[GenreEntry], name: str, except_index: int | None = None
) -> None:
    key = name.casefold()
    for i, genre in enumerate(genres):
        if except_index is not None and i == except_index:
            continue
        if genre.name.casefold() == key:
            raise ValueError(f"Genre already exists: {genre.name}")


def save_genres(genres: list[GenreEntry]) -> None:
    path = genres_yaml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_genres_yaml(genres), encoding="utf-8")
    reload_genres_yaml()


def load_genre_entries() -> list[GenreEntry]:
    current = _load_genres_yaml()
    return list(current.genres) if current else []


def add_genre_entry(entry: GenreEntry) -> None:
    """Append a genre to genres.yaml on disk."""
    if not entry.name.strip():
        raise ValueError("Genre name is required.")

    genres = load_genre_entries()
    _validate_unique_name(genres, entry.name)
    genres.append(entry)
    save_genres(genres)


def update_genre_at(index: int, entry: GenreEntry) -> None:
    if not entry.name.strip():
        raise ValueError("Genre name is required.")

    genres = load_genre_entries()
    if index < 0 or index >= len(genres):
        raise ValueError("Genre not found.")
    _validate_unique_name(genres, entry.name, except_index=index)
    genres[index] = entry
    save_genres(genres)


def delete_genre_at(index: int) -> str:
    genres = load_genre_entries()
    if index < 0 or index >= len(genres):
        raise ValueError("Genre not found.")
    name = genres[index].name
    del genres[index]
    save_genres(genres)
    return name
