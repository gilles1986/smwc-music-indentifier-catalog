# The fingerprint catalog repository

What goes into <https://github.com/gilles1986/smwc-music-indentifier-catalog>,
and how it keeps itself current.

> The repository name says **`indentifier`**. Once a manifest URL pointing at it
> ships inside a release, that spelling is effectively permanent — GitHub
> redirects a renamed repository, but not forever and not for every client.
> Renaming is free today and awkward later.

## What lives there

| Path | What it is |
|---|---|
| `music_fingerprints.json.gz` | The working copy. Each run extends this one. |
| `checked.json` | Entry IDs already examined. The only record of the ones that yielded nothing. |
| `manifest.json` | What the player fetches: where the artifact is, its size, its checksum. |
| `releases/music-<schema>-<date>.json.gz` | Immutable published copies. Never overwritten. |
| `.github/workflows/daily-update.yml` | The job. |
| `smwrom/`, `tools/` | The fingerprint code, vendored from the player. |

The player only ever reads `manifest.json` and whatever it points at. Everything
else is build state.

## Seeding it

The daily job adds what is missing; it is not the way to index nine and a half
thousand entries. Build the first database from a synced `music_library/` and
commit it:

```bash
python tools/build_music_fingerprints.py --out music_fingerprints.json.gz
```

Then write a `checked.json` naming every catalog entry, so the job does not
spend its first weeks re-downloading archives that were already examined and
found to hold nothing:

```bash
python - <<'PY'
import gzip, json
db = json.loads(gzip.open("music_fingerprints.json.gz", "rb").read())
catalog = json.load(open("smwc_catalog.json", encoding="utf-8"))["entries"]
json.dump({"checked": sorted({str(e["id"]) for e in catalog})},
          open("checked.json", "w", encoding="utf-8"), indent=0)
PY
```

## What the job does each night

1. Walks the SMWCentral listing.
2. Subtracts everything already in the database or in `checked.json`.
3. Downloads **only what is left** — a handful on a normal day — one second
   apart, under an identifiable user agent.
4. Reads each archive in memory: the `.txt` gives instrument fingerprints, the
   `.spc` files give note-stream keys. Nothing is extracted to disk.
5. Records every entry it looked at, including the ones that yielded nothing.
   Without that the duds come back every night.
6. Commits, only if the day brought something.

A failed download is *not* recorded as examined: a timeout is not an answer
about the entry, so it stays outstanding and is retried tomorrow.

## Why the code is copied in

The player lives in a private Gitea that a GitHub Action cannot reach, so the
format code is vendored here rather than checked out beside the data.

`smwrom/` is pure standard library — no dependency to resolve, nothing to build
— which is what makes the copy a file copy rather than a packaging problem.

Two copies drift, so:

* Refresh it from the player with
  `python tools/export_fingerprint_kit.py --target <this repo>`.
* Check it before cutting a release with the same command and `--check`; it
  exits non-zero when the copy is stale.
* `smwrom/VENDORED.md` records the commit each copy came from.

The drift that matters is caught anyway, loudly: the manifest carries a
`schema_version`, and the player refuses a database whose version is not the one
it reads — in either direction. This is about noticing before a user does.

## Publishing

`manifest.json` is written by the job and points into `releases/` on the default
branch, served raw by GitHub. If you would rather serve from saphros.de, upload
the file there and change `--base-url`; the player reads the URL from the
manifest and does not care which host answers.

Whatever the host: **never overwrite a published file.** A client may be
part-way through downloading it. That is why the published name carries the
schema version and the date.

## What it cannot do

Fingerprints come from the archive, so an entry SMWCentral has removed, or one
whose download fails permanently, never gets indexed. It also indexes only what
the listing reports — an entry hidden from the section list is invisible to this
and to the player alike.
