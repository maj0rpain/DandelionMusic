import json
import os
import re
from dataclasses import dataclass
from inspect import isawaitable
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Union,
)

import discord
from discord import (
    TextChannel,
    VoiceChannel,
    Thread,
    Role,
    Forbidden,
    HTTPException,
    utils,
)
import sqlalchemy
from sqlalchemy import String, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from alembic.migration import MigrationContext
from alembic.autogenerate import produce_migrations, render_python_code
from alembic.operations import Operations, ops as alembic_ops
from typing_extensions import Annotated

from config import config
from musicbot.utils import StrEnum, get_emoji

# avoiding circular import
if TYPE_CHECKING:
    from musicbot.bot import MusicBot, Context

DIR_PATH = os.path.dirname(os.path.realpath(__file__))
LEGACY_SETTINGS = DIR_PATH + "/generated/settings.json"
# use String for ids to be sure we won't hit overflow
ID_LENGTH = 25  # more than enough to be sure :)
DiscordIdStr = Annotated[str, ID_LENGTH]


class Base(DeclarativeBase):
    type_annotation_map = {
        DiscordIdStr: String(ID_LENGTH),
    }


ConversionErrorText = StrEnum(
    "ConversionErrorText", config.get_dict("ConversionError")
)
SettingsEmbed = StrEnum("SettingsEmbed", config.get_dict("SettingsEmbed"))


class ConversionError(Exception):
    pass


async def convert_emoji(ctx: "Context", value: Optional[str]) -> Optional[str]:
    if not config.ENABLE_BUTTON_PLUGIN:
        raise ConversionError(ConversionErrorText.BUTTON_DISABLED)

    if value is None:
        return None

    ids = re.findall(r"\d{15,20}", value)
    emoji = (
        (utils.get(ctx.bot.emojis, id=int(ids[-1])) if ids else None)
        or utils.get(ctx.guild.emojis, name=value)
        or value
    )

    msg = await ctx.send(config.SETTINGS_EMOJI_CHECK_MSG)
    if isinstance(msg, discord.Interaction):
        msg = await msg.original_response()
    try:
        await msg.add_reaction(emoji)
    except Forbidden as e:
        raise ConversionError(ConversionErrorText.NO_REACTION_PERMS) from e
    except HTTPException as e:
        if e.code == 10014:  # Unknown Emoji
            raise ConversionError(ConversionErrorText.INVALID_EMOJI) from e
        raise

    if isinstance(emoji, discord.Emoji):
        emoji = str(emoji.id)
    return emoji


def convert_object(
    ctx: "Context", value: Optional[discord.Object]
) -> Optional[str]:
    if value is None:
        return None

    return str(value.id)


def convert_bool(ctx: "Context", value: bool) -> bool:
    return value


def convert_volume(ctx: "Context", value: int) -> int:
    if value > 100 or value < 0:
        raise ConversionError(ConversionErrorText.INVALID_VOLUME)
    return value


def _name_or_none(found) -> Optional[str]:
    return found.name if found else None


def _show_channel_or_thread(ctx: "Context", stored: str) -> Optional[str]:
    return _name_or_none(ctx.guild.get_channel_or_thread(int(stored)))


def _show_channel(ctx: "Context", stored: str) -> Optional[str]:
    return _name_or_none(ctx.guild.get_channel(int(stored)))


def _show_role(ctx: "Context", stored: str) -> Optional[str]:
    return _name_or_none(ctx.guild.get_role(int(stored)))


def _show_emoji(ctx: "Context", stored: str):
    return get_emoji(ctx.bot, stored)


def _show_raw(ctx: "Context", stored: Any):
    return stored


def _confirm_mention(value: Any) -> str:
    return value.mention


def _confirm_name(value: Any) -> str:
    return value.name


def _confirm_raw(value: Any) -> Any:
    return value


def _vc_timeout_gate() -> Optional[str]:
    if config.ALLOW_VC_TIMEOUT_EDIT:
        return None
    return config.VC_TIMEOUT_EDIT_DISABLED


@dataclass(frozen=True)
class SettingDescriptor:
    """Everything about one guild setting except its database column.

    The column stays hand-written on GuildSettings: Alembic
    autogeneration diffs against it, so it must not be generated.
    """

    name: str
    # the value a new guild starts with
    default: Any
    # the Discord type of the subcommand's parameter
    param_type: Any
    # the subcommand's parameter name
    param_name: str
    # Discord value -> stored value; may be async and may raise
    # ConversionError
    converter: Callable[["Context", Any], Any]
    # (ctx, stored value) -> what the settings embed shows,
    # or None when the stored value no longer resolves
    display: Callable[["Context", Any], Any]
    # shown instead when display returns None
    invalid: Optional[str]
    # Discord value -> how the success confirmation shows it
    confirm: Callable[[Any], Any]
    # returns the refusal message while the setting may not be
    # edited, None otherwise
    edit_gate: Optional[Callable[[], Optional[str]]] = None

    def show(self, ctx: "Context", stored: Any) -> Any:
        "The settings embed's value for this setting"
        if not stored:
            return SettingsEmbed.FIELD_EMPTY
        shown = self.display(ctx, stored)
        return self.invalid if shown is None else shown


