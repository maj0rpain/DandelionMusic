import asyncio
import io
import sys
from contextlib import contextmanager
from traceback import print_exc
from typing import Callable, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from config import config
from musicbot import admission, library, library_metadata
from musicbot.bot import MusicBot
from musicbot.outcome import render
from musicbot.utils import (
    CheckError,
    get_audiocontroller,
    owner_check,
    joining_command,
)
from musicbot.library_display import KIND_EMOJI, trim
from musicbot.library_session import BrowseSession, Frame
from musicbot.views import OwnedView, admit_click

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

# `query` is a consume-rest parameter, so a prefix invocation can fill
# it with most of a 2000-character message. Anything echoed back has
# to be trimmed first, or the reply itself blows the message limit.
QUERY_ECHO_LIMIT = 100


# A select option label holds 100 UTF-16 code units, and the "\u2026" trim
# appends takes one, leaving 99 for the name.
SELECT_LABEL_LIMIT = 99


def _select_options(pairs) -> List[discord.SelectOption]:
    """Select options from `(label, kind)` pairs: the trimmed label,
    the pair's position as the value, the kind's emoji."""
    return [
        discord.SelectOption(
            label=trim(label, SELECT_LABEL_LIMIT),
            value=str(i),
            emoji=KIND_EMOJI[kind],
        )
        for i, (label, kind) in enumerate(pairs)
    ]


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
    def __init__(self, browse_view: "LibraryBrowseView", action):
        self.action = action
        super().__init__(
            browse_view,
            label=action.label,
            style=discord.ButtonStyle.green,
            row=1,
        )

    async def on_click(self, interaction: discord.Interaction):
        await self.browse_view.queue_current_level(
            interaction, self.action.source
        )


async def queue_songs(interaction, triples, source: str) -> None:
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

    # The checks and the join are inside this, not ahead of it: the
    # placeholder above is a visible message now, so anything
    # escaping here leaves it saying "Queueing..." forever -
    # discord.py logs the traceback and the user is told nothing at
    # all. enter() turns a failed connect into a CheckError, so
    # it is reported as one rather than as a song-info error.
    try:
        ctx, controller = await admit_click(
            interaction, admission.RequestKind.PICK
        )
        tracks = [library.LocalTrack.of(*triple) for triple in triples]
        outcome = await controller.process_local_tracks(
            tracks, source, user=ctx.author
        )
    except CheckError as e:
        await reply(str(e))
        return
    except Exception:
        print_exc(file=sys.stderr)
        await reply(config.SONGINFO_ERROR)
        return

    rendered = render(outcome, announce_songs=False)
    if rendered is not None:
        await reply(rendered.text)
    elif owns_placeholder:
        await interaction.delete_original_response()


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
            await queue_songs(interaction, triples, source)


