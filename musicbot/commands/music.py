from __future__ import annotations
from typing import Iterable, List, Optional, Union, Literal

import discord
from discord import Attachment, Embed, app_commands
from discord.ext import commands

from config import config
from musicbot import admission, utils, loader, playlists
from musicbot.outcome import BatchReport, send_outcome
from musicbot.player_panel import send_song_info
from musicbot.song import Song
from musicbot.bot import MusicBot, Context
from musicbot.utils import dj_check, chunks, joining_command, SimplePaginator
from musicbot.audiocontroller import (
    AudioController,
    RestoreResult,
)
from musicbot.loader import SongError, search_youtube
from musicbot.playlist import PlaylistError
from musicbot.linkutils import get_site_type
from musicbot.message_link import check_message_link
from musicbot.views import MusicButton, OwnedView


def _attachment_track(ctx, file) -> Optional[str]:
    """The URL an attachment-based play passes on as its track, or None.

    A slash command's ctx.message is synthetic: discord.py fills its
    attachments from the command's options, and its jump URL points at
    no real message. So a slash `file` is played from its own URL
    before any message is looked at."""
    if ctx.interaction and file:
        return file.url
    message = ctx.message
    if message is None:
        return None
    if message.attachments:
        return message.jump_url
    reference = message.reference
    resolved = reference.resolved if reference else None
    # a deleted reference resolves to an object with no attachments
    if resolved is not None and getattr(resolved, "attachments", None):
        return resolved.jump_url
    return None


class AudioContext(Context):
    audiocontroller: AudioController


class SearchView(OwnedView):
    """The d!search results: one pick, by the user who ran the search."""

    def __init__(self, ctx):
        super().__init__(ctx)
        # the single-pick claim. discord.py dispatches every click in
        # its own task, and a button's checks and voice join run
        # before the pick stops the view, so stop() alone
        # would let two quick clicks both queue. Taken in
        # claim_click, before the first await of the click.
        self._picked = False

    def claim_click(self) -> bool:
        if self._picked:
            # the second half of a double-click: acknowledged silently
            return False
        self._picked = True
        return True

    def release_pick(self) -> None:
        """Gives the claim back, so the owner can pick again."""
        self._picked = False

    async def use_up_pick(self) -> None:
        """Finishes the view: stopped, disabled, its message
        refreshed. Does nothing once finished."""
        if self.is_finished():
            return
        self.stop()
        self.disable()
        await self.refresh_message()


class SongButton(MusicButton):
    def __init__(self, cog: "Music", num: int, song: str):
        async def play(ctx):
            async with ctx.channel.typing():
                await self.view.use_up_pick()
                await cog._play_song(ctx, song)

        super().__init__(
            play,
            admission.RequestKind.PICK,
            emoji=f"{num}⃣",
        )

    def on_refused(self):
        # the pick was refused (not in voice, a failed join): release
        # the claim so the owner can fix it and pick again
        self.view.release_pick()

    async def on_failure(self, ctx, error):
        # anything but a refusal (a failed defer, a failed action)
        # uses up the pick: the stale message must not invite clicks
        # that are silently refused
        await self.view.use_up_pick()
        # reported as d!play's failures are, which also logs it; not
        # re-raised, so View.on_error does not log it again
        await ctx.bot.on_command_error(ctx, error)


@commands.check
def active_only(ctx):
    if not ctx.audiocontroller.is_active():
        raise utils.CheckError(config.QUEUE_EMPTY)
    return True


def playlists_enabled(ctx):
    if not config.ENABLE_PLAYLISTS:
        raise utils.CheckError(config.PLAYLISTS_ARE_DISABLED)
    return True


