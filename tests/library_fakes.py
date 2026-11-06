"""Library fakes the library test modules share."""

from musicbot.library import LibrarySong
from musicbot.library_metadata import ExternalStats


def library_song(title, **fields) -> LibrarySong:
    """A LibrarySong; `fields` go straight to LibrarySong, so an
    unknown keyword raises TypeError."""
    fields.setdefault("filename", title + ".flac")
    fields.setdefault("fmt", "FLAC")
    for name in LibrarySong._fields:
        if name not in ("title", "filename", "fmt"):
            fields.setdefault(name, None)
    return LibrarySong(title=title, **fields)


def external_stats(**fields) -> ExternalStats:
    """An ExternalStats; `fields` go straight to ExternalStats, so an
    unknown keyword raises TypeError."""
    fields.setdefault("tags", ())
    for name in ExternalStats._fields:
        fields.setdefault(name, None)
    return ExternalStats(**fields)
