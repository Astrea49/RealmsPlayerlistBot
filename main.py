import asyncio
import contextlib
import datetime
import logging
import os
import typing
import uuid
from collections import defaultdict

import rpl_config

rpl_config.load()

import aiohttp
import discord_typings
import humanize
import interactions as ipy
import sentry_sdk
from interactions.ext import prefixed_commands as prefixed
from interactions.ext.sentry import HookedTask
from ordered_set import OrderedSet
from tortoise import Tortoise

import common.help_tools as help_tools
import common.models as models
import common.utils as utils
import db_settings
from common.classes import SemaphoreRedis, TimedDict
from common.realms_api import RealmsAPI
from common.xbox_api import XboxAPI, parse_profile_response

with contextlib.suppress(ImportError):
    import rook  # type: ignore

    if os.environ.get("ROOK_TOKEN"):
        rook.start(
            token=os.environ["ROOK_TOKEN"], labels={"env": os.environ["ROOK_ENV"]}
        )

logger = logging.getLogger("realms_bot")
logger.setLevel(logging.INFO)
handler = logging.FileHandler(
    filename=os.environ["LOG_FILE_PATH"], encoding="utf-8", mode="a"
)
handler.setFormatter(
    logging.Formatter("%(asctime)s:%(levelname)s:%(name)s: %(message)s")
)
logger.addHandler(handler)

ipy_logger = logging.getLogger("interactions")
ipy_logger.setLevel(logging.INFO)
ipy_logger.addHandler(handler)


def default_sentry_filter(
    event: dict[str, typing.Any], hint: dict[str, typing.Any]
) -> typing.Optional[dict[str, typing.Any]]:
    if "log_record" in hint:
        record: logging.LogRecord = hint["log_record"]
        if "interactions" in record.name or "realms_bot" in record.name:
            #  There are some logging messages that are not worth sending to sentry.
            if ": 403" in record.message:
                return None
            if ": 404" in record.message:
                return None
            if record.message.startswith("Ignoring exception in "):
                return None

    if "exc_info" in hint:
        exc_type, exc_value, tb = hint["exc_info"]
        if isinstance(exc_value, KeyboardInterrupt):
            #  We don't need to report a ctrl+c
            return None
    return event


# im so sorry
if not utils.TEST_MODE:
    ipy.Task.on_error_sentry_hook = HookedTask.on_error_sentry_hook
    sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], before_send=default_sentry_filter)


def to_proper_word(num: int) -> str:
    num_str = humanize.intword(num)
    if num_str.startswith("1.0"):
        num_str = num_str.replace("1.0", "1")
    return num_str


class RealmsPlayerlistBot(utils.RealmBotBase):
    @ipy.listen("startup")
    async def on_startup(self) -> None:
        self.xbox = XboxAPI()
        self.realms = RealmsAPI()

        await self.xbox.set_up.wait()

        profile = parse_profile_response(
            await self.xbox.fetch_profile_by_xuid(self.xbox.auth_mgr.xsts_token.xuid)
        )
        user = profile.profile_users[0]
        self.own_gamertag = next(s.value for s in user.settings if s.id == "Gamertag")

        self.session = aiohttp.ClientSession()
        headers = {
            "X-Authorization": os.environ["OPENXBL_KEY"],
            "Accept": "application/json",
            "Accept-Language": "en-US",
        }
        self.openxbl_session = aiohttp.ClientSession(headers=headers)

        self.fully_ready.set()

    @ipy.listen("ready")
    async def on_ready(self) -> None:
        utcnow = ipy.Timestamp.utcnow()
        time_format = f"<t:{int(utcnow.timestamp())}:f>"

        connect_msg = (
            f"Logged in at {time_format}!"
            if self.init_load is True
            else f"Reconnected at {time_format}!"
        )

        if not self.bot_owner:
            self.bot_owner = self.owner  # type: ignore
        if not self.bot_owner:
            self.bot_owner = await self.fetch_user(self.app.owner_id)  # type: ignore

        await self.bot_owner.send(connect_msg)

        self.init_load = False

        activity = ipy.Activity.create(
            name=f"around {to_proper_word(len(bot.guilds))} Realms",
            type=ipy.ActivityType.WATCHING,
        )

        await self.change_presence(activity=activity)

    @ipy.listen("disconnect")
    async def on_disconnect(self) -> None:
        # basically, this needs to be done as otherwise, when the bot reconnects,
        # redis may complain that a connection was closed by a peer
        # this isnt a great solution, but it should work
        with contextlib.suppress(Exception):
            await self.redis.connection_pool.disconnect(inuse_connections=True)

    @ipy.listen("resume")
    async def on_resume_func(self) -> None:
        activity = ipy.Activity.create(
            name=f"around {to_proper_word(len(bot.guilds))} Realms",
            type=ipy.ActivityType.WATCHING,
        )
        await self.change_presence(activity=activity)

    # technically, this is in ipy itself now, but its easier for my purposes to do this
    @ipy.listen("raw_application_command_permissions_update")
    async def i_like_my_events_very_raw(
        self, event: ipy.events.RawGatewayEvent
    ) -> None:
        data: discord_typings.GuildApplicationCommandPermissionData = event.data  # type: ignore

        guild_id = int(data["guild_id"])

        if not self.slash_perms_cache[guild_id]:
            await help_tools.process_bulk_slash_perms(self, guild_id)
            return

        cmds = help_tools.get_commands_for_scope_by_ids(self, guild_id)
        if cmd := cmds.get(int(data["id"])):
            self.slash_perms_cache[guild_id][
                int(data["id"])
            ] = help_tools.PermissionsResolver(
                cmd.default_member_permissions, guild_id, data["permissions"]  # type: ignore
            )

    @ipy.listen(is_default_listener=True)
    async def on_error(self, event: ipy.events.Error) -> None:
        await utils.error_handle(event.error)

    def mention_cmd(self, name: str, scope: int = 0) -> str:
        return self.interactions_by_scope[scope][name].mention(scope)

    def create_task(self, coro: typing.Coroutine) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    def load_extension(
        self, name: str, package: str | None = None, **load_kwargs: typing.Any
    ) -> None:
        super().load_extension(name, package, **load_kwargs)

        # ipy forgets to do this lol
        if not self.sync_ext and self._ready.is_set():
            asyncio.create_task(self._cache_interactions(warn_missing=False))

    @property
    def start_time(self) -> datetime.datetime:
        try:
            return self._connection_states[0].start_time  # type: ignore
        except IndexError:
            return ipy.MISSING

    async def stop(self) -> None:
        await bot.session.close()
        await bot.realms.close()
        await Tortoise.close_connections()
        await bot.redis.close(close_connection_pool=True)

        return await super().stop()


