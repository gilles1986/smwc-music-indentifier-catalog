"""Reading the songs AddmusicK inserted into a hack.

AddmusicK leaves a header in the ROM that starts with the ASCII bytes ``@AMK``,
and from it everything else is reachable: a table of long pointers, one per song
ID, each naming a block of compiled sound-driver data.

Two habits here are not decoration. The first is scanning for ``@AMK`` instead
of reading the address the older identifier tools hardcode — it costs nothing
and survives a relocated or twice-patched header. The second is checking that
every decoded block is *plausible* before believing it. A wrong mapping does not
raise; it hands back block sizes like ``0x9D98`` and instrument counts like
5,628, and a list of confidently wrong songs is worse than an error.

What this module does **not** do is decide which songs are custom. An empty
instrument table means the song uses the stock sample set, which stock songs and
ports of stock-sample tracks both do — `Dreams.smc` has two ports that declare no
instruments at all. Every song is reported; deciding what to show is the
matcher's problem.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from smwrom.mapping import RomImage
from smwrom.notestream import fingerprint as note_fingerprint
from smwrom.notestream import parse_phrases, stream_of_block

#: The AddmusicK signature, and the offsets of the two pointers that follow it.
_SIGNATURE = b"@AMK"
_MUSIC_POINTERS = 8

#: Bytes per instrument in a song's instrument table. The first is a sample
#: index, which is specific to the hack's sample layout; the remaining five —
#: ADSR1, ADSR2, GAIN, tuning high, tuning low — travel with the song and are
#: what makes a fingerprint survive sample-ID remapping.
_INSTRUMENT_SIZE = 6
_INSTRUMENT_TAIL = 5

#: A song block is copied into the sound chip's 64 KB of ARAM, so anything near
#: that size is a decoding error rather than a very long song. The observed
#: maximum across the test ROMs is ``0x3073``.
_MAX_BLOCK = 0x4000

#: Where a block may be told to load. The engine sits at ``$0400``; songs land
#: above it — ``$2A52`` in one test ROM, ``$2B34`` in the other two.
_MIN_ARAM = 0x0400
_MAX_ARAM = 0xF000

#: Song IDs below this are the sound driver's globals and carry null pointers.
_FIRST_SONG = 0x0A

#: Where to stop walking the pointer table if no terminator appears. The table
#: runs to ``$4A`` in one test ROM, so the old ``0x40`` cap used by the
#: prototypes truncated a third of it.
_MAX_SONG_ID = 0x100


class RomMusicError(Exception):
    """Base class for every failure reading music out of a ROM."""


class NoCustomMusicError(RomMusicError):
    """The ROM carries no AddmusicK header.

    Either it has no custom music, or it was built with Addmusic 4.05 or
    AddmusicM, whose layout is entirely different and is not supported.
    """


class CorruptMusicDataError(RomMusicError):
    """A song block decoded to something impossible.

    Nearly always a mapping problem rather than a damaged ROM.
    """


@dataclass(frozen=True)
class Song:
    """One song as it exists in the ROM.

    Attributes:
        song_ids: Every song ID this block is reachable under, ascending. A
            hack can insert the same song twice; `Dreams.smc` does it at
            ``$15``/``$3C`` and ``$17``/``$2F``.
        size: Length of the block's data, excluding its 4-byte header.
        aram_base: Where the sound driver loads the block.
        instrument_tails: The five travelling bytes of each declared
            instrument, in order. Empty for a song on the stock sample set.
        data: The block's data.
    """

    song_ids: Tuple[int, ...]
    size: int
    aram_base: int
    instrument_tails: Tuple[bytes, ...]
    data: bytes

    @property
    def declares_instruments(self) -> bool:
        """Whether the song brings its own samples."""
        return bool(self.instrument_tails)

    @property
    def fingerprint(self) -> str:
        """SHA-1 over the instrument tails, or ``""`` when there are none."""
        if not self.instrument_tails:
            return ""
        return hashlib.sha1(b"".join(self.instrument_tails)).hexdigest()

    @property
    def note_fingerprint(self) -> str:
        """SHA-1 over the song's notes, or ``""`` when it cannot be read.

        The only handle on a song that declares no instruments. Also computed
        for songs that do declare them, since it costs nothing and gives the
        matcher a second chance when the instrument table was edited.
        """
        return note_fingerprint(stream_of_block(self.data, self.aram_base))

    @property
    def label(self) -> str:
        """The song IDs as AddmusicK and Lunar Magic write them, e.g. ``#15, #3C``."""
        return ", ".join("#%02X" % sid for sid in self.song_ids)


@dataclass(frozen=True)
class RomMusic:
    """Everything readable about a ROM's music.

    Attributes:
        songs: One entry per distinct song, ordered by first song ID.
        amk_offsets: File offsets of every ``@AMK`` signature found.
        is_sa1: Whether SA-1 banking was used to read the ROM.
        had_copier_header: Whether a 512-byte header was stripped.
    """

    songs: Tuple[Song, ...]
    amk_offsets: Tuple[int, ...]
    is_sa1: bool
    had_copier_header: bool


def _find_signatures(rom: RomImage) -> List[int]:
    """Return the file offset of every ``@AMK`` signature, in order."""
    found: List[int] = []
    at = rom.data.find(_SIGNATURE)
    while at >= 0:
        found.append(at)
        at = rom.data.find(_SIGNATURE, at + 1)
    return found


