"""The app-command tree is guild-only: Discord never offers slash
commands in DMs or private channels."""

import discord

from musicbot.bot import MusicBot


def test_built_bot_tree_is_guild_only():
    bot = MusicBot([], command_prefix="d!", intents=discord.Intents.default())
    contexts = bot.tree.allowed_contexts
    assert (contexts.guild, contexts.dm_channel, contexts.private_channel) == (
        True,
        False,
        False,
    )