class LibraryBrowseView(LibraryView):
    """The Discord half of the browser: components, message edits,
    deferral, the _busy guard and the enrichment lookup. What the
    message shows, and how it moves, is self.session's - see
    musicbot/library_session.py, which is testable precisely because
    none of this is in it. Nothing here may keep its own copy of the
    session's state; read it back through self.session every time."""

    def __init__(self, ctx, index: library.LibraryIndex):
        super().__init__(ctx, index)
        self.session = BrowseSession(index, PAGE_SIZE)
        # serialises the message edits themselves - see render()
        self._render_lock = asyncio.Lock()

    @classmethod
    def open(cls, ctx, index: library.LibraryIndex):
        """The view and the frame its items were built from; the
        caller sends that frame's embed and then hands the same frame
        to session.landed()."""
        view = cls(ctx, index)
        frame = view.session.frame()
        view.build_items(frame)
        return view, frame

    @property
    def scope(self) -> Tuple[Optional[str], Optional[str]]:
        """The (artist, album) pair of the level shown now."""
        return self.session.scope()

    @staticmethod
    def embed(frame: Frame) -> discord.Embed:
        embed = discord.Embed(title=frame.title, color=config.EMBED_COLOR)
        embed.set_author(name=AUTHOR_LINE)
        if frame.footer:
            embed.set_footer(text=frame.footer)
        if frame.empty:
            embed.description = config.LIBRARY_EMPTY
        else:
            embed.description = frame.description
        for name, value in frame.fields:
            embed.add_field(name=name, value=value, inline=True)
        if frame.thumbnail:
            attached = frame.attachment is not None and (
                frame.thumbnail == frame.attachment.filename
            )
            embed.set_thumbnail(
                url=(
                    f"attachment://{frame.thumbnail}"
                    if attached
                    else frame.thumbnail
                )
            )
        return embed

    @staticmethod
    def _attachments(frame: Frame) -> List[discord.File]:
        if frame.attachment is None:
            return []
        return [
            discord.File(
                io.BytesIO(frame.attachment.data),
                filename=frame.attachment.filename,
            )
        ]

    def build_items(self, frame: Frame):
        self.clear_items()
        if frame.entries:
            self.add_item(
                LibrarySelect(frame.entries, frame.labels, frame.kind, self)
            )
        if frame.queue is not None:
            self.add_item(QueueLevelButton(self, frame.queue))
            self.add_item(BackButton(self))
        if frame.show_prev:
            self.add_item(PageButton(self, -1, "◀ Prev"))
        if frame.show_next:
            self.add_item(PageButton(self, 1, "Next ▶"))

    async def render(self, interaction: discord.Interaction) -> bool:
        """The locked entry point for redrawing the message; see _draw()
        for the rules the draw keeps. `interaction` must already have
        been deferred - every edit goes out as a followup, so that
        waiting on the lock can never eat the three seconds an
        interaction has to be answered in.

        The lock exists because two edits to one message could
        otherwise land in either order, each writing back an attachment
        key sampled before the other ran, so a later navigation omits
        an `attachments` field it needed. A click's edit and the
        enrichment edit (which waits on this lock, and is not covered
        by _busy - see _enter_level()) are the two that can overlap.

        A cursor move whose draw fails here is undone by its caller
        (see _draw_or_undo()) on a False return and on any exception
        alike."""
        async with self._render_lock:
            return await self._draw(interaction)

    async def _render_enrichment(
        self,
        interaction: discord.Interaction,
        token: int,
        enrichment: Optional[library_metadata.Enrichment],
    ) -> None:
        """Draws `enrichment` over the level it was resolved for, unless
        the session says the cursor has left that level, in which case
        nothing is drawn.

        The offer is made *inside* the render lock, because this edit
        is unguarded: a click can move the cursor while it waits here
        for an edit already in flight. Offering before the wait would
        let that click slip past and get the old level's cover and
        stats drawn over the new one. The offer and _draw()'s frame
        then follow without an await between them."""
        async with self._render_lock:
            if not self.session.offer_enrichment(token, enrichment):
                return
            await self._draw(interaction)

    async def _draw(self, interaction: discord.Interaction) -> bool:
        """Draws the session's current frame over the message. The
        caller must hold the render lock (see render()).

        The frame is built in one synchronous call, and nothing is
        awaited between building it and the edit, so the components,
        the embed and the attachments describe one level.

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

        The session's landed() is called only once the edit has
        landed: a failed edit leaves whatever was already on the
        message."""
        frame = self.session.frame()
        previous = list(self.children)
        self.build_items(frame)
        # pointed back at this view - see the docstring
        for item in previous:
            item._update_view(self)
        kwargs = {"embed": self.embed(frame), "view": self}
        if frame.attachments_change:
            kwargs["attachments"] = self._attachments(frame)
        try:
            await interaction.edit_original_response(**kwargs)
        except discord.HTTPException as e:
            self._restore_items(previous)
            print(f"library: render failed: {e}", file=sys.stderr)
            return False
        except BaseException:
            self._restore_items(previous)
            raise
        self.session.landed(frame)
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
        _draw_or_undo()) by session.restore_position(), which leaves
        the drawn enrichment and level_revision alone, so an
        enrichment in flight for this level still lands."""
        position = self.session.position()
        self.session.page_by(delta)
        await self._draw_or_undo(
            interaction,
            lambda: self.session.restore_position(position),
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
        cancellation included - calls `undo`, which puts the session
        back to what the message still shows, and then re-raises the
        exception, if there was one. Returns whether the draw landed.

        Every cursor-moving click goes through here, so none can
        forget the undo. Every draw compares the attachment key, so a
        page turn also repairs a level whose enrichment edit failed:
        that edit's enrichment stays drawn, and the next frame carries
        the attachment the message lacks."""
        landed = False
        try:
            with self.busy():
                # deferred before render() for the reason given there:
                # a render can have to wait for one already in flight
                await interaction.response.defer()
                landed = await self.render(interaction)
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
        previous = self.session.snapshot()
        descent = self.session.descend(chosen)
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
        previous = self.session.snapshot()
        if not self.session.back():
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

    async def _enter_level(self, interaction: discord.Interaction, previous):
        """Shows the new level immediately, then fills the enrichment
        in behind it.

        A level's own contents come straight out of the in-memory
        index, so the screen the user asked for can be drawn at once.
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
        that move and enter_level() below, or a click could bump it
        again first and this call would drop its own enrichment. (It
        holds: descend() and back() both mutate synchronously, and the
        first suspension here is the defer().)

        `previous` is the caller's session.snapshot() from before the
        move. A failed first draw is undone by _draw_or_undo(), which
        restores it, so no enrichment is resolved for a level nobody
        can see."""
        token = self.session.enter_level()
        if not await self._draw_or_undo(
            interaction, lambda: self.session.restore(previous)
        ):
            return

        try:
            enrichment = await library_metadata.get_enrichment(
                self.index, *self.scope
            )
        except Exception as e:
            print(f"library: enrichment failed: {e}", file=sys.stderr)
            return

        # unguarded, staleness checked in _render_enrichment()
        await self._render_enrichment(interaction, token, enrichment)

    async def queue_current_level(
        self, interaction: discord.Interaction, source: str
    ):
        await self.queue(interaction, self.session.tracks(), source)


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
            title=f'Search: "{trim(self.query, QUERY_ECHO_LIMIT)}"',
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


