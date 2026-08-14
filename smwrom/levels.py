"""Which levels play which song, and what those levels are called.

Two separate mechanisms, both read here because the answer needs both.

**The music.** Lunar Magic's "Change Music & Time Limit Settings" does not live
in a table. It sits inside the level's own Layer-1 object data as the 3-byte
record ``40 60 vv``, placed last, immediately before the ``FF`` terminator, and
the track is ``vv - 1``. That is why every table-shaped search for it failed:
there is no table. A level without the record falls back to the 3-bit field in
its header, translated through an 8-byte table in the ROM — a table AddmusicK
overwrites, so it has to be read rather than assumed.

**The names.** Lunar Magic relocates the level-name table and leaves a pointer to
it at the *vanilla* table's address. It is indexed by translevel rather than by
level number, which is why `UNDERGROUND` (level ``$006``) and `GET OUT` (level
``$10A``) sit forty entries apart rather than two hundred and sixty.

Verified against Lunar Magic 3.63's own *Analyze Resources in Levels* report:
512 of 512 levels in two ROMs, plus every known pair in a third, SA-1 one. The
research and its sources are in
``docs/research/rom-music-extraction/level_music_bypass.md``.

What this does **not** cover: overworld music. A map plays a song too, and that
lives somewhere else entirely.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from smwrom.mapping import RomImage

#: SNES address of the Layer-1 data pointers, three bytes per level.
#:
#: Only these. The Layer-2 and sprite pointer tables look equally usable for
#: bounding a level's data and are not: some of their entries point into the
#: middle of a Layer-1 block, which cuts the search window short. Measured over
#: three ROMs, Layer-1 alone scores 1061/1061 and adding the others 1058/1061.
_LAYER1_POINTERS = 0x05E000

#: SNES address of the 8-byte table the header's music field indexes.
#: AddmusicK overwrites it, so hacks disagree with vanilla and with each other.
_MUSIC_TRANSLATION = 0x0584DB

#: PC address of the *vanilla* level-name table. Lunar Magic leaves a 3-byte
#: pointer to the relocated one here.
_NAME_POINTER = 0x01BB57

#: The overworld maps, in the order Lunar Magic's *Edit Submap Music Selection*
#: lists them. Fixed: a hack can rename its levels but not its seven submaps.
SUBMAP_NAMES = (
    "Main map",
    "Yoshi's Island",
    "Vanilla Dome",
    "Forest of Illusion",
    "Bowser's Valley",
    "Special World",
    "Star Road",
)

#: SNES addresses of the submap music table — seven bytes, one song ID per map,
#: in :data:`SUBMAP_NAMES` order. SMW keeps two copies and they have agreed in
#: every ROM seen; both are read and a disagreement is treated as unreadable
#: rather than silently picking one.
_SUBMAP_MUSIC = (0x048D8A, 0x04DBC8)

#: Levels, and bytes per name.
_LEVEL_COUNT = 0x200
_NAME_LENGTH = 19

#: The bypass record, and where its payload sits inside it.
_BYPASS = (0x40, 0x60)
_TERMINATOR = 0xFF

#: A level's primary header, and the byte holding the music field.
_HEADER_LENGTH = 5
_MUSIC_FIELD_BYTE = 2

#: The font has four alphabets, not one: capitals, lowercase, and **two**
#: separate runs of digits.
#:
#: Each was read off a name whose spelling is known. ``$40``–``$59`` is
#: lowercase, from *First Castle Part 2*, which is why the reference tool shows
#: some level names in mixed case — those really are mixed case in the ROM.
#: ``$22`` starts digits, from *Rexen86*; ``$63`` starts them again, from
#: *Level 7* and *Chocolate Island 1*. ``$5A`` is inferred from
#: *#3 Lemmy's Castle*.
_TILES = {
    0x1A: "!", 0x1B: ",", 0x1C: "-", 0x1D: ".", 0x1E: "?", 0x1F: " ",
    0x5A: "#", 0x5D: "'", 0xFC: " ",
}
_TILES.update({tile: chr(ord("A") + tile) for tile in range(0x00, 0x1A)})
_TILES.update({tile: chr(ord("a") + tile - 0x40) for tile in range(0x40, 0x5A)})
_TILES.update({tile: chr(ord("0") + tile - 0x22) for tile in range(0x22, 0x2C)})
_TILES.update({tile: chr(ord("0") + tile - 0x63) for tile in range(0x63, 0x6D)})

#: Lunar Magic's level-name editor calls these **MultiChar Tiles**: one tile
#: carrying more than one letter, so a long word fits a 19-byte field. Two of
#: SMW's own names need them.
#:
#: Confirmed by what they produce rather than by reading the tile sheet: with
#: these, every affected name in all three ROMs decodes to a real Super Mario
#: World level name — *Forest of Illusion 1* through *4*, and the green, yellow,
#: blue and red switch palaces.
_TILES.update({
    0x32: " I", 0x33: "L", 0x34: "L", 0x35: "U", 0x36: "S", 0x37: "I",
    0x38: "Y", 0x39: "E", 0x3A: "L", 0x3B: "L", 0x3C: "OW",
})

#: Stands in for a tile with no known character.
#:
#: Deliberately **not** dropped. Dropping renders *Rexen86* as *Rexen*, which is
#: a different level name and looks perfectly correct — the loss is invisible.
#: A placeholder at least shows that something is missing.
UNKNOWN_TILE = "…"


@dataclass(frozen=True)
class Level:
    """One level's music and name.

    Attributes:
        number: Level number, ``$000``–``$1FF``.
        track: The AddmusicK song ID it plays.
        name: What the overworld calls it, or ``""`` for a level with no
            overworld entry — a sublevel reached through a pipe has none.
        bypassed: Whether the track came from Lunar Magic's per-level record
            rather than from the header's three bits.
    """

    number: int
    track: int
    name: str = ""
    bypassed: bool = False

    @property
    def label(self) -> str:
        """How the level reads in a list: its name, or its number in hex."""
        return self.name or "%X" % self.number


def _decode_name(raw: bytes) -> str:
    """Decode one 19-byte name field, marking tiles this does not know."""
    text = "".join(_TILES.get(value, UNKNOWN_TILE) for value in raw)
    return " ".join(text.split())


def _translevel(level: int) -> int:
    """The overworld index a level's name is filed under."""
    return level if level < 0x100 else 0x25 + (level - 0x101)