def _instrument_tails(block: bytes, aram_base: int) -> Tuple[bytes, ...]:
    """Extract the travelling bytes of each instrument the block declares.

    The block opens with a phrase list of 16-bit entries, terminated by the
    first entry below ``$0100`` — a loop command, which carries one word of
    argument. The instrument table runs from there to the lowest phrase
    address, six bytes per instrument.

    Args:
        block: The block's data, without its 4-byte header.
        aram_base: Where the block loads, so phrase addresses can be made
            relative.

    Returns:
        One 5-byte tail per instrument, in order. Empty when the song declares
        none, or when the header is too short to hold a phrase list.
    """
    phrases, at = parse_phrases(block)
    if not phrases:
        return ()

    table_end = min(phrase - aram_base for phrase in phrases)
    count = (table_end - at) // _INSTRUMENT_SIZE
    if count <= 0 or table_end > len(block):
        return ()
    return tuple(
        block[at + i * _INSTRUMENT_SIZE + 1:at + i * _INSTRUMENT_SIZE + _INSTRUMENT_SIZE]
        for i in range(count)
    )


def _read_block(rom: RomImage, pointer: int, song_id: int) -> Tuple[int, int, bytes]:
    """Decode one song block.

    Args:
        rom: The ROM being read.
        pointer: The block's SNES address, from the music pointer table.
        song_id: Which song ID this pointer sits under, for the error message.

    Returns:
        A tuple of block size, ARAM base, and the block's data.

    Raises:
        CorruptMusicDataError: If the pointer is unmapped, or the block's
            declared size or load address is impossible.
    """
    header = rom.read(pointer, 4)
    if header is None:
        raise CorruptMusicDataError(
            "song $%02X points at $%06X, which is not a ROM address" % (song_id, pointer)
        )
    size = int.from_bytes(header[0:2], "little")
    aram_base = int.from_bytes(header[2:4], "little")
    if not 0 < size < _MAX_BLOCK:
        raise CorruptMusicDataError(
            "song $%02X declares a block of 0x%04X bytes, which cannot fit in ARAM "
            "— the ROM mapping is probably wrong" % (song_id, size)
        )
    if not _MIN_ARAM <= aram_base < _MAX_ARAM:
        raise CorruptMusicDataError(
            "song $%02X loads to ARAM $%04X, which is outside the sound driver's "
            "range — the ROM mapping is probably wrong" % (song_id, aram_base)
        )
    data = rom.read(pointer + 4, size)
    if data is None:
        raise CorruptMusicDataError(
            "song $%02X runs past the end of the ROM" % song_id
        )
    return size, aram_base, data


def read_music(rom: RomImage) -> RomMusic:
    """Read every song a ROM's AddmusicK data describes.

    Songs stored under more than one ID are collapsed into a single entry
    naming all of them, matched on the block's contents rather than its
    address — the duplicates in `Dreams.smc` sit at different addresses.

    Args:
        rom: The ROM to read.

    Returns:
        The songs, in song-ID order, together with what was detected about the
        ROM itself.

    Raises:
        NoCustomMusicError: If the ROM has no AddmusicK header.
        CorruptMusicDataError: If a block decodes to something impossible.
    """
    signatures = _find_signatures(rom)
    if not signatures:
        raise NoCustomMusicError(
            "no AddmusicK header in this ROM — it has no custom music, or it was "
            "built with Addmusic 4.05 or AddmusicM, which are not supported"
        )

    table = rom.to_pc(rom.read_long(signatures[0] + _MUSIC_POINTERS))
    if table is None:
        raise CorruptMusicDataError("the AddmusicK header's music pointer is not a ROM address")

    by_content: Dict[bytes, List[int]] = {}
    decoded: Dict[bytes, Tuple[int, int, bytes]] = {}
    for song_id in range(_FIRST_SONG, _MAX_SONG_ID):
        pointer = rom.read_long(table + song_id * 3)
        if pointer == 0:
            continue
        if pointer == 0xFFFFFF:
            break
        size, aram_base, data = _read_block(rom, pointer, song_id)
        key = hashlib.sha1(data).digest()
        by_content.setdefault(key, []).append(song_id)
        decoded.setdefault(key, (size, aram_base, data))

    songs = [
        Song(
            song_ids=tuple(ids),
            size=decoded[key][0],
            aram_base=decoded[key][1],
            instrument_tails=_instrument_tails(decoded[key][2], decoded[key][1]),
            data=decoded[key][2],
        )
        for key, ids in by_content.items()
    ]
    songs.sort(key=lambda song: song.song_ids[0])

    return RomMusic(
        songs=tuple(songs),
        amk_offsets=tuple(signatures),
        is_sa1=rom.is_sa1,
        had_copier_header=rom.had_copier_header,
    )


def read_rom(path: str) -> RomMusic:
    """Read a ROM file's music.

    Args:
        path: Path to a ``.smc`` or ``.sfc`` file.

    Returns:
        The songs it contains.

    Raises:
        OSError: If the file cannot be read.
        NoCustomMusicError: If the ROM has no AddmusicK header.
        CorruptMusicDataError: If a block decodes to something impossible.
    """
    return read_music(RomImage.from_file(path))
