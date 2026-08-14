"""Bring the published fingerprint database up to date, one day's worth at a time.

``build_music_fingerprints.py`` builds the whole thing from a synced
``music_library/``, which only the maintainer has. This one needs no library: it
compares the live SMWCentral catalog against a database it already has, and
downloads **only the entries missing from it** — usually a handful a day.

That is what lets the database live in a repository and keep itself current from
a scheduled job, rather than waiting for somebody to run a full build and upload
the result.

Two things keep it from re-downloading the same archives forever. Entries that
yielded fingerprints are in the database and are obviously known. Entries that
yielded *nothing* — no instruments, no readable SPC — are recorded in a small
state file instead, because otherwise every run would try them again.

Usage::

    python tools/update_music_fingerprints.py \\
        --database music_fingerprints.json.gz \\
        --state checked.json \\
        --manifest manifest.json \\
        --base-url https://example.com/fingerprints/

See ``tasks/65_music_fingerprint_db.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from typing import Dict, List, Optional, Sequence, Set, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smwrom.fingerprints import (  # noqa: E402  (path set up above)
    Entry,
    FingerprintDatabase,
    parse_aram_size,
    parse_instrument_tails,
)
from smwrom.spc import ARAM_OFFSET, ARAM_SIZE, note_keys  # noqa: E402

#: Between downloads. SMWCentral is somebody else's bandwidth and this runs
#: unattended, so it goes slower than a person would.
REQUEST_DELAY = 1.0
DOWNLOAD_TIMEOUT = (10, 60)

#: The listing endpoint. Three parameters and a page counter.
SMWC_API = "https://www.smwcentral.net/ajax.php"

#: A polite, identifiable agent. An unattended job that looks like a browser is
#: harder for them to talk to if it ever misbehaves.
USER_AGENT = (
    "SMWCMusicIdentifier/1.0 (fingerprint index; "
    "https://github.com/gilles1986/smwc-music-indentifier-catalog)"
)

#: Files inside an archive that are never a song's MML source.
_NOT_SOURCES = frozenset({"readme.txt", "addmusic_sample groups.txt"})

#: Stop after this many new entries in one run, so a first run against an empty
#: database cannot turn into a day-long download.
DEFAULT_LIMIT = 200


def load_state(path: str) -> Set[str]:
    """Return the entry IDs already examined, whatever the outcome."""
    if not path or not os.path.isfile(path):
        return set()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return set()
    return {str(x) for x in (data.get("checked") or [])}


def save_state(path: str, checked: Set[str]) -> None:
    """Write the examined IDs, sorted so a diff is readable."""
    if not path:
        return
    payload = {"checked": sorted(checked, key=lambda v: (len(v), v))}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=0, sort_keys=True)


def fetch_catalog(session: requests.Session) -> List[dict]:
    """Walk the SMWCentral music listing.

    Deliberately not through ``music_api``: this runs in a scheduled job, and
    reaching into the player for one HTTP call would drag its configuration,
    its logging and its dependencies along. The listing endpoint is three
    parameters and a page counter.

    Args:
        session: The HTTP session, already carrying the user agent.

    Returns:
        One row per entry, newest first.

    Raises:
        ValueError: If the endpoint answers with something that is not a
            listing — better than silently indexing nothing.
    """
    rows: List[dict] = []
    page = 1
    while True:
        response = session.get(
            SMWC_API,
            params={"a": "getsectionlist", "s": "smwmusic", "n": page,
                    "o": "date", "d": "desc"},
            timeout=DOWNLOAD_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "data" not in data:
            raise ValueError("SMWCentral did not answer with a listing")
        batch = data.get("data") or []
        if not batch:
            break
        rows.extend(batch)
        pages = int(data.get("pages") or 1)
        if page >= pages:
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    return rows


def fingerprint_archive(payload: bytes, aram_size: int, entry_id: str) -> Optional[Entry]:
    """Fingerprint one entry straight out of its downloaded archive.

    Nothing is written to disk. The MML gives the instrument fingerprints and
    the SPCs give the note-stream keys, exactly as the full build does — the
    only difference is where the bytes come from.

    Args:
        payload: The ``.zip`` as downloaded.
        aram_size: Block size from the catalog.
        entry_id: The SMWC entry ID.

    Returns:
        The entry, or ``None`` when the archive yields nothing usable.
    """
    songs = set()
    keys: Set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for member in archive.namelist():
                name = os.path.basename(member).lower()
                if not name:
                    continue
                if name.endswith(".txt") and name not in _NOT_SOURCES:
                    tails = parse_instrument_tails(
                        archive.read(member).decode("latin1")
                    )
                    if tails:
                        songs.add(tails)
                elif name.endswith(".spc") and aram_size:
                    raw = archive.read(member)
                    if len(raw) >= ARAM_OFFSET + ARAM_SIZE:
                        keys.update(
                            note_keys(raw[ARAM_OFFSET:ARAM_OFFSET + ARAM_SIZE], aram_size)
                        )
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError):
        return None

    if not songs and not keys:
        return None
    return Entry(
        entry_id=entry_id,
        songs=tuple(sorted(songs)),
        aram_size=aram_size,
        note_keys=tuple(sorted(keys)),
    )


def download(session: requests.Session, url: str) -> Optional[bytes]:
    """Fetch one archive, returning ``None`` rather than raising."""
    try:
        response = session.get(url, timeout=DOWNLOAD_TIMEOUT)
        response.raise_for_status()
        return response.content
    except requests.RequestException:
        return None


def write_manifest(
    path: str, database_path: str, base_url: str, entries: int
) -> Dict[str, object]:
    """Write the manifest the player fetches, describing *database_path*.

    The artifact is published under a name carrying its schema and the date, so
    a published file is never overwritten — a client may be mid-download.
    """
    from smwrom.fingerprints import SCHEMA_VERSION

    payload = open(database_path, "rb").read()
    version = time.strftime("%Y-%m-%d")
    name = "music-%d-%s.json.gz" % (SCHEMA_VERSION, version.replace("-", ""))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "catalog_version": version,
        "url": base_url.rstrip("/") + "/" + name,
        "mirrors": [],
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "entries": entries,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    return manifest


def update(
    database_path: str,
    state_path: str,
    limit: int,
    catalog_path: str = "",
) -> Tuple[FingerprintDatabase, Dict[str, int]]:
    """Add whatever SMWCentral has that the database does not.

    Args:
        database_path: The database to extend. Created if absent.
        state_path: Where examined-but-empty IDs are remembered.
        limit: Most entries to add in one run.
        catalog_path: Read the catalog from here instead of the network.

    Returns:
        The database and a dict of counters.
    """
    try:
        database = FingerprintDatabase.load(database_path)
    except (OSError, ValueError):
        database = FingerprintDatabase()

    checked = load_state(state_path) | set(database.entries)

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    if catalog_path:
        with open(catalog_path, encoding="utf-8") as handle:
            rows = json.load(handle)["entries"]
    else:
        rows = fetch_catalog(session)

    missing = [row for row in rows if str(row.get("id", "")) not in checked]
    stats = {"catalog": len(rows), "missing": len(missing), "added": 0, "empty": 0,
             "failed": 0}

    for row in missing[:limit]:
        entry_id = str(row.get("id", ""))
        url = str(row.get("download_url", "") or "")
        if not entry_id or not url:
            continue
        print("  %-8s %s" % (entry_id, str(row.get("name", ""))[:56]), flush=True)
        payload = download(session, url)
        time.sleep(REQUEST_DELAY)
        if payload is None:
            stats["failed"] += 1
            continue

        entry = fingerprint_archive(
            payload, parse_aram_size(row.get("aram_size")), entry_id
        )
        # Recorded either way: an entry that yields nothing is still an entry
        # that has been looked at, and without this every run would try it.
        checked.add(entry_id)
        if entry is None:
            stats["empty"] += 1
            continue
        database.add(entry)
        stats["added"] += 1

    return database, stats, checked


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", default="music_fingerprints.json.gz")
    parser.add_argument("--state", default="checked.json")
    parser.add_argument("--manifest", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--catalog", default="", help="read the catalog from a file")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args(argv)

    try:
        database, stats, checked = update(
            args.database, args.state, args.limit, args.catalog
        )
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if stats["added"]:
        database.built = time.time()
        database.save(args.database)
    save_state(args.state, checked)

    print()
    print("catalog entries : %d" % stats["catalog"])
    print("not yet indexed : %d" % stats["missing"])
    print("added this run  : %d" % stats["added"])
    print("nothing usable  : %d" % stats["empty"])
    print("download failed : %d" % stats["failed"])
    print("database now    : %d entries" % len(database.entries))

    if args.manifest and args.base_url:
        manifest = write_manifest(
            args.manifest, args.database, args.base_url, len(database.entries)
        )
        print("manifest        : %s" % manifest["url"])

    # A run that added nothing is a normal quiet day, not a failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
