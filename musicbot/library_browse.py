"""The browse cursor: where in the library a browse session is, and
how it moves.

Split out of LibraryBrowseView (musicbot/commands/library.py), which
fused these rules to discord.ui.View - every entry point took a
discord.Interaction, so none of it could be reached without a live
gateway. The churn that produced was one bug class repeating: a rule
about where the cursor is or how it moves, wrong in a way only a human
clicking buttons in Discord could catch.

Nothing here imports discord, or config, or anything that reaches the
bot. That is the point of the module, not an accident of what it
happens to need today: the moment it does, the tests below it need a
gateway again. The view keeps everything Discord owns - components,
message edits, deferral, the _busy guard, and the emoji.
"""

from typing import List, NamedTuple, Optional, Tuple

from musicbot import library


class Screen(NamedTuple):
    """Everything one browse screen derives from the index: the
    entries, their display labels, what kind of thing they are, the
    scope they describe, and the aggregate statistics for the level.
    All of it is a pure function of (index, artist, album) - the index
    is a snapshot taken when the cursor was built and never replaced -
    so it is computed once per level rather than per render.

    It used to be computed several times per *render*: build_items()
    asked for entries and for labels, which are the same list above the
    song level, and embed() asked a third time just to decide whether
    the level was empty. At the root each of those is a sort of every
    artist in the library, next to a counts() walk of every album and
    every song - and all of it ran again on each page turn, where by
    definition nothing has changed.

    `counts` is non-None exactly at the root, and `stats` exactly
    below it. That is a cross-module contract now, not an adjacent
    line: LibraryBrowseView._add_stat_fields() unpacks `counts` into
    three names without a guard, having tested `cursor.artist is None`
    rather than the field itself.
    """

    entries: List[str]  # filenames at the song level, folder names above
    labels: List[str]  # display text, parallel to entries
    kind: str  # what the entries are: "artist" | "album" | "song"
    scope: Tuple[Optional[str], Optional[str]]  # the (artist, album) shown
    counts: Optional[Tuple[int, int, int]]  # root level only
    stats: Optional[library.LevelStats]  # every level below the root


class Descent(NamedTuple):
    """What picking an entry did. `track` is set only on the song
    branch, where nothing about the level changed and the caller
    queues the triple instead of re-rendering."""

    changed_level: bool
    track: Optional[Tuple[str, str, str]]


