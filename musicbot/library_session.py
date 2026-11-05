"""The browse session: the cursor plus what the browse message shows.

Split out of LibraryBrowseView (musicbot/commands/library.py), which
kept the draw protocol - undo, which enrichment is drawn, which file
is attached, whether a resolved enrichment still belongs to the level -
next to the Discord calls. Here it is synchronous and Discord-free: the
session builds one `Frame` per draw and the view turns it into
Discord objects.

Imports only `musicbot.library`, `musicbot.library_browse`,
`musicbot.library_display` and `typing`. The enrichment type is named
under TYPE_CHECKING alone and read by duck-typing (`.art`, `.stats`),
because importing `musicbot.library_metadata` would pull in linkutils,
the loader and yt_dlp. The session does no I/O.

The no-await rule is "build the frame, then await the edit": a frame
is built from the state in one synchronous call, so it cannot describe
two levels.
"""

from typing import TYPE_CHECKING, List, NamedTuple, Optional, Tuple

from musicbot import library, library_browse, library_display

if TYPE_CHECKING:
    from musicbot import library_metadata


def _fmt_duration(seconds: Optional[int]) -> Optional[str]:
    """Renders as "42:39" under an hour and "11h 23m" above it - an
    album runtime and a whole discography's runtime want different
    units."""
    if not seconds:
        return None
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}:{secs:02d}"


def _fmt_count(value: Optional[int]) -> Optional[str]:
    """Compact form for the six- and ten-digit counters Last.fm and
    Spotify report - "1.2B" reads at a glance where "1204338291"
    doesn't."""
    if not value:
        return None
    units = ((1_000, "K"), (1_000_000, "M"), (1_000_000_000, "B"))
    chosen = None
    for index, (limit, suffix) in enumerate(units):
        if value >= limit:
            chosen = index
    if chosen is None:
        return str(value)
    # rounded to one decimal *before* the unit is settled, so 999,950
    # reads "1M" rather than "1000K"
    limit, suffix = units[chosen]
    if round(value / limit, 1) >= 1000 and chosen + 1 < len(units):
        limit, suffix = units[chosen + 1]
    text = f"{value / limit:.1f}".rstrip("0").rstrip(".")
    return text + suffix


def _fmt_years(stats: library.LevelStats) -> Optional[str]:
    if not stats.year_min:
        return None
    if stats.year_max and stats.year_max != stats.year_min:
        return f"{stats.year_min}–{stats.year_max}"
    return str(stats.year_min)


Field = Tuple[str, str]


def stat_fields(screen: library_browse.Screen, external) -> List[Field]:


    out - a grid of dashes reads worse than a short grid.
    Branches on `screen.kind`: "artist" is the root (its `counts`),
    "album" and "song" are below it (its `stats`). `external` is the
    drawn enrichment's stats, or None."""
    fields: List[Field] = []

    def add(name: str, value) -> None:
        if value:
            fields.append((name, str(value)))

    if screen.kind == "artist":
        if screen.counts is None:
            return fields
        artists, albums, songs = screen.counts
        add("Artists", f"{artists:,}")
        add("Albums", f"{albums:,}")
        add("Songs", f"{songs:,}")
        return fields
    local = screen.stats
    if local is None:
        return fields
    if screen.kind != "song":
        add("Albums", local.albums)
        add("Tracks", local.tracks)
        add("Runtime", _fmt_duration(local.runtime))
        add("Years", _fmt_years(local))
        add("Formats", ", ".join(local.formats))
        if external:
            add("Listeners", _fmt_count(external.listeners))
        return fields
    add("Tracks", local.tracks)
    add("Runtime", _fmt_duration(local.runtime))
    # the tag-derived year is the one that matches these files;
    # Spotify's release date is only a fallback, and only its year is
    # worth the width
    year = _fmt_years(local) or (
        external.release_date[:4]
        if external and external.release_date
        else None
    )
    add("Year", year)
    add(
        "Format",
        " · ".join(
            part
            for part in (
                local.formats[0] if local.formats else None,
                local.quality,
            )
            if part
        ),
    )
    if external:
        add("Listeners", _fmt_count(external.listeners))
        add(
            "Popularity",
            (
                f"{external.popularity}/100"
                if external.popularity is not None
                else None
            ),
        )
    return fields


def _description(screen: library_browse.Screen, external) -> Optional[str]:
    """Community tags where the online backends know any, the
    library's own genre tags otherwise. Nothing at the root."""
    if screen.stats is None:
        return None
    labels = (external.tags if external else ()) or screen.stats.genres
    if labels:
        return "*" + " · ".join(labels) + "*"
    return None


class Attachment(NamedTuple):
    key: str
    data: bytes
    extension: str

    @property
    def filename(self) -> str:
        return f"cover.{self.extension}"


