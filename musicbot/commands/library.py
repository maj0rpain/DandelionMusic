import asyncio
import io
import sys
from contextlib import contextmanager
from traceback import print_exc
from typing import Callable, List, NamedTuple, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from config import config
from musicbot import library, library_browse, library_metadata
from musicbot.bot import MusicBot
from musicbot.utils import (
    CheckError,
    get_audiocontroller,
    owner_check,
    join_voice,
    joining_command,
    play_check,
)
from musicbot.views import OwnedView

PAGE_SIZE = 25

# Search is the only command here that burns real CPU: scoring is
# pure-Python difflib, so run_in_executor keeps it off the event loop's
# stack but not off the GIL, and several at once would take turns
# starving the loop - and with it the audio sender thread. This runs
# them one at a time instead. _MAX_QUERY_LEN caps what a single search
# can cost, so one at a time is a bounded cost.
#
# Deliberately a lock taken *inside* the command rather than
# commands.max_concurrency: that acquires during Command.prepare,
# before the callback gets a chance to defer, so a slash caller queued
# behind others could blow the three-second acknowledgement window and
# fail outright. Taken after the deferral below, a caller has already
# answered its interaction and can wait as long as it needs to.
# Waiting, not refusing: a cooldown would just make someone retype
# their query.
_search_lock = asyncio.Lock()

# small grey line above the title, so the current scope gets the
# title to itself at every level
AUTHOR_LINE = "Music Library"

# One icon per kind of thing the library holds, shared by the browser
# and the search results so the two can't drift apart. In a Select
# these go in SelectOption's own `emoji` field, which renders them as
# an icon column and leaves the whole 100-character label budget for
# the name; embed titles and footers have no such field, so there they
# are prefixed as literal text.
KIND_EMOJI = {
    "artist": "\U0001f464",
    "album": "\U0001f4bf",
    "song": "\U0001f3b5",
}


# `query` is a consume-rest parameter, so a prefix invocation can fill
# it with most of a 2000-character message. Anything echoed back has
# to be trimmed first, or the reply itself blows the message limit.
QUERY_ECHO_LIMIT = 100


# An embed title holds 256 UTF-16 code units: the icon is two of them,
# the space one and the "\u2026" one, leaving 252 for the name. _trim
# measures in UTF-16 units too, so a non-BMP name counts double.
NAME_TITLE_LIMIT = 252


# A select option label holds 100 UTF-16 code units, and the "\u2026" _trim
# appends takes one, leaving 99 for the name.
SELECT_LABEL_LIMIT = 99


def _trim(text: str, limit: int) -> str:
    """Cuts to `limit` UTF-16 code units plus an ellipsis. A character
    that would straddle the limit is dropped whole. A lone surrogate
    (an undecodable filename byte) counts as one unit."""
    units = 0
    for end, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return text[:end] + "…"
    return text


def _select_options(pairs) -> List[discord.SelectOption]:
    """Select options from `(label, kind)` pairs: the trimmed label,
    the pair's position as the value, the kind's emoji."""
    return [
        discord.SelectOption(
            label=_trim(label, SELECT_LABEL_LIMIT),
            value=str(i),
            emoji=KIND_EMOJI[kind],
        )
        for i, (label, kind) in enumerate(pairs)
    ]


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
        return f"{stats.year_min}\u2013{stats.year_max}"
    return str(stats.year_min)


class _BrowseItem:
    """Mixin for every component of a LibraryBrowseView: owns the stale
    guard, so a component gets it just by subclassing. List it first in
    the bases, so its callback() wins in the MRO; implement on_click()."""

    def __init__(self, browse_view: "LibraryBrowseView", **kwargs):
        super().__init__(**kwargs)
        self.browse_view = browse_view
        self.scope: Tuple[Optional[str], Optional[str]] = browse_view.scope

    async def callback(self, interaction: discord.Interaction):
        """A click on a component built for a scope the cursor has left is
        acknowledged silently and must do nothing. Keyed on the scope, not
        on level_revision, so components a failed draw restores still act.
        Checked at the start of callback(), after the view's ownership and
        double-click checks - never in a per-item interaction_check, which
        discord.py runs before the view's."""
        if self.scope != self.browse_view.scope:
            await interaction.response.defer()
            return
        await self.on_click(interaction)

    async def on_click(self, interaction: discord.Interaction):
        raise NotImplementedError


class LibrarySelect(_BrowseItem, discord.ui.Select):
    def __init__(
        self,
        entries: List[str],
        labels: List[str],
        kind: str,
        browse_view: "LibraryBrowseView",
    ):
        self._entries = entries
        super().__init__(
            browse_view,
            placeholder="Choose...",
            options=_select_options((label, kind) for label in labels),
        )

    async def on_click(self, interaction: discord.Interaction):
        index = int(self.values[0])
        chosen = self._entries[index]
        # the option's own label, not the underlying entry (a raw
        # filename at the song level) - see queue_songs()'s `source`
        label = self.options[index].label
        await self.browse_view.descend(interaction, chosen, label)