async def _send_private(ctx, **kwargs):
    """Send a library reply, ephemeral when the context is a slash
    interaction, and return what `ctx.send` returned.

    Ephemeral only makes sense for an interaction response: a plain
    text message from a prefix command can't be ephemeral. It matches
    search's ephemeral deferral (a public reply behind an ephemeral
    placeholder would leave a stray message) and keeps Context.send off
    its public-message path, which moves the playback controls off the
    player panel and onto this reply - which the ephemeral deferral
    would then hide from everyone but the requester.
    """
    if ctx.interaction is not None:
        kwargs["ephemeral"] = True
    return await ctx.send(**kwargs)


class Library(commands.Cog):
    def __init__(self, bot: MusicBot):
        self.bot = bot

    async def cog_check(self, ctx):
        if not config.MUSIC_LIBRARY_PATH:
            raise CheckError(config.LIBRARY_NOT_CONFIGURED)
        # kept for its not-ready refusal (CheckError), not for the value
        get_audiocontroller(ctx)
        return True

    async def cog_before_invoke(self, ctx):
        ctx.bot.sessions.direct_panel(ctx.guild, ctx.channel)

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
        names = sorted(c.name for c in ctx.command.commands if not c.hidden)
        listed = ", ".join(f"`{n}`" for n in names)
        await ctx.send(f"Use subcommands: {listed}.")

    @_library.command(
        name="refresh",
        hidden=True,
        description=config.HELP_LIBRARY_REFRESH_SHORT,
        help=config.HELP_LIBRARY_REFRESH_LONG,
    )
    # owner-only rather than DJ: only whoever runs the host can add
    # files to MUSIC_LIBRARY_PATH in the first place, so nobody else
    # has a reason to rescan it
    @commands.check(owner_check)
    async def _library_refresh(self, ctx):
        await ctx.defer()
        index = await library.build_index_async()
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
            await _send_private(
                ctx,
                content=config.LIBRARY_SEARCH_NO_RESULTS.format(
                    query=discord.utils.escape_markdown(
                        trim(query, QUERY_ECHO_LIMIT)
                    )
                ),
                # the query is echoed back, so deny it any ability to
                # ping on top of escaping its markdown
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        view = LibrarySearchView(ctx, index, query, results)
        view.message = await _send_private(ctx, embed=view.embed(), view=view)

    @_library.command(
        name="browse",
        description=config.HELP_LIBRARY_BROWSE_SHORT,
        help=config.HELP_LIBRARY_BROWSE_LONG,
    )
    @joining_command
    async def _library_browse(self, ctx):
        # taken once and handed in: called twice, a `d!lib refresh`
        # landing between them (it replaces the module global) would
        # give the view a different - possibly empty - index than the
        # one that passed this check
        index = library.get_index()
        if not index:
            await ctx.send(config.LIBRARY_EMPTY)
            return

        view, frame = LibraryBrowseView.open(ctx, index)
        # whatever this hands back, OwnedView.refresh_message() knows
        # what to do with it - see the note on OwnedView.message
        view.message = await _send_private(
            ctx, embed=view.embed(frame), view=view
        )
        view.session.landed(frame)


async def setup(bot: MusicBot):
    await bot.add_cog(Library(bot))
