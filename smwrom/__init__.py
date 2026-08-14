"""Reading Super Mario World ROM hacks.

The entry point is :func:`read_rom`, which returns every song a hack's
AddmusicK data describes:

    >>> from smwrom import read_rom
    >>> music = read_rom("Dreams.smc")            # doctest: +SKIP
    >>> len(music.songs)                          # doctest: +SKIP
    57

Naming the songs is a separate step — see the matcher. This package only reads
what is actually in the ROM.

The research behind the formats is in ``docs/research/rom-music-extraction/``;
``FINDINGS.md`` supersedes ``HANDOFF.md`` where they disagree.
"""

from smwrom.fingerprints import FingerprintDatabase
from smwrom.mapping import RomImage
from smwrom.matching import (
    EXACT,
    NOTES,
    SIMILAR,
    UNMATCHED,
    Candidate,
    Match,
    Matcher,
)
from smwrom.music import (
    CorruptMusicDataError,
    NoCustomMusicError,
    RomMusic,
    RomMusicError,
    Song,
    read_music,
    read_rom,
)

__all__ = [
    "EXACT",
    "NOTES",
    "SIMILAR",
    "UNMATCHED",
    "Candidate",
    "CorruptMusicDataError",
    "FingerprintDatabase",
    "Match",
    "Matcher",
    "NoCustomMusicError",
    "RomImage",
    "RomMusic",
    "RomMusicError",
    "Song",
    "read_music",
    "read_rom",
]