class Frame(NamedTuple):
    """One message edit, described without Discord."""

    # the select
    entries: List[str]
    labels: List[str]
    kind: str
    # the buttons
    # None at the root, where neither Queue nor Back is drawn; below
    # it, the Queue button's label (and Back is drawn)
    queue_label: Optional[str]
    show_prev: bool
    show_next: bool
    # the embed
    title: str
    footer: Optional[str]
    description: Optional[str]
    fields: Tuple[Field, ...]
    empty: bool
    # an art URL, or the filename of `attachment`, or None
    thumbnail: Optional[str]
    attachment: Optional[Attachment]
    # the attachment key differs from the one the message last landed
    # with, so the edit must carry `attachments`
    attachments_change: bool

    @property
    def attachment_key(self) -> Optional[str]:
        return self.attachment.key if self.attachment else None


class Snapshot(NamedTuple):
    position: library_browse.Position
    enrichment: Optional["library_metadata.Enrichment"]


class BrowseSession:
    def __init__(self, index: library.LibraryIndex, page_size: int):
        self.cursor = library_browse.BrowseCursor(index, page_size)
        self._enrichment: Optional["library_metadata.Enrichment"] = None
        # the attachment key the message carries, written back only by
        # landed()
        self._attached: Optional[str] = None

    # -- cursor moves ----------------------------------------------

    def descend(self, entry: str) -> library_browse.Descent:
        return self.cursor.descend(entry)

    def back(self) -> bool:
        return self.cursor.back()

    def page_by(self, delta: int) -> None:
        self.cursor.page_by(delta)

    # -- the drawn enrichment --------------------------------------

    def enter_level(self) -> int:
        """Call after a level change: forgets the old level's drawn
        enrichment and returns the token an enrichment resolved for
        this level offers back."""
        self._enrichment = None
        return self.cursor.level_revision

    def offer_enrichment(self, token: int, enrichment) -> bool:
        """Takes `enrichment` only if `token` is still the current
        level's. A taken enrichment stays drawn even if its edit then
        fails, so the next draw carries it again."""
        if token != self.cursor.level_revision:
            return False
        self._enrichment = enrichment
        return True

    # -- undo ------------------------------------------------------

    def snapshot(self) -> Snapshot:
        return Snapshot(self.cursor.position(), self._enrichment)

    def restore(self, snap: Snapshot) -> None:
        self.cursor.restore(snap.position)
        self._enrichment = snap.enrichment

    def position(self) -> library_browse.Position:
        return self.cursor.position()

    def restore_position(self, position: library_browse.Position) -> None:
        self.cursor.restore(position)

    # -- drawing ---------------------------------------------------

    def _art(self):
        return self._enrichment.art if self._enrichment else None

    @staticmethod
    def _title(screen: library_browse.Screen) -> str:
        """Only the current scope, prefixed with the icon for what that
        scope is - the path to it lives in the footer."""
        if screen.kind == "artist":
            return f"{library_display.KIND_EMOJI['artist']} Artists"
        artist, album = screen.scope
        if screen.kind == "album":
            name, kind = artist, "artist"
        else:
            name, kind = album, "album"
        icon = library_display.KIND_EMOJI[kind]
        trimmed = library_display.trim(name, library_display.NAME_TITLE_LIMIT)
        return f"{icon} {trimmed}"

    @staticmethod
    def _footer(screen: library_browse.Screen) -> Optional[str]:
        """In the breadcrumb the icons mark entities only - the leading
        "Artists" names the root screen rather than an artist."""
        if screen.kind == "artist":
            return None
        artist_name, album_name = screen.scope
        artist = f"{library_display.KIND_EMOJI['artist']} {artist_name}"
        if screen.kind == "album":
            return f"Artists › {artist}"
        icon = library_display.KIND_EMOJI["album"]
        return f"{artist} › {icon} {album_name}"

    @staticmethod
    def _queue_label(screen: library_browse.Screen) -> Optional[str]:
        if screen.kind == "artist":
            return None
        if screen.kind == "album":
            return "Queue this Artist"
        return "Queue this Album"

    def frame(self) -> Frame:
        cursor = self.cursor
        screen = cursor.screen()
        art = self._art()
        attachment = None
        thumbnail = None
        if art and art.data:
            artist, album = screen.scope
            attachment = Attachment(
                f"{artist}/{album}.{art.extension}", art.data, art.extension
            )
        if art and art.url:
            thumbnail = art.url
        elif attachment:
            thumbnail = attachment.filename
        empty = not screen.entries
        external = self._enrichment.stats if self._enrichment else None
        frame = Frame(
            entries=cursor.page_entries(),
            labels=cursor.page_labels(),
            kind=screen.kind,
            queue_label=self._queue_label(screen),
            show_prev=cursor.has_prev(),
            show_next=cursor.has_next(),
            title=self._title(screen),
            footer=self._footer(screen),
            description=(None if empty else _description(screen, external)),
            fields=() if empty else tuple(stat_fields(screen, external)),
            empty=empty,
            thumbnail=thumbnail,
            attachment=attachment,
            attachments_change=False,
        )
        return frame._replace(
            attachments_change=frame.attachment_key != self._attached
        )

    def landed(self, frame: Frame) -> None:
        """Call only once the frame's edit has landed: a failed edit
        leaves the key as it was."""
        self._attached = frame.attachment_key
