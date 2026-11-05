"""Library fakes the library test modules share."""

from musicbot.library import LibrarySong


def library_song(title, **fields):
    """A LibrarySong; `fields` go straight to LibrarySong, so an
    unknown keyword raises TypeError."""
    fields.setdefault("filename", title + ".flac")
    fields.setdefault("fmt", "FLAC")
    for name in LibrarySong._fields:
        if name not in ("title", "filename", "fmt"):
            fields.setdefault(name, None)
    return LibrarySong(title=title, **fields)
