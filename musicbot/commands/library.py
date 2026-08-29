import io
import sys
from pathlib import Path
from typing import List, Optional

import discord
from discord.ext import commands

from config import config
from musicbot import library, library_metadata
from musicbot.bot import MusicBot
from musicbot.loader import SongError
from musicbot.utils import CheckError, dj_check, play_check

PAGE_SIZE = 25

# small grey line above the title, so the current scope gets the
# title to itself at every level
AUTHOR_LINE = "Music Library"


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
        browse_view: "LibraryBrowseView",
    ):
        self._entries = entries
        self.browse_view = browse_view
        super().__init__(
            placeholder="Choose...",
            options=[
                discord.SelectOption(label=label[:100], value=str(i))
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


class LibraryBrowseView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.index = library.get_index()
        self.artist: Optional[str] = None
        self.album: Optional[str] = None
        self.page = 0
        self._enrichment: Optional[library_metadata.Enrichment] = None
        # guards against a second click landing while a deferred
        # descend()/go_back() is still resolving enrichment - deferring
        # a component interaction clears its click spinner and
        # re-enables the view immediately, it does not show a
        # "thinking" placeholder, so without this two overlapping
        # resolves could land out of order
        self._busy: bool = False
        # set by the caller after sending (Task 8); needed so
        # on_timeout() can disable the stale buttons/select. Left None
        # for the slash-command path, where ctx.interaction is used
        # instead (see on_timeout below).
        self.message: Optional[discord.Message] = None
        self.build_items()

    async def interaction_check(
        self, interaction: discord.Interaction
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This browser belongs to someone else.", ephemeral=True
            )
            return False
        if self._busy:
            await interaction.response.send_message(
                "Still loading, please wait…", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        # Without this, a click after the 5-minute timeout just fails
        # silently client-side (discord.py stops dispatching to a
        # timed-out view's items) and the message's Select/Buttons
        # stay visibly enabled forever.
        for item in self.children:
            item.disabled = True
        try:
            if self.ctx.interaction is not None:
                await self.ctx.interaction.edit_original_response(view=self)
            elif self.message is not None:
                await self.message.edit(view=self)
        except discord.HTTPException:
            pass

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

    def title(self) -> str:
        """Only the current scope - the path to it lives in the
        footer, and "Music Library" in the author line."""
        if self.artist is None:
            return "Artists"
        if self.album is None:
            return self.artist
        return self.album

    def footer(self) -> Optional[str]:
        if self.artist is None:
            return None
        if self.album is None:
            return f"Artists \u203a {self.artist}"
        return f"{self.artist} \u203a {self.album}"

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
            self.add_item(LibrarySelect(page_entries, page_labels, self))
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
        self.page = 0
        if self.artist is None:
            self.artist = chosen
            await self._enter_level(interaction)
        elif self.album is None:
            self.album = chosen
            await self._enter_level(interaction)
        else:
            await self.queue_pairs(interaction, [(self.album, chosen)])

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
            pairs = [(self.album, song.filename) for song in self._songs()]
        else:
            pairs = [
                (album, song.filename)
                for album, songs in self.index.get(self.artist, {}).items()
                for song in songs
            ]
        await self.queue_pairs(interaction, pairs)

    async def queue_pairs(self, interaction, pairs):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        try:
            await play_check(self.ctx)
        except CheckError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        queued = 0
        missing = []
        for album, filename in pairs:
            uri = library.song_uri(self.artist, album, filename)
            try:
                await self.ctx.audiocontroller.process_song(
                    uri, user=self.ctx.author, pickle=False
                )
                queued += 1
            except SongError:
                missing.append(filename)

        if queued:
            self.ctx.audiocontroller.pickle_playlist()

        message = f"Queued {queued} song(s)."
        if missing:
            shown = ", ".join(missing[:5])
            message += f" {len(missing)} skipped (file not found): {shown}"
            if len(missing) > 5:
                message += f", and {len(missing) - 5} more"
            message += ". Try `d!library refresh`."

        await interaction.followup.send(message, ephemeral=True)


class Library(commands.Cog):
    def __init__(self, bot: MusicBot):
        self.bot = bot

    async def cog_check(self, ctx):
        ctx.audiocontroller = ctx.bot.audio_controllers[ctx.guild]
        return True

    async def cog_before_invoke(self, ctx):
        ctx.audiocontroller.command_channel = ctx

    @commands.hybrid_group(
        name="library",
        description=config.HELP_LIBRARY_SHORT,
        help=config.HELP_LIBRARY_LONG,
        invoke_without_command=True,
    )
    async def _library(self, ctx):
        await ctx.send("Use subcommands: `refresh`, `browse`.")

    @_library.command(
        name="refresh",
        description=config.HELP_LIBRARY_REFRESH_SHORT,
        help=config.HELP_LIBRARY_REFRESH_LONG,
    )
    @commands.check(dj_check)
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
