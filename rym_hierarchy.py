"""Parse RateYourMusic hierarchy text and expand genre tags with ancestor labels."""

from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path

# Section roots in the hierarchy file — not useful as Plex genre tags.
_SKIP_ANCESTOR_LABELS = frozenset({"Descriptors", "Genres", "Scenes & Movements", "Regional Music", "North American Music", "Northern American Music", "Hispanic Music", "Hispanic American Music"})


@dataclass(frozen=True)
class ResolvedNode:
    display: str
    ancestors: tuple[str, ...]


def _parse_line(line: str) -> tuple[int, str] | None:
    line = line.rstrip("\n\r")
    if not line.strip():
        return None
    if line.startswith("Source URL:") or line.startswith("Title:"):
        return None
    leading = len(line) - len(line.lstrip(" "))
    if leading % 4 != 0:
        # tolerate irregular indent by rounding down to last multiple of 4
        leading = (leading // 4) * 4
    depth = leading // 4
    raw = line.strip()
    if "::" in raw:
        raw = raw.rsplit("::", 1)[0].strip()
    return depth, raw


def load_nodes(path: Path) -> list[ResolvedNode]:
    """One ResolvedNode per tree line; duplicate display names may appear at different depths."""
    stack: list[tuple[int, str]] = []
    out: list[ResolvedNode] = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        parsed = _parse_line(line)
        if parsed is None:
            continue
        depth, display = parsed
        while stack and stack[-1][0] >= depth:
            stack.pop()
        ancestors = tuple(s[1] for s in stack)
        out.append(ResolvedNode(display=display, ancestors=ancestors))
        stack.append((depth, display))
    return out


def _resolve_one(name: str, nodes: list[ResolvedNode]) -> ResolvedNode | None:
    key = name.strip().casefold()
    if not key:
        return None
    best: ResolvedNode | None = None
    best_rank = (-1, -1)  # (len(ancestors), position)
    for i, n in enumerate(nodes):
        if n.display.casefold() != key:
            continue
        rank = (len(n.ancestors), i)
        if rank > best_rank:
            best_rank = rank
            best = n
    return best


def expand_genre_picks(picks: list[str], nodes: list[ResolvedNode]) -> tuple[list[str], list[str]]:
    """Return Plex genre strings in order, with parents before each recognized pick; dedupe case-insensitively.

    Unknown picks are kept as-is (no parent expansion).
    """
    seen: set[str] = set()
    ordered: list[str] = []
    unknown: list[str] = []

    def add_label(label: str) -> None:
        k = label.casefold()
        if k in seen:
            return
        seen.add(k)
        ordered.append(label)

    for raw in picks:
        term = raw.strip()
        if not term:
            continue
        node = _resolve_one(term, nodes)
        if node is None:
            unknown.append(term)
            add_label(term)
            continue
        for p in node.ancestors:
            if p in _SKIP_ANCESTOR_LABELS:
                continue
            add_label(p)
        add_label(node.display)
    return ordered, unknown


def build_datalist_inner_html(nodes: list[ResolvedNode]) -> str:
    names = sorted({n.display for n in nodes}, key=str.casefold)
    return "\n".join(f'<option value="{html.escape(n)}">' for n in names)


def rym_genre_names_casefold(nodes: list[ResolvedNode]) -> set[str]:
    """Case-insensitive RYM display names for validating Plex genre tags."""
    return {n.display.casefold() for n in nodes}