def _read_names(rom: RomImage) -> Dict[int, str]:
    """Return {level number: name} for every level the overworld names."""
    base = rom.to_pc(rom.read_long(_NAME_POINTER))
    if base is None or not 0 < base < len(rom):
        return {}
    names: Dict[int, str] = {}
    for translevel in range(0x60):
        at = base + translevel * _NAME_LENGTH
        if at + _NAME_LENGTH > len(rom):
            break
        name = _decode_name(rom.data[at:at + _NAME_LENGTH])
        if name:
            level = translevel if translevel < 0x25 else 0x101 + (translevel - 0x25)
            names[level] = name
    return names


def _layer1_start(rom: RomImage, level: int) -> Optional[int]:
    """PC offset of a level's Layer-1 data, or ``None``."""
    table = rom.to_pc(_LAYER1_POINTERS)
    if table is None:
        return None
    start = rom.to_pc(rom.read_long(table + 3 * level))
    if start is None or not 0 < start < len(rom):
        return None
    return start


def _block_starts(rom: RomImage) -> List[int]:
    """Every address a Layer-1 block begins at, ascending."""
    found = set()
    for level in range(_LEVEL_COUNT):
        start = _layer1_start(rom, level)
        if start is not None:
            found.add(start)
    return sorted(found)


def _bypass_track(data: bytes) -> Optional[int]:
    """The track from a ``40 60 vv`` record, or ``None`` if there is none.

    The record is the last object before the terminator. SMW's object stream
    mixes three- and four-byte objects and no parse of it terminated every test
    level exactly, so the record is found by its shape rather than by walking
    to the end.
    """
    for i in range(len(data) - 3):
        if (data[i] == _BYPASS[0] and data[i + 1] == _BYPASS[1]
                and data[i + 3] == _TERMINATOR and data[i + 2] != 0):
            return data[i + 2] - 1      # Lunar Magic stores the value plus one
    return None