class BackButton(_BrowseItem, discord.ui.Button):
    def __init__(self, browse_view: "LibraryBrowseView"):
        super().__init__(
            browse_view, label="Back", style=discord.ButtonStyle.grey, row=1
        )

    async def on_click(self, interaction: discord.Interaction):
        await self.browse_view.go_back(interaction)


class PageButton(_BrowseItem, discord.ui.Button):
    def __init__(
        self, browse_view: "LibraryBrowseView", delta: int, label: str
    ):
        self.delta = delta
        super().__init__(
            browse_view,
            label=label,
            style=discord.ButtonStyle.blurple,
            row=2,
        )

    async def on_click(self, interaction: discord.Interaction):
        await self.browse_view.turn_page(interaction, self.delta)


class QueueLevelButton(_BrowseItem, discord.ui.Button):
    def __init__(self, browse_view: "LibraryBrowseView"):
        label = (
            "Queue this Album"
            if browse_view.cursor.at_album_level
            else "Queue this Artist"
        )
        super().__init__(
            browse_view, label=label, style=discord.ButtonStyle.green, row=1
        )

    async def on_click(self, interaction: discord.Interaction):
        await self.browse_view.queue_current_level(interaction)


async def queue_songs(ctx, interaction, triples, source: str) -> None:
    """Queues (artist, album, filename) triples and reports the
    result ephemerally. Shared by the browser and the search results -
    the browser always works within one artist, but a search hit list
    spans several, so the artist travels with each song rather than
    being read off the view.

    `source` describes what was actually picked in the dropdown/button
    that led here (e.g. "browse: Artist - Album - Song") - passed
    straight through to process_local_tracks() for the console log,
    since a bare file path doesn't say that."""
    owns_placeholder = not interaction.response.is_done()
    if owns_placeholder:
        # A real ephemeral placeholder, edited in place below - not a
        # deferred "thinking" response. Deferring a component
        # interaction with thinking=True sends response type 5
        # (deferred_channel_message), which Discord's client
        # unreliably renders as "This interaction failed" even though
        # the deferral succeeds server-side and the real result still
        # arrives a moment later. A plain defer() avoids that but is
        # deferred_message_update - silent, and it ignores `ephemeral`
        # outright - which left queueing a whole discography looking
        # like a button that did nothing. Responding for real up front
        # sidesteps both problems.
        await interaction.response.send_message("Queueing...", ephemeral=True)

    async def reply(content: str) -> None:
        """Fills in the placeholder above, or sends an ephemeral
        message of its own if something else already answered this
        interaction. Editing the original response would then edit
        whatever that answer was - and for the plain defer() used by
        interaction_check() and _enter_level(), that is the browse
        message itself, so the result would land on the embed."""
        if owns_placeholder:
            await interaction.edit_original_response(content=content)
        else:
            await interaction.followup.send(content, ephemeral=True)

    # walked twice below (once to build the URIs, once to name what
    # was skipped), so it must not be something that can be consumed
    triples = list(triples)

    # The checks and the join are inside this, not ahead of it: the
    # placeholder above is a visible message now, so anything
    # escaping here leaves it saying "Queueing..." forever -
    # discord.py logs the traceback and the user is told nothing at
    # all. join_voice() turns a failed connect into a CheckError, so
    # it is reported as one rather than as a song-info error.
    try:
        await play_check(ctx)
        await join_voice(ctx)
        tracks = [library.song_uri(*triple) for triple in triples]
        songs = await ctx.audiocontroller.process_local_tracks(
            tracks, source, user=ctx.author
        )
    except CheckError as e:
        await reply(str(e))
        return
    except Exception:
        print_exc(file=sys.stderr)
        await reply(config.SONGINFO_ERROR)
        return

    missing = [
        filename
        for (_, _, filename), song in zip(triples, songs)
        if song is None
    ]
    queued = len(songs) - len(missing)

    message = f"Queued {queued} song(s)."
    if missing:
        shown = ", ".join(missing[:5])
        message += f" {len(missing)} skipped (file not found): {shown}"
        if len(missing) > 5:
            message += f", and {len(missing) - 5} more"
        # refresh is owner-only, so this can't tell whoever hit it to
        # just run it themselves
        message += ". The index may be stale - ask the bot owner to run"
        message += " `d!lib refresh`."

    await reply(message)


