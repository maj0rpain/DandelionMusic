"""The browse session: the cursor plus what the browse message shows.

Split out of LibraryBrowseView (musicbot/commands/library.py), which
kept the draw protocol - undo, which enrichment is drawn, which file
is attached, whether a resolved enrichment still belongs to the level -
next to the Discord calls. Here it is synchronous and Discord-free: the
session builds one `Frame` per draw and the view turns it into
Discord objects.

Imports only `musicbot.library`, `musicbot.library_browse` and
`typing`. The enrichment type is named under TYPE_CHECKING alone and
read by duck-typing (`.art`, `.stats`), because importing
`musicbot.library_metadata` would pull in linkutils, the loader and
yt_dlp. The session does no I/O.

The no-await rule is "build the frame, then await the edit": a frame
is built from the state in one synchronous call, so it cannot describe
two levels.
"""

from typing import TYPE_CHECKING, List, NamedTuple, Optional, Tuple

from musicbot import library, library_browse

if TYPE_CHECKING:
    from musicbot import library_metadata

# One icon per kind of thing the library holds, shared by the browser
# and the search results so the two can't drift apart.
KIND_EMOJI = {
    "artist": "\U0001f464",
    "album": "\U0001f4bf",
    "song": "\U0001f3b5",
}

# An embed title holds 256 UTF-16 code units: the icon is two of them,
# the space one and the "…" one, leaving 252 for the name. trim()
# measures in UTF-16 units too, so a non-BMP name counts double.
NAME_TITLE_LIMIT = 252


def trim(text: str, limit: int) -> str:
    """Cuts to `limit` UTF-16 code units plus an ellipsis. A character
    that would straddle the limit is dropped whole. A lone surrogate
    (an undecodable filename byte) counts as one unit."""
    units = 0
    for end, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return text[:end] + "…"
    return text


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
    """The `(name, value)` stat fields of one screen, empty ones left
    out - a grid of dashes reads worse than a short grid.

    Branches on the screen's own fields: `counts` is set at the root,
    `stats` below it. `external` is the drawn enrichment's stats, or
    None."""
    fields: List[Field] = []

    def add(name: str, value) -> None:
        if value:
            fields.append((name, str(value)))

    if screen.counts is not None:
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
    show_queue: bool
    queue_label: Optional[str]
    show_back: bool
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

    def _title(self) -> str:
        """Only the current scope, prefixed with the icon for what that
        scope is - the path to it lives in the footer."""
        cursor = self.cursor
        if cursor.artist is None:
            return f"{KIND_EMOJI['artist']} Artists"
        if cursor.album is None:
            name, kind = cursor.artist, "artist"
        else:
            name, kind = cursor.album, "album"
        return f"{KIND_EMOJI[kind]} {trim(name, NAME_TITLE_LIMIT)}"

    def _footer(self) -> Optional[str]:
        """In the breadcrumb the icons mark entities only - the leading
        "Artists" names the root screen rather than an artist."""
        cursor = self.cursor
        if cursor.artist is None:
            return None
        artist = f"{KIND_EMOJI['artist']} {cursor.artist}"
        if cursor.album is None:
            return f"Artists › {artist}"
        return f"{artist} › {KIND_EMOJI['album']} {cursor.album}"

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
            show_queue=not cursor.at_root,
            queue_label=(
                None
                if cursor.at_root
                else (
                    "Queue this Album"
                    if cursor.at_album_level
                    else "Queue this Artist"
                )
            ),
            show_back=not cursor.at_root,
            show_prev=cursor.has_prev(),
            show_next=cursor.has_next(),
            title=self._title(),
            footer=self._footer(),
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
