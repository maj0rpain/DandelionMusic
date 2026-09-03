import asyncio
import io
import sys
from contextlib import contextmanager
from pathlib import Path
from traceback import print_exc
from typing import List, NamedTuple, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from config import config
from musicbot import library, library_metadata
from musicbot.bot import MusicBot
from musicbot.utils import CheckError, owner_check, play_check

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


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\u2026"


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
    for limit, suffix in (
        (1_000_000_000, "B"),
        (1_000_000, "M"),
        (1_000, "K"),
    ):
        if value >= limit:
            return f"{value / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def _fmt_years(stats: library.LevelStats) -> Optional[str]:
    if not stats.year_min:
        return None
    if stats.year_max and stats.year_max != stats.year_min:
        return f"{stats.year_min}\u2013{stats.year_max}"
    return str(stats.year_min)


class LevelData(NamedTuple):
    """Everything one browse screen derives from the index: the
    entries, their display labels, and the aggregate statistics for
    the level. All of it is a pure function of (index, artist, album)
    - the index is a snapshot taken when the view was built and never
    replaced - so it is computed once per level rather than per
    render.

    It used to be computed several times per *render*: build_items()
    asked for entries and for labels, which are the same list above the
    song level, and embed() asked a third time just to decide whether
    the level was empty. At the root each of those is a sort of every
    artist in the library, next to a counts() walk of every album and
    every song - and all of it ran again on each page turn, where by
    definition nothing has changed."""

    entries: List[str]
    labels: List[str]
    counts: Optional[Tuple[int, int, int]]  # root level only
    stats: Optional[library.LevelStats]  # every level below the root