SETTINGS: Dict[str, SettingDescriptor] = {
    d.name: d
    for d in (
        SettingDescriptor(
            name="command_channel",
            default=None,
            param_type=Union[Thread, VoiceChannel, TextChannel],
            param_name="channel",
            converter=convert_object,
            display=_show_channel_or_thread,
            invalid=SettingsEmbed.INVALID_CHANNEL,
            confirm=_confirm_mention,
        ),
        SettingDescriptor(
            name="start_voice_channel",
            default=None,
            param_type=VoiceChannel,
            param_name="channel",
            converter=convert_object,
            display=_show_channel,
            invalid=SettingsEmbed.INVALID_VOICE_CHANNEL,
            confirm=_confirm_mention,
        ),
        SettingDescriptor(
            name="dj_role",
            default=None,
            param_type=Role,
            param_name="role",
            converter=convert_object,
            display=_show_role,
            invalid=SettingsEmbed.INVALID_ROLE,
            confirm=_confirm_name,
        ),
        SettingDescriptor(
            name="user_must_be_in_vc",
            default=True,
            param_type=bool,
            param_name="value",
            converter=convert_bool,
            display=_show_raw,
            invalid=None,
            confirm=_confirm_raw,
        ),
        SettingDescriptor(
            name="button_emote",
            default=None,
            param_type=str,
            param_name="emoji",
            converter=convert_emoji,
            display=_show_emoji,
            invalid=SettingsEmbed.INVALID_EMOJI,
            confirm=_confirm_raw,
        ),
        SettingDescriptor(
            name="default_volume",
            default=100,
            param_type=int,
            param_name="value",
            converter=convert_volume,
            display=_show_raw,
            invalid=None,
            confirm=_confirm_raw,
        ),
        SettingDescriptor(
            name="vc_timeout",
            default=config.VC_TIMEOUT_DEFAULT,
            param_type=bool,
            param_name="value",
            converter=convert_bool,
            display=_show_raw,
            invalid=None,
            confirm=_confirm_raw,
            edit_gate=_vc_timeout_gate,
        ),
        SettingDescriptor(
            name="announce_songs",
            default=False,
            param_type=bool,
            param_name="value",
            converter=convert_bool,
            display=_show_raw,
            invalid=None,
            confirm=_confirm_raw,
        ),
    )
}


def default_settings() -> Dict[str, Any]:
    "A fresh copy of every setting's default, keyed by setting name"
    return {name: d.default for name, d in SETTINGS.items()}


class GuildSettings(Base):
    __tablename__ = "settings"

    guild_id: Mapped[DiscordIdStr] = mapped_column(primary_key=True)
    command_channel: Mapped[Optional[DiscordIdStr]]
    start_voice_channel: Mapped[Optional[DiscordIdStr]]
    dj_role: Mapped[Optional[DiscordIdStr]]
    user_must_be_in_vc: Mapped[bool]
    button_emote: Mapped[Optional[DiscordIdStr]]
    default_volume: Mapped[int]
    vc_timeout: Mapped[bool]
    announce_songs: Mapped[bool] = mapped_column(
        server_default=sqlalchemy.false()
    )

    @classmethod
    async def load(
        cls, bot: "MusicBot", guild: discord.Guild
    ) -> "GuildSettings":
        "Load object from database or create a new one and commit it"
        guild_id = str(guild.id)
        async with bot.DbSession() as session:
            sett = (
                await session.execute(
                    select(GuildSettings).where(
                        GuildSettings.guild_id == guild_id
                    )
                )
            ).scalar_one_or_none()
            if sett:
                return sett
            session.add(GuildSettings(guild_id=guild_id, **default_settings()))
            # avoiding incomplete detached object
            sett = (
                await session.execute(
                    select(GuildSettings).where(
                        GuildSettings.guild_id == guild_id
                    )
                )
            ).scalar_one()
            await session.commit()
            return sett

    @classmethod
    async def load_many(
        cls, bot: "MusicBot", guilds: List[discord.Guild]
    ) -> Dict[discord.Guild, "GuildSettings"]:
        """Load list of objects from database
        Creates new ones when not found
        Returns dict with guilds as keys and their settings as values"""
        ids = [str(g.id) for g in guilds]
        async with bot.DbSession() as session:
            settings = (
                (
                    await session.execute(
                        select(GuildSettings).where(
                            GuildSettings.guild_id.in_(ids)
                        )
                    )
                )
                .scalars()
                .fetchall()
            )
            missing = set(ids) - {sett.guild_id for sett in settings}
            for new_id in missing:
                session.add(
                    GuildSettings(guild_id=new_id, **default_settings())
                )
            settings.extend(
                (
                    await session.execute(
                        select(GuildSettings).where(
                            GuildSettings.guild_id.in_(missing)
                        )
                    )
                )
                .scalars()
                .fetchall()
            )
            await session.commit()
        # ensure the correct order
        settings.sort(key=lambda x: ids.index(x.guild_id))
        return {g: sett for g, sett in zip(guilds, settings)}

    def format(self, ctx: "Context"):
        embed = discord.Embed(
            title=SettingsEmbed.TITLE,
            description=ctx.guild.name,
            color=config.EMBED_COLOR,
        )

        if ctx.guild.icon:
            embed.set_thumbnail(url=ctx.guild.icon.url)
        embed.set_footer(text=SettingsEmbed.FOOTER)

        for name, descriptor in SETTINGS.items():
            embed.add_field(
                name=name,
                value=descriptor.show(ctx, getattr(self, name)),
                inline=False,
            )

        return embed

    async def update_setting(
        self, setting: str, value: Any, ctx: "Context"
    ) -> bool:
        descriptor = SETTINGS.get(setting)
        if descriptor is None:
            return False

        value = descriptor.converter(ctx, value)
        if isawaitable(value):
            value = await value
        setattr(self, setting, value)
        async with ctx.bot.DbSession() as session:
            session.add(self)
            await session.commit()
        return True