class LevelSnapshot(NamedTuple):
    """Where the cursor was and what the message showed, taken
    before a level change so a failed first draw can be undone."""

    artist: Optional[str]
    album: Optional[str]
    page: int
    enrichment: Optional[library_metadata.Enrichment]


class LibraryView(OwnedView):
    """An OwnedView over one library index snapshot, with a
    double-click guard (`_busy`) for the browser's slow redraws.
    Ownership, timeout and message refresh come from OwnedView."""

    def __init__(self, ctx, index: library.LibraryIndex):
        super().__init__(ctx, timeout=300)
        # the snapshot this view was built from: a `d!library refresh`
        # landing mid-session must not change what the already-shown
        # entries point at
        self.index = index
        # the double-click guard: while it is held, interaction_check()
        # refuses new clicks. discord.py dispatches every click in its
        # own task, and deferring a component interaction clears the
        # click spinner and re-enables the view immediately rather than
        # showing a "thinking" placeholder, so without it a second
        # click could start while the first is still drawing or
        # queueing.
        #
        # Held by a cursor move's draw (a level's first draw or a page
        # turn) and a bulk queue, across the render lock wait as well as
        # the edit. Not by the enrichment edit - see
        # LibraryBrowseView._enter_level(). A click reaches the view
        # during a redraw at all only because LibraryBrowseView._draw()
        # keeps the old components pointed at it - see there.
        #
        # Keeping the message edits in order is not this guard's job:
        # the browser's render lock serialises them - see
        # LibraryBrowseView.render().
        #
        # A depth count rather than a flag, as deliberate defence-in-depth:
        # should two holders ever overlap, the first one out must not clear
        # the other's guard and let a second click queue the same album
        # twice.
        self._busy: int = 0

    @contextmanager
    def busy(self):
        """Refuses clicks for the duration - see _busy."""
        self._busy += 1
        try:
            yield
        finally:
            self._busy -= 1

    def claim_click(self) -> bool:
        # Records no claim: a pure read of _busy, which busy() takes
        # and releases around the work, not per click.
        # (Why a click reaches this check during a redraw: see
        # _busy.) Acknowledged silently rather than answered with an
        # ephemeral complaint. The clicks the guard catches are the
        # second half of a double-click, or clicks made after an
        # accepted one while its draw waits behind an artwork
        # upload (see LibraryBrowseView._enter_level()). Telling
        # someone off for either reads as a malfunction. The one
        # operation still slow enough to be worth explaining - a
        # bulk queue - puts up its own ephemeral "Queueing..."
        # message while it runs, so the user can already see why
        # nothing else is responding.
        return not self._busy

    async def queue(
        self, interaction: discord.Interaction, triples, source: str
    ) -> None:
        """Queues through the _busy guard. queue_songs() answers the
        interaction straight away, which re-enables the select at once
        - the "Queueing..." placeholder it puts up is a separate
        ephemeral message, not a lock on the view - so without this a
        second click while the batch is still loading would queue the
        same album or discography twice over."""
        with self.busy():
            await queue_songs(self.ctx, interaction, triples, source)


