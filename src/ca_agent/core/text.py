"""Purpose: carries extracted text from a reader to the chunker. ARCHITECTURE.md puts readers
and chunking in the same layer, so neither may import the other and they need a shared L0
vocabulary. A TextUnit is one addressable piece of a document - a page, a heading section, a
sheet, a field path - holding both its text and the reference an agent would cite it by, so
every chunk derived from it can name where it came from rather than just an offset.
"""

from __future__ import annotations

from dataclasses import dataclass

from ca_agent.core.enums import UnitType


@dataclass(frozen=True, slots=True)
class TextUnit:
    """One addressable piece of extracted text, in document order."""

    unit_type: UnitType
    unit_ref: str
    text: str
    sequence: int

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("sequence is a zero-based position in the document")
        if not self.unit_ref:
            raise ValueError("unit_ref must name the unit so a chunk can cite it")

    def has_content(self) -> bool:
        """SPEC-01 req 2 forbids embedding empty text, so emptiness is asked about often."""
        return bool(self.text.strip())
