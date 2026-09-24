"""Red-outline marking for required QLineEdits left empty, shared by both
settings dialogs. The outline itself is the QLineEdit[invalid="true"] rule
in gui/theme.py; this just flips that property."""

from PySide6.QtWidgets import QLineEdit


def set_invalid(entry: QLineEdit, invalid: bool) -> None:
    if entry.property("invalid") == invalid:
        return
    entry.setProperty("invalid", invalid)
    # Qt only re-evaluates property selectors in a stylesheet on a re-polish.
    entry.style().unpolish(entry)
    entry.style().polish(entry)


def track_required(entry: QLineEdit) -> None:
    """Clears the outline as soon as something is typed into the field,
    rather than leaving it red until the next Save."""
    entry.textChanged.connect(lambda text: set_invalid(entry, False) if text.strip() else None)


def mark_empty(entries: list[QLineEdit]) -> list[QLineEdit]:
    """Outlines every empty entry in `entries` (and un-outlines filled ones),
    returning the empty ones in order."""
    empty = []
    for entry in entries:
        is_empty = not entry.text().strip()
        set_invalid(entry, is_empty)
        if is_empty:
            empty.append(entry)
    return empty
