"""The app-command tree is guild-only: Discord never offers slash
commands in DMs or private channels."""

from discord_fakes import real_music_bot


def test_built_bot_tree_is_guild_only():
    bot = real_music_bot()
    contexts = bot.tree.allowed_contexts
    assert (contexts.guild, contexts.dm_channel, contexts.private_channel) == (
        True,
        False,
        False,
    )