class SavedPlaylist(Base):
    __tablename__ = "playlists"

    guild_id: Mapped[DiscordIdStr] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(primary_key=True)
    songs_json: Mapped[str]


class WhitelistedGuild(Base):
    """One row per guild the bot is allowed to stay in.

    This used to be config.GUILD_WHITELIST, a list in .env that
    d!guild_whitelist rewrote through Config.save(). Writing a config
    file back out at runtime turned out to be the single buggiest
    thing in the project - a leaked BOT_TOKEN, a colour that changed on
    every save, a tuple setting that made the next startup fail, writes
    landing in the wrong directory, and a removal that silently did not
    persist - so the whitelist lives here instead and the config file
    is read-only again.
    """

    __tablename__ = "guild_whitelist"

    guild_id: Mapped[DiscordIdStr] = mapped_column(primary_key=True)


class BotState(Base):
    """Small key/value store for bot-wide state that is not a setting.

    Currently just the marker recording that the deprecated
    GUILD_WHITELIST environment variable has been imported.
    """

    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    # explicit length: MySQL rejects VARCHAR without one. The same
    # applies to SavedPlaylist.name/songs_json above, which have
    # always lacked it - so the mysql extra has never been able to
    # create a schema. Fixing those needs a reviewed migration
    # (run_migrations refuses AlterColumnOp by design), so it is left
    # alone here rather than breaking every sqlite deployment.
    value: Mapped[str] = mapped_column(String(255))


# marker key for import_env_whitelist() below
ENV_WHITELIST_IMPORTED = "env_guild_whitelist_imported"


async def get_guild_whitelist(bot: "MusicBot") -> Set[int]:
    """Guild ids the bot may stay in. Empty means "no whitelist".

    Read straight from the database on every use rather than cached on
    the bot. The whitelist is consulted only on connect and on joining
    a guild, so the query is free at that rate - and a cache would have
    to be kept in step with the commands that edit it, which is exactly
    the kind of second copy that produced the bugs this table replaces.
    """
    async with bot.DbSession() as session:
        rows = (
            (await session.execute(select(WhitelistedGuild.guild_id)))
            .scalars()
            .all()
        )
    return {int(guild_id) for guild_id in rows}


async def add_to_guild_whitelist(bot: "MusicBot", guild_id: int) -> bool:
    """Returns False if the guild was already whitelisted."""
    async with bot.DbSession() as session:
        session.add(WhitelistedGuild(guild_id=str(guild_id)))
        try:
            await session.commit()
        except IntegrityError:
            return False
    return True


async def remove_from_guild_whitelist(bot: "MusicBot", guild_id: int) -> bool:
    """Returns False if the guild was not whitelisted."""
    async with bot.DbSession() as session:
        result = await session.execute(
            delete(WhitelistedGuild).where(
                WhitelistedGuild.guild_id == str(guild_id)
            )
        )
        await session.commit()
    return result.rowcount > 0


