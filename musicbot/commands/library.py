import io
import sys
from pathlib import Path
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import config
from musicbot import library, library_metadata
from musicbot.bot import MusicBot
from musicbot.loader import SongError
from musicbot.utils import CheckError, owner_check, play_check

PAGE_SIZE = 25

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
        chosen = self._entries[int(self.values[0])]
        await self.browse_view.descend(interaction, chosen)


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
        self.browse_view.page += self.delta
        await self.browse_view.render(interaction)


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


async def queue_songs(ctx, interaction, triples) -> None:
    """Queues (artist, album, filename) triples and reports the
    result ephemerally. Shared by the browser and the search results -
    the browser always works within one artist, but a search hit list
    spans several, so the artist travels with each song rather than
    being read off the view."""
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    try:
        await play_check(ctx)
    except CheckError as e:
        await interaction.followup.send(str(e), ephemeral=True)
        return

    queued = 0
    missing = []
    for artist, album, filename in triples:
        uri = library.song_uri(artist, album, filename)
        try:
            await ctx.audiocontroller.process_song(
                uri, user=ctx.author, pickle=False
            )
            queued += 1
        except SongError:
            missing.append(filename)

    if queued:
        ctx.audiocontroller.pickle_playlist()

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

    await interaction.followup.send(message, ephemeral=True)


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
        # guards against a second click landing while a deferred
        # handler is still resolving - deferring a component
        # interaction clears its click spinner and re-enables the view
        # immediately, it does not show a "thinking" placeholder, so
        # without this two overlapping resolves could land out of order
        self._busy: bool = False
        # whatever Context.send handed back, which is not always a
        # Message: the prefix paths and the deferred search path give
        # a Message/WebhookMessage, but answering an interaction
        # directly returns an InteractionCallbackResponse (discord.py
        # >= 2.5) that has no edit(). on_timeout() below discriminates
        # on the type rather than on which path ran.
        self.message = None

    async def interaction_check(
        self, interaction: discord.Interaction
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This belongs to someone else.", ephemeral=True
            )
            return False
        if self._busy:
            await interaction.response.send_message(
                "Still loading, please wait…", ephemeral=True
            )
            return False
        return True

    async def queue(self, interaction: discord.Interaction, triples) -> None:
        """Queues through the _busy guard. queue_songs() defers, and a
        deferred *component* interaction re-enables the select at
        once, so without this a second click during a long
        process_song() loop would queue the same album or discography
        twice over - a whole-artist result makes that loop long enough
        to hit easily."""
        self._busy = True
        try:
            await queue_songs(self.ctx, interaction, triples)
        finally:
            self._busy = False

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
        self.build_items()

    def _songs(self) -> List[library.LibrarySong]:
        return self.index.get(self.artist, {}).get(self.album, [])

    def _first_sample_file(self) -> Optional[Path]:
        albums = self.index.get(self.artist, {})
        if not albums:
            return None
        first_album = sorted(albums.keys())[0]
        songs = albums[first_album]
        if not songs:
            return None
        return library.song_path(self.artist, first_album, songs[0].filename)

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

    def entries(self) -> List[str]:
        """Selection values - filenames at the song level, folder
        names above it. Always use these (not labels()) for anything
        that needs to look the entry back up in the index."""
        if self.artist is None:
            return sorted(self.index.keys())
        if self.album is None:
            return sorted(self.index.get(self.artist, {}).keys())
        # already sorted by filename in build_index(), preserving
        # track-number order - don't re-sort
        return [song.filename for song in self._songs()]

    def labels(self) -> List[str]:
        """Display text, parallel to entries() - tag-derived titles
        at the song level, folder names above it."""
        if self.artist is not None and self.album is not None:
            return [song.title for song in self._songs()]
        return self.entries()

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
        if self.artist is None:
            artists, albums, songs = library.counts(self.index)
            self._field(embed, "Artists", f"{artists:,}")
            self._field(embed, "Albums", f"{albums:,}")
            self._field(embed, "Songs", f"{songs:,}")
            return

        if self.album is None:
            local = library.artist_stats(self.index, self.artist)
            self._field(embed, "Albums", local.albums)
            self._field(embed, "Tracks", local.tracks)
            self._field(embed, "Runtime", _fmt_duration(local.runtime))
            self._field(embed, "Years", _fmt_years(local))
            self._field(embed, "Formats", ", ".join(local.formats))
            if stats:
                self._field(embed, "Listeners", _fmt_count(stats.listeners))
        else:
            local = library.album_stats(self.index, self.artist, self.album)
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

    def build_items(self):
        self.clear_items()
        entries = self.entries()
        labels = self.labels()
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
        use_followup: bool = False,
        set_attachments: bool = False,
    ):
        self.build_items()
        kwargs = {"embed": self.embed(), "view": self}
        if set_attachments:
            kwargs["attachments"] = self._attachments()
        if use_followup:
            await interaction.edit_original_response(**kwargs)
        else:
            await interaction.response.edit_message(**kwargs)

    async def descend(self, interaction: discord.Interaction, chosen: str):
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
            await self.queue(interaction, [(self.artist, self.album, chosen)])

    async def go_back(self, interaction: discord.Interaction):
        self.page = 0
        if self.album is not None:
            self.album = None
        else:
            self.artist = None
        await self._enter_level(interaction)

    async def _enter_level(self, interaction: discord.Interaction):
        self._busy = True
        try:
            await interaction.response.defer()
            try:
                self._enrichment = await self._resolve_enrichment()
            except Exception as e:
                print(f"library: enrichment failed: {e}", file=sys.stderr)
                self._enrichment = None
            await self.render(
                interaction, use_followup=True, set_attachments=True
            )
        finally:
            self._busy = False

    async def queue_current_level(self, interaction: discord.Interaction):
        if self.album is not None:
            triples = [
                (self.artist, self.album, song.filename)
                for song in self._songs()
            ]
        else:
            triples = [
                (self.artist, album, song.filename)
                for album, songs in self.index.get(self.artist, {}).items()
                for song in songs
            ]
        await self.queue(interaction, triples)


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
        await self.queue(interaction, self._expand(result))


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

        # ephemeral here as well as on the send below: the deferred
        # placeholder's visibility is fixed when it's created, so a
        # public defer would leave a stray public message behind the
        # ephemeral result
        await ctx.defer(ephemeral=True)
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
        # For the text-command path this is a real discord.Message,
        # used by on_timeout() to disable the view later. For the
        # slash path Context.send() routes through
        # interaction.response.send_message(), which returns None -
        # on_timeout() uses ctx.interaction.edit_original_response()
        # instead in that case, so this being None there is expected.
        view.message = await ctx.send(**kwargs)


async def setup(bot: MusicBot):
    await bot.add_cog(Library(bot))
