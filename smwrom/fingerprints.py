"""The instrument fingerprint database, and the one way to build a fingerprint.

A song's instrument table is six bytes per instrument. The first is a sample
index and belongs to whichever hack or upload it came from; the other five —
ADSR1, ADSR2, GAIN, tuning high, tuning low — travel with the song. Hash those
and a song found in a ROM can be named without downloading anything.

Both sides of the comparison live here on purpose. The ROM side reads compiled
bytes and the catalog side reads AddmusicK's ``#instruments`` block out of an
MML source file, and the whole scheme rests on those producing identical bytes.
Keeping them in one module means a change to one is a change in front of the
other.

Reading the MML rather than compiling it is what keeps AddmusicK out of the
build entirely — no compiler, no version matrix.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: The bytes of an instrument that are not the sample index.
TAIL_SIZE = 5

#: What the database is called wherever it lives.
DATABASE_NAME = "music_fingerprints.json.gz"

#: Where to look, in order. ``data/`` comes first so a database fetched later
#: replaces the one shipped inside the build; ``dist_catalog/`` is the build
#: tool's own output and only matters in a source checkout.
_SEARCH_DIRS = ("data", None, "dist_catalog")

#: Bumped when the on-disk layout changes in a way an old reader cannot handle.
#: 2 gave each entry a *list* of songs: 1,229 catalog entries ship more than one
#: — two soundtrack bundles hold 310 and 172 — and keeping only the largest left
#: the rest unmatchable.
#: 3 added note-stream keys, the only handle on the 1,771 entries that declare
#: no instruments at all.
SCHEMA_VERSION = 3

_INSTRUMENT_BLOCK = re.compile(r"#instruments\s*\{(.*?)\}", re.DOTALL | re.IGNORECASE)
_HEX_BYTE = re.compile(r"\$([0-9a-fA-F]{2})")


def find_database() -> str:
    """Return the path to the fingerprint database, or ``""`` if there is none.

    A packaged build carries the database inside its bundle, which PyInstaller
    unpacks to ``sys._MEIPASS`` — not to the working directory, so looking only
    beside the executable finds nothing. A source checkout has it wherever
    ``tools/build_music_fingerprints.py`` last wrote it.

    Returns:
        A path that exists, or ``""``.
    """
    bundle = getattr(sys, "_MEIPASS", "") if getattr(sys, "frozen", False) else ""
    for directory in _SEARCH_DIRS:
        if directory is None:
            if not bundle:
                continue
            candidate = os.path.join(bundle, DATABASE_NAME)
        else:
            candidate = os.path.join(directory, DATABASE_NAME)
        if os.path.isfile(candidate):
            return candidate
    return ""


def parse_instrument_tails(mml: str) -> Tuple[bytes, ...]:
    """Extract instrument tails from an AddmusicK MML source file.

    Args:
        mml: The contents of a song's ``.txt``.

    Returns:
        One 5-byte tail per instrument, in declaration order. Empty when the
        file declares no instruments, which means the song uses the stock
        sample set — a real and common case, not an error.
    """
    block = _INSTRUMENT_BLOCK.search(mml)
    if block is None:
        return ()
    tails: List[bytes] = []
    for line in block.group(1).splitlines():
        line = line.split(";", 1)[0].strip()
        if not line:
            continue
        values = _HEX_BYTE.findall(line)
        if len(values) >= TAIL_SIZE:
            tails.append(bytes(int(v, 16) for v in values[-TAIL_SIZE:]))
    return tuple(tails)


def fingerprint(tails: Sequence[bytes]) -> str:
    """Hash instrument tails into a lookup key.

    Args:
        tails: Instrument tails in declaration order.

    Returns:
        A hex SHA-1, or ``""`` when there are no tails — a song on the stock
        sample set has no instrument fingerprint, and must not be given one
        that collides with every other such song.
    """
    if not tails:
        return ""
    return hashlib.sha1(b"".join(tails)).hexdigest()


@dataclass(frozen=True)
class Entry:
    """One catalog entry's fingerprints — one per song in its archive.

    Most uploads are a single song, but 1,229 of the 9,641 catalog entries hold
    more than one, and the two soundtrack bundles hold 310 and 172. So this is a
    list, not a single fingerprint.

    Attributes:
        entry_id: The SMWC entry ID, as a string.
        songs: One tuple of instrument tails per song that declares any.
        aram_size: Block size from the catalog, used to break ties. It describes
            the entry rather than any one song, so it is only a hint for a
            multi-song upload.
        note_keys: Note-stream keys read out of the entry's SPCs. Several per
            file: the song's position in the ARAM dump can only be narrowed to
            eight or ten candidates, and the wrong ones are harmless because a
            junk stream matches no real song. Keeping only the likeliest was
            measured and drops recovery from 55 of 62 to 3 of 62.
    """

    entry_id: str
    songs: Tuple[Tuple[bytes, ...], ...]
    aram_size: int
    note_keys: Tuple[str, ...] = ()

    @property
    def fingerprints(self) -> Tuple[str, ...]:
        """A lookup key per song, skipping any that declare no instruments."""
        return tuple(fingerprint(tails) for tails in self.songs if tails)


@dataclass
class FingerprintDatabase:
    """Fingerprints for every catalog song that declares instruments.

    Attributes:
        entries: Entry ID to :class:`Entry`.
        built: Unix timestamp of the newest source file the build read — not
            the moment it ran. A wall clock would make every rebuild of an
            unchanged library a different file, and this one gets published.
        by_fingerprint: Lookup key to every entry ID sharing it. A key maps to
            a *list*: the same port gets uploaded more than once, and a
            measured build has 75 keys covering 180 entries.
    """

    entries: Dict[str, Entry] = field(default_factory=dict)
    built: float = 0.0
    by_fingerprint: Dict[str, List[str]] = field(default_factory=dict, repr=False)
    by_note_key: Dict[str, List[str]] = field(default_factory=dict, repr=False)

    def add(self, entry: Entry) -> None:
        """Add an entry and index every song in it, by both kinds of key.

        Re-adding an ID replaces it rather than indexing it twice: a duplicate
        in the index would make :meth:`Matcher.match` report two candidates for
        one catalog entry, and an unambiguous match would reach the user marked
        uncertain.
        """
        if not entry.fingerprints and not entry.note_keys:
            return
        if entry.entry_id in self.entries:
            self._unindex(entry.entry_id)
        self.entries[entry.entry_id] = entry
        self._index(entry)

    def _index(self, entry: Entry) -> None:
        """Add *entry* to both indexes."""
        for index, keys in ((self.by_fingerprint, entry.fingerprints),
                            (self.by_note_key, entry.note_keys)):
            for key in keys:
                if not key:
                    continue
                ids = index.setdefault(key, [])
                if entry.entry_id not in ids:
                    ids.append(entry.entry_id)

    def _unindex(self, entry_id: str) -> None:
        """Drop every index reference to *entry_id*."""
        for index in (self.by_fingerprint, self.by_note_key):
            for key in list(index):
                ids = index[key]
                if entry_id in ids:
                    ids.remove(entry_id)
                    if not ids:
                        del index[key]

    def lookup(self, key: str) -> List[str]:
        """Return every entry ID with this instrument fingerprint."""
        if not key:
            return []
        return list(self.by_fingerprint.get(key, ()))

    def lookup_notes(self, key: str) -> List[str]:
        """Return every entry ID with this note-stream key."""
        if not key:
            return []
        return list(self.by_note_key.get(key, ()))

    def reindex(self) -> None:
        """Rebuild both indexes from :attr:`entries`."""
        self.by_fingerprint = {}
        self.by_note_key = {}
        for entry_id in sorted(self.entries):
            self._index(self.entries[entry_id])

    def to_json(self) -> Dict[str, object]:
        """Return the database as a JSON-serialisable dict.

        Entries are written in sorted order so that rebuilding an unchanged
        library produces a byte-identical file.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "built": self.built,
            "entries": {
                entry_id: {
                    "songs": [
                        b"".join(tails).hex()
                        for tails in self.entries[entry_id].songs
                        if tails
                    ],
                    "aram_size": self.entries[entry_id].aram_size,
                    "notes": sorted(k for k in self.entries[entry_id].note_keys if k),
                }
                for entry_id in sorted(self.entries)
            },
        }

    def save(self, path: str) -> None:
        """Write the database as gzipped JSON.

        The gzip header carries neither a timestamp nor the source file name,
        so rebuilding an unchanged library produces a byte-identical file and
        publishing does not churn. Both have to be suppressed explicitly:
        ``GzipFile`` writes the name it was opened with, which would otherwise
        embed the temporary file's name.

        Args:
            path: Destination file. Written to ``path + ".tmp"`` first and then
                moved into place, as every other data file in this app is.
        """
        import os
        import shutil

        payload = json.dumps(self.to_json(), separators=(",", ":"), sort_keys=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as handle:
                handle.write(payload.encode("utf-8"))
        if os.path.exists(path):
            os.replace(tmp, path)
        else:
            shutil.move(tmp, path)

    @classmethod
    def load(cls, path: str) -> "FingerprintDatabase":
        """Read a database written by :meth:`save`.

        Args:
            path: The gzipped JSON file.

        Returns:
            The database.

        Raises:
            OSError: If the file cannot be read.
            ValueError: If it is not a fingerprint database this code
                understands.
        """
        with gzip.GzipFile(path, "rb") as handle:
            raw = json.loads(handle.read().decode("utf-8"))
        if not isinstance(raw, dict) or "entries" not in raw:
            raise ValueError("%s is not a fingerprint database" % path)
        version = raw.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                "fingerprint database is schema version %r, this build reads %d"
                % (version, SCHEMA_VERSION)
            )
        database = cls(built=float(raw.get("built") or 0.0))
        for entry_id, row in raw["entries"].items():
            songs = []
            for encoded in row.get("songs") or ():
                blob = bytes.fromhex(encoded)
                songs.append(tuple(
                    blob[i:i + TAIL_SIZE] for i in range(0, len(blob), TAIL_SIZE)
                ))
            database.entries[entry_id] = Entry(
                entry_id=entry_id,
                songs=tuple(songs),
                aram_size=int(row.get("aram_size") or 0),
                note_keys=tuple(row.get("notes") or ()),
            )
        database.reindex()
        return database


def parse_aram_size(text: Optional[str]) -> int:
    """Parse a catalog ``aram_size`` field such as ``"0x0473 bytes"``.

    Args:
        text: The field's value, or ``None``.

    Returns:
        The size in bytes, or ``0`` when it cannot be read.
    """
    if not text:
        return 0
    try:
        return int(str(text).split()[0], 16)
    except (ValueError, IndexError):
        return 0