intents = ipy.Intents.new(
    guilds=True,
    messages=True,
)
mentions = ipy.AllowedMentions.all()

bot = RealmsPlayerlistBot(
    sync_interactions=False,  # big bots really shouldn't have this on
    sync_ext=False,
    allowed_mentions=mentions,
    intents=intents,
    interaction_context=utils.RealmInteractionContext,
    slash_context=utils.RealmContext,
    component_context=utils.RealmComponentContext,
    modal_context=utils.RealmModalContext,
    context_menu_context=utils.RealmContextMenuContext,
    autocomplete_context=utils.RealmAutocompleteContext,
    auto_defer=ipy.AutoDefer(enabled=True, time_until_defer=0),
    # we do not need messages
    message_cache=ipy.utils.TTLCache(10, 10, 50),
    # we don't need that many
    user_cache=ipy.utils.TTLCache(60, 500, 1000),
    # do not need at all
    voice_state_cache=ipy.utils.NullCache(),
    user_guilds=ipy.utils.NullCache(),
    dm_channels=ipy.utils.NullCache(),
    # lets not make this too huge
    channel_cache=ipy.utils.TTLCache(600, 5000, 10000),
    logger=logger,
)
prefixed.setup(bot, prefixed_context=utils.RealmPrefixedContext)
bot.init_load = True
bot.bot_owner = None  # type: ignore
bot.color = ipy.Color(int(os.environ["BOT_COLOR"]))  # b05bff, aka 11557887
bot.online_cache = defaultdict(set)
bot.slash_perms_cache = defaultdict(dict)
bot.live_playerlist_store = defaultdict(set)
bot.uuid_cache = defaultdict(uuid.uuid4)
bot.mini_commands_per_scope = {}
bot.offline_realms = OrderedSet()  # type: ignore
bot.dropped_offline_realms = set()
bot.background_tasks = set()

# TODO: remove once discord patches this properly
from interactions.client import const

const.CLIENT_FEATURE_FLAGS["FOLLOWUP_INTERACTIONS_FOR_IMAGES"] = True


async def start() -> None:
    await Tortoise.init(db_settings.TORTOISE_ORM)
    bot.redis = SemaphoreRedis.from_url(
        os.environ["REDIS_URL"], decode_responses=True, semaphore_value=15
    )

    # mark players as offline if they were online more than 5 minutes ago
    five_minutes_ago = ipy.Timestamp.utcnow() - datetime.timedelta(minutes=5)
    await models.PlayerSession.filter(
        online=True, last_seen__lt=five_minutes_ago
    ).update(online=False)

    # add all online players to the online cache
    async for player in models.PlayerSession.filter(online=True):
        bot.uuid_cache[player.realm_xuid_id] = player.custom_id
        bot.online_cache[int(player.realm_id)].add(player.xuid)

    if not utils.TEST_MODE:
        async for realm_id in bot.redis.scan_iter("missing-realm-*"):
            bot.offline_realms.add(int(realm_id.removeprefix("missing-realm-")))

    # add info for who has live playerlist on, as we can't rely on anything other than
    # pure memory for the playerlist getting code
    async for config in models.GuildConfig.filter(
        premium_code__id__not_isnull=True,
        realm_id__not_isnull=True,
        playerlist_chan__not_isnull=True,
        live_playerlist=True,
    ).prefetch_related("premium_code"):
        bot.live_playerlist_store[config.realm_id].add(config.guild_id)  # type: ignore

    bot.realm_name_cache = TimedDict(expires=300)
    bot.fully_ready = asyncio.Event()
    bot.pl_sem = asyncio.Semaphore(12)

    ext_list = utils.get_all_extensions(os.environ["DIRECTORY_OF_BOT"])
    for ext in ext_list:
        # skip loading voting ext if token doesn't exist
        if "voting" in ext and not os.environ.get("TOP_GG_TOKEN"):
            continue

        if utils.TEST_MODE and ("autorun" in ext or "etc" in ext):
            continue

        try:
            bot.load_extension(ext)
        except ipy.errors.ExtensionLoadException:
            raise

    with contextlib.suppress(asyncio.CancelledError):
        await bot.astart(os.environ["MAIN_TOKEN"])


if __name__ == "__main__":
    loop_factory = None

    with contextlib.suppress(ImportError):
        import uvloop  # type: ignore

        loop_factory = uvloop.new_event_loop

    with asyncio.Runner(loop_factory=loop_factory) as runner:
        asyncio.run(start())
