from typing import List, Optional

import discord
from discord.ext import commands

from config import config
from musicbot import library
from musicbot.bot import MusicBot
from musicbot.utils import dj_check

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
            # Task 7 replaces this with queueing the chosen song.
            await interaction.response.send_message(
                f"(queueing {chosen!r} - implemented in Task 7)",
                ephemeral=True,
            )

    async def go_back(self, interaction: discord.Interaction):
        self.page = 0
        if self.album is not None:
            self.album = None
        else:
            self.artist = None
        await self.render(interaction)


class Library(commands.Cog):
    def __init__(self, bot: MusicBot):
        self.bot = bot

    async def cog_check(self, ctx):
        ctx.audiocontroller = ctx.bot.audio_controllers[ctx.guild]
        return True

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


async def setup(bot: MusicBot):
    await bot.add_cog(Library(bot))