class LibrarySelect(discord.ui.Select):
    def __init__(
        self,
        entries: List[str],
        labels: List[str],
        kind: str,
        browse_view: "LibraryBrowseView",
    ):
        self._entries = entries
        self.browse_view = browse_view
        super().__init__(
            placeholder="Choose...",
            options=[
                discord.SelectOption(
                    label=label[:100],
                    value=str(i),
                    emoji=KIND_EMOJI[kind],
                )
                for i, label in enumerate(labels)
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        index = int(self.values[0])
        chosen = self._entries[index]
        # the option's own label, not the underlying entry (a raw
        # filename at the song level) - see queue_songs()'s `source`
        label = self.options[index].label
        await self.browse_view.descend(interaction, chosen, label)


class BackButton(discord.ui.Button):
    def __init__(self, browse_view: "LibraryBrowseView"):
        super().__init__(label="Back", style=discord.ButtonStyle.grey, row=1)
        self.browse_view = browse_view

    async def callback(self, interaction: discord.Interaction):
        await self.browse_view.go_back(interaction)


class PageButton(discord.ui.Button):
    def __init__(
        self, browse_view: "LibraryBrowseView", delta: int, label: str
    ):
        super().__init__(label=label, style=discord.ButtonStyle.blurple, row=2)
        self.browse_view = browse_view
        self.delta = delta

    async def callback(self, interaction: discord.Interaction):
        await self.browse_view.turn_page(interaction, self.delta)


class QueueLevelButton(discord.ui.Button):
    def __init__(self, browse_view: "LibraryBrowseView"):
        label = (
            "Queue this Album"
            if browse_view.album is not None
            else "Queue this Artist"
        )
        super().__init__(label=label, style=discord.ButtonStyle.green, row=1)
        self.browse_view = browse_view

    async def callback(self, interaction: discord.Interaction):
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
    if not interaction.response.is_done():
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

    # walked twice below (once to build the URIs, once to name what
    # was skipped), so it must not be something that can be consumed
    triples = list(triples)

    # play_check() is inside this, not ahead of it: it connects to
    # voice, and a failed connect raises asyncio.TimeoutError or
    # discord.ClientException rather than CheckError. The placeholder
    # above is a visible message now, so anything escaping here leaves
    # it saying "Queueing..." forever - discord.py logs the traceback
    # and the user is told nothing at all.
    try:
        await play_check(ctx)
        tracks = [library.song_uri(*triple) for triple in triples]
        songs = await ctx.audiocontroller.process_local_tracks(
            tracks, source, user=ctx.author
        )
    except CheckError as e:
        await interaction.edit_original_response(content=str(e))
        return
    except Exception:
        print_exc(file=sys.stderr)
        await interaction.edit_original_response(content=config.SONGINFO_ERROR)
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

    await interaction.edit_original_response(content=message)


class LibraryView(discord.ui.View):
    """Ownership and lifetime handling shared by the browser and the
    search results - both are single-user views on a message that
    outlives their 5-minute timeout."""

    def __init__(self, ctx, index: library.LibraryIndex):
        super().__init__(timeout=300)
        self.ctx = ctx
        # the snapshot this view was built from: a `d!library refresh`
        # landing mid-session must not change what the already-shown
        # entries point at
        self.index = index
        # serializes the message edits themselves. discord.py
        # dispatches every click in its own task, and deferring a
        # component interaction clears the click spinner and re-enables
        # the view immediately rather than showing a "thinking"
        # placeholder, so without this two overlapping handlers could
        # edit the same message out of order. Held only across the
        # edits - never across the slow work that produces them, or a
        # rapid click would spend seconds being refused.
        #
        # A depth count rather than a flag, because two handlers can
        # legitimately hold it at once: a queue runs unguarded work in
        # the middle, and an enrichment that resolves during it would,
        # as a flag, clear the queue's guard on the way out and let a
        # second click queue the same album twice.
        self._busy: int = 0
        # whatever Context.send handed back, which is not always a
        # Message: the prefix paths and the deferred search path give
        # a Message/WebhookMessage, but answering an interaction
        # directly returns an InteractionCallbackResponse (discord.py
        # >= 2.5) that has no edit(). on_timeout() below discriminates
        # on the type rather than on which path ran.
        self.message = None

    @contextmanager
    def busy(self):
        """Refuses clicks for the duration - see _busy."""
        self._busy += 1
        try:
            yield
        finally:
            self._busy -= 1

    async def interaction_check(
        self, interaction: discord.Interaction
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This belongs to someone else.", ephemeral=True
            )
            return False
        if self._busy:
            # acknowledged silently rather than answered with an
            # ephemeral complaint: the guard's window is now a single
            # message edit for navigation and paging, so the clicks it
            # catches are overwhelmingly the second half of a
            # double-click. Telling someone off for that reads as a
            # malfunction. The one operation still slow enough to be
            # worth explaining - a bulk queue - shows its own
            # "thinking" placeholder while it runs, so the user can
            # already see why nothing else is responding.
            await interaction.response.defer()
            return False
        return True

    async def queue(
        self, interaction: discord.Interaction, triples, source: str
    ) -> None:
        """Queues through the _busy guard. queue_songs() defers, and
        deferring re-enables the select at once - the "thinking"
        placeholder it puts up is a separate ephemeral message, not a
        lock on the view - so without this a second click while the
        batch is still loading would queue the same album or
        discography twice over."""
        with self.busy():
            await queue_songs(self.ctx, interaction, triples, source)

    async def on_timeout(self):
        # Without this, a click after the 5-minute timeout just fails
        # silently client-side (discord.py stops dispatching to a
        # timed-out view's items) and the message's Select/Buttons
        # stay visibly enabled forever.
        for item in self.children:
            item.disabled = True
        try:
            # A view sent after a defer goes out as a followup with its
            # own id, so @original is only the deferred placeholder and
            # editing it would leave the real components enabled
            # forever - those have to be edited directly. Answering an
            # interaction without deferring puts the view on the
            # original response, and hands back an
            # InteractionCallbackResponse rather than the message, so
            # that case has to go through the interaction. Testing what
            # we actually hold keeps this right whichever path ran.
            if isinstance(self.message, discord.Message):
                await self.message.edit(view=self)
            elif self.ctx.interaction is not None:
                await self.ctx.interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass


class LibraryBrowseView(LibraryView):
    def __init__(self, ctx):
        super().__init__(ctx, library.get_index())
        self.artist: Optional[str] = None
        self.album: Optional[str] = None
        self.page = 0
        self._enrichment: Optional[library_metadata.Enrichment] = None
        # bumped on every level change, so an enrichment that resolves
        # after the user has navigated on can tell that it describes a
        # level nobody is looking at any more and drop itself
        self._nav = 0
        # what _attachment_key() described the last time an edit
        # actually carried an `attachments` field - see render()
        self._attached: Optional[str] = None
        # (level key, data) for the level currently shown - see level()
        self._level: Optional[
            Tuple[Tuple[Optional[str], Optional[str]], LevelData]
        ] = None
        # serialises the message edits themselves - see render()
        self._render_lock = asyncio.Lock()
        self.build_items()

    def _songs(self) -> List[library.LibrarySong]:
        return self.index.get(self.artist, {}).get(self.album, [])

    def _first_sample_file(self) -> Optional[Path]:
        # only ever called at the artist level, whose entries are
        # already that artist's sorted album folder names
        albums = self.level().entries
        if not albums:
            return None
        songs = self.index.get(self.artist, {}).get(albums[0], [])
        if not songs:
            return None
        return library.song_path(self.artist, albums[0], songs[0].filename)

    async def _resolve_enrichment(
        self,
    ) -> Optional[library_metadata.Enrichment]:
        if self.artist is None:
            return None
        if self.album is None:
            sample = self._first_sample_file()
            if sample is None:
                return None
            return await library_metadata.get_artist_enrichment(
                self.artist, sample
            )
        songs = self._songs()
        if not songs:
            return None
        sample = library.song_path(self.artist, self.album, songs[0].filename)
        return await library_metadata.get_album_enrichment(
            self.artist, self.album, sample
        )

    def _build_level(self) -> LevelData:
        if self.artist is None:
            entries = sorted(self.index.keys())
            # entries and labels are the same list above the song
            # level; nothing mutates either, so they can share it
            return LevelData(
                entries, entries, library.counts(self.index), None
            )
        if self.album is None:
            entries = sorted(self.index.get(self.artist, {}).keys())
            return LevelData(
                entries,
                entries,
                None,
                library.artist_stats(self.index, self.artist),
            )
        songs = self._songs()
        return LevelData(
            # already sorted by filename in build_index(), preserving
            # track-number order - don't re-sort
            [song.filename for song in songs],
            [song.title for song in songs],
            None,
            library.album_stats(self.index, self.artist, self.album),
        )

    def level(self) -> LevelData:
        """The current level's data, computed on first use and held
        until the level changes. One slot rather than a per-level
        cache: what this is worth is not recomputing within a render or
        across a page turn, and keeping every level a long browse
        touched would retain the whole index a second time over."""
        key = (self.artist, self.album)
        if self._level is None or self._level[0] != key:
            self._level = (key, self._build_level())
        return self._level[1]

    def entries(self) -> List[str]:
        """Selection values - filenames at the song level, folder
        names above it. Always use these (not labels()) for anything
        that needs to look the entry back up in the index."""
        return self.level().entries

    def labels(self) -> List[str]:
        """Display text, parallel to entries() - tag-derived titles
        at the song level, folder names above it."""
        return self.level().labels

    def level_kind(self) -> str:
        """What the entries at this level are - every option in one
        screen's Select is the same kind of thing."""
        if self.artist is None:
            return "artist"
        if self.album is None:
            return "album"
        return "song"

    def title(self) -> str:
        """Only the current scope, prefixed with the icon for what
        that scope is - the path to it lives in the footer, and
        "Music Library" in the author line."""
        if self.artist is None:
            return f"{KIND_EMOJI['artist']} Artists"
        if self.album is None:
            return f"{KIND_EMOJI['artist']} {self.artist}"
        return f"{KIND_EMOJI['album']} {self.album}"

    def footer(self) -> Optional[str]:
        """In the breadcrumb the icons mark entities only - the
        leading "Artists" names the root screen rather than an artist,
        so it stays plain. title() runs the other way round, because
        there "Artists" *is* the screen being shown and takes the icon
        for the kind of thing it lists."""
        if self.artist is None:
            return None
        artist = f"{KIND_EMOJI['artist']} {self.artist}"
        if self.album is None:
            return f"Artists \u203a {artist}"
        return f"{artist} \u203a {KIND_EMOJI['album']} {self.album}"

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
        level = self.level()
        if self.artist is None:
            artists, albums, songs = level.counts
            self._field(embed, "Artists", f"{artists:,}")
            self._field(embed, "Albums", f"{albums:,}")
            self._field(embed, "Songs", f"{songs:,}")
            return

        local = level.stats
        if self.album is None:
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
        if not self.entries():
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
        return f"{self.artist}/{self.album}.{art.extension}"

    def build_items(self):
        self.clear_items()
        level = self.level()
        entries = level.entries
        labels = level.labels
        page = slice(self.page * PAGE_SIZE, (self.page + 1) * PAGE_SIZE)
        page_entries = entries[page]
        page_labels = labels[page]
        if page_entries:
            self.add_item(
                LibrarySelect(
                    page_entries, page_labels, self.level_kind(), self
                )
            )
        if self.artist is not None:
            self.add_item(QueueLevelButton(self))
            self.add_item(BackButton(self))
        if self.page > 0:
            self.add_item(PageButton(self, -1, "◀ Prev"))
        if (self.page + 1) * PAGE_SIZE < len(entries):
            self.add_item(PageButton(self, 1, "Next ▶"))

    async def render(
        self,
        interaction: discord.Interaction,
        sync_attachments: bool = False,
    ):
        """Redraws the message. `interaction` must already have been
        deferred - every edit goes out as a followup, so that waiting
        on the lock below can never eat the three seconds an
        interaction has to be answered in.

        Serialised, because _busy does not cover this. _busy turns away
        new *clicks*; the enrichment edit in _enter_level() is not one.
        It resumes on its own after a wait that is deliberately
        unguarded, so it can arrive here while a page turn's edit is
        still in flight. Two edits to one message would then land in
        either order - drawing the enrichment and then replacing it
        with the page turn's older embed - and each would write back an
        _attached it sampled before the other ran. That last part is
        the lasting damage: the record of what the message carries ends
        up disagreeing with the message, so a later navigation omits an
        `attachments` field it needed and strands a cover under an
        embed that no longer references it."""
        async with self._render_lock:
            self.build_items()
            kwargs = {"embed": self.embed(), "view": self}
            attached = self._attached
            if sync_attachments:
                attached = self._attachment_key()
                if attached != self._attached:
                    kwargs["attachments"] = self._attachments()
            await interaction.edit_original_response(**kwargs)
            # only once the edit has landed: a failed edit leaves
            # whatever was already on the message
            self._attached = attached

    async def turn_page(self, interaction: discord.Interaction, delta: int):
        """Applies a page delta under the same guard as everything
        else, and clamps the result.

        Both matter. The button stays clickable until the edit lands
        and discord.py dispatches every click in its own task, so a
        double-click used to apply the delta twice; the guard is what
        keeps the second click from doing that, and the clamp is what
        keeps any other route to an out-of-range page from being
        silently destructive. Out of range in either direction the page
        slice comes back empty - past the end because there is nothing
        there, and below zero because slice(-25, 0) selects nothing -
        and build_items() then draws a screen with no Select on it at
        all, and no button that leads back."""
        last = max(0, (len(self.entries()) - 1) // PAGE_SIZE)
        self.page = min(max(self.page + delta, 0), last)
        with self.busy():
            # deferred before render() for the reason given there: a
            # render can have to wait for one already in flight
            await interaction.response.defer()
            # Syncs attachments even though a page turn cannot change
            # them, so that this heals a level whose enrichment edit
            # failed. That edit assigns _enrichment before sending, so
            # a failure (a 429 that outlives its retries, a 5xx) leaves
            # the embed asking for attachment://cover.<ext> while the
            # message carries no such file - and every later page turn
            # would redraw that broken reference. Costs nothing when
            # nothing has changed: render() compares the key first and
            # omits the field.
            await self.render(interaction, sync_attachments=True)

    async def descend(
        self,
        interaction: discord.Interaction,
        chosen: str,
        label: Optional[str] = None,
    ):
        # only a change of level resets the page. Queueing a track
        # doesn't re-render, so zeroing it there would leave the
        # displayed page and self.page disagreeing, and the next
        # "Next" click would jump back to page 1.
        if self.artist is None:
            self.page = 0
            self.artist = chosen
            await self._enter_level(interaction)
        elif self.album is None:
            self.page = 0
            self.album = chosen
            await self._enter_level(interaction)
        else:
            source = f"browse: {self.artist} – {self.album} – {label}"
            await self.queue(
                interaction, [(self.artist, self.album, chosen)], source
            )

    async def go_back(self, interaction: discord.Interaction):
        self.page = 0
        if self.album is not None:
            self.album = None
        else:
            self.artist = None
        await self._enter_level(interaction)

    async def _enter_level(self, interaction: discord.Interaction):
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

        The two edits are each made under _busy so a second click
        can't interleave its own edit between them, but the wait
        between them is deliberately left unguarded: that is exactly
        when someone browsing quickly clicks again, and they should be
        able to."""
        self._nav += 1
        nav = self._nav
        self._enrichment = None
        with self.busy():
            await interaction.response.defer()
            await self.render(interaction, sync_attachments=True)

        try:
            enrichment = await self._resolve_enrichment()
        except Exception as e:
            print(f"library: enrichment failed: {e}", file=sys.stderr)
            return

        # Checked *and* acted on without awaiting in between, so a
        # click can neither slip past the check nor find _busy clear
        # while this second edit is in flight.
        if nav != self._nav:
            return
        with self.busy():
            self._enrichment = enrichment
            await self.render(interaction, sync_attachments=True)

    async def queue_current_level(self, interaction: discord.Interaction):
        if self.album is not None:
            triples = [
                (self.artist, self.album, song.filename)
                for song in self._songs()
            ]
            source = f"browse: entire album {self.artist} – {self.album}"
        else:
            triples = [
                (self.artist, album, song.filename)
                for album, songs in self.index.get(self.artist, {}).items()
                for song in songs
            ]
            source = f"browse: entire discography of {self.artist}"
        await self.queue(interaction, triples, source)


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
            options=[
                discord.SelectOption(
                    label=result.label[:100],
                    value=str(i),
                    emoji=KIND_EMOJI[result.kind],
                )
                for i, result in enumerate(results)
            ],
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

    def _expand(self, result: library.SearchResult):
        """One hit to the (artist, album, filename) triples it stands
        for - a song is itself, an album is its tracks, an artist is
        their whole discography."""
        if result.kind == "song":
            return [(result.artist, result.album, result.filename)]
        albums = self.index.get(result.artist, {})
        if result.kind == "album":
            return [
                (result.artist, result.album, song.filename)
                for song in albums.get(result.album, [])
            ]
        return [
            (result.artist, album, song.filename)
            for album, songs in albums.items()
            for song in songs
        ]

    async def queue_result(
        self, interaction: discord.Interaction, result: library.SearchResult
    ):
        source = f"search {self.query!r}: {result.kind} {result.label!r}"
        await self.queue(interaction, self._expand(result), source)


class Library(commands.Cog):
    def __init__(self, bot: MusicBot):
        self.bot = bot

    async def cog_check(self, ctx):
        ctx.audiocontroller = ctx.bot.audio_controllers[ctx.guild]
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
    async def _library_browse(self, ctx):
        if not config.MUSIC_LIBRARY_PATH:
            await ctx.send(config.LIBRARY_NOT_CONFIGURED)
            return
        if not library.get_index():
            await ctx.send(config.LIBRARY_EMPTY)
            return

        view = LibraryBrowseView(ctx)
        kwargs = {"embed": view.embed(), "view": view}
        # ephemeral only makes sense for an interaction (slash) response -
        # a plain text message from a prefix command can't be ephemeral
        if ctx.interaction is not None:
            kwargs["ephemeral"] = True
        # whatever this hands back, LibraryView.on_timeout() knows what
        # to do with it - see the note on LibraryView.message
        view.message = await ctx.send(**kwargs)


async def setup(bot: MusicBot):
    await bot.add_cog(Library(bot))