class LibraryBrowseView(LibraryView):
    """The Discord half of the browser: components, message edits,
    deferral, the _busy guard and the enrichment. Where the browse
    session is and how it moves is self.cursor's - see
    musicbot/library_browse.py, which is testable precisely because
    none of this is in it. Nothing here may keep its own copy of the
    cursor's state; read it back through self.cursor every time."""

    def __init__(self, ctx, index: library.LibraryIndex):
        super().__init__(ctx, index)
        self.cursor = library_browse.BrowseCursor(index, PAGE_SIZE)
        self._enrichment: Optional[library_metadata.Enrichment] = None
        # what _attachment_key() described the last time an edit
        # actually carried an `attachments` field - see _draw()
        self._attached: Optional[str] = None
        # serialises the message edits themselves - see render()
        self._render_lock = asyncio.Lock()
        self.build_items()

    @property
    def scope(self) -> Tuple[Optional[str], Optional[str]]:
        """The (artist, album) pair of the level shown now."""
        return self.cursor.screen().scope

    async def _resolve_enrichment(
        self,
    ) -> Optional[library_metadata.Enrichment]:
        """Whatever the online backends know about the current
        level, or None where there is nothing to ask about: the root,
        and any scope holding no file to read tags and embedded
        artwork off."""
        sample = self.cursor.sample_track()
        if sample is None:
            return None
        path = library.song_path(*sample)
        if self.cursor.album is None:
            return await library_metadata.get_artist_enrichment(
                self.cursor.artist, path
            )
        return await library_metadata.get_album_enrichment(
            self.cursor.artist, self.cursor.album, path
        )

    def title(self) -> str:
        """Only the current scope, prefixed with the icon for what
        that scope is - the path to it lives in the footer, and
        "Music Library" in the author line."""
        if self.cursor.artist is None:
            return f"{KIND_EMOJI['artist']} Artists"
        if self.cursor.album is None:
            return (
                f"{KIND_EMOJI['artist']} "
                f"{_trim(self.cursor.artist, NAME_TITLE_LIMIT)}"
            )
        return (
            f"{KIND_EMOJI['album']} "
            f"{_trim(self.cursor.album, NAME_TITLE_LIMIT)}"
        )

    def footer(self) -> Optional[str]:
        """In the breadcrumb the icons mark entities only - the
        leading "Artists" names the root screen rather than an artist,
        so it stays plain. title() runs the other way round, because
        there "Artists" *is* the screen being shown and takes the icon
        for the kind of thing it lists."""
        if self.cursor.artist is None:
            return None
        artist = f"{KIND_EMOJI['artist']} {self.cursor.artist}"
        if self.cursor.album is None:
            return f"Artists \u203a {artist}"
        return f"{artist} \u203a {KIND_EMOJI['album']} {self.cursor.album}"

    @staticmethod
    def _field(embed: discord.Embed, name: str, value) -> None:
        """A field with nothing behind it is left out entirely rather
        than rendered as a placeholder dash - which backends answer
        varies per entity, and a grid of dashes reads worse than a
        short grid."""
        if value:
            embed.add_field(name=name, value=str(value), inline=True)

    def _add_stat_fields(self, embed: discord.Embed) -> None:
        stats = self._enrichment.stats if self._enrichment else None
        screen = self.cursor.screen()
        if self.cursor.artist is None:
            # non-None exactly at the root, which is what the test
            # above is standing in for - see Screen
            artists, albums, songs = screen.counts
            self._field(embed, "Artists", f"{artists:,}")
            self._field(embed, "Albums", f"{albums:,}")
            self._field(embed, "Songs", f"{songs:,}")
            return

        local = screen.stats
        if self.cursor.album is None:
            self._field(embed, "Albums", local.albums)
            self._field(embed, "Tracks", local.tracks)
            self._field(embed, "Runtime", _fmt_duration(local.runtime))
            self._field(embed, "Years", _fmt_years(local))
            self._field(embed, "Formats", ", ".join(local.formats))
            if stats:
                self._field(embed, "Listeners", _fmt_count(stats.listeners))
        else:
            self._field(embed, "Tracks", local.tracks)
            self._field(embed, "Runtime", _fmt_duration(local.runtime))
            # the tag-derived year is the one that matches these
            # files; Spotify's release date is only a fallback, and
            # only its year is worth the width
            year = _fmt_years(local) or (
                stats.release_date[:4]
                if stats and stats.release_date
                else None
            )
            self._field(embed, "Year", year)
            self._field(
                embed,
                "Format",
                " \u00b7 ".join(
                    part
                    for part in (
                        local.formats[0] if local.formats else None,
                        local.quality,
                    )
                    if part
                ),
            )
            if stats:
                self._field(embed, "Listeners", _fmt_count(stats.listeners))
                self._field(
                    embed,
                    "Popularity",
                    (
                        f"{stats.popularity}/100"
                        if stats.popularity is not None
                        else None
                    ),
                )

        # community tags where the online backends know any, the
        # library's own genre tags otherwise
        labels = (stats.tags if stats else ()) or local.genres
        if labels:
            embed.description = "*" + " \u00b7 ".join(labels) + "*"

    def embed(self) -> discord.Embed:
        embed = discord.Embed(title=self.title(), color=config.EMBED_COLOR)
        embed.set_author(name=AUTHOR_LINE)
        footer = self.footer()
        if footer:
            embed.set_footer(text=footer)
        if not self.cursor.screen().entries:
            embed.description = config.LIBRARY_EMPTY
        else:
            self._add_stat_fields(embed)
        art = self._enrichment.art if self._enrichment else None
        if art and art.url:
            embed.set_thumbnail(url=art.url)
        elif art and art.data:
            embed.set_thumbnail(url=f"attachment://cover.{art.extension}")
        return embed

    def _attachments(self) -> List[discord.File]:
        art = self._enrichment.art if self._enrichment else None
        if art and art.data:
            return [
                discord.File(
                    io.BytesIO(art.data), filename=f"cover.{art.extension}"
                )
            ]
        return []

    def _attachment_key(self) -> Optional[str]:
        """Identity of the file the current level needs on the
        message, or None when it needs none. Embedded artwork runs to
        megabytes and is re-sent in full whenever an edit carries an
        `attachments` field, which makes it the most expensive part of
        a render by a wide margin - so an edit only carries that field
        when this changes."""
        art = self._enrichment.art if self._enrichment else None
        if art is None or not art.data:
            return None
        # The scope of the same Screen build_items() and embed() just
        # read, so the key names the level they drew. Reading it here
        # is not what makes that true: screen() rebuilds as soon as
        # its scope stops matching the cursor, which makes this
        # equivalent to reading cursor.artist/album directly. What
        # keeps all three agreeing is _draw() never awaiting between
        # them - see there.
        artist, album = self.cursor.screen().scope
        return f"{artist}/{album}.{art.extension}"

    def build_items(self):
        self.clear_items()
        page_entries = self.cursor.page_entries()
        if page_entries:
            self.add_item(
                LibrarySelect(
                    page_entries,
                    self.cursor.page_labels(),
                    self.cursor.screen().kind,
                    self,
                )
            )
        if not self.cursor.at_root:
            self.add_item(QueueLevelButton(self))
            self.add_item(BackButton(self))
        if self.cursor.has_prev():
            self.add_item(PageButton(self, -1, "◀ Prev"))
        if self.cursor.has_next():
            self.add_item(PageButton(self, 1, "Next ▶"))

    async def render(
        self,
        interaction: discord.Interaction,
        sync_attachments: bool = False,
    ) -> bool:
        """The locked entry point for redrawing the message; see _draw()
        for the rules the draw keeps. `interaction` must already have
        been deferred - every edit goes out as a followup, so that
        waiting on the lock can never eat the three seconds an
        interaction has to be answered in.

        The lock exists because two edits to one message could
        otherwise land in either order, each writing back an _attached
        sampled before the other ran, so a later navigation omits an
        `attachments` field it needed. A click's edit and the
        enrichment edit (which waits on this lock, and is not covered
        by _busy - see _enter_level()) are the two that can overlap.

        A cursor move whose draw fails here is undone by its caller
        (see _draw_or_undo()) on a False return and on any exception
        alike."""
        async with self._render_lock:
            return await self._draw(interaction, sync_attachments)

    async def _render_enrichment(
        self,
        interaction: discord.Interaction,
        level_revision: int,
        enrichment: Optional[library_metadata.Enrichment],
    ) -> None:
        """Draws `enrichment` over the level it was resolved for, unless
        the cursor has left that level, in which case nothing is drawn
        and _enrichment is left alone.

        The check is made *inside* the render lock, because this edit
        is unguarded: a click can move the cursor while it waits here
        for an edit already in flight. Checking before the wait would
        let that click slip past and get the old level's cover and
        stats drawn over the new one. The check, the assignment and
        _draw()'s reads of the cursor then follow without an await
        between them, so no click can land in between either."""
        async with self._render_lock:
            if level_revision != self.cursor.level_revision:
                return
            self._enrichment = enrichment
            await self._draw(interaction, sync_attachments=True)

    async def _draw(
        self, interaction: discord.Interaction, sync_attachments: bool
    ) -> bool:
        """Draws the current cursor position over the message. The
        caller must hold the render lock (see render()).

        Nothing may be awaited between the first read of self.cursor
        below and the edit: build_items(), embed() and
        _attachment_key() each read the cursor separately, and a click
        landing between them would move it, leaving the components, the
        embed and the attachment key describing two different levels on
        one message.

        Returns True when the edit lands. When it raises
        discord.HTTPException (a 429 that outlives its retries, a 5xx)
        this puts the previous components back, logs the failure and
        returns False; any other exception puts them back and
        propagates. Putting them back keeps the message usable:
        build_items() detaches every old child before the edit, so each
        is pointed back at this view with Item._update_view() -
        discord.py drops a click on an item whose .view is None, and
        keeps the old items registered until the edit that replaces
        them lands. A click made during the edit therefore reaches
        interaction_check(). If the edit fails the old items are also
        put back as children, so the next edit serialises them and they
        keep working; once it lands, discord.py forgets their
        custom_ids and leaving their .view set does no harm.

        _attached is written back only once the edit has landed: a
        failed edit leaves whatever was already on the message."""
        previous = list(self.children)
        self.build_items()
        # pointed back at this view - see the docstring
        for item in previous:
            item._update_view(self)
        kwargs = {"embed": self.embed(), "view": self}
        attached = self._attached
        if sync_attachments:
            attached = self._attachment_key()
            if attached != self._attached:
                kwargs["attachments"] = self._attachments()
        try:
            await interaction.edit_original_response(**kwargs)
        except discord.HTTPException as e:
            self._restore_items(previous)
            print(f"library: render failed: {e}", file=sys.stderr)
            return False
        except BaseException:
            self._restore_items(previous)
            raise
        # only once the edit has landed - see the docstring
        self._attached = attached
        return True

    def _restore_items(self, items) -> None:
        """Puts back the components a failed edit left on the
        message, each with .view pointing at this view again."""
        self.clear_items()
        for item in items:
            self.add_item(item)

    async def turn_page(self, interaction: discord.Interaction, delta: int):
        """Applies a page delta under the double-click guard (see _busy).

        The guard matters on its own account: the button stays
        clickable until the edit lands and discord.py dispatches every
        click in its own task, so without it a double-click applies the
        delta twice. The cursor clamps the result - see
        BrowseCursor.page_by() for why an unclamped page is silently
        destructive.

        The turn is undone when its draw does not land (see
        _draw_or_undo()): the page goes back to the one the message
        still shows. It is restored by page_by(saved - page), never by
        page_by(-delta), because the clamp may have absorbed part of
        the delta. level_revision and _enrichment are not touched, so
        an enrichment in flight for this level still lands."""
        saved = self.cursor.page
        self.cursor.page_by(delta)
        await self._draw_or_undo(
            interaction,
            lambda: self.cursor.page_by(saved - self.cursor.page),
        )

    async def _draw_or_undo(
        self,
        interaction: discord.Interaction,
        undo: Callable[[], None],
    ) -> bool:
        """A cursor move's draw (a level's first draw or a page turn):
        under busy(), defers the interaction and renders the new
        position. When the draw does not land - render() returned
        False, or the defer or the render raised anything at all,
        cancellation included - calls `undo`, which puts the cursor
        back to what the message still shows, and then re-raises the
        exception, if there was one. Returns whether the draw landed.

        Every cursor-moving click goes through here, so none can
        forget the undo."""
        landed = False
        try:
            with self.busy():
                # deferred before render() for the reason given there:
                # a render can have to wait for one already in flight
                await interaction.response.defer()
                # Every move syncs attachments. On a level change they
                # really change, so the sync is required. On a page
                # turn they cannot, and the sync repairs a level whose
                # enrichment edit failed: that edit assigns
                # _enrichment before sending, so a failure (a 429 that
                # outlives its retries, a 5xx) leaves the embed asking
                # for attachment://cover.<ext> while the message
                # carries no such file - and every later page turn
                # would redraw that broken reference. It costs nothing
                # when the attachment key is unchanged: _draw()
                # compares the key first and omits the field.
                landed = await self.render(interaction, sync_attachments=True)
        finally:
            if not landed:
                undo()
        return landed

    async def descend(
        self,
        interaction: discord.Interaction,
        chosen: str,
        label: Optional[str] = None,
    ):
        previous = self._level_snapshot()
        descent = self.cursor.descend(chosen)
        if descent.changed_level:
            await self._enter_level(interaction, previous)
            return
        artist, album, _ = descent.track
        # ASCII throughout: unlike everything else built here this
        # ends up in print(), and a redirected stdout encodes with
        # the locale's codec rather than UTF-8
        source = f"browse: {artist} - {album} - {label}"
        await self.queue(interaction, [descent.track], source)

    async def go_back(self, interaction: discord.Interaction):
        previous = self._level_snapshot()
        if not self.cursor.back():
            # Already at the root: no level to enter, and nothing on
            # the message would change, so answer the interaction and
            # stop - an unanswered one shows the user "This
            # interaction failed" three seconds later.
            #
            # Defensive: BackButton is only added below the root, so a
            # click normally never reaches here. A Back left over from
            # a level already left is dispatchable until the edit
            # replacing it lands (_draw() keeps its view set), but the
            # guard turns such a click away while that edit is in
            # flight. It stays because back() returning False is part
            # of the cursor's contract, and one defer costs less than
            # being wrong about that.
            await interaction.response.defer()
            return
        await self._enter_level(interaction, previous)

    def _level_snapshot(self) -> LevelSnapshot:
        return LevelSnapshot(
            self.cursor.artist,
            self.cursor.album,
            self.cursor.page,
            self._enrichment,
        )

    def _undo_level_change(self, previous: LevelSnapshot) -> None:
        """Moves the cursor back to `previous` through its own API,
        after a level change whose first draw failed. The message -
        and the components _draw() restored - still show that level,
        so the cursor has to agree with them again. level_revision is
        left bumped: any enrichment still in flight for the old level
        was started before the move and is dropped either way."""
        artist, album, page, enrichment = previous
        depth = (artist is not None) + (album is not None)
        here = (self.cursor.artist is not None) + (
            self.cursor.album is not None
        )
        if here > depth:
            self.cursor.back()
        else:
            self.cursor.descend(album if album is not None else artist)
        self.cursor.page_by(page)
        self._enrichment = enrichment

    async def _enter_level(
        self, interaction: discord.Interaction, previous: LevelSnapshot
    ):
        """Shows the new level immediately, then fills the enrichment
        in behind it.

        A level's own contents - its entries, and every statistic in
        _add_stat_fields() - come straight out of the in-memory index,
        so the screen the user asked for can be drawn at once.
        Enrichment cannot: it reads tags and embedded artwork off disk
        and queries two HTTP backends, each with its own three-second
        timeout, so resolving it first would leave the *previous*
        level on screen for up to several seconds. There is nothing to
        soften that with, either - deferring a component interaction
        is a silent acknowledgement, not a spinner, so the stale
        screen keeps its buttons and looks entirely live.

        Only the first draw is made under _busy, so the second half
        of a double-click on a Select entry is turned away instead of
        skipping a level. Everything after it - the wait for the
        enrichment and the edit that draws it, which can upload
        megabytes of embedded artwork - is deliberately left
        unguarded: that is exactly when someone browsing quickly
        clicks again, and a click made then must be accepted, not
        dropped. Such a click moves the cursor at once, and its own
        redraw queues behind the enrichment edit on the render lock.
        An enrichment for a level the cursor has left is never drawn -
        see _render_enrichment(). Leaving a level does not cancel its
        enrichment either: the lookup finishes in the background and
        caches its result, so a revisit is instant.

        The cursor has already moved by the time this is called, and
        bumped level_revision doing it. Nothing may be awaited between
        that move and the capture below, or a click could bump it again
        first and this call would drop its own enrichment. (It holds:
        descend() and back() both mutate synchronously, and the first
        suspension here is the defer().) For the same reason
        level_revision has to stay a plain attribute - a coroutine
        property would put an await inside the staleness check in
        _render_enrichment() - and _enrichment has to stay on the view,
        since it describes what this message is showing rather than
        where the cursor is.

        `previous` is the caller's _level_snapshot() from before the
        move. A failed first draw is undone by _draw_or_undo(), which
        puts the cursor back to what the message still shows, so no
        enrichment is resolved for a level nobody can see."""
        level_revision = self.cursor.level_revision
        self._enrichment = None
        if not await self._draw_or_undo(
            interaction, lambda: self._undo_level_change(previous)
        ):
            return

        try:
            enrichment = await self._resolve_enrichment()
        except Exception as e:
            print(f"library: enrichment failed: {e}", file=sys.stderr)
            return

        # unguarded, staleness checked in _render_enrichment()
        await self._render_enrichment(interaction, level_revision, enrichment)

    async def queue_current_level(self, interaction: discord.Interaction):
        artist, album = self.cursor.artist, self.cursor.album
        if album is not None:
            source = f"browse: entire album {artist} - {album}"
        else:
            source = f"browse: entire discography of {artist}"
        await self.queue(interaction, self.cursor.tracks(), source)


