"""Fingerprinting a song by its notes, for songs that declare no instruments.

The instrument fingerprint cannot see a song built on the stock sample set —
there is no instrument table to hash. Those are not rare: 1,771 catalog entries
and roughly two songs per hack. What is left is the music itself.

The stream keeps notes, ties, rests, percussion and note lengths, and drops
every command in ``$DA``–``$FF`` using AddmusicK's own length table. That is what
makes it survive an edit: volume, tempo, echo and instrument changes are all
commands, so changing them changes no byte of the stream. It is also what makes
Bowser Scene 2 and 3 indistinguishable — they differ only in ``t65`` versus
``t70``, and tempo is a command.

The one thing worth stating twice, because getting it wrong produces a stream
that matches nothing and looks like a dead end: **channel data starts at
``max(phrases) + 16``**, past the last phrase's eight channel pointers. Starting
at ``min(phrases)`` reads those pointers as if they were notes.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional, Sequence, Tuple

#: Total length of each command, indexed by ``opcode - 0xDA``. Lifted from
#: AddmusicK's ``asm/CommandTable.asm``; the final entry is ``$FF``, which the
#: engine treats as a no-op remap and which we skip one byte at a time.
COMMAND_LENGTHS = (
    0x02, 0x02, 0x03, 0x04, 0x04, 0x01,
    0x02, 0x03, 0x02, 0x03, 0x02, 0x03, 0x02, 0x02,
    0x03, 0x04, 0x02, 0x04, 0x04, 0x03, 0x02, 0x04,
    0x01, 0x04, 0x04, 0x03, 0x02, 0x09, 0x03, 0x04,
    0x02, 0x03, 0x03, 0x03, 0x05, 0x01, 0x01, 0x00,
)

#: First command opcode. Below this are note lengths, notes and percussion.
FIRST_COMMAND = 0xDA

#: Eight channel pointers per phrase.
PHRASE_BYTES = 16

#: A song must have at least this many stream bytes before it is worth
#: fingerprinting. Short streams are mostly the by-product of a mis-located
#: block, and they collide with each other.
MIN_STREAM = 16

#: Hex characters kept from each key. A song's position in an SPC can only be
#: narrowed to eight or ten candidates, so the database holds about 99,000 of
#: these and their length is most of its size. 64 bits leaves the chance of any
#: two colliding at roughly one in four billion, against a full digest costing
#: two and a half times the bytes.
KEY_LENGTH = 16


def parse_phrases(block: bytes, at: int = 0) -> Tuple[List[int], int]:
    """Read a block's phrase list.

    Args:
        block: The song block's data.
        at: Where the list starts.

    Returns:
        The phrase addresses and the offset just past the list. The list ends at
        the first entry below ``$0100`` — a loop command, which carries one word
        of argument when it is not zero.
    """
    phrases: List[int] = []
    position = at
    while position + 1 < len(block):
        word = int.from_bytes(block[position:position + 2], "little")
        position += 2
        if word < 0x100:
            if word:
                position += 2
            break
        phrases.append(word)
        if len(phrases) > 128:
            return [], position
    return phrases, position


def channel_start(phrases: Sequence[int], aram_base: int) -> Optional[int]:
    """Where a block's channel data begins, as an offset into the block.

    Args:
        phrases: The phrase addresses.
        aram_base: Where the block loads.

    Returns:
        The offset, or ``None`` when there are no phrases.
    """
    if not phrases:
        return None
    return max(phrases) + PHRASE_BYTES - aram_base


def note_stream(block: bytes, start: int) -> bytes:
    """Reduce channel data to notes and durations.

    Args:
        block: The song block's data.
        start: Where channel data begins.

    Returns:
        Note, tie, rest, percussion and length bytes, with channel ends kept as
        separators. Commands contribute nothing — not even their length — so an
        edit to any of them leaves the stream untouched.
    """
    out = bytearray()
    position = start
    while 0 <= position < len(block):
        value = block[position]
        if value == 0x00:
            out.append(0x00)
            position += 1
        elif value < 0x80:
            out.append(value)
            position += 1
            # A length may be followed by a quantisation/velocity byte, which
            # says how the note is played rather than which note it is.
            if position < len(block) and block[position] < 0x80:
                position += 1
        elif value < FIRST_COMMAND:
            out.append(value)
            position += 1
        else:
            position += COMMAND_LENGTHS[value - FIRST_COMMAND] or 1
    return bytes(out)


def stream_of_block(block: bytes, aram_base: int) -> Optional[bytes]:
    """Note stream of a whole song block.

    Args:
        block: The block's data, without its 4-byte header.
        aram_base: Where the block loads.

    Returns:
        The stream, or ``None`` when the block has no phrase list or the
        channel data would start outside it.
    """
    phrases, _ = parse_phrases(block)
    start = channel_start(phrases, aram_base)
    if start is None or not 0 < start < len(block):
        return None
    return note_stream(block, start)


def fingerprint(stream: Optional[bytes]) -> str:
    """Hash a note stream into a lookup key.

    Args:
        stream: The stream, or ``None``.

    Returns:
        A hex SHA-1, or ``""`` when there is no stream or it is too short to
        identify anything.
    """
    if not stream or len(stream) < MIN_STREAM:
        return ""
    return hashlib.sha1(stream).hexdigest()[:KEY_LENGTH]