def _saved_playlist_error_reply(e: playlists.SavedPlaylistError) -> str:
    """The reply text for a saved-playlist failure."""
    if isinstance(e, playlists.PlaylistNotFound):
        return config.PLAYLIST_NOT_FOUND
    if isinstance(e, playlists.CorruptPlaylist):
        return config.PLAYLIST_CORRUPT
    if isinstance(e, playlists.InvalidPosition):
        return f"Invalid position. Playlist has {e.size} songs."
    if isinstance(e, playlists.PlaylistExists):
        return config.PLAYLIST_ALREADY_EXISTS
    if isinstance(e, playlists.OnlySongInPlaylist):
        return "Can't remove the only song from playlist."
    raise e


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
        # the playlist gate refuses before any voice or channel refusal
        # a button's context (Context.from_interaction) has no command
        cmd = ctx.command
        if cmd is not None and (cmd.root_parent or cmd) is self._playlist:
            playlists_enabled(ctx)
        ctx.admitted = await admission.check(ctx)
        ctx.audiocontroller = ctx.admitted.audiocontroller
        return True

    async def cog_before_invoke(self, ctx):
        # The hook never joins voice: a joining command joins in its
        # own body (_enter), once its checks on its input have passed.
        ctx.bot.sessions.direct_panel(ctx.guild, ctx.channel)

    async def _enter(self, ctx):
        """Joins voice for a joining command, after its own input
        checks. A slow connect must not push a slash command past
        Discord's 3-second deadline, so it is deferred first -
        publicly, as the reply that follows is. Acts on the guild's
        current controller, not the one cog_check saw."""
        if ctx.admitted.join_needed:
            await ctx.defer()
        ctx.audiocontroller = await ctx.admitted.enter()

    @commands.hybrid_command(
        name="play",
        description=config.HELP_YT_LONG,
        help=config.HELP_YT_SHORT,
        aliases=["p", "yt"],
    )
    @joining_command
    async def _play(self, ctx, *, track: str = None, file: Attachment = None):
        await self._play_request(ctx, track, file, playnext=False)

    async def _play_request(self, ctx, track, file, playnext):
        if track is None:
            track = _attachment_track(ctx, file)
        if track is None:
            await ctx.send(config.PLAY_ARGS_MISSING)
            return

        await self._enter(ctx)
        await ctx.defer()
        await self._play_song(ctx, track, playnext=playnext)

    async def _play_song(
        self, ctx, track: Union[str, Iterable[str]], playnext=False
    ):
        # reset timer
        await ctx.audiocontroller.start_idle_timer(True)

        try:
            outcome = await ctx.audiocontroller.process_song(
                track, user=ctx.author, playnext=playnext
            )
        except SongError as e:
            await ctx.send(e)
            return

        await send_outcome(ctx, outcome)

    @commands.hybrid_command(
        name="playnext",
        description=config.HELP_YT_LONG,
        help=config.HELP_YT_SHORT,
        aliases=["pn"],
    )
    @joining_command
    async def _play_next(
        self, ctx, *, track: str = None, file: Attachment = None
    ):
        await self._play_request(ctx, track, file, playnext=True)

    @commands.hybrid_command(
        name="search",
        description=config.HELP_SEARCH_LONG,
        help=config.HELP_SEARCH_SHORT,
        aliases=["sc"],
    )
    @joining_command
    async def _search(self, ctx, *, query: str):
        await ctx.defer()
        songs = await search_youtube(query, config.SEARCH_RESULTS)

        view = SearchView(ctx)
        for i, song in enumerate(songs, start=1):
            view.add_item(SongButton(self, i, song.webpage_url))

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
    @commands.check(dj_check)
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
            await ctx.send(f"Removed #{queue_number}: {song.display_name}")
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
    @joining_command
    async def _restore(self, ctx):
        await ctx.defer()  # restoring extracts the head of the backup
        result = ctx.audiocontroller.load_backup()
        if result is RestoreResult.READY:
            await self._enter(ctx)
            result = await ctx.audiocontroller.start_restored()
        if result is RestoreResult.REFUSED_WHILE_ACTIVE:
            await ctx.send(config.RESTORE_WHILE_ACTIVE)
        elif result is RestoreResult.BACKUP_UNREADABLE:
            await ctx.send(config.RESTORE_BACKUP_UNREADABLE)
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
    @joining_command
    async def _prev(self, ctx):
        if not ctx.audiocontroller.playlist.has_prev():
            await ctx.send(config.NO_PREVIOUS_TRACK)
            return
        await self._enter(ctx)
        if ctx.audiocontroller.prev_song():
            await ctx.send("Playing previous song :track_previous:")
        else:
            await ctx.send(config.NO_PREVIOUS_TRACK)

    @commands.hybrid_command(
        name="songinfo",
        description=config.HELP_SONGINFO_LONG,
        help=config.HELP_SONGINFO_SHORT,
        aliases=["np"],
    )
    @active_only
    async def _songinfo(self, ctx):
        await send_song_info(ctx, ctx.audiocontroller.current_song)

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
            await ctx.send("Error: Volume must be a number 0-100")
            return

        if ctx.audiocontroller.volume >= value:
            await ctx.send("Volume set to {}% :sound:".format(str(value)))
        else:
            await ctx.send("Volume set to {}% :loud_sound:".format(str(value)))
        ctx.audiocontroller.set_volume(value)

    async def _playlist_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        if not config.ENABLE_PLAYLISTS:
            return []
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
        await ctx.defer()
        songs = [
            playlists.PlaylistEntry(song.webpage_url, song.title)
            for song in ctx.audiocontroller.playlist.playque
        ]
        if not songs:
            await ctx.send(config.QUEUE_EMPTY)
            return
        try:
            await playlists.save(
                ctx.bot.DbSession, _playlist_ref(ctx, name), songs
            )
        except playlists.SavedPlaylistError as e:
            await ctx.send(_saved_playlist_error_reply(e))
            return
        await ctx.send(config.PLAYLIST_SAVED_MESSAGE)

    @_playlist.command(
        name="load",
        aliases=["l"],
        description=config.HELP_LOAD_PLAYLIST_LONG,
        help=config.HELP_LOAD_PLAYLIST_SHORT,
    )
    @app_commands.autocomplete(name=_playlist_autocomplete)
    @joining_command
    async def _playlist_load(
        self,
        ctx,
        name: str,
    ):
        await ctx.defer()
        try:
            contents = await playlists.get(
                ctx.bot.DbSession, _playlist_ref(ctx, name)
            )
        except playlists.SavedPlaylistError as e:
            await ctx.send(_saved_playlist_error_reply(e))
            return
        if contents is None:
            await ctx.send(config.PLAYLIST_NOT_FOUND)
            return
        await self._enter(ctx)
        outcome = await ctx.audiocontroller.enqueue(
            (
                Song(
                    get_site_type(entry.url),
                    entry.url,
                    title=entry.title,
                    saved_entry=playlists.SavedEntry(contents.ref, entry.url),
                )
                for entry in contents.entries
            ),
            batch=BatchReport(),
        )
        await send_outcome(ctx, outcome)

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
        except playlists.SavedPlaylistError as e:
            await ctx.send(_saved_playlist_error_reply(e))
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
    @app_commands.autocomplete(playlist=_playlist_autocomplete)
    async def _playlist_show(
        self,
        ctx,
        playlist: str,
    ):
        await ctx.defer()

        try:
            contents = await playlists.get(
                ctx.bot.DbSession,
                _playlist_ref(ctx, playlist),
            )
        except playlists.SavedPlaylistError as e:
            await ctx.send(_saved_playlist_error_reply(e))
            return
        if contents is None:
            await ctx.send(config.PLAYLIST_NOT_FOUND)
            return
        pages = []
        i = 1
        for part in chunks(contents.entries, 25):
            embed = Embed(title=contents.ref.name)
            for song in part:
                url = song.url
                title = song.title or utils.bare_url(url)
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
        try:
            await check_message_link(track, ctx.guild, ctx.author)
            result = await loader.load_song(track)
        except SongError as e:
            await ctx.send(e)
            return
        try:
            await playlists.add_songs(
                ctx.bot.DbSession,
                _playlist_ref(ctx, playlist),
                [
                    playlists.PlaylistEntry(s.webpage_url, s.title)
                    for s in result.songs
                ],
            )
        except playlists.SavedPlaylistError as e:
            await ctx.send(_saved_playlist_error_reply(e))
            return
        if result.truncation is not None:
            await ctx.send(result.truncation.notice(len(result.songs)))
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
        except playlists.SavedPlaylistError as e:
            await ctx.send(_saved_playlist_error_reply(e))
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
        except playlists.SavedPlaylistError as e:
            await ctx.send(_saved_playlist_error_reply(e))
            return
        await ctx.send(config.PLAYLIST_UPDATED)


async def setup(bot: MusicBot):
    await bot.add_cog(Music(bot))