class SearchSelect(discord.ui.Select):
    def __init__(
        self,
        results: List[library.SearchResult],
        search_view: "LibrarySearchView",
    ):
        self._results = results
        self.search_view = search_view
        super().__init__(
            placeholder="Choose...",
            options=_select_options((r.label, r.kind) for r in results),
        )

    async def callback(self, interaction: discord.Interaction):
        result = self._results[int(self.values[0])]
        await self.search_view.queue_result(interaction, result)


class LibrarySearchView(LibraryView):
    """The ranked hit list for one query. Unlike the browser this has
    no levels to descend through - picking any entry queues it, and
    the mixed kinds are what the per-option icons distinguish."""

    def __init__(
        self,
        ctx,
        index: library.LibraryIndex,
        query: str,
        results: List[library.SearchResult],
    ):
        super().__init__(ctx, index)
        self.query = query
        self.results = results
        self.add_item(SearchSelect(results, self))

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            # embed titles are rendered as plain text, so a query
            # containing markdown or a mention is inert here and only
            # needs trimming to stay under the 256-character cap
            title=f'Search: "{_trim(self.query, QUERY_ECHO_LIMIT)}"',
            color=config.EMBED_COLOR,
        )
        embed.set_author(name=AUTHOR_LINE)
        count = len(self.results)
        embed.set_footer(
            text=f"{count} closest match{'es' if count != 1 else ''}"
        )
        return embed

    async def queue_result(
        self, interaction: discord.Interaction, result: library.SearchResult
    ):
        source = f"search {self.query!r}: {result.kind} {result.label!r}"
        # No dispatch on result.kind: SearchResult already carries
        # exactly the path components its kind implies - album is None
        # for an artist hit, filename is set only for a song hit - so
        # the three cases are the three tracks_for() already handles.
        await self.queue(
            interaction,
            library.tracks_for(
                self.index, result.artist, result.album, result.filename
            ),
            source,
        )


