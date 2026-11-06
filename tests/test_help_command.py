"""The bot's help lists every command that is not hidden, whatever the
member's voice state, the bot's playback state, the channel or the DJ
role, and never runs admission."""

import discord
import pytest

from musicbot.admission import RequestKind
from musicbot.bot import MusicBot
from musicbot.utils import owner_check
from discord_fakes import situation
from helpers import run

EXTENSIONS = [
    "musicbot.commands.music",
    "musicbot.commands.general",
    "musicbot.commands.developer",
    "musicbot.commands.library",
]

DJ_ROLE = 12345

NAMED_BY_THE_ISSUE = {
    "play",
    "playnext",
    "search",
    "loop",
    "shuffle",
    "queue",
    "move",
    "remove",
    "skip",
    "restore",
    "prev",
    "songinfo",
}


@pytest.fixture(scope="module")
def bot():
    built = MusicBot(
        [], command_prefix="d!", intents=discord.Intents.default()
    )

    async def load():
        for extension in EXTENSIONS:
            await built.load_extension(extension)

    run(load())
    return built


def refusing_context(bot):
    """A context admission refuses: author not in voice, bot idle with no
    voice client, another channel than the command channel, and a DJ role
    the author does not hold."""
    guild, ctx = situation(
        RequestKind.JOINING_COMMAND,
        bot=None,
        author=None,
        wrong_channel=True,
        dj_role=DJ_ROLE,
    )
    ctx.bot.can_run = bot.can_run
    ctx.bot.get_command = bot.get_command
    return ctx


def help_for(bot, ctx):
    "The bot's help command as it runs: a copy with the context set"
    help_command = bot._default_help._original.copy()
    help_command.context = ctx
    return help_command


def listed(bot, ctx, candidates):
    help_command = help_for(bot, ctx)
    return {
        c.qualified_name
        for c in run(help_command.filter_commands(candidates, sort=True))
    }


def non_hidden(group):
    return {c.qualified_name for c in group.commands if not c.hidden}


def test_help_lists_commands_admission_would_refuse(bot):
    ctx = refusing_context(bot)
    music = bot.get_cog("Music")
    shown = listed(bot, ctx, music.get_commands())
    assert NAMED_BY_THE_ISSUE <= shown


@pytest.mark.parametrize("name", ["playlist", "setting"])
def test_group_help_lists_every_non_hidden_subcommand(bot, name):
    group = bot.get_command(name)
    shown = listed(bot, refusing_context(bot), group.commands)
    assert shown == non_hidden(group) != set()


def test_help_leaves_no_admission_state_on_the_context(bot):
    ctx = refusing_context(bot)
    listed(bot, ctx, bot.get_cog("Music").get_commands())
    assert not hasattr(ctx, "admitted")
    assert not hasattr(ctx, "audiocontroller")


def test_help_for_a_hidden_owner_command_still_renders(bot):
    ctx = refusing_context(bot)
    sent = []

    async def send(content=None, **kwargs):
        sent.append(content)

    ctx.send = send
    ctx.clean_prefix = "d!"
    help_command = help_for(bot, ctx)
    command = bot.get_command("guild_whitelist add")
    assert command.hidden
    run(help_command.send_command_help(command))
    assert any("d!add" in m and "Arguments" in m for m in sent)


def walk(command):
    yield command
    for sub in getattr(command, "commands", ()):
        yield from walk(sub)


def test_every_owner_only_command_is_hidden(bot):
    unhidden = [
        c.qualified_name
        for top in bot.commands
        for c in walk(top)
        if owner_check in c.checks and not c.hidden
    ]
    assert unhidden == []
