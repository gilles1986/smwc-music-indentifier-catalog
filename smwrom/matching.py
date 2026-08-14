"""Naming a song found in a ROM.

Three passes, in this order, and the order is the whole design:

1. **Exact.** The instrument fingerprint, looked up in the local database.
2. **Size, as a tie-break.** One fingerprint can name several catalog entries —
   the same port uploaded twice, or two arrangements sharing a sample set. The
   block size usually separates them.
3. **Similarity.** No exact hit means the song was edited, so compare instrument
   tails as a multiset and take the best above a threshold.

An earlier design had size filtering first, to avoid downloading candidate
archives at import time. With the database local there are no downloads to
avoid, and putting size first is actively harmful: `Cubes Kaizo World 5.smc` has
eight songs whose block size matches no catalog entry at all, and the instrument
fingerprint finds almost all of them exactly. They are on SMWCentral, compiled
with a different AddmusicK version — which moves the size and leaves the
instruments alone.

An exact hit is never displaced by a similar one.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from smwrom.fingerprints import FingerprintDatabase
from smwrom.music import RomMusic, Song
from smwrom.notestream import stream_of_block

#: How alike two instrument sets must be before a similar match is offered.
#: Tuned on three ROMs, where the two correct fuzzy hits scored 0.86 and 0.91.
SIMILARITY_THRESHOLD = 0.6

#: Added to a similarity score when the candidate's block size matches exactly.
#: Enough to order near-ties, not enough to lift a poor match over the bar.
SIZE_BONUS = 0.15

#: Below this many instruments, a fingerprint must be corroborated by the block
#: size before it counts.
#:
#: One instrument is five bytes, which is not enough to name anything on its
#: own. Measured across the three test ROMs: every exact hit resting on a single
#: instrument whose size disagreed was **wrong** — both were the stock
#: ``IntroScreen`` song, matched to unrelated catalog jingles. The one whose size
#: agreed was right (*Konami Logo*). Every hit with a disagreeing size and more
#: than one instrument had at least five and was correct — those are songs
#: compiled with a different AddmusicK version, where the size moves and the
#: instruments do not.
#:
#: So the bar sits where the evidence puts it. Whether two- or three-instrument
#: fingerprints also need corroborating is untested: no sample here disagreed on
#: size, so there was nothing to learn from.
MIN_UNCORROBORATED_INSTRUMENTS = 2

#: Below this many note-stream bytes, a note match must be corroborated by the
#: block size before it counts.
#:
#: The same argument as above, for the other kind of evidence. A song's note
#: stream runs to 707 bytes at the median across the three test ROMs, and only
#: five of 159 fall below 64 — but the database holds about 99,000 note keys,
#: most of them from candidate positions that are not songs at all, so a handful
#: of note events is not enough to pick one out. Long streams are unaffected,
#: which is nearly all of them.
MIN_UNCORROBORATED_STREAM = 64

EXACT = "exact"
SIMILAR = "similar"
UNMATCHED = "unmatched"

#: Matched on the music rather than the instruments. The only thing that can
#: name a song built on the stock sample set, and a second chance for one whose
#: instrument table was edited.
NOTES = "notes"


@dataclass(frozen=True)
class Candidate:
    """One catalog entry a song might be.

    Attributes:
        entry_id: The SMWC entry ID.
        score: ``1.0`` for an exact fingerprint hit, otherwise the similarity.
        same_size: Whether the entry's ARAM size equals the ROM block's.
    """

    entry_id: str
    score: float
    same_size: bool


@dataclass(frozen=True)
class Match:
    """What could be established about one song.

    Attributes:
        song: The song as read from the ROM.
        kind: :data:`EXACT`, :data:`SIMILAR` or :data:`UNMATCHED`.
        candidates: Best first. Empty when unmatched.
    """

    song: Song
    kind: str
    candidates: Tuple[Candidate, ...]

    @property
    def best(self) -> Optional[Candidate]:
        """The leading candidate, or ``None`` when there is none."""
        return self.candidates[0] if self.candidates else None

    @property
    def is_certain(self) -> bool:
        """Whether exactly one entry matched, on evidence that admits no doubt.

        A note-stream hit counts: the stream is the music itself, and two
        different songs sharing one byte for byte are the same song. Anything
        else — a similar match, or a key shared by several entries — has to
        reach the user marked as uncertain.
        """
        return self.kind in (EXACT, NOTES) and len(self.candidates) == 1

    @property
    def is_sample_less(self) -> bool:
        """Whether the song is built on the stock sample set.

        It has no instrument fingerprint, so only its notes can name it. That
        succeeds often but not always, which is why this stays worth reporting:
        an unmatched sample-less song failed for a different reason than an
        unmatched one that brought its own samples.
        """
        return not self.song.declares_instruments


def _jaccard(left: Sequence[bytes], right: Sequence[bytes]) -> float:
    """Return multiset Jaccard similarity of two instrument tail sequences.

    A multiset rather than a set: a song using the same instrument settings
    three times is not the same as one using them once, and order is dropped
    because an edit that reorders instruments does not change the music.
    """
    if not left or not right:
        return 0.0
    a = collections.Counter(left)
    b = collections.Counter(right)
    intersection = sum((a & b).values())
    union = sum((a | b).values())
    return intersection / union if union else 0.0


class Matcher:
    """Names songs, given a fingerprint database.

    The similarity index is built on first use, so a session where every song
    matches exactly never pays for it.
    """

    def __init__(self, database: FingerprintDatabase) -> None:
        """Wrap a database.

        Args:
            database: The fingerprint database to match against.
        """
        self.database = database
        self._tails: Optional[Dict[str, Tuple[Tuple[bytes, ...], ...]]] = None

    def _similarity_index(self) -> Dict[str, Tuple[Tuple[bytes, ...], ...]]:
        """Return entry ID to its songs' instrument tails, building it once."""
        if self._tails is None:
            self._tails = {
                entry_id: tuple(tails for tails in entry.songs if tails)
                for entry_id, entry in self.database.entries.items()
                if any(entry.songs)
            }
        return self._tails

    def _is_credible(self, song: Song, same_size: bool) -> bool:
        """Whether a song carries enough instruments to be named on its own.

        A fingerprint built from one instrument is five bytes and collides with
        unrelated jingles. When there is that little to go on, the block size
        has to agree as well. See :data:`MIN_UNCORROBORATED_INSTRUMENTS`.
        """
        if len(song.instrument_tails) >= MIN_UNCORROBORATED_INSTRUMENTS:
            return True
        return same_size

    def _exact(self, song: Song) -> List[Candidate]:
        """Return exact fingerprint hits, size-matching ones first."""
        hits = self.database.lookup(song.fingerprint)
        if not hits:
            return []
        candidates = [
            candidate
            for candidate in (
                Candidate(
                    entry_id=entry_id,
                    score=1.0,
                    same_size=self.database.entries[entry_id].aram_size == song.size,
                )
                for entry_id in hits
            )
            if self._is_credible(song, candidate.same_size)
        ]
        if not candidates:
            return []
        same_size = [c for c in candidates if c.same_size]
        # Size only decides between entries that already agree on instruments,
        # so it is safe to drop the rest once it separates them.
        if len(same_size) == 1:
            return same_size
        candidates.sort(key=lambda c: (not c.same_size, c.entry_id))
        return candidates

    def _by_notes(self, song: Song) -> List[Candidate]:
        """Return entries whose music is byte-for-byte this song's.

        Runs after the instrument fingerprint but before similarity: identical
        notes are stronger evidence than similar instruments, and it is the only
        evidence there is for a song on the stock sample set.
        """
        hits = self.database.lookup_notes(song.note_fingerprint)
        if not hits:
            return []
        stream = stream_of_block(song.data, song.aram_base)
        thin = stream is None or len(stream) < MIN_UNCORROBORATED_STREAM
        candidates = [
            candidate
            for candidate in (
                Candidate(
                    entry_id=entry_id,
                    score=1.0,
                    same_size=self.database.entries[entry_id].aram_size == song.size,
                )
                for entry_id in hits
            )
            if candidate.same_size or not thin
        ]
        if not candidates:
            return []
        same_size = [c for c in candidates if c.same_size]
        if len(same_size) == 1:
            return same_size
        candidates.sort(key=lambda c: (not c.same_size, c.entry_id))
        return candidates

    def _similar(self, song: Song, limit: int) -> List[Candidate]:
        """Return the best similar candidates above the threshold."""
        scored: List[Tuple[float, Candidate]] = []
        for entry_id, songs in self._similarity_index().items():
            # An upload can hold many songs; it is as similar as its closest one.
            score = max(_jaccard(song.instrument_tails, tails) for tails in songs)
            if score < SIMILARITY_THRESHOLD:
                continue
            same_size = self.database.entries[entry_id].aram_size == song.size
            if not self._is_credible(song, same_size):
                continue
            ranked = score + (SIZE_BONUS if same_size else 0.0)
            scored.append((ranked, Candidate(entry_id, score, same_size)))
        scored.sort(key=lambda pair: (-pair[0], pair[1].entry_id))
        return [candidate for _ranked, candidate in scored[:limit]]

    def match(self, song: Song, limit: int = 5) -> Match:
        """Name one song.

        Args:
            song: A song read out of a ROM.
            limit: How many alternatives to keep for a similar match.

        Returns:
            The match, which is always an object — an unrecognised song is a
            result, not an error.
        """
        if song.declares_instruments:
            exact = self._exact(song)
            if exact:
                return Match(song=song, kind=EXACT, candidates=tuple(exact))

        notes = self._by_notes(song)
        if notes:
            return Match(song=song, kind=NOTES, candidates=tuple(notes))

        if song.declares_instruments:
            similar = self._similar(song, limit)
            if similar:
                return Match(song=song, kind=SIMILAR, candidates=tuple(similar))

        return Match(song=song, kind=UNMATCHED, candidates=())

    def match_all(self, songs: Iterable[Song], limit: int = 5) -> Tuple[Match, ...]:
        """Name every song in a ROM.

        Args:
            songs: The songs, typically ``RomMusic.songs``.
            limit: How many alternatives to keep per similar match.

        Returns:
            One match per song, in the order given.
        """
        return tuple(self.match(song, limit) for song in songs)

    def match_rom(self, music: RomMusic, limit: int = 5) -> Tuple[Match, ...]:
        """Name every song a ROM contains.

        Args:
            music: The result of reading a ROM.
            limit: How many alternatives to keep per similar match.

        Returns:
            One match per song.
        """
        return self.match_all(music.songs, limit)