async def import_env_whitelist(bot: "MusicBot"):
    """Seed the whitelist table from the deprecated GUILD_WHITELIST
    environment variable, once.

    Guarded by a marker row rather than by the table being empty:
    emptying the whitelist through d!guild_whitelist is a legitimate
    state, and re-importing whenever the table is empty would resurrect
    every id on the next restart - the same failure the move to the
    database is meant to end.
    """
    async with bot.DbSession() as session:
        if await session.get(BotState, ENV_WHITELIST_IMPORTED) is not None:
            return
        # Skip ids the table already holds rather than assuming a
        # missing marker means an empty table. Two processes sharing
        # one database can both pass the check above, and a restored
        # backup can have rows without the marker; inserting blindly
        # then raises IntegrityError out of MusicBot.start() and the
        # bot never logs in.
        existing = set(
            (await session.execute(select(WhitelistedGuild.guild_id)))
            .scalars()
            .all()
        )
        for guild_id in config.GUILD_WHITELIST:
            if str(guild_id) not in existing:
                session.add(WhitelistedGuild(guild_id=str(guild_id)))
        session.add(
            BotState(
                key=ENV_WHITELIST_IMPORTED,
                value=str(len(config.GUILD_WHITELIST)),
            )
        )
        await session.commit()
    if config.GUILD_WHITELIST:
        print(
            f"Imported {len(config.GUILD_WHITELIST)} guild(s) from the"
            " GUILD_WHITELIST environment variable into the database."
            " That variable is no longer read after this point - manage"
            " the whitelist with d!guild_whitelist and remove it from"
            " your .env."
        )


# Operation types that only ever add to the schema (new table,
# new column) and can never lose existing data. Anything else
# (dropped/renamed/altered tables or columns) is refused below -
# allow-listed rather than deny-listed, so an unrecognized or
# future Alembic op type is treated as unsafe by default.
SAFE_MIGRATION_OPS = (
    alembic_ops.CreateTableOp,
    alembic_ops.AddColumnOp,
)


def _find_unsafe_ops(op):
    """Yields any migration operation that isn't a known-additive
    schema change."""
    if isinstance(op, alembic_ops.OpContainer):
        for child in op.ops:
            yield from _find_unsafe_ops(child)
    elif not isinstance(op, SAFE_MIGRATION_OPS):
        yield op


def run_migrations(connection):
    """Automatically creates tables and adds columns to reflect
    additive code changes. Refuses to start if the generated diff
    contains anything destructive (a dropped/renamed/altered table
    or column) instead of silently applying it - that needs a
    manual, reviewed migration."""
    ctx = MigrationContext.configure(connection)
    upgrade_ops = produce_migrations(ctx, Base.metadata).upgrade_ops

    unsafe_ops = [type(op).__name__ for op in _find_unsafe_ops(upgrade_ops)]
    if unsafe_ops:
        raise RuntimeError(
            "Refusing to start: the database schema differs from the "
            "code in a way that isn't a plain additive change "
            f"({', '.join(unsafe_ops)}). This usually means a column "
            "or table was renamed or removed. Back up the database "
            "and apply the schema change manually, then restart."
        )

    code = render_python_code(upgrade_ops, migration_context=ctx)
    if connection.engine.echo:
        # debug mode
        print(code)
    with Operations.context(ctx) as op:
        variables = {"op": op, "sa": sqlalchemy}
        exec("def run():\n" + code, variables)
        variables["run"]()
    connection.commit()


async def extract_legacy_settings(bot: "MusicBot"):
    "Load settings from deprecated json file to DB"
    if not os.path.isfile(LEGACY_SETTINGS):
        return
    with open(LEGACY_SETTINGS) as file:
        json_data = json.load(file)
    async with bot.DbSession() as session:
        existing = (
            (
                await session.execute(
                    select(GuildSettings.guild_id).where(
                        GuildSettings.guild_id.in_(list(json_data))
                    )
                )
            )
            .scalars()
            .fetchall()
        )
        for guild_id, data in json_data.items():
            if guild_id in existing:
                continue
            new_settings = default_settings()
            new_settings.update(
                {k: v for k, v in data.items() if k in new_settings}
            )
            session.add(GuildSettings(guild_id=guild_id, **new_settings))
        await session.commit()
    os.rename(LEGACY_SETTINGS, LEGACY_SETTINGS + ".back")


async def migrate_old_playlists(bot: "MusicBot"):
    async with bot.DbSession() as session:
        playlists = (
            (await session.execute(select(SavedPlaylist))).scalars().fetchall()
        )
        for playlist in playlists:
            songs = json.loads(playlist.songs_json)
            for i, song in enumerate(songs):
                if isinstance(song, str):
                    songs[i] = {"url": song, "title": None}
            playlist.songs_json = json.dumps(songs)
        await session.commit()