class BrowseCursor:
    """Where a browse session is in the library, and every rule for
    moving it. Synchronous and Discord-free: the view drives this and
    renders what it reports.

    `page_size` is required rather than defaulted - the page size is a
    Discord fact (a Select holds 25 options), and a default here would
    be a second, silently disagreeing copy of it.
    """

    def __init__(self, index: library.LibraryIndex, page_size: int):
        # the snapshot this cursor was built from: a `d!lib refresh`
        # landing mid-session must not change what the already-shown
        # entries point at
        self.index = index
        self.page_size = page_size
        self.artist: Optional[str] = None
        self.album: Optional[str] = None
        self.page = 0
        # Bumped on every level change, and *only* on a level change,
        # so an enrichment that resolves after the user has navigated
        # on can tell that it describes a level nobody is looking at
        # any more and drop itself. Named for what it counts rather
        # than for navigation in general, so that paging - which
        # navigates, but stays on the level the enrichment describes -
        # doesn't get "tidied" into bumping it.
        self.level_revision = 0
        # the screen currently shown - see screen()
        self._screen: Optional[Screen] = None

    # -- where we are ---------------------------------------------

    @property
    def at_root(self) -> bool:
        return self.artist is None

    @property
    def at_album_level(self) -> bool:
        return self.album is not None

    def _songs(self) -> List[library.LibrarySong]:
        return self.index.get(self.artist, {}).get(self.album, [])

    def _build_screen(self) -> Screen:
        if self.artist is None:
            entries = sorted(self.index.keys())
            # entries and labels are the same list above the song
            # level; nothing mutates either, so they can share it
            return Screen(
                entries,
                entries,
                "artist",
                (None, None),
                library.counts(self.index),
                None,
            )
        if self.album is None:
            entries = sorted(self.index.get(self.artist, {}).keys())
            return Screen(
                entries,
                entries,
                "album",
                (self.artist, None),
                None,
                library.artist_stats(self.index, self.artist),
            )
        songs = self._songs()
        return Screen(
            # already sorted by filename in build_index(), preserving
            # track-number order - don't re-sort
            [song.filename for song in songs],
            [song.title for song in songs],
            "song",
            (self.artist, self.album),
            None,
            library.album_stats(self.index, self.artist, self.album),
        )

    def screen(self) -> Screen:
        """The current level's screen, computed on first use and held
        until the level changes. One slot rather than a per-level
        cache: what this is worth is not recomputing within a render or
        across a page turn, and keeping every level a long browse
        touched would retain the whole index a second time over.

        The held Screen needs no separate key - `scope` is assigned
        once, at build, so comparing it against the cursor's own
        position is the check. That is also why nothing exposes the
        current level key directly: a key built from `screen().scope`
        cannot describe a level other than the one actually rendered.
        """
        if self._screen is None or self._screen.scope != (
            self.artist,
            self.album,
        ):
            self._screen = self._build_screen()
        return self._screen

    # -- paging ----------------------------------------------------

    def _page_slice(self) -> slice:
        return slice(
            self.page * self.page_size, (self.page + 1) * self.page_size
        )

    def page_entries(self) -> List[str]:
        return self.screen().entries[self._page_slice()]

    def page_labels(self) -> List[str]:
        return self.screen().labels[self._page_slice()]

    def last_page(self) -> int:
        return max(0, (len(self.screen().entries) - 1) // self.page_size)

    def has_prev(self) -> bool:
        return self.page > 0

    def has_next(self) -> bool:
        return self.page < self.last_page()

    def page_by(self, delta: int) -> None:
        """Applies a page delta, clamped to the pages that exist.

        The clamp is what keeps any route to an out-of-range page from
        being silently destructive: out of range in either direction
        the page slice comes back empty - past the end because there
        is nothing there, and below zero because slice(-25, 0) selects
        nothing - and the view would then draw a screen with no Select
        on it at all, and no button that leads back.

        Must not touch level_revision: a page turn stays on the level
        the pending enrichment describes, and bumping here would make
        that enrichment drop itself and cost the level its cover art.
        """
        self.page = min(max(self.page + delta, 0), self.last_page())

    # -- moving between levels -------------------------------------

    def descend(self, entry: str) -> Descent:
        """Picks `entry` on the current screen: a level down above the
        song level, and the track's triple at it.

        Only a change of level resets the page and bumps
        level_revision. Picking a track changes neither: nothing
        re-renders, so zeroing the page would leave the displayed page
        and self.page disagreeing and the next "Next" click would jump
        back to page 1, and an unconditional bump would tell the
        in-flight enrichment that the album the user is still looking
        at is stale, silently dropping its cover art.
        """
        if self.artist is None:
            self.artist = entry
        elif self.album is None:
            self.album = entry
        else:
            return Descent(False, (self.artist, self.album, entry))
        self.page = 0
        self.level_revision += 1
        return Descent(True, None)

    def back(self) -> bool:
        """One level up. Returns False at the root, having changed
        nothing - there is nowhere to go, so the caller has nothing to
        re-render and no reason to restart an enrichment."""
        if self.artist is None:
            return False
        if self.album is not None:
            self.album = None
        else:
            self.artist = None
        self.page = 0
        self.level_revision += 1
        return True

    # -- what the current level stands for -------------------------

    def tracks(self) -> List[Tuple[str, str, str]]:
        """Every track under the current scope - one album's at the
        album level, the whole discography at the artist level. Empty
        at the root: queueing the entire library is not an operation
        the browser offers, and the view draws no button for it."""
        if self.artist is None:
            return []
        return library.tracks_for(self.index, self.artist, self.album)

    def sample_track(self) -> Optional[Tuple[str, str, str]]:
        """One track to read tags and embedded artwork off, for the
        enrichment of the current level - the album's first track at
        the album level, and the first track of the artist's first
        album above it. None at the root, and None wherever the scope
        holds no files at all."""
        if self.artist is None:
            return None
        album = self.album
        if album is None:
            # the current screen's entries are already that artist's
            # sorted album folder names
            albums = self.screen().entries
            if not albums:
                return None
            album = albums[0]
        songs = self.index.get(self.artist, {}).get(album, [])
        if not songs:
            return None
        return (self.artist, album, songs[0].filename)
