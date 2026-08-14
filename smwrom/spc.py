"""Finding a song inside an SPC's memory dump.

An SPC file is a snapshot of the sound chip: a 256-byte header, then 64 KB of
ARAM with the driver, the samples and the song all sitting in it. The song is
what we want, and nothing says where it is.

It cannot be looked up. AddmusicK places the song after the samples, whose size
varies per upload, so the address varies too — 56 songs measured across the test
ROMs gave 28 distinct addresses, and no fixed ARAM word holds it.

So it is searched for, and the search has to be cheap enough to run over 13,419
files. The trick is that a block's first word is its first phrase pointer, which
sits one header above the block's own address. Requiring that gap to be small
rejects almost every offset with a single comparison, and the expensive
structural check then runs on the handful that survive: about 0.017 seconds per
file, four minutes over the corpus, eight to ten candidates each.

Only one candidate is the song. The others are kept anyway — a junk stream
matches no real song, and discarding them by any of the obvious heuristics loses
the real one far more often. Keeping the longest candidate alone was measured at
3 correct out of 62, against 55 out of 62 for keeping them all.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from smwrom.notestream import PHRASE_BYTES, fingerprint, parse_phrases, stream_of_block

#: Where the ARAM dump starts inside an SPC file, and how big it is.
ARAM_OFFSET = 0x100
ARAM_SIZE = 0x10000

#: Below this the driver lives, never a song.
FIRST_BASE = 0x400

#: The most a block's header — phrase list plus instrument table — can span
#: before its first phrase. Comfortably past the largest seen.
MAX_HEADER = 0x300


def read_aram(path: str) -> Optional[bytes]:
    """Read an SPC file's ARAM dump.

    Args:
        path: Path to a ``.spc`` file.

    Returns:
        The 64 KB dump, or ``None`` if the file is too short to hold one.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    if len(raw) < ARAM_OFFSET + ARAM_SIZE:
        return None
    return raw[ARAM_OFFSET:ARAM_OFFSET + ARAM_SIZE]


def _is_consistent(aram: bytes, base: int, size: int) -> bool:
    """Whether a block of *size* bytes at *base* parses as a song.

    Everything it points at has to land inside itself: phrase addresses, the
    phrase tables, and every live channel pointer. The instrument table has to
    be a whole number of instruments.
    """
    block = aram[base:base + size]
    phrases, header_end = parse_phrases(block)
    if not phrases:
        return False
    low, high = base, base + size
    if not all(low <= phrase < high for phrase in phrases):
        return False
    table_end = max(phrases) + PHRASE_BYTES
    if table_end > high:
        return False
    for phrase in phrases:
        offset = phrase - base
        channels = [
            int.from_bytes(block[offset + c * 2:offset + c * 2 + 2], "little")
            for c in range(8)
        ]
        live = [c for c in channels if c]
        if not live or not all(table_end <= c < high for c in live):
            return False
    return (min(phrases) - base - header_end) % 6 == 0


def candidate_bases(aram: bytes, size: int) -> List[int]:
    """Every ARAM address where a song of *size* bytes could begin.

    Args:
        aram: The 64 KB dump.
        size: The block size, from the catalog's ``aram_size``.

    Returns:
        The addresses, ascending. Typically eight to ten, only one of them the
        song.
    """
    if not 0 < size < ARAM_SIZE:
        return []
    found: List[int] = []
    limit = len(aram) - size
    for base in range(FIRST_BASE, limit):
        # One comparison rejects almost everything: the first word must be a
        # phrase pointer sitting a header's length above the base.
        word = aram[base] | (aram[base + 1] << 8)
        gap = word - base
        if gap < 4 or gap > MAX_HEADER or word >= base + size:
            continue
        if _is_consistent(aram, base, size):
            found.append(base)
    return found


def note_keys(aram: bytes, size: int) -> List[str]:
    """Note-stream keys for every candidate song position in a dump.

    Args:
        aram: The 64 KB dump.
        size: The block size, from the catalog.

    Returns:
        One key per candidate that yields a usable stream, deduplicated and
        sorted so a rebuild of unchanged input is byte-identical.
    """
    keys = set()
    for base in candidate_bases(aram, size):
        key = fingerprint(stream_of_block(aram[base:base + size], base))
        if key:
            keys.add(key)
    return sorted(keys)


def keys_for_files(paths: Sequence[str], size: int) -> List[str]:
    """Note-stream keys across several SPC files.

    Args:
        paths: Paths to ``.spc`` files, typically one entry's archive.
        size: The block size, from the catalog.

    Returns:
        Every key found, deduplicated and sorted.
    """
    keys = set()
    for path in paths:
        aram = read_aram(path)
        if aram is not None:
            keys.update(note_keys(aram, size))
    return sorted(keys)
