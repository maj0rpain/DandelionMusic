from typing import List, Optional

import discord
from discord.ext import commands

from config import config
from musicbot import library
from musicbot.bot import MusicBot
from musicbot.loader import SongError
from musicbot.utils import CheckError, dj_check, play_check

PAGE_SIZE = 25


class LibrarySelect(discord.ui.Select):
    def __init__(self, entries: List[str], browse_view: "LibraryBrowseView"):
        self._entries = entries
        self.browse_view = browse_view
        super().__init__(
            placeholder="Choose...",
            options=[
                discord.SelectOption(label=entry[:100], value=str(i))
                for i, entry in enumerate(entries)
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

    def entries(self) -> List[str]:
        if self.artist is None:
            return sorted(self.index.keys())
        if self.album is None:
            return sorted(self.index.get(self.artist, {}).keys())
        return sorted(self.index.get(self.artist, {}).get(self.album, []))

    def title(self) -> str:
        if self.artist is None:
            return "Music Library — Artists"
        if self.album is None:
            return f"Music Library — {self.artist} — Albums"
        return f"Music Library — {self.artist} / {self.album} — Songs"

    def embed(self) -> discord.Embed:
        embed = discord.Embed(title=self.title(), color=config.EMBED_COLOR)
        if not self.entries():
            embed.description = config.LIBRARY_EMPTY
        return embed

    def build_items(self):
        self.clear_items()
        entries = self.entries()
        page_entries = entries[
            self.page * PAGE_SIZE : (self.page + 1) * PAGE_SIZE
        ]
        if page_entries:
            self.add_item(LibrarySelect(page_entries, self))
        if self.artist is not None:
            self.add_item(QueueLevelButton(self))
            self.add_item(BackButton(self))
        if self.page > 0:
            self.add_item(PageButton(self, -1, "◀ Prev"))
        if (self.page + 1) * PAGE_SIZE < len(entries):
            self.add_item(PageButton(self, 1, "Next ▶"))

    async def render(self, interaction: discord.Interaction):
        self.build_items()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def descend(self, interaction: discord.Interaction, chosen: str):
        self.page = 0
        if self.artist is None:
            self.artist = chosen
            await self.render(interaction)
        elif self.album is None:
            self.album = chosen
            await self.render(interaction)
        else:
            await self.queue_pairs(interaction, [(self.album, chosen)])

    async def go_back(self, interaction: discord.Interaction):
        self.page = 0
        if self.album is not None:
            self.album = None
        else:
            self.artist = None
        await self.render(interaction)

    async def queue_current_level(self, interaction: discord.Interaction):
        if self.album is not None:
            pairs = [
                (self.album, song)
                for song in self.index.get(self.artist, {}).get(self.album, [])
            ]
        else:
            pairs = [
                (album, song)
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
