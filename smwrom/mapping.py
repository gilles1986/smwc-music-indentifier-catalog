"""Turning a SNES address into an offset in a ROM file.

Two things stand between an address printed in a disassembly and the byte it
names. The first is the 512-byte copier header some dumps carry and some do
not — one of the three test ROMs has one. The second is the mapping, and that
is where SA-1 hacks diverge from everything else.

On an ordinary LoROM cartridge, banks ``$80``-``$BF`` mirror ``$00``-``$3F``.
Under SA-1 they do not: the Super MMC hands out four separate 1 MB windows, so
``$82`` is a megabyte away from ``$02`` rather than the same place. Reading an
SA-1 hack with the LoROM rule does not fail — it returns plausible-looking
garbage, which is far worse. `Cubes Kaizo World 5.smc` yields 21 sane and 44
insane music blocks that way, against 65 sane and 0 insane with the mapping
below.
"""

from __future__ import annotations

from typing import Optional

#: Offset of the internal header in a LoROM image, headerless. Super Mario
#: World and every hack of it is LoROM, so the alternative HiROM position is
#: never consulted.
_HEADER = 0x7FC0

#: Chipset byte, relative to the internal header. These three values mean SA-1.
_CHIPSET = 22
_SA1_CHIPSETS = frozenset({0x33, 0x34, 0x35})

#: SA-1 Super MMC windows: (first bank, last bank, ROM offset of the window).
#: The ``$C0``-``$FF`` half is the documented layout but is **untested** — no
#: SA-1 ROM here puts anything we read into those banks.
_SA1_WINDOWS = (
    (0x00, 0x1F, 0x000000),
    (0x20, 0x3F, 0x100000),
    (0x80, 0x9F, 0x200000),
    (0xA0, 0xBF, 0x300000),
)
_SA1_HIROM_WINDOWS = (
    (0xC0, 0xCF, 0x000000),
    (0xD0, 0xDF, 0x100000),
    (0xE0, 0xEF, 0x200000),
    (0xF0, 0xFF, 0x300000),
)


class RomImage:
    """A ROM in memory, with the copier header gone and the mapping decided.

    Attributes:
        data: The ROM bytes, without any copier header.
        had_copier_header: Whether 512 bytes were stripped on load.
        is_sa1: Whether the internal header declares an SA-1 chipset.
    """

    def __init__(self, data: bytes) -> None:
        """Wrap raw ROM bytes, stripping a copier header if one is present.

        Args:
            data: The full contents of a ``.smc``/``.sfc`` file.
        """
        self.had_copier_header = len(data) % 0x8000 == 512
        self.data = data[512:] if self.had_copier_header else data
        self.is_sa1 = (
            len(self.data) > _HEADER + _CHIPSET
            and self.data[_HEADER + _CHIPSET] in _SA1_CHIPSETS
        )

    @classmethod
    def from_file(cls, path: str) -> "RomImage":
        """Load a ROM from disk.

        Args:
            path: Path to a ``.smc`` or ``.sfc`` file. The two extensions name
                the same format; only the file name differs.

        Returns:
            The loaded image.

        Raises:
            OSError: If the file cannot be read.
        """
        with open(path, "rb") as handle:
            return cls(handle.read())

    def __len__(self) -> int:
        """Return the ROM size in bytes, excluding any copier header."""
        return len(self.data)

    def to_pc(self, snes: int) -> Optional[int]:
        """Convert a 24-bit SNES address to an offset in :attr:`data`.

        Args:
            snes: A SNES long address, e.g. ``0x0E8000``.

        Returns:
            The offset, or ``None`` when the address names no ROM byte — an
            offset below ``$8000`` in a LoROM bank is RAM or hardware, and an
            SA-1 bank outside the mapped windows is nothing at all.
        """
        bank = (snes >> 16) & 0xFF
        offset = snes & 0xFFFF

        if self.is_sa1:
            for first, last, base in _SA1_WINDOWS:
                if first <= bank <= last:
                    if offset < 0x8000:
                        return None
                    return base + ((bank - first) << 15) + (offset - 0x8000)
            for first, last, base in _SA1_HIROM_WINDOWS:
                if first <= bank <= last:
                    return base + ((bank - first) << 16) + offset
            return None

        if offset < 0x8000:
            return None
        return ((bank & 0x7F) << 15) | (offset - 0x8000)

    def read(self, snes: int, count: int) -> Optional[bytes]:
        """Read *count* bytes starting at a SNES address.

        Args:
            snes: A SNES long address.
            count: How many bytes to read.

        Returns:
            The bytes, or ``None`` if the address is unmapped or the read would
            run past the end of the ROM.
        """
        start = self.to_pc(snes)
        if start is None or start < 0 or start + count > len(self.data):
            return None
        return self.data[start:start + count]

    def read_long(self, pc: int) -> int:
        """Read a 3-byte little-endian pointer at a *file* offset.

        Args:
            pc: Offset into :attr:`data`.

        Returns:
            The 24-bit value, or ``0`` if the read runs past the end.
        """
        if pc < 0 or pc + 3 > len(self.data):
            return 0
        return int.from_bytes(self.data[pc:pc + 3], "little")