def read_levels(rom: RomImage) -> Tuple[Level, ...]:
    """Read every level's music and name.

    Args:
        rom: The ROM to read.

    Returns:
        One :class:`Level` per level that has Layer-1 data, ascending by number.
        Empty when the ROM has no readable level table at all.
    """
    table = rom.to_pc(_MUSIC_TRANSLATION)
    if table is None or table + 8 > len(rom):
        return ()
    translation = rom.data[table:table + 8]
    names = _read_names(rom)
    starts = _block_starts(rom)

    levels: List[Level] = []
    for number in range(_LEVEL_COUNT):
        start = _layer1_start(rom, number)
        if start is None:
            continue
        index = bisect.bisect_right(starts, start)
        end = starts[index] if index < len(starts) else len(rom)
        track = _bypass_track(rom.data[start + _HEADER_LENGTH:end])
        bypassed = track is not None
        if track is None:
            field = (rom.data[start + _MUSIC_FIELD_BYTE] >> 4) & 7
            track = translation[field]
        levels.append(Level(
            number=number,
            track=track,
            name=names.get(number, ""),
            bypassed=bypassed,
        ))
    return tuple(levels)


def read_submaps(rom: RomImage) -> Dict[int, Tuple[str, ...]]:
    """Read which song each of the seven overworld maps plays.

    A map is not a level and has no level number, so it cannot go through
    :func:`read_levels` — but a song only a map plays would otherwise show
    nothing at all.

    Args:
        rom: The ROM to read.

    Returns:
        Song ID to the maps playing it, in :data:`SUBMAP_NAMES` order. Empty
        when the table cannot be read or its two copies disagree.
    """
    tables = []
    for snes in _SUBMAP_MUSIC:
        at = rom.to_pc(snes)
        if at is None or at + len(SUBMAP_NAMES) > len(rom):
            return {}
        tables.append(bytes(rom.data[at:at + len(SUBMAP_NAMES)]))
    if len(set(tables)) != 1:
        return {}

    playing: Dict[int, List[str]] = {}
    for name, track in zip(SUBMAP_NAMES, tables[0]):
        playing.setdefault(track, []).append(name)
    return {track: tuple(names) for track, names in playing.items()}


def levels_by_track(levels: Sequence[Level]) -> Dict[int, Tuple[Level, ...]]:
    """Group levels by the song they play.

    Args:
        levels: The result of :func:`read_levels`.

    Returns:
        Track number to the levels playing it, each ascending by level number.
    """
    grouped: Dict[int, List[Level]] = {}
    for level in levels:
        grouped.setdefault(level.track, []).append(level)
    return {track: tuple(found) for track, found in grouped.items()}


def describe(
    levels: Sequence[Level],
    maps: Sequence[str] = (),
    limit: int = 6,
) -> str:
    """Render where one song plays, the way the target format reads.

    Maps come first, then named levels, then the rest by number — roughly the
    order of how recognisable they are.

    Args:
        levels: Levels playing the song.
        maps: Overworld maps playing it, from :func:`read_submaps`.
        limit: How many to name before summarising the rest.

    Returns:
        Something like ``FINAL DESTINATION (10), 11, 13``, or ``""``.
    """
    ordered = sorted(levels, key=lambda level: (not level.name, level.number))
    shown = list(maps) + [
        "%s (%X)" % (level.name, level.number) if level.name else "%X" % level.number
        for level in ordered
    ]
    if not shown:
        return ""
    if len(shown) > limit:
        return ", ".join(shown[:limit] + ["+%d more" % (len(shown) - limit)])
    return ", ".join(shown)
