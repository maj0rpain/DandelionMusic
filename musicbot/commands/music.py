from __future__ import annotations
from typing import Iterable, List, Union, Literal

import discord
from discord import Attachment, Embed, app_commands
from discord.ui import View
from discord.ext import commands

from config import config
from musicbot import linkutils, utils, loader, playlists
from musicbot.song import Song
from musicbot.bot import MusicBot, Context
from musicbot.utils import dj_check, chunks, SimplePaginator
from musicbot.audiocontroller import (
    PLAYLIST,
    AudioController,
    MusicButton,
    RestoreResult,
)
from musicbot.loader import SongError, search_youtube
from musicbot.playlist import PlaylistError
from musicbot.playlists import PlaylistEntry
from musicbot.linkutils import get_site_type, url_regex


class AudioContext(Context):
    audiocontroller: AudioController


class SearchView(View):
    """The d!search results: one pick, by the user who ran the search.

    discord.py's View has no `message` attribute (py-cord's had one),
    so it is kept here, set by the search command to whatever its
    reply send returned."""

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.message = None
        # the single-pick claim. discord.py dispatches every click in
        # its own task, and a button's play check (which can join
        # voice) runs before the pick stops the view, so stop() alone
        # would let two quick clicks both queue. Taken in
        # interaction_check, before the first await of the click.
        self.picked = False

    async def interaction_check(
        self, interaction: discord.Interaction
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This belongs to someone else.", ephemeral=True
            )
            return False
        if self.picked:
            # the second half of a double-click: acknowledged silently
            await interaction.response.defer()
            return False
        self.picked = True
        return True

    def disable(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def show(self):
        """Edits the message to the view's current state. A failed
        edit is swallowed: it must never cost the pick."""
        try:
            if self.message is not None and not isinstance(
                self.message, discord.InteractionCallbackResponse
            ):
                await self.message.edit(view=self)
            elif self.ctx.interaction is not None:
                await self.ctx.interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        self.disable()
        await self.show()


class SongButton(MusicButton):
    def __init__(self, cog: "Music", num: int, song: str):
        async def check(ctx):
            try:
                return await cog.cog_check(ctx)
            except utils.CheckError:
                # the pick was refused (e.g. not in voice): release the
                # claim so the owner can fix it and pick again
                self.view.picked = False
                raise

        async def play(ctx):
            view = self.view
            view.stop()
            view.disable()
            async with ctx.channel.typing():
                await view.show()
                await cog._play_song(ctx, song)

        super().__init__(play, check, emoji=f"{num}⃣")

    async def callback(self, inter: discord.Interaction):
        try:
            await super().callback(inter)
        except Exception:
            # anything but a refused check (a failed voice join, a
            # failed defer) uses up the pick: the stale message must
            # not invite clicks that are silently refused
            view = self.view
            if not view.is_finished():
                view.stop()
                view.disable()
                await view.show()
            raise


@commands.check
def active_only(ctx):
    if not ctx.audiocontroller.is_active():
        raise utils.CheckError(config.QUEUE_EMPTY)
    return True


def _playlist_ref(ctx, name: str) -> playlists.PlaylistRef:
    return playlists.PlaylistRef(str(ctx.guild.id), name)


class Music(commands.Cog):
    """A collection of the commands related to music playback.

    Attributes:
        bot: The instance of the bot that is executing the commands.
    """

    def __init__(self, bot: MusicBot):
        self.bot = bot

    async def cog_check(self, ctx):
        ctx.audiocontroller = utils.get_audiocontroller(ctx)
        return await utils.play_check(ctx)

    async def cog_before_invoke(self, ctx):
        ctx.audiocontroller.command_channel = ctx

    @commands.hybrid_command(
        name="play",
        description=config.HELP_YT_LONG,
        help=config.HELP_YT_SHORT,
        aliases=["p", "yt"],
    )
    async def _play(self, ctx, *, track: str = None, file: Attachment = None):
        if track is None:
            if ctx.message:
                if ctx.message.attachments:
                    track = ctx.message.jump_url
                elif (
                    ctx.message.reference
                    and ctx.message.reference.resolved
                    and ctx.message.reference.resolved.attachments
                ):
                    track = ctx.message.reference.resolved.jump_url
            elif file:
                track = file.url
        if track is None:
            await ctx.send(config.PLAY_ARGS_MISSING)
            return

        await ctx.defer()
        await self._play_song(ctx, track)

    async def _play_song(
        self, ctx, track: Union[str, Iterable[str]], playnext=False
    ):
        # reset timer
        await ctx.audiocontroller.timer.start(True)

        try:
            song = await ctx.audiocontroller.process_song(
                track, user=ctx.author
            )
        except SongError as e:
            await ctx.send(e)
            return
        if song is None:
            await ctx.send(config.SONGINFO_UNSUPPORTED)
            return

        if song is PLAYLIST:
            await ctx.send(config.SONGINFO_PLAYLIST_QUEUED)
        else:
            if len(ctx.audiocontroller.playlist) != 1:
                await ctx.send(
                    embed=song.format_output(config.SONGINFO_QUEUE_ADDED)
                )
            elif not utils.get_settings(ctx).announce_songs:
                # auto-announce is disabled, announce here
                await ctx.send(
                    embed=song.format_output(config.SONGINFO_NOW_PLAYING)
                )
            if playnext:
                if len(ctx.audiocontroller.playlist) > 2:
                    src_pos = len(ctx.audiocontroller.playlist)
                    dest_pos = 2
                    try:
                        ctx.audiocontroller.move(src_pos - 1, dest_pos - 1)
                    except PlaylistError as e:
                        await ctx.send(e)

    @commands.hybrid_command(
        name="playnext",
        description=config.HELP_YT_LONG,
        help=config.HELP_YT_SHORT,
        aliases=["pn"],
    )
    async def _play_next(
        self, ctx, *, track: str = None, file: Attachment = None
    ):
        if track is None and ctx.message:
            if ctx.message.attachments:
                track = ctx.message.jump_url
            elif (
                ctx.message.reference
                and ctx.message.reference.resolved
                and ctx.message.reference.resolved.attachments
            ):
                track = ctx.message.reference.resolved.jump_url
        if track is None:
            await ctx.send(config.PLAY_ARGS_MISSING)
            return

        await ctx.defer()
        await self._play_song(ctx, track, playnext=True)

    @commands.hybrid_command(
        name="search",
        description=config.HELP_SEARCH_LONG,
        help=config.HELP_SEARCH_SHORT,
        aliases=["sc"],
    )
    async def _search(self, ctx, *, query: str):
        await ctx.defer()
        results = await search_youtube(query, config.SEARCH_RESULTS)
        # search_youtube returns None when extraction fails outright
        # (yt-dlp error, rate limit, blocked request) - without this
        # the loop below raised TypeError and on_command_error echoed
        # it into the channel
        if not results:
            await ctx.send(config.SEARCH_NO_RESULTS)
            return
        songs = []
        for data in results:
            song = Song(
                linkutils.SiteTypes.YT_DLP,
                webpage_url=data["url"],
            )
            song.update(data)
            songs.append(song)

        view = SearchView(ctx)
        for i, data in enumerate(results, start=1):
            view.add_item(SongButton(self, i, data["url"]))

        view.message = await ctx.send(
            embed=utils.songs_embed(config.SEARCH_EMBED_TITLE, songs),
            view=view,
        )

    @commands.hybrid_command(
        name="loop",
        description=config.HELP_LOOP_LONG,
        help=config.HELP_LOOP_SHORT,
        aliases=["l"],
    )
    @active_only
    @commands.check(dj_check)
    async def _loop(
        self,
        ctx,
        mode: Literal["off", "single", "all"] = None,
    ):
        result = ctx.audiocontroller.loop(mode)
        await ctx.send(result.value)

    @commands.hybrid_command(
        name="shuffle",
        description=config.HELP_SHUFFLE_LONG,
        help=config.HELP_SHUFFLE_SHORT,
        aliases=["sh"],
    )
    @active_only
    @commands.check(dj_check)
    async def _shuffle(self, ctx):
        ctx.audiocontroller.shuffle()
        await ctx.send("Shuffled queue :twisted_rightwards_arrows:")

    @commands.hybrid_command(
        name="pause",
        description=config.HELP_PAUSE_LONG,
        help=config.HELP_PAUSE_SHORT,
        aliases=["resume"],
    )
    @commands.check(dj_check)
    async def _pause(self, ctx):
        result = ctx.audiocontroller.pause()
        await ctx.send(result.value)

    @commands.hybrid_command(
        name="queue",
        description=config.HELP_QUEUE_LONG,
        help=config.HELP_QUEUE_SHORT,
        aliases=["q"],
    )
    @active_only
    async def _queue(self, ctx):
        playlist = ctx.audiocontroller.playlist
        await ctx.send(embed=playlist.queue_embed())

    @commands.hybrid_command(
        name="stop",
        description=config.HELP_STOP_LONG,
        help=config.HELP_STOP_SHORT,
        aliases=["st"],
    )
    async def _stop(self, ctx):
        ctx.audiocontroller.stop()
        await ctx.send("Stopped all sessions :octagonal_sign:")

    @commands.hybrid_command(
        name="move",
        description=config.HELP_MOVE_LONG,
        help=config.HELP_MOVE_SHORT,
        aliases=["mv"],
    )
    @active_only
    @commands.check(dj_check)
    async def _move(
        self,
        ctx,
        src_pos: int = None,
        dest_pos: int = None,
    ):
        if src_pos is None:
            src_pos = len(ctx.audiocontroller.playlist)
        if dest_pos is None:
            dest_pos = 2

        try:
            ctx.audiocontroller.move(src_pos - 1, dest_pos - 1)
            await ctx.send("Moved ↔️")
        except PlaylistError as e:
            await ctx.send(e)

    @commands.hybrid_command(
        name="remove",
        description=config.HELP_REMOVE_LONG,
        help=config.HELP_REMOVE_SHORT,
        aliases=["rm"],
    )
    @active_only
    @commands.check(dj_check)
    async def _remove(
        self,
        ctx,
        queue_number: int = None,
    ):
        if queue_number is None:
            queue_number = len(ctx.audiocontroller.playlist)
        try:
            song = ctx.audiocontroller.remove(queue_number - 1)
            title = song.title or song.webpage_url
            await ctx.send(f"Removed #{queue_number}: {title}")
        except PlaylistError as e:
            await ctx.send(e)

    @commands.hybrid_command(
        name="skip",
        description=config.HELP_SKIP_LONG,
        help=config.HELP_SKIP_SHORT,
        aliases=["s", "next"],
    )
    @active_only
    @commands.check(dj_check)
    async def _skip(self, ctx):
        ctx.audiocontroller.skip()
        await ctx.send("Skipped current song :fast_forward:")

    @commands.hybrid_command(
        name="restore",
        description=config.HELP_RESTORE,
        help=config.HELP_RESTORE,
    )
    @commands.check(dj_check)
    async def _restore(self, ctx):
        result = await ctx.audiocontroller.restore()
        if result is RestoreResult.REFUSED_WHILE_ACTIVE:
            await ctx.send(config.RESTORE_WHILE_ACTIVE)
        elif result is RestoreResult.NOTHING_TO_RESTORE:
            await ctx.send(config.QUEUE_EMPTY)
        else:
            await ctx.send("Restored playlist")

    @commands.hybrid_command(
        name="clear",
        description=config.HELP_CLEAR_LONG,
        help=config.HELP_CLEAR_SHORT,
        aliases=["cl"],
    )
    @commands.check(dj_check)
    async def _clear(self, ctx):
        ctx.audiocontroller.clear()
        await ctx.send("Cleared queue :no_entry_sign:")

    @commands.hybrid_command(
        name="prev",
        description=config.HELP_PREV_LONG,
        help=config.HELP_PREV_SHORT,
        aliases=["back"],
    )
    @commands.check(dj_check)
    async def _prev(self, ctx):
        if ctx.audiocontroller.prev_song():
            await ctx.send("Playing previous song :track_previous:")
        else:
            await ctx.send("No previous track.")

    @commands.hybrid_command(
        name="songinfo",
        description=config.HELP_SONGINFO_LONG,
        help=config.HELP_SONGINFO_SHORT,
        aliases=["np"],
    )
    @active_only
    async def _songinfo(self, ctx):
        song = ctx.audiocontroller.current_song
        await ctx.send(embed=song.format_output(config.SONGINFO_SONGINFO))

    @commands.hybrid_command(
        name="history",
        description=config.HELP_HISTORY_LONG,
        help=config.HELP_HISTORY_SHORT,
    )
    async def _history(self, ctx):
        await ctx.send(ctx.audiocontroller.track_history())

    @commands.hybrid_command(
        name="volume",
        aliases=["vol"],
        description=config.HELP_VOL_LONG,
        help=config.HELP_VOL_SHORT,
    )
    @commands.check(dj_check)
    async def _volume(
        self,
        ctx,
        value: int = None,
    ):
        if value is None:
            await ctx.send(
                "Current volume: {}% :speaker:".format(
                    ctx.audiocontroller.volume
                )
            )
            return

        if value > 100 or value < 0:
            await ctx.send("Error: Volume must be a number 1-100")
            return

        if ctx.audiocontroller.volume >= value:
            await ctx.send("Volume set to {}% :sound:".format(str(value)))
        else:
            await ctx.send("Volume set to {}% :loud_sound:".format(str(value)))
        ctx.audiocontroller.set_volume(value)

    async def _playlist_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        choices = await playlists.list_names(
            self.bot.DbSession, str(interaction.guild.id), prefix=current
        )
        return [
            app_commands.Choice(name=name, value=name) for name in choices
        ][:25]

    @commands.hybrid_group(
        name="playlist",
        aliases=["pl"],
        invoke_without_command=True,
    )
    async def _playlist(self, ctx):
        await ctx.send("Use subcommands to manage playlists.")

    @_playlist.command(
        name="save",
        aliases=["s"],
        description=config.HELP_SAVE_PLAYLIST_LONG,
        help=config.HELP_SAVE_PLAYLIST_SHORT,
    )
    @commands.check(dj_check)
    async def _playlist_save(self, ctx, name: str):
        if not config.ENABLE_PLAYLISTS:
            await ctx.send(config.PLAYLISTS_ARE_DISABLED)
            return

        await ctx.defer()
        songs = [
            PlaylistEntry(song.webpage_url, song.title)
            for song in ctx.audiocontroller.playlist.playque
        ]
        if not songs:
            await ctx.send(config.QUEUE_EMPTY)
            return
        try:
            await playlists.save(
                ctx.bot.DbSession, _playlist_ref(ctx, name), songs
            )
        except playlists.PlaylistExists:
            await ctx.send(config.PLAYLIST_ALREADY_EXISTS)
            return
        await ctx.send(config.PLAYLIST_SAVED_MESSAGE)

    @_playlist.command(
        name="load",
        aliases=["l"],
        description=config.HELP_LOAD_PLAYLIST_LONG,
        help=config.HELP_LOAD_PLAYLIST_SHORT,
    )
    @app_commands.autocomplete(name=_playlist_autocomplete)
    async def _playlist_load(
        self,
        ctx,
        name: str,
    ):
        await ctx.defer()
        contents = await playlists.get(
            ctx.bot.DbSession, _playlist_ref(ctx, name)
        )
        if contents is None:
            await ctx.send(config.PLAYLIST_NOT_FOUND)
            return
        await ctx.audiocontroller.queue(
            Song(
                get_site_type(entry.url),
                entry.url,
                title=entry.title,
                saved_playlist=contents.ref,
            )
            for entry in contents.entries
        )
        await ctx.send(config.SONGINFO_PLAYLIST_QUEUED)

    @_playlist.command(
        name="remove",
        aliases=["r"],
        description=config.HELP_REMOVE_PLAYLIST_LONG,
        help=config.HELP_REMOVE_PLAYLIST_SHORT,
    )
    @commands.check(dj_check)
    @app_commands.autocomplete(name=_playlist_autocomplete)
    async def _playlist_remove(
        self,
        ctx,
        name: str,
    ):
        await ctx.defer()
        try:
            await playlists.delete(
                ctx.bot.DbSession,
                _playlist_ref(ctx, name),
            )
        except playlists.PlaylistNotFound:
            await ctx.send(config.PLAYLIST_NOT_FOUND)
            return
        await ctx.send(config.PLAYLIST_REMOVED)

    @_playlist.command(
        name="list",
        aliases=["li"],
        description=config.HELP_LIST_PLAYLISTS_LONG,
        help=config.HELP_LIST_PLAYLISTS_SHORT,
    )
    async def _playlist_list(self, ctx):
        names = await playlists.list_names(
            ctx.bot.DbSession, str(ctx.guild.id)
        )
        if not names:
            await ctx.send("No playlists.")
            return

        playlist_names = "\n".join(f"- {name}" for name in names)
        await ctx.send(f"**Playlists:**\n{playlist_names}")

    @_playlist.command(
        name="show",
        aliases=["sw"],
        description=config.HELP_PLAYLIST_SHOW_LONG,
        help=config.HELP_PLAYLIST_SHOW_SHORT,
    )
    @commands.check(dj_check)
    @app_commands.autocomplete(playlist=_playlist_autocomplete)
    async def _playlist_show(
        self,
        ctx,
        playlist: str,
    ):
        await ctx.defer()

        contents = await playlists.get(
            ctx.bot.DbSession,
            _playlist_ref(ctx, playlist),
        )
        if contents is None:
            await ctx.send(config.PLAYLIST_NOT_FOUND)
            return
        pages = []
        i = 1
        for part in chunks(contents.entries, 25):
            embed = Embed(title=contents.ref.name)
            for song in part:
                url = song.url
                title = song.title or url_regex.fullmatch(url).group("bare")
                embed.add_field(
                    name=str(i), value=f"[{title}]({url})", inline=False
                )
                i += 1
            pages.append(embed)
        await SimplePaginator(pages).send(ctx)

    @_playlist.command(
        name="add_song",
        aliases=["as"],
        description=config.HELP_ADD_TO_PLAYLIST_LONG,
        help=config.HELP_ADD_TO_PLAYLIST_SHORT,
    )
    @commands.check(dj_check)
    @app_commands.autocomplete(playlist=_playlist_autocomplete)
    async def _playlist_add_song(
        self,
        ctx,
        playlist: str,
        track: str,
    ):
        await ctx.defer()
        song = await loader.load_song(track)
        if song is None:
            await ctx.send(config.SONGINFO_ERROR)
            return
        songs = [song] if isinstance(song, Song) else song
        try:
            await playlists.add_songs(
                ctx.bot.DbSession,
                _playlist_ref(ctx, playlist),
                [PlaylistEntry(s.webpage_url, s.title) for s in songs],
            )
        except playlists.PlaylistNotFound:
            await ctx.send(config.PLAYLIST_NOT_FOUND)
            return
        await ctx.send(config.PLAYLIST_UPDATED)

    @_playlist.command(
        name="remove_song",
        aliases=["rs"],
        description=config.HELP_REMOVE_FROM_PLAYLIST_LONG,
        help=config.HELP_REMOVE_FROM_PLAYLIST_SHORT,
    )
    @commands.check(dj_check)
    @app_commands.autocomplete(playlist=_playlist_autocomplete)
    async def _playlist_remove_song(
        self,
        ctx,
        playlist: str,
        position: int,
    ):
        await ctx.defer()

        try:
            await playlists.remove_song(
                ctx.bot.DbSession,
                _playlist_ref(ctx, playlist),
                position,
            )
        except playlists.PlaylistNotFound:
            await ctx.send(config.PLAYLIST_NOT_FOUND)
            return
        except playlists.InvalidPosition as e:
            await ctx.send(f"Invalid position. Playlist has {e.size} songs.")
            return
        except playlists.OnlySongInPlaylist:
            await ctx.send("Can't remove the only song from playlist.")
            return
        await ctx.send(config.PLAYLIST_UPDATED)

    @_playlist.command(
        name="move_song",
        aliases=["ms"],
        description=config.HELP_MOVE_IN_PLAYLIST_LONG,
        help=config.HELP_MOVE_IN_PLAYLIST_SHORT,
    )
    @commands.check(dj_check)
    @app_commands.autocomplete(playlist=_playlist_autocomplete)
    async def _playlist_move_song(
        self,
        ctx,
        playlist: str,
        source_position: int,
        destination_position: int,
    ):
        await ctx.defer()

        try:
            await playlists.move_song(
                ctx.bot.DbSession,
                _playlist_ref(ctx, playlist),
                source_position,
                destination_position,
            )
        except playlists.PlaylistNotFound:
            await ctx.send(config.PLAYLIST_NOT_FOUND)
            return
        except playlists.InvalidPosition as e:
            await ctx.send(f"Invalid position. Playlist has {e.size} songs.")
            return
        await ctx.send(config.PLAYLIST_UPDATED)


async def setup(bot: MusicBot):
    await bot.add_cog(Music(bot))