class Library(commands.Cog):
    def __init__(self, bot: MusicBot):
        self.bot = bot

    async def cog_check(self, ctx):
        ctx.audiocontroller = get_audiocontroller(ctx)
        return True

    async def cog_before_invoke(self, ctx):
        ctx.audiocontroller.command_channel = ctx

    # `lib` is a prefix-only alias: discord.py registers aliases for
    # the text form of a hybrid command, not as extra slash commands,
    # so `d!lib browse` works while the slash form stays `/library`
    # (same as `d!p` against `/play` elsewhere in this bot).
    @commands.hybrid_group(
        name="library",
        aliases=["lib"],
        description=config.HELP_LIBRARY_SHORT,
        help=config.HELP_LIBRARY_LONG,
        invoke_without_command=True,
    )
    async def _library(self, ctx):
        await ctx.send("Use subcommands: `search`, `browse`, `refresh`.")

    @_library.command(
        name="refresh",
        description=config.HELP_LIBRARY_REFRESH_SHORT,
        help=config.HELP_LIBRARY_REFRESH_LONG,
    )
    # owner-only rather than DJ: only whoever runs the host can add
    # files to MUSIC_LIBRARY_PATH in the first place, so nobody else
    # has a reason to rescan it
    @commands.check(owner_check)
    async def _library_refresh(self, ctx):
        if not config.MUSIC_LIBRARY_PATH:
            await ctx.send(config.LIBRARY_NOT_CONFIGURED)
            return
        await ctx.defer()
        index = await library.build_index_async()
        # the enrichment caches hold tag and artwork reads keyed by
        # file path; a rescan exists to pick up what changed on disk,
        # so they have to go with it
        library_metadata.clear_caches()
        artists, albums, songs = library.counts(index)
        await ctx.send(
            config.LIBRARY_REFRESHED.format(
                artists=artists, albums=albums, songs=songs
            )
        )

    @_library.command(
        name="search",
        description=config.HELP_LIBRARY_SEARCH_SHORT,
        help=config.HELP_LIBRARY_SEARCH_LONG,
    )
    @app_commands.describe(query="Artist, album or song to look for")
    @joining_command
    async def _library_search(self, ctx, *, query: str):
        if not config.MUSIC_LIBRARY_PATH:
            await ctx.send(config.LIBRARY_NOT_CONFIGURED)
            return
        index = library.get_index()
        if not index:
            await ctx.send(config.LIBRARY_EMPTY)
            return

        # ephemeral here as well as on the sends below: the deferred
        # placeholder's visibility is fixed when it's created, so a
        # public defer would leave a stray public message behind the
        # ephemeral result. typing() rather than defer() so the prefix
        # path, where deferring does nothing, still shows the user
        # something while a big library is scored.
        async with ctx.typing(ephemeral=True):
            async with _search_lock:
                results = await library.search_async(index, query)
        if not results:
            kwargs = {
                # the query is echoed back, so deny it any ability to
                # ping on top of escaping its markdown
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            # matches the deferral above, and matches the hit-list send
            # below. Without it Context.send takes its public-message
            # path, which moves the playback controls off the
            # now-playing message and onto this one - which the
            # ephemeral deferral then hides from everyone but the
            # searcher.
            if ctx.interaction is not None:
                kwargs["ephemeral"] = True
            await ctx.send(
                config.LIBRARY_SEARCH_NO_RESULTS.format(
                    query=discord.utils.escape_markdown(
                        _trim(query, QUERY_ECHO_LIMIT)
                    )
                ),
                **kwargs,
            )
            return

        view = LibrarySearchView(ctx, index, query, results)
        kwargs = {"embed": view.embed(), "view": view}
        if ctx.interaction is not None:
            kwargs["ephemeral"] = True
        view.message = await ctx.send(**kwargs)

    @_library.command(
        name="browse",
        description=config.HELP_LIBRARY_BROWSE_SHORT,
        help=config.HELP_LIBRARY_BROWSE_LONG,
    )
    @joining_command
    async def _library_browse(self, ctx):
        if not config.MUSIC_LIBRARY_PATH:
            await ctx.send(config.LIBRARY_NOT_CONFIGURED)
            return
        # taken once and handed in: called twice, a `d!lib refresh`
        # landing between them (it replaces the module global) would
        # give the view a different - possibly empty - index than the
        # one that passed this check
        index = library.get_index()
        if not index:
            await ctx.send(config.LIBRARY_EMPTY)
            return

        view = LibraryBrowseView(ctx, index)
        kwargs = {"embed": view.embed(), "view": view}
        # ephemeral only makes sense for an interaction (slash) response -
        # a plain text message from a prefix command can't be ephemeral
        if ctx.interaction is not None:
            kwargs["ephemeral"] = True
        # whatever this hands back, OwnedView.refresh_message() knows
        # what to do with it - see the note on OwnedView.message
        view.message = await ctx.send(**kwargs)


async def setup(bot: MusicBot):
    await bot.add_cog(Library(bot))
